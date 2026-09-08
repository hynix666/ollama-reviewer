"""The selftest mutation marker: one home for its path and read-only state.

selftest owns the write side - it claims the marker while --mutate runs and
clears stale or dead-holder markers on contact, recovery mirroring the
dashboard pidfile lock. dashboard_status only reports, so everything here is
read-only by design: no clearing, no gating, no waiting. Standard library
only and never a mutation target, safe to import before the quiescence gate.
"""

import os
import re
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pidutil  # noqa: E402  sibling: holder liveness, same import safety

# Age at which a marker self-expires even when it gates. selftest gates and
# clears by the same number; this module only reports it.
AGE_BACKSTOP_S = 950


def path():
    """Marker path: the _SELFTEST_MUTATION_MARKER seam over the ambient default.

    The seam is read per call so tests can steer it after import, the way
    dashboard re-reads DASHBOARD_PIDFILE per call."""
    return os.environ.get("_SELFTEST_MUTATION_MARKER") or os.path.join(
        tempfile.gettempdir(), "selftest-mutating-"
        + re.sub(r"[^A-Za-z0-9]", "_", os.path.dirname(os.path.abspath(__file__)))[-48:])


def journal_path():
    """Path of the write-side journal that pairs with the marker: the
    original bytes of the module a --mutate run currently holds rewritten.
    Always path() + \".journal\", so the pair can never split."""
    return path() + ".journal"


def status():
    """Read-only marker state; never clears anything, never waits.

    Returns {"state", "holder", "holder_alive", "age_s", "path"}. state is
    what the next suite will see: "absent", "in flight" (the marker would
    gate it - a live holder, or unparseable content, which gates until the
    age backstop), or "stranded" (present but cleared on contact - a dead
    holder, or age past the backstop)."""
    p = path()
    st = {"state": "absent", "holder": None, "holder_alive": None,
          "age_s": None, "path": p,
          "journal": os.path.exists(journal_path())}
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            holder = fh.read().strip()
        age = int(time.time() - os.path.getmtime(p))
    except OSError:
        return st  # absent
    st["age_s"] = age
    if holder.isdigit():
        st["holder"] = int(holder)
        st["holder_alive"] = pidutil.pid_alive(int(holder))
    if age > AGE_BACKSTOP_S:
        st["state"] = "stranded"  # expired: the next suite clears it
        return st
    if holder.isdigit():
        st["state"] = "in flight" if st["holder_alive"] else "stranded"
    else:
        st["state"] = "in flight"  # unparseable content gates fresh, expires by age
    return st
