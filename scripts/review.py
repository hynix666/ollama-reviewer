"""Review orchestration: parse model output, run models over chunks, assemble.

`cli.py` owns argument parsing, exit codes and front-end concerns; this module
owns what happens during a review and deliberately knows nothing about
argparse - callers pass a ReviewOptions rather than a Namespace, so the
orchestration can be driven from a test or another front end. Tolerant
parsing of model output lives here too: it is the first stage of the review
ladder, not presentation.
"""

import json
import os
import re
import time

import consensus
import ollama_client as oc
import prompts
import render

SEVERITY_RANK = {s: i for i, s in enumerate(prompts.SEVERITIES)}
FATAL_KINDS = ("unreachable", "model_missing", "cloud_blocked")
MIN_CHUNK_BUDGET_S = 5


def _balanced_object(text):
    """Extract the first balanced {...} region, ignoring braces inside strings."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def parse_findings(text):
    """Tolerantly extract findings from model output.

    Returns (findings, parse_mode) where parse_mode is "strict", "fenced",
    "salvaged", or None when nothing parseable was found.
    """
    if not text:
        return None, None

    for candidate, mode in _candidates(text):
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, list):
            data = {"findings": data}
        if not isinstance(data, dict):
            continue
        raw = data.get("findings")
        if raw is None and "severity" in data:
            raw = [data]
        if not isinstance(raw, list):
            continue
        return [_normalize(f) for f in raw if isinstance(f, dict)], mode

    return None, None


def _candidates(text):
    stripped = text.strip()
    yield stripped, "strict"
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fence:
        yield fence.group(1).strip(), "fenced"
    salvaged = _balanced_object(text)
    if salvaged:
        yield salvaged, "salvaged"


def _normalize(f):
    """Coerce a finding into the contract, tolerating model sloppiness."""
    sev = str(f.get("severity", "info")).strip().lower()
    if sev not in SEVERITY_RANK:
        sev = "info"
    cat = str(f.get("category", "logic")).strip().lower()
    if cat not in prompts.FOCUS_AREAS:
        cat = "logic"
    return {
        "severity": sev,
        "category": cat,
        "location": str(f.get("location") or "unspecified").strip(),
        "issue": str(f.get("issue") or "").strip(),
        "why": str(f.get("why") or "").strip(),
        "suggested_fix": str(f.get("suggested_fix") or "").strip(),
    }


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


def scale_timeout_for_models(base_timeout_s, model_count):
    """Budget for an N-model run, returned as (timeout_s, note_or_None).

    Each extra model re-reviews every chunk, so a budget sized for one model
    starves the later files. This is review-engine policy rather than a front
    end's concern, so it lives here and cli.py and mcp_server.py both use it.
    A front end with an explicit user timeout simply never calls this.
    """
    n = max(1, int(model_count))
    if n <= 1:
        return int(base_timeout_s), None
    scaled = int(base_timeout_s) * n
    return scaled, "Timeout scaled to %ds for %d models." % (scaled, n)


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


def _raw_finding(chunk, raw):
    """Tier 3: the model's unparseable output, preserved as an info finding."""
    return {
        "severity": "info",
        "category": "logic",
        "location": chunk.label,
        "issue": "Reviewer output could not be parsed as structured findings.",
        "why": "The model did not return valid JSON after two attempts.",
        "suggested_fix": "Raw reviewer output follows:\n\n%s" % raw[:4000],
    }


def _qualify_location(finding, chunk):
    """Prefix a bare location with the chunk label - and only once.

    A location that already names the file ("collect.py:42") must not be
    turned into "scripts/collect.py: collect.py:42". Only genuinely bare
    locations ("", "unspecified", "the retry loop") get the prefix.
    """
    loc = finding.get("location") or ""
    base = os.path.basename(chunk.label.replace("\\", "/")).lower()
    if (not loc or loc == "unspecified") or (
        "/" not in loc and base not in loc.lower()
    ):
        finding["location"] = "%s: %s" % (chunk.label, loc)


def review_chunk(cfg, model, chunk, input_kind, focus, opts, budget_s):
    """Review one chunk through the three-tier degradation ladder.

    Returns (findings, mode, meta). Raises OllamaError only if every tier fails.
    """
    started = time.time()
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
        findings, mode = parse_findings(text)
        if findings is not None:
            return findings, mode, meta
        tier1_text = text
    except oc.OllamaError as e:
        if e.kind in FATAL_KINDS or e.kind == "timeout":
            raise
        tier1_text = None

    # Tier 2: unconstrained, tolerant parse. Some quantised models handle the
    # free-form prompt better than constrained decoding. It runs on the time
    # tier 1 left, not a fresh budget, so the shared deadline holds.
    remaining_s = max(0.0, budget_s - (time.time() - started))
    try:
        text, meta = oc.generate(
            cfg, model, system, user,
            schema=None,
            timeout=remaining_s,
            temperature=opts.temperature,
            debug=opts.debug,
        )
    except oc.OllamaError as e:
        if tier1_text is None or e.kind in FATAL_KINDS:
            raise
        # Tier 2's transport failed, but tier 1 did answer - badly. Surface
        # that text rather than discarding the model's only output.
        return (
            [_raw_finding(chunk, tier1_text.strip())],
            "raw",
            {"degraded": "second pass failed (%s); surfaced the unparseable "
                         "first pass" % e.kind},
        )
    findings, mode = parse_findings(text)
    if findings is not None:
        meta["degraded"] = "schema-constrained output failed; used free-form parse"
        return findings, mode, meta

    # Tier 3: surface the raw text rather than losing the reviewer's thinking.
    raw = (text or tier1_text or "").strip()
    meta["degraded"] = "output was not parseable as JSON; surfaced as raw text"
    return [_raw_finding(chunk, raw)], "raw", meta


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
                    _qualify_location(f, chunk)
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
