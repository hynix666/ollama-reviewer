<!--
Delete any section that does not apply. A short PR with the right sections
filled in is better than a long one with everything half-answered.
-->

## What changed, and why

<!-- The "why" matters more than the "what" - the diff already shows the what.
This repository's history records rejected alternatives on purpose. -->

## How you verified it

<!-- Which command you ran, and what it printed. -->

- [ ] `python scripts/selftest.py` passes (or `--offline` if you have no Ollama server — say which)
- [ ] CI is green

## Constraints

The model advises; it never decides. See
[CONTRIBUTING.md](https://github.com/hynix666/ollama-reviewer/blob/main/CONTRIBUTING.md).

- [ ] Does not give the model a filesystem write path
- [ ] Does not let findings affect the exit code
- [ ] No new third-party dependency
- [ ] No syntax newer than Python 3.8 (no `match`, `X | Y` annotations, `dict | dict`, builtin generics)

---

### If you changed `prompts.py`

Prompt edits are the riskiest change here: `selftest.py` checks prompts are
well-formed, never that they are *good*. A worse prompt passes every check.

- [ ] Ran `python scripts/selftest.py --live` before and after

| | Defects caught (of 4) | False positives |
|---|---|---|
| Before | | |
| After | | |

Model used: <!-- e.g. qwen3-coder:30b -->

<!-- A change catching more real defects but also raising false positives is a
trade-off to discuss, not an automatic win. -->

### If you added an error kind

- [ ] Added to `RETRYABLE` in `ollama_client.py` *only* if retrying could plausibly succeed
- [ ] Mapped in `ERROR_EXIT` in `cli.py` if it needs a non-default exit code
- [ ] Row added to the error playbook in `README.md`
- [ ] `selftest.py` check proving it is produced and classified correctly
- [ ] Listed in `NEEDS_SERVER` if the check needs a live Ollama server

### If behaviour changed

- [ ] `README.md` updated
- [ ] `docs/` updated — documentation drift is treated as a defect
