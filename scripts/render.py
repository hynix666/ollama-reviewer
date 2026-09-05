"""Markdown rendering of review results: the presentation half of the output.

Model-output parsing lives in review.py next to its engine consumer; this
module turns finished results and errors into what the user sees.
"""

import consensus
from prompts import SEVERITIES

SEVERITY_ICON = {
    "critical": "[CRITICAL]",
    "high": "[HIGH]",
    "medium": "[MEDIUM]",
    "low": "[LOW]",
    "info": "[INFO]",
}

def compress_degraded(entries):
    """Fold identical degradation notes into one line naming the chunks.

    When several chunks hit the same failure - the common case is a model
    fighting the transport, where tier 2 dies the same way on every chunk -
    one note per chunk drowns the report in copies of the same sentence.
    The (model, message) pair is the group key, so nothing is lost: every
    affected chunk and model still appears by name. A singleton keeps the
    exact per-chunk wording, so a lone degradation reads exactly as before.

    `entries` are (chunk_label, model, message) tuples as collected by
    review.run_review; report wording belongs here, the engine stays
    presentation-free.
    """
    by_group = {}
    order = []
    for label, model, msg in entries:
        key = (model, msg)
        if key not in by_group:
            by_group[key] = []
            order.append(key)
        by_group[key].append(label)
    out = []
    for model, msg in order:
        labels = by_group[(model, msg)]
        if len(labels) == 1:
            out.append("%s (%s): %s" % (labels[0], model, msg))
        else:
            out.append(
                "%s (%s) on %d chunk(s): %s"
                % (msg, model, len(labels), ", ".join(labels))
            )
    return out


def sort_findings(findings):
    """Report order: the consensus key, shared with consensus.sort_merged.

    One implementation of "corroborated first, then severity, then category",
    so the rule cannot drift between formats: a two-model-agreed high
    outranks a lone critical, and single-model runs (no model_count) keep
    plain severity order because the corroboration term defaults to 1.
    """
    return sorted(findings, key=consensus.sort_key)


def to_markdown(result):
    """Render a result dict as Markdown for a human or for Claude to read."""
    lines = []
    status = result.get("status")

    if status == "error":
        err = result.get("error") or {}
        lines += [
            "# Ollama review unavailable",
            "",
            "**Problem:** %s" % err.get("detail", "unknown"),
            "",
            "**How to fix:** %s" % err.get("remedy", "(no remedy recorded)"),
            "",
            "> The review did not run. Nothing about the code has been verified or "
            "cleared by this tool.",
        ]
        return "\n".join(lines)

    inp = result.get("input", {})
    agreement = result.get("agreement")
    models = result.get("models") or [result.get("model") or "?"]
    title = models[0] if len(models) == 1 else "%d models" % len(models)
    # The engine's budget decision travels in the result; show it so "1.2s"
    # can be read against what it was allowed to spend. Absent (hand-made
    # dicts, the error path) the header keeps its old shape.
    budget_s = result.get("timeout_s")
    spent = "%.1fs" % result.get("elapsed_s", 0.0)
    if budget_s:
        spent += " of %ds budget" % budget_s
    lines += [
        "# Local review - %s" % title,
        "",
        "%s | %s chunk(s), %s chars | %s"
        % (
            inp.get("kind", "?"),
            inp.get("chunks", "?"),
            inp.get("chars", "?"),
            spent,
        ),
        "",
    ]
    if agreement:
        raw = agreement.get("raw_counts", {})
        lines += [
            "Reviewed by: %s"
            % ", ".join("`%s` (%d raw)" % (m, raw.get(m, 0)) for m in agreement["models"]),
            "",
            "After reconciling: **%d corroborated**, %d raised by a single model."
            % (agreement["corroborated"], agreement["single"]),
            "",
            "> Corroboration raises confidence; it does not confer correctness, and a "
            "single-model finding is not thereby wrong. Verify either way.",
            "",
        ]

    # Front-end process notes first; then a labeled Degradations section
    # naming what the run itself survived - chunk_errors folded in. The
    # engine echoes degradations into notes for JSON consumers; rendering
    # both would duplicate every line, and a single blank line would let
    # the Note bullets read as a continuation of the section's list.
    degradations = list(result.get("degradations") or [])
    for ce in result.get("chunk_errors") or []:
        degradations.append("chunk `%s` failed: %s" % (ce["label"], ce["error"]["detail"]))
    for note in result.get("notes", []):
        if degradations and note in degradations:
            continue
        lines.append("- Note: %s" % note)
    for warn in inp.get("warnings", []):
        lines.append("- Warning: %s" % warn)
    for sk in inp.get("skipped", []):
        lines.append("- Skipped `%s` (%s)" % (sk["path"], sk["reason"]))
    if degradations:
        if lines[-1] != "":
            lines.append("")
        lines += ["## Degradations", ""]
        for d in degradations:
            lines.append("- %s" % d)
    if lines[-1] != "":
        lines.append("")

    findings = sort_findings(result.get("findings", []))
    if not findings:
        lines += [
            "**No findings.** The reviewer reported nothing in the requested focus "
            "areas.",
            "",
            "> A clean local review is weak evidence, not proof. It does not replace "
            "tests.",
        ]
        return "\n".join(lines)

    counts = {}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    summary = ", ".join(
        "%d %s" % (counts[s], s) for s in SEVERITIES if s in counts
    )
    lines += ["**%d finding(s):** %s" % (len(findings), summary), ""]

    for i, f in enumerate(findings, 1):
        header = "### %d. %s %s - %s" % (
            i,
            SEVERITY_ICON.get(f["severity"], ""),
            f["category"],
            f["location"],
        )
        if f.get("raised_by"):
            # ASCII only: Windows consoles default to cp1252 and mangle anything else.
            header += "  --  %s" % (
                "agreed by %s" % ", ".join(f["raised_by"])
                if f.get("model_count", 1) > 1
                else "only %s" % f["raised_by"][0]
            )
        lines += [
            header,
            "",
            "**Issue:** %s" % (f["issue"] or "(none stated)"),
            "",
            "**Trigger:** %s" % (f["why"] or "(none stated)"),
            "",
            "**Suggested fix:** %s" % (f["suggested_fix"] or "(none stated)"),
            "",
        ]
        if f.get("severity_spread"):
            lines += [
                "**Severity disputed:** %s rated it %s."
                % (
                    "models" if f.get("model_count", 1) > 1 else "runs",
                    " / ".join(f["severity_spread"]),
                ),
                "",
            ]

    if result.get("status") == "partial":
        lines += [
            "---",
            "",
            "> Status: **partial**. Some output could not be parsed as structured "
            "findings and appears above as raw text.",
            "",
        ]

    lines += [
        "---",
        "",
        "> These are suggestions from a local assistant model. Each must be verified "
        "before acting on it.",
    ]
    return "\n".join(lines)


def status_markdown(payload):
    """Render the health-check payload."""
    if payload.get("status") == "error":
        err = payload.get("error", {})
        return "\n".join(
            [
                "# Ollama status: UNAVAILABLE",
                "",
                "**Problem:** %s" % err.get("detail"),
                "",
                "**How to fix:** %s" % err.get("remedy"),
            ]
        )

    lines = [
        "# Ollama status: OK",
        "",
        "Endpoint: `%s`" % payload.get("base_url"),
        "Configured model: `%s`" % payload.get("configured_model"),
        "Resolved model: `%s`" % (payload.get("resolved_model") or "(unresolved)"),
        "Fallback chain: %s" % (", ".join(payload.get("fallback_models") or []) or "(none)"),
        "",
        "## Installed models",
        "",
        "| Model | Size | Context |",
        "| --- | --- | --- |",
    ]
    for m in payload.get("models", []):
        lines.append(
            "| `%s` | %s | %s |"
            % (m.get("name"), m.get("size_h", "?"), m.get("context", "?"))
        )
    for note in payload.get("notes", []):
        lines += ["", "- %s" % note]
    return "\n".join(lines)
