# earnings-summary — Repo Agent Instructions

Loads on top of the global `AGENTS.md`. This file adds ONLY earnings-summary-specific guidance — the general code standards, type discipline, testing, PR conventions, branching, engineering bar, and per-session model-selection rule all live in the global `AGENTS.md` and are not repeated here.

**What this repo is.** A solo-built, pull-only, localhost equity-research platform. The business-logic library lives under `src/` (`llm`, `ui`, `compute`, `ask`, `signals`, `pipeline`, `report`, `dcf`, `models`); `execution/` holds thin single-purpose CLI entrypoints that import `src/` (data fetches, builds, the morning pipeline, and `execution/comments_server.py` — the Flask cockpit at http://127.0.0.1:7421). State lives in `data/portfolio.db` under alembic migrations; intermediate artifacts in `.tmp/`. It does run automated data pipelines (transcripts, financials, web scraping), and the layered discipline below applies to that work.

LLMs are probabilistic, business logic is deterministic. The 3-layer architecture below forces deterministic logic into code, leaving the LLM to handle routing and synthesis.

## The 3-Layer Architecture

### Layer 1: Directive (Intent & Parameters)

- **Location**: `directives/` (Markdown files, one per task type)
- **Function**: Defines goals, required inputs, authorized tools, expected outputs, schemas, constraints, and known edge cases (API limits, rate limits, fallback behavior)
- **Rule**: Immutable baseline for a task. Do not modify without explicit user authorization.
- **Each directive must specify**: target source, output schema, refresh cadence, idempotency key, rate-limit budget, failure-mode policy.
- Operational discoveries become a proposed directive patch with evidence. Authorization to edit a directive and authorization to commit that edit are separate; do not infer either from permission to run the pipeline.

### Layer 2: Orchestration (Routing & Decision)

- **Function**: Core agent operating layer. Read directives, verify inputs, sequence `execution/` script calls, process stdout/stderr, hand off to next step.
- **Rule**: Do not implement business logic in this layer. If you find yourself writing transformations in the agent's reasoning, that logic belongs in an `execution/` script.
- **State management**: On multi-step failure, check `.tmp/` for intermediate state and resume from the last successful checkpoint. Do not restart from step one unless data integrity requires it.
- **Ownership**: Fable/Sol retains routing, exception decisions, and final synthesis. Use one to three Sonnet/Terra workers only for independent, bounded reads or implementations with explicit file ownership; use Haiku/Luna only for mechanical extraction. Delegation depth stays at one.
- **Concurrency**: One process owns each mutable pipeline state, database write set, cursor, or output artifact. Parallelize read-only discovery, never competing writers. Scheduled and interactive runs must acquire or honor the same run lock before mutation.

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

- Directives must be refined, not bloated. When you learn something new (an API quirk, a rate limit, a date format), propose a consolidated edit rather than appending a running diary.
- Request permission before editing a directive, validate the resulting procedure, then request separate permission before committing it.

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
- **Compose the kit; never reinvent a component.** Anything rendered — a button, a status badge, a chip/tag, a callout, a ticker label — uses the `src/ui/controls.py` primitive, not freehand CSS: buttons → `.k-btn`(`-primary`/`-quiet`/`-danger`/`-sm`); filled status pill → `.k-pill`(+`-ok/-warn/-bad`); outline kind/filter tag → `.k-chip`(+tones/`-mono`/`-btn`); callout block → `.k-well`; ticker+name → `ticker_label()`; rendered prose → `ui.prose.render_prose`. A surface adds **layout only** (width/flex/grid/gap), and preserves JS-hook classes *alongside* the kit class (e.g. `class="ix-act k-btn k-btn-quiet k-btn-sm"`). Using on-scale tokens (`var(--fs-body)`, `var(--radius)`, a `color-mix` tone fill) does NOT make a hand-rolled button/pill compliant — it is still §4 drift.
- **The guard is partial — `tests/test_ui_controls.py` auto-enforces tokens + the `kit-badge` component check (a reinvented filled status pill fails CI); the rest of §4 is on you.** A NEW `src/**.py` that emits `var(--` must be added to that file's `REGISTERED` set and be token-clean (or quarantined) or CI fails. **Run `python -m pytest tests/test_ui_controls.py -q` for any frontend change** — targeted test selection misses the surface-discovery + component checks. Touching a report renderer also needs `GOLDEN_REGEN=1 python -m pytest tests/test_workspace_golden.py` and a diff review.

## Security

- Credentials live in `.env`, `credentials.json`, `token.json`. Never log, output, or commit these (also in global rules).
- API keys passed to scripts via environment variables only — never as CLI args (they leak into shell history and process lists).
- Secrets used in URL query strings get redacted in any logged output.

## Session & Agent Model Selection — repo scope note

The per-session rule in the global `AGENTS.md` applies unchanged: Fable/Sol is the primary orchestrator, Sonnet/Terra is the execution tier, and Haiku/Luna is reserved for mechanical work. Skip delegation for small cohesive tasks.

Repo-specific scope: that rule governs **coding/session** model choice. The application's **in-app per-purpose LLM routing** is a separate concern, governed by `LLM_MODELS` in `src/llm/cli.py`, the model-downgrade eval loop (`directives/model_eval_loop.md`), and the cheapest-at-parity routing design (`directives/cheapest_model_routing.md`).

## Scheduling & Quota Discipline — repo scope note

The app's in-app LLM transport (`src/llm/cli.py` → subscription `claude` CLI) **shares one quota with every interactive Claude Code session on this machine**. The global `AGENTS.md` → "Scheduling & Quota Discipline" rules apply here with these repo specifics (full detail: `directives/llm_quota_scheduling.md`):

- Protected windows (America/Los_Angeles): **04:00 morning pipeline** (LLM legs: stage 0b `decision_conditions_extract`, stage 0/1 news + `material_news_classification`), **03:00 on the 1st** (`refresh_scenario_priors`), **Sun ~10:30** (weekly eval rungs). Multi-agent bursts must not still be burning 03:00–05:00; segment waves ≥6–7h apart.
- Every NEW scheduled job with an LLM leg follows the per-item degrade pattern (transient CLI failure → defer + tally + retry next run; hard stops loud) — reference: `attach_conditions` post-#814 — and registers its window in `directives/llm_quota_scheduling.md`.

## General Code Standards — see global AGENTS.md

The full backend/code standards (typing, the NEVER/ALWAYS lists, classification, testing discipline, the pre-push checklist, PR conventions, Deep Modules) live in the global `AGENTS.md` and apply here unchanged — do not duplicate them in this file. The one repo nuance: a single `cast(...)` at a validated JSON / external-data boundary (right after an `isinstance`/schema check) is the accepted pattern here; never `# type: ignore` (this matches the global NEVER list's JSON-boundary exception). See `src/log_redact.py` for the canonical credential-redaction helper the global secret-handling rules reference.

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
