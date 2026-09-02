# Local Ollama Reviewer — Design

**Date:** 2026-09-02
**Status:** Implemented and verified (37/37 self-checks passing)
**Location:** `~/.claude/skills/ollama-reviewer/`

---

## 1. Problem

The goal was a working protocol: Claude Code remains the primary agent and sole
decision-maker, while a locally hosted Ollama model acts strictly as a reviewer
assistant that advises and never decides.

Investigation showed the protocol could not be honoured as stated. The Ollama server
was running and well-stocked — 24 models including `qwen3-coder:30b` and
`gemma4:26b` — but **no path existed from Claude to it**:

| Assumed to exist | Actual state |
|---|---|
| `/ollama:review`, `/ollama:adversarial-review` | No such plugin installed |
| `ollama_review_code`, `ollama_review_file` MCP tools | Not configured; only GitKraken and a failing MCP_DOCKER |
| Ollama server | Running, 24 models, warm (0.7s round trip) |

The reviewer engine existed; the invocation path did not. Adopting the protocol
without building that path would have made it a silent no-op — Claude would claim to
follow a review protocol it had no mechanism to execute.

## 2. Goals and non-goals

**Goals**

- A reachable, reliable invocation path from Claude to the local model.
- Structured findings: severity, category, location, explanation, suggested fix.
- Production-grade error handling — every failure typed, actionable, and non-fatal.
- Steerable prompts (focus areas, free-text direction, adversarial mode).
- Simple installation; no third-party dependencies.
- Role separation enforced **mechanically**, not merely documented.

**Non-goals**

- The model editing files or making decisions. Never, under any configuration.
- Automatic gating of every change (see D7).
- Background job management (see D8).
- Cloud inference. Blocked by default.
- Replacing tests. A clean review is weak evidence, not proof.

## 3. Core constraint: role separation

| Claude | Ollama model |
|---|---|
| Owns architecture, implementation, final decisions | Offers opinions on code it is shown |
| Writes and edits all files | Never edits anything |
| Verifies every finding before acting | Cannot verify its own claims |
| Accountable for the result | Not accountable |

This is enforced by four mechanisms, not by good intentions:

1. **The tool only reads.** It has no write path to the filesystem. There is no
   configuration in which the model can modify code.
2. **Findings never produce a failing exit code.** Exit codes describe tool health
   (`0` ran, `2` bad input, `3` unavailable, `4` timeout, `5` internal). If findings
   could fail the command, the model's opinion would become a gate — inverting the
   authority the design exists to protect.
3. **The prompt demands falsifiable claims.** Every finding must name the concrete
   condition that triggers it, which makes verification possible and makes
   unverifiable assertions visibly deficient.
4. **The skill mandates triage reporting.** After every review Claude must state what
   it accepted, what it rejected, and why. Silent acceptance is the failure mode this
   guards against.

## 4. Decisions

### D1 — CLI engine with skill and slash-command wrappers, not an MCP server

The original request framed the choice as "skill + slash command **or** MCP server."
That framing does not hold: **in Claude Code, slash commands are Markdown prompt
files, not executables.** A slash command cannot run code; it expands into
instructions. So an engine script is required either way, and the real question is
how Claude reaches it.

Chosen: a Python CLI, wrapped by a skill (protocol knowledge) and four slash commands
(ergonomic entry points).

*Rejected — MCP server.* It would provide native structured tool calls, but requires
a `~/.claude.json` edit plus a session restart before its tools register. That means
it could not review its own implementation, and would leave dead commands until
restart. The CLI works immediately and is independently runnable from a terminal,
which also makes it debuggable without Claude in the loop. An MCP server remains an
easy later wrapper around the same engine — the engine already emits JSON.

### D2 — Three-tier output degradation

Ollama supports JSON-schema-constrained decoding, but on a 4-bit quantised model
constrained decoding sometimes degrades reasoning or stalls outright. Rather than
depend on it:

1. **Schema-constrained request.** The normal path.
2. **Free-form retry with tolerant parsing** on schema violation, empty output, or
   truncation. The parser tries strict JSON, then a fenced ```json block, then the
   first balanced `{...}` region (string-aware, so braces inside strings do not
   confuse it).
3. **Raw passthrough.** Unparseable output becomes a single `info` finding with
   `status: "partial"`, preserving the reviewer's thinking rather than discarding it.

Invalid severities and categories are normalised (to `info` and `logic`) rather than
rejected, so one bad enum value never discards an otherwise useful review.

### D3 — File-level batching, not character chunking

Splitting a large diff at a character boundary cuts functions in half, and a model
shown half a function reports confident findings about code it cannot see. Inputs are
therefore split at **file** boundaries; diffs are parsed on `diff --git` headers.
Oversized single files are truncated with an explicit marker instructing the model not
to speculate about the omitted portion.

### D4 — Findings never fail the command

See §3, mechanism 2. This is the load-bearing decision of the whole design.

### D5 — Health-probe timeouts classify as `unreachable`, not `timeout`

Discovered during testing. A server that cannot answer `/api/tags` within a five-second
budget is not present; reporting "timeout" would send the user to tune settings when
the actual remedy is `ollama serve`. Liveness probes and work requests are classified
separately.

### D6 — Bind addresses rewritten to loopback

Discovered during testing. This machine's `OLLAMA_HOST` is `0.0.0.0:11434` — a valid
*bind* address so the server listens on all interfaces, but not a valid *connect*
address. Connecting to it fails on Windows with `WinError 10049`. The client rewrites
`0.0.0.0` → `127.0.0.1` and `[::]` → `[::1]`, and notes the rewrite in its output so
the behaviour is visible rather than magic.

### D7 — On-demand triggering, not automatic gating

The original protocol required a review before any non-trivial change could be
considered complete. When asked directly, the user chose **on demand only**. This
relaxes the written protocol and is recorded here deliberately so the deviation is not
mistaken for an oversight: the tool is available and cheap to reach, but is not a
mandatory checkpoint.

### D8 — Background jobs and cancellation cut from scope

The original spec asked for `--background`, a job directory, and `/ollama-cancel`.
Cut, because Claude Code's Bash tool already provides backgrounding and cancellation,
and a PID-file job system would duplicate that machinery while introducing its classic
failure modes — orphaned processes and stale lock files. All work is foreground and
bounded by a shared deadline, so nothing can hang indefinitely. Four slash commands
instead of five.

### D9 — Context window sized from the prompt

Found while writing this document. `num_ctx` was hardcoded to 16384 while
`max_file_chars` allowed 60000 characters — roughly 20,900 tokens. Ollama silently
drops whatever exceeds `num_ctx`, so a large file would have been reviewed only in
part while the output reported confidently on the whole. This is the worst class of
defect available here: silent, and it manufactures false confidence.

`num_ctx` is now computed from actual prompt length (pessimistic 3.0 chars/token,
2048 tokens reserved for the answer, clamped to 8192–65536). Additionally, when the
server reports `prompt_eval_count` close to the limit, the review is marked
`partial` with an explicit warning. Two regression tests guard the invariant.

### D10 — Commands live in the repo, linked out by directory junction

The four slash commands must sit under `~/.claude/commands/` to be discovered, but
belong in the repository so a clone is self-contained. Linking rather than copying
keeps one source of truth.

*Rejected — file symlinks.* Windows requires Developer Mode or an elevated shell to
create them, and requesting administrator rights to install a code-review helper is
disproportionate.

*Rejected — hardlinks.* They work unprivileged, but git replaces files rather than
editing them in place, so a `checkout` or `pull` silently severs the link. Silent
breakage is worse than no link.

Chosen: a **directory junction** (`mklink /J`) on Windows, a symlink on POSIX. Both
resolve a path rather than an inode, so they survive git operations. Junctions need
no elevation.

The consequence is a rename: Claude Code namespaces commands by folder, so linking
the directory as `~/.claude/commands/ollama` yields `/ollama:review`,
`/ollama:review-file`, `/ollama:adversarial`, and `/ollama:status` rather than the
original flat `/ollama-review` form. This happens to match the naming in the
original protocol that motivated the project.

### D11 — Consensus annotates; it does not filter

The original sketch in this document proposed reporting only findings that two
models both raise. Implementation reversed that.

Filtering to agreements buys precision by spending recall, and recall is this
reviewer's weak side: against the four-defect fixture a single model caught
three, and the defect that mattered most during dogfooding — the silent context
truncation in D9 — was raised by no model at all. Discarding lone findings would
remove exactly the class of result that is scarce.

So `--models a,b` and `--consensus` run every model over every chunk and
reconcile the results, tagging each finding with which models produced it and
sorting corroborated ones first. Nothing is dropped. Where models disagree on
severity, the spread is reported rather than silently resolved.

Matching is structural and deterministic — same category, same file, then line
proximity, symbol overlap, or `difflib` similarity on the issue text. A third
model adjudicating was rejected: it would add latency, a new failure mode, and
another unverifiable judgment to the pipeline.

A dead model drops out mid-run rather than aborting the review; the survivors
finish and the report names what was lost.

**Model choice matters more than the mechanism.** Measured on the fixture:
`gpt-oss:20b` 5 findings, `qwen3.8:27b` 4, `qwen3-coder:30b` 3, and
`gemma4:26b` **0** — valid, empty, useless JSON. The first configured default
paired qwen3-coder with gemma4 and produced zero corroboration by construction;
it was replaced with gpt-oss after measurement. Prefer models from different
families, so their errors are less correlated.

### D12 — MCP server as a second front end, not a replacement

D1 rejected MCP as the *primary* interface because it needs a client restart
before its tools register, which would have left the tool unusable while it was
being built. That objection was about sequencing, not about MCP, so the server
now exists alongside the CLI rather than instead of it.

It speaks JSON-RPC 2.0 over stdio directly. The official MCP SDK is a PyPI
package, and the zero-dependency rule is worth more here than the few hundred
lines it would save: the surface actually required is `initialize`,
`tools/list`, `tools/call` and `ping`.

Four tools — `ollama_review_file`, `ollama_review_code`, `ollama_review_diff`,
`ollama_list_models`. The first three names come from the protocol that
originally motivated the project.

The refactor in the previous change is what made this cheap. Because
`review.run_review` takes a `ReviewOptions` rather than an argparse `Namespace`
and returns a dict rather than printing, the server calls the engine in-process
and formats the return value. Had the orchestration still lived inside
`cmd_review`, this adapter would have had to shell out and parse its own CLI's
output.

**The stdout discipline is the sharp edge.** A stdio MCP server may emit nothing
but JSON-RPC frames; one stray `print` corrupts the stream and the client
disconnects. Diagnostics therefore go to stderr, and a selftest check drives the
protocol directly to keep that honest.

Tool descriptions repeat that findings are advisory and must be verified — a
check enforces this, because an MCP client sees only the description, never
`SKILL.md`, and the role separation must survive that context loss.

### D13 — Prose skipped by default; the deadline scales with model count

Both changes come from running the tool on a real diff of this repository rather
than on the fixture.

Three of six chunks were Markdown. The reviewer spent roughly half a shared
deadline analysing documentation with a prompt that asks for logic, security and
performance defects, and `selftest.py` was never reviewed by either model
because the budget ran out first.

Markdown, plain text and lockfiles are therefore skipped by default rather than
behind an opt-in flag: the run that exposed the problem would not have set an
opt-in flag, because the waste was invisible until measured. Skipped files are
listed by name in the output, so the filter is visible rather than silent, and
`--all-files` restores the old behaviour. A diff containing nothing but prose
reviews the prose instead of failing — the filter yields rather than blocking.

Separately, the default 180s budget was sized for one model but shared across
all of them, so consensus starved the later files. It now multiplies by the
model count unless the user passed `--timeout`, in which case their number is
respected exactly.

## 5. Architecture

```
  ~/.claude/commands/ollama  --junction-->  <repo>/commands/*.md
            |                                prompt files -> tell Claude what to run
            v
  SKILL.md  protocol: roles, triage duty, honest limits
            |
            v
  cli.py    orchestration, exit codes, top-level exception barrier
     |
     +-- collect.py        git diff / files / stdin -> validated chunks
     +-- prompts.py        system + user prompts, response schema
     +-- ollama_client.py  HTTP, error taxonomy, retries, context sizing
     +-- render.py         tolerant parsing + Markdown rendering
     +-- review.py         orchestration: models over chunks, result assembly
     +-- mcp_server.py     MCP stdio server over the same engine
     +-- selftest.py       37 checks, all error paths
     +-- consensus.py      cross-model reconciliation of findings
```

Six modules, ~1,700 lines, Python standard library only. Each module has one
responsibility and can be read whole.

**Data flow:** source selection → validation and capping → per-chunk prompt
construction → model call with retries → tiered parsing → normalisation and merge →
Markdown or JSON.

All chunks share one deadline. When the budget runs out, remaining chunks are
recorded as skipped rather than silently dropped — the report distinguishes "reviewed
and clean" from "never reviewed."

## 6. Error taxonomy

| Kind | Detection | Retryable | Exit | User sees |
|---|---|---|---|---|
| `unreachable` | Connect refused, DNS, health timeout | no | 3 | "Start it with: ollama serve" |
| `model_missing` | Absent from `/api/tags` | no | 3 | `ollama pull <name>` + list of installed models |
| `cloud_blocked` | `*:cloud` while disabled | no | 3 | How to enable in config |
| `oom` | HTTP 500 + body match | yes | 3 | `ollama ps` / `ollama stop` / smaller model |
| `loading` | HTTP 500 + body match | yes | — | Retried automatically |
| `http_4xx` | Status < 500 | no | 2 | Status + safe body excerpt |
| `http_5xx` | Status ≥ 500 | yes | 2 | Status + safe body excerpt |
| `timeout` | Socket timeout on work request | yes | 4 | Raise `--timeout`, fewer files, smaller model |
| `malformed` | Empty or non-JSON body | yes | 2 | Handled by parser tiers; URL included |
| `input` | Path, binary, empty, or git failure | no | 2 | Names each rejection and its reason |
| `internal` | Unexpected exception | no | 5 | One line; traceback only under `--debug` |

Transient classes retry up to three times with exponential backoff (base 1.5).
Non-transient classes fail immediately rather than burning the time budget.

Every message carries both a problem statement and a remedy. Nothing crashes the
Claude session: failures render as a Markdown "review unavailable" section or, with
`--json`, a structured `error` object. The "unavailable" text states explicitly that
nothing was verified, so a failed review can never be mistaken for a clean one.

## 7. Input handling

**Sources:** uncommitted diff (default), `--staged`, `--ref REF` (`REF...HEAD`),
`--file PATH...`, `--stdin` (auto-detects whether piped input is a diff).

**Rejections, each with its own message:** missing paths, directories, binaries (by
extension *and* null-byte probe), empty files, empty diffs, non-repositories,
permission errors, unknown revisions.

**Caps:** 60,000 chars per file, 180,000 total, 25 files. Exceeding a cap produces an
explicit warning naming what was dropped — never a silent omission.

## 8. Prompt design

The system prompt's effectiveness rests on three framings, each chosen against a
known failure mode of small quantised models:

- **"You are ONE reviewer among several"** whose output will be independently
  verified and discarded if unjustified. This reduces padding and flattery.
- **Explicit permission to return zero findings** ("Returning zero findings is a valid
  and frequently correct answer"). Without it, small models invent problems to appear
  useful.
- **The evidence rule:** report a finding only when you can name the concrete
  condition under which the code misbehaves. This is the main defence against
  confident nonsense, and it is what makes verification tractable — a finding whose
  stated trigger does not exist in the code is self-refuting.

The prompt also forbids restating what the code does, praise, and style commentary,
and requires a precise location anchor (never "various" or "throughout").

Adversarial mode adds a block instructing the model to assume the design is wrong and
to attack assumptions rather than syntax — concurrency, hostile input, partial
failure, restart mid-operation, 100× scale — while explicitly keeping the evidence
rule in force.

## 9. Testing

`selftest.py` runs 37 checks, 34 of them with no inference required: configuration loading,
connectivity, model resolution (including bare family names), all six error classes,
input rejections, truncation, the three parser tiers, context sizing, and render
safety. `--live` additionally reviews a fixture containing planted defects.

The suite proved its worth immediately by catching two real bugs in the
implementation on its first run: the `0.0.0.0` connect failure (D6) and the health
timeout misclassification (D5).

## 10. Measured signal quality

This section exists because the design depends on an honest estimate of how good the
reviewer actually is.

**Planted-defect fixture (4 defects):** caught the SQL injection, the unhandled
`None`, and the off-by-one. **Missed a `ZeroDivisionError` on a line it did comment
on** — it described the pagination bug in `last_page` without noticing that
`per_page=0` crashes the same expression. It found that same class of bug instantly
when the function was isolated, which suggests attention dilutes across a file rather
than the capability being absent.

**Dogfooding (reviewing its own source, 9 findings):**

| Verdict | Count | Examples |
|---|---|---|
| Accepted, modified | 2 | Missing request context in malformed-response errors; a blank configured model printing `ollama pull None` |
| Rejected | 7 | Claimed a 404 was misclassified (it inverted the ternary); claimed a variable was not reassigned (it is, two lines above); claimed integer overflow (Python ints are arbitrary-precision) |

**Roughly a 2-in-9 survival rate.** Notably, the highest-severity finding it produced
(a "HIGH" 404 misclassification) was a pure hallucination, while the defect that
actually mattered most — D9's silent context truncation — was found by a human
reading the config, not by the reviewer at all.

The implication is structural, not incidental: **triage is mandatory, and severity
labels carry no authority.** This is why §3's reporting requirement exists.

## 11. Future work

- **MCP wrapper.** Implemented — see D12. The CLI remains primary.
- **Background mode.** Cut in D8. The seams (chunk labels, shared deadline) would
  accommodate it without rework should long reviews become common.
- **Multi-model consensus.** Implemented — see D11, which reverses this section's
  original "report only findings both raise" proposal in favour of annotating
  corroboration without discarding anything.

## 12. Known risks

- **Chars-per-token is an estimate.** The 3.0 ratio is pessimistic for English but
  code with long identifiers can tokenize denser. The `prompt_eval_count` check is
  the backstop; it warns rather than prevents.
- **The dead `--staged` fallback.** In `collect.py`, falling back to `git diff
  --staged` after an empty `git diff HEAD` is unreachable, since `HEAD` already
  includes staged changes. Inert, left in place, noted here rather than silently
  carried.
- **Not under version control.** `~/.claude` is not a git repository, and
  initialising one would begin tracking credentials and unrelated configuration.
  These files are currently unversioned by deliberate omission.
