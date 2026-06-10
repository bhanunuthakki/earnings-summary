# infra-devops — L2 (Multi-tenant Beta) audit

**Date:** 2026-06-08 · **Gate:** L2 `B` (blocking) · **Mode:** AUDIT (read-only) · **Verdict: 🔴 BLOCK**

## Findings

| Severity | Location | Finding | Recommended fix |
|---|---|---|---|
| critical | (entire repo) | No IaC, no deployment automation, no CD. No Dockerfile/Terraform/Pulumi/CDK, no staging, no deploy job. | Add IaC (minimal Docker Compose + deploy script as a first step) + a CI deploy job gating a staging environment before prod. |
| critical | (entire repo); migrations `0027,0031,0057` | No rollback mechanism. No deploy → no rollback; three migrations permanently DROP tables (downgrade restores schema, not data). | Document a deploy runbook; define rollback = pre-deploy SQLite backup + `alembic downgrade` with tested restore; annotate the destructive migrations + require a snapshot first. |
| high | `alembic.ini:3` | DB path hardcoded `sqlite:///data/portfolio.db` (relative to CWD); CI never runs `alembic upgrade head` (ci.yml:51-53) — migration chain never exercised. | Parameterize `sqlalchemy.url` via env var; add `alembic upgrade head` against a temp DB to CI. |
| high | (entire repo) | No environment separation — one SQLite file is dev+test+prod; changes can't be tested without touching live data. | Define dev/staging/prod; config via env vars not hardcoded paths; per-tenant DB/schema isolation for L2. |
| high | `src/identity.py:16` (+ `run_morning_pipeline.py:70`) | Single-user identity hardcoded: `DEFAULT_USER_ID = os.environ.get("CIO_USER_ID","bhanu")` — a second tenant without the env var silently inherits the first tenant's identity. | Remove the `"bhanu"` fallback; raise if `CIO_USER_ID` unset. Prerequisite for L2. |
| medium | `requirements.txt:1-18` | All 18 runtime deps unpinned, no lockfile → non-reproducible builds; silent upstream breakage. | Pin exact versions (`pip freeze`/`uv` lock); `[dev]` `>=` bounds are fine. |
| medium | `fetch_fmp_historical_data.py:41`, `fetch_etf_data.py:202-203`, `fetch_macro_series.py`; `fetch_drug_patent_status.py`, `load_index_constituents.py` | FMP key in URL query param; some fetchers call `raise_for_status` without importing `_redact` → key leak to logs. | Pass key via header if supported, or route all exception strings through `log_redact`; add the import where missing. |
| medium | (entire repo) | No feature flags / kill-switch — a bad LLM prompt or broken endpoint affecting all tenants has no lever short of a deploy. | Add a minimal flag config (checked-in JSON + env override) to gate pipeline stages. |

## Credited / OK
CI (`ci.yml`) runs tests on push/PR, ruff format/lint on changed lines, pyright strict ratchet (fails closed); Makefile mirrors CI; pre-commit runs ruff + pyright; secrets gitignored, never committed; Alembic single head (`0072`), no branch divergence, every migration has a real `downgrade()`; destructive migrations use `IF EXISTS` guards.

## Out of scope
Runtime monitoring/SLOs/DR → `infra-sre`. Test design → `qa-test-strategy`. Vuln-scan content → `sec-appsec`.
