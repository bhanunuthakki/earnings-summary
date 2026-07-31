# FMP backpopulation — the ~6-monthly paid bulk refresh

**Status**: Active (built 2026-07-02, PR chain #767+). Owner decision, verbatim: *"Build pipeline for EDGAR; I will also get FMP every 6 months or so for a full backpopulation from last FMP update."*

**Division of labor**: `directives/edgar_pipeline.md` (EDGAR = continuous free weekly freshness for the ~45 statement line_items) + this directive (FMP = periodic paid bulk refresh for everything else — segments, growth series, TTM, ratios, analyst estimates, DCF, price history, peers, 10-K/10-Q JSON — plus a safety-net re-fetch of statements where EDGAR has a gap).

## What the script does

`execution/fmp_backpop.py` builds a `save_fmp_data.py --manifest`-shaped job list, diff-aware against EDGAR coverage:

- **Always included** (61 of the 67 `per_ticker_jobs` endpoints): everything EDGAR structurally cannot provide — segmentation, growth series, TTM statements, ratios/key-metrics, analyst estimates, DCF, 10-K/10-Q JSON, price history, peers, profile, etc. This is the actual point of the ~6-monthly window; EDGAR never touches these. Exception (2026-07-30, PR #1086): `index_member` rows resolve to the shallow 8-endpoint peer contract (`save_fmp_data.PEER_ENDPOINT_ALLOWLIST`), so a backpop scoped to peers via `--tickers` fetches only those 8 families at peer depth.
- **Diff-aware skip candidates** (6 jobs: income-statement / balance-sheet-statement / cashflow-statement × annual/quarter): skipped for a ticker ONLY when `sec_covers_well()` returns `True` — at least one `financial_facts` row with `extracted_by='sec_xbrl'` and `period_end` within `SEC_FRESHNESS_DAYS` (400), AND zero unresolved `validation_issues` rows with `rule='source_disagreement'` for that ticker. Any gap (no CIK, EDGAR never ran, stale coverage, an active disagreement) falls back to fetching the FMP statement too.

This mirrors the existing `execution/refresh_cache.py` manifest contract exactly (same `[{ticker, endpoint, period}]` shape `save_fmp_data.py --manifest` already accepts) — no new fetch/rate-limit/retry machinery was written. `refresh_cache.py` itself is the *daily* cadence-staleness queue; this script is a distinct, deliberately-invoked *bulk* pass and doesn't touch or compete with the daily cron.

## Usage

```
python execution/fmp_backpop.py                          # dry-run: manifest + report only, no network calls
python execution/fmp_backpop.py --tickers NU,MELI         # scope to specific tickers
python execution/fmp_backpop.py --apply --max-calls 5000  # fetch + index + extract
```

Default scope (no `--tickers`): portfolio + watchlist + evaluation (`ANALYZED_LIST_TYPES`). `--tickers` overrides to an explicit comma-separated list — use this to cover the tracked universe more broadly (index_member/etf) if the owner wants those refreshed too during a given window.

`--apply` **requires** `--max-calls` — a bounded budget the owner sets per invocation, matching `save_fmp_data.py`'s own `--max-calls` contract. There is no default cap; an unbounded paid-key run is exactly what this script exists to prevent.

`--apply` chains three existing, already-tested pieces in sequence:
1. `execution/save_fmp_data.py --manifest <path> --max-calls N` — the fetch.
2. `pipeline.fmp_doc_index.index_fmp_files_for_ticker` (in-process, one call per ticker in scope) — indexes the newly-written JSON files into `documents`.
3. `execution/extract_facts.py --all` — extracts `documents` → `financial_facts` for every doc_type the dispatch table covers.

## Before running

1. Turn on the paid FMP key / confirm `FMP_API_KEY` in `.env` is the paid one for this window.
2. Set `FMP_TIER` (and optionally `FMP_RATE_LIMIT_PER_SEC`) in the environment to match the current subscription — same env vars `save_fmp_data.py` and `refresh_cache.py` already read. `fmp_backpop.py` does not set these itself.
3. Run without `--apply` first to see the manifest size and the `sec_covered_tickers` / `jobs_skipped_via_edgar` counts before spending budget.
4. Run `--apply --max-calls N` with `N` sized to the tier's daily/session limits (e.g. premium: 720/min, no daily cap — a few thousand calls fits in minutes; free/basic: 250/day — spread the manifest across multiple days via repeated `--apply` invocations, since `save_fmp_data.py --skip-existing`-style re-runs of the same manifest just re-fetch, so pass `--tickers` in smaller batches instead).

## Provenance

FMP rows insert through the normal extractors with `extracted_by='fmp'`, same as any other FMP run — no special-casing. The tier-aware loader (`SOURCE_QUALITY_TIER_RANK`, see `directives/edgar_pipeline.md`) keeps SEC facts winning disagreements regardless of which source was fetched more recently. This script only decides which FMP calls are worth spending budget on; it does not change how the two sources reconcile at read time.

## Known narrowness of the disagreement signal

`_check_source_disagreement` (`src/pipeline/validation_engine.py`) currently only checks 4 line_items (`revenue`, `operating_income`, `net_income`, `gross_profit`) across `>=2` distinct `source_type`s. A ticker can have real drift on a line_item outside that set (e.g. a mis-tagged balance-sheet line) without tripping `has_disagreement`. This is an existing validation-engine limitation, not something this script papers over — widening `_check_source_disagreement`'s line_item set is separate follow-up work, not required for this PR.
