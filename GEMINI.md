# Data Pipeline Repo — Agent Instructions

This repo runs automated data pipelines (transcripts, financials, web scraping). Loads on top of the global `GEMINI.md`.

LLMs are probabilistic, business logic is deterministic. This 3-layer architecture forces deterministic logic into code, leaving the LLM to handle routing and synthesis.

## The 3-Layer Architecture

### Layer 1: Directive (Intent & Parameters)

- **Location**: `directives/` (Markdown files, one per task type)
- **Function**: Defines goals, required inputs, authorized tools, expected outputs, schemas, constraints, and known edge cases (API limits, rate limits, fallback behavior)
- **Rule**: Immutable baseline for a task. Do not modify without explicit user authorization.
- **Each directive must specify**: target source, output schema, refresh cadence, idempotency key, rate-limit budget, failure-mode policy.

### Layer 2: Orchestration (Routing & Decision)

- **Function**: Core agent operating layer. Read directives, verify inputs, sequence `execution/` script calls, process stdout/stderr, hand off to next step.
- **Rule**: Do not implement business logic in this layer. If you find yourself writing transformations in the agent's reasoning, that logic belongs in an `execution/` script.
- **State management**: On multi-step failure, check `.tmp/` for intermediate state and resume from the last successful checkpoint. Do not restart from step one unless data integrity requires it.

### Layer 3: Execution (Deterministic Action)

- **Location**: `execution/` (Python scripts)
- **Function**: All network requests, data transformations, file I/O, schema validation.
- **Rules**:
  - Each script is a single-purpose CLI entrypoint with typed arguments (use `typer` or `argparse` with explicit types).
  - All inputs and outputs validated with Pydantic models. No `dict[str, Any]` flowing between scripts.
  - Outputs >2,000 lines or >100KB written to `.tmp/`, with only file paths and a summary returned to stdout.
  - Scripts are idempotent: rerunning with the same inputs produces the same outputs (or a clear "already done" exit).
  - Scripts log structured events (one JSON line per event) to stderr. Never mix logs and data on stdout.

## Operating Principles

### Tool Prioritization

- Query `execution/` for an existing script before writing new code. Most pipelines reuse the same primitives (HTTP client, HTML parser, transcript fetcher, financial-data adapter, S3/Drive uploader).
- If a new script is genuinely needed, it goes in `execution/` — never inline in agent reasoning.

### Bounded Self-Annealing

- **Trigger**: An execution script returns a non-zero exit code or raises.
- **Action**: Read the stack trace and stderr, identify the fix, edit the script, re-run.
- **Circuit breaker**: 3 consecutive retries max per task. On the 4th failure, halt and request user intervention with: the script, the inputs, the full error, the changes attempted.
- **Distinguish error classes** before retrying:
  - **Transient** (network, 5xx, rate limit): retry with backoff is appropriate.
  - **Schema/contract** (4xx, parse error, missing field): retry without code change is wrong. Fix the script first.
  - **Auth** (401, 403): halt immediately, surface to user.

### Directive Maintenance

- Directives must be refined, not bloated. When you learn something new (an API quirk, a rate limit, a date format), update the directive — but consolidate, don't append endlessly.
- Request permission before committing changes to anything in `directives/`.

## File Organization & State

### Categories

- **Deliverables**: Push to canonical destinations (Google Drive, Sheets, S3, Postgres). Never to `.tmp/`.
- **Intermediates**: Write exclusively to `.tmp/`. Safe to wipe.
- **Cached responses**: Optional, in `.cache/` if implemented. Always include a TTL or invalidation rule.

### Directory Structure

- `directives/` — version-controlled SOPs, one file per task type
- `execution/` — deterministic Python tools, single-purpose CLIs
- `.tmp/` — ephemeral state (raw scrape data, JSON dumps, pagination cursors, partial results). Gitignored.
- `.cache/` — optional response cache. Gitignored.
- `output/` or cloud destination — final deliverables

### State & Idempotency

- Every pipeline run must have a deterministic idempotency key (e.g., `{source}_{ticker}_{period}_{run_date}`).
- Before executing, check whether the deliverable for that key already exists. Skip with a logged "already done" if so, unless `--force` is passed.
- Pagination state, partial scrapes, and multi-page reports check pointed in `.tmp/<task_id>/state.json` so resumption is exact.

## Data Pipeline Specifics

### Network & Scraping

- Default to a configured `requests.Session` with a sane User-Agent, timeout (10s connect, 30s read), and retry adapter for 5xx/429.
- Respect `robots.txt` and any documented rate limits. Build the rate-limit budget into the directive.
- Never scrape behind authenticated sessions unless the directive explicitly authorizes it and credentials are properly loaded.
- For JS-rendered pages, prefer the underlying API (check the network tab) over headless browser automation. Headless is a last resort.

### Financial Data

- Always validate that data falls within plausible ranges before persisting. A revenue figure 10x off is more likely a unit error (cents vs dollars, thousands vs millions) than a real outlier. Halt and surface.
- Currency must be explicit on every numeric field. No "assumed USD."
- Date ranges must be explicit (period start, period end, fiscal vs calendar). No "last quarter."
- When pulling from multiple sources, log source provenance per field. Don't silently merge.

### Transcripts

- Speaker attribution must be preserved. Never strip or collapse speaker tags.
- Time codes (if available) preserved.
- Source URL and pull timestamp stored alongside transcript text.

### Schema Drift Defense

- Every parsed response validated against a Pydantic model. On validation error, halt and dump the raw response to `.tmp/` for inspection. Do not attempt a guess-fix in the agent loop — the schema changed, the directive needs updating.

## Security

- Credentials live in `.env`, `credentials.json`, `token.json`. Never log, output, or commit these (also in global rules).
- API keys passed to scripts via environment variables only — never as CLI args (they leak into shell history and process lists).
- Secrets used in URL query strings get redacted in any logged output.

## Session & Agent Model Selection (Token Discipline)

When proposing or spawning work that spans multiple sessions/agents (chips, worktree sessions, subagents, scheduled agents), state a recommended model **per session**, chosen by task nature — never one global default. Any plan that proposes N sessions must list the model next to each session.

- **Opus-class** (Claude Opus 4.8 / Fable 5; Gemini Ultra-tier) — architecture or design under ambiguity, writing directives/plans for multi-session tracks, financial-judgment calibration (e.g. DCF/SOTP assumptions), prompt & eval-rubric design, anything where a wrong early decision is expensive to unwind. Few sessions should need this.
- **Sonnet-class** (Claude Sonnet 4.6; Gemini Pro-tier) — the default for implementation: features with a directive/spec and acceptance criteria, refactors behind tests, bugfixes, pipeline/schema work. When unsure, pick Sonnet.
- **Haiku-class** (Claude Haiku 4.5; Gemini Flash-tier) — mechanical work with no judgment: renames, formatting/checklist sweeps, fixed-recipe backfills, high-volume classification. Also the default tier for mechanical subagent fan-out (search, verification sweeps) inside any session.

Rules of thumb: a directive with acceptance criteria exists → Sonnet executes it; writing the directive itself → Opus-class. Escalate a session's model only after it actually fails on quality, not preemptively.

Scope note: this governs **coding/session** model choice. In-app per-purpose LLM routing is governed separately by `LLM_MODELS` in `src/llm/cli.py` plus the model-downgrade eval loop (`directives/model_eval_loop.md`).


# Backend Development Guidelines — Full Reference

General-purpose rules for backend code. Examples are in Python/Pydantic but the principles port directly to TypeScript, Go, Rust, etc. Apply retroactively — update existing code to comply as you touch it.

A short "always-load" subset lives in `BACKEND_CORE.md`.

## Core Principles

**Engineering bar.** When planning a feature, fixing a bug, or making any change — no matter how small — approach it as a senior principal engineer would. Think through the full picture before writing a single line: the data model, the API boundaries, the failure modes, the edge cases, the naming, the file organization, the testability. The architecture should be obvious to anyone reading it for the first time. The code should be minimal, precise, and self-explanatory — no over-engineering, no under-engineering. Every abstraction must earn its existence. Every function, module, and file should have a clear reason to exist and a clear place in the structure. If something feels hacky or bolted-on, redesign it. The bar is: would this pass a rigorous code review from the best engineer you know?

**Branching.** Do not switch to a new branch unless explicitly instructed.

**Multi-agent coordination.** Other agents or developers may be making changes in the same repos concurrently. Do not touch or revert unrelated changes you encounter — just continue with your own work. If those changes directly conflict with yours, ask before resolving conflicts.

**Respect existing edits.** Assume any unexplained changes already in the tree were made intentionally. Do not "clean them up" without being asked.

## Code Standards

### NEVER

- Inline imports — all imports at the top of each module
- `getattr` gymnastics, permissive fallbacks, or defensive patterns that hide bugs
- `try/except pass` (or equivalent) — never silence errors
- `cast`, purposeless `isinstance` checks, or assert-driven type coercions
- Type-error suppression directives (`# noqa`, `@ts-ignore`, etc.) unless explicitly instructed
- `Any` / `unknown` / `interface{}` as a type — model types precisely
- Keyword or substring matching to classify responses, detect intent, or branch logic
- Magic strings or constants sprinkled through code — define enums or module-level constants
- Silent fallbacks on unexpected input — let it raise

### ALWAYS

- Strong typing enforced by the strictest typechecker available — if a type is wrong, fix the annotations, don't escape them
- Schema-validated models (Pydantic, Zod, Go structs with validators) for structured data, request/response payloads, and configuration surfaces
- Functions under ~80 lines — break large conditionals into helpers
- DRY — extract shared logic instead of copy/pasting
- Fail loudly — raise clear exceptions when invariants are violated
- Docstrings on every non-trivial module and class
- Direct attribute access and explicit failures over permissive fallbacks

### Type Discipline — Examples

❌ **BAD**:
```python
def resolve_ticket(data: dict[str, Any]) -> dict[str, Any]: ...
def get_config(settings: dict[str, dict[str, list[Any]]]): ...
```

✅ **GOOD**:
```python
class TicketResolution(BaseModel):
    ticket_id: str
    status: ResolutionStatus
    actions_taken: list[ActionRecord]

def resolve_ticket(data: TicketInput) -> TicketResolution: ...
```

### Classification & Intent Detection — Examples

❌ **BAD**:
```python
if "password" in ticket.subject.lower():
    category = "password_reset"
if "confirmed" in human_message:
    proceed = True
```

✅ **GOOD**: Use structured outputs, enums, or model-driven classification — never substring matching.

## Testing & Quality Assurance

- Scripts intended for CLI use must rely on repo-relative paths so they work from the project root.
- **Never** write tests that assert on exact prompt wording, template labels, or sentence substrings (e.g., `assert "Current technician message:" in prompt`). Prompts and copy change constantly — these tests break immediately and provide zero value. Instead, test structural properties: output is non-empty, deterministic, differs between modes, includes user-supplied input values, respects length constraints, etc. If you find existing tests matching on prompt text, delete the offending assertions.
- The same principle applies to any test that asserts on log messages, error message wording, or other human-readable strings that aren't part of the contract.

### Pre-Push Checklist

Run in order before every commit/push, adapted to the project's toolchain:

1. Sync dependencies (if changed)
2. Format
3. Lint
4. Typecheck (run all available typecheckers; e.g., `pyright` + `basedpyright`)
5. Pre-commit hooks
6. Tests

If the project has a `Makefile` or task runner, keep it in sync when adding new tooling.

## Pull Request Conventions

### PR Title

- Imperative mood ("Add webhook retry logic", not "Added webhook retry logic")
- Short and focused on WHAT changed, not HOW

### PR Description

```
## Why
[Problem or motivation for the change]

## Changes
- [High-level change 1]
- [High-level change 2]

## Test Plan
- [How this was verified]
```

Avoid line-by-line code narration or implementation details obvious from the diff.

## Infrastructure as Code

All cloud resource changes must be made via IaC (Terraform, Pulumi, CDK, etc.) so they are auditable and reproducible. Do not create, modify, or delete cloud resources using the CLI, console, or SDKs directly.

- Always run `plan` (or equivalent dry-run) and review the diff before `apply`.
- Use remote state with locking for any shared environment.
- Use `import` blocks only as a one-time migration step to adopt existing resources into state — not part of day-to-day workflow.
- Never commit credentials. Use environment variables or a secrets manager.

## Debugging Production

When debugging a production issue from a trace ID, log link, or error report:

1. Pull the full trace/log context first — don't guess from symptoms.
2. Walk the call tree end-to-end: inputs, intermediate state, outputs, latencies, errors.
3. Reproduce locally before patching when feasible.
4. Fix the root cause, not the symptom. If a defensive `try/except` would hide the bug, that's a signal you haven't found it yet.
