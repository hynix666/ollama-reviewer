"""ollama-review CLI.

Contract: this program never raises into the caller's session. Every path prints
valid Markdown (or JSON with --json) and exits with a meaningful code.

Exit codes
  0  the review ran (findings, or none, are both success)
  2  input error - bad paths, empty diff, not a repo
  3  Ollama unavailable - server down, model missing, cloud blocked
  4  timeout
  5  internal error
"""

import argparse
import json
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import collect  # noqa: E402
import ollama_client as oc  # noqa: E402
import prompts  # noqa: E402
import render  # noqa: E402

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json"
)

EXIT_OK, EXIT_INPUT, EXIT_UNAVAILABLE, EXIT_TIMEOUT, EXIT_INTERNAL = 0, 2, 3, 4, 5

ERROR_EXIT = {
    "unreachable": EXIT_UNAVAILABLE,
    "model_missing": EXIT_UNAVAILABLE,
    "cloud_blocked": EXIT_UNAVAILABLE,
    "timeout": EXIT_TIMEOUT,
    "internal": EXIT_INTERNAL,
}

DEFAULTS = {
    "base_url": "http://127.0.0.1:11434",
    "model": "qwen3-coder:30b",
    "fallback_models": [],
    "temperature": 0.1,
    "timeout_s": 180,
    "connect_timeout_s": 5,
    "max_retries": 3,
    "backoff_base_s": 1.5,
    "max_file_chars": 60000,
    "max_total_chars": 180000,
    "max_files": 25,
    "allow_cloud_models": False,
}


def load_config():
    """Config file over defaults, environment over both. Never fails hard."""
    cfg = dict(DEFAULTS)
    notes = []
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    except FileNotFoundError:
        notes.append("No config.json found; using built-in defaults.")
    except json.JSONDecodeError as e:
        notes.append("config.json is malformed (%s); using built-in defaults." % e)

    env_url = os.environ.get("OLLAMA_HOST")
    if env_url:
        cfg["base_url"] = env_url
        notes.append("Endpoint overridden by OLLAMA_HOST (%s)." % env_url)

    normalized = oc.normalize_base_url(cfg["base_url"])
    if normalized != cfg["base_url"].rstrip("/"):
        notes.append(
            "Rewrote bind address %s to %s for connecting." % (cfg["base_url"], normalized)
        )
    cfg["base_url"] = normalized

    if os.environ.get("OLLAMA_REVIEW_MODEL"):
        cfg["model"] = os.environ["OLLAMA_REVIEW_MODEL"]
        notes.append("Model overridden by OLLAMA_REVIEW_MODEL.")
    if os.environ.get("OLLAMA_REVIEW_TIMEOUT"):
        try:
            cfg["timeout_s"] = int(os.environ["OLLAMA_REVIEW_TIMEOUT"])
        except ValueError:
            notes.append("OLLAMA_REVIEW_TIMEOUT is not an integer; ignored.")
    return cfg, notes


def emit(result, as_json, markdown_fn=render.to_markdown):
    if as_json:
        print(json.dumps(result, indent=2))
    else:
        print(markdown_fn(result))


def fail(error_dict, as_json, notes=None, markdown_fn=render.to_markdown):
    """Emit a structured failure and return its exit code. Never raises."""
    result = {
        "status": "error",
        "findings": [],
        "error": error_dict,
        "notes": notes or [],
    }
    emit(result, as_json, markdown_fn)
    return ERROR_EXIT.get(error_dict.get("kind"), EXIT_INPUT)


def human_size(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return "%.0f%s" % (n, unit)
        n /= 1024
    return "%.1fTB" % n


def cmd_status(args):
    cfg, notes = load_config()
    try:
        models = oc.list_models(cfg)
    except oc.OllamaError as e:
        return fail(e.to_dict(), args.json, notes, render.status_markdown)

    names = {m.get("name") for m in models if m.get("name")}
    resolved, rnotes = None, []
    try:
        resolved, rnotes = oc.resolve_model(cfg, args.model, names)
    except oc.OllamaError as e:
        notes.append("%s %s" % (e.detail, e.remedy))

    payload = {
        "status": "ok",
        "base_url": cfg["base_url"],
        "configured_model": cfg["model"],
        "resolved_model": resolved,
        "fallback_models": cfg.get("fallback_models"),
        "notes": notes + rnotes,
        "models": [
            {
                "name": m.get("name"),
                "size_h": human_size(m.get("size")),
                "context": (m.get("details") or {}).get("context_length", "?"),
            }
            for m in sorted(models, key=lambda x: x.get("name") or "")
        ],
    }
    emit(payload, args.json, render.status_markdown)
    return EXIT_OK


def review_chunk(cfg, model, chunk, input_kind, focus, args, budget_s):
    """Review one chunk through the three-tier degradation ladder.

    Returns (findings, mode, meta). Raises OllamaError only if every tier fails.
    """
    system = prompts.build_system_prompt(args.adversarial)
    user = prompts.build_user_prompt(
        chunk.label,
        chunk.text,
        input_kind,
        focus,
        adversarial=args.adversarial,
        extra_instructions=args.instructions,
        truncated=chunk.truncated,
    )

    # Tier 1: schema-constrained decoding.
    try:
        text, meta = oc.generate(
            cfg, model, system, user,
            schema=prompts.RESPONSE_SCHEMA,
            timeout=budget_s,
            temperature=args.temperature,
            debug=args.debug,
        )
        findings, mode = render.parse_findings(text)
        if findings is not None:
            return findings, mode, meta
        tier1_text = text
    except oc.OllamaError as e:
        if e.kind in ("unreachable", "model_missing", "cloud_blocked", "timeout"):
            raise
        tier1_text = None

    # Tier 2: unconstrained, tolerant parse. Some quantised models handle the
    # free-form prompt better than constrained decoding.
    text, meta = oc.generate(
        cfg, model, system, user,
        schema=None,
        timeout=budget_s,
        temperature=args.temperature,
        debug=args.debug,
    )
    findings, mode = render.parse_findings(text)
    if findings is not None:
        meta["degraded"] = "schema-constrained output failed; used free-form parse"
        return findings, mode, meta

    # Tier 3: surface the raw text rather than losing the reviewer's thinking.
    raw = (text or tier1_text or "").strip()
    meta["degraded"] = "output was not parseable as JSON; surfaced as raw text"
    return (
        [
            {
                "severity": "info",
                "category": "logic",
                "location": chunk.label,
                "issue": "Reviewer output could not be parsed as structured findings.",
                "why": "The model did not return valid JSON after two attempts.",
                "suggested_fix": "Raw reviewer output follows:\n\n%s" % raw[:4000],
            }
        ],
        "raw",
        meta,
    )


def cmd_review(args):
    cfg, notes = load_config()
    if args.timeout:
        cfg["timeout_s"] = args.timeout

    focus = prompts.DEFAULT_FOCUS
    if args.focus:
        requested = [f.strip().lower() for f in args.focus.split(",") if f.strip()]
        bad = [f for f in requested if f not in prompts.FOCUS_AREAS]
        if bad:
            return fail(
                {
                    "kind": "input",
                    "detail": "Unknown focus area(s): %s" % ", ".join(bad),
                    "remedy": "Valid areas: %s" % ", ".join(prompts.FOCUS_AREAS),
                },
                args.json,
                notes,
            )
        focus = requested
    if args.adversarial and "design" not in focus:
        focus = focus + ["design"]

    # ---- collect input -------------------------------------------------
    try:
        if args.stdin:
            inp = collect.from_stdin(cfg)
        elif args.file:
            inp = collect.from_files(cfg, args.file)
        else:
            inp = collect.from_git(cfg, ref=args.ref, staged=args.staged, cwd=args.cwd)
    except collect.InputError as e:
        return fail(e.to_dict(), args.json, notes)

    # ---- resolve model -------------------------------------------------
    try:
        models = oc.list_models(cfg)
        names = {m.get("name") for m in models if m.get("name")}
        model, rnotes = oc.resolve_model(cfg, args.model, names)
        notes += rnotes
    except oc.OllamaError as e:
        return fail(e.to_dict(), args.json, notes)

    # ---- review each chunk under a shared deadline ---------------------
    started = time.time()
    deadline = started + cfg["timeout_s"]
    findings = []
    chunk_errors = []
    degraded = []

    for chunk in inp.chunks:
        remaining = deadline - time.time()
        if remaining <= 5:
            chunk_errors.append(
                {
                    "label": chunk.label,
                    "error": {
                        "kind": "timeout",
                        "detail": "Overall time budget exhausted before this chunk.",
                        "remedy": "Raise --timeout or review fewer files at once.",
                    },
                }
            )
            continue
        try:
            got, mode, meta = review_chunk(
                cfg, model, chunk, inp.kind, focus, args, remaining
            )
            for f in got:
                if f["location"] in ("unspecified", "") or "/" not in f["location"]:
                    f["location"] = "%s: %s" % (chunk.label, f["location"])
            findings += got
            if meta.get("degraded"):
                degraded.append("%s: %s" % (chunk.label, meta["degraded"]))
            if meta.get("output_truncated"):
                degraded.append("%s: model output hit the length limit" % chunk.label)
            if meta.get("input_possibly_truncated"):
                degraded.append(
                    "%s: %s" % (chunk.label, meta["input_possibly_truncated"])
                )
        except oc.OllamaError as e:
            chunk_errors.append({"label": chunk.label, "error": e.to_dict()})
            if e.kind in ("unreachable", "model_missing", "cloud_blocked"):
                break

    if not findings and chunk_errors and len(chunk_errors) == len(inp.chunks):
        worst = chunk_errors[0]["error"]
        return fail(worst, args.json, notes)

    status = "partial" if (chunk_errors or degraded) else "ok"
    result = {
        "status": status,
        "model": model,
        "adversarial": bool(args.adversarial),
        "focus": focus,
        "elapsed_s": round(time.time() - started, 2),
        "input": inp.summary(),
        "findings": findings,
        "chunk_errors": chunk_errors,
        "notes": notes + degraded,
        "error": None,
    }
    emit(result, args.json)
    return EXIT_OK


def build_parser():
    p = argparse.ArgumentParser(
        prog="ollama-review",
        description="Local Ollama code reviewer (assistant only - never authoritative).",
    )
    p.add_argument("--json", action="store_true", help="emit JSON instead of Markdown")
    p.add_argument("--debug", action="store_true", help="include tracebacks and traces")
    sub = p.add_subparsers(dest="cmd", required=True)

    st = sub.add_parser("status", help="check Ollama health and list models")
    st.add_argument("--model", help="model to test resolution for")
    st.set_defaults(func=cmd_status)

    rv = sub.add_parser("review", help="review a diff, files, or stdin")
    src = rv.add_argument_group("input source")
    src.add_argument("--file", nargs="+", help="explicit file paths")
    src.add_argument("--ref", help="review the diff against this ref (REF...HEAD)")
    src.add_argument("--staged", action="store_true", help="review the staged diff")
    src.add_argument("--stdin", action="store_true", help="review piped input")
    src.add_argument("--cwd", default=".", help="repository directory")
    rv.add_argument("--focus", help="comma-separated: %s" % ",".join(prompts.FOCUS_AREAS))
    rv.add_argument("--adversarial", action="store_true", help="adversarial design critique")
    rv.add_argument("--instructions", help="extra steering, e.g. 'focus on the retry loop'")
    rv.add_argument("--model", help="override the review model")
    rv.add_argument("--temperature", type=float, help="override temperature")
    rv.add_argument("--timeout", type=int, help="overall time budget in seconds")
    rv.set_defaults(func=cmd_review)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        sys.stderr.write("\nCancelled by user. No changes were made.\n")
        return EXIT_INTERNAL
    except Exception as e:
        detail = "Internal error: %r" % (e,)
        if args.debug:
            detail += "\n\n" + traceback.format_exc()
        return fail(
            {
                "kind": "internal",
                "detail": detail,
                "remedy": "Re-run with --debug for a full traceback, then report it.",
            },
            args.json,
        )


if __name__ == "__main__":
    sys.exit(main())
