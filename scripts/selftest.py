"""Setup verification plus deliberate exercise of the error paths.

Run:  python selftest.py          fast checks, no model inference
      python selftest.py --live   also runs a real review of a planted-defect file

Exits non-zero if any check fails.
"""

import argparse
import atexit
import base64
import contextlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mutation_marker  # noqa: E402  stdlib-only, never a mutation target: safe pre-gate

# ---------------------------------------------------------------------------
# Concurrent-run discipline: a --mutate run temporarily rewrites module
# sources on disk, and every suite creates temp dirs. Two mechanisms keep a
# watcher cycle from flipping an unrelated run red:
#   1. A marker file in the OS temp dir while --mutate is in flight. A run
#      started during it waits for quiescence BEFORE importing project
#      modules, and disk-reading checks skip rather than lie. The mutator's
#      own children carry _MUTATOR_ENV and ignore the marker, so mutation
#      verification stays strict. A crashed mutator's marker is recovered
#      like the dashboard pidfile lock: the holder pid is checked, and a
#      dead holder clears at once - a killed session never gates the next
#      suite - while only unparseable content waits out the 950s age
#      backstop (no mutate run lives past its 900s child timeouts). A hard
#      kill can also land mid-rewrite, leaving a mutation applied; the run
#      journals each module's original bytes first, and the next suite
#      restores them from that journal before importing anything.
#   2. The residue check re-checks survivors across a short grace window,
#      because an in-flight dir from any concurrent suite clears in seconds.

_MUTATOR_ENV = "_SELFTEST_MUTATOR"
# _SELFTEST_MUTATION_MARKER: test seam (same trust level as _MUTATOR_ENV) -
# lets the coordination check point a spawned child at a privately held
# marker so the ambient one can never deadlock the grandchild. The default
# path has one home: mutation_marker.path().
_MUTATION_MARKER = os.environ.get("_SELFTEST_MUTATION_MARKER") or mutation_marker.path()


def _clear_mutation_marker():
    try:
        os.remove(_MUTATION_MARKER)
    except OSError:
        pass


def _mutation_in_flight():
    """True while another process's --mutate run holds the marker.

    Recovery mirrors the dashboard pidfile lock: a marker whose holder pid
    has died clears at once, so a killed session never gates the next
    suite. Only unparseable content falls back to the age backstop, which
    bounds even that."""
    if _MUTATOR_ENV in os.environ:  # the mutator's own children are sanctioned
        return False
    try:
        with open(_MUTATION_MARKER, "r", encoding="utf-8", errors="replace") as fh:
            holder = fh.read().strip()
        age = time.time() - os.path.getmtime(_MUTATION_MARKER)
    except OSError:
        return False  # absent
    if age > mutation_marker.AGE_BACKSTOP_S:
        _clear_mutation_marker()  # stale: self-expires, never wedges
        return False
    import pidutil  # safe mid-mutation: stdlib-only, never a mutation target
    if holder.isdigit() and not pidutil.pid_alive(int(holder)):
        sys.stderr.write("selftest: mutation marker holder pid %s is dead; "
                         "clearing the marker\n" % holder)
        _clear_mutation_marker()
        return False
    return True  # live holder, or unparseable content: age guard bounds it


def _clear_pycache():
    """Drop compiled caches under scripts/ so the next import sees source.

    mtime+size pyc validation can miss a same-size mutation-restore cycle
    that lands within one mtime second. Defined early: the quiescence gate
    and the journal heal both run at import time, before the rest of the
    module exists."""
    here = os.path.dirname(os.path.abspath(__file__))
    cache = os.path.join(here, "__pycache__")
    if os.path.isdir(cache):
        shutil.rmtree(cache, ignore_errors=True)


def _journal_path():
    """The write-side journal pairs with the marker: marker path + suffix,
    so seam-swapped tests move both together."""
    return _MUTATION_MARKER + ".journal"


def _clear_mutation_journal():
    try:
        os.remove(_journal_path())
    except OSError:
        pass


def _write_mutation_journal(name, rel, original_bytes):
    """Record the pre-rewrite bytes of a module under mutation.

    Written before the rewrite and cleared after the restore, so a journal
    that outlives the run means a mutation may still be applied. Atomic
    replace: a kill mid-write leaves the previous (already-restored) entry,
    never a torn one."""
    entry = {"name": name, "file": rel,
             "content_b64": base64.b64encode(original_bytes).decode("ascii")}
    tmp = _journal_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(entry, fh)
    os.replace(tmp, _journal_path())


def _heal_mutation_journal():
    """Restore sources if a dead --mutate run left a mutation applied.

    Called only when no live mutate holds the marker: the journal then
    cannot belong to a working run, so writing the journaled bytes back is
    always safe - a no-op when the file was already restored. Refuses for
    the mutator's own children: they run mid-journal by design, so that
    guard lives here rather than at the call site."""
    if _MUTATOR_ENV in os.environ:  # the mutator's own children never heal
        return
    try:
        with open(_journal_path(), "r", encoding="utf-8") as fh:
            entry = json.load(fh)
        original = base64.b64decode(entry["content_b64"])
        name, rel = entry["name"], entry["file"]
    except (OSError, ValueError, KeyError):
        return  # absent or unreadable: nothing safe to do automatically
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), rel)
    try:
        with open(path, "rb") as fh:
            current = fh.read()
    except OSError:
        return  # target gone since: the journal is stale beyond repair
    if current == original:
        _clear_mutation_journal()  # restored already; just drop the stale entry
        return
    with open(path, "wb") as fh:
        fh.write(original)
    _clear_pycache()
    _clear_mutation_journal()
    sys.stderr.write("selftest: restored %s, left mutated by a dead --mutate "
                     "run (%s)\n" % (rel, name))


def _wait_for_quiescence():
    """Block until a concurrent --mutate run releases the source files.

    Importing a module mid-mutation runs mutated code, so every check would
    be untrustworthy; waiting is slow but honest. A dead holder's marker
    clears at once (holder liveness via pidutil) and the age guard bounds
    the rest, so the wait always terminates. A journal left by a run that
    died mid-rewrite is healed here too, before anything imports."""
    if _mutation_in_flight():
        sys.stderr.write("selftest: concurrent --mutate in flight; waiting for it "
                         "to finish before importing modules\n")
        deadline = time.time() + 960
        while _mutation_in_flight() and time.time() < deadline:
            time.sleep(5)
        if _mutation_in_flight():  # unreachable in practice (age guard)
            _clear_mutation_marker()
    _heal_mutation_journal()  # refuses for the mutator's own children itself


_wait_for_quiescence()

import cli  # noqa: E402
import config  # noqa: E402
import consensus  # noqa: E402
import collect  # noqa: E402
import ollama_client as oc  # noqa: E402
import prompts  # noqa: E402
import render  # noqa: E402
import mcp_server  # noqa: E402
import review  # noqa: E402

PLANTED_DEFECTS = '''\
import sqlite3

def find_user(conn, name):
    # planted: SQL injection
    return conn.execute("SELECT * FROM users WHERE name = '" + name + "'").fetchall()

def last_page(items, per_page):
    # planted: off-by-one, and ZeroDivisionError when per_page is 0
    return items[(len(items) // per_page - 1) * per_page:]

def greet(user):
    # planted: unhandled None
    return "Hello " + user["profile"]["name"].upper()
'''

RESULTS = []


# Checks that need a live Ollama server. Everything else is pure logic or local
# filesystem work, so CI can run the bulk of the suite without an inference server.
NEEDS_SERVER = {
    "server reachable",
    "default model resolves",
    "bare family name resolves",
    "live review of planted defects",
}

# Checks that re-read module sources from disk: unreliable while a concurrent
# --mutate run has them rewritten, so they skip honestly instead of failing.
# (The residue check needs no exemption: it judges only dirs this run owns.)
MUTATION_SENSITIVE = {
    "orchestration + focus decoupled",
    "dashboard: watcher pidfile lock",
    "selftest: coordinates concurrent runs",
}


def check(name, fn):
    try:
        detail = fn()
        RESULTS.append(("PASS", name, detail or "ok"))
    except AssertionError as e:
        RESULTS.append(("FAIL", name, "assertion failed: %s" % e))
    except Exception as e:
        RESULTS.append(("FAIL", name, "unexpected %s: %s" % (type(e).__name__, e)))


def skip(name, reason):
    RESULTS.append(("SKIP", name, reason))


# --------------------------------------------------------------------------
# configuration and connectivity
# --------------------------------------------------------------------------

def t_config():
    cfg, notes = config.load_config()
    assert cfg["base_url"].startswith("http"), "base_url must be a URL"
    assert cfg["timeout_s"] > 0, "timeout must be positive"
    return "endpoint=%s model=%s" % (cfg["base_url"], cfg["model"])


def t_server_reachable():
    cfg, _ = config.load_config()
    models = oc.list_models(cfg)
    assert models, "server reachable but no models installed"
    return "%d model(s) installed" % len(models)


def t_model_resolves():
    cfg, _ = config.load_config()
    names = {m["name"] for m in oc.list_models(cfg)}
    model, _notes = oc.resolve_model(cfg, None, names)
    return "resolved to %s" % model


def t_bare_family_name_resolves():
    cfg, _ = config.load_config()
    names = {m["name"] for m in oc.list_models(cfg)}
    family = sorted(names)[0].split(":")[0]
    model, notes = oc.resolve_model(cfg, family, names)
    assert model in names, "bare family name did not resolve to an installed tag"
    return "%s -> %s" % (family, model)


# --------------------------------------------------------------------------
# error paths - each must produce a typed error, not an exception
# --------------------------------------------------------------------------

def t_err_unreachable():
    cfg, _ = config.load_config()
    cfg = dict(cfg, base_url="http://127.0.0.1:9", connect_timeout_s=2)
    try:
        oc.list_models(cfg)
    except oc.OllamaError as e:
        assert e.kind == "unreachable", "expected unreachable, got %s" % e.kind
        assert "ollama serve" in e.remedy, "remedy must tell the user how to start it"
        return e.kind
    raise AssertionError("expected an OllamaError")


def t_err_model_missing():
    cfg, _ = config.load_config()
    try:
        oc.resolve_model(cfg, "definitely-not-a-real-model", {"a:1", "b:2"})
    except oc.OllamaError as e:
        assert e.kind == "model_missing"
        assert "ollama pull" in e.remedy, "remedy must suggest pulling the model"
        return e.kind
    raise AssertionError("expected an OllamaError")


def t_err_cloud_blocked():
    cfg, _ = config.load_config()
    cfg = dict(cfg, allow_cloud_models=False)
    try:
        oc.resolve_model(cfg, "something:cloud", set())
    except oc.OllamaError as e:
        assert e.kind == "cloud_blocked"
        return e.kind
    raise AssertionError("expected cloud model to be blocked")


def t_err_missing_file():
    cfg, _ = config.load_config()
    try:
        collect.from_files(cfg, ["./__no_such_file_here__.py"])
    except collect.InputError as e:
        assert "does not exist" in e.detail
        return "rejected"
    raise AssertionError("expected InputError")


def t_err_binary_file():
    cfg, _ = config.load_config()
    with _tmpdir() as tmp:
        p = os.path.join(tmp, "blob.dat")
        with open(p, "wb") as fh:
            fh.write(b"\x00\x01\x02binary\x00content")
        try:
            collect.from_files(cfg, [p])
        except collect.InputError as e:
            assert "null bytes" in e.detail or "binary" in e.detail
            return "rejected"
        raise AssertionError("expected binary file to be rejected")


def t_err_empty_file():
    cfg, _ = config.load_config()
    with _tmpdir() as tmp:
        p = os.path.join(tmp, "empty.py")
        open(p, "w").close()
        try:
            collect.from_files(cfg, [p])
        except collect.InputError as e:
            assert "empty" in e.detail
            return "rejected"
        raise AssertionError("expected empty file to be rejected")


def t_err_not_a_repo():
    """Every scope gets the clear not-a-repo error. --staged once reached
    git's --no-index mode outside a repo and relayed 'unknown option staged'
    as the diagnosis instead."""
    cfg, _ = config.load_config()
    with _tmpdir() as tmp:
        for kwargs in ({}, {"staged": True}, {"ref": "HEAD"}):
            try:
                collect.from_git(cfg, cwd=tmp, **kwargs)
            except collect.InputError as e:
                assert "not a git repositor" in e.detail.lower(), e.detail
                assert "--file" in e.remedy, e.remedy
                assert "unknown option" not in e.detail.lower(), e.detail
            else:
                raise AssertionError("expected InputError for %r" % (kwargs,))
        return "handled for all scopes"


def t_truncation():
    cfg, _ = config.load_config()
    cfg = dict(cfg, max_file_chars=200)
    with _tmpdir() as tmp:
        p = os.path.join(tmp, "big.py")
        raw = "x = 1\n" * 5000
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(raw)
        inp = collect.from_files(cfg, [p])
        assert inp.chunks[0].truncated, "oversized file should be marked truncated"
        capped = inp.chunks[0].text
        assert "TRUNCATED" in capped
        # The cut itself, not just the label: the marker's claimed "shown"
        # count must match the configured cap and its "of" total the true
        # source size, and the text really must be cap-sized - a
        # pass-through cap would send the full file while claiming most of
        # it was omitted.
        m = re.search(r"\[TRUNCATED: (\d+) of (\d+) characters shown", capped)
        assert m, capped[:80]
        assert int(m.group(1)) == cfg["max_file_chars"], capped[-120:]
        assert int(m.group(2)) == len(raw), capped[-120:]
        assert len(capped) < cfg["max_file_chars"] + 500, len(capped)
        return "capped at %d chars" % cfg["max_file_chars"]


# --------------------------------------------------------------------------
# parser tiers
# --------------------------------------------------------------------------

def t_parse_strict():
    f, mode = review.parse_findings('{"findings":[]}')
    assert f == [] and mode == "strict"
    return mode


def t_parse_fenced():
    text = 'Sure, here you go:\n```json\n{"findings":[{"severity":"high",' \
           '"category":"security","location":"a.py:1","issue":"i","why":"w",' \
           '"suggested_fix":"s"}]}\n```\nHope that helps.'
    f, mode = review.parse_findings(text)
    assert mode == "fenced" and len(f) == 1
    return mode


def t_parse_salvaged():
    text = 'Here is my review. {"findings":[{"severity":"nonsense",' \
           '"category":"bogus","location":"x"}]} Let me know.'
    f, mode = review.parse_findings(text)
    assert mode in ("salvaged", "fenced"), "expected salvage, got %s" % mode
    assert f[0]["severity"] == "info", "invalid severity must degrade to info"
    assert f[0]["category"] == "logic", "invalid category must degrade to logic"
    return mode


def t_parse_garbage():
    f, mode = review.parse_findings("I am afraid I cannot help with that.")
    assert f is None and mode is None
    return "returns None so the caller can degrade"


def _finding(
    sev="high",
    cat="security",
    loc="a.py:10",
    issue="SQL injection via string concatenation",
    fix="use parameters",
):
    return {
        "severity": sev,
        "category": cat,
        "location": loc,
        "issue": issue,
        "why": "attacker controls the name argument",
        "suggested_fix": fix,
    }


def t_consensus_merges_agreement():
    a = _finding()
    b = _finding(loc="a.py:11", issue="SQL injection through string concatenation")
    merged = consensus.reconcile([("m1", [a]), ("m2", [b])])
    assert len(merged) == 1, "the same defect should merge, got %d" % len(merged)
    assert merged[0]["agreement"] == "corroborated"
    assert merged[0]["raised_by"] == ["m1", "m2"]
    return "2 models -> 1 corroborated finding"


def t_consensus_keeps_singles():
    a = _finding()
    b = _finding(cat="performance", loc="z.py:99", issue="unbounded cache growth")
    merged = consensus.reconcile([("m1", [a]), ("m2", [b])])
    assert len(merged) == 2, "unrelated findings must not merge"
    assert all(f["agreement"] == "single" for f in merged)
    return "nothing discarded"


def t_consensus_respects_file_and_category():
    a = _finding()
    same_text_other_file = _finding(loc="b.py:10")
    assert not consensus.same_defect(a, same_text_other_file), "different files merged"
    same_text_other_cat = _finding(cat="logic")
    assert not consensus.same_defect(a, same_text_other_cat), "different categories merged"
    return "file and category are hard boundaries"


def t_consensus_sorts_corroborated_first():
    a = _finding(sev="low")
    b = _finding(sev="low", loc="a.py:10")
    lone = _finding(sev="critical", cat="logic", loc="q.py:1", issue="off by one")
    merged = consensus.sort_merged(
        consensus.reconcile([("m1", [a, lone]), ("m2", [b])])
    )
    assert merged[0]["model_count"] == 2, "corroborated finding should sort first"
    return "corroborated outranks a lone critical"


def t_consensus_severity_spread():
    a = _finding(sev="critical")
    b = _finding(sev="low", loc="a.py:10")
    merged = consensus.reconcile([("m1", [a]), ("m2", [b])])
    assert merged[0]["severity_spread"] == ["critical", "low"]
    assert merged[0]["severity"] == "critical", "representative takes the worst severity"
    return "disagreement recorded"


def t_consensus_parses_messy_locations():
    for loc, want_file, want_line in [
        ("C:/Users/x/planted.py: find_user", "planted.py", None),
        ("collect.py:104", "collect.py", 104),
        ("in the retry loop", None, None),
    ]:
        f, ln, _ = consensus.parse_location(loc)
        assert f == want_file, "%r -> file %r, wanted %r" % (loc, f, want_file)
        assert ln == want_line, "%r -> line %r, wanted %r" % (loc, ln, want_line)
    return "handles paths, line numbers and prose"


def _capture_cli(argv):
    """Run cli.main() and capture its stdout without touching real streams."""
    buf = io.StringIO()
    code, out = None, ""
    real = sys.stdout
    sys.stdout = buf
    try:
        code = cli.main(list(argv))
    finally:
        sys.stdout = real
        out = buf.getvalue()
    return code, out


@contextlib.contextmanager
def _fake_host(base_url):
    """Steer config-driven clients at a fake Ollama for the enclosed block."""
    saved = os.environ.get("OLLAMA_HOST")
    os.environ["OLLAMA_HOST"] = base_url
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("OLLAMA_HOST", None)
        else:
            os.environ["OLLAMA_HOST"] = saved


# Every temp dir this process's fixtures ever created, in creation order.
# t_tempdir_leaves_no_residue asserts these are all gone - ownership, not
# snapshot timing, so a concurrent suite's churn cannot flip the verdict.
_OUR_TMPDIRS = []


@contextlib.contextmanager
def _tmpdir():
    """Yield a temp directory that is removed afterwards.

    The second pass handles Windows, where git writes its object files
    read-only and the first rmtree cannot remove them.
    """
    tmp = tempfile.mkdtemp()
    _OUR_TMPDIRS.append(tmp)
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if os.path.exists(tmp):
            for root, _dirs, files in os.walk(tmp):
                for f in files:
                    os.chmod(os.path.join(root, f), stat.S_IWRITE)
            shutil.rmtree(tmp, ignore_errors=True)

def _git(args, cwd):
    out = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)
    assert out.returncode == 0, "git %s failed: %s" % (args, out.stderr)
    return out.stdout


def _write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


@contextlib.contextmanager
def _repo(commits=1, files=()):
    """Temp git repo: `commits` empty commits, then `files` written
    (name -> text or bytes). Yields the repo path."""
    with _tmpdir() as tmp:
        _git(["init"], tmp)
        _git(["config", "user.email", "selftest@example.com"], tmp)
        _git(["config", "user.name", "selftest"], tmp)
        for _ in range(commits):
            _git(["commit", "--allow-empty", "-m", "c"], tmp)
        for name, data in (files or {}).items():
            with open(os.path.join(tmp, name), "wb") as fh:
                fh.write(data if isinstance(data, bytes) else data.encode("utf-8"))
        yield tmp





def t_tempdir_leaves_no_residue():
    """Fixture cleanup must leave nothing behind in the OS temp dir.

    Windows regression lock: git writes its loose object files read-only,
    so a naive shutil.rmtree cannot remove a fixture repo (WinError 5).
    _tmpdir has a second pass that chmods and re-removes; this check fails
    if any directory this suite's fixtures created still exists - empty
    shell or not (the Windows residue is a populated repo with read-only
    objects).

    Ownership, not snapshots: only dirs _this_ run created are judged, so
    a concurrently running suite's in-flight dirs can never flip the
    verdict, and no grace-window timing heuristic is needed.
    """
    leaked = sorted(d for d in _OUR_TMPDIRS if os.path.isdir(d))
    assert not leaked, "fixture residue survived cleanup: %s" % leaked
    return "temp dir clean: all %d fixture dirs removed" % len(_OUR_TMPDIRS)

def t_modules_stay_focused():
    """CONTRIBUTING promises modules stay near 400 lines; enforce it.

    selftest.py is exempt: test files grow with coverage, and splitting them
    buys nothing. cli.py already breached this once, which is why it exists.
    """
    limit = 400
    here = os.path.dirname(os.path.abspath(__file__))
    oversized = []
    for name in sorted(os.listdir(here)):
        if not name.endswith(".py") or name == "selftest.py":
            continue
        with open(os.path.join(here, name), encoding="utf-8") as fh:
            n = sum(1 for _ in fh)
        if n > limit:
            oversized.append("%s (%d)" % (name, n))
    assert not oversized, "over %d lines: %s" % (limit, ", ".join(oversized))
    return "all tool modules within %d lines" % limit


def t_dashboard_lock_is_exclusive():
    """The watcher pidfile lock: claims, refuses rivals, recovers stale files."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import dashboard
    with _tmpdir() as tmp:
        lock = os.path.join(tmp, "watch.pid")

        ok, holder = dashboard._claim(lock)
        assert ok and holder is None, "first claim on an empty dir must acquire"
        assert int(open(lock).read().strip()) == os.getpid(), "pidfile must hold our pid"

        ok, holder = dashboard._claim(lock)
        assert not ok and holder == os.getpid(), "second claim must refuse while holder lives"
        assert int(open(lock).read().strip()) == os.getpid(), "refusal must not clobber the file"

        with open(lock, "w") as fh:  # fake a long-dead holder: pid far above pid_max
            fh.write(str(1 << 30))
        ok, holder = dashboard._claim(lock)
        assert ok and holder is None, "claim over a dead holder's pidfile must recover"

        with open(lock, "w") as fh:  # corrupted pidfile: unreadable pid
            fh.write("garbage")
        ok, holder = dashboard._claim(lock)
        assert ok and holder is None, "claim over a corrupt pidfile must recover"

        os.remove(lock)
    here = os.path.dirname(os.path.abspath(__file__))
    src = open(os.path.join(here, "dashboard.py"), encoding="utf-8").read()
    assert "_claim(PID_PATH)" in src, "watch() must still acquire the pidfile lock"

    # Process-level end to end, the stdio-E2E doctrine applied to the watcher:
    # spawn a real rival against a scratch pidfile and pin that the lock holds
    # at the process boundary too - the second process exits 1, explains why,
    # and writes nothing. Scratch artifacts live under .freebuff/preview/ (not
    # the OS temp dir) so the residue check sees nothing; DASHBOARD_PIDFILE
    # keeps the production watcher's pidfile out of reach.
    scratch = os.path.join(here, os.pardir, ".freebuff", "preview")
    os.makedirs(scratch, exist_ok=True)
    base = os.path.join(scratch, "_lock_e2e_%d" % os.getpid())
    pidfile, r_out, r_err = base + ".pid", base + ".out", base + ".err"
    try:
        with open(pidfile, "w") as fh:  # pretend a watcher is already running
            fh.write(str(os.getpid()))
        env = dict(os.environ, DASHBOARD_PIDFILE=pidfile)
        rival = subprocess.run(
            [sys.executable, os.path.join(here, "dashboard.py"), "--watch",
             "--interval", "3600", base + ".html"],
            capture_output=True, text=True, timeout=60, cwd=here, env=env)
        assert rival.returncode == 1, (rival.returncode, rival.stderr[-300:])
        assert "refusing to start" in rival.stderr, rival.stderr[-300:]
        assert "already running" in rival.stderr, rival.stderr[-300:]
        assert not os.path.exists(base + ".html"), "rival must not write the artifact"
        assert int(open(pidfile).read().strip()) == os.getpid(), (
            "rival must not clobber the pidfile")
    finally:
        for p in (pidfile, r_out, r_err, base + ".html"):
            if os.path.exists(p):
                os.remove(p)
    return "claim, refuse, stale/corrupt recovery, and process-level refusal all behave"


def t_dashboard_watch_reacts_to_disk():
    """The watcher wakes on a watched-source edit instead of sleeping out
    the interval: content-hash fingerprinting over scripts/*.py plus
    config.json, an interruptible wait, and watch() wired to both."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import config
    import dashboard
    import dashpage
    with _tmpdir() as tmp:
        scripts = os.path.join(tmp, "scripts")
        os.makedirs(scripts)
        a = os.path.join(scripts, "a.py")
        b = os.path.join(scripts, "b.py")
        cfg = os.path.join(tmp, "config.json")
        for path, body in ((a, "x = 1\n"), (b, "y = 2\n"), (cfg, "{}")):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
        saved_root, saved_cfg = dashboard.ROOT, config.CONFIG_PATH
        dashboard.ROOT, config.CONFIG_PATH = tmp, cfg  # steer at the seams
        try:
            fp = dashboard.watched_fingerprint()
            assert sorted(os.path.normpath(k) for k in fp) == \
                sorted(os.path.normpath(k) for k in (a, b, cfg)), (
                    "fingerprint must cover the watched set exactly: %s" % sorted(fp))

            with open(a, "w", encoding="utf-8") as fh:  # a real edit registers
                fh.write("x = 2\n")
            fp_edit = dashboard.watched_fingerprint()
            assert fp_edit != fp, "an edit must change the fingerprint"

            body = open(a, encoding="utf-8").read()
            with open(a, "w", encoding="utf-8") as fh:  # same bytes, new mtime
                fh.write(body)
            assert dashboard.watched_fingerprint() == fp_edit, (
                "a restore-after-rewrite (mutation registry's signature move) "
                "must not register")

            os.remove(b)  # deletion and creation register too
            assert b not in dashboard.watched_fingerprint()
            c = os.path.join(scripts, "c.py")
            with open(c, "w", encoding="utf-8") as fh:
                fh.write("z = 3\n")
            assert os.path.normpath(c) in (
                os.path.normpath(k) for k in dashboard.watched_fingerprint())

            # The wake: a rewrite from another thread ends the wait far early.
            def touch():
                time.sleep(0.25)
                with open(a, "w", encoding="utf-8") as fh:
                    fh.write("x = 3\n")
            fp_wait = dashboard.watched_fingerprint()
            th = threading.Thread(target=touch)
            th.start()
            started = time.time()
            woke = dashboard._wait_for_change(fp_wait, 5.0)
            took = time.time() - started
            th.join()
            assert woke == "edit", "a watched edit must end the wait early: %r" % woke
            assert took < 3.0, "wait should have been cut short, took %.1fs" % took

            assert dashboard._wait_for_change(
                dashboard.watched_fingerprint(), 0.3) == "", (
                "an unchanged tree must sleep out the full budget")
        finally:
            dashboard.ROOT, config.CONFIG_PATH = saved_root, saved_cfg

    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "dashboard.py"), encoding="utf-8").read()
    assert "snapshot = watched_fingerprint()" in src, (
        "watch() must fingerprint the sources before gathering")
    assert "_wait_for_change(snapshot" in src, (
        "watch() must wait interruptibly on that snapshot")
    for reason in ("startup", "interval tick", "watched source edit",
                   "pause requested"):
        assert reason in src, "the generation reason %r must exist" % reason
    assert '"generation"' in src, "watch() must record why each cycle ran"

    page = {"rows": [], "mods": [], "up": True, "n_models": 0, "live": False,
            "mut_ok": True, "mut_total": 21, "ci": [], "ci_note": None,
            "commits": [], "tree": "clean",
            "marker": {"state": "absent", "holder": None,
                       "holder_alive": None, "age_s": None}}
    assert "cycle" not in dashpage.render(page).split("<footer>")[1], (
        "a one-shot page must not claim a cycle")
    page["generation"] = (14, "watched source edit")
    footer = dashpage.render(page).split("<footer>")[1]
    assert "cycle 14, watched source edit" in footer, footer[:200]
    return "an edit wakes the watcher, a restore does not, reasons reach the footer"


def t_dashboard_degraded_cycles_explain_themselves():
    """A child without a verdict must say so on the page.

    gather attaches a bounded note (exit code, stream tails) to any child
    whose summary went missing - a crash or a kill - and the page renders
    it as a warning with a NO VERDICT chip instead of a bare zero that
    reads like a regression that never happened."""
    import dashboard
    import dashpage
    note = dashboard._degradation_note("the offline selftest", 1,
                                       "partial output", "boom traceback")
    assert "exit=1" in note and "boom traceback" in note, note
    assert len(dashboard._degradation_note("m", 2, "", "x" * 5000)) < 800, (
        "notes must be bounded")

    marker = {"state": "absent", "holder": None, "holder_alive": None,
              "age_s": None}
    page = {"rows": [], "mods": [], "up": True, "n_models": 0, "live": False,
            "mut_ok": True, "mut_total": 21, "ci": [], "ci_note": None,
            "commits": [], "tree": "clean", "marker": marker,
            "rows_note": None, "mut_note": None}
    assert "&#9888;" not in dashpage.render(page), "a healthy page must not warn"

    page["rows_note"] = note
    html = dashpage.render(page)
    assert "&#9888;" in html and "boom traceback" in html and "exit=1" in html, (
        html[-400:])

    page["rows_note"], page["mut_note"] = None, dashboard._degradation_note(
        "the mutation registry run", 3, "", "baseline assert blew up")
    page["mut_ok"] = False  # no verdict is not a regression verdict
    html = dashpage.render(page)
    assert "NO VERDICT" in html and "baseline assert blew up" in html, html[-400:]
    assert "REGRESSION" not in html, "a no-verdict cycle must not claim regression"

    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "dashboard.py"), encoding="utf-8").read()
    assert '"rows_note"' in src and '"mut_note"' in src, (
        "gather must carry the degradation notes to the page")
    return "degraded cycles render exit codes and stderr tails as page warnings"


def t_dashboard_watcher_stops_cleanly():
    """--stop asks the watcher to exit at a safe point and waits for it.

    The sentinel beside the pidfile wakes the interruptible wait; the loop
    only exits between gathers, so selftest and mutate children always
    finish - no half-rewrite, marker, or journal survives a stop. A fresh
    watcher clears a stale sentinel instead of dying on it."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import threading
    import dashboard
    saved = os.environ.get("DASHBOARD_PIDFILE")
    try:
        with _tmpdir() as tmp:
            pf = os.path.join(tmp, "watch.pid")
            os.environ["DASHBOARD_PIDFILE"] = pf  # the stop seams read it per call

            assert not dashboard._consume_request("stop"), (
                "no sentinel: consume must report nothing")
            with open(pf + ".stop", "w") as fh:
                fh.write("stop\n")
            assert dashboard._consume_request("stop"), "a sentinel must consume"
            assert not os.path.exists(pf + ".stop"), "consume must remove it"

            # the interruptible wait wakes on stop, before its budget runs out
            snapshot = dashboard.watched_fingerprint()

            def request_stop():
                time.sleep(0.2)
                with open(pf + ".stop", "w") as fh:
                    fh.write("stop\n")
            th = threading.Thread(target=request_stop)
            th.start()
            started = time.time()
            waited = dashboard._wait_for_change(snapshot, 30.0)
            took = time.time() - started
            th.join()
            assert waited == "stop", "a stop request must cut the wait: %r" % waited
            assert took < 10.0, "stop must wake the wait, took %.1fs" % took

            # not running: both stale-pidfile shapes exit 0 and clean up
            with open(pf + ".stop", "w") as fh:
                fh.write("stop\n")
            with open(pf, "w") as fh:
                fh.write(str(1 << 30))
            assert dashboard.shutdown_watcher("stop", timeout=5) == 0, (
                "a dead holder is 'not running'")
            assert not os.path.exists(pf + ".stop"), (
                "a stale request must be cleared, never inherited")

            # timeout: a live holder that ignores the request reports and keeps it
            with open(pf + ".stop", "w") as fh:
                fh.write("stop\n")
            with open(pf, "w") as fh:
                fh.write(str(os.getpid()))
            assert dashboard.shutdown_watcher("stop", timeout=2) == 1, (
                "a live holder past the timeout must report failure")
            assert os.path.exists(pf + ".stop"), (
                "a pending request must survive a timed-out stop")
            os.remove(pf + ".stop")

            # success: the holder exits, releasing the pidfile like the real
            # watcher's finally does; the command reports a clean stop
            child = subprocess.Popen([sys.executable, "-c",
                                      "import os, time, sys; time.sleep(0.5); "
                                      "os.remove(sys.argv[1])", pf])
            with open(pf, "w") as fh:
                fh.write(str(child.pid))
            assert dashboard.shutdown_watcher("stop", timeout=30) == 0, (
                "a holder that exits must stop cleanly")
            assert not os.path.exists(pf + ".stop"), (
                "a completed stop must leave no sentinel")
    finally:
        if saved is None:
            os.environ.pop("DASHBOARD_PIDFILE", None)
        else:
            os.environ["DASHBOARD_PIDFILE"] = saved

    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "dashboard.py"), encoding="utf-8").read()
    assert 'if _consume_request("stop"):' in src, (
        "the watch loop must check for stop at the safe point")
    assert 'for kind in ("stop", "pause"):  # a fresh watcher' in src, (
        "a fresh watcher must clear stale requests of both kinds")
    return "stop sentinel wakes the wait, times out loudly, and cleans up"


def t_dashboard_pause_freezes_a_final_page():
    """--pause exits at the next safe point but freezes a fresh page first.

    --stop leaves the page as it stands; --pause renders one final page
    (reason "pause requested", auto-refresh dropped) so the artifact stops
    updating from an accurate snapshot instead of a stale one."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import threading
    import dashboard
    saved = os.environ.get("DASHBOARD_PIDFILE")
    try:
        with _tmpdir() as tmp:
            pf = os.path.join(tmp, "watch.pid")
            os.environ["DASHBOARD_PIDFILE"] = pf  # the pause seams read it per call

            # the interruptible wait wakes on pause, before its budget runs out
            def request_pause():
                time.sleep(0.2)
                with open(pf + ".pause", "w") as fh:
                    fh.write("pause\n")
            th = threading.Thread(target=request_pause)
            th.start()
            started = time.time()
            waited = dashboard._wait_for_change(
                dashboard.watched_fingerprint(), 30.0)
            took = time.time() - started
            th.join()
            assert waited == "pause", "a pause request must cut the wait: %r" % waited
            assert took < 10.0, "pause must wake the wait, took %.1fs" % took

            # client side: a stale request is cleared, a live holder is waited out
            with open(pf, "w") as fh:
                fh.write(str(1 << 30))
            with open(pf + ".pause", "w") as fh:
                fh.write("pause\n")
            assert dashboard.shutdown_watcher("pause", timeout=5) == 0, (
                "a dead holder is 'not running'")
            assert not os.path.exists(pf + ".pause"), (
                "a stale pause request must be cleared, never inherited")

            child = subprocess.Popen([sys.executable, "-c",
                                      "import os, time, sys; time.sleep(0.5); "
                                      "os.remove(sys.argv[1])", pf])
            with open(pf, "w") as fh:
                fh.write(str(child.pid))
            assert dashboard.shutdown_watcher("pause", timeout=30) == 0, (
                "a holder that exits must pause cleanly")
            assert not os.path.exists(pf + ".pause"), (
                "a completed pause must leave no sentinel")

            # the state machine: pause at a safe point renders exactly one
            # final page; stop wins when both requests are pending
            class Args:
                interval = 120.0
                output = os.path.join(tmp, "out.html")
            calls = []
            orig = dashboard._cycle, dashboard._wait_for_change
            dashboard._cycle = (
                lambda n, reason, args: calls.append((n, reason)) or ({}, 0.0))

            def fake_wait(snapshot, budget, **kw):
                with open(pf + ".pause", "w") as fh:  # the wait reports only
                    fh.write("pause\n")               # requests that are on disk
                return "pause"
            dashboard._wait_for_change = fake_wait
            try:
                dashboard.watch(Args())
                assert calls == [(1, "startup"), (2, "pause requested")], calls
                calls.clear()
                with open(pf + ".pause", "w") as fh:  # paused before any cycle
                    fh.write("pause\n")
                dashboard.watch(Args())
                assert calls == [(1, "pause requested")], calls
                calls.clear()
                with open(pf + ".pause", "w") as fh:
                    fh.write("pause\n")
                with open(pf + ".stop", "w") as fh:  # stop wins over pause
                    fh.write("stop\n")
                dashboard.watch(Args())
                assert calls == [], "stop must win over a pending pause: %r" % calls
                os.remove(pf + ".pause")
            finally:
                dashboard._cycle, dashboard._wait_for_change = orig
    finally:
        if saved is None:
            os.environ.pop("DASHBOARD_PIDFILE", None)
        else:
            os.environ["DASHBOARD_PIDFILE"] = saved

    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "dashboard.py"), encoding="utf-8").read()
    assert '_cycle(n + 1, "pause requested", args)' in src, (
        "the pause safe point must render one final page")
    assert 'refresh = 0 if reason == "pause requested"' in src, (
        "a frozen page must not claim to auto-refresh")
    return "--pause wakes the wait, freezes one final page, and cleans up"


def t_review_options_decoupled():
    """run_review must not need argparse types, and focus policy must have
    one home: prompts.resolve_focus (validation + adversarial injection)."""
    opts = review.ReviewOptions(adversarial=True, instructions="focus on retries")
    assert opts.adversarial and opts.temperature is None
    assert not hasattr(opts, "json"), "ReviewOptions should not carry CLI concerns"
    err = review.ReviewFailure({"kind": "timeout", "detail": "d", "remedy": "r"})
    assert err.error["kind"] == "timeout"
    focus, ferr = prompts.resolve_focus("security, bogus")
    assert ferr and ferr.startswith("Unknown focus area(s):"), ferr
    focus, ferr = prompts.resolve_focus("security", adversarial=True)
    assert ferr is None and focus == ["security", "design"], focus
    focus, ferr = prompts.resolve_focus(" SECURITY ", adversarial=True)
    assert ferr is None and focus == ["security", "design"], focus
    here = os.path.dirname(os.path.abspath(__file__))
    cli_src = open(os.path.join(here, "cli.py"), encoding="utf-8").read()
    assert "Unknown focus area" not in cli_src, (
        "focus rejection wording must live only in prompts.resolve_focus")
    mcp_src = open(os.path.join(here, "mcp_server.py"), encoding="utf-8").read()
    assert "Unknown focus area" not in mcp_src, (
        "focus rejection wording must live only in prompts.resolve_focus")
    return "orchestration is argparse-free; focus policy has one home"



def t_retries_respect_total_budget():
    """The timeout is the budget for the whole call, retries included.

    Regression guard: it was once applied per attempt, so three retries overran
    the caller's deadline threefold - measured at 532s against a 360s budget.
    """
    import fake_ollama

    srv = fake_ollama.start({"m": [{"name": "http500", "status": 500}]})
    try:
        cfg, _ = config.load_config()
        cfg = dict(cfg, base_url=srv.base_url, max_retries=3, backoff_base_s=2.0)
        budget = 4
        started = time.time()
        try:
            oc.generate(cfg, "m", "sys", "usr", timeout=budget)
        except oc.OllamaError as e:
            assert e.kind in ("http_5xx", "timeout"), "unexpected kind %s" % e.kind
        else:
            raise AssertionError("expected the 500s to surface as an OllamaError")
        elapsed = time.time() - started
        assert elapsed < budget * 1.6, (
            "overran the budget: %.1fs against %ds" % (elapsed, budget))
        return "gave up in %.1fs within a %ds budget" % (elapsed, budget)
    finally:
        srv.close()




def t_stdin_diff_honours_code_filter():
    cfg, _ = config.load_config()
    diff = (
        "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n"
        "@@ -1 +1 @@\n-a\n+b\n"
        "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x\n+y\n"
    )
    filtered = collect.from_text(cfg, diff, kind="stdin")
    assert [c.label for c in filtered.chunks] == ["app.py"], (
        "piped diff should drop prose, got %s" % [c.label for c in filtered.chunks])
    everything = collect.from_text(cfg, diff, kind="stdin", code_only=False)
    assert len(everything.chunks) == 2, "--all-files should keep the README"
    return "piped diffs filter like git diffs"


def t_code_filter_drops_prose():
    cfg, _ = config.load_config()
    kept = [("README.md", "docs"), ("a.py", "code"), ("yarn.lock", "junk")]
    skipped, warnings = [], []
    out = collect.apply_code_filter(list(kept), skipped, warnings, True)
    assert [l for l, _ in out] == ["a.py"], "kept %s" % [l for l, _ in out]
    assert len(skipped) == 2, "both prose files should be reported as skipped"
    assert all("--all-files" in s["reason"] for s in skipped), "reason must name the escape hatch"
    return "prose and lockfiles dropped, and reported"


def t_code_filter_yields_when_all_prose():
    """An all-docs diff must review the docs, not fail with nothing to do."""
    kept = [("README.md", "docs"), ("CHANGES.rst", "more docs")]
    skipped, warnings = [], []
    out = collect.apply_code_filter(list(kept), skipped, warnings, True)
    assert len(out) == 2, "filter should yield rather than empty the set"
    assert not skipped and warnings, "should warn, not silently skip"
    return "yields rather than leaving nothing"


def t_code_filter_off_keeps_everything():
    kept = [("README.md", "docs"), ("a.py", "code")]
    skipped = []
    out = collect.apply_code_filter(list(kept), skipped, [], False)
    assert len(out) == 2 and not skipped
    return "--all-files keeps prose"


def t_from_text_variants():
    """from_text shipped without coverage; the reviewer noticed, correctly."""
    cfg, _ = config.load_config()

    plain = collect.from_text(cfg, "x = 1\n", "snippet.py")
    assert plain.chunks[0].label == "snippet.py"
    assert "(diff)" not in plain.kind

    diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
    parsed = collect.from_text(cfg, diff, "ignored", "stdin")
    assert parsed.kind == "stdin (diff)", "diff kind was %r" % parsed.kind
    assert parsed.chunks[0].label == "a.py", "diff should split by file"

    for empty in ("", "   \n\t "):
        try:
            collect.from_text(cfg, empty)
        except collect.InputError:
            pass
        else:
            raise AssertionError("empty input %r should raise InputError" % empty)
    return "plain, diff and empty inputs all handled"


def t_serve_survives_closed_stdout():
    """A client disconnecting mid-write must not produce a traceback."""

    class ClosedPipe:
        def write(self, _):
            raise BrokenPipeError(32, "broken pipe")

        def flush(self):
            pass

    request = '{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
    rc = mcp_server.serve(stdin=iter([request]), stdout=ClosedPipe())
    assert rc == 0, "expected a clean exit, got %r" % rc
    return "broken pipe exits cleanly"


def t_mcp_handshake():
    init = mcp_server.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05"}}
    )
    assert init["result"]["serverInfo"]["name"] == "ollama-reviewer"
    assert init["result"]["capabilities"]["tools"] is not None
    listed = mcp_server.dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = [t["name"] for t in listed["result"]["tools"]]
    for want in ("ollama_review_file", "ollama_review_code", "ollama_list_models"):
        assert want in names, "missing tool %s" % want
    assert mcp_server.dispatch({"jsonrpc": "2.0", "id": 3, "method": "ping"})["result"] == {}
    return "%d tools advertised" % len(names)


def t_mcp_protocol_errors():
    bad = mcp_server.dispatch({"jsonrpc": "2.0", "id": 9, "method": "no/such"})
    assert bad["error"]["code"] == -32601, "unknown method must be -32601"
    missing = mcp_server.dispatch(
        {"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {}}
    )
    assert missing["error"]["code"] == -32602, "missing tool name must be -32602"
    note = mcp_server.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert note is None, "notifications must not get a response"
    return "-32601, -32602, notifications silent"


def t_mcp_tool_schemas():
    for t in mcp_server.TOOLS:
        assert t["name"] and t["description"], "tool missing name or description"
        schema = t["inputSchema"]
        assert schema["type"] == "object"
        for req in schema.get("required", []):
            assert req in schema["properties"], "%s requires undeclared %s" % (
                t["name"], req)
    reviewers = [t for t in mcp_server.TOOLS if t["name"].startswith("ollama_review")]
    assert reviewers, "no review tools"
    for t in reviewers:
        low = t["description"].lower()
        assert "verif" in low or "advisory" in low, (
            "%s must tell callers findings need verifying" % t["name"])
    return "%d schemas valid, advisory framing present" % len(mcp_server.TOOLS)


def t_mcp_unknown_tool_is_error():
    out = mcp_server.call_tool("not_a_tool", {})
    assert out["isError"] is True
    assert "Unknown tool" in out["content"][0]["text"]
    return "unknown tool -> isError, not an exception"


def t_mcp_dispatch_all_tools_on_fake():
    """Real dispatch of all three review tools against a fake Ollama server.

    ollama_review_file previously had no dispatch-level coverage at all; the
    other two were only ever dispatched against a live server. The stub keeps
    this offline, deterministic, and fast.
    """
    import fake_ollama

    finding = {
        "severity": "high",
        "category": "security",
        "location": "1",
        "issue": "hardcoded eval",
        "why": "arbitrary code execution",
        "suggested_fix": "remove the eval",
    }
    good = json.dumps({"findings": [finding]})
    behaviors = [{"name": "review-ok", "body": good}]
    srv = fake_ollama.start(
        {"fake:1b": list(behaviors), "fake:2b": list(behaviors)})
    # call_tool builds its own config, so the client is steered to the
    # stub through the documented OLLAMA_HOST seam rather than a cfg dict.
    try:
        with _fake_host(srv.base_url), _tmpdir() as tmp:
            target = os.path.join(tmp, "t.py")
            with open(target, "w", encoding="utf-8") as fh:
                fh.write("import os\neval(os.getenv('X'))\n")

            # 1: ollama_review_file - its first-ever tools/call.
            out1 = mcp_server.call_tool(
                "ollama_review_file", {"paths": [target], "models": ["fake:1b"]})
            assert out1["isError"] is not True, out1
            t1 = out1["content"][0]["text"]
            assert "fake:1b" in t1 and "hardcoded eval" in t1, t1[:300]

            # 2: ollama_review_code - same shared _run tail, pasted input.
            out2 = mcp_server.call_tool(
                "ollama_review_code",
                {"code": "eval(os.getenv('X'))", "label": "t.py", "models": ["fake:1b"]},
            )
            assert out2["isError"] is not True, out2
            assert "hardcoded eval" in out2["content"][0]["text"], out2["content"][0]["text"][:300]

            # 3: ollama_review_diff - the MCP-visible part of the F7 guard, with
            # no scope flags (default uncommitted scope on a non-repo must come
            # back as the unified not-a-repo tool error, not a crash).
            out3 = mcp_server.call_tool(
                "ollama_review_diff", {"cwd": tmp, "models": ["fake:1b"]})
            assert out3["isError"] is True, out3
            t3 = out3["content"][0]["text"]
            assert "Not a git repository" in t3, t3[:200]

            # 4: two scripted models exercise scaling + corroboration end to end.
            out4 = mcp_server.call_tool(
                "ollama_review_code",
                {"code": "eval(os.getenv('X'))", "label": "t.py",
                 "models": ["fake:1b", "fake:2b"]},
            )
            assert out4["isError"] is not True, out4
            t4 = out4["content"][0]["text"]
            # The exact factor is pinned by t_timeout_scaling_is_shared; at
            # dispatch level the note must fire with the real model count.
            assert "Timeout scaled to" in t4 and "for 2 models." in t4, t4[:300]
            assert "agreed by" in t4, t4[:400]
            # All generate traffic must have hit the stub and nowhere else.
            assert len(srv.log) >= 4, len(srv.log)
            assert all(r["model"] in ("fake:1b", "fake:2b") for r in srv.log)
    finally:
        srv.close()


def t_mcp_dashboard_status_tool():
    """The dashboard_status MCP tool: dispatch, all four watcher states, shapes,
    pending shutdown requests, and every mutation-marker state through the
    same call_tool path - with the page chip rendered from the same states."""
    import dashboard
    import dashpage

    saved = os.environ.get("DASHBOARD_PIDFILE")
    try:
        with _tmpdir() as tmp:
            pf = os.path.join(tmp, "watch.pid")
            os.environ["DASHBOARD_PIDFILE"] = pf  # watcher_status reads it per call

            # no pidfile -> not running
            st = dashboard.watcher_status()
            assert st == {"running": False, "pid": None, "stale": False,
                          "pidfile": pf, "pending": []}, st
            out = mcp_server.call_tool("dashboard_status", {})
            assert out["isError"] is False, out
            assert "not running" in out["content"][0]["text"], out["content"][0]["text"]

            # dead holder -> stale, reported honestly, never as "running"
            with open(pf, "w") as fh:
                fh.write(str(1 << 30))
            st = dashboard.watcher_status()
            assert st["running"] is False and st["stale"] is True, st
            assert st["pid"] == 1 << 30, st
            t = mcp_server.call_tool("dashboard_status", {})["content"][0]["text"]
            assert "stale" in t and "not running" in t, t

            # live holder -> running (this very process holds the "lock")
            with open(pf, "w") as fh:
                fh.write(str(os.getpid()))
            st = dashboard.watcher_status()
            assert st["running"] is True and st["pid"] == os.getpid(), st
            assert not st["stale"], st
            t = mcp_server.call_tool("dashboard_status", {})["content"][0]["text"]
            assert "running (pid %d)" % os.getpid() in t, t

            # corrupt pidfile -> same shape as missing, never an exception
            with open(pf, "w") as fh:
                fh.write("garbage")
            st = dashboard.watcher_status()
            assert st == {"running": False, "pid": None, "stale": False,
                          "pidfile": pf, "pending": []}, st

            # pending shutdown requests through the same body: the sentinel
            # paths ride the pidfile seam, so private files drive every state
            t = dashboard.watcher_status_text()
            assert "shutdown requests: none" in t, t
            for kind, line in (("stop", "shutdown requests: stop (watcher "
                                "exits at its next safe point)"),
                               ("pause", "shutdown requests: pause (one final "
                                "page renders, then exit)")):
                with open(pf + "." + kind, "w") as fh:
                    fh.write(kind + "\n")
                assert line in dashboard.watcher_status_text()
                os.remove(pf + "." + kind)
            with open(pf + ".stop", "w") as fh:  # both pending: stop wins
                fh.write("stop\n")
            with open(pf + ".pause", "w") as fh:
                fh.write("pause\n")
            t = dashboard.watcher_status_text()
            assert ("shutdown requests: stop+pause (both pending - stop wins)"
                    in t), t
            with open(pf, "w") as fh:  # the dangerous case: a request
                fh.write(str(1 << 30))  # outliving a dead watcher
            t = dashboard.watcher_status_text()
            assert ("shutdown requests: stop+pause" in t
                    and "not running" in t and "stale" in t), t
            os.remove(pf + ".stop")
            os.remove(pf + ".pause")

            # mutation-marker states through the same tool body: the marker
            # path is a per-call seam, so a private marker drives every state
            # without touching the ambient one (a real mutate may be running)
            saved_marker = os.environ.get("_SELFTEST_MUTATION_MARKER")
            os.environ["_SELFTEST_MUTATION_MARKER"] = mk = os.path.join(tmp, "mut.mark")

            def page_with_marker():
                """The page, rendered from a minimal gather with the marker
                state read live off the private seam."""
                page = {"rows": [], "mods": [], "up": True, "n_models": 0,
                        "live": False, "mut_ok": True, "mut_total": 20,
                        "ci": [], "ci_note": None, "commits": [], "tree": "clean"}
                page["marker"] = mutation_marker.status()
                return dashpage.render(page)

            try:
                assert "mutation marker: none" in dashboard.watcher_status_text()
                assert 'mutation marker: <b>none</b>' in page_with_marker()
                with open(mk, "w") as fh:  # live holder: this process
                    fh.write(str(os.getpid()))
                t = dashboard.watcher_status_text()
                assert ("mutation marker: in flight (holder pid %d alive"
                        % os.getpid()) in t, t
                assert ('mutation marker: <b>in flight</b> (pid %d,'
                        % os.getpid()) in page_with_marker(), t
                with open(mk, "w") as fh:  # a killed session's leftover
                    fh.write(str(1 << 30))
                t = dashboard.watcher_status_text()
                assert ("mutation marker: STRANDED (holder pid %d is dead"
                        % (1 << 30)) in t, t
                assert ('mutation marker: <b>STRANDED</b> (pid %d dead'
                        % (1 << 30)) in page_with_marker(), t
                with open(mk, "w") as fh:  # unparseable: gates until the backstop
                    fh.write("garbage")
                t = dashboard.watcher_status_text()
                assert "mutation marker: in flight (unparseable holder" in t, t
                assert ('mutation marker: <b>in flight</b> (unparseable holder'
                        ) in page_with_marker(), t
                backstop = mutation_marker.AGE_BACKSTOP_S
                with open(mk, "w") as fh:  # live holder, but past the backstop
                    fh.write(str(os.getpid()))
                stamp = time.time() - (backstop + 60)
                os.utime(mk, (stamp, stamp))
                t = dashboard.watcher_status_text()
                assert ("mutation marker: STRANDED (holder pid %d alive but past"
                        " the %ds backstop" % (os.getpid(), backstop)) in t, t
                assert ('mutation marker: <b>STRANDED</b> (pid %d past the age '
                        'backstop' % os.getpid()) in page_with_marker(), t
                assert ("marker file: %s" % mk) in t, t
            finally:
                if saved_marker is None:
                    os.environ.pop("_SELFTEST_MUTATION_MARKER", None)
                else:
                    os.environ["_SELFTEST_MUTATION_MARKER"] = saved_marker

            # wrong-typed arguments are schema-rejected like every other tool
            e = mcp_server.call_tool("dashboard_status", {"x": 1})
            assert e["isError"] is True, e
            assert "unknown argument(s) x" in e["content"][0]["text"], e
    finally:
        if saved is None:
            os.environ.pop("DASHBOARD_PIDFILE", None)
        else:
            os.environ["DASHBOARD_PIDFILE"] = saved
    return ("dispatch + 4 watcher states + pending shutdown requests + "
            "5 marker states + page chips pinned")


def t_mcp_arguments_are_typed():
    """MCP tool arguments are validated, not silently reinterpreted.

    The CLI exits 2 at the parser for contradictory input (_Once, F7); the
    MCP front end had no equivalent, so wrongly-typed JSON reached the
    engine and was silently reinterpreted: models "m1,m2" became five
    one-character model names via list(str), staged "yes" became True via
    bool(str), format "yaml" rendered markdown anyway, and a duplicated
    JSON key kept the last value (RFC 8259 leaves that choice open, so the
    caller's stated intent is unknowable). Null counts as unset, which is
    the same-as-absent behavior these arguments always had.
    """

    def err_of(name, args):
        out = mcp_server.call_tool(name, args)
        assert out["isError"] is True, out
        return out["content"][0]["text"]

    # The silent mangles, now loud. Wordings are schema-derived, so they
    # speak the schema's vocabulary ("array", "JSON boolean").
    e = err_of("ollama_review_code", {"code": "x = 1", "models": "m1,m2"})
    assert "'models' must be an array" in e and "got str" in e, e
    e = err_of("ollama_review_diff", {"cwd": ".", "staged": "yes"})
    assert "'staged' must be a JSON boolean" in e, e
    e = err_of("ollama_review_file", {"paths": "a.py"})
    assert "'paths' must be an array" in e, e
    e = err_of("ollama_review_code", {"code": "x = 1", "format": "yaml"})
    assert "'format' must be one of 'markdown', 'json', got 'yaml'." in e, e
    e = err_of("ollama_review_file", {"paths": [1, 2]})
    assert "array of strings" in e, e
    # A misspelled argument was once dropped without a trace (model vs
    # models); now it names the miss and the valid set.
    e = err_of("ollama_review_code", {"code": "x = 1", "model": "fake:1b"})
    assert "unknown argument(s) model. Valid: " in e, e
    # focus accepts a comma-separated string deliberately (resolve_focus).
    e = err_of("ollama_review_code", {"code": "x = 1", "focus": "security,bogus"})
    assert "Unknown focus area(s): bogus" in e, e
    # arguments itself must be an object - the old `args or {}` made []
    # indistinguishable from "no arguments".
    e = err_of("ollama_review_file", [])
    assert "must be a JSON object" in e, e

    # null is unset, not an error: a well-formed call to a missing file
    # still reaches the collector and fails with its own message.
    out = mcp_server.call_tool(
        "ollama_review_file",
        {"paths": ["/definitely/not/here.py"], "models": None, "format": None},
    )
    assert out["isError"] is True, out
    assert "No reviewable files" in out["content"][0]["text"], out

    # Duplicate JSON keys are rejected at the parse seam (-32700), not
    # last-one-wins; the response carries no id because the line never
    # parsed.
    line = (
        '{"jsonrpc":"2.0","id":7,"method":"tools/call","params":'
        '{"name":"ollama_review_code","arguments":'
        '{"code":"x","code":"y"}}}'
    )
    buf = io.StringIO()
    mcp_server.serve(stdin=iter([line]), stdout=buf)
    resp = json.loads(buf.getvalue())
    assert "error" in resp and resp["error"]["code"] == -32700, (
        "duplicated key must be a parse error, got: %s" % (buf.getvalue().strip(),))
    assert resp["id"] is None and "Parse error" in resp["error"]["message"], resp

    # The duplicate-key hook must not disturb well-formed traffic.
    buf2 = io.StringIO()
    mcp_server.serve(
        stdin=iter(['{"jsonrpc":"2.0","id":1,"method":"ping"}']), stdout=buf2)
    assert json.loads(buf2.getvalue()) == {"jsonrpc": "2.0", "id": 1, "result": {}}, (
        buf2.getvalue())

    return "wrong types loud; dup keys parse-error; null unset; ping still parses"


def t_mcp_error_paths_keep_shape():
    """Every error path returns the full content+isError shape - never a bare
    string, a partial dict, or an escaping exception.

    Clients branch on this shape, so it is a contract: tools/call failures
    are MCP *results* (isError true, one text content part), protocol
    failures are JSON-RPC *errors*, and the two are never mixed. Walks every
    error path reachable offline: dispatcher rejections, argument
    validation, collector rejections, a dead server (OllamaError), and an
    unexpected exception via a patched collector to prove the catch-all.
    Ok-path shapes are pinned by the fake-server dispatch check.
    """

    def shaped(out, expect_error=True):
        # call_tool's contract: never raises, always the full shape.
        assert isinstance(out, dict), "bare/partial return: %r" % (out,)
        assert set(out) == {"content", "isError"}, sorted(out)
        assert isinstance(out["isError"], bool), out["isError"]
        assert out["isError"] is expect_error, out
        assert isinstance(out["content"], list) and len(out["content"]) == 1, out
        part = out["content"][0]
        assert part.get("type") == "text", part
        assert isinstance(part.get("text"), str) and part["text"].strip(), part
        return part["text"]

    # Dispatcher-level rejections - config load only, no server.
    shaped(mcp_server.call_tool("no_such_tool", {}))
    shaped(mcp_server.call_tool("ollama_review_file", []))    # args not an object
    shaped(mcp_server.call_tool("ollama_review_file", {"model": "x"}))  # unknown arg
    shaped(mcp_server.call_tool("ollama_review_file", {"paths": "a.py"}))  # wrong type
    shaped(mcp_server.call_tool("ollama_review_file", {"paths": []}))  # nothing to review

    # Collector rejection with a real (missing) path.
    shaped(mcp_server.call_tool(
        "ollama_review_file", {"paths": ["/definitely/not/here.py"]}))

    with _tmpdir() as tmp:
        target = os.path.join(tmp, "t.py")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("x = 1\n")

        # Focus validation failure - reachable only past collection.
        shaped(mcp_server.call_tool(
            "ollama_review_file", {"paths": [target], "focus": "bogus"}))

        # Dead server: the OllamaError path, host pinned to a refused port.
        with _fake_host("http://127.0.0.1:9"):
            shaped(mcp_server.call_tool(
                "ollama_review_file", {"paths": [target]}))

        # Unexpected exception: the collector blows up; the catch-all must
        # still return the full shape instead of letting it escape.
        orig = collect.from_files

        def boom(*a, **k):
            raise RuntimeError("boom")

        collect.from_files = boom
        try:
            text = shaped(mcp_server.call_tool(
                "ollama_review_file", {"paths": [target]}))
            assert "Internal error" in text, text
        finally:
            collect.from_files = orig

    # Tool failures ride in results, protocol failures in errors.
    for args in ({}, {"arguments": "oops"}):
        r = mcp_server.dispatch({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                                 "params": dict({"name": "no_such_tool"}, **args)})
        assert r["jsonrpc"] == "2.0" and r["id"] == 5, r
        assert "error" not in r, r          # a tool failure is NOT a protocol error
        shaped(r["result"])
    for msg_id, bad in ((6, {"method": "no/such"}),
                        (7, {"method": "tools/call", "params": {}})):
        r = mcp_server.dispatch(dict({"jsonrpc": "2.0", "id": msg_id}, **bad))
        assert "result" not in r and r["id"] == msg_id, r
        assert isinstance(r["error"]["code"], int) and r["error"]["message"], r

    return "error paths full-shaped; tool-vs-protocol split pinned; catch-all proven"


def t_mcp_stdio_end_to_end():
    """The real mcp_server.py process, driven over stdio with JSON-RPC frames.

    The in-process dispatch checks bypass serve() entirely - newline
    framing, the stdin/stdout loop, and process lifecycle are only
    exercised by spawning the server the way an MCP client does: requests
    in, exactly one response line per id-ed request, silence for
    notifications. The review tool calls run against the fake Ollama, so
    this stays offline while covering the whole wire - including how
    consensus corroboration tags render over it - and a degraded run where
    one model fights the transport while the other succeeds.
    """
    import fake_ollama

    finding = {
        "severity": "high",
        "category": "security",
        "location": "1",
        "issue": "hardcoded eval",
        "why": "arbitrary code execution",
        "suggested_fix": "remove the eval",
    }
    good = json.dumps({"findings": [finding]})
    behaviors = [{"name": "review-ok", "body": good}]
    # The fight recipe (mirrors t_degradation_notes_compress scenario 2):
    # tier 1 answers prose, tier 2 dies with HTTP 500 - fake:2b degrades
    # while fake:1b keeps succeeding, so the run is partial, not failed.
    fight = [
        {"name": "tier1-prose", "body": "I looked at the code. It is fine."},
        {"name": "tier2-http500", "status": 500},
    ]
    srv = fake_ollama.start(
        {"fake:1b": list(behaviors), "fake:2b": list(behaviors) + fight})
    try:
        with _tmpdir() as tmp:
            target = os.path.join(tmp, "t.py")
            with open(target, "w", encoding="utf-8") as fh:
                fh.write("import os\neval(os.getenv('X'))\n")

            def frame(msg_id, method, params=None):
                m = {"jsonrpc": "2.0", "id": msg_id, "method": method}
                if params is not None:
                    m["params"] = params
                return json.dumps(m) + "\n"

            frames = [
                frame(1, "initialize", {"protocolVersion": "2024-11-05"}),
                # A notification: no id, and it must get no response line.
                '{"jsonrpc": "2.0", "method": "notifications/initialized"}\n',
                frame(2, "tools/list"),
                frame(3, "tools/call", {
                    "name": "ollama_review_file",
                    "arguments": {"paths": [target], "models": ["fake:1b"]},
                }),
                frame(4, "tools/call", {"name": "no_such_tool"}),
                frame(5, "tools/call", {
                    "name": "ollama_review_file",
                    "arguments": {"paths": "t.py"},
                }),
                # Two models, identical findings -> corroborated over the wire.
                frame(6, "tools/call", {
                    "name": "ollama_review_file",
                    "arguments": {"paths": [target],
                                  "models": ["fake:1b", "fake:2b"]},
                }),
                # Same call, but fake:2b now fights the transport: a partial
                # run whose Degradations section must reach the tool result.
                frame(7, "tools/call", {
                    "name": "ollama_review_file",
                    "arguments": {"paths": [target],
                                  "models": ["fake:1b", "fake:2b"]},
                }),
                # dashboard_status over the wire: read-only watcher report,
                # asserted state-agnostically (CI has no watcher; a dev box
                # may - both must satisfy the same shape contract).
                frame(8, "tools/call", {
                    "name": "dashboard_status",
                    "arguments": {},
                }),
                # Same tool, unknown argument: the schema guard must fire
                # over the wire exactly as it does in-process.
                frame(9, "tools/call", {
                    "name": "dashboard_status",
                    "arguments": {"stale": "yes"},
                }),
            ]
            here = os.path.dirname(os.path.abspath(__file__))
            proc = subprocess.run(
                [sys.executable, os.path.join(here, "mcp_server.py")],
                input="".join(frames), capture_output=True, text=True,
                timeout=60, cwd=here,
                env=dict(os.environ, OLLAMA_HOST=srv.base_url),
            )
            assert proc.returncode == 0, (proc.returncode, proc.stderr[-400:])

            lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
            assert len(lines) == 9, (
                "expected 9 response lines (notification silent), got %d; "
                "stdout=%r" % (len(lines), proc.stdout[:400]))
            by_id = {}
            for ln in lines:
                r = json.loads(ln)
                assert r["id"] is not None, r
                by_id[r["id"]] = r

            init = by_id[1]["result"]
            assert init["serverInfo"]["name"] == "ollama-reviewer", init
            tools = [t["name"] for t in by_id[2]["result"]["tools"]]
            assert "ollama_review_file" in tools and len(tools) == 5, tools

            ok = by_id[3]["result"]
            assert ok["isError"] is False, ok
            t3 = ok["content"][0]["text"]
            assert "hardcoded eval" in t3, ok["content"][0]
            # Single-model run: the model is named in the report title and
            # no per-finding tag renders at all - raised_by only exists
            # after multi-model reconciliation.
            assert "# Local review - fake:1b" in t3, t3
            assert "agreed by" not in t3, t3

            bad = by_id[4]            # unknown tool: a result, never an error
            assert "error" not in bad, bad
            assert bad["result"]["isError"] is True, bad
            assert "Unknown tool" in bad["result"]["content"][0]["text"], bad

            typed = by_id[5]["result"]  # schema-derived rejection, pre-flight
            assert typed["isError"] is True and "must be an array" in (
                typed["content"][0]["text"]), typed

            cons = by_id[6]["result"]   # two models -> corroboration tag
            assert cons["isError"] is False, cons
            t6 = cons["content"][0]["text"]
            assert "agreed by fake:1b, fake:2b" in t6, t6[:500]
            assert "After reconciling: **1 corroborated**" in t6, t6[:500]
            assert "-- only " not in t6, t6[:500]

            deg = by_id[7]["result"]    # one model fights -> partial, not error
            assert deg["isError"] is False, (
                "a partial run is a successful tool result")
            t7 = deg["content"][0]["text"]
            assert "## Degradations" in t7, t7[:500]
            # The section proper: from the heading to the findings block.
            section = t7.split("## Degradations")[1].split("\n### ")[0]
            assert "(fake:2b): second pass failed (http_5xx)" in section, (
                t7[:600])
            # Finding tags stay out of the section; the surviving finding
            # carries the lone-model tag on the wire.
            assert "only fake:1b" not in section, section
            assert "only fake:1b" in t7, t7[:600]
            assert "Status: **partial**" in t7, t7[:600]

            dash = by_id[8]["result"]   # watcher report over the wire
            assert dash["isError"] is False, dash
            t8 = dash["content"][0]["text"]
            assert "dashboard watcher:" in t8 and "lock:" in t8, t8
            # Shape agreement, whichever state the host is in: text and the
            # not-running/running wording must not contradict each other.
            if "not running" in t8:
                assert "stale" in t8 or "pid" in t8, t8
            else:
                assert "running (pid " in t8, t8

            dash_bad = by_id[9]["result"]  # schema guard over the wire
            assert dash_bad["isError"] is True, dash_bad
            assert "unknown argument(s) stale" in (
                dash_bad["content"][0]["text"]), dash_bad
    finally:
        srv.close()

    return ("spawned stdio server: 10 frames in, 9 responses out, clean, "
            "consensus, degraded run and dashboard_status via fake ollama")


def t_render_never_crashes():
    md = render.to_markdown({"status": "error", "error": {"detail": "d", "remedy": "r"}})
    assert "unavailable" in md.lower()
    md2 = render.to_markdown(
        {"status": "ok", "model": "m", "input": {}, "findings": [], "elapsed_s": 1.0}
    )
    assert "No findings" in md2
    return "error and empty renders both fine"


def t_context_covers_max_input():
    """A max-size chunk must not exceed the context we ask Ollama for.

    Regression guard: num_ctx was once hardcoded below max_file_chars, which made
    the server silently drop input while the review still reported confidently.
    """
    cfg, _ = config.load_config()
    big = "x" * int(cfg["max_file_chars"])
    system = prompts.build_system_prompt()
    user = prompts.build_user_prompt("big.py", big, "files", prompts.DEFAULT_FOCUS)
    ctx = oc.size_context(system, user)
    needed = (len(system) + len(user)) / oc.CHARS_PER_TOKEN
    assert ctx >= needed, "num_ctx %d below estimated prompt %d" % (ctx, needed)
    assert ctx <= oc.CTX_MAX
    return "max input needs ~%d tokens, num_ctx=%d" % (needed, ctx)


def t_context_scales_down():
    small = oc.size_context("sys", "tiny prompt")
    assert small == oc.CTX_MIN, "small prompts should use the floor, got %d" % small
    return "floor %d" % small


def t_prompt_shape():
    u = prompts.build_user_prompt(
        "a.py", "print(1)", "files", prompts.DEFAULT_FOCUS, adversarial=True
    )
    assert "ADVERSARIAL MODE" in u
    assert "findings" in u
    s = prompts.build_system_prompt()
    assert "triggering condition" in s or "concrete condition" in s
    return "%d chars system, %d chars user" % (len(s), len(u))


# --------------------------------------------------------------------------
# regression guards from the 2026-09 audit (one per fix)
# --------------------------------------------------------------------------

def t_render_sorts_corroborated_first():
    """Markdown must keep the documented corroborated-first order.

    Regression guard: the renderer re-sorted by severity, so a lone critical
    outranked a two-model-agreed high in Markdown while --json showed the
    consensus order.
    """
    agreed = _finding(sev="high", loc="a.py:10")
    agreed2 = _finding(sev="high", loc="a.py:11")
    lone = _finding(sev="critical", cat="logic", loc="q.py:1", issue="off by one")
    merged = consensus.sort_merged(
        consensus.reconcile([("m1", [agreed, lone]), ("m2", [agreed2])]))
    md = render.to_markdown({
        "status": "ok", "models": ["m1", "m2"], "input": {},
        "findings": merged, "elapsed_s": 1.0})
    first = md.split("### 1. ")[1].split("### 2. ")[0]
    assert "[HIGH]" in first and "agreed by" in first, first
    second = md.split("### 2. ")[1]
    assert "[CRITICAL]" in second, "lone critical must come second"
    # The other tag branch: a finding only one model raised, inside a
    # multi-model run, renders "only m1" - never the corroboration form.
    assert "only m1" in second and "agreed by" not in second, second
    single = render.to_markdown({
        "status": "ok", "model": "m1", "input": {},
        "findings": [lone, agreed], "elapsed_s": 1.0})
    assert single.index("### 1. [CRITICAL]") < single.index("[HIGH]"), (
        "single-model runs keep plain severity order")
    return "markdown order matches consensus"


def t_status_renders_unresolved():
    """A missing resolved model renders as (unresolved), never a bare None."""
    md = render.status_markdown({
        "status": "ok", "base_url": "http://127.0.0.1:11434",
        "configured_model": "m", "resolved_model": None,
        "fallback_models": [], "models": [],
    })
    assert "(unresolved)" in md, md
    assert "None" not in md.replace("(unresolved)", ""), "bare None leaked"
    return "status shows (unresolved), not None"


def t_markdown_shows_budget():
    """The markdown header shows the effective budget next to time spent.

    Regression guard: run_pipeline's budget decision (result["timeout_s"])
    was JSON-only; the markdown header showed elapsed time with nothing to
    read it against, so "1.2s" could not be told from "1.2s of a 3600s one".
    A result without the key (hand-made dicts, the error path) renders the
    old bare header rather than "of None".
    """
    md = render.to_markdown({
        "status": "ok", "model": "m", "input": {}, "findings": [],
        "elapsed_s": 1.2, "timeout_s": 360})
    assert "1.2s of 360s budget" in md, md[:400]
    bare = render.to_markdown({
        "status": "ok", "model": "m", "input": {}, "findings": [],
        "elapsed_s": 1.2})
    assert "of None" not in bare and "budget" not in bare, bare[:400]
    assert "1.2s" in bare, bare[:400]
    return "header reads '1.2s of 360s budget'; absent budget renders bare"


def t_markdown_degradations_section():
    """Degradations render under a labeled section, not generic Note bullets.

    Regression guard: degradations and front-end notes shared one `notes`
    list, so the report introduced everything as "- Note:" and a reader
    could not tell what the run survived from what the front end did. The
    engine now returns `degradations` separately; the section also folds in
    chunk_errors, which used to be bare bullets. Old results without the
    key keep rendering their notes as bullets.
    """
    base = {"status": "partial", "model": "m", "input": {}, "elapsed_s": 1.0}
    md = render.to_markdown(dict(
        base,
        degradations=["msg (m) on 2 chunk(s): a.py, b.py"],
        chunk_errors=[{"label": "a.py", "model": "m",
                       "error": {"detail": "budget exhausted"}}],
        notes=["Timeout scaled to 90s for 1 models.",
               "msg (m) on 2 chunk(s): a.py, b.py"],
        findings=[],
    ))
    assert "## Degradations" in md, md[:400]
    # The section proper: heading up to the next block, not the whole tail.
    section = md.split("## Degradations")[1].split("\n**")[0]
    assert "- msg (m) on 2 chunk(s): a.py, b.py" in section, section
    assert "- chunk `a.py` failed: budget exhausted" in section, section
    # Process notes stay outside the labeled section; the engine's echoed
    # degradation copy in notes is deduped, not doubled as a Note bullet.
    assert "- Note: Timeout scaled" in md and "Timeout scaled" not in section, md[:400]
    assert "- Note: msg (m)" not in md, md[:400]
    # chunk_errors must not ALSO appear as a bare bullet anywhere.
    assert md.count("failed: budget exhausted") == 1, md
    # An old-shaped result (no degradations key) still renders its notes.
    old = render.to_markdown(dict(
        base, notes=["a process note"], findings=[]))
    assert "- Note: a process note" in old and "## Degradations" not in old, old[:400]
    # Neither key: clean render, no stray section.
    empty = render.to_markdown(dict(base, findings=[]))
    assert "Degradations" not in empty and "Note:" not in empty, empty[:400]
    return "degradations labeled; chunk failures folded in; legacy shape intact"


def t_no_double_prefixed_locations():
    """Bare locations get the chunk label once; file-naming ones do not.

    Regression guard: a location like collect.py:42 became
    scripts/collect.py: collect.py:42 because the prefix step ignored the
    file the model had already named.
    """
    chunk = collect.Chunk("scripts/collect.py", "x = 1")
    named = {"location": "collect.py:42"}
    review._qualify_location(named, chunk)
    assert named["location"] == "collect.py:42", named["location"]
    bare = {"location": "the retry loop"}
    review._qualify_location(bare, chunk)
    assert bare["location"] == "scripts/collect.py: the retry loop", bare["location"]
    empty = {"location": ""}
    review._qualify_location(empty, chunk)
    assert empty["location"] == "scripts/collect.py: ", empty["location"]
    sep_chunk = collect.Chunk("scripts" + os.sep + "collect.py", "y = 2")
    sep_named = {"location": "collect.py:7"}
    review._qualify_location(sep_named, sep_chunk)
    assert sep_named["location"] == "collect.py:7", sep_named["location"]
    return "prefixed once, named locations untouched"

def t_pinned_timeout_is_not_scaled():
    """An explicit --timeout must be honoured, not silently rescaled.

    Regression guard: when timeout scaling moved behind the shared engine
    entry point, the pin gate existed in the signature but not the body, so
    a pinned --timeout was silently scaled anyway. The suite had never
    driven this path; this check makes it permanent.
    """
    import fake_ollama

    good = json.dumps(
        {"findings": [{"line": 1, "severity": "high", "category": "correctness",
                       "title": "t", "detail": "d", "suggestion": "s",
                       "confidence": "high"}]})
    scripts = {"fake:1b": [{"body": good}] * 3,
               "fake:2b": [{"body": good}] * 3}
    srv = fake_ollama.start(scripts)
    try:
        with _fake_host(srv.base_url), _tmpdir() as tmp:
            target = os.path.join(tmp, "vuln.py")
            with open(target, "w", encoding="utf-8") as fh:
                fh.write("x = 1 + 2")
            base_timeout = int(config.load_config()[0]["timeout_s"])
            pinned = _capture_cli([
                "review", "--file", target, "--json",
                "--models", "fake:1b,fake:2b", "--timeout", "123"])
            result = json.loads(pinned[1])
            assert pinned[0] == 0 and result["status"] == "ok", pinned
            assert not any(
                "scaled" in n.lower() for n in result.get("notes", [])), result["notes"]
            assert result["timeout_s"] == 123, (
                "pinned budget must be returned in the result, got %s"
                % result["timeout_s"])
            loose = _capture_cli([
                "review", "--file", target, "--json",
                "--models", "fake:1b,fake:2b"])
            result2 = json.loads(loose[1])
            assert loose[0] == 0 and result2["status"] == "ok", loose
            assert any(
                "scaled" in n.lower() for n in result2.get("notes", [])), result2["notes"]
            assert result2["timeout_s"] == base_timeout * 2, (
                "scaled budget must travel in the result, got %s"
                % result2["timeout_s"])
    finally:
        srv.close()
    return "explicit --timeout honoured; unpinned run scales"


def t_all_chunks_failed_is_an_error():
    """Every model failing on every chunk must fail loudly, not exit 0.

    Regression guard: the ReviewFailure raise was gated on a model dropping
    fatally, so non-fatal kinds (5xx, budget exhaustion) returned success
    with status "partial" - scripts checking exit codes saw a clean 0 for
    a review where nothing came back at all.
    """
    import fake_ollama

    srv = fake_ollama.start({"fake:1b": [{"status": 500}] * 4})
    try:
        with _fake_host(srv.base_url), _tmpdir() as tmp:
            target = os.path.join(tmp, "vuln.py")
            with open(target, "w", encoding="utf-8") as fh:
                fh.write("x = 1 + 2")
            code, out = _capture_cli(
                ["review", "--file", target, "--json", "--models", "fake:1b"])
            rep = json.loads(out)
            assert code == 3, "total loss must exit 3, got %s" % code
            assert rep["status"] == "error", rep["status"]
            assert rep["error"]["kind"] == "http_5xx", rep["error"]
            assert not rep["findings"], rep["findings"]
            assert rep["error"]["detail"] and rep["error"]["remedy"], rep["error"]
    finally:
        srv.close()
    return "total loss exits 3 with a typed error, not success"


def t_partial_run_keeps_surviving_findings():
    """A failing chunk degrades the run but preserves everything else.

    Wire proof of the partial contract: status "partial", the surviving
    model's findings from both chunks, chunk_errors naming each failing
    chunk x model combo, and the degradation visible in markdown.
    """
    import fake_ollama

    good = json.dumps(
        {"findings": [{"line": 1, "severity": "high", "category": "correctness",
                       "location": "1", "title": "t", "detail": "d",
                       "suggestion": "s", "confidence": "high"}]})
    diff = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1 +1 @@
-x
+y
diff --git a/b.py b/b.py
--- a/b.py
+++ b/b.py
@@ -1 +1 @@
-x
+y
"""
    srv = fake_ollama.start({
        "fake:1b": [{"body": good}] * 3,
        "fake:2b": [{"status": 500}] * 5,
    })
    try:
        cfg, _ = config.load_config()
        cfg = dict(cfg, base_url=srv.base_url)
        inp = collect.from_text(cfg, diff, "ignored", "stdin")
        labels = [c.label for c in inp.chunks]
        assert labels == ["a.py", "b.py"], labels
        notes = []
        result = review.run_pipeline(
            cfg, ["fake:1b", "fake:2b"], inp, prompts.DEFAULT_FOCUS,
            review.ReviewOptions(), notes)
        assert result["status"] == "partial", result["status"]
        assert result["error"] is None, result["error"]
        findings = result["findings"]
        assert len(findings) >= 2, findings
        covered = set()
        for f in findings:
            for label in labels:
                if (f.get("location") or "").startswith(label):
                    covered.add(label)
        assert covered == set(labels), [f.get("location") for f in findings]
        failing = [(ce["label"], ce["model"]) for ce in result["chunk_errors"]]
        assert sorted(set(l for l, _ in failing)) == labels, failing
        assert all(m == "fake:2b" for _, m in failing), failing
        assert not any("Dropped" in n for n in result["notes"]), result["notes"]
        md = render.to_markdown(result)
        assert "## Degradations" in md, md[:600]
        section = md.split("## Degradations")[1]
        assert "- chunk `a.py` failed:" in section, section
        assert "- chunk `b.py` failed:" in section, section
        # Front-end process notes still render, outside the labeled section.
        assert "- Note: Timeout scaled" in md, md[:600]
        assert all(r["model"] in ("fake:1b", "fake:2b") for r in srv.log)
    finally:
        srv.close()
    return "partial: surviving findings kept, failures named and rendered"


def t_fatal_drop_does_not_kill_the_run():
    """A model dying fatally drops out; the others carry on every chunk.

    The stub cannot script a fatal kind by construction - the client maps
    connect failures to unreachable and scripted HTTP to 4xx/5xx - so the
    fatal error is injected at oc.generate, keeping the rest of the run on
    the real wire. Proves the FATAL_KINDS contract: the dead model is
    dropped after its first chunk, the drop is reported, and the survivor
    still reviews every remaining chunk.
    """
    import fake_ollama

    good = json.dumps(
        {"findings": [{"line": 1, "severity": "high", "category": "correctness",
                       "location": "1", "title": "t", "detail": "d",
                       "suggestion": "s", "confidence": "high"}]})
    diff = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1 +1 @@
-x
+y
diff --git a/b.py b/b.py
--- a/b.py
+++ b/b.py
@@ -1 +1 @@
-x
+y
"""
    srv = fake_ollama.start({
        "fake:1b": [{"body": good}] * 3,
        "fake:2b": [{"body": good}] * 3,
    })
    orig = oc.generate
    try:
        def poisoned(cfg, model, *a, **k):
            if model == "fake:2b":
                raise oc.OllamaError(
                    "unreachable", "simulated server death mid-run",
                    "start the server and retry")
            return orig(cfg, model, *a, **k)
        oc.generate = poisoned
        cfg, _ = config.load_config()
        cfg = dict(cfg, base_url=srv.base_url)
        inp = collect.from_text(cfg, diff, "ignored", "stdin")
        labels = [c.label for c in inp.chunks]
        notes = []
        result = review.run_pipeline(
            cfg, ["fake:1b", "fake:2b"], inp, prompts.DEFAULT_FOCUS,
            review.ReviewOptions(), notes)
        assert result["status"] == "partial", result["status"]
        covered = set()
        for f in result["findings"]:
            for label in labels:
                if (f.get("location") or "").startswith(label):
                    covered.add(label)
        assert covered == set(labels), covered
        assert [n for n in result["notes"] if "Dropped fake:2b" in n], (
            result["notes"])
        assert [ce["model"] for ce in result["chunk_errors"]] == ["fake:2b"], (
            result["chunk_errors"])
        assert [ce["error"]["kind"] for ce in result["chunk_errors"]] == [
            "unreachable"], result["chunk_errors"]
        assert len(srv.log) == 2 and all(
            r["model"] == "fake:1b" for r in srv.log), srv.log
    finally:
        oc.generate = orig
        srv.close()
    return "fatal drop: dead model gone, survivor finished every chunk"



def t_engine_leaves_caller_state_untouched():
    """run_pipeline must not mutate the caller's cfg or notes list.

    Ownership pass: the budget decision used to be written into the
    caller's cfg dict (180 -> 360 on the caller's object) and notes were
    appended into the caller's list by reference, so the engine's state
    leaked into its caller. Both now live only in the returned dict --
    result["timeout_s"] and result["notes"] -- and the caller's objects
    are provably unchanged.
    """
    import fake_ollama

    good = json.dumps(
        {"findings": [{"line": 1, "severity": "high", "category": "correctness",
                       "location": "1", "title": "t", "detail": "d",
                       "suggestion": "s", "confidence": "high"}]})
    srv = fake_ollama.start({
        "fake:1b": [{"body": good}] * 2,
        "fake:2b": [{"body": good}] * 2,
    })
    try:
        cfg, _ = config.load_config()
        cfg = dict(cfg, base_url=srv.base_url)
        seed = ["a seed note from the caller"]
        base = cfg["timeout_s"]
        inp = collect.from_text(cfg, "x = 1" + chr(10), "t.py")
        result = review.run_pipeline(
            cfg, ["fake:1b", "fake:2b"], inp, prompts.DEFAULT_FOCUS,
            review.ReviewOptions(), seed)
        assert cfg["timeout_s"] == base, (
            "run_pipeline rewrote the caller's cfg: %s -> %s"
            % (base, cfg["timeout_s"]))
        assert seed == ["a seed note from the caller"], (
            "caller's notes list was mutated: %r" % seed)
        assert result["timeout_s"] == base * 2, (
            "scaled budget must be returned in the result, got %s"
            % result["timeout_s"])
        assert result["notes"][0] == "a seed note from the caller", (
            result["notes"])
        assert any("Timeout scaled" in n for n in result["notes"]), (
            result["notes"])
    finally:
        srv.close()
    return "engine returns its budget and notes; caller state untouched"



def t_status_snapshot_is_shared():
    """Both front ends must build status through one engine snapshot.

    The payload assembly (model table, human sizes, resolution notes)
    used to live twice, once per front end, and could drift. It now has
    one home beside the raw material it shapes.
    """
    import fake_ollama

    srv = fake_ollama.start({"fake:1b": [{"body": "{}"}]})
    try:
        with _fake_host(srv.base_url):
            hit = _capture_cli(["status", "--json", "--model", "fake:1b"])
            hit_data = json.loads(hit[1])
            assert hit[0] == 0 and hit_data["status"] == "ok", hit
            assert hit_data["resolved_model"] == "fake:1b", hit_data
            assert hit_data["models"] and all(
                "size_h" in m and "context" in m for m in hit_data["models"]), (
                hit_data["models"])
            miss = _capture_cli(["status", "--json", "--model", "ghost:7b"])
            miss_data = json.loads(miss[1])
            assert miss[0] == 0 and miss_data["status"] == "ok", miss
            assert miss_data["resolved_model"] is None, miss_data
            assert any(
                "ghost:7b" in n for n in miss_data.get("notes", [])), miss_data["notes"]
            out = mcp_server.call_tool("ollama_list_models", {})
            assert out["isError"] is not True, out
            assert "fake:1b" in out["content"][0]["text"], out["content"][0]["text"]
    finally:
        srv.close()
    here = os.path.dirname(os.path.abspath(__file__))
    for front in ("cli.py", "mcp_server.py"):
        src = open(os.path.join(here, front), encoding="utf-8").read()
        assert "status_snapshot(" in src, (
            front + " must build status through the engine snapshot")
        for symbol in ("human_size(", "base_url", "configured_model"):
            assert symbol not in src, (
                front + " must not assemble the status payload itself: " + symbol)
    assert "def status_snapshot" in open(
        os.path.join(here, "ollama_client.py"), encoding="utf-8").read(), (
        "the snapshot must live in the engine client layer")
    assert "def human_size" in open(
        os.path.join(here, "ollama_client.py"), encoding="utf-8").read()
    assert "def human_size" not in open(
        os.path.join(here, "render.py"), encoding="utf-8").read(), (
        "human_size is a status-payload concern now, not a renderer")
    return "one snapshot, two front ends, both paths pinned"


def t_timeout_scaling_is_shared():
    """CLI and MCP must apply the same timeout-scaling rule.

    Regression guard: only the CLI scaled the budget for multi-model runs, so
    an MCP review with N models starved the later chunks. Both front ends now
    go through review.run_pipeline, the one assembly of resolve/scale/run.
    """
    assert review.scale_timeout_for_models(180, 1) == (180, None)
    t, note = review.scale_timeout_for_models(180, 3)
    assert t == 540, "3 models should triple the budget, got %s" % t
    assert "540" in note and "3 models" in note, note
    here = os.path.dirname(os.path.abspath(__file__))
    mcp_src = open(os.path.join(here, "mcp_server.py"), encoding="utf-8").read()
    assert "review.run_pipeline" in mcp_src, (
        "mcp_server.py must go through the engine entry point")
    assert "import cli" not in mcp_src, (
        "mcp_server.py is a front end; it must not import another front end")
    cli_src = open(os.path.join(here, "cli.py"), encoding="utf-8").read()
    assert "review.run_pipeline" in cli_src, (
        "cli.py must go through the engine entry point")
    for front in (cli_src, mcp_src):
        for symbol in ("resolve_models(", "scale_timeout_for_models(", "run_review("):
            assert symbol not in front, (
                "front ends must not bypass run_pipeline: %s" % symbol)
    assert "def run_pipeline" in open(os.path.join(here, "review.py"), encoding="utf-8").read(), (
        "the entry point must live in the engine")
    return "one pipeline, two front ends"

def t_conflicting_source_flags_fail():
    """Asking for two input sources is an error, never a silent priority.

    Regression guard: --staged with --ref fell through to from_git, where ref
    won and the tool exited 0 after reviewing a diff the user did not ask
    for. The engine guard (from_git) covers the MCP front end too, which
    exposes ref and staged on the same tool.
    """
    cfg, _ = config.load_config()
    with _repo(files={"a.py": "x = 1\n"}) as tmp:
        _git(["add", "a.py"], tmp)
        _git(["commit", "-m", "one"], tmp)
        _write(os.path.join(tmp, "a.py"), "x = 2\n")
        _git(["add", "a.py"], tmp)
        # A stated --ref conflicts even when empty: argparse delivers --ref ""
        # and MCP callers can send {"ref": ""}; truthiness here once let
        # staged win silently (exit 0).
        for ref in ("HEAD", ""):
            try:
                collect.from_git(cfg, ref=ref, staged=True, cwd=tmp)
                raise AssertionError("from_git accepted ref and staged together")
            except collect.InputError as e:
                assert "--staged" in e.detail and "--ref" in e.detail, e.detail
            code, out = _capture_cli(["review", "--staged", "--ref", ref, "--json"])
            assert code == 2, "conflicting flags must exit 2, got %s" % code
            err = json.loads(out).get("error", {})
            assert err.get("kind") == "input", err
            assert "--staged" in err.get("detail", ""), err
    return "conflicting sources rejected at both front ends, empty stated --ref included"


def t_repeated_flags_neither_silent():
    """Repeating --file unions; repeating any scalar override exits 2.

    Regression guard: argparse's default store silently kept the last value,
    so `--file a.py --file b.py --file c.py` reviewed one file while exiting
    0, and any repeated override (--ref, --cwd, --timeout, ...) silently let
    the last occurrence win - the same class of bug as the conflicting-sources
    guard above, just quieter. The 2026-09 audit: booleans are idempotent and
    stay plain; --file unions (a set of paths is unambiguous); every scalar
    override uses _Once and exits 2 at the parser, naming the form that does
    mean "several" where one exists.
    """
    from cli import build_parser

    # --file repeats union: all paths survive, none dropped.
    args = build_parser().parse_args(
        ["review", "--file", "a.py", "b.py", "--file", "c.py"])
    assert args.file == ["a.py", "b.py", "c.py"], args.file

    # Every scalar override rejects its second occurrence with exit 2 and a
    # remedy, at the parser - before any server call. --models keeps its
    # specific hint; the rest get the plain pass-it-once form. Each entry
    # carries its full argv: splicing name.split() in front of the flags
    # would put a second --flag token where its own value belongs.
    twice = {
        "review --model": (
            ["review", "--model", "m1", "--model", "m2"], "--models a,b"),
        "review --models": (
            ["review", "--models", "a,b", "--models", "c,d"], "--models a,b"),
        "status --model": (
            ["status", "--model", "m1", "--model", "m2"], None),
        "review --ref": (["review", "--ref", "HEAD~1", "--ref", "HEAD"], None),
        "review --cwd": (
            ["review", "--cwd", "somewhere", "--cwd", "elsewhere"], None),
        "review --focus": (
            ["review", "--focus", "security", "--focus", "logic"], None),
        "review --instructions": (
            ["review", "--instructions", "a", "--instructions", "b"], None),
        "review --temperature": (
            ["review", "--temperature", "0.2", "--temperature", "0.9"], None),
        "review --timeout": (
            ["review", "--timeout", "30", "--timeout", "90"], None),
    }
    for name, (argv, hint) in sorted(twice.items()):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = 0
            try:
                build_parser().parse_args(argv)
            except SystemExit as e:
                code = e.code
        assert code == 2, "%s repeated must exit 2, got %s" % (name, code)
        assert "given more than once" in err.getvalue(), (name, err.getvalue())
        if hint:
            assert hint in err.getvalue(), (name, err.getvalue())

    # A value default (--cwd '.') must survive first use: the guard tracks
    # occurrences per destination, never truthiness of the value itself.
    assert build_parser().parse_args(["review", "--cwd", "d"]).cwd == "d"
    assert build_parser().parse_args(["review"]).cwd == "."

    # Booleans are idempotent - double --json is the documented ordering.
    args_bool = build_parser().parse_args(
        ["--json", "review", "--staged", "--staged", "--json"])
    assert args_bool.json is True and args_bool.staged is True, (
        args_bool.json, args_bool.staged)

    # Single uses are untouched and compose.
    args2 = build_parser().parse_args(
        ["review", "--file", "x.py", "--model", "m", "--timeout", "10"])
    assert (args2.model, args2.file, args2.timeout) == ("m", ["x.py"], 10), (
        args2.model, args2.file, args2.timeout)
    return "--file unions; 9 scalar flags exit 2 on repeat; booleans idempotent"


def t_default_diff_includes_untracked():
    """The default review must see never-added files, and say so.

    Regression guard: `git diff HEAD` never shows untracked files, so a
    brand-new file was invisible to the default review - and when it was the
    only change, the tool failed with "the diff is empty" while the user was
    looking at real changes.
    """
    cfg, _ = config.load_config()
    with _repo(files={"tracked.py": "x = 1\n"}) as tmp:
        _git(["add", "tracked.py"], tmp)
        _git(["commit", "-m", "init"], tmp)
        _write(os.path.join(tmp, "brand_new.py"), "import os\n")
        _write(os.path.join(tmp, "NOTES.md"), "# prose\n")
        _write(os.path.join(tmp, "tracked.py"), "x = 2\n")
        _git(["add", "tracked.py"], tmp)
        inp = collect.from_git(cfg, cwd=tmp)
        labels = [c.label for c in inp.chunks]
        assert "brand_new.py" in labels, "untracked file missing: %s" % labels
        assert "NOTES.md" not in labels, "prose should still be filtered"
        assert any("untracked" in w for w in inp.warnings), inp.warnings
        # --staged keeps its narrow, explicit scope: no untracked files
        staged = collect.from_git(cfg, staged=True, cwd=tmp)
        assert [c.label for c in staged.chunks] == ["tracked.py"], (
            "staged scope leaked: %s" % [c.label for c in staged.chunks])
    # untracked-only work is reviewed, not rejected as an empty diff
    with _repo() as tmp2:
        _write(os.path.join(tmp2, "only_new.py"), "y = 2\n")
        only = collect.from_git(cfg, cwd=tmp2)
        assert [c.label for c in only.chunks] == ["only_new.py"], (
            "untracked-only work must be reviewed, got %s"
            % [c.label for c in only.chunks])
        assert "untracked" in only.kind, only.kind
    return "untracked reviewed once, staged stays scoped"


def t_untracked_scope_rules():
    """--ref compares two commits; untracked files belong to neither.

    Pins the scope decision: the default uncommitted-diff review includes
    untracked files, --staged cannot (they are unstaged by definition), and
    --ref must not silently widen "what did this branch change" into "what
    does the tree look like". Also guards the no-commits repo, where
    untracked files are the only thing there is to review.
    """
    cfg, _ = config.load_config()
    with _repo(files={"base.py": "x = 1\n", "untracked_new.py": "y = 2\n"}) as tmp:
        _git(["add", "base.py"], tmp)
        _git(["commit", "-m", "one"], tmp)
        _write(os.path.join(tmp, "base.py"), "x = 2\n")
        _git(["commit", "-am", "two"], tmp)
        scoped = collect.from_git(cfg, ref="HEAD~1", cwd=tmp)
        assert [c.label for c in scoped.chunks] == ["base.py"], (
            "--ref scope leaked untracked files: %s" % [c.label for c in scoped.chunks])
        assert "untracked" not in scoped.kind, scoped.kind
    with _repo(commits=0) as tmp2:
        _write(os.path.join(tmp2, "first.py"), "import os\n")
        inp = collect.from_git(cfg, cwd=tmp2)
        assert [c.label for c in inp.chunks] == ["first.py"], [c.label for c in inp.chunks]
        assert "untracked" in inp.kind, inp.kind
    return "ref stays commit-scoped; empty repo still reviews untracked"


def t_untracked_honest_pipeline():
    """Untracked files ride the same filter/cap pipeline as tracked ones.

    Pins three invariants: prose skipping runs before the max_files cap, so a
    directory of untracked Markdown cannot eat file slots ahead of code; the
    inclusion warning counts what was folded into the diff, while binary or
    unreadable files (never folded in) are named in their own warning; and
    prose files folded in but filtered downstream are itemized in skipped.
    """
    cfg, _ = config.load_config()
    with _repo(files={"blob.bin": bytes(range(256)), "zz_code.py": "x = 1\n"}) as tmp:
        for i in range(3):
            _write(os.path.join(tmp, "aaa_notes_%d.md" % i), "# prose %d\n" % i)
        inp = collect.from_git(dict(cfg, max_files=2), cwd=tmp)
        assert [c.label for c in inp.chunks] == ["zz_code.py"], (
            "code starved by untracked prose: %s" % [c.label for c in inp.chunks])
        assert len([s for s in inp.skipped if "prose" in s["reason"]]) == 3, inp.skipped
        joined = chr(10).join(inp.warnings)
        assert "Included 4 untracked" in joined, inp.warnings
        assert "blob.bin skipped (binary content)" in joined, inp.warnings
    return "untracked files share the tracked pipeline; warnings stay honest"


def t_empty_diff_names_skips():
    """Nothing-to-review errors must say what was skipped, in the body.

    On an all-binary-or-unreadable untracked repo the old error said only "the
    diff is empty" and buried the explanation in the remedy clause. The body
    itself now names each skipped file and its reason.
    """
    cfg, _ = config.load_config()
    with _repo(files={"blob.bin": bytes(range(256))}) as tmp:
        try:
            collect.from_git(cfg, cwd=tmp)
            raise AssertionError("expected InputError for binary-only untracked repo")
        except collect.InputError as e:
            assert "blob.bin" in e.detail and "binary content" in e.detail, e.detail
            assert "git add -N" not in e.remedy, e.remedy
    with _repo(commits=0, files={"img.png": bytes(range(256))}) as tmp2:
        _git(["add", "img.png"], tmp2)
        try:
            collect.from_git(cfg, staged=True, cwd=tmp2)
            raise AssertionError("expected InputError for staged binary-only diff")
        except collect.InputError as e:
            assert "img.png" in e.detail and "binary" in e.detail, e.detail
    return "empty-diff bodies name skipped files and reasons"


def t_tier2_budget_and_tier1_rescue():
    """Tier 2 inherits the remaining budget; tier-1 text survives a tier-2 crash.

    Regression guard: tier 2 was handed the budget as of chunk start, so heavy
    tier-1 retries could overrun the shared deadline roughly twofold; and if
    tier 2's transport failed, the tier-1 text tier 3 exists to preserve was
    discarded with the exception. Driven over a fake Ollama HTTP server so the
    real client timeouts enforce the deadline end to end.
    """
    import fake_ollama

    cfg, _ = config.load_config()
    cfg = dict(cfg)
    cfg["max_retries"] = 1
    chunk = collect.Chunk("t.py", "x = 1")
    opts = review.ReviewOptions()
    prose = "the model wrote prose, not JSON"

    # Scenario 1: tier 1 burns 1.2s of a 2.5s budget on the wire and answers
    # prose; tier 2 must be handed only the remaining ~1.3s (the client
    # refuses attempts under its 1s floor), hit the deadline mid-flight, and
    # the chunk must end near 1.3s - not near 3.7s, the fresh-window cost.
    srv = fake_ollama.start({"m": [
        {"name": "tier1-prose", "body": prose, "delay": 1.2},
        {"name": "tier2-slow", "body": prose, "delay": 5.0},
    ]})
    try:
        cfg["base_url"] = srv.base_url
        t0 = time.time()
        got, mode, meta = review.review_chunk(
            cfg, "m", chunk, "files", prompts.DEFAULT_FOCUS, opts, 2.5)
        wall = time.time() - t0
        # A tier-2 timeout is rescued (tier-1 text survives), so the ladder
        # returns raw with a note naming the kind rather than raising.
        assert mode == "raw", mode
        assert "second pass failed (timeout)" in meta["degraded"], meta
        assert prose in got[0]["suggested_fix"], got[0]
        assert wall < 2.6, (
            "chunk ran %.1fs; tier 2 must inherit the remaining budget, "
            "not a fresh window" % wall)
        shapes = [(r["schema"], r["behavior"]) for r in srv.log]
        assert shapes == [(True, "tier1-prose"), (False, "tier2-slow")], shapes
    finally:
        srv.close()

    # Scenario 2: tier 2's transport dies (HTTP 500); the tier-1 text, which
    # never parsed, must surface as the raw finding instead of vanishing.
    srv = fake_ollama.start({"m": [
        {"name": "tier1-prose", "body": "tier-one text that never parsed"},
        {"name": "tier2-http500", "status": 500},
    ]})
    try:
        cfg["base_url"] = srv.base_url
        got, mode, meta = review.review_chunk(
            cfg, "m", chunk, "files", prompts.DEFAULT_FOCUS, opts, 30.0)
        assert mode == "raw", mode
        assert "tier-one text" in got[0]["suggested_fix"], got[0]
        assert "second pass failed" in meta["degraded"], meta
        assert "http_5xx" in meta["degraded"], meta
    finally:
        srv.close()
    return "budget inherited, tier-1 text rescued (over the wire)"


def t_degradation_notes_compress():
    """Identical degradation notes fold into one line naming the chunks.

    Regression guard: run_review appended one note per chunk x model, so a
    model fighting the transport repeated the same sentence once per chunk
    and the report drowned its findings in copies of it. Compression keyed
    on (model, message) keeps every chunk and model named; a singleton
    keeps the exact per-chunk wording. Compressed degradations travel in
    result["degradations"] and reach the report's labeled section.

    Wire setup mirrors t_tier2_budget_and_tier1_rescue scenario 2: tier 1
    answers prose (never parses), tier 2 dies with HTTP 500 - the chunk's
    rescue meta carries the identical transport-fight message.
    """
    import fake_ollama

    prose = "the model wrote prose, not JSON"
    diff = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1 +1 @@
-x
+y
diff --git a/b.py b/b.py
--- a/b.py
+++ b/b.py
@@ -1 +1 @@
-x
+y
diff --git a/c.py b/c.py
--- a/c.py
+++ b/c.py
@@ -1 +1 @@
-x
+y
diff --git a/d.py b/d.py
--- a/d.py
+++ b/d.py
@@ -1 +1 @@
-x
+y
"""
    srv = fake_ollama.start({"m": [
        {"name": "a-t1", "body": prose},
        {"name": "a-t2", "status": 500},
        {"name": "b-t1", "body": prose},
        {"name": "b-t2", "status": 500},
        {"name": "c-t1", "body": prose},
        {"name": "c-t2", "status": 500},
        # d.py degrades differently: both tiers answer prose, so tier 3
        # surfaces it - a distinct message, never grouped with the fights.
        {"name": "d-t1", "body": prose},
        {"name": "d-t2", "body": prose},
    ]})
    try:
        cfg, _ = config.load_config()
        cfg = dict(cfg, base_url=srv.base_url, max_retries=1)
        inp = collect.from_text(cfg, diff, "ignored", "stdin")
        labels = [c.label for c in inp.chunks]
        assert labels == ["a.py", "b.py", "c.py", "d.py"], labels
        notes = []
        result = review.run_pipeline(
            cfg, ["m"], inp, prompts.DEFAULT_FOCUS,
            review.ReviewOptions(), notes)
        assert result["status"] == "partial", result["status"]
        # The engine's degraded entries: compressed, chunk_errors excluded.
        degs = result["degradations"]
        assert len(degs) == 2, degs
        # The identical transport fights fold into one line naming every
        # affected chunk and the model; no per-chunk copy may survive.
        fights = [n for n in degs if "second pass failed" in n]
        assert len(fights) == 1, degs
        assert "http_5xx" in fights[0] and "(m)" in fights[0], degs
        assert "on 3 chunk(s)" in fights[0], degs
        for label in labels[:-1]:
            assert label in fights[0], (label, fights)
        assert "d.py" not in fights[0], degs
        # The distinct tier-3 degradation keeps the singleton's exact
        # per-chunk wording, model and all.
        clean = [n for n in degs if "output was not parseable" in n]
        assert clean == ["d.py (m): output was not parseable as JSON; "
                         "surfaced as raw text"], degs
        # Back-compat: notes still carries the degradation lines for JSON
        # consumers, after the front-end notes.
        assert result["notes"][-2:] == degs, (result["notes"], degs)
        md = render.to_markdown(result)
        assert "## Degradations" in md, md[:400]
        section = md.split("## Degradations")[1]
        assert fights[0] in section and clean[0] in section, section
        # Every tier-2 failure was rescued here, so chunk_errors is empty
        # and no "chunk ... failed" line may appear in the section. (The
        # fight line itself says "second pass failed", so grep the shape.)
        assert "chunk `" not in section, section
        # All notes in this run are degradation echoes; the dedup must
        # leave no duplicated Note bullets behind.
        assert "- Note:" not in md, md[:600]
    finally:
        srv.close()
    return "identical rescue notes fold into one line; singleton keeps its wording"

# --------------------------------------------------------------------------
# live inference
# --------------------------------------------------------------------------

def t_live_review():
    """A live review must actually surface the planted defects.

    The run's own JSON is parsed so the check can fail: a non-ok status, zero
    findings, no finding referencing the planted file, or an unrenderable
    report is a failure - not something a human notices by eyeballing output.
    """
    with _tmpdir() as tmp:
        p = os.path.join(tmp, "planted.py")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(PLANTED_DEFECTS)
        code, out = _capture_cli(["review", "--file", p, "--json", "--timeout", "240"])
        assert code == 0, "live review exited %s" % code
        rep = json.loads(out)
        assert rep.get("status") == "ok", "review status: %r" % rep.get("status")
        findings = rep.get("findings") or []
        assert findings, "live review reported zero findings for planted defects"
        assert any("planted.py" in (f.get("location") or "") for f in findings), (
            "no finding references the planted file: %s"
            % [f.get("location") for f in findings])
        md = render.to_markdown(rep)
        assert md.strip(), "live review produced an empty markdown report"
        return "planted defects surfaced (%d findings); report renders" % len(findings)




# Registered mutations for `--mutate`: each asserts that its checks have
# teeth by breaking exactly one behavior and requiring the named checks to
# fail. old must match the file byte for byte; new is the broken form; the
# restore is the reverse substitution, in a finally, so a failed assertion
# mid-mutation can never leave the tree dirty. After each restore the
# module's __pycache__ is cleared: a same-size mutation-restore cycle can
# land within one mtime second, and CPython's mtime+size pyc validation
# would then serve the mutated bytecode as if the source were restored.
MUTATIONS = [
    {
        "name": "repeated-flag guard removed",
        "file": "cli.py",
        "old": '''        if getattr(namespace, marker, False):
            prev = getattr(namespace, self.dest, None)
            hint = self.hint if getattr(self, "hint", None) else ""
            parser.error(
                "%s given more than once: %r then %r. Pass it once;%s"
                % (option_string, prev, values, hint)
            )
''',
        "new": "",
        "checks": ["cli: repeated flags never silent"],
    },
    {
        "name": "budget line removed from the header",
        "file": "render.py",
        "old": '    if budget_s:\n        spent += " of %ds budget" % budget_s\n',
        "new": "",
        "checks": ["render: budget shown in markdown"],
    },
    {
        "name": "degradation compression skipped",
        "file": "review.py",
        "old": "    degraded = render.compress_degraded(degraded)\n",
        "new": "",
        "checks": ["review: degradation residue compresses"],
    },
    {
        "name": "MCP argument type enforcement removed",
        "file": "mcp_server.py",
        "old": "        _check_types(name, args)\n",
        "new": "",
        "checks": ["mcp: tool arguments are typed"],
    },
    {
        "name": "MCP duplicate-key parse guard removed",
        "file": "mcp_server.py",
        "old": "json.loads(line, object_pairs_hook=_no_duplicate_keys)",
        "new": "json.loads(line)",
        "checks": ["mcp: tool arguments are typed"],
    },
    {
        "name": "MCP error shape broken (bare string from _err)",
        "file": "mcp_server.py",
        "old": 'def _err(text):\n    return {"content": [{"type": "text", "text": text}], "isError": True}',
        "new": "def _err(text):\n    return text",
        "checks": ["mcp: error paths keep the result shape"],
    },
    {
        "name": "MCP catch-all removed",
        "file": "mcp_server.py",
        "old": '''    except Exception as e:  # never take the client down with us
        sys.stderr.write(traceback.format_exc())
        return _err("Internal error in the review server: %r" % (e,))
''',
        "new": "",
        "checks": ["mcp: error paths keep the result shape"],
    },
    {
        "name": "corroboration tag wording mangled",
        "file": "render.py",
        "old": '"agreed by %s" % ", ".join(f["raised_by"])',
        "new": '"x-agreed %s" % ", ".join(f["raised_by"])',
        "checks": ["render: corroborated first in markdown",
                   "mcp: stdio end to end"],
    },
    {
        "name": "lone-finding tag wording mangled",
        "file": "render.py",
        "old": 'else "only %s" % f["raised_by"][0]',
        "new": 'else "lone %s" % f["raised_by"][0]',
        "checks": ["render: corroborated first in markdown",
                   "mcp: stdio end to end"],
    },
    {
        "name": "degradations heading renamed",
        "file": "render.py",
        "old": '"## Degradations"',
        "new": '"## Survival notes"',
        "checks": ["render: degradations section", "mcp: stdio end to end"],
    },
    {
        "name": "MCP dispatcher drops degradations",
        "file": "mcp_server.py",
        "old": '''    result = review.run_pipeline(
        cfg, list(args.get("models") or []), inp, focus, opts, notes)
''',
        "new": '''    result = review.run_pipeline(
        cfg, list(args.get("models") or []), inp, focus, opts, notes)
    result = dict(result); result.pop("degradations", None)
''',
        "checks": ["mcp: stdio end to end"],
    },
    {
        "name": "conflicting-sources guard disabled",
        "file": "collect.py",
        "old": "    if ref is not None and staged:\n",
        "new": "    if False and ref is not None and staged:\n",
        "checks": ["collect+cli: conflicting sources fail loudly"],
    },
    {
        "name": "pinned --timeout scaled anyway",
        "file": "review.py",
        "old": "    if not timeout_pinned:\n",
        "new": "    if True:\n",
        "checks": ["explicit --timeout is honoured"],
    },
    {
        "name": "per-chunk truncation cap removed",
        "file": "collect.py",
        "old": "    head = text[:limit]\n",
        "new": "    head = text\n",
        "checks": ["oversized input truncates"],
    },
    {
        "name": "focus rejection wording changed",
        "file": "prompts.py",
        "old": '        return None, "Unknown focus area(s): %s. Valid: %s" % (\n',
        "new": '        return None, "Unknown focus zones: %s. Valid: %s" % (\n',
        "checks": ["orchestration + focus decoupled",
                   "mcp: tool arguments are typed"],
    },
    {
        "name": "watcher lock acquire removed",
        "file": "dashboard.py",
        "old": "        ok, holder = _claim(PID_PATH)\n",
        "new": "        pass  # lock acquire removed by mutation\n",
        "checks": ["dashboard: watcher pidfile lock"],
    },
    {
        "name": "watcher lock refuse branch disabled",
        "file": "dashboard.py",
        "old": "        if holder and pid_alive(holder):\n            return False, holder\n",
        "new": "        if False and holder and pid_alive(holder):\n            return False, holder\n",
        "checks": ["dashboard: watcher pidfile lock"],
    },
    {
        "name": "dashboard_status dispatch removed",
        "file": "mcp_server.py",
        "old": "        if name == \"dashboard_status\":\n            import dashboard  # local: only this tool touches the dashboard\n            return _ok(dashboard.watcher_status_text())\n",
        "new": "",
        "checks": ["mcp: dashboard_status tool"],
    },
    {
        "name": "concurrent-run wait removed",
        "file": "selftest.py",
        "old": "_wait_for_quiescence()\n\nimport cli  # noqa: E402",
        "new": "import cli  # noqa: E402",
        "checks": ["selftest: coordinates concurrent runs"],
    },
    {
        "name": "marker holder liveness check removed",
        "file": "selftest.py",
        "old": "    if holder.isdigit() and not pidutil.pid_alive(int(holder)):\n",
        "new": "    if False and holder.isdigit() and not pidutil.pid_alive(int(holder)):\n",
        "checks": ["selftest: coordinates concurrent runs"],
    },
    {
        "name": "mutation journal heal disabled",
        "file": "selftest.py",
        "old": "    with open(path, \"wb\") as fh:\n        fh.write(original)\n",
        "new": "    if True:  # journal heal disabled by mutation\n        pass\n",
        "checks": ["selftest: coordinates concurrent runs"],
    },
    {
        "name": "watcher stop wake removed",
        "file": "dashboard.py",
        "old": "            if os.path.exists(_sentinel_path(kind)):\n                return kind\n",
        "new": "            if os.path.exists(_sentinel_path(kind)):\n                pass\n",
        "checks": ["dashboard: watcher stops cleanly",
                   "dashboard: --pause freezes a final page"],
    },
    {
        "name": "watcher pause final render removed",
        "file": "dashboard.py",
        "old": "            _cycle(n + 1, \"pause requested\", args)  # freeze the artifact fresh\n            return\n",
        "new": "            return\n",
        "checks": ["dashboard: --pause freezes a final page"],
    },
    {
        "name": "shutdown request status removed",
        "file": "dashboard.py",
        "old": ("    return \"%s\\nlock: %s\\n%s\\n%s\\nmarker file: %s\" % (\n"
                "        first, st[\"pidfile\"], _pending_line(st[\"pending\"]),\n"
                "        _marker_line(ms), ms[\"path\"])"),
        "new": ("    return \"%s\\nlock: %s\\n%s\\nmarker file: %s\" % (\n"
                "        first, st[\"pidfile\"],\n"
                "        _marker_line(ms), ms[\"path\"])"),
        "checks": ["mcp: dashboard_status tool"],
    },
]


def _run_mutations():
    """Prove every registered guard has teeth.

    For each mutation: byte-exact restore in a finally, then a fresh
    subprocess that asserts every named check fails under the mutation and
    that the source is back. A passing baseline first, so a failure means a
    loss of coverage, not a pre-existing break. All mutations run offline.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    argv = [sys.executable, os.path.abspath(__file__), "--offline"]
    # Hold the marker for the whole run: unrelated concurrent runs wait or
    # skip honestly instead of importing mutated sources. Our own children
    # carry _MUTATOR_ENV and stay strict - mutation verification requires it.
    with open(_MUTATION_MARKER, "w") as fh:
        fh.write(str(os.getpid()))
    atexit.register(_clear_mutation_marker)  # normal exit, sys.exit, crash
    mutenv = dict(os.environ, **{_MUTATOR_ENV: str(os.getpid())})
    base = subprocess.run(argv, capture_output=True, text=True, timeout=900,
                          cwd=here, env=mutenv)
    assert base.returncode == 0, "baseline suite must pass before mutating:\n%s" % (
        base.stdout[-1500:],)
    print("baseline: offline suite green\n")

    bad = []
    for mut in MUTATIONS:
        path = os.path.join(here, mut["file"])
        with open(path, "r", encoding="utf-8", newline="") as fh:
            src = fh.read()
        if mut["old"] not in src:
            bad.append((mut["name"], "anchor no longer matches - update it"))
            continue
        with open(path, "rb") as fh:  # journaled before the rewrite, always
            original_bytes = fh.read()
        _write_mutation_journal(mut["name"], mut["file"], original_bytes)
        try:
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(src.replace(mut["old"], mut["new"], 1))
            child_args = list(argv)
            for c in mut["checks"]:
                child_args += ["--mutate-check", c]
            run = subprocess.run(
                child_args,
                capture_output=True, text=True, timeout=900, cwd=here, env=mutenv)
            # The runner prints "<name padded> STATUS <detail>"; recover
            # exact check names (they contain spaces and colons, so split()
            # is not an option - slice at the status word instead).
            statuses = {}
            for ln in run.stdout.splitlines():
                m = re.search(r"\s(PASS|FAIL|SKIP)\s", ln)
                if m:
                    statuses[ln[:m.start()].strip()] = m.group(1)
            got_bad = []
            for want in mut["checks"]:
                status = statuses.get(want, "not reported")
                if status != "FAIL":
                    got_bad.append("%s -> %s" % (want, status))
            if got_bad:
                bad.append((mut["name"], "; ".join(got_bad) + "\n  child tail: %r"
                            % run.stdout[-300:]))
        finally:
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(src)
            _clear_pycache()
            _clear_mutation_journal()

        print("mutate: %-44s caught by %s" % (
            mut["name"], ", ".join(c.split(":")[0] for c in mut["checks"])))

    _clear_pycache()
    print("\n" + "-" * 78)
    if bad:
        for name, why in bad:
            print("MUTATION %s: %s" % (name, why))
        print("%d of %d mutations NOT caught" % (len(bad), len(MUTATIONS)))
        return 1
    print("all %d mutations caught - every guard has teeth" % len(MUTATIONS))
    return 0


def t_selftest_coordinates_concurrent_runs():
    """The wait-for-quiescence gate must actually gate.

    Spawns a filtered child suite against a privately held mutation marker:
    the path is env-seamed, so the ambient marker - held for the whole run
    when this check itself runs under --mutate - cannot deadlock the child,
    and a concurrent real mutate cannot make the child import mutated
    sources. The child must block before importing, say so on stderr, and
    then pass. Removal of the _wait_for_quiescence call fails this. Holder
    liveness is pinned too: a marker whose holder pid died clears at once,
    while unparseable content stays age-governed.
    """
    import threading
    here = os.path.dirname(os.path.abspath(__file__))
    with _tmpdir() as tmp:
        marker = os.path.join(tmp, "mutating")
        release = threading.Event()

        def hold():
            with open(marker, "w") as fh:
                fh.write("held")
            release.wait(6)  # hold ~6s, then release
            try:
                os.remove(marker)
            except OSError:
                pass

        threading.Thread(target=hold, daemon=True).start()
        for _ in range(20):  # spawn only once the marker provably exists
            if os.path.exists(marker):
                break
            time.sleep(0.1)
        env = {k: v for k, v in os.environ.items() if k != _MUTATOR_ENV}
        env["_SELFTEST_MUTATION_MARKER"] = marker
        t0 = time.time()
        child = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "--offline",
             "--check", "config loads"],
            capture_output=True, text=True, timeout=300, cwd=here, env=env)
        elapsed = time.time() - t0
        release.set()
        assert child.returncode == 0, (child.returncode, child.stderr[-300:])
        assert "waiting for it" in child.stderr, (
            "child did not wait out the mutation marker: %r" % child.stderr[-200:])
        assert elapsed >= 5, "child too fast to have waited: %.1fs" % elapsed
        assert "1 passed" in child.stdout, child.stdout[-200:]

        # Holder liveness, pinned in-process against a private marker (never
        # the ambient one): a dead holder's marker clears at once - the same
        # recovery the dashboard pidfile lock applies - instead of limboing
        # for the 950s age backstop. Unparseable content ("held", what the
        # spawned child above waits on) stays age-governed.
        global _MUTATION_MARKER
        saved_marker = _MUTATION_MARKER
        saved_env = os.environ.get(_MUTATOR_ENV)
        os.environ.pop(_MUTATOR_ENV, None)  # pins must walk the unsanctioned path
        try:
            _MUTATION_MARKER = os.path.join(tmp, "ambient")
            with open(_MUTATION_MARKER, "w") as fh:  # a killed session's leftover
                fh.write(str(1 << 30))
            assert not _mutation_in_flight(), "dead holder's marker must not gate"
            assert not os.path.exists(_MUTATION_MARKER), (
                "dead holder's marker must be cleared, not left to age out")
            with open(_MUTATION_MARKER, "w") as fh:  # live holder: this process
                fh.write(str(os.getpid()))
            assert _mutation_in_flight(), "live holder's marker must gate"
            assert os.path.exists(_MUTATION_MARKER), (
                "live holder's marker must survive the check")
            os.remove(_MUTATION_MARKER)
            with open(_MUTATION_MARKER, "w") as fh:  # unparseable content
                fh.write("garbage")
            assert _mutation_in_flight() and os.path.exists(_MUTATION_MARKER), (
                "unparseable content must stay age-governed")
            os.remove(_MUTATION_MARKER)

            # Journal heal: a --mutate run journals a module's original bytes
            # before each rewrite; a journal that outlives its run restores
            # them on the next suite's contact. The journal follows the
            # marker's seam, so these stay on private paths.
            journal = _MUTATION_MARKER + ".journal"
            target = os.path.join(tmp, "muttarget.py")
            payload = b"original = 1\n"
            entry = {"name": "mut X",
                     "file": os.path.relpath(target, here),
                     "content_b64": base64.b64encode(payload).decode("ascii")}
            with open(target, "wb") as fh:  # as a killed run leaves it
                fh.write(b"mutated = 2\n")
            with open(journal, "w", encoding="utf-8") as fh:
                json.dump(entry, fh)
            _heal_mutation_journal()
            with open(target, "rb") as fh:
                assert fh.read() == payload, "journal must restore the original"
            assert not os.path.exists(journal), "healed journal must be cleared"

            with open(journal, "w", encoding="utf-8") as fh:  # already restored
                json.dump(entry, fh)
            _heal_mutation_journal()
            assert not os.path.exists(journal), "stale journal must be dropped"

            with open(journal, "w", encoding="utf-8") as fh:  # a sanctioned child
                json.dump(entry, fh)
            os.environ[_MUTATOR_ENV] = saved_env or "1"
            _heal_mutation_journal()
            os.environ.pop(_MUTATOR_ENV, None)
            assert os.path.exists(journal), "the mutator's own child must never heal"

            src = open(os.path.join(here, "selftest.py"), encoding="utf-8").read()
            assert "\n    _heal_mutation_journal()  # refuses" in src, (
                "the quiescence gate must heal a dead run's journal")
            os.remove(journal)
        finally:
            _MUTATION_MARKER = saved_marker
            if saved_env is not None:
                os.environ[_MUTATOR_ENV] = saved_env
    return ("child suite waited out a held marker (%.0fs), then ran clean; "
            "dead holder clears at once, garbage stays age-governed") % elapsed


def t_doc_counts_match_roster():
    """README/CONTRIBUTING claim the suite's size; drift must fail the build.

    Every new check used to require a human to remember four hardcoded
    counts across three docs - and one release shipped with them stale.
    The roster (_build_checks) is the single source of truth; this check
    derives the true totals from it and pins the docs to match, so doc
    drift fails CI instead of waiting for a reader to notice.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    total = len(_build_checks(True))
    offline = sum(1 for name, _ in _build_checks(True) if name not in NEEDS_SERVER)
    readme = open(os.path.join(here, os.pardir, "README.md"),
                  encoding="utf-8").read()
    contributing = open(os.path.join(here, os.pardir, "CONTRIBUTING.md"),
                        encoding="utf-8").read()
    rm = re.search(r"(\d+) checks should pass \((\d+) without a running", readme)
    assert rm, "README lost its check-count sentence"
    assert int(rm.group(1)) == total, (
        "README claims %s checks, roster has %d" % (rm.group(1), total))
    assert int(rm.group(2)) == offline, (
        "README claims %s offline, roster has %d" % (rm.group(2), offline))
    cm = re.search(r"all (\d+) checks \(needs Ollama running\)", contributing)
    assert cm and int(cm.group(1)) == total, (
        "CONTRIBUTING claims %s checks, roster has %d" % (
            cm.group(1) if cm else "?", total))
    cm2 = re.search(r"--offline\s+# (\d+) checks, no server needed", contributing)
    assert cm2 and int(cm2.group(1)) == offline, (
        "CONTRIBUTING claims %s offline, roster has %d" % (
            cm2.group(1) if cm2 else "?", offline))
    dash = open(os.path.join(here, "dashpage.py"), encoding="utf-8").read()
    dm = re.search(r"<b>(\d+)-mutation registry</b>", dash)
    assert dm and int(dm.group(1)) == len(MUTATIONS), (
        "dashpage.py claims %s mutations, registry has %d" % (
            dm.group(1) if dm else "?", len(MUTATIONS)))
    return "docs claim %d live / %d offline; dashboard pins %d mutations" % (
        total, offline, len(MUTATIONS))


def _build_checks(live):
    """The check roster, one place: doc-count assertions derive from it."""
    checks = [
        ("config loads", t_config),
        ("server reachable", t_server_reachable),
        ("default model resolves", t_model_resolves),
        ("bare family name resolves", t_bare_family_name_resolves),
        ("error: server unreachable", t_err_unreachable),
        ("error: model not installed", t_err_model_missing),
        ("error: cloud model blocked", t_err_cloud_blocked),
        ("error: missing file", t_err_missing_file),
        ("error: binary file", t_err_binary_file),
        ("error: empty file", t_err_empty_file),
        ("error: not a git repo", t_err_not_a_repo),
        ("oversized input truncates", t_truncation),
        ("parse: strict JSON", t_parse_strict),
        ("parse: fenced JSON", t_parse_fenced),
        ("parse: salvaged + normalised", t_parse_salvaged),
        ("parse: garbage returns None", t_parse_garbage),
        ("context covers max input", t_context_covers_max_input),
        ("context scales down", t_context_scales_down),
        ("consensus: merges agreement", t_consensus_merges_agreement),
        ("consensus: keeps singles", t_consensus_keeps_singles),
        ("consensus: file/category bounds", t_consensus_respects_file_and_category),
        ("consensus: corroborated first", t_consensus_sorts_corroborated_first),
        ("consensus: severity spread", t_consensus_severity_spread),
        ("consensus: messy locations", t_consensus_parses_messy_locations),
        ("modules stay focused", t_modules_stay_focused),
        ("orchestration + focus decoupled", t_review_options_decoupled),
        ("client: retries respect budget", t_retries_respect_total_budget),
        ("collect: stdin diff filtered", t_stdin_diff_honours_code_filter),
        ("collect: code filter drops prose", t_code_filter_drops_prose),
        ("collect: filter yields if all prose", t_code_filter_yields_when_all_prose),
        ("collect: --all-files keeps prose", t_code_filter_off_keeps_everything),
        ("collect: from_text variants", t_from_text_variants),
        ("mcp: broken pipe exits clean", t_serve_survives_closed_stdout),
        ("mcp: handshake + tools", t_mcp_handshake),
        ("mcp: protocol errors", t_mcp_protocol_errors),
        ("mcp: tool schemas", t_mcp_tool_schemas),
        ("mcp: unknown tool", t_mcp_unknown_tool_is_error),
        ("mcp: all tools dispatched on fake ollama", t_mcp_dispatch_all_tools_on_fake),
        ("mcp: tool arguments are typed", t_mcp_arguments_are_typed),
        ("mcp: error paths keep the result shape", t_mcp_error_paths_keep_shape),
        ("mcp: stdio end to end", t_mcp_stdio_end_to_end),
        ("render never crashes", t_render_never_crashes),
        ("prompts well-formed", t_prompt_shape),
        ("render: corroborated first in markdown", t_render_sorts_corroborated_first),
        ("collect: untracked files reviewed", t_default_diff_includes_untracked),
        ("collect: untracked scope rules pinned", t_untracked_scope_rules),
        ("collect: untracked pipeline honest", t_untracked_honest_pipeline),
        ("collect: empty-diff bodies name skips", t_empty_diff_names_skips),
        ("review: tier 2 budget + tier 1 rescue", t_tier2_budget_and_tier1_rescue),
        ("review: degradation residue compresses", t_degradation_notes_compress),
        ("review: all chunks failed is an error", t_all_chunks_failed_is_an_error),
        ("review: partial run keeps surviving findings", t_partial_run_keeps_surviving_findings),
        ("review: fatal drop leaves others running", t_fatal_drop_does_not_kill_the_run),
        ("explicit --timeout is honoured", t_pinned_timeout_is_not_scaled),
        ("engine: caller state untouched by run_pipeline", t_engine_leaves_caller_state_untouched),
        ("status snapshot shared by cli and mcp", t_status_snapshot_is_shared),
        ("timeout scaling shared by cli and mcp", t_timeout_scaling_is_shared),
        ("collect+cli: conflicting sources fail loudly", t_conflicting_source_flags_fail),
        ("review: no double-prefixed locations", t_no_double_prefixed_locations),
        ("status: unresolved renders, not None", t_status_renders_unresolved),
        ("render: budget shown in markdown", t_markdown_shows_budget),
        ("render: degradations section", t_markdown_degradations_section),
        ("cli: repeated flags never silent", t_repeated_flags_neither_silent),
        ("dashboard: watcher pidfile lock", t_dashboard_lock_is_exclusive),
        ("dashboard: watcher wakes on source edit", t_dashboard_watch_reacts_to_disk),
        ("dashboard: degraded cycles explain themselves", t_dashboard_degraded_cycles_explain_themselves),
        ("dashboard: watcher stops cleanly", t_dashboard_watcher_stops_cleanly),
        ("dashboard: --pause freezes a final page", t_dashboard_pause_freezes_a_final_page),
        ("mcp: dashboard_status tool", t_mcp_dashboard_status_tool),
        ("doc counts match the roster", t_doc_counts_match_roster),
        ("selftest: coordinates concurrent runs", t_selftest_coordinates_concurrent_runs),
    ]
    if live:
        checks.append(("live review of planted defects", t_live_review))
    checks.append(("fixtures leave no temp residue", t_tempdir_leaves_no_residue))
    return checks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="also run real inference")
    ap.add_argument(
        "--offline",
        action="store_true",
        help="skip checks needing a live Ollama server (for CI)",
    )
    ap.add_argument(
        "--check", action="append", metavar="SUBSTR",
        help="run only checks whose name contains this substring; may repeat. "
        "Fails loudly when nothing matches, so CI cannot green-pass a typo",
    )
    ap.add_argument(
        "--mutate", action="store_true",
        help="apply registered mutations and require their checks to fail "
        "(meta-verification that the guards have teeth; not part of the suite)",
    )
    ap.add_argument(
        "--mutate-check", action="append", metavar="SUBSTR",
        help=argparse.SUPPRESS)  # internal: used by _run_mutations' subprocess
    args = ap.parse_args()
    if args.mutate:
        return _run_mutations()

    checks = _build_checks(args.live)

    if args.check or args.mutate_check:
        needles = (args.check or []) + (args.mutate_check or [])
        checks = [c for c in checks if any(s in c[0] for s in needles)]
        if not checks:
            print("no check matches: %s" % ", ".join(needles))
            return 2

    for name, fn in checks:
        if args.offline and name in NEEDS_SERVER:
            skip(name, "offline mode: needs a live Ollama server")
        elif _mutation_in_flight() and name in MUTATION_SENSITIVE:
            skip(name, "concurrent --mutate in flight: source reads would lie")
        else:
            check(name, fn)

    print("\n%-34s %s" % ("CHECK", "RESULT"))
    print("-" * 78)
    failed = passed = skipped = 0
    for status, name, detail in RESULTS:
        if status == "FAIL":
            failed += 1
        elif status == "SKIP":
            skipped += 1
        else:
            passed += 1
        print("%-34s %-4s %s" % (name, status, detail))
    print("-" * 78)
    summary = "%d passed, %d failed" % (passed, failed)
    if skipped:
        summary += ", %d skipped" % skipped
    print(summary)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
