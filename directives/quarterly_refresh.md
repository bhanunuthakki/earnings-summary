# Directive: Quarterly Refresh (Cron Entry Point)

## Goal

A single idempotent orchestrator that processes everything currently in the DB
across every source type (FMP, IR docs, transcripts) without requiring an LLM
for the steps that don't need one — and surfaces the LLM-needed work as a
queue for follow-up.

## Tools / Scripts

| Purpose | Script |
|---|---|
| Cron entry point | `execution/quarterly_refresh.py` |
| Stage orchestrator (importable) | `src/pipeline/quarterly_refresh.py::refresh_portfolio` |

## Per-source Bespoke Stages

Each stage runs once per ticker, in order, and is independently idempotent:

| Stage | Source | Action | Auto? |
|---|---|---|---|
| `extract_fmp_facts` | `documents` rows in 11 FMP fact-producing `doc_type`s | Re-run extractors via INSERT OR IGNORE; produces new `financial_facts`/`segment_facts` rows for any docs not yet processed. | yes |
| `ingest_ir_transcripts` | `documents` where `doc_type='ir_transcript'` and no `transcripts` row | Parse PDF, write `transcripts` + `transcript_segments` (with speaker whitelist where configured). | yes |
| `derive_fmp_kpis` | `financial_facts` rows from `*_quarterly.json` source files | Compute Operating Margin (GAAP), Net Income Margin (GAAP), Gross Margin (GAAP), Revenue YoY Growth (USD); insert into `kpi_facts`. | yes |
| `match_commitments` | `management_commitments` with NULL outcome | Look up the realized value from `kpi_facts`; classify HIT / MISS / BEAT / NO_DATA. | yes |
| `evaluate_thesis` | `kpi_facts` × `micro_thesis/holdings/<TICKER>.json::break_rules` | Update `thesis_state.breach_status`; append history row to `thesis_evaluations`. | yes |
| `surface_pending_llm` | IR PDFs not yet contributing `kpi_facts` + `transcripts` not yet linked to commitments | Emit a typed `PendingWorkItem` list. **Not auto-executed** — the user attacks these in a Claude session. | no (`needs_llm`) |

## What's NOT in the cron

The cron does NOT call any external API. New FMP / SEC / IR / audio fetches
remain explicit user actions:

- `execution/fetch_fmp_*.py` — refresh FMP fundamentals (paid plan required)
- `execution/fetch_ir_documents.py` — refresh IR docs (URL manifest required)
- `execution/fetch_audio_transcripts.py` — pull yt-dlp + Whisper transcribe
- `execution/fetch_sec_historical_pipeline.py` — SEC XBRL backfill

After any of those land new bytes on disk, the cron will pick them up on the
next scheduled run.

## Schedule

Recommended cadence: **monthly, on the 5th, 06:00 local time** — covers the
typical window where the prior quarter's earnings have all reported.

### Linux

```cron
0 6 5 * *  cd /path/to/earnings-summary && venv/bin/python execution/quarterly_refresh.py >> logs/refresh.log 2>&1
```

### Windows Task Scheduler

```powershell
schtasks /Create `
  /TN "EarningsRefresh" `
  /TR "C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary\venv\Scripts\python.exe C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary\execution\quarterly_refresh.py" `
  /SC MONTHLY /D 5 /ST 06:00
```

## Output Schema

Stdout (default) is a compact human-readable table. With `--json`, the full
structured report:

```json
{
  "run_id": "quarterly_refresh_<scope>_<timestamp>_<short>",
  "started_at": "2026-05-04T...",
  "ended_at": "2026-05-04T...",
  "tickers": [
    {
      "ticker": "MELI",
      "breach_status": "ok",
      "breach_status_changed": false,
      "stages": [{"name": "extract_fmp_facts", "status": "ok", "rows_processed": 0, "notes": "..."}, ...],
      "pending_work": [{"kind": "ir_pdf_kpi_extraction", "document_id": 13100, ...}, ...]
    }
  ]
}
```

`run_id` is also persisted to `ingestion_runs` and every `thesis_evaluations`
row that gets appended in this run.

## Failure-mode policy

Per-ticker stages capture errors locally (no halt). The CLI exits with code 1
if any single stage in any ticker reports `status='failed'`. The full report is
still printed so a cron log captures the diagnostic.

`status='needs_llm'` is NOT a failure — it's the expected state for IR PDFs
and unanalyzed transcripts. The CLI exits 0 in that case.

## Verification

After running:

- [ ] `ingestion_runs` has a new row with `directive='quarterly_refresh'`.
- [ ] `thesis_evaluations` has one new row per thesis-tracked ticker.
- [ ] Tickers in `thesis_state` whose status changed since the prior run show
      `breach_status_changed: true` in the report (and `(CHANGED)` in the
      `evaluate_thesis` stage notes).
- [ ] Total `pending_work` count matches the number of IR PDFs without
      `kpi_facts` plus transcripts without `management_commitments`.
