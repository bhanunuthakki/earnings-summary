# data-engineer — L2 (Multi-tenant Beta) re-verify

**Date:** 2026-06-08 · **Gate:** L2 `↻` (re-verify; not a hard blocking gate) · **Mode:** AUDIT (read-only) · **Status: FINDINGS-LOGGED** (0 critical, 2 high)

The two highs are pre-conditions for *safe multi-tenancy*, not current single-user data corruption — but both must be resolved before a second `user_id` is ever written.

## Findings

| Severity | Location | Finding | Recommended fix |
|---|---|---|---|
| high | `db.py:119` (`tracked_companies`) vs `alembic 0060:68,0061:65,0062:74,0063:84` | Split `user_id` type — `tracked_companies.user_id` INTEGER `1` vs substrate tables TEXT `'bhanu'`; `Company` model is `int`, `AlertRow` is `str`; not co-comparable. Cross-table "delete all data for user X" needs a coercion that silently returns 0 rows if omitted. | Migrate `tracked_companies.user_id` to TEXT `'bhanu'` (backfill `1`→`'bhanu'`); update `Company.user_id`→str + all `db.py`/`queries.py` callers. Prereq for any tenant-wide sweep. **(Flagged as standalone task.)** |
| high | `db.py:597-611` (`remove_company`) + all fact tables | No cascading deletion path — `remove_company` deletes only the `tracked_companies` row; no code removes rows from documents/financial_facts/kpi_facts/segment_facts/alerts/ledger/sizing/registry/ingestion_runs/etc. RTBF/offboard has no executable script (20+ tables). | Implement `purge_tenant_data(user_id, ticker)` (and `purge_user` for RTBF) with FK-safe cascading deletes behind `--confirm`. |
| medium | `alembic/0067_ticker_settings.py:43` | `ticker_settings` has no `user_id` (keyed on ticker only) — two tenants share a ticker's `bypass_budget` flag. | Add `user_id TEXT NOT NULL DEFAULT 'bhanu'`; unique on `(user_id, ticker)`. |
| medium | `pipeline/kpi_persistence.py:269-284` | `purge_duplicate_kpi_facts` dedups by `source_doc_id` order, not `source_quality_tier` — a later LLM-extracted row can beat an earlier SEC XBRL fact, defeating 0054's tier column; `metrics`/`ratios` views also order by `source_doc_id DESC`. | Order by `source_quality_tier_rank DESC, source_doc_id DESC` (join `documents`); define the rank constant map. |
| medium | `alembic/0064_queued_actions.py`; `alerts/store.py:441` | `queued_actions` no direct `user_id`; `get_action`/`list_actions_for_alert` fetch by PK/alert_id with no user check → authorization gap once multi-tenant. | Add a `user_id` guard (join `alerts`) or denormalize `user_id` onto `queued_actions`. |
| medium | DB / no config found | No backup/restore procedure documented or tested; single SQLite file; `busy_timeout=30s` confirms concurrent writers — corruption blast radius. | Daily `.dump`/online-backup + verified restore runbook (shared with `infra-sre`). |
| low | `src/identity.py:16` | `DEFAULT_USER_ID` resolved at import time — a process importing `identity` before setting `CIO_USER_ID` silently uses `'bhanu'` forever. | Make it a callable `get_default_user_id()` reading the current env. |
| info | `alembic/0054_audit_columns.py` | **Credited:** `extracted_by`/`supersedes_id`/`confidence`/`source_quality_tier` (with backfill) + the restatement detector preserving incumbent+restated rows = L2-grade lineage and point-in-time support. | Maintain; no action. |

## Out of scope
App query security → `sec-appsec`. Tenant-isolation enforcement → `sec-tenant-isolation`. Backup/restore operation + SLOs → `infra-sre`.
