# Earnings Summary

A research-grade pipeline that turns raw filings, earnings calls, IR documents, and market data into a **per-ticker analyst workspace** — a self-contained HTML report with tabbed views over thesis, financials, segments, earnings, news, valuation, bear case, and provenance. The system ingests data on a daily cron, evaluates it against per-ticker break rules, regenerates only when material has changed, and exposes a commentable + chattable interface for iterating with the analyst.

Active universe lives in `tracked_companies` (DB-driven, not hardcoded) with four list types: `portfolio` (P1 tier, daily refresh), `watchlist` (P2, weekly), `evaluation` (P2, weekly with 7-day skip), and `archived`. Per-ticker thesis specs live as JSON in [`micro_thesis/holdings/`](micro_thesis/holdings/).

The **dashboard command center** — `python execution/comments_server.py` → `http://127.0.0.1:7421` — is the primary interface: portfolio + per-ticker status, cross-ticker analytics (budget, decisions, trigger ladder), a per-ticker drill-down (artifacts, analyses-ran, thesis, live position), refreshes with per-step/force overrides, and comment + thesis editing with a preview→apply diff. It also deep-links each ticker into the companion **portfolio-tracker** app. See [HOW_TO_USE_REPORTS.md → Command center](HOW_TO_USE_REPORTS.md#command-center-start-here).

This README is the system overview + user manual. Two companion docs:

- **[HOW_TO_USE_REPORTS.md](HOW_TO_USE_REPORTS.md)** — day-to-day analyst workflow: the command center, slash-keyword shortcuts, comment processor, refresh-vs-rebuild matrix, onboarding a new ticker.
- **[cron/SETUP_WINDOWS_SCHEDULER.md](cron/SETUP_WINDOWS_SCHEDULER.md)** — one-time Windows Task Scheduler install for every cron.

---

## Table of contents

1. [Architecture at a glance](#architecture-at-a-glance)
2. [Repository layout](#repository-layout)
3. [Setup](#setup)
4. [The deliverable: workspace report](#the-deliverable-workspace-report)
5. [User-facing components](#user-facing-components)
6. [Cron jobs and automation](#cron-jobs-and-automation)
7. [When the report auto-updates](#when-the-report-auto-updates)
8. [How data is picked and validated](#how-data-is-picked-and-validated)
9. [Common workflows](#common-workflows)
10. [State, idempotency, and resumption](#state-idempotency-and-resumption)
11. [Pre-push checklist](#pre-push-checklist)
12. [Security](#security)

---

## Architecture at a glance

**Three layers**, enforced by [GEMINI.md](GEMINI.md):

1. **Directives** (`directives/`) — Layer 1 SOPs. One Markdown file per task type. Immutable without authorization. These define schemas, refresh cadences, idempotency keys, and failure-mode policy.
2. **Orchestration** — Layer 2. Routes between executions, reads stdout/stderr, manages state. No business logic.
3. **Execution** (`execution/`) — Layer 3 single-purpose Python CLIs with typed Pydantic I/O. ~85 scripts, each a single CLI entrypoint.

**The canonical 8-stage pipeline** ([`directives/data_pipeline_dag.md`](directives/data_pipeline_dag.md)):

```
INGEST → TRANSCRIBE → PARSE → VALIDATE → PERSIST → COMPUTE → SYNTHESIZE → PUBLISH
```

Each stage is idempotent, resumable from `stage_transitions`, and writes typed outputs to either the DB or `.tmp/`.

**Three trigger modes** drive when work happens:

- **Cron** — 6 daily + 2 weekly + 1 monthly + hourly catch-up. The daily 03:00→06:30 chain refreshes data; the daily 06:30 worker drains a queue of "dirty" tickers and regenerates briefs.
- **Comment-driven** — when the analyst applies a comment with `--apply`, the comment processor edits holdings JSON, re-runs the affected stages synchronously, and rebuilds the brief inline.
- **Manual CLI** — every step has a direct invocation. `.bat` launchers wrap the most common ones for cmd.exe.

**Data flow** (single-ticker view):

```
                      (manual: _inbox/PDFs)
                              │
INGEST ──┬── FMP fetchers ────┼── SEC XBRL fetcher ── transcripts (yt-dlp + whisper)
         │                    │
        triggers (migration 0026): financial_facts/segment_dimensions/kpi_facts INSERT
                              │     ↓
         tracked_companies.brief_dirty = 1
                              ↓
[Daily 06:30 cron]   daily_fetch_and_brief.py
                              ↓
                Gate A (tier-cadence) → Gate B (material-change hash) → Gate C (eval-cadence)
                              ↓
              thesis_evaluator → refresh_dcf → build_artifacts (workspace HTML + MD + JSON + DCF xlsx)
```

---

## Repository layout

| Path | Purpose |
|---|---|
| `src/db.py`, `src/parser.py`, `src/intake.py`, `src/ir_uploads.py`, `src/comments.py`, `src/chat_session.py` | Core utilities — DB session, document parsing, _inbox intake, IR upload registry, comments/chat persistence |
| `src/llm/` | LLM wiring: `cli.py` (Claude CLI subprocess wrapper), `anchors.py` (reusable thesis + bear-case context blocks), `fallback.py` (Gemini fallback when Claude fails), `ledger.py` (call accounting) |
| `src/report/` | Report generator. `builder.py` produces a typed `ReportSpec`; `models.py` defines the section schemas; `sections/` builds each section; `renderers/` emits HTML/Markdown/JSON/Excel + the workspace tabbed renderer + the chat/comments overlay |
| `src/report/renderers/workspace_*.py` | The analyst workspace renderer — splits into `workspace_html.py` (tab layout), `workspace_styles.py`, `workspace_script.py`, `workspace_data.py` (data hand-off to JS), `workspace_charts.py`, `workspace_comments.py`, `workspace_chat.py` |
| `src/report/sections/` | One file per report section: `snapshot`, `thesis`, `financials`, `segments`, `earnings`, `recent_developments`, `valuation`, `bear_case`, `qa_roster`, `saydo`, `ir_docs`, `filing_intelligence`, `portfolio_position`, `exec_compensation`, `evaluation_snapshot`, `etf_holdings`, `provenance`, `appendix`, `synthesis` |
| `src/compute/` | Deterministic financial computations: `income_statement`, `balance_sheet`, `cashflow`, `as_reported`, `segments`, `segment_definitions`, `segment_oi_10k`, `segment_crosstabs_llm`, `company_description`, `say_do` + `say_do_extractor`, `thesis_evaluator`, `soft_rule_evaluator`, `holding_scorecard`, `kpi_extract_summaries`, `valuation_basis`, `dcf`, `earnings_surprise` |
| `src/dcf/` | DCF subsystem: `workbook_reader` (extract FCF stream from xlsx), `valuation` (PV/share + over-under %), `forecast` (auto-derive forecast assumptions from history), `live_price` (FMP profile lookup), `seeder` + `refresher` (workbook lifecycle), `persist` (upsert `dcf_runs`) |
| `src/pipeline/` | Pipeline plumbing: `source_routing`, `run_accounting`, `queries`, `kpi_persistence`, `sec_xbrl`, `quarterly_refresh`, `restatement_detector`, `validation_engine`, `tier_runner`, `segment_junction_writer`, `dashboard_html`/`dashboard_status`, `analytical_dashboard`, `refresh_eval` |
| `src/synthesis/lenses/` | LLM "lenses" — one per analytical perspective: `five_min_reread`, `thesis_drift_qoq`, `bull_case`, `cross_portfolio_synthesis`, `mgmt_credibility_score`, `reverse_dcf`, `macro_scenario`, `portfolio_macro_stress`, `catalyst_calendar`, `customer_concentration_risk`, `filing_diff_narrative`, `footnote_anomaly`, `underweighted_facts`, `llm_calibration` |
| `src/timeseries/` | Time-series intelligence layer: `loaders` (tier-aware fact loaders + restatement chains + as-of-date time travel), `primitives` (trend/inflection/zscore/decompose/correlation/YoY acceleration), `signal_writer` |
| `src/sources/` | External-source adapters: `earnings_calendar` (FMP + yfinance), `price` (live quote), `registry` |
| `src/table_extractors/` | Document table extractors: `customer_concentration`, `lease_commitments`, `investor_decks` |
| `src/models/` | Pydantic schemas: `documents`, `facts`, `kpis`, `fmp_payloads`, `patents`, `runs`, `validation`, `companies`, `instruments`, `ir_uploads`, `artifacts` |
| `execution/` | ~85 CLI entrypoints. See [User-facing components](#user-facing-components) and [Cron jobs](#cron-jobs-and-automation) for the active subset |
| `directives/` | Layer 1 SOPs. Highlights: `data_pipeline_dag.md`, `data_provenance.md`, `quarterly_refresh.md`, `intake_documents.md`, `report_comments_and_chat.md`, `platform_backlog.md` (cross-workspace bug/feature tracker) |
| `micro_thesis/holdings/` | Per-ticker thesis JSONs (schema v2: thesis + tier-1/2/3 KPIs + break rules + WACC + MoS bar + DCF defaults + business_model_rules) |
| `micro_thesis/sources/` | Per-ticker drop folders for review documents |
| `micro_thesis/diligence/` | P2 diligence markdown per evaluation candidate |
| `dcf/` | Canonical per-ticker DCF workbooks (`<TICKER>.xlsx`, user-edited; system refreshes historicals only) |
| `data/ticker_specific/<TICKER>/` | Per-ticker custom research feeds (e.g. NVO patent timeline) consumed by §10 Bear Case prompt |
| `data/historical/fmp/` | FMP JSON cache (per ticker × endpoint) — gitignored, reproducible |
| `data/sec/` | SEC XBRL cache |
| `data/bear_case/`, `data/valuation_basis/`, `data/company_description/`, `data/qa_topics/` | LLM-output caches (SHA256-keyed; rebuilt on input change) |
| `data/surprise/`, `data/report_comments/`, `data/report_chats/` | Surprise ledger + per-report comment/chat stores |
| `data/portfolio.db` | SQLite store — facts, KPIs, segments, transcripts, validation issues, thesis evaluations, DCF runs, comments. Migrations in `alembic/versions/` (head: `0057_drop_segment_facts`) |
| `cron/` | Windows Task Scheduler XMLs + `.bat` wrappers for the 9 scheduled tasks |
| `tests/` | Pytest suite — compute modules + pipeline contracts |
| `transcripts/raw/`, `transcripts/processed/` | Earnings transcript flow (gitignored) |
| `ir_documents/` | IR PDFs filed by ticker × period (gitignored) |
| `output/research/<TICKER>/` | Generated brief artifacts (`<DATE>_workspace.html` etc.) — primary deliverable |
| `output/earnings_calendar.html` | Portfolio + watchlist earnings calendar |
| `output/dashboard/` | Portfolio analytical dashboard outputs |
| `.tmp/` | Ephemeral state — parsed payloads, indexes, pressure-test audits, lens cache, cron logs (gitignored) |

---

## Setup

Requires Python ≥3.11.

```bash
pip install -r requirements.txt
pip install -e ".[dev]"      # adds pytest, alembic, ruff, pyright, basedpyright
alembic upgrade head         # initialize data/portfolio.db (head: 0057_drop_segment_facts)
```

Create `.env` with whichever providers you need:

```env
ANTHROPIC_API_KEY=...        # Claude CLI (metered API billing — the default for this project)
GEMINI_API_KEY=...           # automatic fallback when the Claude CLI fails
FMP_API_KEY=...              # fundamentals, statements, calendar, transcripts
FMP_TIER=premium|starter|basic    # rate limits — defaults to "basic" (250/day) if unset
```

Claude calls go through the `claude` CLI as a subprocess (see [src/llm/cli.py](src/llm/cli.py)). The CLI honors whichever auth is configured — set `ANTHROPIC_API_KEY` for metered billing, or `claude auth login` for Pro/Max subscription. For this repo, **API key is the documented default** — don't unset it.

Then install the cron tasks: see [cron/SETUP_WINDOWS_SCHEDULER.md](cron/SETUP_WINDOWS_SCHEDULER.md).

---

## The deliverable: workspace report

Every ticker gets a per-day workspace at `output/research/<TICKER>/<YYYY-MM-DD>_workspace.html` — a self-contained HTML file that opens in any browser via `file://`. Tabs and their data sources:

| Tab | What it shows | Data source |
|---|---|---|
| **Thesis** (portfolio/watchlist flavor) | Investment lede + tier-1 KPI status table + break-rule evaluation + competitive watchlist | `micro_thesis/holdings/<T>.json` + `kpi_facts` + `thesis_evaluations` |
| **Eval Screen** (evaluation flavor) | 3y quick-categorization data table | `financial_facts` + `key_metrics` |
| **Earnings** | Per-quarter analytical notes (newest first) + Q&A roster + commentary | `transcripts` + LLM-summarized `.tmp/<T>_Q<n>_<Y>_summary.txt` |
| **News** | Last 7 days, ranked by thesis KPI impact, each item tags which tier-1 KPI it touches | Claude WebSearch / WebFetch with thesis anchor injected |
| **Say · Do** | Print-vs-guide for most recent quarter pair + trajectory verdict bar | `management_commitments` (extracted from transcripts) + Q-on-Q outcome match |
| **Financials** | 12-quarter YoY% heatmap (line items, segments, geographies, OI, tracked KPIs) + 12Q level table + segment drill-down | tier-aware load from `financial_facts` + `segment_dimensions` |
| **Valuation** | Diagnostic multiple (Opus-picked, override-able), current value, 12Q sparkline, range, rich/cheap verdict, DCF over-under %, trim/sell trigger badge | `valuation_basis` cache + `dcf_runs` + live FMP price |
| **Bear case** | `most_underweighted` callout + named failure modes — each card has Evidence / Leading indicator / Quant impact / Refutation | LLM call grounded on thesis anchor + per-ticker enhancements |
| **Company** | Business overview + revenue mechanics + segments + geographies + IR doc summaries | `data/company_description/<T>.json` overlaid with current segment shares |
| **Position** (when held) | Shares, cost basis, P&L, transactions, open vs closed decisions | `portfolio_positions` + `decisions` |
| **Sources** (Provenance) | Coverage matrix, validation issues, source-doc audit, restatement chain links | `quarterly_artifacts` + `documents` + `validation_issues` |

A matching `<DATE>_report.md` + `<DATE>_sections.json` are also emitted on every build. The workspace HTML is the only HTML report — the legacy non-tabbed `<DATE>_report.html` renderer was retired.

---

## User-facing components

Every direct interaction with the system goes through one of these.

### 1. Workspace HTML report

Open `output/research/<TICKER>/<YYYY-MM-DD>_workspace.html` in any browser (double-click in Explorer, drag into Chrome). Self-contained — no server needed for read-only viewing. Tabs as listed above.

**Inline commenting** lights up when the comments server is running (see §2). Until then, the report is read-only with no overlay.

### 2. Comments + chat server (Flask, localhost:7421)

Start it with `start_comments_server.bat` (or `python execution/comments_server.py`). Endpoints under `localhost:7421`:

- `GET /` — **Dashboard**: read-only portfolio status table. Columns per ticker: last FMP pull (relative), last transcript quarter (with Q&A marker), last build mtime, open comment count, breach state badge (intact/watch/broken/pending), Open↗ link to the latest workspace report. Two tables (Portfolio + Evaluation; watchlist hidden). Sources: `tracked_companies`, `fmp_endpoint_status`, `transcripts`, `output/research/` mtime, `report_comments`, `thesis_evaluations`.
- `GET /reports/<ticker>` — Serves the latest `<DATE>_workspace.html` for the ticker.
- `POST /comments` — Create a new comment with `(ticker, report_date, anchor, text, selected_text, intent)`.
- `GET /comments?ticker=&report_date=` — List comments.
- `PATCH /comments/<id>` — Update status / resolution / intent.
- `DELETE /comments/<id>` — Hard delete.
- `POST /chat/<ticker>` — Streaming chat (SSE) with Claude Sonnet 4.6 loaded with thesis + bear case + valuation + company description context, plus read-only filesystem access to `data/`, `micro_thesis/`, `.tmp/`, `transcripts/`.
- `POST /chat/<ticker>/apply` — Apply a chatbot-proposed diff to disk.
- `POST /actions/refresh` — Trigger an on-demand per-ticker refresh dispatcher (`stale` or `full` mode; SSE-streamed line-by-line).
- `GET /healthz` — Health check.

Comments persist to `data/report_comments/<T>/<YYYY-MM-DD>.json`; chat threads to `data/report_chats/<T>/<YYYY-MM-DD>.json`. Posting / chat needs the server running; viewing existing comments + highlights works offline.

**Slash-keyword intents** (see [HOW_TO_USE_REPORTS.md §Slash-keywords](HOW_TO_USE_REPORTS.md#slash-keyword-shortcuts-fastest-path--skip-the-dropdown) for the full table):

| Prefix | Routes to | What the processor does |
|---|---|---|
| `/kpi` | `drop_kpi` | Removes the KPI from `micro_thesis/holdings/<T>.json` |
| `/thesis` or `/update` | `edit_thesis` | Opus revises the thesis paragraph |
| `/ask` or `/q` | `ask_question` | Opus answers with full thesis + bear-case context |
| `/fix` | `fix_data` | Logs a TODO in `directives/data_fixes.md` |
| `/rewrite` | `rewrite_section` | Emits cache-invalidation instructions for the targeted section |
| `/platform`, `/feature`, `/bug` | `platform_change` | Lands as a tagged entry in [`directives/platform_backlog.md`](directives/platform_backlog.md) — the canonical cross-workspace bug/feature tracker. NOT routed into a single-ticker brief edit |

### 3. Earnings calendar HTML

`output/earnings_calendar.html`, regenerated by `python execution/build_earnings_calendar.py`. One self-contained page covering every portfolio + watchlist ticker, split into:

- **Upcoming** (next 90 days) — sorted by date, portfolio rows pinned + amber-shaded
- **Recently reported** (last 45 days)
- **No calendar data** (collapsed)

Each row links to the most recent brief. Overwritten in place on every run.

### 4. Analytical dashboard

`output/dashboard/<DATE>_portfolio_dashboard.html`, built by `python execution/build_analytical_dashboard.py`. Cross-ticker view: portfolio-wide synthesis, per-holding 5-min rereads, decisions ledger (hit-rate strip + graded outcomes), trigger ladder (SELL/TRIM/HOLD/ADD), cross-ticker insider activity (90d, conviction-scored), prediction outcomes (SayDo / bear-case / risk-factor materialization), LLM budget panel. Regenerated by the **weekly_synthesis** cron (Sunday 23:00).

### 5. `.bat` launchers (repo root — cmd.exe-friendly)

| Launcher | Wraps | Use |
|---|---|---|
| `build_report.bat <T> [--enable-llm]` | `execution/build_artifacts.py` | Build the workspace report. `--enable-llm` re-runs bear case + news + valuation + company description |
| `refresh_fmp.bat <T> [LIMIT]` | `execution/fetch_fmp_historical_data.py` | Pull fresh FMP financial data |
| `refresh_transcripts.bat <T>` | `execution/backfill_transcripts.py` | Backfill last 6 quarters of transcripts |
| `refresh_news.bat <T> [DAYS]` | `execution/refresh_news.py` | Force-refresh §News with fresh WebSearch |
| `full_refresh.bat <T>` | Orchestrates 6 steps | FMP → transcripts → IR processing → KPI extract → SayDo → workspace report |
| `start_comments_server.bat` | `execution/comments_server.py` | Flask server on `:7421` (dashboard + comments + chat) |
| `process_comments.bat <T> [--apply] [--clear]` | `execution/process_report_comments.py` | Drain open comments → edits + LLM calls + rebuild |

Every `.bat` self-locates the repo, so you can run them from any cwd. They forward all args after `<T>` to the underlying script.

### 6. Direct Python CLIs

Every step is also a direct Python entrypoint. See [HOW_TO_USE_REPORTS.md §Full CLI reference](HOW_TO_USE_REPORTS.md#full-cli-reference) for the user-invocable subset. Cron-only scripts are in §[Cron jobs](#cron-jobs-and-automation) below.

---

## Cron jobs and automation

Nine scheduled tasks. Installation: [cron/SETUP_WINDOWS_SCHEDULER.md](cron/SETUP_WINDOWS_SCHEDULER.md). All run as `InteractiveToken` under `%USERNAME%`, log to `.tmp/cron_logs/<task>_<TS>.log`, and are registered under the `\earnings-summary\` namespace in Task Scheduler.

### Daily chain (03:00 → 06:30)

The five daily tasks run as a chain. The 90/75/30/15-minute gaps absorb slow upstream responses and let each step's writes commit before the next reads.

| Task | Cadence | Script | What it does |
|---|---|---|---|
| `refresh_cache` | Daily 03:00 | `execution/refresh_cache.py run` | **Tier-aware FMP refresh queue.** Reads `FMP_TIER` from `.env` (`basic`=250/day, `starter`=unlimited @ 5/sec, `premium`=unlimited @ 12/sec). Drains highest-priority stale endpoints up to the daily cap. Failed endpoints (403 / Legacy) get a 30-day retry window — a downgrade builds a backlog automatically; an upgrade catches up over following days. Force-stale hints from `schedule_pre_earnings_refresh` override cadence for tickers reporting in the next 7 days |
| `backfill_transcripts` | Daily 04:30 | `execution/backfill_transcripts.py` | For every active ticker, fetches the last 6 fiscal quarters of Q&A from the free aggregator chain (roic.ai → stockanalysis.com → tickertrends.io), ingests, extracts forward-looking commitments. Idempotent — file-exists check + sha256 dedup |
| `fetch_fmp_earnings_calendar` | Daily 05:45 | `execution/fetch_fmp_earnings_calendar.py --all` then `execution/refresh_expected_earnings.py` | Step 1 refreshes `data/historical/fmp/<TICKER>_earnings_calendar.json` for every portfolio + watchlist + evaluation ticker (on free/basic tier FMP refuses — 402 since 2026-06-10 — and the cache stays at its last good state). Step 2 materializes the **canonical `expected_earnings` table** through the `next_earnings_date` stack in [src/sources/earnings_calendar.py](src/sources/earnings_calendar.py) (FMP cache → yfinance fallback); the Home rail's upcoming-earnings strip, cockpit, and portfolio-tracker bridge all read that table |
| `backfill_earnings_surprises` | Daily 06:15 | `execution/backfill_earnings_surprises.py` + `ingest_earnings_surprises.py` | Two-stage: merges `<TICKER>_earnings_calendar.json` (FMP primary, EPS + Revenue surprise) with `yfinance.Ticker.earnings_dates` (fallback, EPS-only) into `data/surprise/<TICKER>_surprises.json`, then upserts into `earnings_surprises`. Stage-2 gate prevents partial ingestion if stage-1 fails |
| `daily_fetch_and_brief` | Daily 06:30 | `execution/daily_fetch_and_brief.py --enable-llm` | **The drainer.** Picks up every ticker with `brief_dirty=1`, applies three gates (see §[When the report auto-updates](#when-the-report-auto-updates)), runs `thesis_evaluator → match_commitments → refresh_dcf → build_artifacts` for un-skipped tickers, clears the flag |

### Hourly catch-up

| Task | Cadence | Script | What it does |
|---|---|---|---|
| `onboard_pending` | Hourly at :17 | `execution/onboard_pending_tickers.py` | Idempotent belt-and-suspenders for tickers that bypassed `db.track_company`'s auto-onboard hook (raw SQL / external API inserts). Detects 5 pending reasons (no instrument_type, no financial_facts, no dcf_run, etc.) and runs the appropriate fetch chain. No-op when nothing is pending |

### Weekly + monthly

| Task | Cadence | Script | What it does |
|---|---|---|---|
| `weekly_p2_lens_refresh` | Sunday 02:00 | `execution/run_due_lenses.py --cadence weekly` | Regenerates P2-tier (watchlist + evaluation) lens artifacts drifted past their cadence. Idempotent via `artifact_store` cached-inputs hash — stable tickers cost nothing |
| `weekly_synthesis` | Sunday 23:00 | 5-step pipeline | (1) `refresh_dirty_artifacts.py --manifest-only` to drain dirty LLM artifacts; (2) per-portfolio `run_lens.py --all`; (3) `cross_portfolio_synthesis` Opus lens; (4) `build_analytical_dashboard.py`; (5) `grade_bear_cases.py --all-portfolio` for predictions whose target_period has passed |
| `monthly_p3_refresh` | 1st of month, 03:00 | `execution/run_due_lenses.py --cadence monthly` | P3-tier (index constituents / ETFs / no-tier) lens refresh. P3 lens set is minimal (`five_min_reread` only) so runtime stays bounded even with 2k+ index constituents |

### Supplementary / not on a cron

| Script | Trigger | What it does |
|---|---|---|
| `schedule_pre_earnings_refresh.py` | Called from `refresh_cache.py` daily startup (~23h TTL) | Polls FMP for tickers reporting in next 7 days, falls back to per-ticker `next_earnings_date()` for FMP-universe misses. Writes force-stale hints to `.tmp/cacher/forced_stale.json` (14-day TTL covering pre-print + post-print window). Cacher reads these on every audit and prioritizes marked tickers |
| `refresh_dirty_artifacts.py` | Embedded in `weekly_synthesis`; also runs ad-hoc | Drains `llm_artifacts.dirty=1` (set by migration 0043 triggers when upstream facts change). Groups by `(ticker, purpose)`, invokes the purpose-specific regenerator, clears dirty |
| `refresh_dispatch.py` | Manual / dashboard `/actions/refresh` | Per-ticker dispatcher with `--mode full` or `--mode stale` (skip FMP if pulled in last 7d). Line-buffered output for SSE streaming |

### Cron chain map

```
03:00  refresh_cache ───────► FMP cache files + financial_facts/* writes
         │                    └─► (SQL trigger 0026) brief_dirty=1
         ▼
04:30  backfill_transcripts ──► transcripts/processed/* + transcripts + management_commitments
         │
         ▼
05:45  fetch_fmp_earnings_calendar ──► data/historical/fmp/<T>_earnings_calendar.json
         │                             └─► refresh_expected_earnings ──► expected_earnings table
         │                                 (canonical calendar: home strip, cockpit, portfolio-tracker)
         │
         ▼
06:15  backfill_earnings_surprises ──► data/surprise/<T>_surprises.json + earnings_surprises
         │
         ▼
06:30  daily_fetch_and_brief ──► drains brief_dirty queue, gates A/B/C, regenerates briefs

[hourly :17]   onboard_pending  (idempotent catch-up)
[Sun 02:00]    weekly_p2_lens_refresh
[Sun 23:00]    weekly_synthesis (drain dirty → per-ticker lenses → portfolio synthesis → dashboard → grading)
[1st 03:00]    monthly_p3_refresh
```

---

## When the report auto-updates

Two refresh paths, with different latency and gating:

### Path A — data-driven (queued)

```
new fact lands in financial_facts/kpi_facts/segment_dimensions/dcf_runs
                          ↓
        (AFTER INSERT trigger from migration 0026)
                          ↓
        tracked_companies.brief_dirty = 1   (instant, same txn)
                          ↓
        [Daily 06:30 cron] daily_fetch_and_brief.py
                          ↓
        Gate A (tier-cadence) → Gate B (material-change hash) → Gate C (eval-cadence)
                          ↓
        thesis_evaluator → refresh_dcf → build_artifacts ──► fresh <DATE>_workspace.html
                          ↓
        brief_dirty = 0   +   last_brief_hash + last_built_at recorded
```

**SQL triggers** ([alembic/versions/0026_brief_dirty_triggers.py](alembic/versions/0026_brief_dirty_triggers.py)) fire on `financial_facts`, `kpi_facts`, `segment_dimensions`, and `dcf_runs` INSERT/UPDATE. Each one sets `tracked_companies.brief_dirty = 1` for the affected ticker in the same transaction. Not covered (intentionally): `thesis_evaluations` (would self-loop), `management_commitments`, `transcripts` (builder reads files directly).

**The three gates** in [`execution/daily_fetch_and_brief.py`](execution/daily_fetch_and_brief.py):

- **Gate A — tier cadence** (`tracked_companies.processing_tier`)
  - P1 (portfolio): always rebuilds
  - P2 (watchlist): rebuilds only if `last_built_at > 7 days`
  - P3 (evaluation / none): rebuilds only if `last_built_at > 30 days`
  - Bypassed by `--ignore-tier` or explicit `--ticker`
- **Gate B — material-change hash** (`_compute_brief_hash`). Hash inputs: `MAX(financial_facts.period_end)`, `MAX(kpi_facts.period_end)`, transcript count, commitment count, holdings JSON bytes, DCF workbook mtime. If `current_hash == last_brief_hash` AND `last_built_at < 7 days ago` → skip with reason `no_material_change`. TTL configurable via `--no-change-ttl-days` (default 7)
- **Gate C — evaluation cadence**. If `list_type='evaluation'` AND `last_built_at < eval_cadence_days` (default 7) → skip with reason `evaluation_cadence`

### Path B — comment-driven (synchronous)

When the analyst posts a comment and runs `process_comments.bat <T> --apply`, the processor:

1. Classifies intent (Haiku auto-classify or slash-keyword override)
2. Sequences edits (Opus determines apply order)
3. Routes by intent: `edit_thesis` / `drop_kpi` / `edit_structured` mutate `micro_thesis/holdings/<T>.json`; `ask_question` answers in-thread; `fix_data` appends to `directives/data_fixes.md`
4. If any holdings-mutating intent applied, runs a **synthesis coherence pass**, then immediately chains: `seed_kpi_definitions → extract_kpis_from_summaries(earnings,ir) → run_thesis_evaluator → build_artifacts`

The brief regenerates inline — no `brief_dirty` flag involved, no waiting for the daily cron.

### Path C — pre-earnings boost

[`schedule_pre_earnings_refresh.py`](execution/schedule_pre_earnings_refresh.py) runs daily inside `refresh_cache`. For tickers reporting in the next 7 days, it writes force-stale hints to `.tmp/cacher/forced_stale.json` keyed by ticker with a 14-day TTL (pre + post window). On its next audit, `refresh_cache` prioritizes time-sensitive endpoints (`ratings`, `dcf`, `price_target`, `ttm_metrics`, `profile`, `market_cap`) for marked tickers ahead of normal cadence. Their writes flip `brief_dirty=1`, and the daily 06:30 worker rebuilds.

### Cache invalidation matrix

| Cache file | Written by | Read by | Invalidation trigger | TTL / key |
|---|---|---|---|---|
| `data/bear_case/<T>.json` | `src/report/sections/bear_case.py` (LLM call) | `build_artifacts` §10 Bear Case | `process_report_comments.py` clears on `edit_thesis` / `edit_structured`; rebuild with `--enable-llm` always re-runs if stale | 40-day TTL |
| `data/valuation_basis/<T>.json` | Opus call picking diagnostic multiple | §6 Valuation tab | Delete the file + rebuild with `--enable-llm` to re-pick. `valuation_multiple_override` in holdings JSON pins it manually | No TTL — sha256 of (multiples table + thesis) |
| `data/company_description/<T>.json` | `extract_company_description.py` | §8 Company tab | Implicit: sha256 over 10-K + thesis + recent earnings/IR docs changes. Or `--refresh` flag forces re-call | sha256 of inputs |
| `data/qa_topics/<T>.json` | Q&A roster extractor | §2 Earnings tab Q&A panel | Rebuild with `--enable-llm` | sha256 of latest transcript |
| `.tmp/news_cache/<T>_<hash>.json` | `src/sources/news.py` (WebSearch + WebFetch) | §3 News tab | `--refresh-news` flag bypasses; or `refresh_news.bat <T>` | 7-day TTL (configurable via `--news-cache-ttl-days`) |
| `data/segment_definitions/<T>.json` | `src/compute/segment_definitions.py` | Segment drill-down tooltips | sha256 of latest form_10k JSON changes | sha256 of source |
| `.tmp/cacher/forced_stale.json` | `schedule_pre_earnings_refresh.py` | `refresh_cache.py` audit pass | Daily rewrite; 14-day TTL per hint | per-hint expiry |

### Manual override matrix

| Situation | Right command |
|---|---|
| Just edited the thesis JSON | `build_report.bat <T> --enable-llm` |
| Want to re-prompt the bear case on the same data | delete `data/bear_case/<T>.json`, then `build_report.bat <T> --enable-llm` |
| News feels stale | `refresh_news.bat <T>` |
| Just edited the HTML rendering / CSS | `build_report.bat <T>` (no `--enable-llm` — fast, reuses caches) |
| New quarter just landed | `refresh_fmp.bat <T>` → `refresh_transcripts.bat <T>` → `build_report.bat <T> --enable-llm` |
| Want everything fresh for one ticker | `full_refresh.bat <T>` |
| Quarterly catch-all for everyone | `python execution/quarterly_refresh.py` |

The full refresh-vs-rebuild table is in [HOW_TO_USE_REPORTS.md §When to refresh vs rebuild](HOW_TO_USE_REPORTS.md#when-to-refresh-vs-rebuild).

---

## How data is picked and validated

For any `(ticker, metric, period_end)`, multiple sources may report a value. The system picks one deterministically, never silently merges, and surfaces disagreements.

### 1. Source-quality tier ranking

Defined in [`src/timeseries/loaders.py`](src/timeseries/loaders.py) — `SOURCE_QUALITY_TIER_RANK`:

| Tier | Rank | Sources |
|---|---|---|
| `sec_official` | 4 | SEC XBRL filings (10-K, 10-Q) — highest authority |
| `fmp_normalized` | 3 | FMP API + IR doc parses + manual CSV/entry |
| `llm_extracted` | 2 | LLM-extracted values from prose (transcripts, decks) |
| `yfinance_fallback` | 1 | yfinance — last resort |

The loader picks one row per logical key via a correlated subquery: `ORDER BY tier_rank DESC, id DESC LIMIT 1`. SEC values always beat FMP for the same period; ties go to the newest insert. Backfill mapping from legacy `source_type` to tier is in migration 0054.

### 2. Restatement chains

When a new write lands on an existing logical key AND the source document is a **later filing** (e.g., FY 10-K supersedes an earlier 10-Q for the same period), [`src/pipeline/restatement_detector.py`](src/pipeline/restatement_detector.py) sets the incumbent's `supersedes_id` pointing at the new row. Both rows survive — the loader picks the newer one by default via the tier + id ordering. Use `--as-of-date YYYY-MM-DD` to time-travel: the loader excludes rows backed by documents fetched after that date, so you can reproduce historical briefs deterministically.

### 3. The four validation rules

[`execution/run_validation_engine.py`](execution/run_validation_engine.py) calls [`src/pipeline/validation_engine.py`](src/pipeline/validation_engine.py) which runs four rule families and emits rows to `validation_issues`:

| Rule | What it checks | Trigger | Severity |
|---|---|---|---|
| `PLAUSIBLE_RANGE` (financial) | Hard bounds per line_item (e.g., `total_assets >= 0`, `weighted_avg_shares >= 0`) | Value outside bound | WARN |
| `PLAUSIBLE_RANGE` (KPI) | Unit-specific bounds: percent `[-1000, 1000]`, ratio `[-100, 100]`, bps `[-100000, 100000]`, count `[0, 10B]` | Value outside unit bound | WARN |
| `MAGNITUDE_JUMP` | Sequential same-line ratio `>5x` (e.g., Q1 in millions, Q2 in thousands) on `revenue`, `operating_income`, `net_income` | Catches unit errors | WARN |
| `SOURCE_DISAGREEMENT` | Two different `source_type`s reporting the same `(ticker, period, line_item)` diverge by `>0.5%` | Surfaces FMP-vs-SEC discrepancies | WARN |

Designed `HALT` severity (for >3 orders of magnitude — the "revenue 10x off" rule from [GEMINI.md](GEMINI.md)) is wired but not currently emitted by live rules. KPI persistence ([`src/pipeline/kpi_persistence.py`](src/pipeline/kpi_persistence.py)) re-checks ranges before INSERT and writes a WARN row on violation.

### 4. The `validation_issues` table

Schema (migration 0006): `(id, run_id, source_doc_id, ticker, severity, rule, raw_value, expected, raised_at, resolved_at)`. Indexed on `(severity, resolved_at)` for fast dashboard queries.

Consumers:
- **§11 Provenance** in every workspace report queries open issues (`resolved_at IS NULL`), caps at 50 rows, renders as a collapsible table per ticker
- The portfolio dashboard at `localhost:7421/` could in future expose a per-ticker open-issue count column

### 5. Per-cell lineage

Every fact row carries `source_doc_id` + `extracted_by` + `supersedes_id` (migration 0054). For any value rendered in a report, you can trace:

```
financial_facts.id ──► source_doc_id ──► documents.{doc_type, file_path, sha256, fetched_at, source_quality_tier}
                  └── extracted_by ──► "fmp" | "sec_xbrl" | "llm:claude-sonnet-4-6" | "manual_csv"
                  └── supersedes_id ──► earlier version (if restated); follow forward via latest_in_chain()
```

§11 Provenance surfaces this for every line item in the report. The `documents` table is the authoritative source-doc audit log.

### 6. LLM-extracted data validation

KPI extraction ([`src/compute/kpi_extract_summaries.py`](src/compute/kpi_extract_summaries.py)) writes through a Pydantic-validated `KpiExtractionManifest` with explicit `confidence` ∈ [0.0, 1.0], a `Unit` enum, and per-unit range checks before persistence.

Segment cross-tab extraction ([`src/compute/segment_crosstabs_llm.py`](src/compute/segment_crosstabs_llm.py)) enforces a strict JSON contract with axis-name aliasing ("products" → `PRODUCT`, "region" → `GEOGRAPHY`) and validates that every cell's `(period_end, fiscal_period_type)` exists in `segment_periods` for the same ticker (FK in the junction writer).

### 7. Range checks for currency

Values are stored as strings cast to Decimal, with no USD scaling — INR / JPY / KRW filers report much larger nominal numbers and would false-positive a fixed bound. `MAGNITUDE_JUMP` catches unit errors via sequential 5x ratios instead.

---

## Common workflows

The full analyst workflow lives in **[HOW_TO_USE_REPORTS.md](HOW_TO_USE_REPORTS.md)** — slash-keyword shortcuts, refresh-vs-rebuild table, onboarding a new ticker, comment hygiene, troubleshooting. This section is the quick command map.

### Build the workspace report

```cmd
build_report.bat META --enable-llm
:: ↳ output/research/META/<YYYY-MM-DD>_workspace.html
```

Omit `--enable-llm` for a fast rebuild that reuses cached LLM outputs. Pass `--flavor evaluation --allow-untracked` to screen a new name without onboarding it.

### Refresh data for one ticker

```cmd
refresh_fmp.bat NVO 20            :: 20 quarters of FMP fundamentals
refresh_transcripts.bat NVO       :: last 6 quarters of Q&A
refresh_news.bat NVO 14           :: 14-day news lookback
full_refresh.bat NVO              :: everything end-to-end (5-15 min)
```

### Onboard a new ticker

```cmd
:: 1. Create micro_thesis/holdings/<NEW>.json (copy an existing one)
python execution/onboard_ticker.py --ticker NEW --list-type portfolio
full_refresh.bat NEW
```

### Comment workflow

```cmd
start_comments_server.bat                          :: leave running on :7421
:: ...open the workspace HTML in browser, comment + chat...
process_comments.bat NU --apply                    :: drain comments → edits + rebuild
```

### Evaluate a candidate (P2)

```cmd
build_report.bat AMD --enable-llm --flavor evaluation --allow-untracked
python execution/build_diligence.py --ticker AMD
:: ...build a DCF workbook in dcf/AMD.xlsx...
python execution/refresh_dcf.py --ticker AMD
:: ...edit micro_thesis/holdings/AMD.json...
python execution/pressure_test_thesis.py --ticker AMD
python execution/check_initiation_gate.py --ticker AMD
```

### Per-ticker enhancements

Drop a JSON in `data/ticker_specific/<TICKER>/<feature>.json` (e.g., NVO patent timeline, drug pipeline milestones). The §10 Bear Case prompt picks it up automatically. See [`directives/per_ticker_enhancements.md`](directives/per_ticker_enhancements.md).

### Output retention sweep

```cmd
python execution/sweep_output_history.py --dry-run
python execution/sweep_output_history.py --keep 10 --ticker META
```

Groups files in `output/research/<TICKER>/` by `YYYY-MM-DD_` prefix; deletes everything older than the latest N distinct dates per ticker. Files without a date prefix survive any sweep.

---

## State, idempotency, and resumption

- Every pipeline run has `run_id = {directive}_{ticker_scope}_{period_end}_{started_at_iso}`.
- Stage-level idempotency key: `(run_id, ticker, period_end, stage)` in `stage_transitions`.
- Resumption: re-run the relevant CLI. It queries `stage_transitions` for the run and proceeds from the first stage where `status != ok`. See [`directives/data_pipeline_dag.md` §Resumption](directives/data_pipeline_dag.md).
- Per-source idempotency keys: [`directives/data_provenance.md` §4](directives/data_provenance.md).
- All ephemeral state lives in `.tmp/`; deliverables live in `output/research/<TICKER>/` and `data/`. Never mix.
- Source-call provenance: every external HTTP call (FMP, yfinance, SEC XBRL) writes one row to `source_calls` with `(source_name, kind, ticker, called_at, status, latency_ms)`. Write-many / read-rarely; intended for future intelligent routing.

---

## Pre-push checklist

```bash
ruff format .
ruff check . --fix
pyright
basedpyright
pytest
```

Strict typing is enforced (`pyright` strict + `basedpyright` all). No `Any`, no `# noqa`, no substring-matching for classification — see [GEMINI.md](GEMINI.md) for the full code standards.

---

## Security

- `.env`, `credentials.json`, `token.json`, and any `*.pem` are gitignored and must never be logged or echoed.
- API keys pass via environment variables only — never CLI args (they leak into shell history + process lists).
- FMP fetchers route exception strings through [`src/log_redact.py`](src/log_redact.py) before logging, since `requests.HTTPError.__str__` embeds the full URL (with `apikey=...`). Every new integration that talks to a credentialed HTTP endpoint should import the same helper.
- The chat server's filesystem access is read-only and scoped to `data/`, `micro_thesis/`, `.tmp/`, `transcripts/`. The chatbot can propose diffs but only writes to disk via the explicit `/chat/<ticker>/apply` endpoint after the analyst clicks Apply.
