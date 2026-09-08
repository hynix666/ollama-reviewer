#!/usr/bin/env python3
"""Gather the repository's real state and render the HTML status dashboard.

Every number on the page is generated, not hand-typed: the check table is the
parsed output of a fresh selftest run, the commit list comes from git, and the
module table from the files on disk. Presentation lives in dashpage.py.
Run:  python scripts/dashboard.py [--live | --watch [--interval S] | --stop] [output.html]

Without --live the check table reflects `selftest.py --offline` (the CI view);
--live runs the full suite against a real Ollama server (a dead server renders
as an explicit warning). --watch regenerates
forever, guarded by a pidfile lock: a second watcher refuses to start, and a
dead watcher's stale pidfile is recovered automatically. An edit to any
watched source file (scripts/*.py, config.json) regenerates immediately
instead of waiting out the interval. --stop asks a running watcher to exit at its next safe point, in-flight
runs finishing first; --pause also renders one final fresh page, freezing
the artifact at a current state (footer "pause requested", no auto-refresh).

Writes the dashboard HTML (default: .freebuff/preview/dashboard.html). Pure stdlib.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dashpage  # noqa: E402
from pidutil import pid_alive  # noqa: E402  one home: selftest's marker recovery shares it
import mutation_marker  # noqa: E402  read-only marker state for dashboard_status
from gh_runs import ci_runs  # noqa: E402  GitHub Actions via gh CLI, one home

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(ROOT, ".freebuff", "preview", "dashboard.html")
# DASHBOARD_PIDFILE lets tests point spawned watchers at a scratch pidfile.
PID_PATH = os.environ.get("DASHBOARD_PIDFILE") or os.path.join(
    ROOT, ".freebuff", "preview", "dashboard.pid")
# How often the watch wait re-fingerprints the watched files between polls.
_POLL_S = 0.5


def sh(argv):
    out = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError("%s failed: %s" % (" ".join(argv), out.stderr.strip()))
    return out.stdout


def _degradation_note(what, returncode, stdout, stderr, limit=400):
    """Page warning: exit code plus stream tails - a degraded cycle explains itself."""
    out = (stdout or "").strip()[-limit:]
    err = (stderr or "").strip()[-limit:]
    return "%s produced no verdict (exit=%d); stdout tail: %s | stderr tail: %s" % (
        what, returncode, out or "<empty>", err or "<empty>")


def selftest_rows(live):
    """Run the suite; (rows, summary, note) - note warns when the child left no table."""
    argv = [sys.executable, os.path.join("scripts", "selftest.py"),
            "--live" if live else "--offline"]
    out = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True)
    rows, in_table, seps, summary = [], False, 0, ""
    for line in out.stdout.splitlines():
        if line.strip() and set(line.strip()) == {"-"}:
            in_table = (seps := seps + 1) == 1  # 1st rule opens the table, 2nd closes
        m = re.match(r"^(.*?)\s+(PASS|FAIL|SKIP)\s*(.*)$", line)
        if in_table and m:
            rows.append((m.group(1), m.group(2), m.group(3)))
        elif "passed" in line and "failed" in line:
            summary = line.strip()
    note = None
    if not rows:  # no table: the child died before or during the run
        note = _degradation_note("the %s selftest" % ("live" if live else "offline"),
                                 out.returncode, out.stdout, out.stderr)
    return rows, summary, note


def server_state():
    """Probe the configured Ollama server. Returns (up, n_models)."""
    import config
    cfg, _notes = config.load_config()
    url = cfg["base_url"].rstrip("/") + "/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            models = json.load(r).get("models", [])
        return True, len(models)
    except Exception:
        return False, 0


def mutation_result():
    """Run the mutation registry; (total, all_caught, note) - note = no verdict."""
    argv = [sys.executable, os.path.join("scripts", "selftest.py"), "--mutate"]
    out = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True)
    text = out.stdout + out.stderr
    m = re.search(r"all (\d+) mutations caught", text)
    if m:
        return int(m.group(1)), True, None
    m = re.search(r"(\d+) of (\d+) mutations NOT caught", text)
    if m:
        return int(m.group(2)), False, None
    return 0, False, _degradation_note("the mutation registry run", out.returncode,
                                       out.stdout, out.stderr)


def commits(n=10):
    lines = sh(["git", "log", "--oneline", "-%d" % n]).splitlines()
    return [(h[:7], dashpage.E(s)) for h, _, s in (ln.partition(" ") for ln in lines)]


def modules():
    d = Path(ROOT) / "scripts"
    return [(p.name, len(p.read_text(encoding="utf-8").splitlines()))
            for p in sorted(d.iterdir()) if p.name.endswith(".py")]


def tree_state():
    out = sh(["git", "status", "--porcelain"])
    return sum(1 for ln in out.splitlines() if not ln.startswith("??"))


def gather(live):
    """Collect everything the page shows. One call, one honest snapshot."""
    rows, _summary, rows_note = selftest_rows(live)
    up, n_models = server_state()
    mut_total, mut_ok, mut_note = mutation_result()
    ci, ci_note = ci_runs()
    return {
        "live": live, "rows": rows, "up": up, "n_models": n_models,
        "mut_total": mut_total, "mut_ok": mut_ok, "ci": ci, "ci_note": ci_note,
        "mods": modules(), "commits": commits(), "marker": mutation_marker.status(),
        "rows_note": rows_note, "mut_note": mut_note,
        "tree": "%d uncommitted change(s)" % tree_state() if tree_state() else "clean",
    }


def write_page(path, html_text):
    """Write atomically: a reader (or the preview) never sees a half page."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(html_text)
    os.replace(tmp, path)


def _claim(path):
    """Create-or-refuse pidfile lock; stale files (dead holder) removed."""
    holder = 0
    if os.path.exists(path):
        try:
            holder = int(Path(path).read_text().strip())
        except (ValueError, OSError):
            pass
        if holder and pid_alive(holder):
            return False, holder
        try:
            os.remove(path)
        except OSError:
            pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(fd, ("%d\n" % os.getpid()).encode())
    os.close(fd)
    return True, None


def watcher_status():
    """Watcher state plus pending shutdown requests, re-read per call."""
    path = os.environ.get("DASHBOARD_PIDFILE") or PID_PATH
    try:
        pid = int(Path(path).read_text().strip())
        alive = pid_alive(pid)
    except (ValueError, OSError):
        pid, alive = None, False
    return {"running": alive, "pid": pid, "stale": pid is not None and not alive,
            "pidfile": path, "pending": [k for k in ("stop", "pause")
                                         if os.path.exists(_sentinel_path(k))]}


def _marker_line(ms):
    """Render a mutation_marker.status() dict: "in flight" gates, "stranded" clears."""
    if ms["state"] == "absent":
        return "mutation marker: none"
    if ms["state"] == "in flight" and ms["holder"] is not None:
        return "mutation marker: in flight (holder pid %d alive, age %ds)" % (
            ms["holder"], ms["age_s"])
    if ms["state"] == "in flight":
        return ("mutation marker: in flight (unparseable holder, age %ds - "
                "gates until the %ds backstop)"
                % (ms["age_s"], mutation_marker.AGE_BACKSTOP_S))
    if ms["holder"] is None:  # stranded: expired, unparseable content
        return ("mutation marker: STRANDED (unparseable holder, age %ds - "
                "next suite clears it)" % ms["age_s"])
    if ms["holder_alive"] is False:
        return ("mutation marker: STRANDED (holder pid %d is dead, age %ds - "
                "next suite clears it)" % (ms["holder"], ms["age_s"]))
    return ("mutation marker: STRANDED (holder pid %d alive but past the %ds "
            "backstop)" % (ms["holder"], mutation_marker.AGE_BACKSTOP_S))


def _pending_line(kinds):
    """Render pending shutdown requests; one outliving a dead watcher is dangerous."""
    verb = {"stop": "watcher exits at its next safe point",
            "pause": "one final page renders, then exit"}
    if len(kinds) == 2:
        return "shutdown requests: stop+pause (both pending - stop wins)"
    return "shutdown requests: %s (%s)" % (kinds[0], verb[kinds[0]]) if kinds \
        else "shutdown requests: none"


def watcher_status_text():
    """The MCP dashboard_status body: lock, shutdown requests, marker."""
    st = watcher_status()
    first = "dashboard watcher: running (pid %d)" % st["pid"] if st["running"] \
        else "dashboard watcher: not running%s" % (
            " (stale pidfile holds dead pid %d)" % st["pid"] if st["stale"] else "")
    ms = mutation_marker.status()
    return "%s\nlock: %s\n%s\n%s\nmarker file: %s" % (
        first, st["pidfile"], _pending_line(st["pending"]),
        _marker_line(ms), ms["path"])


def watched_fingerprint():
    """Content digest of everything feeding the page: scripts/*.py plus
    config.json. Hash-based, so timestamp granularity never hides an edit
    and the mutation registry's rewrite-then-restore leaves it untouched."""
    import config
    try:
        found = [p for p in (Path(ROOT) / "scripts").iterdir()
                 if p.name.endswith(".py")]
    except OSError:  # watched dir gone: every key vanishing is the signal
        found = []
    items = {}
    for p in sorted(found + [Path(config.CONFIG_PATH)]):
        try:
            items[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError:
            items[str(p)] = "<unreadable>"
    return items


def _sentinel_path(kind):
    """Shutdown sentinel ("stop"/"pause"): pidfile + "." + kind, read per call."""
    return (os.environ.get("DASHBOARD_PIDFILE") or PID_PATH) + "." + kind


def _consume_request(kind):
    """Consume a pending shutdown sentinel; True when one existed."""
    try:
        os.remove(_sentinel_path(kind))
        return True
    except OSError:
        return False


def shutdown_watcher(kind="stop", timeout=900):
    """Ask the watcher to exit ("stop" or "pause") and wait; 0 on success.
    A watcher mid-gather lets its children finish, so no residue is left."""
    pf = os.environ.get("DASHBOARD_PIDFILE") or PID_PATH
    try:
        holder = int(Path(pf).read_text().strip())
        live = pid_alive(holder)
    except (ValueError, OSError):
        holder, live = 0, False
    if not live:
        _consume_request(kind)  # a stale request must not kill the next watcher
        print("dashboard watcher: not running (pidfile %s)" % pf)
        return 0
    with open(_sentinel_path(kind), "w", encoding="utf-8") as fh:
        fh.write(kind + "\n")
    print("%s requested; waiting for watcher pid %d" % (kind, holder), flush=True)
    deadline = time.time() + timeout
    while (time.time() < deadline and os.path.exists(pf)
           and pid_alive(holder)):
        time.sleep(1)
    if os.path.exists(pf) and pid_alive(holder):
        print("watcher stuck after %ds; delete %s to cancel"
              % (timeout, _sentinel_path(kind)), file=sys.stderr)
        return 1
    _consume_request(kind)  # the watcher released its lock; dead weight either way
    print("watcher (pid %d) exited cleanly" % holder)
    return 0


def _wait_for_change(snapshot, budget, poll=_POLL_S):
    """Sleep up to budget seconds; return "stop"/"pause" when shutdown is
    requested, "edit" when the fingerprint drifts, or "" when budget ends."""
    deadline = time.time() + budget
    while True:
        for kind in ("stop", "pause"):  # stop wins when both are pending
            if os.path.exists(_sentinel_path(kind)):
                return kind
        if watched_fingerprint() != snapshot:
            return "edit"
        remaining = deadline - time.time()
        if remaining <= 0:
            return ""
        time.sleep(min(poll, remaining))


def _cycle(n, reason, args):
    """One generation; returns (snapshot, seconds spent); a pause render drops refresh."""
    snapshot = watched_fingerprint()  # the disk state this cycle renders
    started = time.time()
    refresh = 0 if reason == "pause requested" else int(args.interval)
    try:
        data = gather(False)
        data["generation"] = (n, reason)
        text = dashpage.render(data, refresh=refresh)
        write_page(args.output, text)
        print("[%d] %s wrote %s (%d bytes, offline)" % (
            n, time.strftime("%H:%M:%S"), args.output,
            len(text.encode("utf-8"))), flush=True)
    except Exception as e:
        # Keep the previous artifact in place; report and try again next cycle.
        print("[%d] %s generation failed: %s" % (
            n, time.strftime("%H:%M:%S"), e), flush=True)
    return snapshot, time.time() - started


def watch(args):
    """Regenerate the dashboard every args.interval seconds until stopped.
    Always the offline check view (live inference costs minutes); the server
    probe and Actions fetch stay live. An edit during the wait cuts it short
    and the footer records why the cycle ran; --stop/--pause exit safely."""
    n = 0
    reason = "startup"
    while True:
        if _consume_request("stop"):  # safe point: nothing is in flight
            print("[%d] %s stop requested - watcher exiting" % (
                n, time.strftime("%H:%M:%S")), flush=True)
            return
        if _consume_request("pause"):
            print("[%d] %s pause requested - rendering one final page" % (
                n, time.strftime("%H:%M:%S")), flush=True)
            _cycle(n + 1, "pause requested", args)  # freeze the artifact fresh
            return
        n += 1
        snapshot, spent = _cycle(n, reason, args)
        # One generation per interval; a watched-source edit wakes the wait.
        budget = max(0.0, args.interval - spent)
        waited = _wait_for_change(snapshot, budget)
        if waited in ("stop", "pause"):
            continue  # the top of the loop consumes the request
        reason = "watched source edit" if waited == "edit" else "interval tick"
        if waited == "edit":
            print("[%d] %s watched source changed - regenerating immediately" % (
                n, time.strftime("%H:%M:%S")), flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true",
                      help="full suite against a real Ollama server, not the offline CI view")
    mode.add_argument("--watch", action="store_true",
                      help="regenerate every --interval seconds until stopped (holds a pidfile lock)")
    mode.add_argument("--stop", action="store_true",
                      help="ask a running watcher to exit at its next safe point and wait for it")
    mode.add_argument("--pause", action="store_true",
                      help="like --stop, but render one final fresh page first")
    ap.add_argument("--interval", type=float, default=120,
                    help="seconds between regenerations in --watch (default 120)")
    ap.add_argument("--stop-timeout", type=float, default=900,
                    help="seconds to wait for the watcher to exit (default 900)")
    ap.add_argument("output", nargs="?", default=DEFAULT_OUT)
    args = ap.parse_args()
    if args.stop or args.pause:
        return shutdown_watcher("pause" if args.pause else "stop", timeout=args.stop_timeout)
    if args.watch:
        ok, holder = _claim(PID_PATH)
        if not ok:
            print("refusing to start: dashboard watcher already running (pid %d) - pidfile %s"
                  % (holder, PID_PATH), file=sys.stderr)
            return 1
        try:
            for kind in ("stop", "pause"):  # a fresh watcher inherits no request
                _consume_request(kind)
            watch(args)
        finally:
            try:
                os.remove(PID_PATH)
            except OSError:
                pass
        return 0
    text = dashpage.render(gather(args.live))
    write_page(args.output, text)
    print("wrote %s (%d bytes, %s)" % (args.output, len(text.encode("utf-8")),
                                       "live" if args.live else "offline"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
