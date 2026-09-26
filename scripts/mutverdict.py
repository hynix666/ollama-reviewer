#!/usr/bin/env python3
"""The mutate verdict: run selftest.py --mutate and read its transcript.

One job: apply the registry to this machine and turn the runner's summary
into the dashboard's verdict fields. The runner owns the wording (selftest's
_MUTATE_* lines); this module is the single reader of it, and a selftest
check pins the two against each other, so a reworded transcript fails loudly
instead of turning the page's verdict into a false NO VERDICT - or into a
regression that never happened.

A registry entry scoped to another platform is disclosed by the run and
counted here as an entry but not as caught, so "24 / 25 caught (1 windows-only,
scoped away here)" reads as the platform fact it is. A child that leaves no
verdict at all degrades to a warning the page renders, never a silent gap -
the gh_runs.py shape. Standard library only; never a mutation target.
"""

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def parse(text):
    """A --mutate transcript as (caught, total, scoped, scope, all_caught).

    total counts registry entries, so it stays ahead of caught by exactly the
    entries this platform scoped away. all_caught False with total 0 means the
    child printed no verdict at all (a crash), not a regression.
    """
    m = re.search(r"all (\d+) mutations caught", text)
    if not m:
        m = re.search(r"(\d+) of (\d+) mutations NOT caught", text)
        if not m:
            return 0, 0, 0, None, False
        return 0, int(m.group(2)), 0, None, False
    caught = int(m.group(1))
    sm = re.search(
        r"(\d+) of (\d+) entries declared (\w+)-only, inert on (\w+)", text)
    if sm:
        return caught, int(sm.group(2)), int(sm.group(1)), sm.group(3), True
    return caught, caught, 0, None, True


def _no_verdict(out, limit=400):
    """Page warning: exit code plus stream tails - a degraded cycle explains itself."""
    out_tail = (out.stdout or "").strip()[-limit:] or "<empty>"
    err_tail = (out.stderr or "").strip()[-limit:] or "<empty>"
    return ("the mutation registry run produced no verdict (exit=%d); stdout "
            "tail: %s | stderr tail: %s" % (out.returncode, out_tail, err_tail))


def run():
    """Run the registry child; (fields, note) - note explains a lost verdict.

    fields carries the page's verdict keys: mut_ok, mut_total, mut_caught,
    mut_scoped and mut_scope (the declared scope of any entry this platform
    scoped away). note is None when the child left a verdict, or a warning
    built from its exit code and stream tails when it did not.
    """
    argv = [sys.executable, os.path.join("scripts", "selftest.py"), "--mutate"]
    out = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True)
    caught, total, scoped, scope, ok = parse(out.stdout + out.stderr)
    fields = {"mut_ok": ok, "mut_total": total, "mut_caught": caught,
              "mut_scoped": scoped, "mut_scope": scope}
    return fields, None if total else _no_verdict(out)
