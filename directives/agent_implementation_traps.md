# Agent implementation traps

Task mechanics under the canonical authorities in `../AGENTS.md`; these do not override approved
product intent or authorize live operations.

## Architectural & Execution Traps (Operational Learnings)

- **Alembic Baseline Migrations:** Baseline migrations (`0001_initial_schema.py`) must wrap all DDL in `IF NOT EXISTS` syntax, and seed inserts must execute after table creation. Test fixtures building clean databases must invoke `command.upgrade(cfg, "head")` directly (since `command.stamp()` populates `alembic_version` without executing table DDL).
- **Transitive Reachability Scans:** Before excising code from legacy modules (e.g., `src/provenance/`), run a full transitive dependency scan from non-provenance entrypoints (`src/timeseries/`, `src/pipeline/`, `execution/`) to prevent breaking hidden product imports.
- **Request-Scoped DB Connection Pooling:** Surface renderers and server routes (`comments_server.py`) must thread a single request-scoped `sqlite3.Connection` via `open_repo_db(repo_root, conn=conn)` and Flask `g.request_read_db` (closed via `@app.teardown_request`) to eliminate per-section connection churn.
- **Pre-Persist Fact Plausibility:** Bulk writes to `financial_facts` must route through `insert_with_restatement_detection` to execute pre-persist plausibility gates (`_validate_financial_fact_plausibility`) before committing.
- **Resumable Multi-Stage Orchestration:** Multi-stage orchestrators (`execution/run_morning_pipeline.py`) track completed stage keys in `.tmp/morning_pipeline/state.json` (18h TTL) to enable exact resumption from the last successful stage on failure/retry.
- **CI Delegation for Large Diffs:** Use `FAST_PUSH=1 git push` only when the project hook explicitly preserves security and privacy checks while delegating the expensive matrix to CI. Never bypass hooks.
