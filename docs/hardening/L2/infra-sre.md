# infra-sre — L2 (Multi-tenant Beta) audit

**Date:** 2026-06-08 · **Gate:** L2 `B` (blocking) · **Mode:** AUDIT (read-only) · **Verdict: 🔴 BLOCK**

## Findings

| Severity | Location | Finding | Recommended fix |
|---|---|---|---|
| critical | `execution/fetch_fmp_historical_data.py:40-52` | No retry on 429/5xx — single `requests.get`; a transient FMP rate-limit returns `None` and the caller silently skips data (sibling fetchers do back off). | Apply the token-bucket + 429 exponential-backoff used in `save_fmp_data.py:_http_get`. |
| high | `src/db.py:687` (`init_db`) | SQLite WAL never enabled — default rollback-journal serializes readers/writers; concurrent scheduled tasks + detached onboarding subprocess hit `database is locked` despite `busy_timeout`. Data-loss/corruption risk. | Add `PRAGMA journal_mode=WAL` + `PRAGMA synchronous=NORMAL` in `get_connection()`. |
| high | `data/` (manual `portfolio.db.bak-*` observed) | Backups manual, ad-hoc, untested — no automated job, no restore drill, no RPO/RTO; single durable store. | Daily scheduled `.backup` with 7-day retention; documented + timed restore drill against an RPO/RTO target. |
| high | all `execution/*` entrypoints | Logging inconsistent/non-centralized — freeform `basicConfig` per script; `run_id` exists in the LLM ledger but isn't propagated into `logging`; no correlation id → at multi-tenant scale incidents are uninvestigable. | Central `logging_config.py` with a JSON formatter + `run_id`/`ticker` injected via `LoggerAdapter`/contextvars across all entrypoints. |
| high | `src/llm/fallback.py:60-121` | Gemini fallback has no timeout — `generate_content` can hang indefinitely; especially damaging in unattended cron jobs with no watchdog. | Pass `request_options={"timeout":120}` or wrap in an executor with a timeout. |
| medium | codebase; `sources/registry.py` | No SLOs, no alerting, no on-call/runbook path — p50/p95 latency + error rates collected but no targets, no alert rules, no `docs/runbooks/`; cron has no failure-notification. | Define availability + FMP error-rate SLOs + LLM-budget alert; wire a failure-notification step to each runner. |
| medium | `fetch_fmp_historical_data.py:40-52`, `save_fmp_data.py:200-225` | Retry backoff has no jitter — parallel tickers/cron jobs wake together and storm FMP. | Add randomized jitter to each wait. |
| low | `data/secrets/gsheets_credentials.json`, `…token.json` | OAuth creds inside `data/` → included in any naive `cp -r data/` backup, widening blast radius. | Store creds outside `data/` (`%APPDATA%`/vault) via the env vars the code already supports. |

## Credited / OK
LLM cost ledger (`llm_call_ledger.py`, `llm/ledger.py`) — never-raises, captures tokens/cost/cache/fallback per call; `log_redact.py` applied at FMP error sites; `sources/registry.py` p50/p95 + error-rate collection; `save_fmp_data.py` token-bucket (12 req/s) + 429 backoff; `db.py` 30s `busy_timeout`; Gemini fallback ledger-instrumented + togglable.

## Out of scope
Pipeline/deploy mechanics → `infra-devops`. Retention policy → `data-engineer`/`legal-compliance`. LLM-call cost logging → `llm-evals-orchestrator`.
