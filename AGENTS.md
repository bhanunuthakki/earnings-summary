# earnings-summary — Repo Agent Instructions

This file adds earnings-summary facts and constraints to the global contract.

**What this repo is.** A solo-built, pull-only, localhost equity-research platform. Reusable business logic lives under `src/`; `execution/` contains operational entrypoints plus `execution/comments_server.py`, the Flask cockpit at http://127.0.0.1:7421. Production state belongs to the configured canonical Windows database authority; its path must be resolved from approved runtime configuration, never guessed from a checkout. Mac tests use explicit temporary or provenance-bearing restored-snapshot databases. Intermediate artifacts live in `.tmp/`.

## Purpose and improvement latitude

Help the owner understand what changed in a company, whether it changes the investment case, and
what evidence supports that judgment. Improve acquisition/extraction completeness, novel-metric
discovery, comparability, provenance access, reading flow, and recovery. Explore analytical hypotheses
and better UI families within authorized work while distinguishing reported facts, assumptions and
analyst inference. Production authority, financial lineage and publication permissions remain fixed
unless the owner explicitly changes them.

## Production and test boundary

The production database authority is configured outside this repository; the canonical Windows host
owns production dashboard and tracker state. The implicit checkout-default `data/portfolio.db` is
what “checkout-local database” means here: a checkout-local database is never a live, fallback, replica, or roster authority
and must not exist. Explicit disposable migrated tests and approved provenance-bearing `.tmp/` snapshot
restores are separate authorized artifacts, never silently inferred production state. Before live
access, restoration, listener repair or resource handoff, read `directives/agent_host_operations.md`
for exact private-origin, loopback-only, resource-ownership and recovery requirements.

## Investment-grade financial-data invariant

Identification, acquisition, parsing, storage, resolution, and surfacing of company-reported financial data are the central product contract, not best-effort enrichment.

- A decision-grade fact is unusable until it carries issuer identity, an effective-dated metric-definition revision, fiscal period, explicit unit/currency/scope/basis, source-document identity and locator, immutable Observation Version, capture/extractor version, and semantic admission or disposition.
- Preserve exact raw source bytes and management wording. Corrections and restatements supersede prior observations; they never erase them. Conflicting sources remain explicit and ranked by shared policy rather than silently merged.
- Acquisition completeness and extraction completeness are separate typed receipts. A discovered page, downloaded document, or single extracted fact never proves that an issuer archive, document package, page, section, table, or expected fact population is complete. Unknown, partial, stale, rejected, and failed states remain visible and fail closed.
- Definition, presentation, geography, period, unit, accounting basis, or segment changes create versioned lineage and a comparability disposition before a value can join a canonical series. Never normalize away a semantic break merely to keep a chart continuous.
- Preserve novel and one-off management observations with speaker, source locator, raw label/value, period/scope, and recurring/promotion status even when they are not admitted to the recurring KPI database.
- Every consumer—reports, time series, thesis evaluation, alerts, models, transcripts, and earnings readouts—uses the same provenance-aware fact resolver. User-facing outputs persist a complete source/context manifest or claim-level citations and distinguish reported fact, management claim, consensus estimate, calculation, and analyst inference.
- A pipeline or UI may claim `decision-grade` only when source authority, completeness, semantic admission, reader parity, and reconstruction checks all pass. Otherwise report the precise degraded state and missing evidence.
- Never substitute generic web search for company-reported data, earnings updates, or thesis thresholds. Query local `micro_thesis/holdings/<TICKER>.json`, the restored database, and `transcripts/` (or `cron/fetch_transcripts.py`) as the primary authorities.

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
- Gate: `python scripts/check_design_sync.py`; report-renderer changes also require `python -m pytest tests/test_workspace_golden.py` (comparison mode) plus visual diff review

UI work uses the shared `frontend-quality` procedure. Registered controls and family recipes are the default. An authorized redesign may evolve the design language through its master/registry extension path, with evidence for the changed user task; do not bypass that owner with local visual CSS, open-ended style APIs, or runtime style mutations. Golden regeneration (`GOLDEN_REGEN=1`) is an intentional expectation update, never the checking gate: review the changed expectations, then rerun comparison mode. Preserve the protected behavior when updating an obsolete expectation. `source_chip.py` and `workspace_styles.py` are existing surface-specific authorities whose shared semantics must remain aligned even where geometry differs. Material frontend work requires before/after browser evidence for the affected task and an explicit note for any unverified state.

## Tests and merge gates

- Keep PRs small enough to review migrations and golden changes together; long Alembic history makes migration-head collisions costly.
- Use `make check-fast` (format, lint, typecheck, and changed test files) during iteration. Use `make check` before pushing, or `FAST_PUSH=1 git push` to delegate the full matrix to CI.
- **Bounded Test Execution:** Direct and targeted `pytest` runs are serial so they do not fan out across every local CPU. `make test` caps distribution at `PYTEST_WORKERS=2` with `--dist=loadfile` (override only after measuring the machine); CI also passes its worker count explicitly. Keep tests hermetic; use session-scoped `migrated_db` in `tests/conftest.py` rather than re-running Alembic migrations from scratch inside test functions.
- **Ratchet Quality Gates:** Ruff linting and strict Pyright type-checking use diff-aware ratchets against `origin/main`. PRs must be clean on changed lines/files and must not increase overall Pyright error counts.

## Security

- Repo credential files: `.env`, `credentials.json`, `token.json` (global no-log/no-commit rules apply). Pass keys to scripts via environment variables, never CLI args; `src/log_redact.py` is the canonical redaction helper.

## Application LLM authority

The global fleet policy and usage index own ordinary membership-provider order and its environment variables. `src/llm/fleet_policy.py` resolves that policy for the application; it does not set a competing provider order. `LLM_MODELS` in `src/llm/cli.py` owns per-purpose model pins, `directives/model_eval_loop.md` owns qualification and promotion, and `directives/cheapest_model_routing.md` owns economic ordering. These local owners do not override fleet provider priority. Explicit Judge selection and independence remain separate at the authorities registered in the fleet usage index. Session delegation and review follow the global contract.

## LLM scheduling and quota — repo scope note

The app's membership-backed pools share quota with interactive sessions. Before scheduling an LLM job or launching a material agent burst, read the complete current window registry and degradation contract in `directives/llm_quota_scheduling.md`; do not mirror schedules here. New scheduled LLM work must register a collision-free window there in the same change.

## Code-change specifics

A single `cast(...)` immediately after an `isinstance`/schema check at a JSON or external-data boundary is accepted; never use `# type: ignore`.

## Local implementation traps

Read `directives/agent_implementation_traps.md` before migration/fixture changes, legacy-code
removal, DB-using renderers, financial-fact writes, or morning-pipeline resumption. It owns the
specific failure-prevention mechanics under the canonical domain contracts above.
