# earnings-summary — Repo Agent Instructions

Loads on top of the global `GEMINI.md`. This file adds ONLY earnings-summary-specific guidance — the general code standards, type discipline, testing, PR conventions, branching, engineering bar, and per-session model-selection rule all live in the global `GEMINI.md` and are not repeated here.

**What this repo is.** A solo-built, pull-only, localhost equity-research platform. The business-logic library lives under `src/` (`llm`, `ui`, `compute`, `ask`, `signals`, `pipeline`, `report`, `dcf`, `models`); `execution/` holds thin single-purpose CLI entrypoints that import `src/` (data fetches, builds, the morning pipeline, and `execution/comments_server.py` — the Flask cockpit at http://127.0.0.1:7421). State lives in `data/portfolio.db` under alembic migrations; intermediate artifacts in `.tmp/`. It does run automated data pipelines (transcripts, financials, web scraping), and the layered discipline below applies to that work.

LLMs are probabilistic, business logic is deterministic. The 3-layer architecture below forces deterministic logic into code, leaving the LLM to handle routing and synthesis.

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

## UI / Front-end

- Any HTML surface work follows `directives/design_language.md` (canonical): tokens from `src/ui/tokens.py`, controls/chips/ticker-labels from `src/ui/controls.py`, no raw hex or off-scale font sizes in surface CSS.

## Security

- Credentials live in `.env`, `credentials.json`, `token.json`. Never log, output, or commit these (also in global rules).
- API keys passed to scripts via environment variables only — never as CLI args (they leak into shell history and process lists).
- Secrets used in URL query strings get redacted in any logged output.

## Session & Agent Model Selection — repo scope note

The per-session AGENT model-selection rule (Opus/Sonnet/Haiku-class by task nature) lives in the global `GEMINI.md` → "Session & Agent Model Selection (Token Discipline)" and applies here unchanged.

Repo-specific scope: that rule governs **coding/session** model choice. The application's **in-app per-purpose LLM routing** is a separate concern, governed by `LLM_MODELS` in `src/llm/cli.py`, the model-downgrade eval loop (`directives/model_eval_loop.md`), and the cheapest-at-parity routing design (`directives/cheapest_model_routing.md`).

## General Code Standards — see global GEMINI.md

The full backend/code standards (typing, the NEVER/ALWAYS lists, classification, testing discipline, the pre-push checklist, PR conventions, Deep Modules) live in the global `GEMINI.md` and apply here unchanged — do not duplicate them in this file. The one repo nuance: a single `cast(...)` at a validated JSON / external-data boundary (right after an `isinstance`/schema check) is the accepted pattern here; never `# type: ignore` (this matches the global NEVER list's JSON-boundary exception). See `src/log_redact.py` for the canonical credential-redaction helper the global secret-handling rules reference.

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
