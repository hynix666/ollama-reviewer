"""Review orchestration: run models over chunks and assemble the result.

`cli.py` owns argument parsing, configuration loading and exit codes. This
module owns what actually happens during a review, and deliberately knows
nothing about argparse - callers pass a ReviewOptions rather than a Namespace,
so the orchestration can be driven from a test or another front end.
"""

import time

import consensus
import ollama_client as oc
import prompts
import render

FATAL_KINDS = ("unreachable", "model_missing", "cloud_blocked")
MIN_CHUNK_BUDGET_S = 5


class ReviewFailure(Exception):
    """Nothing usable came back. Carries the error dict cli.py should render."""

    def __init__(self, error):
        super().__init__(error.get("detail", "review failed"))
        self.error = error


class ReviewOptions:
    """What a review needs from its caller, free of any CLI types."""

    def __init__(
        self, adversarial=False, instructions=None, temperature=None, debug=False
    ):
        self.adversarial = adversarial
        self.instructions = instructions
        self.temperature = temperature
        self.debug = debug


def resolve_models(cfg, requested, notes):
    """Resolve requested model names against what is installed.

    `requested` may be empty, meaning "use the configured default and its
    fallback chain". Returns the resolved list, de-duplicated, order preserved.
    """
    installed = oc.list_models(cfg)
    names = {m.get("name") for m in installed if m.get("name")}

    if not requested:
        resolved, rnotes = oc.resolve_model(cfg, None, names)
        notes += rnotes
        return [resolved]

    models = []
    for want in requested:
        resolved, rnotes = oc.resolve_model(cfg, want, names)
        notes += rnotes
        if resolved not in models:
            models.append(resolved)

    if len(models) == 1 and len(requested) > 1:
        notes.append(
            "All requested models resolved to %s; consensus needs at least two "
            "distinct models." % models[0]
        )
    return models


def review_chunk(cfg, model, chunk, input_kind, focus, opts, budget_s):
    """Review one chunk through the three-tier degradation ladder.

    Returns (findings, mode, meta). Raises OllamaError only if every tier fails.
    """
    system = prompts.build_system_prompt(opts.adversarial)
    user = prompts.build_user_prompt(
        chunk.label,
        chunk.text,
        input_kind,
        focus,
        adversarial=opts.adversarial,
        extra_instructions=opts.instructions,
        truncated=chunk.truncated,
    )

    # Tier 1: schema-constrained decoding.
    try:
        text, meta = oc.generate(
            cfg, model, system, user,
            schema=prompts.RESPONSE_SCHEMA,
            timeout=budget_s,
            temperature=opts.temperature,
            debug=opts.debug,
        )
        findings, mode = render.parse_findings(text)
        if findings is not None:
            return findings, mode, meta
        tier1_text = text
    except oc.OllamaError as e:
        if e.kind in FATAL_KINDS or e.kind == "timeout":
            raise
        tier1_text = None

    # Tier 2: unconstrained, tolerant parse. Some quantised models handle the
    # free-form prompt better than constrained decoding.
    text, meta = oc.generate(
        cfg, model, system, user,
        schema=None,
        timeout=budget_s,
        temperature=opts.temperature,
        debug=opts.debug,
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


def _budget_exhausted_error(model_count):
    return {
        "kind": "timeout",
        "detail": "Overall time budget exhausted before this chunk.",
        "remedy": "Raise --timeout or review fewer files at once."
        + (
            " Reviewing with %d models multiplies the work." % model_count
            if model_count > 1
            else ""
        ),
    }


def run_review(cfg, models, inp, focus, opts, notes):
    """Run every model over every chunk under one shared deadline.

    `notes` is appended to in place. Raises ReviewFailure when nothing usable
    came back at all; a partial result is returned rather than raised, so a
    single dead model never discards the others' work.
    """
    started = time.time()
    deadline = started + cfg["timeout_s"]
    per_model = [(m, []) for m in models]
    by_model = dict((m, fs) for m, fs in per_model)
    active = list(models)
    chunk_errors = []
    degraded = []

    for chunk in inp.chunks:
        for model in list(active):
            remaining = deadline - time.time()
            if remaining <= MIN_CHUNK_BUDGET_S:
                chunk_errors.append(
                    {
                        "label": chunk.label,
                        "model": model,
                        "error": _budget_exhausted_error(len(models)),
                    }
                )
                continue
            try:
                got, _mode, meta = review_chunk(
                    cfg, model, chunk, inp.kind, focus, opts, remaining
                )
                for f in got:
                    if f["location"] in ("unspecified", "") or "/" not in f["location"]:
                        f["location"] = "%s: %s" % (chunk.label, f["location"])
                by_model[model] += got
                for key, fixed in (
                    ("degraded", None),
                    ("output_truncated", "model output hit the length limit"),
                    ("input_possibly_truncated", None),
                ):
                    if meta.get(key):
                        degraded.append(
                            "%s (%s): %s" % (chunk.label, model, fixed or meta[key])
                        )
            except oc.OllamaError as e:
                chunk_errors.append(
                    {"label": chunk.label, "model": model, "error": e.to_dict()}
                )
                # A dead model drops out of the rest of the run; others carry on.
                if e.kind in FATAL_KINDS:
                    active.remove(model)
                    notes.append("Dropped %s after a fatal error: %s" % (model, e.detail))
        if not active:
            break

    total = sum(len(fs) for fs in by_model.values())
    if not total and chunk_errors and not active:
        raise ReviewFailure(chunk_errors[0]["error"])

    if len(models) > 1:
        merged = consensus.sort_merged(consensus.reconcile(per_model))
        agreement = consensus.summarize(per_model, merged)
    else:
        merged = by_model[models[0]]
        agreement = None

    return {
        "status": "partial" if (chunk_errors or degraded) else "ok",
        "model": models[0] if len(models) == 1 else None,
        "models": models,
        "agreement": agreement,
        "adversarial": bool(opts.adversarial),
        "focus": focus,
        "elapsed_s": round(time.time() - started, 2),
        "input": inp.summary(),
        "findings": merged,
        "chunk_errors": chunk_errors,
        "notes": notes + degraded,
        "error": None,
    }
