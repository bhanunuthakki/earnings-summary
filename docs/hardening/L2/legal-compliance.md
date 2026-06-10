# legal-compliance — L2 (Multi-tenant Beta) audit

**Date:** 2026-06-08 · **Gate:** L2 `B` (blocking) · **Mode:** AUDIT (read-only) · **Verdict: 🔴 BLOCK**

> Surfaces legal/regulatory obligations and readiness gaps; **not legal advice** and does not clear the items. High-risk items need qualified counsel.

Framing: the system is architecturally single-tenant today (one `CIO_USER_ID`, unauthenticated localhost server — see `comments_server.py:175`). Admitting real beta tenants means third parties' financial PII (holdings, watchlists, theses, and via Plaid: cost basis / unrealized P&L / account names / tax treatment) enters a system with no tenant boundary, no privacy policy, and a data-rights posture the upstream vendor's terms appear to prohibit.

## Findings

| Severity | Area | Finding | Recommended action |
|---|---|---|---|
| critical | Data-rights / FMP license | FMP standard/personal tiers are for individual non-commercial use and forbid third-party access/resale/redisplay; multi-tenant redisplay needs a separate **FMP Data Display & Licensing Agreement** (enterprise, custom-quoted). Every report redisplays FMP data on one shared key. | Confirm tier with FMP in writing + sign the redisplay/licensing agreement before any external tenant; re-verify yfinance + SEC XBRL for commercial redisplay. Hard BLOCK until signed. |
| critical | Privacy program | No privacy policy, lawful basis, data inventory/classification, or DPA register anywhere. Financial PII processed for identifiable users without these = GDPR/CCPA exposure at first real user. | Publish a privacy policy before signups; define lawful basis; produce a PII→store→subprocessor→retention inventory. |
| critical | Subprocessors / DPAs | User content → Anthropic (Claude CLI) with automatic **Gemini fallback** (same PII to a 2nd processor), plus WebSearch/WebFetch; metered-API default plane has different retention/training terms than enterprise; no disclosure, no DPAs. | Enumerate every subprocessor (Anthropic, Google, FMP, yfinance, Plaid) in a public list; execute DPAs; pursue zero-retention/no-training terms before real PII flows. |
| critical | Data-subject rights | No deletion or export path exists; PII fanned across SQLite tables + loose JSON (`data/report_comments/`, `report_chats/`, `micro_thesis/holdings/`, ledger/sizing/KPI tables). GDPR/CCPA erasure/access currently unfulfillable. | Build per-tenant export + hard-delete across DB rows and on-disk JSON; depends on a real tenant key (see isolation). |
| high | Tenant isolation (legal consequence) | No tenant boundary to enforce rights/confidentiality; unauthenticated server; chat grants the model read of the entire `data/`/`micro_thesis/`/`transcripts/` tree → in a shared deploy every tenant's PII exposed; breach scope = "all users". | Treat authN/Z + per-tenant partitioning as a legal prerequisite (owned by isolation/authz/multitenancy). |
| high | Plaid-derived financial data | `src/integrations/portfolio_tracker_client.py` ingests positions, cost basis, P&L, account names, tax treatment; flows into LLM prompts (9 files ref cost_basis/pnl/tax_treatment). Plaid imposes its own consent/disclosure/deletion/purpose-limitation duties — none reflected. | Confirm the Plaid end-user consent + policy chain; decide explicitly if Plaid data is in L2 scope; if in, the regulatory bar rises materially. |
| high | Retention & residency | No retention schedule or residency control for PII; only `sweep_output_history.py` prunes generated HTML; store is local SQLite + JSON on a Drive-synced Windows path → residency undefined; `.tmp/cron_logs/` may hold user context. | Define + enforce a retention schedule per PII class; state a residency commitment matching actual storage. |
| medium | Breach notification + access auditing | No breach-notification plan; `0054_audit_columns` is data provenance, not who-accessed-what — a breach can't be scoped/notified within statutory windows. | Draft a breach runbook (detection→scoping→clocks→templates); add access logging once a tenant boundary exists. |
| medium | Cookie/analytics consent | Not triggered today (file:// + localhost, no analytics); becomes a consent obligation once served over a network with cookies/analytics. | Flag for when beta leaves localhost: consent banner + analytics lawful basis. (Advisory.) |

## Minimum to clear L2
(a) Written FMP confirmation + signed redisplay/licensing agreement; (b) published privacy policy + lawful basis + data inventory; (c) subprocessor list + executed DPAs (Anthropic, Gemini-fallback, FMP, yfinance, Plaid) with retention/no-training confirmed; (d) per-tenant export + hard-delete (depends on a real tenant boundary).

## Out of scope
Tenant-isolation/authN implementation → `sec-tenant-isolation`/`sec-authz`/`backend-multitenancy`. Code-level PII controls + encryption-at-rest → `sec-appsec`. LLM data-plane controls → `sec-llm`. Formal legal sign-off + contract execution → counsel.

**Sources (FMP data-rights):** FMP Terms of Service; FMP Pricing; FMP enterprise/data-API licensing guidance (site.financialmodelingprep.com).
