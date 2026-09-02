---
name: ollama-reviewer
description: Use when you want a second opinion on code from the local Ollama model - after implementing a feature, before merging, or when reviewing a diff for logic bugs, security issues, performance, edge cases, or missing tests. Also use when the user asks to review code locally, run an adversarial design critique, or check Ollama status.
---

# Local Ollama reviewer

A local Ollama model acting as an **assistant reviewer**. It advises. It never decides.

## Role separation - non-negotiable

| Claude (you) | Ollama model |
|---|---|
| Owns architecture, implementation, and every final decision | Offers opinions on code it is shown |
| Writes and edits all files | Never edits anything |
| Verifies every finding before acting | Cannot verify its own claims |
| Accountable for the result | Not accountable for anything |

The model is a 4-bit quantised local model. It produces confident, well-written,
plausibly-argued findings that are sometimes simply wrong. Treat every finding as a
claim to check, never as an instruction to follow. Never edit code solely because
the reviewer said so — only because you checked and agree.

**Never** hand the reviewer authority: no "the local model says X so I'll do X". If
you cannot verify a finding against the actual code, say so and leave it alone.

## Invocation

`SCRIPTS=~/.claude/skills/ollama-reviewer/scripts`

```bash
python $SCRIPTS/cli.py status
python $SCRIPTS/cli.py review                          # uncommitted diff
python $SCRIPTS/cli.py review --ref main               # branch diff
python $SCRIPTS/cli.py review --staged                 # staged only
python $SCRIPTS/cli.py review --file a.py b.py         # explicit files
git diff | python $SCRIPTS/cli.py review --stdin       # piped
python $SCRIPTS/cli.py review --adversarial --file x.py
python $SCRIPTS/cli.py review --focus security,tests --file x.py
python $SCRIPTS/cli.py review --consensus --file x.py   # cross-check two models
python $SCRIPTS/cli.py review --instructions "focus on the retry loop" --file x.py
python $SCRIPTS/cli.py review --json                   # machine-readable
python $SCRIPTS/selftest.py                            # verify setup
```

Options: `--model`, `--temperature`, `--timeout`, `--debug`, `--cwd`.
Focus areas: `logic`, `security`, `performance`, `edge-cases`, `tests`, `design`.

Exit codes: `0` ran · `2` bad input · `3` Ollama unavailable · `4` timeout · `5` internal.
**Findings never cause a non-zero exit** — the tool reports, you judge.

## When to invoke

On demand: when the user asks, or when you want a second opinion on
security-sensitive code, intricate logic, or a design you are unsure about.
This is not an automatic gate — do not run it after every trivial edit.

## Required workflow

1. **Implement first.** Finish your own design and code. Never ask the reviewer what to build.
2. **Run the reviewer** on the diff or the specific files.
3. **Evaluate every finding** against the real code. Confirm the triggering condition
   actually exists. A finding that names no reachable condition is noise.
4. **Apply only what you agree with.**
5. **Report the triage** to the user: what you accepted, what you rejected, and why.
   This step is mandatory — it is what keeps the reviewer honest and visible.

Reporting format:

> Local review (`qwen3-coder:30b`): 3 findings.
> **Accepted 1** — the SQL injection in `find_user` is real; parameterised it.
> **Rejected 2** — the "null deref" in `greet` misreads the guard on line 12,
> and the pagination claim describes intended behaviour.

## When the reviewer is unavailable

A failed review is a non-event, not a blocker. Report it in one line
(`Local review unavailable: Ollama is not running`) and carry on with your own work.
Never claim code was reviewed when the review did not run. Never let an Ollama
failure stall the task.

## Honest limits

- Misses as much as it catches. In testing it found a SQL injection, an unhandled
  `None`, and an off-by-one, but missed a `ZeroDivisionError` on the adjacent line.
- Sees only what it is sent. It cannot know your callers, tests, or invariants.
- A clean review is weak evidence, not proof. It never substitutes for tests.
