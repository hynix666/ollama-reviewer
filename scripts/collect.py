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
        "reviewer - do not infer anything about the omitted portion.]\n"
        % (limit, len(text))
    )
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
            + (" ..." if len(dropped) > 12 else "")
        )
        raw_chunks = raw_chunks[:max_files]

    chunks = []
    running = 0
    for label, text in raw_chunks:
        capped, was_truncated = _cap(text, max_file)
        if running + len(capped) > max_total:
            warnings.append(
                "Total size cap (%d chars) reached; stopped before %s."
                % (max_total, label)
            )
            break
        running += len(capped)
        chunks.append(Chunk(label, capped, was_truncated))

    if not chunks:
        raise InputError(
            "Nothing reviewable remained after validation.",
            "All candidate files were empty, binary, or over the size caps.",
        )
    return InputSet(kind, chunks, warnings, skipped)


def from_git(cfg, ref=None, staged=False, cwd="."):
    """Collect a diff: explicit ref, staged changes, or all uncommitted work."""
    warnings = []
    if ref:
        diff = _run_git(["diff", "--no-color", "%s...HEAD" % ref], cwd)
        kind = "git-diff (%s...HEAD)" % ref
    elif staged:
        diff = _run_git(["diff", "--no-color", "--staged"], cwd)
        kind = "git-diff (staged)"
    else:
        diff = _run_git(["diff", "--no-color", "HEAD"], cwd)
        kind = "git-diff (uncommitted)"
        if not diff.strip():
            diff = _run_git(["diff", "--no-color", "--staged"], cwd)
            if diff.strip():
                kind = "git-diff (staged)"
                warnings.append("No unstaged changes; reviewed the staged diff instead.")

    if not diff.strip():
        raise InputError(
            "The diff is empty - there is nothing to review.",
            "Make some changes first, or review specific files with --file PATH, "
            "or compare against a branch with --ref main.",
        )

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
        raise InputError(
            "The diff contains only binary files.",
            "Nothing textual to review. Use --file to point at source directly.",
        )
    return _finalize(kind, kept, cfg, warnings, skipped)


def from_files(cfg, paths):
    """Collect explicit file paths, rejecting missing, binary, and empty files."""
    kept = []
    skipped = []
    for p in paths:
        if not os.path.exists(p):
            skipped.append({"path": p, "reason": "does not exist"})
            continue
        if os.path.isdir(p):
            skipped.append({"path": p, "reason": "is a directory (pass files, not dirs)"})
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
        except PermissionError:
            skipped.append({"path": p, "reason": "permission denied"})
            continue
        except OSError as e:
            skipped.append({"path": p, "reason": "read error: %s" % e})
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
    return _finalize("files", kept, cfg, [], skipped)


def from_stdin(cfg):
    """Collect pasted code or a piped diff from stdin."""
    if sys.stdin is None or sys.stdin.isatty():
        raise InputError(
            "--stdin was given but no data was piped in.",
            "Pipe content, e.g.: git diff | ollama-review review --stdin",
        )
    text = sys.stdin.read()
    if not text.strip():
        raise InputError("stdin was empty.", "Pipe non-empty content and retry.")
    if text.lstrip().startswith("diff --git "):
        sections = split_diff_by_file(text)
        if sections:
            return _finalize("stdin-diff", sections, cfg, [], [])
    return _finalize("stdin", [("pasted-input", text)], cfg, [], [])
