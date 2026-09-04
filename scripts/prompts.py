"""The exact prompts sent to the local model, plus the response schema.

Design notes:
  - The system prompt tells the model it is *one* reviewer whose output will be
    filtered. That framing measurably reduces padding and flattery.
  - It is given explicit permission to return zero findings. Without that, small
    quantised models invent problems to look useful.
  - Every finding must name a triggering condition. That single rule is the main
    defence against confident nonsense from a Q4 model.
"""

FOCUS_AREAS = {
    "logic": "Logic correctness and potential bugs",
    "security": "Security issues: injection, auth/authz, secrets, unsafe deserialisation, path traversal, unsafe defaults",
    "performance": "Performance: needless work in hot paths, N+1 queries, unbounded memory, blocking I/O",
    "edge-cases": "Edge cases and missing error handling: nulls, empty inputs, boundaries, partial failure, concurrency",
    "tests": "Test coverage gaps, plus the specific tests that should exist",
    "design": "Design tradeoffs and architectural concerns",
}

DEFAULT_FOCUS = ["logic", "security", "performance", "edge-cases", "tests"]


def resolve_focus(requested, adversarial=False):
    """Validate a focus request against FOCUS_AREAS; inject 'design' for
    adversarial runs. Shared by every front end so the wording of the
    rejection and the adversarial rule exist in exactly one place.

    Returns (focus_list, None) on success or (None, message) when the request
    names unknown areas. `requested` may be None/empty for the default set,
    a comma-separated string (CLI style), or a list (MCP style).
    """
    if isinstance(requested, str):
        requested = [f.strip().lower() for f in requested.split(",") if f.strip()]
    focus = list(requested) if requested else list(DEFAULT_FOCUS)
    bad = [f for f in focus if f not in FOCUS_AREAS]
    if bad:
        return None, "Unknown focus area(s): %s. Valid: %s" % (
            ", ".join(bad), ", ".join(sorted(FOCUS_AREAS)))
    if adversarial and "design" not in focus:
        focus.append("design")
    return focus, None

SEVERITIES = ["critical", "high", "medium", "low", "info"]

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": SEVERITIES},
                    "category": {"type": "string", "enum": list(FOCUS_AREAS.keys())},
                    "location": {"type": "string"},
                    "issue": {"type": "string"},
                    "why": {"type": "string"},
                    "suggested_fix": {"type": "string"},
                },
                "required": [
                    "severity",
                    "category",
                    "location",
                    "issue",
                    "why",
                    "suggested_fix",
                ],
            },
        }
    },
    "required": ["findings"],
}

SYSTEM_PROMPT = """\
You are a meticulous senior code reviewer.

You are ONE reviewer among several. A primary engineer will independently verify \
every point you raise and will discard anything you cannot justify. Your value \
comes from precision, not volume.

Rules you must follow:

1. Review ONLY the code provided. Never invent context you were not given. If a \
function is called but not shown, do not guess what it does.
2. Report a finding ONLY if you can name the concrete condition under which the \
code misbehaves - a specific input, state, or sequence of events. A finding \
without a triggering condition is noise; omit it.
3. Do not restate what the code does. Do not praise it. Do not comment on \
formatting, naming style, or anything a linter would catch.
4. If you are unsure whether something is a real defect, either omit it or set its \
severity to "info" and say plainly what you are unsure about.
5. Returning zero findings is a valid and frequently correct answer. Never pad.
6. When reviewing a diff, judge the changed lines. Mention surrounding code only \
when the change breaks it.

Severity means:
  critical - data loss, remote compromise, or corruption in normal operation
  high     - a bug that will occur in realistic use, or a real security weakness
  medium   - a bug requiring unusual but plausible conditions
  low      - a minor robustness or maintainability defect with real consequences
  info     - an observation you cannot fully verify from the code shown

For "location", give the most precise anchor you can: file:line, or the function \
name, or the diff hunk header. Never write "various" or "throughout".
"""

ADVERSARIAL_BLOCK = """\

ADVERSARIAL MODE
Assume this code is broken and that its design is wrong. Your task is to find the \
failure, not to confirm the author's thinking.

Attack the assumptions rather than the syntax. Specifically consider: what breaks \
under concurrency, hostile input, partial failure, restart mid-operation, or 100x \
scale? What does this design make difficult that a different design would make \
easy? Name the tradeoff the author most likely did not consider, and state the \
strongest single argument against the approach taken.

Every rule above still applies - especially rule 2. An adversarial review that \
cannot name triggering conditions is worthless.
"""


def build_user_prompt(chunk_label, chunk_text, input_kind, focus, adversarial=False,
                      extra_instructions=None, truncated=False):
    """Assemble the user-turn prompt for one reviewable chunk."""
    focus_lines = "\n".join(
        "  - %s" % FOCUS_AREAS[f] for f in focus if f in FOCUS_AREAS
    )

    parts = [
        "## Review scope",
        "",
        "Focus your review on these areas only:",
        focus_lines,
        "",
        "## What you are looking at",
        "",
        "Source type: %s" % input_kind,
        "Unit under review: %s" % chunk_label,
    ]
    if truncated:
        parts.append(
            "NOTE: this content was truncated. Do not speculate about the omitted part."
        )
    if adversarial:
        parts.append(ADVERSARIAL_BLOCK)
    if extra_instructions:
        parts += [
            "",
            "## Additional direction from the engineer",
            "",
            extra_instructions.strip(),
        ]

    parts += [
        "",
        "## Code",
        "",
        "```",
        chunk_text,
        "```",
        "",
        "## Output format",
        "",
        'Respond with JSON only - no prose before or after - shaped as:',
        '{"findings": [{"severity": "...", "category": "...", "location": "...",',
        ' "issue": "...", "why": "...", "suggested_fix": "..."}]}',
        "",
        'severity must be one of: %s' % ", ".join(SEVERITIES),
        'category must be one of: %s' % ", ".join(FOCUS_AREAS.keys()),
        "",
        '"issue" states the defect. "why" names the exact condition that triggers it.',
        '"suggested_fix" is a concrete change, not a general principle.',
        "",
        'If the code is sound, return exactly: {"findings": []}',
    ]
    return "\n".join(parts)


def build_system_prompt(adversarial=False):
    return SYSTEM_PROMPT
