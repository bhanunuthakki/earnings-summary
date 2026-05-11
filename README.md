# Earnings Summary

Portfolio-wide data pipeline for tracking 11 holdings (AMZN, GOOG, META, MELI, NU, NVO, NOW, WIX, RBRK, VEEV, BN) plus 1 watchlist (LLY) across earnings transcripts, IR documents, FMP fundamentals, and SEC XBRL. The system ingests raw documents, parses and validates them into a SQLite store, computes derived metrics (segment OI, DCF, per-segment quarterly DCF), and synthesizes per-quarter LLM summaries plus a micro-thesis tracker.

## Architecture

Three layers, enforced by `GEMINI.md`:

1. **Directives** (`directives/`) — Layer 1 SOPs. One Markdown file per task type. Immutable without explicit authorization.
2. **Orchestration** — Layer 2. Routes between executions, reads stdout/stderr, manages state. No business logic.
3. **Execution** (`execution/`) — Layer 3 single-purpose Python CLIs with typed Pydantic I/O.

The canonical 8-stage pipeline (`directives/data_pipeline_dag.md`):

```
INGEST → TRANSCRIBE → PARSE → VALIDATE → PERSIST → COMPUTE → SYNTHESIZE → PUBLISH
```

Each stage is idempotent, resumable from `stage_transitions`, and writes typed outputs to either the DB or `.tmp/`.

## Layout

| Path | Purpose |
|---|---|
| `src/` | Core utilities: `db.py`, `llm_client.py`, `parser.py`, `portfolio.py`, `alias_manager.py`, `calendar_manager.py`, `intake.py`, `ir_uploads.py`, `index_manager.py`, `transcript_qa.py` |
| `src/report/` | Unified brief generator. `builder.py` → `ReportSpec` (11 sections); renderers in `renderers/{html,markdown,sections_json,workbook}.py`; per-section builders in `sections/` |
| `src/dcf/` | DCF subsystem: `workbook_reader.py` extracts FCF stream, `valuation.py` computes PV/share + over-under %, `live_price.py` reads live FMP price, `persist.py` upserts `dcf_runs` |
| `src/compute/` | Deterministic financial computations: `income_statement`, `balance_sheet`, `cashflow`, `as_reported`, `segments`, `segment_oi_10k`, `say_do`, `say_do_extractor`, `thesis_evaluator`, `holding_scorecard` |
| `src/models/` | Pydantic schemas for documents, facts, KPIs, FMP payloads, patents, runs, validation |
| `src/pipeline/` | Source routing, accounting runner, query helpers, KPI persistence, SEC XBRL parser, quarterly refresh orchestrator |
| `execution/` | CLI entrypoints — FMP fetchers, IR doc fetchers, SEC pipelines, transcript fetcher (`yt-dlp` + `faster-whisper`), DCF refresher, brief builder + news refresher, daily worker + earnings calendar watcher, P2 (diligence + pressure-test + initiation-gate) |
| `directives/` | Layer 1 SOPs — pipeline DAG, data provenance, per-source fetch directives, per-ticker enhancements |
| `micro_thesis/holdings/` | Per-ticker JSON KPI specs (schema v2: thesis + tier-1/2/3 KPIs + break rules + WACC + MoS bar + DCF defaults) |
| `micro_thesis/sources/` | Per-ticker drop folders for review documents |
| `micro_thesis/diligence/` | P2 diligence markdown per candidate ticker (built by `build_diligence.py`) |
| `dcf/` | Canonical per-ticker DCF workbooks (`<TICKER>.xlsx`, user-edited; system refreshes historicals only) |
| `data/ticker_specific/` | Per-ticker enhancement JSON (e.g. `NVO/patent_timeline.json`) — fed into the brief's §9 Bear Case prompt |
| `examples/dcf/` | Reference DCF workbook templates (AMZN, GOOG, META) — seed for new `dcf/<TICKER>.xlsx` |
| `alembic/` | Schema migrations against `data/portfolio.db` |
| `cron/` | Windows Task Scheduler XMLs + `.bat` wrappers for the daily crons |
| `tests/` | Pytest suite covering compute modules and pipeline contracts |
| `transcripts/raw|processed/` | Earnings transcript flow (gitignored) |
| `ir_documents/` | Downloaded IR PDFs (gitignored) |
| `output/research/<TICKER>/` | Generated brief artifacts (`<DATE>_report.html` etc.) — primary deliverable |
| `data/` | SQLite DB + FMP JSON cache (gitignored, reproducible) |
| `.tmp/` | Ephemeral state, parsed payloads, indexes, pressure-test audits (gitignored) |

## Setup

Requires Python ≥3.11.

```bash
pip install -r requirements.txt
pip install -e ".[dev]"      # adds pytest, alembic, ruff, pyright, basedpyright
alembic upgrade head         # initialize data/portfolio.db
```

Create `.env` with whichever providers you need:

```env
GEMINI_API_KEY=...           # transcript summaries / Say-Do analysis
FMP_API_KEY=...              # fundamentals, statements, calendar, transcripts
```

> Python scripts that call Claude must route through `C:\Users\Bhanu\.gemini\snippets\claude_cli.py` so they bill against the user's subscription, not the metered API. See global `CLAUDE.md` for the rationale.

## Common workflows

The system is built around two daily crons + a small set of on-demand CLIs. The crons keep the brief auto-refreshed; the CLIs cover one-off operations.

### Daily auto-refresh loop

```bash
# 06:00 — scan FMP earnings calendar, populate the queue
python execution/earnings_calendar_watcher.py

# 06:30 — drain tracked_companies.brief_dirty:
#   thesis_evaluator → match_commitments → refresh_dcf → build_artifacts
python execution/daily_fetch_and_brief.py --enable-llm
```

Both are wired in `cron/*.task.xml`. Fact-table inserts auto-flip `brief_dirty=1` via the SQL triggers from migration 0026 — no manual invalidation needed.

### Refresh fundamentals for a ticker

```bash
python execution/save_fmp_data.py --ticker NVO
python execution/extract_facts.py --ticker NVO        # parse FMP JSONs into financial_facts
```

The triggers then flip `brief_dirty=1`, and the next daily worker tick picks it up. Or force the refresh inline:

```bash
python execution/daily_fetch_and_brief.py --ticker NVO --enable-llm
```

### Fetch + transcribe an earnings call

```bash
python execution/fetch_audio_transcripts.py --ticker MELI --year 2026 --quarter 1
```

Downloads audio via `yt-dlp`, transcribes locally with `faster-whisper`, writes `transcripts/raw/MELI_Q1_2026.txt`, and registers it in `.tmp/transcript_index.json`.

### Generate the unified brief (single ticker)

```bash
# Portfolio flavor (default — Snapshot + thesis + KPI strip in §1)
python execution/build_artifacts.py --ticker META --enable-llm

# Evaluation flavor (3y quick-categorization data table in §1 — for new-name screening)
python execution/build_artifacts.py --ticker AMD --flavor evaluation --allow-untracked
```

Writes `output/research/<TICKER>/<DATE>_report.html` + `.md` + `_sections.json` + `_dcf.xlsx`. `--enable-llm` opts §8 Recent Developments + §9 Bear Case into real Claude calls (subscription billing); omit to keep them stubbed.

### Refresh just the news section (faster than a full rebuild)

```bash
python execution/refresh_news.py --ticker META
```

Bypasses the 7-day news cache and re-queries WebSearch; the rest of the brief regenerates from the same DB state.

### IR document pipeline

```bash
python execution/fetch_ir_documents.py --ticker GOOG
python execution/process_ir_documents.py --ticker GOOG
```

### Compute / refresh the DCF for a ticker

Drop your canonical workbook at `dcf/<TICKER>.xlsx` (copy `examples/dcf/<TICKER>-*.xlsx` as a starting template). Then:

```bash
python execution/refresh_dcf.py --ticker META
```

Reads the FCF stream from the workbook's Valuation sheet, computes PV/share at the per-ticker WACC (from `micro_thesis/holdings/<TICKER>.json`), pulls live price from FMP `profile.json`, computes over/under %, and writes to `dcf_runs`. The brief's §1 Valuation Card surfaces the result with the trim/sell trigger badge.

### Evaluate a new candidate (P2)

```bash
# Day 0 — quick screen
python execution/build_artifacts.py --ticker AMD --flavor evaluation --allow-untracked

# Day 1 — diligence template
python execution/build_diligence.py --ticker AMD

# Day 2 — build DCF (copy template, edit forecast assumptions) then refresh
python execution/refresh_dcf.py --ticker AMD

# Day 3 — draft thesis (edit micro_thesis/holdings/AMD.json), then pressure-test
python execution/pressure_test_thesis.py --ticker AMD

# Day 4 — initiation gate (GO / NO-GO + per-gate reasons)
python execution/check_initiation_gate.py --ticker AMD
```

### Per-ticker enhancements (custom research)

Drop a JSON in `data/ticker_specific/<TICKER>/<feature>.json` (e.g. NVO patent timeline, drug pipeline milestones). The brief's §9 Bear Case prompt picks it up automatically. See `directives/per_ticker_enhancements.md`.

### Recurring catch-up for orphan tickers

```bash
python execution/onboard_pending_tickers.py
```

Hourly cron. Belt-and-suspenders for tickers that enter `tracked_companies` outside the `track_company` hook — runs FMP fetch + parse + DCF for any pending ticker. See `directives/onboard_pending_tickers.md`.

## Pre-push checklist

```bash
ruff format .
ruff check . --fix
pyright
basedpyright
pytest
```

Strict typing is enforced (`pyright` strict + `basedpyright` all). No `Any`, no `# noqa`, no substring-matching for classification — see global `CLAUDE.md` and the repo `GEMINI.md` for the full code standards.

## State and idempotency

- Every pipeline run has `run_id = {directive}_{ticker_scope}_{period_end}_{started_at_iso}`.
- Stage-level keys: `(run_id, ticker, period_end, stage)`. Resumption: re-run `python execution/daily_fetch_and_brief.py --ticker <T>`; it queries `stage_transitions` for the run and proceeds from the first stage where `status != ok` (see `directives/data_pipeline_dag.md` §Resumption).
- Per-source idempotency keys: `directives/data_provenance.md` §4.
- All ephemeral state lives in `.tmp/`; deliverables live in `output/research/<TICKER>/` and `data/`. Never mix.

## Security

`.env`, `credentials.json`, `token.json`, and any `*.pem` are gitignored and must never be logged or echoed. API keys are passed via environment variables only — never CLI args.
