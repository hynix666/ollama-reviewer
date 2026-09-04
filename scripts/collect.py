"""Input collection and validation: git diffs, files, or stdin.

Everything the reviewer sees passes through here first. The job is to reject or
truncate bad input loudly, and to split large inputs at file boundaries so the
model never sees half a function.
"""

import os
import subprocess
import sys

BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".pdf", ".zip",
    ".gz", ".tar", ".7z", ".rar", ".exe", ".dll", ".so", ".dylib", ".bin",
    ".pyc", ".class", ".jar", ".woff", ".woff2", ".ttf", ".otf", ".mp3",
    ".mp4", ".mov", ".avi", ".wasm", ".db", ".sqlite",
}


# Prose and generated lockfiles. The reviewer's prompt is code-specific, so
# a README costs a real file its turn on a shared deadline.
PROSE_EXTS = {".md", ".markdown", ".rst", ".txt", ".adoc", ".org", ".tex"}
LOCKFILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "cargo.lock", "composer.lock", "gemfile.lock", "go.sum", "uv.lock",
}


def is_prose(path):
    base = os.path.basename(path.replace("\\", "/")).lower()
    return os.path.splitext(base)[1] in PROSE_EXTS or base in LOCKFILES


def apply_code_filter(kept, skipped, warnings, code_only):
    """Drop prose and lockfiles, unless that would leave nothing to review.

    Returning an empty set would turn "your diff is all docs" into an error,
    which is worse than just reviewing the docs. So the filter yields rather
    than blocks, and says so.
    """
    if not code_only:
        return kept
    code = [(label, text) for label, text in kept if not is_prose(label)]
    if not code:
        if kept:
            warnings.append(
                "Nothing but prose or lockfiles here, so those were reviewed "
                "anyway; the filter yields rather than leaving you with nothing.")
        return kept
    for label, _ in kept:
        if is_prose(label):
            skipped.append({"path": label, "reason": "prose or lockfile; --all-files includes it"})
    return code


class InputError(Exception):
    def __init__(self, detail, remedy):
        super().__init__(detail)
        self.detail = detail
        self.remedy = remedy

    def to_dict(self):
        return {"kind": "input", "detail": self.detail, "remedy": self.remedy}


class Chunk:
    """One reviewable unit - normally a single file or a single file's diff."""

    def __init__(self, label, text, truncated=False):
        self.label = label
        self.text = text
        self.truncated = truncated


class InputSet:
    def __init__(self, kind, chunks, warnings=None, skipped=None):
        self.kind = kind
        self.chunks = chunks
        self.warnings = warnings or []
        self.skipped = skipped or []

    @property
    def total_chars(self):
        return sum(len(c.text) for c in self.chunks)

    def summary(self):
        return {
            "kind": self.kind,
            "chunks": len(self.chunks),
            "chars": self.total_chars,
            "truncated_chunks": [c.label for c in self.chunks if c.truncated],
            "skipped": self.skipped,
            "warnings": self.warnings,
        }


def _run_git(args, cwd):
    """Run git, raising InputError with a useful message on any failure."""
    try:
        proc = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        raise InputError(
            "git is not installed or not on PATH.",
            "Install git, or review explicit paths with --file instead.",
        )
    except subprocess.TimeoutExpired:
        raise InputError(
            "git command timed out after 30s.",
            "The repository may be very large or an index lock may be held.",
        )
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        # Converts the rev-parse precondition into the typed not-a-repo error.
        if "not a git repository" in err.lower():
            raise InputError(
                "Not a git repository: %s" % cwd,
                "Run from inside a repo, or use --file PATH to review specific files.",
            )
        if "unknown revision" in err.lower() or "bad revision" in err.lower():
            raise InputError(
                "Unknown git revision: %s" % err[:200],
                "Check the branch or ref name with: git branch -a",
            )
        raise InputError(
            "git %s failed: %s" % (" ".join(args), err[:300] or "(no stderr)"),
            "Resolve the git error above, then retry.",
        )
    return proc.stdout


def split_diff_by_file(diff_text):
    """Split a unified diff into per-file sections, preserving each file's header."""
    if not diff_text.strip():
        return []
    sections = []
    current_label = None
    current = []
    for line in diff_text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current_label is not None:
                sections.append((current_label, "".join(current)))
            parts = line.split()
            current_label = parts[-1][2:] if len(parts) >= 4 else "unknown"
            current = [line]
        else:
            if current_label is None:
                current_label = "diff"
            current.append(line)
    if current_label is not None:
        sections.append((current_label, "".join(current)))
    return sections


def _cap(text, limit):
    if len(text) <= limit:
        return text, False
    head = text[:limit]
    marker = (
        "\n\n[TRUNCATED: %d of %d characters shown. The remainder was not sent to the "
        "reviewer - do not infer anything about the omitted portion.]\n" % (limit, len(text)))
    return head + marker, True


def _finalize(kind, raw_chunks, cfg, warnings, skipped):
    """Apply per-chunk and global caps, then build the InputSet."""
    max_file = int(cfg["max_file_chars"])
    max_files = int(cfg["max_files"])
    max_total = int(cfg["max_total_chars"])

    if len(raw_chunks) > max_files:
        dropped = [lbl for lbl, _ in raw_chunks[max_files:]]
        warnings.append(
            "Only the first %d of %d files were reviewed. Not reviewed: %s"
            % (max_files, len(raw_chunks), ", ".join(dropped[:12]))
            + (" ..." if len(dropped) > 12 else ""))
        raw_chunks = raw_chunks[:max_files]

    chunks = []
    running = 0
    for label, text in raw_chunks:
        capped, was_truncated = _cap(text, max_file)
        if running + len(capped) > max_total:
            warnings.append("Total size cap (%d chars) reached; stopped before %s."
                            % (max_total, label))
            break
        running += len(capped)
        chunks.append(Chunk(label, capped, was_truncated))

    if not chunks:
        raise InputError(
            "Nothing reviewable remained after validation.",
            "All candidate files were empty, binary, or over the size caps.",
        )
    return InputSet(kind, chunks, warnings, skipped)


def _untracked_sections(untracked, cwd):
    """Untracked files as (label, whole-file diff text) plus (label, reason) skips."""
    sections, skipped = [], []
    for u in untracked:
        reason, text, raw = None, None, None
        try:
            with open(os.path.join(cwd, u), "rb") as fh:
                raw = fh.read()
            if chr(0) in raw[:8192].decode("latin-1"):
                reason = "binary content"
        except OSError:
            reason = "unreadable"
        if reason is None:
            text = raw.decode("utf-8", errors="replace")
            if not text.strip():
                reason = "empty file"
        if reason:
            skipped.append((u, reason))
            continue
        header = ("diff --git a/{p} b/{p}\nnew file mode 100644\n"
                  "--- /dev/null\n+++ b/{p}\n@@ -0,0 +1,{n} @@").format(p=u, n=len(text.splitlines()))
        body = "".join("+" + line + "\n" for line in text.splitlines())
        sections.append((u, header + "\n" + body + "\n"))
    return sections, skipped


def _empty_diff_error(skipped_untracked):
    """Empty-diff error whose body names skipped untracked files and why."""
    detail = "The diff is empty - there is nothing to review."
    if skipped_untracked:
        rows = ["  %s (%s)" % (p, r) for p, r in skipped_untracked[:10]]
        if len(skipped_untracked) > 10:
            rows.append("  ... and %d more" % (len(skipped_untracked) - 10))
        detail += (" Untracked file(s) were found but none could be reviewed:"
                   + chr(10) + chr(10).join(rows))
    return InputError(
        detail,
        "Make some changes first, use --file PATH, or compare with --ref main. "
        "Untracked text files are picked up automatically for the default review.",
    )


def from_git(cfg, ref=None, staged=False, cwd=".", code_only=True):
    """Collect a diff: explicit ref, staged changes, or all uncommitted work."""
    # A stated --ref conflicts even when empty: MCP callers can send {"ref": ""}
    # and truthiness would silently let staged win.
    if ref is not None and staged:
        raise InputError(
            "--staged and --ref pick different scopes (--ref: REF...HEAD; "
            "--staged: the index). Pick one.",
            "Pass --staged alone, --ref REF alone, or neither for the "
            "default uncommitted-diff review.",
        )
    # Precondition for every scope: --staged would otherwise reach git's
    # --no-index mode outside a repo and relay noise, not this error.
    _run_git(["rev-parse", "--is-inside-work-tree"], cwd)
    warnings = []
    if ref:
        diff = _run_git(["diff", "--no-color", "%s...HEAD" % ref], cwd)
        kind = "git-diff (%s...HEAD)" % ref
    elif staged:
        diff = _run_git(["diff", "--no-color", "--staged"], cwd)
        kind = "git-diff (staged)"
    else:
        # No commits yet means no HEAD for `git diff HEAD`; probe, don't fail.
        head_ok = subprocess.run(["git", "rev-parse", "--verify", "--quiet", "HEAD"],
                                 cwd=cwd, capture_output=True).returncode == 0
        if head_ok:
            diff = _run_git(["diff", "--no-color", "HEAD"], cwd)
            kind = "git-diff (uncommitted)"
            if not diff.strip():
                diff = _run_git(["diff", "--no-color", "--staged"], cwd)
                if diff.strip():
                    kind = "git-diff (staged)"
                    warnings.append("No unstaged changes; reviewed the staged diff instead.")
        else:
            diff = ""
            kind = "git-diff (no commits yet)"
            warnings.append("Repository has no commits yet; reviewing untracked files only.")
        # `git diff` never shows untracked files. Scope is deliberate:
        # --staged can't include them; --ref compares commits they join neither of.
        out = _run_git(["ls-files", "--others", "--exclude-standard", "-z"], cwd)
        untracked = [u for u in out.split(chr(0)) if u]
        sections, skipped_untracked = _untracked_sections(untracked, cwd)
        warnings.extend("Untracked file %s skipped (%s)." % (p, r)
                        for p, r in skipped_untracked)
        included = [lbl for lbl, _ in sections]
        if diff and not diff.endswith("\n"):
            diff += "\n"
        diff += "".join(text for _, text in sections)
        listed = ", ".join(included[:5]) + (" ..." if len(included) > 5 else "")
        warnings.append("Included %d untracked file(s): %s. Use --staged or "
                        "--ref to restrict the scope." % (len(included), listed))
        kind += " + untracked"
        if not diff.strip() and skipped_untracked:
            # Nothing reviewable remained; the error body must say why.
            raise _empty_diff_error(skipped_untracked)


    if not diff.strip():
        raise _empty_diff_error([])

    sections = split_diff_by_file(diff)
    skipped = []
    kept = []
    for label, text in sections:
        ext = os.path.splitext(label)[1].lower()
        if ext in BINARY_EXTS:
            skipped.append({"path": label, "reason": "binary file type"})
            continue
        if "Binary files" in text and "differ" in text:
            skipped.append({"path": label, "reason": "git reports a binary diff"})
            continue
        kept.append((label, text))

    if not kept:
        detail = "The diff contains only binary files."
        if skipped:
            rows = ["  %s (%s)" % (s["path"], s["reason"]) for s in skipped[:10]]
            detail += " Skipped:" + chr(10) + chr(10).join(rows)
        raise InputError(
            detail,
            "Nothing textual to review. Use --file to point at source directly.",
        )
    kept = apply_code_filter(kept, skipped, warnings, code_only)
    return _finalize(kind, kept, cfg, warnings, skipped)


def from_files(cfg, paths, code_only=True):
    """Collect explicit file paths, rejecting missing, binary, and empty files."""
    kept = []
    skipped = []
    for p in paths:
        exists = os.path.exists(p)
        if not exists or os.path.isdir(p):
            skipped.append({"path": p, "reason": "does not exist" if not exists
                            else "is a directory (pass files, not dirs)"})
            continue
        if os.path.splitext(p)[1].lower() in BINARY_EXTS:
            skipped.append({"path": p, "reason": "binary file type"})
            continue
        try:
            with open(p, "rb") as fh:
                probe = fh.read(8192)
            if b"\x00" in probe:
                skipped.append({"path": p, "reason": "contains null bytes (binary)"})
                continue
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as e:
            skipped.append({"path": p, "reason": (
                "permission denied" if isinstance(e, PermissionError)
                else "read error: %s" % e)})
            continue
        if not text.strip():
            skipped.append({"path": p, "reason": "file is empty"})
            continue
        kept.append((p, text))

    if not kept:
        detail = "No reviewable files. " + "; ".join(
            "%s (%s)" % (s["path"], s["reason"]) for s in skipped[:10]
        )
        raise InputError(detail, "Check the paths and that they are text source files.")
    warnings = []
    kept = apply_code_filter(kept, skipped, warnings, code_only)
    return _finalize("files", kept, cfg, warnings, skipped)


def from_text(cfg, text, label="pasted-input", kind="pasted code", code_only=True):
    """Collect an in-memory string. Recognises a unified diff and splits it.

    A piped diff gets the same prose filter as one read from git; a single
    pasted snippet does not, since there is nothing to filter against.
    """
    if not (text or "").strip():
        raise InputError(
            "No code was provided.", "Pass a non-empty string to review."
        )
    if text.lstrip().startswith("diff --git "):
        sections = split_diff_by_file(text)
        if sections:
            warnings, skipped = [], []
            sections = apply_code_filter(sections, skipped, warnings, code_only)
            return _finalize(kind + " (diff)", sections, cfg, warnings, skipped)
    return _finalize(kind, [(label, text)], cfg, [], [])


def from_stdin(cfg, code_only=True):
    """Collect pasted code or a piped diff from stdin."""
    if sys.stdin is None or sys.stdin.isatty():
        raise InputError(
            "--stdin was given but no data was piped in.",
            "Pipe content, e.g.: git diff | ollama-review review --stdin",
        )
    return from_text(cfg, sys.stdin.read(), "pasted-input", "stdin", code_only)
