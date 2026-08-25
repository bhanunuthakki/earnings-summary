# earnings-summary — Repo Agent Instructions

Loads on top of the global `AGENTS.md`. This file adds only earnings-summary facts and constraints. Cross-project safety, authority, and routing stay in the root contract; detailed global workflows live in routed procedures and load only when their trigger applies.

**What this repo is.** A solo-built, pull-only, localhost equity-research platform. The business-logic library lives under `src/` (`llm`, `ui`, `compute`, `ask`, `signals`, `pipeline`, `report`, `dcf`, `models`); `execution/` holds thin single-purpose CLI entrypoints that import `src/` (data fetches, builds, the morning pipeline, and `execution/comments_server.py` — the Flask cockpit at http://127.0.0.1:7421). State lives in `data/portfolio.db` under alembic migrations; intermediate artifacts in `.tmp/`. It does run automated data pipelines (transcripts, financials, web scraping), and the layered discipline below applies to that work.

## Mac/Windows listener ownership

- The always-on production-shaped host is Windows: `es-dashboard` owns loopback `127.0.0.1:7421`, and the Portfolio Tracker API owns loopback `127.0.0.1:8000`. The dashboard reaches the tracker on that same Windows host.
- A Mac browser must open the exact private HTTPS origin printed by live `tailscale serve status` on Windows. Mac `127.0.0.1:7421`, a remembered Windows computer name, a raw Tailnet IP, or the DNS name from `tailscale status` is not a substitute.
- Expose only the dashboard through Tailscale Serve. Keep both backends loopback-only; do not expose port 8000 separately and never use Funnel.
- After a Windows or Tailscale rename, run the documented Serve reset/reapply flow, set `COMMENTS_SERVER_CORS_WHITELIST` to that exact new HTTPS origin, restart `es-dashboard`, then prove Windows-local dashboard/tracker health and Mac-to-Windows dashboard hydration.

LLMs are probabilistic, business logic is deterministic. The 3-layer architecture below forces deterministic logic into code, leaving the LLM to handle routing and synthesis.

## The 3-Layer Architecture

### Layer 1: Directive (Intent & Parameters)

- **Location and class authority**: `directives/directive_manifest.json` classifies every Markdown file. Only `canonical` entries own active policy or task contracts. A `runbook` executes a named canonical contract but does not redefine it. `draft` and `history` entries never govern current behavior.
- **Function**: An executable canonical directive defines its goal, required inputs, authorized tools, expected outputs, schemas, constraints, and relevant edge cases. Its runbook may add provider or environment mechanics within that contract.
- **Change control**: A canonical contract is the approved baseline for its scope. Do not change it without explicit user authorization; a runbook edit must remain consistent with its canonical owner.
- **Applicable metadata**: Executable canonical directives and runbooks that authorize recurring, networked, or mutating work specify the relevant target source, output schema, refresh cadence, Logical Idempotency Key, rate-limit or cost budget, and failure-mode policy. Omit fields that do not apply instead of adding boilerplate. Use all four identity terms from `DEFINITIONS.md` wherever an operation persists or retries work; an Attempt Identity is never a Logical Idempotency Key.
- Operational discoveries become a proposed canonical or runbook patch with evidence. Authorization to edit and authorization to commit are separate; do not infer either from permission to run the pipeline.

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
  - Scripts are logically idempotent: rerunning the same Logical Idempotency Key produces no duplicate effect (or a clear "already done" exit). New source bytes remain a new Observation Version rather than being overwritten.
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

- Canonical directives and runbooks must be refined, not bloated. When you learn something new (an API quirk, a rate limit, a date format), propose a consolidated edit to the correct manifest class rather than appending a running diary.
- Request permission before editing a canonical contract, validate the resulting procedure, then request separate permission before committing it. Drafts and history remain explicitly non-governing.

### Operations & Governance Surface Impact

- A change that adds, removes, renames, or materially changes an operation, operational observation, or operator action must follow `directives/operations_governance_surface.md` before it is complete.
- The change must leave a truthful surface update or an explicit tested no-surface-change disposition. Never infer health from configuration, silently leave a removed capability visible, or expose a mutating control merely because an execution path exists.

### Locally Owned, Exit-Ready Reconstructability Invariant

- The core assets of `earnings-summary` (data models, SQLite schemas and alembic migrations, financial compute formulas, synthesis lenses, DCF valuation models, evaluation suites, and deterministic CLI entrypoints) are locally owned, versioned, and runtime-neutral.
- Prompts, schemas, domain semantics, and test suites must never depend on vendor-specific harness state, proprietary memory stores, or unversioned cloud environments.
- Provider SDKs, hosted CI workflows, Claude/Gemini CLI subscription wrappers, model IDs, external financial APIs (FMP, SEC EDGAR), and cloud backup targets are replaceable boundary adapters.
- All 11 platform subsystems must satisfy the deterministic inventory and verification contract in `reconstruction_manifest.json` and `execution/verify_reconstruction_inventory.py`.
- Reconstructability is exit-ready by design: any replacement agent or local harness can verify, reconstruct, and operate the platform using version-controlled code, migrations, and SQLite snapshots.

## File Organization & State

### Categories

- **Deliverables**: Push to canonical destinations (Google Drive, Sheets, S3, Postgres). Never to `.tmp/`.
- **Intermediates**: Write exclusively to `.tmp/`. Safe to wipe.
- **Cached responses**: Optional, in `.cache/` if implemented. Always include a TTL or invalidation rule.

### Directory Structure

`directives/folder_structure.md` owns the exact repository topology and its
machine-checked contract. Invariants at this layer: source belongs in its registered
source root, resumable intermediates belong in `.tmp/`, durable application state
belongs under `data/`, and generated deliverables belong under `output/` or their
declared external destination.

### State & Idempotency

- Every pipeline operation declares a stable Logical Idempotency Key separately from its unique Attempt Identity.
- Before executing, check whether the logical deliverable already exists at the required Observation Version. Skip with a logged "already done" if so, unless `--force` is passed.
- Pagination state, partial scrapes, and multi-page reports are checkpointed in `.tmp/<run_id>/state.json`, where `run_id` is the Attempt Identity, so resumption is exact.

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

- UI work uses the shared `procedures/frontend-quality.md` with `directives/design_language.md`. This remains a solo, local-first product: material browser-grounded UX evidence is required now, while commercial or multi-tenant work is an explicit future transition through `/harden --full`, not present complexity.
- Executable authority lives in `src/ui/tokens.py`, `src/ui/controls.py`, `src/ui/design_registry.py`, and `src/ui/conformance_scan.py`.
- Consumers select registered controls and family recipes; they do not add local visual CSS, open-ended style APIs, or runtime style mutations. A new visual decision must enter the appropriate master with a typed contract and adversarial test.
- `directives/design_language.md` owns same-project continuity: start from the nearest shipped sibling and its registered family, and require explicit evidence before creating a new visual family.
- For material frontend work, render the affected primary task before and after changes, inspect applicable states and supported widths, and record any unavailable visual verification. Run `python scripts/check_design_sync.py` for every visual change. Report-renderer changes also require `GOLDEN_REGEN=1 python -m pytest tests/test_workspace_golden.py` and visual diff review.

## Testing, CI & Merge Velocity Discipline

- **Merge Frequency & PR Sizing:** Never batch weeks of work into giant feature branches. Land small, intent-driven PRs frequently (e.g. migration/model → script CLI → UI cockpit). With 270+ Alembic migrations and golden snapshot tests, small merges prevent migration head collisions and unreviewable diff cascades.
- **Fast Local Feedback (`make check-fast`):** Use `make check-fast` (format + lint + typecheck + `pytest` on changed test files only) during active iteration. Use `make check` for complete pre-push verification, or `FAST_PUSH=1 git push` to delegate full matrix testing to CI.
- **Bounded Test Execution:** Direct and targeted `pytest` runs are serial so they do not fan out across every local CPU. `make test` caps distribution at `PYTEST_WORKERS=2` with `--dist=loadfile` (override only after measuring the machine); CI also passes its worker count explicitly. Keep tests hermetic; use session-scoped `migrated_db` in `tests/conftest.py` rather than re-running Alembic migrations from scratch inside test functions.
- **Ratchet Quality Gates:** Ruff linting and strict Pyright type-checking use diff-aware ratchets against `origin/main`. PRs must be clean on changed lines/files and must not increase overall Pyright error counts.

## Security

- Repo credential files: `.env`, `credentials.json`, `token.json` (global no-log/no-commit rules apply). Pass keys to scripts via environment variables, never CLI args; `src/log_redact.py` is the canonical redaction helper.

## Agent delegation and review — repo scope note

Use [[root:Evidence and delegation]] plus `procedures/agent-operations.md` for delegation and `procedures/judging.md` for J0-J3 review rigor. The repository does not redefine either policy.

Repo-specific scope: that rule governs **coding/session** model choice. The application's **in-app per-purpose LLM routing** is a separate concern, governed by `LLM_MODELS` in `src/llm/cli.py`, the model-downgrade eval loop (`directives/model_eval_loop.md`), and the cheapest-at-parity routing design (`directives/cheapest_model_routing.md`).

## LLM scheduling and quota — repo scope note

The app's in-app LLM path uses the Codex membership transport first with the Claude subscription fallback (`src/llm/cli.py`). Each pool shares quota with its corresponding interactive sessions on this machine. Apply global `agent-operations.SCHEDULING.md` with these repo specifics (full detail: `directives/llm_quota_scheduling.md`):

- Protected windows (America/Los_Angeles): **04:00 morning pipeline** (LLM legs: stage 0b `decision_conditions_extract`, stage 0/1 news + `material_news_classification`), **03:00 on the 1st** (`refresh_scenario_priors`), **Sun ~10:30** (weekly eval rungs). Multi-agent bursts must not still be burning 03:00–05:00; segment waves ≥6–7h apart.
- Every new scheduled job with an LLM leg follows the shared per-item degrade pattern (transient CLI failure → defer + tally + retry next run; hard stops loud) and registers its window in `directives/llm_quota_scheduling.md`.

## Code-change specifics

Use the global `code-change` procedure for typing, testing, architecture review, and validation. The one repo nuance: a single `cast(...)` at a validated JSON / external-data boundary (right after an `isinstance`/schema check) is accepted; never use `# type: ignore`. See `src/log_redact.py` for the canonical credential-redaction helper.

## Architectural & Execution Traps (Operational Learnings)

- **Alembic Baseline Migrations:** Baseline migrations (`0001_initial_schema.py`) must wrap all DDL in `IF NOT EXISTS` syntax, and seed inserts must execute after table creation. Test fixtures building clean databases must invoke `command.upgrade(cfg, "head")` directly (since `command.stamp()` populates `alembic_version` without executing table DDL).
- **Transitive Reachability Scans:** Before excising code from legacy modules (e.g., `src/provenance/`), run a full transitive dependency scan from non-provenance entrypoints (`src/timeseries/`, `src/pipeline/`, `execution/`) to prevent breaking hidden product imports.
- **Request-Scoped DB Connection Pooling:** Surface renderers and server routes (`comments_server.py`) must thread a single request-scoped `sqlite3.Connection` via `open_repo_db(repo_root, conn=conn)` and Flask `g.request_read_db` (closed via `@app.teardown_request`) to eliminate per-section connection churn.
- **Pre-Persist Fact Plausibility:** Bulk writes to `financial_facts` must route through `insert_with_restatement_detection` to execute pre-persist plausibility gates (`_validate_financial_fact_plausibility`) before committing.
- **Resumable Multi-Stage Orchestration:** Multi-stage orchestrators (`execution/run_morning_pipeline.py`) track completed stage keys in `.tmp/morning_pipeline/state.json` (18h TTL) to enable exact resumption from the last successful stage on failure/retry.
- **CI Delegation for Large Diffs:** Use `FAST_PUSH=1 git push` (or `git push --no-verify`) to delegate full matrix testing and LF-normalization checks to CI when pushing large file reorganizations or archived migration sweeps.
