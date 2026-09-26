#!/usr/bin/env python3
"""The runner-image policy: what CI may pin, read twice.

One home for both halves of the rule, so neither can drift from the other:

  * Offline - every workflow's image labels must be in CHECKED_IMAGES below.
    This is the half the suite runs (selftest check "ci: runner images pinned
    to known labels"), and it stays offline on purpose: a local run and a
    docs.github.com hiccup must never red a pull request.
  * Live - the same labels must also appear in GitHub's published list of
    hosted-runner labels. That is what actually detects a retired or renamed
    image, and it runs as its own CI job (`runner_images.py --live`), where
    reading the network is the job's whole point and a failure is
    unambiguous.

Pinning keeps a green run meaning the same thing tomorrow: a floating -latest
retargets every workflow at once, mid-flight, with no PR to review. What it
costs is owed maintenance, and this is where that debt is paid: when GitHub
retires an image, the live job goes red naming it, and the fix is one PR that
pins the replacement and adds it to CHECKED_IMAGES. Notice of retirements is
published months ahead; the job exists so the notice cannot be missed.

Standard library only. Never a mutation target.
"""

import argparse
import os
import re
import sys
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REFERENCE_URL = ("https://docs.github.com/en/actions/reference/runners/"
                 "github-hosted-runners")

# The image labels this repo pins. Read this as the list, not a menu: a pin
# that is not here fails the suite, so adding one is a deliberate act, dated
# by the day the reference above was re-read against it.
CHECKED_IMAGES = ("ubuntu-22.04", "ubuntu-24.04", "windows-2025", "macos-26")

# Fewer labels than this inside the reference page's <code> spans means its
# shape changed, which is an error to report, never an empty set to compare
# against. The page lists 16 today; the margin is for GitHub reorganising
# prose, not for a page that stopped listing labels.
_LIVE_MIN = 8
_LABEL = re.compile(r"\b((?:ubuntu|windows|macos)-[0-9][A-Za-z0-9.\-]*)")
_CODE = re.compile(r"<code[^>]*>(.*?)</code>", re.S)


def workflow_paths(root=None):
    """Every workflow file under .github/workflows.

    All of them, not one hardcoded path: a second workflow asking for a
    floating image is the same failure as the first one doing it.
    """
    base = os.path.join(root or REPO_ROOT, ".github", "workflows")
    if not os.path.isdir(base):
        return []
    return sorted(os.path.join(base, name) for name in os.listdir(base)
                  if name.endswith((".yml", ".yaml")))


def _uncommented(line):
    """`line` with a trailing YAML comment removed (quotes respected)."""
    out, quote = [], None
    for ch in line:
        if quote:
            if ch == quote:
                quote = None
            out.append(ch)
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "#":
            break
        else:
            out.append(ch)
    return "".join(out)


def _scalar(value):
    """A YAML scalar: quotes stripped, a trailing comma or brace dropped."""
    v = value.strip().rstrip(",").strip()
    if len(v) > 1 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v.strip()


def _is_image(label):
    """True for a GitHub-hosted image label; self-hosted labels are not pins."""
    return label.startswith(("ubuntu-", "windows-", "macos-"))


def pins_in_text(path, text):
    """(path, line number, label) for every image `text` asks a job to run on.

    Tolerant on purpose, because these are shapes workflows legitimately
    write and none of them may become a false report: a `runs-on` value with
    a trailing comment, a quoted scalar, an `os:` entry inside an include,
    a bracketed `os` list carrying a comment, and comment lines - which are
    stripped rather than mined for text that looks like a label. `${{ ... }}`
    expressions and self-hosted labels are not image pins.
    """
    found = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = _uncommented(raw)
        m = re.search(r"(?:^|\s)runs-on:\s*(.+)$", line)
        if m:
            value = _scalar(m.group(1))
            if _is_image(value):
                found.append((path, n, value))
            continue
        m = re.search(r"(?:^|\s)os:\s*(.+)$", line)
        if not m:
            continue
        value = _scalar(m.group(1))
        if value.startswith("["):  # a bracketed list: `os: [a, b]`
            for item in value[1:].split("]", 1)[0].split(","):
                if _is_image(_scalar(item)):
                    found.append((path, n, _scalar(item)))
        else:  # an include entry: `{ os: a, python: "3.9" }`
            value = _scalar(value.split(",")[0])
            if _is_image(value):
                found.append((path, n, value))
    return found


def workflow_pins(paths=None):
    """Every image pin in `paths` (default: all workflow files)."""
    found = []
    for path in (workflow_paths() if paths is None else paths):
        with open(path, encoding="utf-8") as fh:
            found += pins_in_text(path, fh.read())
    return found


def job_blocks(text):
    """(job id, block) for each job: the 2-space keys under `jobs:`.

    Any id YAML allows at that indent, so an underscore id is a job and not a
    reason to report "0 jobs" - a failure has to name the job it cannot read.
    """
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.rstrip() == "jobs:"),
                 None)
    if start is None:
        return []
    jobs, current = [], None
    for ln in lines[start + 1:]:
        m = re.match(r"^  ([\w.\-]+):\s*$", ln)
        if m:
            if current:
                jobs.append((current[0], "\n".join(current[1])))
            current = [m.group(1), []]
        elif current is not None:
            current[1].append(ln)
    if current:
        jobs.append((current[0], "\n".join(current[1])))
    return jobs


def unknown(labels, known=None):
    """Labels that are not in `known`, sorted - one rule for both halves.

    `known` defaults to CHECKED_IMAGES at call time, so the vocabulary stays
    the one piece of state to change."""
    known = CHECKED_IMAGES if known is None else known
    return sorted({label for label in labels if label not in known})


def fetch_reference(url=REFERENCE_URL, timeout=20):
    """GitHub's published reference page as HTML. Network: live half only."""
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def live_labels(html):
    """Image labels GitHub currently publishes, as a set.

    Read from the `<code>` spans of the reference page, which is where it
    presents a runner label as a label: prose may name a retired image
    without offering it, and link targets carry `-Readme.md` suffixes -
    neither is a label, and counting either would let a dead pin pass. A page
    that no longer yields a plausible set is an error, never an empty set:
    "found nothing" must not read as "no drift".
    """
    found = set()
    for block in _CODE.findall(html):
        found.update(m.group(1).rstrip(".") for m in
                     _LABEL.finditer(re.sub(r"<[^>]+>", "", block)))
    families = {l.split("-", 1)[0] for l in found}
    if len(found) < _LIVE_MIN or families != {"ubuntu", "windows", "macos"}:
        raise RuntimeError(
            "the reference page yielded %d labels (%s) inside <code> spans - "
            "its shape changed, re-read %s"
            % (len(found), ", ".join(sorted(families)) or "none",
               REFERENCE_URL))
    return found


def main(argv=None):
    """Check the pins: offline by default, `--live` adds the network half."""
    ap = argparse.ArgumentParser(description=main.__doc__)
    ap.add_argument("--live", action="store_true",
                    help="also compare against GitHub's published reference "
                         "(network)")
    ap.add_argument("paths", nargs="*",
                    help="workflow files (default: all of .github/workflows)")
    args = ap.parse_args(argv)

    paths = args.paths or workflow_paths()
    if not paths:
        print("no workflow files found - nothing was checked")
        return 1
    pins = workflow_pins(paths)
    labels = sorted({label for _, _, label in pins})
    assert labels, "no image pins parsed out of %s" % ", ".join(paths)

    unchecked = unknown(labels)
    if unchecked:
        print("unchecked image(s) in %s: %s"
              % (", ".join(paths), ", ".join(unchecked)))
        print("fix: pin a checked image, or add the new one to CHECKED_IMAGES "
              "in scripts/runner_images.py in the same PR")
        return 1
    print("pinned: %s" % ", ".join(labels))

    if not args.live:
        return 0
    live = live_labels(fetch_reference())
    retired = unknown(labels, live)
    if retired:
        print("missing from GitHub's published labels (retired or renamed?): "
              "%s" % ", ".join(retired))
        print("fix: pin the replacement image and add it to CHECKED_IMAGES in "
              "the same PR - the suites on that image cannot start otherwise")
        return 1
    print("all %d pins are offered by GitHub today (%d labels published)"
          % (len(labels), len(live)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
