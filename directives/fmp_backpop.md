# FMP backpopulation — the ~6-monthly paid bulk refresh

**Status**: Active (built 2026-07-02, PR chain #767+). Owner decision, verbatim: *"Build pipeline for EDGAR; I will also get FMP every 6 months or so for a full backpopulation from last FMP update."*

**Division of labor**: `directives/edgar_pipeline.md` (EDGAR = continuous free weekly freshness for the ~45 statement line_items) + this directive (FMP = periodic paid bulk refresh for everything else — segments, growth series, TTM, ratios, analyst estimates, DCF, price history, peers, 10-K/10-Q JSON — plus a safety-net re-fetch of statements where EDGAR has a gap).

## What the script does

`execution/fmp_backpop.py` builds a `save_fmp_data.py --manifest`-shaped job list, diff-aware against EDGAR coverage:

- **Always included** (61 of the 67 `per_ticker_jobs` endpoints): everything EDGAR structurally cannot provide — segmentation, growth series, TTM statements, ratios/key-metrics, analyst estimates, DCF, 10-K/10-Q JSON, price history, peers, profile, etc. This is the actual point of the ~6-monthly window; EDGAR never touches these. Exception (2026-07-30, PR #1086): `index_member` rows resolve to the shallow peer contract (`save_fmp_data.PEER_ENDPOINT_ALLOWLIST`), so a backpop scoped to peers via `--tickers` fetches only the current allowed families at peer depth. The executable allowlist owns membership; prose counts can become stale.
- **Diff-aware skip candidates** (6 jobs: income-statement / balance-sheet-statement / cashflow-statement × annual/quarter): skipped for a ticker ONLY when `sec_covers_well()` returns `True` — at least one `financial_facts` row with `extracted_by='sec_xbrl'` and `period_end` within `SEC_FRESHNESS_DAYS` (400), AND zero unresolved `validation_issues` rows with `rule='source_disagreement'` for that ticker. Any gap (no CIK, EDGAR never ran, stale coverage, an active disagreement) falls back to fetching the FMP statement too.

This mirrors the existing `execution/refresh_cache.py` manifest contract exactly (same `[{ticker, endpoint, period}]` shape `save_fmp_data.py --manifest` already accepts) — no new fetch/rate-limit/retry machinery was written. `refresh_cache.py` itself is the *daily* cadence-staleness queue; this script is a distinct, deliberately-invoked *bulk* pass and doesn't touch or compete with the daily cron.

## Usage

```sh
python execution/sqlite_bootstrap.py execution/fmp_backpop.py \
  --db /absolute/path/to/approved-state.db --repo-root /absolute/path/to/retained-state
python execution/sqlite_bootstrap.py execution/fmp_backpop.py \
  --db /absolute/path/to/approved-state.db --repo-root /absolute/path/to/retained-state \
  --tickers NU,MELI --apply --max-calls 5000
```

Resolve live database and artifact-root authority through the host runbook before
using these examples. Mac runs require explicit isolated test or approved restored
state. The command rejects the checkout-default and unavailable database before
creating artifacts. Planning opens SQLite read-only and makes no network call.
Executable source paths remain separate from the selected retained artifact root.

Default scope (no `--tickers`): portfolio + watchlist + evaluation (`ANALYZED_LIST_TYPES`). `--tickers` overrides to an explicit comma-separated list of current-user, classified, nonarchived tracked companies. Explicit permitted index-member/ETF scope retains its existing catalog depth. Unknown, archived and other-user rows cannot become implicit `none` companies.

`--apply` **requires** `--max-calls` — a bounded budget the owner sets per invocation, matching `save_fmp_data.py`'s own `--max-calls` contract. There is no default cap; an unbounded paid-key run is exactly what this script exists to prevent.

`--apply` uses the existing lock owners and processing interfaces:

1. `save_fmp_data.py` receives the exact manifest, database, retained root, ticker
   scope and positive attempt limit. Its plural `--work-receipts` surface returns
   existing typed per-work results. The singular `--work-receipt` contract remains
   unchanged. The parent holds the shared artifact/budget lock across the full chain.
   The fetch child reuses only a validated live ancestor proof. Database locking
   starts at indexing, so network work does not hold the coarse database lock.
   The attempt limit covers shared-client retries and fallback rungs. Plural
   runs stop on account or schema failures and mark dependent remaining work
   `prerequisite_not_attempted`.
2. The orchestrator reconciles every manifest coordinate and verifies each success
   against retained bytes. Missing, duplicate, extra, stale, denied, failed and
   budget-not-attempted work cannot be inferred as success from exit code zero.
   An empty result for the optional `stock-peers/peers` coordinate remains an
   explicit unavailable gap. Empty financial envelopes fail the shared payload
   contract before artifact or success writes. Failed
   prerequisites stop dependent stages.
3. `index_fmp_files_for_ticker` registers retained FMP files only for selected
   tickers at the selected root. This includes valid previously retained files;
   it is not an assertion that every indexed file came from this fetch.
4. `extract_facts.py --tickers <exact scope> --db <same database> --repo-root
   <same retained root>` uses the existing extraction owner. It does not expand
   the selection through `--all`. Index failure prevents extraction.

A reconciled manifest proves the recorded work outcomes and captured hashes.
It does not prove archive completeness, semantic admission or shared-reader
parity. Empty history is unavailable evidence, never a financial zero. Retain
failed coordinates for bounded recovery through the existing FMP work owners.

## Before running

1. Turn on the paid FMP key / confirm `FMP_API_KEY` in `.env` is the paid one for this window.
2. Set `FMP_TIER` (and optionally `FMP_RATE_LIMIT_PER_SEC`) in the environment to match the current subscription — same env vars `save_fmp_data.py` and `refresh_cache.py` already read. `fmp_backpop.py` does not set these itself.
3. Run without `--apply` first to see the manifest size and the `sec_covered_tickers` / `jobs_skipped_via_edgar` counts before spending budget.
4. Size the reviewed manifest and positive attempt budget to the verified current endpoint entitlement, shared quota and bandwidth allowance. A configured tier is not evidence of purchased access. Do not use an old prose rate or a theoretical request ceiling as a runtime estimate. Recover failed work through the existing exact-work machinery rather than repeatedly fetching the entire successful manifest.

## Provenance

FMP rows insert through the normal extractors with `extracted_by='fmp'`, same as any other FMP run — no special-casing. The tier-aware loader (`SOURCE_QUALITY_TIER_RANK`, see `directives/edgar_pipeline.md`) keeps SEC facts winning disagreements regardless of which source was fetched more recently. This script only decides which FMP calls are worth spending budget on; it does not change how the two sources reconcile at read time.

## Known narrowness of the disagreement signal

`_check_source_disagreement` (`src/pipeline/validation_engine.py`) currently only checks 4 line_items (`revenue`, `operating_income`, `net_income`, `gross_profit`) across `>=2` distinct `source_type`s. A ticker can have real drift on a line_item outside that set (e.g. a mis-tagged balance-sheet line) without tripping `has_disagreement`. This is an existing validation-engine limitation, not something this script papers over — widening `_check_source_disagreement`'s line_item set is separate follow-up work, not required for this PR.
