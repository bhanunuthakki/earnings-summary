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
| `src/` | Core app: Flask UI (`static/`), `main.py` ingestion driver, `db.py` portfolio store, `llm_client.py`, `parser.py`, `pdf_builder.py`, `index_manager.py`, `alias_manager.py`, `calendar_manager.py` |
| `src/compute/` | Deterministic financial computations: `income_statement`, `balance_sheet`, `cashflow`, `as_reported`, `segments`, `segment_oi_10k`, `dcf` |
| `src/models/` | Pydantic schemas for documents, facts, KPIs, FMP payloads, patents, runs, validation |
| `src/pipeline/` | Source routing, accounting runner, query helpers |
| `execution/` | CLI entrypoints — FMP fetchers, IR doc fetchers, SEC pipelines, transcript fetcher (`yt-dlp` + `faster-whisper`), DCF runners, thesis tracker, NVO patent timeline, LLY SEC cross-check |
| `directives/` | Layer 1 SOPs — pipeline DAG, data provenance, per-source fetch directives, micro-thesis runbook |
| `micro_thesis/holdings/` | Per-ticker JSON KPI specs and thesis break conditions |
| `micro_thesis/sources/` | Per-ticker drop folders for review documents |
| `examples/dcf/` | Reference DCF workbooks (AMZN, GOOG, META) |
| `alembic/` | Schema migrations against `data/portfolio.db` |
| `tests/` | Pytest suite covering compute modules and pipeline contracts |
| `transcripts/raw|processed|master/` | Earnings transcript flow (gitignored) |
| `ir_documents/` | Downloaded IR PDFs (gitignored) |
| `data/` | SQLite DB + FMP JSON cache (gitignored, reproducible) |
| `.tmp/` | Ephemeral state, parsed payloads, indexes (gitignored) |

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

### Refresh fundamentals for a ticker

```bash
python execution/save_fmp_data.py --ticker NVO
python execution/run_pipeline.py --ticker NVO
```

### Fetch and transcribe an earnings call

```bash
python execution/fetch_audio_transcripts.py --ticker MELI --year 2026 --quarter 1
```

Downloads audio via `yt-dlp`, transcribes locally with `faster-whisper` (`large-v3-turbo`, int8 CPU), writes `transcripts/raw/MELI_Q1_2026.txt`, and registers it in `.tmp/transcript_index.json`.

### Build the per-company Master PDF

Drop `Company_Qx_YYYY.{txt,pdf,mp3,m4a}` files into `transcripts/raw/` (auto-rename will fix close matches), then:

```bash
python src/main.py
```

Output: `transcripts/master/<Company>_Master_Transcripts.pdf` with cover pages, cached LLM summaries, pairwise Say-Do analysis (when ≥2 quarters), and the full transcripts behind a clickable TOC.

### IR document pipeline

```bash
python execution/fetch_ir_documents.py --ticker GOOG
python execution/process_ir_documents.py --ticker GOOG
```

### Compute DCF / per-segment quarterly DCF

```bash
python execution/run_dcf.py --ticker META
python execution/run_quarterly_segment_dcf.py --ticker GOOG
```

### Update the micro-thesis tracker

```bash
python execution/update_thesis_tracker.py
```

Reads each `micro_thesis/holdings/<TICKER>.json` spec, evaluates Tier 1 KPIs against break conditions, and emits a tracker note following `directives/micro_thesis_runbook.md`.

### NVO competitive cross-check

```bash
python execution/extract_nvo_patent_timeline.py
python execution/fetch_lly_sec_filings.py
python execution/extract_market_signals_from_transcripts.py --ticker LLY
```

See `directives/nvo_external_sources.md` for the tirzepatide/retatrutide vs semaglutide signal pipeline.

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
- Stage-level keys: `(run_id, ticker, period_end, stage)`. Resumption uses `python execution/run_pipeline.py --resume <run_id>`.
- Per-source idempotency keys: `directives/data_provenance.md` §4.
- All ephemeral state lives in `.tmp/`; deliverables live in `transcripts/master/`, `data/`, or cloud destinations. Never mix.

## Security

`.env`, `credentials.json`, `token.json`, and any `*.pem` are gitignored and must never be logged or echoed. API keys are passed via environment variables only — never CLI args.
