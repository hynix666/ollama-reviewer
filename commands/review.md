---
description: Review the current git diff (or given files) with the local Ollama model
---

Run a local review, then triage the results yourself.

$ARGUMENTS may name files, a ref (e.g. `main`), or focus areas. Pick the matching form:

```bash
python ~/.claude/skills/ollama-reviewer/scripts/cli.py review
python ~/.claude/skills/ollama-reviewer/scripts/cli.py review --ref <REF>
python ~/.claude/skills/ollama-reviewer/scripts/cli.py review --file <PATHS>
python ~/.claude/skills/ollama-reviewer/scripts/cli.py review --focus security,tests
```

With no arguments, review the uncommitted diff.

Then follow `~/.claude/skills/ollama-reviewer/SKILL.md`: you are the decision-maker.
Verify each finding against the real code, apply only what you agree with, and report
to the user what you accepted, what you rejected, and why. Do not edit code merely
because the reviewer suggested it.

If the tool reports the reviewer is unavailable, say so in one line and continue —
never claim the code was reviewed.
