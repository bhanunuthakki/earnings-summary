# Directive: Monthly Portfolio + Watchlist Autopilot

## Goal

Single-command, end-to-end refresh of the portfolio's analytical state on a monthly cadence (or any time the user asks). Replaces the older `check_quarterly_releases` cron — autopilot already runs that as its first stage.

## Tools / Scripts

| Purpose | Script |
|---|---|
| Orchestrator | `execution/autopilot.py` |
| Stage 1 — PULL | `execution/check_quarterly_releases.py` |
| Stage 2 — PROCESS | `src/main.py` (per-quarter summary + SayDo + master PDF) |
| Stage 3 — TRACK | `execution/update_thesis_tracker.py --all` |
| Stage 4 — MEMO | `execution/generate_memo.py --portfolio` |
| Cron wrapper | `cron/run_autopilot.bat` |
| Task definition | `cron/autopilot.task.xml` |

## Stages, in order

```
PULL    → PROCESS    → TRACK    → MEMO    → CONSOLIDATE
(aggregators) (LLM summaries  (LLM thesis  (LLM memos    (output/research/<TICKER>/
              + SayDo + PDFs)  trackers)    + Claude       + index.html
                                            web research)  + DB register)
```

A hard failure in any stage halts the chain; downstream stages would just report the same gap. Each stage runs as a subprocess so a failure in one can't poison Python interpreter state for the next. Each stage's stdout/stderr is tee'd into `.tmp/cron_logs/<run_id>__<stage>.log`; the orchestrator writes a structured `RunReport` to `.tmp/cron_runs/<run_id>.json`.

### Stage 1 — PULL

`check_quarterly_releases.py --include-watchlist --days <pull_days> --limit-quarters <pull_limit_quarters>`

Aggregator chain (roic.ai → stockanalysis → tickertrends) writes new transcripts into `transcripts/raw/`. See `directives/check_quarterly_releases.md`. Default window 45 days = monthly cron with 2-week safety overlap.

### Stage 2 — PROCESS

`python src/main.py`

Auto-discovers everything in `transcripts/raw/` and `transcripts/processed/`, writes per-quarter summaries to `.tmp/<TICKER>_<Q>_<YEAR>_summary.txt`, generates pairwise SayDo verdicts to `.tmp/SayDo_<TICKER>_<...>.txt`, and rebuilds master PDFs at `transcripts/master/<TICKER>_Master_Transcripts.pdf`. Idempotent — already-cached entries are skipped via the per-company manifest.

LLM routing is via `src/llm_router.call_llm()` (Claude CLI primary, Gemini fallback). Throttling lives in the router; `main.py` does not sleep between calls.

### Stage 3 — TRACK

`python execution/update_thesis_tracker.py --all`

For each `micro_thesis/holdings/<TICKER>.json`, generates a fresh `micro_thesis/thesis-tracker-<TICKER>-<YYYY-MM-DD>.md` from the cached summaries. Tickers without a holdings JSON are skipped silently. ~1 LLM call per holding.

### Stage 4 — MEMO

`python execution/generate_memo.py --portfolio --news-days 14`

For each portfolio name (NOT watchlist), produce a styled HTML memo at `transcripts/memos/<TICKER>_memo_<YYYY-MM-DD>.html`. Uses:
- Per-quarter transcript summaries (REQUIRED)
- Pairwise SayDo verdicts (optional)
- Thesis JSON (optional)
- Latest thesis tracker (optional)
- IR-doc filenames in `micro_thesis/sources/<TICKER>/` (optional, listed only)
- **Claude WebSearch + WebFetch** for the "Recent Developments" section (primary news source — Claude does its own queries against Bloomberg/Reuters/CNBC/etc. and cites URLs inline)
- FMP `/stable/news/stock` (optional fallback hint when Claude web tools unavailable)

The LLM call routes through `llm_router.call_llm_with_web()` which invokes the Claude CLI with `--allowedTools WebSearch WebFetch`. Falls through to plain `call_llm()` (no web) on Claude failure so a memo is still produced.

Graceful degradation: a memo is produced even when only the transcript summaries exist (acid test: AMZN has no thesis JSON, no tracker, no IR docs, and still gets a fully-cited memo with real Bloomberg/CNBC URLs).

### Stage 5 — CONSOLIDATE

`python execution/consolidate_outputs.py`

Copy the latest of each per-ticker artifact (memo HTML, thesis tracker MD, master PDF) into a unified `output/research/<TICKER>/` folder, write a per-ticker `output/research/<TICKER>/index.html` landing page, and a top-level `output/research/index.html` portfolio dashboard. Each artifact path is registered in the SQLite `output_artifacts` table with `is_latest=1` so any downstream consumer can find them via:

```sql
SELECT path FROM output_artifacts
WHERE ticker = ? AND kind = 'memo' AND is_latest = 1;
```

Idempotent — re-running just refreshes copies and re-registers latest paths. Older versions in `output/research/<TICKER>/` are NOT pruned (manual housekeeping if disk pressure becomes an issue).

## CLI

```
python execution/autopilot.py                          # full chain
python execution/autopilot.py --dry-run                # print stage plan, run nothing
python execution/autopilot.py --only memo              # one stage
python execution/autopilot.py --skip-pull --skip-process   # tail two stages only
python execution/autopilot.py --with-audio-fallback        # PULL escalates to ytsearch on aggregator miss
python execution/autopilot.py --memo-news-days 30          # widen news window
```

## Outputs

| Artifact | Path | Notes |
|---|---|---|
| Run report | `.tmp/cron_runs/autopilot_<TS>.json` | `RunReport` JSON: per-stage status + log paths |
| Stage logs | `.tmp/cron_logs/autopilot_<TS>__<stage>.log` | Full stdout/stderr per stage |
| Transcripts | `transcripts/raw/` → `transcripts/processed/` | PROCESS moves them |
| Summaries | `.tmp/<TICKER>_<Q>_<YEAR>_summary.txt` | Per-quarter, idempotent |
| SayDo | `.tmp/SayDo_<TICKER>_<...>.txt` | Pairwise verdicts |
| Master PDFs | `transcripts/master/<TICKER>_Master_Transcripts.pdf` | One per ticker |
| Thesis trackers | `micro_thesis/thesis-tracker-<TICKER>-<DATE>.md` | One per holding with JSON |
| Memos | `transcripts/memos/<TICKER>_memo_<DATE>.html` | One per portfolio name (Claude WebSearch-sourced) |
| Consolidated outputs | `output/research/<TICKER>/{<DATE>_memo.html, <DATE>_tracker.md, master_transcripts.pdf, index.html}` | Per-ticker landing folder. Naming follows the pre-existing `<DATE>_report.html` convention so memos sit alongside any other dated research artefacts. |
| Research dashboard | `output/research/index.html` | Top-level table linking every ticker's folder |
| Output index (DB) | SQLite `output_artifacts` table | `(ticker, kind, path, is_latest)` rows for downstream queries |

## Schedule

`cron/autopilot.task.xml` triggers on the **15th of every month at 06:00 local time**. Earnings-season releases land in the first ~3 weeks of Feb/May/Aug/Nov; by the 15th the aggregators have indexed everything and the run finishes well before market open.

`StartWhenAvailable=true` + `RestartOnFailure` (1h interval, 2 retries) ensures missed firings catch up when the machine returns online. `ExecutionTimeLimit=PT12H` is a safety cap; typical full chain finishes inside an hour.

## FMP cost per run

| Stage | FMP calls |
|---|---|
| PULL | ~54 (income-statement + earnings × 27 tickers) |
| MEMO | ~11 (news/stock × portfolio names) |

Comfortable on free tier.

## Edge cases

- **Newly-IPO'd ticker added to portfolio mid-cycle**: PULL picks it up the next firing if FMP has the company. PROCESS auto-discovers any transcript files it sees. TRACK skips silently if no holdings JSON yet (Thesis Formation is a separate, human-driven exercise per `directives/micro_thesis_skill.md`). MEMO generates a "no formal thesis on file" version from transcript evidence.
- **Aggregator regex change**: a sudden batch of `[miss]` entries in the PULL log is the canary. Re-run `scratch/probe_aggregators.py` and update `src/aggregator_sources.py` regexes.
- **LLM provider chain**: Claude CLI primary; Gemini fallback only fires on transient Claude failures or setup issues. To force fallback for testing, set `ANTHROPIC_API_KEY` and unset `GEMINI_API_KEY` (router will pop the API key from env to preserve subscription billing — see `src/llm_router.py`).
- **Watchlist memos**: not produced by default (only portfolio names get memos). Run `python execution/generate_memo.py --all` for a one-off pass that includes watchlist.

## Verification

After a run completes:
- [ ] `.tmp/cron_runs/autopilot_<latest>.json` shows all 4 stages with `status=ok`.
- [ ] `transcripts/master/` has 1 PDF per ticker that was in PROCESS scope.
- [ ] `micro_thesis/thesis-tracker-<TICKER>-<TODAY>.md` exists for each holdings JSON.
- [ ] `transcripts/memos/<TICKER>_memo_<TODAY>.html` exists for each portfolio name.
- [ ] Re-running immediately is a fast no-op (PULL sees nothing new, PROCESS skips via manifest, TRACK regenerates trackers, MEMO regenerates memos — TRACK + MEMO always re-do because they're cheap).
