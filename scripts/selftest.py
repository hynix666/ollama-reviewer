"""Setup verification plus deliberate exercise of the error paths.

Run:  python selftest.py          fast checks, no model inference
      python selftest.py --live   also runs a real review of a planted-defect file

Exits non-zero if any check fails.
"""

import argparse
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cli  # noqa: E402
import collect  # noqa: E402
import ollama_client as oc  # noqa: E402
import prompts  # noqa: E402
import render  # noqa: E402

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


def check(name, fn):
    try:
        detail = fn()
        RESULTS.append((True, name, detail or "ok"))
    except AssertionError as e:
        RESULTS.append((False, name, "assertion failed: %s" % e))
    except Exception as e:
        RESULTS.append((False, name, "unexpected %s: %s" % (type(e).__name__, e)))


# --------------------------------------------------------------------------
# configuration and connectivity
# --------------------------------------------------------------------------

def t_config():
    cfg, notes = cli.load_config()
    assert cfg["base_url"].startswith("http"), "base_url must be a URL"
    assert cfg["timeout_s"] > 0, "timeout must be positive"
    return "endpoint=%s model=%s" % (cfg["base_url"], cfg["model"])


def t_server_reachable():
    cfg, _ = cli.load_config()
    models = oc.list_models(cfg)
    assert models, "server reachable but no models installed"
    return "%d model(s) installed" % len(models)


def t_model_resolves():
    cfg, _ = cli.load_config()
    names = {m["name"] for m in oc.list_models(cfg)}
    model, _notes = oc.resolve_model(cfg, None, names)
    return "resolved to %s" % model


def t_bare_family_name_resolves():
    cfg, _ = cli.load_config()
    names = {m["name"] for m in oc.list_models(cfg)}
    family = sorted(names)[0].split(":")[0]
    model, notes = oc.resolve_model(cfg, family, names)
    assert model in names, "bare family name did not resolve to an installed tag"
    return "%s -> %s" % (family, model)


# --------------------------------------------------------------------------
# error paths - each must produce a typed error, not an exception
# --------------------------------------------------------------------------

def t_err_unreachable():
    cfg, _ = cli.load_config()
    cfg = dict(cfg, base_url="http://127.0.0.1:9", connect_timeout_s=2)
    try:
        oc.list_models(cfg)
    except oc.OllamaError as e:
        assert e.kind == "unreachable", "expected unreachable, got %s" % e.kind
        assert "ollama serve" in e.remedy, "remedy must tell the user how to start it"
        return e.kind
    raise AssertionError("expected an OllamaError")


def t_err_model_missing():
    cfg, _ = cli.load_config()
    try:
        oc.resolve_model(cfg, "definitely-not-a-real-model", {"a:1", "b:2"})
    except oc.OllamaError as e:
        assert e.kind == "model_missing"
        assert "ollama pull" in e.remedy, "remedy must suggest pulling the model"
        return e.kind
    raise AssertionError("expected an OllamaError")


def t_err_cloud_blocked():
    cfg, _ = cli.load_config()
    cfg = dict(cfg, allow_cloud_models=False)
    try:
        oc.resolve_model(cfg, "something:cloud", set())
    except oc.OllamaError as e:
        assert e.kind == "cloud_blocked"
        return e.kind
    raise AssertionError("expected cloud model to be blocked")


def t_err_missing_file():
    cfg, _ = cli.load_config()
    try:
        collect.from_files(cfg, ["./__no_such_file_here__.py"])
    except collect.InputError as e:
        assert "does not exist" in e.detail
        return "rejected"
    raise AssertionError("expected InputError")


def t_err_binary_file():
    cfg, _ = cli.load_config()
    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "blob.dat")
        with open(p, "wb") as fh:
            fh.write(b"\x00\x01\x02binary\x00content")
        try:
            collect.from_files(cfg, [p])
        except collect.InputError as e:
            assert "null bytes" in e.detail or "binary" in e.detail
            return "rejected"
        raise AssertionError("expected binary file to be rejected")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_err_empty_file():
    cfg, _ = cli.load_config()
    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "empty.py")
        open(p, "w").close()
        try:
            collect.from_files(cfg, [p])
        except collect.InputError as e:
            assert "empty" in e.detail
            return "rejected"
        raise AssertionError("expected empty file to be rejected")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t_err_not_a_repo():
    cfg, _ = cli.load_config()
    tmp = tempfile.mkdtemp()
    try:
        collect.from_git(cfg, cwd=tmp)
    except collect.InputError as e:
        assert "git repositor" in e.detail.lower() or "empty" in e.detail.lower()
        return "handled"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    raise AssertionError("expected InputError outside a repository")


def t_truncation():
    cfg, _ = cli.load_config()
    cfg = dict(cfg, max_file_chars=200)
    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "big.py")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("x = 1\n" * 5000)
        inp = collect.from_files(cfg, [p])
        assert inp.chunks[0].truncated, "oversized file should be marked truncated"
        assert "TRUNCATED" in inp.chunks[0].text
        return "capped at %d chars" % cfg["max_file_chars"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# parser tiers
# --------------------------------------------------------------------------

def t_parse_strict():
    f, mode = render.parse_findings('{"findings":[]}')
    assert f == [] and mode == "strict"
    return mode


def t_parse_fenced():
    text = 'Sure, here you go:\n```json\n{"findings":[{"severity":"high",' \
           '"category":"security","location":"a.py:1","issue":"i","why":"w",' \
           '"suggested_fix":"s"}]}\n```\nHope that helps.'
    f, mode = render.parse_findings(text)
    assert mode == "fenced" and len(f) == 1
    return mode


def t_parse_salvaged():
    text = 'Here is my review. {"findings":[{"severity":"nonsense",' \
           '"category":"bogus","location":"x"}]} Let me know.'
    f, mode = render.parse_findings(text)
    assert mode in ("salvaged", "fenced"), "expected salvage, got %s" % mode
    assert f[0]["severity"] == "info", "invalid severity must degrade to info"
    assert f[0]["category"] == "logic", "invalid category must degrade to logic"
    return mode


def t_parse_garbage():
    f, mode = render.parse_findings("I am afraid I cannot help with that.")
    assert f is None and mode is None
    return "returns None so the caller can degrade"


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
    cfg, _ = cli.load_config()
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
# live inference
# --------------------------------------------------------------------------

def t_live_review():
    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "planted.py")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(PLANTED_DEFECTS)
        code = cli.main(["review", "--file", p, "--json", "--timeout", "240"])
        assert code == 0, "live review exited %s" % code
        return "completed (inspect the JSON above for findings)"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="also run real inference")
    args = ap.parse_args()

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
        ("render never crashes", t_render_never_crashes),
        ("prompts well-formed", t_prompt_shape),
    ]
    for name, fn in checks:
        check(name, fn)

    if args.live:
        check("live review of planted defects", t_live_review)

    print("\n%-34s %s" % ("CHECK", "RESULT"))
    print("-" * 78)
    failed = 0
    for ok, name, detail in RESULTS:
        if not ok:
            failed += 1
        print("%-34s %-4s %s" % (name, "PASS" if ok else "FAIL", detail))
    print("-" * 78)
    print("%d passed, %d failed" % (len(RESULTS) - failed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
