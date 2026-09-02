"""The output contract: tolerant parsing of model output, and Markdown rendering.

Parsing and rendering live together because they are two halves of one contract -
if you change the finding shape, you must change both.
"""

import json
import re

from prompts import FOCUS_AREAS, SEVERITIES

SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}
SEVERITY_ICON = {
    "critical": "[CRITICAL]",
    "high": "[HIGH]",
    "medium": "[MEDIUM]",
    "low": "[LOW]",
    "info": "[INFO]",
}


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
    if cat not in FOCUS_AREAS:
        cat = "logic"
    return {
        "severity": sev,
        "category": cat,
        "location": str(f.get("location") or "unspecified").strip(),
        "issue": str(f.get("issue") or "").strip(),
        "why": str(f.get("why") or "").strip(),
        "suggested_fix": str(f.get("suggested_fix") or "").strip(),
    }


def sort_findings(findings):
    return sorted(
        findings, key=lambda f: (SEVERITY_RANK.get(f["severity"], 99), f["category"])
    )


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
    lines += [
        "# Local review - %s" % title,
        "",
        "%s | %s chunk(s), %s chars | %.1fs"
        % (
            inp.get("kind", "?"),
            inp.get("chunks", "?"),
            inp.get("chars", "?"),
            result.get("elapsed_s", 0.0),
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

    for note in result.get("notes", []):
        lines.append("- Note: %s" % note)
    for warn in inp.get("warnings", []):
        lines.append("- Warning: %s" % warn)
    for sk in inp.get("skipped", []):
        lines.append("- Skipped `%s` (%s)" % (sk["path"], sk["reason"]))
    if result.get("chunk_errors"):
        for ce in result["chunk_errors"]:
            lines.append(
                "- Chunk `%s` failed: %s" % (ce["label"], ce["error"]["detail"])
            )
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
        "Resolved model: `%s`" % payload.get("resolved_model", "(unresolved)"),
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
