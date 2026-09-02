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
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import collect  # noqa: E402
import ollama_client as oc  # noqa: E402
import prompts  # noqa: E402
import render  # noqa: E402
import review  # noqa: E402

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
    "consensus_models": [],
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


def cmd_review(args):
    cfg, notes = load_config()
    explicit_timeout = bool(args.timeout)
    if explicit_timeout:
        cfg["timeout_s"] = args.timeout

    # ---- validate focus ------------------------------------------------
    focus = prompts.DEFAULT_FOCUS
    if args.focus:
        wanted = [f.strip().lower() for f in args.focus.split(",") if f.strip()]
        bad = [f for f in wanted if f not in prompts.FOCUS_AREAS]
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
        focus = wanted
    if args.adversarial and "design" not in focus:
        focus = focus + ["design"]

    # ---- which models were asked for -----------------------------------
    if args.models:
        requested = [m.strip() for m in args.models.split(",") if m.strip()]
    elif args.consensus:
        requested = list(cfg.get("consensus_models") or [])
        if not requested:
            return fail(
                {
                    "kind": "input",
                    "detail": "--consensus was given but consensus_models is empty.",
                    "remedy": 'Set "consensus_models" in config.json, or use '
                    "--models a,b to name them directly.",
                },
                args.json,
                notes,
            )
    elif args.model:
        requested = [args.model]
    else:
        requested = []

    # ---- collect input, resolve models, run -----------------------------
    try:
        code_only = not args.all_files
        if args.stdin:
            inp = collect.from_stdin(cfg, code_only=code_only)
        elif args.file:
            inp = collect.from_files(cfg, args.file, code_only=code_only)
        else:
            inp = collect.from_git(
                cfg, ref=args.ref, staged=args.staged, cwd=args.cwd,
                code_only=code_only,
            )
    except collect.InputError as e:
        return fail(e.to_dict(), args.json, notes)

    try:
        models = review.resolve_models(cfg, requested, notes)
    except oc.OllamaError as e:
        return fail(e.to_dict(), args.json, notes)

    # Each extra model re-reviews every chunk, so a budget sized for one model
    # silently starves the later files. Scale it unless the user set one.
    if not explicit_timeout and len(models) > 1:
        cfg["timeout_s"] *= len(models)
        notes.append(
            "Timeout scaled to %ds for %d models; pass --timeout to override."
            % (cfg["timeout_s"], len(models))
        )

    opts = review.ReviewOptions(
        adversarial=args.adversarial,
        instructions=args.instructions,
        temperature=args.temperature,
        debug=args.debug,
    )
    try:
        result = review.run_review(cfg, models, inp, focus, opts, notes)
    except review.ReviewFailure as e:
        return fail(e.error, args.json, notes)

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

    # --json and --debug are also accepted after the subcommand. argparse would
    # otherwise reject `review --json`, which is the ordering everyone reaches for.
    # default=SUPPRESS matters: without it the subparser writes its own False over
    # a --json given before the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS
    )
    common.add_argument(
        "--debug", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS
    )

    st = sub.add_parser(
        "status", parents=[common], help="check Ollama health and list models"
    )
    st.add_argument("--model", help="model to test resolution for")
    st.set_defaults(func=cmd_status)

    rv = sub.add_parser(
        "review", parents=[common], help="review a diff, files, or stdin"
    )
    src = rv.add_argument_group("input source")
    src.add_argument("--file", nargs="+", help="explicit file paths")
    src.add_argument("--ref", help="review the diff against this ref (REF...HEAD)")
    src.add_argument("--staged", action="store_true", help="review the staged diff")
    src.add_argument("--stdin", action="store_true", help="review piped input")
    src.add_argument("--cwd", default=".", help="repository directory")
    rv.add_argument("--focus", help="comma-separated: %s" % ",".join(prompts.FOCUS_AREAS))
    rv.add_argument(
        "--all-files",
        action="store_true",
        help="also review prose and lockfiles; by default Markdown, text and "
        "lockfiles are skipped so the time budget goes to code",
    )
    rv.add_argument("--adversarial", action="store_true", help="adversarial design critique")
    rv.add_argument("--instructions", help="extra steering, e.g. 'focus on the retry loop'")
    rv.add_argument("--model", help="override the review model")
    rv.add_argument(
        "--models",
        help="comma-separated models to review with; findings are reconciled "
        "across them and tagged with which models raised each one",
    )
    rv.add_argument(
        "--consensus",
        action="store_true",
        help="review with the models in config.json's consensus_models",
    )
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
