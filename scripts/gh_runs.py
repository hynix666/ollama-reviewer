"""GitHub Actions runs via the gh CLI: the dashboard's CI section.

One job: ask `gh` for the latest runs and their jobs, degrading to a
human-readable note instead of raising when gh is missing,
unauthenticated, offline, or the repo is unknown - the page renders the
note, never a silent gap. Standard library only; never a mutation target.
"""

import json
import os
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = "hynix666/ollama-reviewer"


def _gh_json(args, what):
    """Run one gh query with --repo REPO. Returns (payload, note); note None on success."""
    try:
        out = subprocess.run(["gh"] + args + ["--repo", REPO],
                             cwd=ROOT, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return None, "gh CLI not installed - install it and run 'gh auth login'"
    except Exception as e:  # network down, timeout
        return None, "gh %s unavailable: %s" % (what, e)
    if out.returncode != 0:
        return None, "gh %s failed: %s" % (what, out.stderr.strip() or out.returncode)
    try:
        return json.loads(out.stdout), None
    except ValueError as e:
        return None, "gh %s returned bad JSON: %s" % (what, e)


def ci_runs(n=3):
    """Latest GitHub Actions runs with their jobs, via the gh CLI.

    Returns (rows, note); note is None when gh worked, or a degradation
    explanation (gh missing, not authenticated, offline, repo not found)."""
    runs, note = _gh_json(
        ["run", "list", "--limit", str(n),
         "--json", "displayTitle,headSha,status,conclusion,event,createdAt,url"],
        "run list")
    if note:
        return [], note

    rows = []
    for r in runs:
        data, _note = _gh_json(["run", "view", r["url"].rsplit("/", 1)[-1],
                                "--json", "jobs"], "run view")
        rows.append({
            "title": r.get("displayTitle", ""), "sha": r.get("headSha", "")[:7],
            "event": r.get("event", ""), "status": r.get("status", ""),
            "conclusion": r.get("conclusion") or "",
            "created": (r.get("createdAt") or "")[:16].replace("T", " "),
            "url": r.get("url", ""),
            "jobs": [(j.get("name", ""), j.get("conclusion") or j.get("status", ""))
                     for j in (data or {}).get("jobs", [])],
        })
    return rows, None
