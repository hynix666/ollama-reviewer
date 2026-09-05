# Contributing

Thanks for looking at this. Before anything else, please read the one constraint
that shapes every other rule here.

## The constraint: the model advises, it never decides

This tool exists to give a coding agent a second opinion, not a second author. A
local quantised model produces confident, well-argued findings that are wrong a
substantial fraction of the time — when this tool reviewed its own source, 2 of 9
findings survived verification, and its highest-severity finding that run was a
hallucination.

Four mechanisms keep that in its place. **A change that weakens any of them will be
declined, however convenient it looks:**

1. **The tool has no filesystem write path.** It reads code and prints findings.
   There is no flag, config key, or mode in which the model edits anything.
2. **Findings never set a failing exit code.** Exit codes describe tool health only
   (`0` ran, `2` bad input, `3` unavailable, `4` timeout, `5` internal). If findings
   could fail the command, the model's opinion becomes a gate — which inverts the
   authority this design exists to protect.
3. **Every finding must name a falsifiable trigger.** The prompt requires the
   concrete condition under which the code misbehaves. This is what makes a finding
   checkable, and what makes a bad one visibly deficient.
4. **The skill mandates triage reporting.** The agent must state what it accepted,
   what it rejected, and why. Silent acceptance is the failure mode.

If you find yourself wanting `--auto-fix`, that is a different project.

## Setup

```bash
git clone https://github.com/hynix666/ollama-reviewer.git
cd ollama-reviewer
python scripts/selftest.py
```

No install step, no virtualenv, no dependencies. You need Python 3.8+ and, for the
full suite, a running Ollama server with at least one code-capable model.

## Running the tests

```bash
python scripts/selftest.py            # all 54 checks (needs Ollama running)
python scripts/selftest.py --offline  # 51 checks, no server needed - what CI runs
python scripts/selftest.py --live     # adds real inference on planted defects
```

CI runs `--offline` across Python 3.8–3.13 on Linux, Windows and macOS, plus a job
that syntax-checks both installers. All jobs must pass before a merge.

## Two hard rules

**Standard library only.** No `requests`, no `pydantic`, nothing from PyPI. The tool
installs by cloning, and adding a dependency breaks that. `urllib.request` is
verbose but sufficient.

**Python 3.8 is the floor.** CI enforces it, so these will fail the build rather
than slip through:

| Avoid | Since | Use instead |
|---|---|---|
| `dict_a \| dict_b` | 3.9 | `dict(a, **b)` |
| `list[str]`, `dict[str, int]` annotations | 3.9 | `typing.List`, or no annotation |
| `str.removeprefix` / `removesuffix` | 3.9 | slicing |
| `match` statements | 3.10 | `if`/`elif` |
| `int \| None` annotations | 3.10 | `typing.Optional` |

f-strings and the walrus operator are fine on 3.8, but the existing code uses `%`
formatting throughout — match the file you are editing rather than mixing styles.

## Adding an error case

Every failure must be typed, and every typed failure must tell the user how to fix
it. Bare exceptions escaping to the caller are a bug.

```python
raise OllamaError(
    kind="model_missing",        # machine-readable; drives retry and exit code
    detail="What went wrong, specifically.",
    remedy="The exact command or action that fixes it.",
)
```

Add the `kind` to `RETRYABLE` in `ollama_client.py` only if retrying could plausibly
succeed — a missing model will not appear on its own, and retrying it just burns the
time budget. Map it in `ERROR_EXIT` in `cli.py` if it needs a non-default exit code,
and add a row to the error playbook in `README.md`.

Then add a check to `selftest.py` proving the error is produced and classified
correctly. Checks needing a live server must be listed in `NEEDS_SERVER` so
`--offline` skips them.

## Changing the prompts

This is the highest-risk change in the repository and the one least covered by
tests. `selftest.py` verifies prompts are well-formed; nothing verifies they are
*good*. A prompt edit can quietly make the reviewer worse while every check passes.

Before and after any edit to `prompts.py`, run the planted-defect fixture:

```bash
python scripts/selftest.py --live
```

The fixture contains a SQL injection, an off-by-one, an unhandled `None`, and a
`ZeroDivisionError`. Report in your PR how many the reviewer caught before and after,
on which model. A change that catches more real defects but also raises the
false-positive rate is a trade-off worth discussing explicitly, not a clear win.

Resist making the prompt more encouraging. The framings that matter most — "you are
one reviewer among several", explicit permission to return zero findings, and the
evidence rule — exist because small quantised models otherwise pad their output to
appear useful.

## Where things live

| Path | Responsibility |
|---|---|
| `scripts/ollama_client.py` | HTTP, error taxonomy, retries, context sizing |
| `scripts/collect.py` | Input gathering and validation (git, files, stdin) |
| `scripts/prompts.py` | System and user prompts, response schema |
| `scripts/review.py` | Review orchestration: models over chunks, result assembly |
| `scripts/mcp_server.py` | MCP stdio server (JSON-RPC 2.0, no SDK dependency) |
| `scripts/consensus.py` | Cross-model reconciliation of findings |
| `scripts/render.py` | Tolerant parsing and Markdown rendering |
| `scripts/cli.py` | Orchestration, exit codes, exception barrier |
| `scripts/selftest.py` | Verification, including deliberate error paths |
| `commands/` | Slash commands, linked into `~/.claude/commands/ollama` |
| `docs/` | Design document, including rejected alternatives |

Keep modules focused. If one grows past roughly 400 lines it is probably doing two
jobs - `selftest.py` enforces this, so a breach fails the build rather than
drifting. `selftest.py` itself is exempt; test files grow with coverage.

## Pull requests

- One concern per PR.
- `python scripts/selftest.py` passes locally, and CI is green.
- Explain the *why* in the commit message. The history here records rejected
  alternatives on purpose — a future reader should learn why hardlinks were not used
  for the command linking without having to rediscover it.
- Update `README.md` and `docs/` when behaviour changes. Documentation drift is
  treated as a defect.

## Code of conduct

Participation is governed by the [Code of Conduct](CODE_OF_CONDUCT.md).
Reports go through GitHub's private reporting form, since this project publishes
no contact email.

## Reporting bugs

Include the output of:

```bash
python scripts/cli.py status
python scripts/cli.py review --file <path> --debug
```

`--debug` adds tracebacks and the retry trace. Check the output before pasting it:
it contains file paths and your model list, though never credentials.
