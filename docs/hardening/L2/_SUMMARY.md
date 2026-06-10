# Hardening — L2 (Multi-tenant Beta) gate summary

**Date:** 2026-06-08 · **Current rung:** L1 · **Target:** L2 · **Mode:** audit-only (no fixes applied) · **Verdict: 🔴 BLOCKED**

The project is a strong **single-user** application. L2 asks "is it safe to admit multiple real tenants?" — and the answer is no, comprehensively, because single-user assumptions are baked into identity, data model, control plane, and data-rights. This is the expected result for a tool that has never been multi-tenant; the value is the precise blocker list below.

## Gate results

| Expert | Gate | Verdict | crit / high |
|---|:--:|:--:|:--:|
| sec-appsec | B | 🔴 BLOCK | 2 / 2 |
| sec-authz | B | 🔴 BLOCK | 3 / 2 |
| sec-tenant-isolation | B | 🔴 BLOCK | 4 / 2 |
| sec-llm | B | 🔴 BLOCK | 1 / 3 |
| backend-multitenancy | B | 🔴 BLOCK | 2 / 3 |
| infra-devops | B | 🔴 BLOCK | 2 / 3 |
| infra-sre | B | 🔴 BLOCK | 1 / 4 |
| legal-compliance | B | 🔴 BLOCK | 4 / 3 |
| data-engineer | ↻ | FINDINGS-LOGGED | 0 / 2 |

## The five blocker clusters (everything traces to these)

1. **No authenticated identity.** The Flask control plane (`execution/comments_server.py`) has no authN/authZ on any route — including state-changing `/actions/*` (spawn subprocesses), budget overrides, and `/chat/<ticker>/apply` (writes files). `user_id` is read from an unauthenticated `?user_id=` query param, defaulting to `"bhanu"` (`src/identity.py:16`). *(sec-authz, sec-appsec)*
2. **No tenant boundary.** ~90% of tables have no `user_id`; identity is a process-global env var, not per-request; on-disk artifacts (`data/historical/fmp/`, `transcripts/`) are ticker-keyed and globally shared; the cross-portfolio synthesis lenses and the LLM-artifact cache aggregate/serve across all tenants. ~98 files use hand-rolled SQL with no central choke point to add scoping. *(backend-multitenancy, sec-tenant-isolation, data-engineer)*
3. **LLM apply-path agency.** `/chat/<ticker>/apply` writes a request-body diff to disk without checking it against the model's stored proposal, into a scope that includes `directives/` (pipeline control specs) — and that content is read back into later prompts (injection-persistence loop). Untrusted external content (filings, transcripts, IR docs) is interpolated into prompts with zero delimiting. *(sec-llm)*
4. **Not operable as a service.** No IaC, no environments, no deploy/rollback, no feature flags; SQLite without WAL risks data loss under the concurrent scheduled jobs; no automated/tested backup-restore; Gemini fallback has no timeout (unbounded hangs); logging isn't centralized/correlated. *(infra-devops, infra-sre)*
5. **Data-rights + privacy.** FMP's terms appear to prohibit multi-tenant redisplay without a separate commercial Data Display/Licensing Agreement — a potential hard stop. Plus: no privacy policy, no subprocessor DPAs (Anthropic + Gemini-fallback + FMP + yfinance + Plaid), no data-subject deletion/export path, no retention schedule. Plaid-derived cost-basis/P&L raises the bar further. *(legal-compliance, data-engineer)*

## Credited — real L1 strengths (do not regress)
Per-row lineage/provenance (`alembic 0054`: extracted_by / supersedes_id / source_quality_tier) with a restatement detector; LLM cost ledger (`llm_call_ledger.py`); `log_redact.py` applied at FMP error sites with `from None`; parameterized SQL throughout; reversible Alembic migrations (single head); ruff + pyright-strict-ratchet + pytest CI; per-source p50/p95 latency collection; CORS-as-CSRF hardening on the local server; `.env`/`data/secrets/` gitignored.

## Minimum path to clear L2 (dependency order)
1. **Identity first** — a `tenants` table + one canonical id type (the `int 1` vs `str 'bhanu'` split must be reconciled), real authN, `user_id` derived from the principal not the query param, remove the `"bhanu"` default.
2. **Tenancy** — central data-access layer; `tenant_id` on every owned table + mandatory scoping; per-tenant on-disk namespacing; tenant context threaded into jobs/subprocesses; scope the cross-portfolio lenses + LLM cache.
3. **LLM apply-path** — verify applied diffs against stored proposals; drop `directives/` from writable scope; delimit untrusted content in prompts.
4. **Ops** — IaC + staging + rollback + feature flags; enable WAL; automated + tested backups; timeouts on all external calls; centralized correlated logging.
5. **Legal** — sign the FMP commercial/redisplay license; publish privacy policy + lawful basis + data inventory; execute subprocessor DPAs; build per-tenant export + hard-delete.

## Already flagged as standalone fix tasks
- Redact the FMP key leak in `fetch_etf_data.py` (verified true positive).
- Reconcile the split tenant-id (`int 1` vs `str 'bhanu'`) behind a canonical `tenants` table.

*Per-expert detail in the sibling files in this directory. All findings are from read-only audits; no fixes were applied.*
