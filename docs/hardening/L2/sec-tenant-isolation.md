# sec-tenant-isolation — L2 (Multi-tenant Beta) audit

**Date:** 2026-06-08 · **Gate:** L2 `B` (blocking) · **Mode:** AUDIT (read-only) · **Verdict: 🔴 BLOCK**

**Depends on:** `sec-authz` (BLOCK) + `backend-multitenancy` (BLOCK) — both upstream gates fail, so this inherits an unsound foundation.

## Findings

| Severity | Location | Finding | Recommended fix |
|---|---|---|---|
| critical | `src/db.py:31,87-97` | No tenant scoping at the data layer — of ~56 tables, only 6 carry any tenant key; ~50 core tables (financial_facts, documents, dcf_runs, predictions, news, …) have no `user_id` and no scoping predicate. Two tenants on the same ticker share rows. | Add `tenant_id` to every owned table; enforce via a mandatory base-query layer or per-tenant DB. |
| critical | `src/synthesis/lenses/cross_portfolio_synthesis.py:96-176`; `portfolio_macro_stress.py:102-150` | Cross-portfolio lenses select `tracked_companies` with no `user_id` filter, then fan out by ticker → tenant A's memo computed over **every** tenant's holdings/sizing/theses, into an LLM prompt + persisted artifact. Highest-confidence leak. | Thread `user_id` into the lens context and filter the holdings query and every per-ticker subquery. |
| critical | `src/llm_artifact_store.py:157-279` | LLM artifact cache keyed `(ticker, scope, purpose, fiscal_period)` — no tenant dimension; tenant A's generated analysis served to tenant B. `scope='portfolio'` rows written `ticker=NULL` → one global row all tenants read. | Add `user_id` to `llm_artifacts` and to the cache-key tuple (read/upsert/mark_dirty); include tenant in `compute_input_sha256`. |
| critical | `src/identity.py:16`; `src/db.py:120,544` | No per-request tenant context — identity resolved once at import from `CIO_USER_ID`; ~30 call sites take `user_id=DEFAULT_USER_ID` default, so omitting it operates as ambient tenant; two id types (`1` int vs `'bhanu'` str). Concurrent multi-tenant serving structurally impossible. | Request-scoped tenant context (contextvar / explicit param); remove `user_id` defaults; unify id type. |
| high | `data/historical/fmp/<TICKER>_*.json`; `transcripts/*` (`src/db.py:32,365-405`) | On-disk artifacts share a global ticker-keyed namespace; onboarding reads/overwrites another tenant's files by construction; `data/secrets/` in the shared tree. | Namespace all artifacts under a per-tenant root derived from tenant context. |
| high | `src/db.py:493-541` (`_spawn_onboard_async`), `pipeline/quarterly_refresh.py`, `execution/onboard_ticker.py` | Background jobs/subprocesses carry no tenant context — child resolves ambient `CIO_USER_ID`, writes global tables/dirs. | Pass `tenant_id` into every spawned command + cron; enforce in child before any I/O. |
| medium | `src/predictions_store.py:111-277`; `src/news/store.py:53-70`; `src/pipeline/queries.py:16-63` | Object-level fetch-by-id/ticker validates no ownership (latent IDOR once multi-tenant). | Require tenant context in every store entry point; add to WHERE; validate ownership on id-based mutations. |
| medium | whole codebase; `db.py:597-632` | No cross-tenant negative tests and no environment to run them; offboarding leaves all FMP JSON/transcripts/rows on disk — no purge. | Add A-vs-B isolation tests (with `qa-test-strategy`); define tenant-scoped offboarding purge (with `data-engineer`, `legal-compliance`). |

## Out of scope
Tenant data-model / pool-vs-silo → `backend-multitenancy`. AuthN + principal → `sec-authz`. Retention/purge policy → `legal-compliance`. Prompt-injection beyond per-tenant scoping → `sec-llm`.
