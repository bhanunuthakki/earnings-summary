# earnings-summary — Repo Agent Instructions

Loads on top of the global `AGENTS.md`. This file adds only earnings-summary facts and constraints. Cross-project safety, authority, and routing stay in the root contract; detailed global workflows live in routed procedures and load only when their trigger applies.

**What this repo is.** A solo-built, pull-only, localhost equity-research platform. Reusable business logic lives under `src/`; `execution/` contains operational entrypoints plus `execution/comments_server.py`, the Flask cockpit at http://127.0.0.1:7421. Production state lives only in the canonical Windows checkout's Alembic-managed `data/portfolio.db`; Mac tests use explicit temporary or restored-snapshot databases. Intermediate artifacts live in `.tmp/`.

## Mac/Windows listener ownership

- The always-on production-shaped host is Windows: `es-dashboard` owns loopback `127.0.0.1:7421`, and the Portfolio Tracker API owns loopback `127.0.0.1:8000`. The dashboard reaches the tracker on that same Windows host.
- The production database authority is configured outside this repository. A checkout-local database is never a live, fallback, replica, or roster authority and must not exist. Treat one as an invalid local artifact: do not inspect it for product facts, migrate it, seed it, or make code pass against it.
- Mac development and tests must name an explicit disposable migrated database under a test/temp root. A Mac task that needs live roster or production facts must coordinate Windows access and use the canonical Windows database read-only or an explicitly approved provenance-bearing snapshot/export. It must never silently create the checkout-default database.
- A Mac browser must open the exact private HTTPS origin printed by live `tailscale serve status` on Windows. Mac `127.0.0.1:7421`, a remembered Windows computer name, a raw Tailnet IP, or the DNS name from `tailscale status` is not a substitute.
- Expose only the dashboard through Tailscale Serve. Keep both backends loopback-only; do not expose port 8000 separately and never use Funnel.
- After a Windows or Tailscale rename, run the documented Serve reset/reapply flow, set `COMMENTS_SERVER_CORS_WHITELIST` to that exact new HTTPS origin, restart `es-dashboard`, then prove Windows-local dashboard/tracker health and Mac-to-Windows dashboard hydration.
- A CRD or browser tab that is auto-reconnecting, including one stuck on Connecting, still owns the GUI session. Before handoff, close or navigate it away, verify that it does not reconnect, and explicitly transfer the database, scheduler, service, and browser resources that remain in scope.

## Investment-grade financial-data invariant

Identification, acquisition, parsing, storage, resolution, and surfacing of company-reported financial data are the central product contract, not best-effort enrichment.

- A decision-grade fact is unusable until it carries issuer identity, an effective-dated metric-definition revision, fiscal period, explicit unit/currency/scope/basis, source-document identity and locator, immutable Observation Version, capture/extractor version, and semantic admission or disposition.
- Preserve exact raw source bytes and management wording. Corrections and restatements supersede prior observations; they never erase them. Conflicting sources remain explicit and ranked by shared policy rather than silently merged.
- Acquisition completeness and extraction completeness are separate typed receipts. A discovered page, downloaded document, or single extracted fact never proves that an issuer archive, document package, page, section, table, or expected fact population is complete. Unknown, partial, stale, rejected, and failed states remain visible and fail closed.
- Definition, presentation, geography, period, unit, accounting basis, or segment changes create versioned lineage and a comparability disposition before a value can join a canonical series. Never normalize away a semantic break merely to keep a chart continuous.
- Preserve novel and one-off management observations with speaker, source locator, raw label/value, period/scope, and recurring/promotion status even when they are not admitted to the recurring KPI database.
- Every consumer—reports, time series, thesis evaluation, alerts, models, transcripts, and earnings readouts—uses the same provenance-aware fact resolver. User-facing outputs persist a complete source/context manifest or claim-level citations and distinguish reported fact, management claim, consensus estimate, calculation, and analyst inference.
- A pipeline or UI may claim `decision-grade` only when source authority, completeness, semantic admission, reader parity, and reconstruction checks all pass. Otherwise report the precise degraded state and missing evidence.

## Authority map

- `directives/directive_manifest.json` classifies every directive. Only `canonical` entries own active policy; a `runbook` supplies task mechanics within its named owners; `draft` and `history` entries do not govern current behavior.
- `directives/folder_structure.md` owns repository topology and artifact lifecycle. `directives/data_pipeline_dag.md` owns pipeline stages, identity, resumption, retries, and intermediate retention. `directives/data_provenance.md` owns fact and document lineage. `directives/operations_governance_surface.md` owns operator actions and operational truth; operational capability changes update it or record an explicit tested no-surface-change disposition. Use `DEFINITIONS.md` for the four pipeline identities and domain terms.
- Reusable business logic and schemas live in typed modules under `src/`; `execution/` contains single-purpose CLI entrypoints and the provider mechanics assigned there by canonical runbooks. Orchestration selects and sequences those interfaces; it does not reproduce transformations in agent reasoning.
- One process owns each mutable database write set, cursor, checkpoint, or output artifact. Resume an exact checkpoint when safe; do not restart merely because a later stage failed. Schema, auth, and validation failures fail closed rather than entering an unchanged retry loop.
- The locally owned models, schemas, formulas, prompts, evals, and deterministic entrypoints are the durable product. `reconstruction_manifest.json` and `execution/verify_reconstruction_inventory.py` own reconstruction coverage; provider SDKs, hosted services, and model IDs remain replaceable adapters.

## Interface

- Profile: dense-desktop
- Contract: directives/design_language.md
- Executable authority: src/ui/tokens.py, src/ui/controls.py, src/ui/design_registry.py, src/ui/conformance_scan.py, src/ui/source_chip.py, src/report/renderers/workspace_styles.py
- Render: `python execution/sqlite_bootstrap.py execution/comments_server.py --port 7421`, then exercise the affected primary task and its supported widths
- Gate: `python scripts/check_design_sync.py`; report-renderer changes also require `GOLDEN_REGEN=1 python -m pytest tests/test_workspace_golden.py` plus visual diff review

UI work uses the shared `frontend-quality` procedure. New work selects registered controls and family recipes rather than adding local visual CSS, open-ended style APIs, or runtime style mutations. `source_chip.py` and `workspace_styles.py` are existing surface-specific authorities whose shared semantics must remain aligned even where geometry differs. Material frontend work requires before/after browser evidence for the affected task and an explicit note for any unverified state.

## Testing, CI & Merge Velocity Discipline

- **Merge Frequency & PR Sizing:** Never batch weeks of work into giant feature branches. Land small, intent-driven PRs frequently (e.g. migration/model → script CLI → UI cockpit). The long Alembic history and golden snapshot tests make small merges important for avoiding migration-head collisions and unreviewable diff cascades.
- **Fast Local Feedback (`make check-fast`):** Use `make check-fast` (format + lint + typecheck + `pytest` on changed test files only) during active iteration. Use `make check` for complete pre-push verification, or `FAST_PUSH=1 git push` to delegate full matrix testing to CI.
- **Bounded Test Execution:** Direct and targeted `pytest` runs are serial so they do not fan out across every local CPU. `make test` caps distribution at `PYTEST_WORKERS=2` with `--dist=loadfile` (override only after measuring the machine); CI also passes its worker count explicitly. Keep tests hermetic; use session-scoped `migrated_db` in `tests/conftest.py` rather than re-running Alembic migrations from scratch inside test functions.
- **Ratchet Quality Gates:** Ruff linting and strict Pyright type-checking use diff-aware ratchets against `origin/main`. PRs must be clean on changed lines/files and must not increase overall Pyright error counts.

## Security

- Repo credential files: `.env`, `credentials.json`, `token.json` (global no-log/no-commit rules apply). Pass keys to scripts via environment variables, never CLI args; `src/log_redact.py` is the canonical redaction helper.

## Agent delegation and review — repo scope note

Use the global Evidence and delegation rules plus `procedures/agent-operations.md` for delegation and `procedures/judging.md` for J0-J3 review rigor. The repository does not redefine either policy.

Repo-specific scope: that rule governs **coding/session** model choice. The application's **in-app per-purpose LLM routing** is a separate concern, governed by `LLM_MODELS` in `src/llm/cli.py`, the model-downgrade eval loop (`directives/model_eval_loop.md`), and the cheapest-at-parity routing design (`directives/cheapest_model_routing.md`).

## LLM scheduling and quota — repo scope note

The app's membership-backed pools share quota with interactive sessions. Before scheduling an LLM job or launching a material agent burst, read the complete current window registry and degradation contract in `directives/llm_quota_scheduling.md`; do not mirror schedules here. New scheduled LLM work must register a collision-free window there in the same change.

## Code-change specifics

Use the global `code-change` procedure for typing, testing, architecture review, and validation. The one repo nuance: a single `cast(...)` at a validated JSON / external-data boundary (right after an `isinstance`/schema check) is accepted; never use `# type: ignore`. See `src/log_redact.py` for the canonical credential-redaction helper.

## Architectural & Execution Traps (Operational Learnings)

- **Alembic Baseline Migrations:** Baseline migrations (`0001_initial_schema.py`) must wrap all DDL in `IF NOT EXISTS` syntax, and seed inserts must execute after table creation. Test fixtures building clean databases must invoke `command.upgrade(cfg, "head")` directly (since `command.stamp()` populates `alembic_version` without executing table DDL).
- **Transitive Reachability Scans:** Before excising code from legacy modules (e.g., `src/provenance/`), run a full transitive dependency scan from non-provenance entrypoints (`src/timeseries/`, `src/pipeline/`, `execution/`) to prevent breaking hidden product imports.
- **Request-Scoped DB Connection Pooling:** Surface renderers and server routes (`comments_server.py`) must thread a single request-scoped `sqlite3.Connection` via `open_repo_db(repo_root, conn=conn)` and Flask `g.request_read_db` (closed via `@app.teardown_request`) to eliminate per-section connection churn.
- **Pre-Persist Fact Plausibility:** Bulk writes to `financial_facts` must route through `insert_with_restatement_detection` to execute pre-persist plausibility gates (`_validate_financial_fact_plausibility`) before committing.
- **Resumable Multi-Stage Orchestration:** Multi-stage orchestrators (`execution/run_morning_pipeline.py`) track completed stage keys in `.tmp/morning_pipeline/state.json` (18h TTL) to enable exact resumption from the last successful stage on failure/retry.
- **CI Delegation for Large Diffs:** Use `FAST_PUSH=1 git push` only when the project hook explicitly preserves security and privacy checks while delegating the expensive matrix to CI. Never bypass hooks.
