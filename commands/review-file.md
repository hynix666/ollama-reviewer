---
description: Review specific files by path with the local Ollama model
---

Review the file(s) named in $ARGUMENTS. If none are named, ask which files, or
offer the most recently modified source files as candidates.

```bash
python ~/.claude/skills/ollama-reviewer/scripts/cli.py review --file $ARGUMENTS
```

Add `--focus security,tests` if the user asked to narrow the review, and
`--instructions "..."` to steer it at a specific function or concern.

Then follow `~/.claude/skills/ollama-reviewer/SKILL.md`: verify each finding against
the real code, apply only what you agree with, and report your triage — accepted,
rejected, and the reasoning for each.
