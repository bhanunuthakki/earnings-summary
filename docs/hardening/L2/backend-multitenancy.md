# backend-multitenancy — L2 (Multi-tenant Beta) audit

**Date:** 2026-06-08 · **Gate:** L2 `B` (blocking) · **Mode:** AUDIT (read-only) · **Verdict: 🔴 BLOCK**

**Depends on:** `data-engineer` (schema foundation). SQLite-as-one-file forces a **pool** model (shared DB, row-level `tenant_id` scoping); silo (DB-per-tenant) would re-pull identical market data per tenant and multiply FMP cost.

## Findings

| Severity | Location | Finding | Recommended fix |
|---|---|---|---|
| critical | `alembic/versions/0004,0002,0013,0038,0041,0065,0035,0045` (facts/documents/dcf/predictions/insider/news/llm/macro) | Entire financial-data substrate has **no tenant column** — only `ticker`. In a pool model every such table is fully cross-tenant. | Add `tenant_id` (NOT NULL, backfill default) to every owned table + leading edge of each natural-key index; explicitly classify shared reference data vs tenant-private. |
| critical | `pipeline/queries.py:178`, `db.py:65`, `user_state/_db.py:25`, + ~60 ad-hoc `sqlite3.connect` across 98 files (~549 execute/connect) | No central data-access layer — three competing connection helpers + dozens of modules with hand-rolled SQL; no single place to inject scoping; retrofit touches ~98 files. | Collapse to one tenant-aware connection/session factory; route every store through it; ban bare `sqlite3.connect`. |
| high | `synthesis/lenses/cross_portfolio_synthesis.py:96-122`; `portfolio_macro_stress.py`; `db.py:648` | Cross-portfolio aggregates select `tracked_companies` (filter only `list_type`) and fan out unfiltered by `user_id` → silent cross-tenant aggregation in synthesis output. | Thread tenant context into every aggregate; filter `tracked_companies` by tenant and propagate downstream; add an A≠B test. |
| high | `tracked_companies.user_id` INTEGER `1` (`db.py:98`, `0028`) vs substrate `user_id` TEXT `'bhanu'` (`0060`–`0063`); `models/companies.py:54` (int) vs `alerts/store.py:80` (str) | Tenant id is two incompatible types in two namespaces for one human; no `tenants` table, no FK, no join — multi-tenant joins impossible. | Introduce a `tenants` table with one PK type; migrate `1` and `'bhanu'` to one id; FK everywhere; reconcile model types. **(Flagged as standalone task.)** |
| high | `alembic/0064_queued_actions.py:73-93`; `alerts/store.py:413-434` | `queued_actions` has no `user_id`; ownership only via JOIN to `alerts`; direct reads unscoped; on approval these **write** to ledger/sizing tables. | Denormalize `user_id` onto `queued_actions`; filter directly in drafter/lister/applier. |
| medium | `src/identity.py:1-16` | Tenant identity is a module-global constant resolved once at import from `CIO_USER_ID`; no request/task-scoped context — one process can't safely serve two tenants. | Replace with an explicit `TenantContext` (or contextvar set per request/task); wire stores from it. |
| medium | `db.py:471-519` (`_spawn_onboard_async`), `db.py:522` (`track_company`), `execution/onboard_ticker.py` | Provisioning is tenant-unaware: onboarding spawns by ticker with no tenant arg; writes shared on-disk artifacts keyed only by ticker → collisions. | Define a tenant-provisioning lifecycle; namespace artifact paths by tenant (or treat captured market data as shared reference); pass tenant through onboarding. |
| advisory | whole repo | No per-tenant config, quotas, or noisy-neighbor controls; `llm_budget`/`fmp_endpoint_status` global — one tenant can exhaust budgets for all. | Per-tenant budget/rate/refresh-queue rows. Lower priority than schema/enforcement. |

## Out of scope
Isolation security proof → `sec-tenant-isolation`. Identity/authz → `sec-authz`. Schema mechanics/migrations → `data-engineer`.
