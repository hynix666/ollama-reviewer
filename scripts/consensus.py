"""Cross-model reconciliation of findings.

Runs the same review through several models and reconciles the results so
corroboration becomes visible.

Nothing is discarded. Filtering to findings that two models both raise would
buy precision with recall, and recall is where this reviewer is already weakest
- against a four-defect fixture it caught three, and in dogfooding the defect
that mattered most was one no model raised at all. So every finding survives,
tagged with which models produced it, and corroborated ones sort first.

Matching is structural and deterministic: no extra inference, no third model
adjudicating. Two findings describe the same defect when they share a category
and a file, and then agree by line proximity, symbol overlap, or text
similarity.
"""

import difflib
import os
import re

from prompts import SEVERITIES

SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}

# A file-ish token: something.ext, with a short extension.
FILE_RE = re.compile(r"[\w./\\-]+\.[A-Za-z0-9]{1,6}")
# A line number, as ":123".
LINE_RE = re.compile(r":(\d+)")
# Identifier-ish words, long enough not to be noise.
WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")

LINE_PROXIMITY = 3
RATIO_ALONE = 0.50  # text similarity sufficient on its own
RATIO_WITH_SYMBOL = 0.35  # ... when a symbol name also matches
RATIO_WITH_LINE = 0.30  # ... when the lines are adjacent


def parse_location(loc):
    """Split a free-form location into (file, line, symbols).

    Models write locations inconsistently - "a.py:42", "a.py: find_user",
    "in the parse loop". Anything unparseable degrades to None rather than
    blocking a match.
    """
    loc = loc or ""
    files = FILE_RE.findall(loc)
    fname = os.path.basename(files[0].replace("\\", "/")).lower() if files else None
    m = LINE_RE.search(loc)
    line = int(m.group(1)) if m else None
    words = set(w.lower() for w in WORD_RE.findall(loc))
    if fname:
        words.discard(fname)
        words.discard(os.path.splitext(fname)[0])
    return fname, line, words


def _norm(text):
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).split())


def _ratio(a, b):
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def same_defect(a, b):
    """Whether two findings plausibly describe the same underlying defect."""
    if a.get("category") != b.get("category"):
        return False

    fa, la, wa = parse_location(a.get("location"))
    fb, lb, wb = parse_location(b.get("location"))

    # Different files is a hard no. One side being unknown is not.
    if fa and fb and fa != fb:
        return False

    ratio = _ratio(a.get("issue"), b.get("issue"))
    shared_symbol = bool(wa & wb)

    if la is not None and lb is not None and abs(la - lb) <= LINE_PROXIMITY:
        return shared_symbol or ratio >= RATIO_WITH_LINE
    if shared_symbol:
        return ratio >= RATIO_WITH_SYMBOL
    return ratio >= RATIO_ALONE


def _pick_representative(members):
    """Prefer the most severe account, then the one with the most actionable fix."""
    return min(
        members,
        key=lambda m: (
            SEVERITY_RANK.get(m["finding"].get("severity"), 99),
            -len(m["finding"].get("suggested_fix") or ""),
            -len(m["finding"].get("why") or ""),
        ),
    )["finding"]


def _merge(members):
    models = sorted({m["model"] for m in members})
    rep = dict(_pick_representative(members))
    rep["raised_by"] = models
    rep["model_count"] = len(models)
    rep["agreement"] = "corroborated" if len(models) > 1 else "single"

    severities = sorted(
        {m["finding"].get("severity") for m in members},
        key=lambda s: SEVERITY_RANK.get(s, 99),
    )
    if len(severities) > 1:
        # Models disagreeing on how bad something is is itself a signal.
        rep["severity_spread"] = severities
    return rep


def reconcile(per_model):
    """Cluster findings across models.

    `per_model` is a list of (model_name, findings). Returns merged findings,
    each carrying `raised_by`, `model_count` and `agreement`.
    """
    clusters = []
    for model, findings in per_model:
        for f in findings:
            placed = False
            for c in clusters:
                if any(same_defect(f, m["finding"]) for m in c):
                    c.append({"model": model, "finding": f})
                    placed = True
                    break
            if not placed:
                clusters.append([{"model": model, "finding": f}])
    return [_merge(c) for c in clusters]


def sort_key(finding):
    """Corroborated first, then by severity, then by category.

    Findings without model_count default to 1, so for single-model runs the
    corroboration term is a constant and plain severity order falls out.
    """
    return (
        -finding.get("model_count", 1),
        SEVERITY_RANK.get(finding.get("severity"), 99),
        finding.get("category") or "",
    )


def sort_merged(findings):
    """Findings sorted by sort_key: the one report order."""
    return sorted(findings, key=sort_key)


def summarize(per_model, merged):
    """Counts for the report header."""
    corroborated = sum(1 for f in merged if f.get("model_count", 1) > 1)
    return {
        "models": [m for m, _ in per_model],
        "raw_counts": {m: len(fs) for m, fs in per_model},
        "merged": len(merged),
        "corroborated": corroborated,
        "single": len(merged) - corroborated,
    }
