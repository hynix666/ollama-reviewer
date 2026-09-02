---
description: Adversarial design critique of code or a diff, via the local Ollama model
---

Run an adversarial review — the model is told to assume the design is wrong and to
attack its assumptions rather than its syntax.

```bash
python ~/.claude/skills/ollama-reviewer/scripts/cli.py review --adversarial
python ~/.claude/skills/ollama-reviewer/scripts/cli.py review --adversarial --file $ARGUMENTS
```

Use the `--file` form when $ARGUMENTS names paths; otherwise review the current diff.
Pass `--instructions` to aim the critique at a specific design decision.

Adversarial mode deliberately raises the false-positive rate — it is instructed to
find fault. So your filtering matters more here, not less. Follow
`~/.claude/skills/ollama-reviewer/SKILL.md`: verify before accepting anything, and
report clearly which criticisms you think land and which you reject.

A critique you disagree with is still worth reporting to the user, with your reasoning —
the disagreement is often the useful part.
