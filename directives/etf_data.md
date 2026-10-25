# ETF Published-Data Pipeline

The data lane for ETF instruments on the evaluation list (AVDV, AVUV, VWO, …).
First-principles source hierarchy: everything an ETF evaluation needs is
**published** — by regulation (SEC N-PORT) and by the fund administrator
(fund pages). The FMP ETF endpoints are optional enrichment only; nothing in
the evaluation lane depends on them.

## Sources (in authority order)

| Rung | Source | What it provides | Freshness |
|---|---|---|---|
| 1. Spine | SEC EDGAR Form NPORT-P (`etf_sources/nport.py`) | Complete holdings: constituent, name, ticker-when-disclosed, **country** (`invCountry`), shares, value, % of net assets | Quarterly public, ~60-day lag. Acceptable: factor/thematic baskets churn slowly |
| 2. Overlay | Issuer fund-page APIs (`etf_sources/issuer_registry.py`) | Fresher holdings; basket characteristics (expense ratio, P/E, P/B, weighted mkt cap) where published | Daily/monthly, issuer-dependent |
| 3. Prices | yfinance dividend-adjusted closes → `data/factor_proxies/<T>.json` | Daily price series for fit/OLS/what-if | Daily (morning stage 0g refreshes proxies; ETF series fetched at onboard + weekly) |
| 4. Enrichment (optional) | FMP `/stable/etf/{info,holdings}` (`execution/fetch_etf_data.py`) | ER/AUM/NAV when the plan allows | Plan-gated; 402s tolerated |

### EDGAR resolution chain (spine)

1. `www.sec.gov/files/company_tickers_mf.json` → ticker → (trust CIK, seriesId).
2. `data.sec.gov/submissions/CIK##########.json` → recent NPORT-P accessions
   (a trust interleaves many series; up to 24 accessions probed, newest first).
3. Accession `primary_doc.xml` → parse; keep the document whose `<genInfo>
   <seriesId>` matches. `repPdDate` is the portfolio as-of date.

### Issuer adapter status

- **vanguard** — LIVE (scouted 2026-07-10).
  `investor.vanguard.com/investment-products/etfs/profile/api/{T}/portfolio-holding/stock`
  (paginated `fund.entity[]`; `percentWeight` in percent units; `asOfDate`
  top-level) and `…/api/{T}/profile` (`fundProfile.expenseRatio`, percent
  units). No basket P/E-P/B on this API.
- **avantis** — NOT YET. avantisinvestors.com is JS-rendered (AEM) with no
  static data URLs; needs a network-tab scout of the fund page's XHR calls to
  find the holdings/characteristics endpoints. Until then Avantis names ride
  the N-PORT spine (holdings + country land; ER/P-B come from FMP enrichment
  or stay absent → dependent factors degrade neutral+partial, never faked).

## Output schema

- `etf_holdings` (alembic 0044 + 0144): `(ticker, as_of_date,
  constituent_ticker)` PK; `weight_pct` **decimal fraction** (0.0742 = 7.42%);
  `country` ISO-3166 alpha-2 from N-PORT; `source` ∈ {`nport`,
  `issuer:<name>`, `fmp`}. Snapshots from different sources coexist on their
  own `as_of_date` rows; readers take `MAX(as_of_date)`.
- `etf_profile` (0044 + 0144): identity + `pe_ratio`/`pb_ratio` (multiples,
  14.3 = 14.3x), `weighted_avg_mktcap_usd_m`, `characteristics_as_of`,
  `characteristics_source`. Characteristics merge read-modify-write — a
  source that publishes only ER never blanks another source's fields.
- `etf_profile.field_evidence_json`: per-field value, source, timezone-aware
  `captured_at`, and nullable source-published `source_as_of` for deterministic
  investment labels. Legacy rows default to `{}`; no backfilled timestamp is
  invented. An issuer overlay updates receipts only for fields it actually
  publishes and does not renew `profile_fetched_at` for the original profile.
  Holdings dates must not be assigned to separately fetched profile fields.
- Live FMP enrichment retains the exact decompressed JSON response bytes through
  `register_fmp_snapshot`. It no longer updates the mutable
  `<TICKER>_etf_{info,holdings}.json` cache. Each received response has its own
  capture UUID and aware receive time. Equal bytes reuse the document/version
  and add a retrieval observation; exact replay preserves its original clocks.
  Existing source URLs and first-observation times do not change.
- FMP field receipts also require a complete document ID, native document version,
  retrieval observation, SHA256, credential-free source URL, JSON pointer and
  parser version. Pointers identify raw values before normalization. Only the
  five existing label fields receive these links. Saved-cache and arbitrary
  payload ingestion remain available but cannot create verified capture links.
  Issuer receipt policy is unchanged. Capture linkage does not establish
  canonical financial-fact admission, current fund facts or archive completeness.
- Label acquisition admission uses `etf_weekly_capture.v1`: at most seven days
  since a verified capture, derived from the weekly refresh cadence below.
  This means **recently captured**, not proof that underlying issuer facts are
  current. Undated source observations retain an explicit unknown-publication
  warning. Dated observations predating their capture remain historical/unknown
  currency and cannot authorize current profile labels until an applicable
  issuer/field validity policy exists. There is no universal issuer-data TTL.
  Future/ambiguous timestamps, changed values without matching receipts, legacy
  rows, and filesystem-mtime-only cache replays cannot authorize labels. Other
  independently admitted style/book evidence remains usable.
- Prices: `data/factor_proxies/<T>.json` `{"ticker","fetched_at","rows":
  [[iso_date, close]]}` — `allocation.price_history.load_daily_closes` falls
  back to this store when the FMP price-chart cache has nothing (FMP wins
  when both exist).

## Cadence, identity, and budget

- **Onboard**: `execution/onboard_ticker.py` routes `instrument_type='etf'`
  to the published-data path (`run_etf_onboarding`) and **skips**
  quarterly_refresh / transcripts / IR / Say-Do. Escape hatch when the FMP
  profile is unavailable for auto-classification: `--instrument etf`.
- **Refresh**: `execution/fetch_etf_published_data.py --ticker T` — weekly is
  ample (holdings churn slowly); register alongside the IR-document weekly
  cadence when ETFs are actively tracked. Role-in-portfolio research runs
  only on an explicit owner request through `execution/build_etf_workup.py`.
  Onboarding and portfolio-fit refresh do not generate role research.
  Published-data collection and deterministic fit/ETF-score refresh retain
  their existing schedules and source boundaries.
- **Logical Idempotency Key:** `etf_{ticker}_{rep_period_date}_{source}`. An
  already-ingested N-PORT report is an explicit `already_done` skip; `--force`
  requests reprocessing without changing the logical identity.
- **Content Identity:** SHA-256 of the exact N-PORT, issuer, or price payload.
- **Observation Version:** representative period/source filing identity plus
  source-published and fetched-at times.
- **Attempt Identity:** unique onboarding or refresh invocation and its receipt.
  Price-fetch presence checks are repeat-safety guards, not identities.
- **Rate limits**: SEC fair-access — declared UA with contact, ≥150 ms
  between requests; ≤ ~26 requests per
  ticker refresh (1 map + 1 submissions + ≤24 probes). Vanguard API: plain
  GETs, ~10-60 pages per holdings refresh, UA declared. yfinance: 1 call.

## Failure modes

- Live FMP info requires one object with the requested symbol. Holdings require
  an array of objects; constituent symbols are not fund identities. Duplicate
  JSON keys, nonfinite numbers, body/payload mismatch, invalid capture clocks,
  missing captures or a different final endpoint halt before database writes.
- The optional FMP unit saves both snapshots, source observations, links, profile
  and holdings in one caller-owned transaction. Its local savepoint preserves
  unrelated pending writes. The caller commits only a complete unit. The
  published-data CLI commits SEC/issuer/price results before optional FMP, and
  rolls back failed enrichment before reporting it unavailable.
- The legacy document table uses global SHA256 identity. Equal bytes registered
  under another ticker or document type cause an explicit optional FMP failure;
  a shared empty holdings body can hit this limit. No identity is silently
  changed. Immutable files installed before a database rollback can remain as
  content-addressed residue. They are not registered capture proof and cannot
  supply a recovered receive clock. Existing files are never overwritten.
- **N-PORT schema drift** (fetched but unparseable): `NportParseError` —
  HALT, raw XML dumped to `.tmp/etf_nport/<T>_<accession>.xml`. Fix the
  parser/directive; never guess-fix in the agent loop.
- **N-PORT unavailable** (network, ticker not in fund map, series not in
  recent window): degrade — status `unavailable`, look-through analytics
  render their missing state.
- **Issuer overlay failure** (page redesign, network): soft degrade to the
  spine, logged `issuer_overlay_failed`. Adapters parse defensively and
  return None on surprise — an issuer redesign must never block evaluation.
- **yfinance failure**: last-good proxy file untouched; `price_status=failed`
  only when there was nothing on file at all.
- **Both holdings sources fail**: onboard/refresh exits 1 with a WARNING —
  fit still computes from prices; overlap/geography factors degrade.

## Safety rails (what ETFs must never touch)

- `src/dcf/universe.py` excludes `instrument_type='etf'` — no FCFF DCF over
  a fund. `execution/build_artifacts.py` dispatches ETFs to the ETF brief.
- Say-Do never runs for ETFs (`_saydo_should_run` instrument gate).
- The list-type reconciler never promotes ETFs to `portfolio` (by design).
