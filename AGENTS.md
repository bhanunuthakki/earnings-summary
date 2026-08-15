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
- **Ownership**: the orchestrator retains routing, exception decisions, and final synthesis; workers take independent, bounded reads or implementations with explicit file ownership, per the global delegation policy.
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

- The shared pipeline primitives (HTTP client, HTML parser, transcript fetcher, financial-data adapter, S3/Drive uploader) already live in `execution/` — reuse them; a genuinely new script also goes in `execution/`, never inline in agent reasoning.

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

- Respect `robots.txt` and documented rate limits (the budget lives in the directive); never scrape behind authenticated sessions unless the directive explicitly authorizes it.
- Prefer a page's underlying API over headless browser automation; headless is a last resort.

### Financial Data

- Always validate that data falls within plausible ranges before persisting. A revenue figure 10x off is more likely a unit error (cents vs dollars, thousands vs millions) than a real outlier. Halt and surface.
- Currency must be explicit on every numeric field. No "assumed USD."
- Date ranges must be explicit (period start, period end, fiscal vs calendar). No "last quarter."
- When pulling from multiple sources, log source provenance per field. Don't silently merge.

### Transcripts

- Preserve speaker attribution and time codes; store source URL and pull timestamp alongside the transcript text.

### Schema Drift Defense

- Every parsed response validated against a Pydantic model. On validation error, halt and dump the raw response to `.tmp/` for inspection. Do not attempt a guess-fix in the agent loop — the schema changed, the directive needs updating.

## UI / Front-end

- Any HTML surface work follows `directives/design_language.md` (canonical): tokens from `src/ui/tokens.py`, controls/chips/ticker-labels from `src/ui/controls.py`, no raw hex or off-scale font sizes in surface CSS.
- **Compose the kit; never reinvent a component.** Anything rendered — a button, a status badge, a chip/tag, a callout, a ticker label — uses the `src/ui/controls.py` primitive, not freehand CSS: buttons → `.k-btn`(`-primary`/`-quiet`/`-danger`/`-sm`); filled status pill → `.k-pill`(+`-ok/-warn/-bad`); outline kind/filter tag → `.k-chip`(+tones/`-mono`/`-btn`); callout block → `.k-well`; ticker+name → `ticker_label()`; rendered prose → `ui.prose.render_prose`. A surface adds **layout only** (width/flex/grid/gap), and preserves JS-hook classes *alongside* the kit class (e.g. `class="ix-act k-btn k-btn-quiet k-btn-sm"`). Using on-scale tokens (`var(--fs-body)`, `var(--radius)`, a `color-mix` tone fill) does NOT make a hand-rolled button/pill compliant — it is still §4 drift.
- **Harvey/Legora 3-Layer Work OS Architecture & Token Purity (2026-08-07).** Dashboard surfaces adopt the 3-Layer Sidebar navigation hierarchy (L1 Portfolio Intelligence, L2 Research Engine, L3 Operations & Governance) with Ultra-Deep Warm Obsidian ground (`#090a0c`). Action cards implement the zero-layout-pop interactive collapse contract (`dismissCard()`, height-locked transition with transparent border) and prompt drawers incorporate grounded AI copilot suggestion chips. Every spatial, font, blur, shadow, transform, and border property outside `:root` must bind to CSS custom variables (`var(--sp-*)`, `var(--fs-*)`, `var(--radius-*)`, `var(--blur-*)`, `var(--shadow-*)`, `var(--bw-*)`) — zero raw pixel escapes allowed!
- **The guard is partial — `tests/test_ui_controls.py` auto-enforces tokens + the `kit-badge` component check (a reinvented filled status pill fails CI); the rest of §4 is on you.** A NEW `src/**.py` that emits `var(--` must be added to that file's `REGISTERED` set and be token-clean (or quarantined) or CI fails. **Run `python -m pytest tests/test_ui_controls.py -q` for any frontend change** — targeted test selection misses the surface-discovery + component checks. Touching a report renderer also needs `GOLDEN_REGEN=1 python -m pytest tests/test_workspace_golden.py` and a diff review.

## Testing, CI & Merge Velocity Discipline

- **Merge Frequency & PR Sizing:** Never batch weeks of work into giant feature branches. Land small, intent-driven PRs frequently (e.g. migration/model → script CLI → UI cockpit). With 270+ Alembic migrations and golden snapshot tests, small merges prevent migration head collisions and unreviewable diff cascades.
- **Fast Local Feedback (`make check-fast`):** Use `make check-fast` (format + lint + typecheck + `pytest` on changed test files only) during active iteration. Use `make check` for complete pre-push verification, or `FAST_PUSH=1 git push` to delegate full matrix testing to CI.
- **Bounded Test Execution:** Direct and targeted `pytest` runs are serial so they do not fan out across every local CPU. `make test` caps distribution at `PYTEST_WORKERS=2` with `--dist=loadfile` (override only after measuring the machine); CI also passes its worker count explicitly. Keep tests hermetic; use session-scoped `migrated_db` in `tests/conftest.py` rather than re-running Alembic migrations from scratch inside test functions.
- **Ratchet Quality Gates:** Ruff linting and strict Pyright type-checking use diff-aware ratchets against `origin/main`. PRs must be clean on changed lines/files and must not increase overall Pyright error counts.

## Security

- Repo credential files: `.env`, `credentials.json`, `token.json` (global no-log/no-commit rules apply). Pass keys to scripts via environment variables, never CLI args; `src/log_redact.py` is the canonical redaction helper.

## Agent delegation and review — repo scope note

Use [[root:Delegation & Subagent Calibration]] for agent delegation and [[root:Evidence governance]] plus the `judging` procedure for J0-J3 review rigor. The repository does not redefine either policy.

Repo-specific scope: that rule governs **coding/session** model choice. The application's **in-app per-purpose LLM routing** is a separate concern, governed by `LLM_MODELS` in `src/llm/cli.py`, the model-downgrade eval loop (`directives/model_eval_loop.md`), and the cheapest-at-parity routing design (`directives/cheapest_model_routing.md`).

## LLM scheduling and quota — repo scope note

The app's in-app LLM transport (`src/llm/cli.py` → subscription `claude` CLI) **shares one quota with every interactive Claude Code session on this machine**. Apply global `agent-operations.SCHEDULING.md` with these repo specifics (full detail: `directives/llm_quota_scheduling.md`):

- Protected windows (America/Los_Angeles): **04:00 morning pipeline** (LLM legs: stage 0b `decision_conditions_extract`, stage 0/1 news + `material_news_classification`), **03:00 on the 1st** (`refresh_scenario_priors`), **Sun ~10:30** (weekly eval rungs). Multi-agent bursts must not still be burning 03:00–05:00; segment waves ≥6–7h apart.
- Every NEW scheduled job with an LLM leg follows the per-item degrade pattern (transient CLI failure → defer + tally + retry next run; hard stops loud) — reference: `attach_conditions` post-#814 — and registers its window in `directives/llm_quota_scheduling.md`.

## Code-change specifics

Use the global `code-change` procedure for typing, testing, architecture review, and validation. The one repo nuance: a single `cast(...)` at a validated JSON / external-data boundary (right after an `isinstance`/schema check) is accepted; never use `# type: ignore`. See `src/log_redact.py` for the canonical credential-redaction helper.

## Architectural & Execution Traps (Operational Learnings)

- **Alembic Baseline Migrations:** Baseline migrations (`0001_initial_schema.py`) must wrap all DDL in `IF NOT EXISTS` syntax, and seed inserts must execute after table creation. Test fixtures building clean databases must invoke `command.upgrade(cfg, "head")` directly (since `command.stamp()` populates `alembic_version` without executing table DDL).
- **Transitive Reachability Scans:** Before excising code from legacy modules (e.g., `src/provenance/`), run a full transitive dependency scan from non-provenance entrypoints (`src/timeseries/`, `src/pipeline/`, `execution/`) to prevent breaking hidden product imports.
- **Request-Scoped DB Connection Pooling:** Surface renderers and server routes (`comments_server.py`) must thread a single request-scoped `sqlite3.Connection` via `open_repo_db(repo_root, conn=conn)` and Flask `g.request_read_db` (closed via `@app.teardown_request`) to eliminate per-section connection churn.
- **Pre-Persist Fact Plausibility:** Bulk writes to `financial_facts` must route through `insert_with_restatement_detection` to execute pre-persist plausibility gates (`_validate_financial_fact_plausibility`) before committing.
- **Resumable Multi-Stage Orchestration:** Multi-stage orchestrators (`execution/run_morning_pipeline.py`) track completed stage keys in `.tmp/morning_pipeline/state.json` (18h TTL) to enable exact resumption from the last successful stage on failure/retry.
- **CI Delegation for Large Diffs:** Use `FAST_PUSH=1 git push` (or `git push --no-verify`) to delegate full matrix testing and LF-normalization checks to CI when pushing large file reorganizations or archived migration sweeps.



