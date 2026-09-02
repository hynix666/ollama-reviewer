# ollama-reviewer

[![CI](https://github.com/hynix666/ollama-reviewer/actions/workflows/ci.yml/badge.svg)](https://github.com/hynix666/ollama-reviewer/actions/workflows/ci.yml)
[![License](https://img.shields.io/github/license/hynix666/ollama-reviewer?color=blue)](LICENSE)
![Last commit](https://img.shields.io/github/last-commit/hynix666/ollama-reviewer)
![Python](https://img.shields.io/badge/python-3.8%2B-3776AB?logo=python&logoColor=white)
![Dependencies](https://img.shields.io/badge/dependencies-none-2ea44f)
![Inference](https://img.shields.io/badge/inference-100%25%20local-ff6d00)
![Role](https://img.shields.io/badge/model%20role-advisory%20only-6f42c1)

A local Ollama model as a **code reviewer assistant** for Claude Code. It advises;
Claude decides. The model never edits files and never has final say.

Pure Python standard library — no pip install, no dependencies, no network beyond
your own Ollama server.

Tested on Python 3.14; uses no standard-library API newer than 3.7.

## Install

Clone into your skills directory, then link the commands:

```bash
git clone git@github.com:hynix666/ollama-reviewer.git ~/.claude/skills/ollama-reviewer
powershell -File ~/.claude/skills/ollama-reviewer/install.ps1     # Windows
bash ~/.claude/skills/ollama-reviewer/install.sh                  # macOS / Linux
```

The installer links `commands/` in this repo to `~/.claude/commands/ollama`, so the
repo stays the single source of truth — edit a command here and the slash command
changes with it. On Windows it uses a **directory junction**, which needs no
administrator rights; on macOS and Linux, a symlink.

Then verify:

```bash
python ~/.claude/skills/ollama-reviewer/scripts/selftest.py
```

37 checks should pass (34 without a running Ollama server). Add `--live` to also run real inference against a file with
deliberately planted defects, or `--offline` to skip the three checks that need a
running Ollama server — that is what CI runs, across Python 3.8–3.13 on Linux,
Windows and macOS.

## Commands

| Slash command | What it does |
|---|---|
| `/ollama:review` | Review the uncommitted diff, a ref, or named files |
| `/ollama:review-file` | Review specific paths |
| `/ollama:adversarial` | Assume the design is wrong; attack the assumptions |
| `/ollama:status` | Ollama health plus installed models |

The `ollama:` prefix comes from the directory name the installer links to
(`~/.claude/commands/ollama/`); Claude Code namespaces commands by folder.

## CLI

```bash
S=~/.claude/skills/ollama-reviewer/scripts

python $S/cli.py status                        # health + model list
python $S/cli.py review                        # uncommitted diff
python $S/cli.py review --staged               # staged only
python $S/cli.py review --ref main             # main...HEAD
python $S/cli.py review --file a.py b.py       # explicit files
git diff | python $S/cli.py review --stdin     # piped diff or pasted code

python $S/cli.py review --adversarial --file api.py
python $S/cli.py review --focus security,tests --file api.py
python $S/cli.py review --instructions "focus on the retry loop" --file api.py
python $S/cli.py review --models qwen3-coder:30b,gpt-oss:20b   # cross-check
python $S/cli.py review --consensus                            # models from config
python $S/cli.py review --model gpt-oss:20b --temperature 0.2 --timeout 300
python $S/cli.py review --all-files            # include Markdown and lockfiles
python $S/cli.py review --json                 # machine-readable
python $S/cli.py review --debug                # tracebacks and retry traces
```

Focus areas: `logic`, `security`, `performance`, `edge-cases`, `tests`, `design`.

**Markdown, plain text and lockfiles are skipped by default.** The prompt is
code-specific, so a model call spent on a README returns little and, on a shared
deadline, costs a real file its turn. Skips are listed by name in the output.
`--all-files` includes them; a diff containing nothing but prose reviews the
prose rather than failing.

**The timeout scales with the model count.** Two models get twice the budget,
since each re-reviews every chunk. An explicit `--timeout` is never overridden.
Bare model families work — `--model qwen3-coder` resolves to an installed tag.

## Configuration

`config.json` beside this README:

```json
{
  "base_url": "http://127.0.0.1:11434",
  "model": "qwen3-coder:30b",
  "fallback_models": ["gemma4:26b"],
  "consensus_models": ["qwen3-coder:30b", "gpt-oss:20b"],
  "temperature": 0.1,
  "timeout_s": 180,
  "max_file_chars": 60000,
  "max_files": 25,
  "allow_cloud_models": false
}
```

Environment overrides: `OLLAMA_HOST`, `OLLAMA_REVIEW_MODEL`, `OLLAMA_REVIEW_TIMEOUT`.

Cloud models (`*:cloud`) are refused unless `allow_cloud_models` is true. A bind
address like `0.0.0.0` is rewritten to loopback for connecting, since `0.0.0.0` is
not a valid destination on Windows.

## MCP server

The same engine is also exposed over MCP, for clients that prefer native tool
calls to shelling out. Register it in `~/.claude.json`:

```json
{
  "mcpServers": {
    "ollama-reviewer": {
      "command": "python",
      "args": ["<abs path>/skills/ollama-reviewer/scripts/mcp_server.py"]
    }
  }
}
```

Restart the client, and four tools appear: `ollama_review_file`,
`ollama_review_code`, `ollama_review_diff`, and `ollama_list_models`. Each
accepts `focus`, `adversarial`, `instructions`, `models` and `format`.

It speaks JSON-RPC 2.0 over stdio with no SDK dependency — the protocol surface
needed is small enough that adding a PyPI package to get it would cost more than
it saves. **Nothing but JSON-RPC frames may reach stdout**; diagnostics go to
stderr, since a stray print corrupts the stream and disconnects the client.

The CLI remains the primary interface: it works without a client restart and is
debuggable from a terminal.

## Multi-model consensus

Review with several models and see which findings they independently agree on:

```bash
python $S/cli.py review --consensus                            # models from config
python $S/cli.py review --models qwen3-coder:30b,gpt-oss:20b   # named explicitly
```

Findings are reconciled across models and tagged `agreed by ...` or `only ...`,
with corroborated ones sorted first. Where models disagree on how bad something
is, the report says so.

**Nothing is discarded.** Filtering to agreements only would buy precision with
recall, and recall is this reviewer's weak side — on a four-defect fixture a
single model caught three, and the defect that mattered most in dogfooding was
one no model raised. A lone finding is not thereby wrong; corroboration is a
prioritisation signal, not a verdict.

Pick models from *different families*. Their mistakes are then less correlated,
which is the entire point. Two runs cost roughly double the time of one.

Choose the second model deliberately: measured on the planted-defect fixture,
`qwen3-coder:30b` found 3, `gpt-oss:20b` 5, `qwen3.8:27b` 4 — and `gemma4:26b`
found **0**, returning valid, empty, useless JSON. A model that finds nothing
contributes nothing but latency.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | The review ran. Findings, or none, are both success. |
| 2 | Input error — bad paths, empty diff, not a repository |
| 3 | Ollama unavailable — server down, model missing, cloud blocked |
| 4 | Timeout |
| 5 | Internal error |

**Findings never produce a non-zero exit.** The script reports; you judge.

## Error playbook

| What you see | Cause | Fix |
|---|---|---|
| `Cannot reach the Ollama server` | Not running or wrong port | `ollama serve`; check `OLLAMA_HOST` |
| `did not respond to a health check` | Server hung or absent | Restart Ollama |
| `None of the candidate models are installed` | Model not pulled | `ollama pull <model>` — the message lists what you do have |
| `Ollama reports insufficient memory` | VRAM/RAM exhausted | `ollama ps`, `ollama stop <model>`, or use a smaller `--model` |
| `Model is still loading` | Cold start | Retried automatically with backoff |
| `Request exceeded the configured timeout` | Big input or slow model | Raise `--timeout`, fewer files, smaller model |
| `Ollama returned HTTP 4xx/5xx` | Bad request or server fault | 404 usually means a wrong model name |
| `returned non-JSON output` | Model emitted prose | Handled automatically by the parser tiers |
| `The diff is empty` | Nothing changed | Use `--file` or `--ref` |
| `Not a git repository` | Wrong directory | Use `--cwd` or `--file` |
| `Skipped <path> (binary...)` | Non-text input | Expected; binaries are never sent |
| `TRUNCATED` in output | File over `max_file_chars` | Raise the cap or review fewer files |
| `some input may have been dropped` | Prompt filled the context window | Review fewer files at once; the finding set may be incomplete |

Every error message names both the problem and the remedy. Nothing crashes the
Claude session; failures render as a Markdown "review unavailable" section or, with
`--json`, a structured `error` object.

## How each error class is handled

**Connectivity.** Health checks run on a short budget and classify a timeout as
*unreachable* rather than *slow*, because a server that cannot answer `/api/tags` in
five seconds is not there. Connection refused, DNS failure, and invalid bind
addresses all resolve to the same actionable message.

**Model availability.** Models are listed before any review runs, so a missing model
fails in a second rather than after a long timeout. The error names your installed
models. Without an explicit `--model`, the configured fallback chain is tried in order.

**Transient failures.** OOM, model-loading, HTTP 5xx, timeouts, and malformed
responses retry up to three times with exponential backoff. Non-transient errors —
missing model, blocked cloud model, 4xx — fail immediately rather than burning the
time budget.

**Malformed output.** Three tiers: schema-constrained decoding; on failure a
free-form retry with a tolerant parser that extracts fenced or embedded JSON; and
finally the raw text surfaced as a single `info` finding with `status: partial`.
Invalid severities and categories are normalised rather than rejected, so one bad
enum never discards a whole review.

**Input.** Missing paths, directories, binaries (by extension *and* null-byte probe),
empty files, empty diffs, and non-repositories are each rejected with their own
message. Oversized files are truncated with an explicit marker that tells the model
not to speculate about the omitted part. Diffs split at file boundaries, never
mid-function.

**Context window.** `num_ctx` is computed from the actual prompt size rather than
hardcoded, because Ollama silently discards anything beyond it — an undersized window
means the model reviews part of a file while reporting as though it saw all of it. If
the server reports it consumed nearly the whole window anyway, the review is marked
`partial` with a warning rather than presented as complete.

**Runtime.** A top-level handler converts any unexpected exception into a structured
internal error; tracebacks appear only under `--debug`. Ctrl-C exits cleanly.
Temporary files in the selftest are removed in `finally` blocks. All work is
foreground and bounded by a shared deadline, so nothing can hang indefinitely or
orphan a process.

## Example workflow

Claude has just implemented a token-refresh endpoint.

**1. Implement first.** Claude writes the code and its tests. The reviewer is never
asked what to build.

**2. Review the diff.**

```bash
python ~/.claude/skills/ollama-reviewer/scripts/cli.py review --focus security,edge-cases
```

**3. Triage.** Suppose three findings come back:

- *Refresh tokens are not rotated on use* — Claude checks: true, the handler reuses
  the token. **Accepted.**
- *Missing rate limiting* — real, but middleware already handles it two layers up.
  **Rejected**, with the reason.
- *Possible race on the token table* — Claude checks: the write is inside a
  transaction with `SELECT ... FOR UPDATE`. **Rejected**, misread.

**4. Apply only what survived.** Claude implements rotation. Nothing else changes.

**5. Report.**

> Local review (`qwen3-coder:30b`): 3 findings. Accepted 1 — refresh tokens weren't
> rotated on use; fixed. Rejected 2 — rate limiting is handled in middleware, and
> the race claim misses the `FOR UPDATE` lock.

**When the reviewer is unavailable**, step 2 prints "review unavailable" with a
remedy and exits 3. Claude reports one line — `Local review unavailable: Ollama is
not running` — and continues. The work is not blocked, and Claude never claims the
code was reviewed.

## Limits, honestly

Against a fixture with four planted defects, `qwen3-coder:30b` found the SQL
injection, the unhandled `None`, and the off-by-one — and missed a
`ZeroDivisionError` on an adjacent line. It sees only what it is sent: not your
callers, tests, or invariants. A clean review is weak evidence, not proof, and never
a substitute for tests.

## Security

See [SECURITY.md](SECURITY.md) for how to report a vulnerability, and for what the
tool does with your code: it is sent to whatever endpoint you configure (localhost
by default, cloud models refused unless you enable them), secrets inside reviewed
files are not redacted, and reviewer output should be treated as untrusted data
when the code under review is untrusted.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The short version: standard library only,
Python 3.8 is the floor, and nothing may give the model authority to decide or edit.

## License

[MIT](LICENSE) © 2026 hynix666
