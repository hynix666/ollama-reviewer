---
description: Check local Ollama health and list available review models
---

```bash
python ~/.claude/skills/ollama-reviewer/scripts/cli.py status
```

Report the endpoint, the resolved review model, and the installed models.

If $ARGUMENTS names a model, check that it resolves:

```bash
python ~/.claude/skills/ollama-reviewer/scripts/cli.py status --model $ARGUMENTS
```

If Ollama is unreachable, relay the remedy from the output verbatim rather than
guessing at a cause.

To verify the whole tool including its error paths:

```bash
python ~/.claude/skills/ollama-reviewer/scripts/selftest.py
```
