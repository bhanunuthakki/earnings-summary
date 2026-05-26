# Cross-asset data model

**Status**: Scoping (Phase 1). Adopted path is **C — extend `tracked_companies` as the polymorphic instruments table** rather than the brief's Option A (rename to `instruments` + per-kind detail tables) or Option B (single table + JSON). Reasoning in §2.

## Why this design exists

The fresh-review memo flagged that the system treats every tracked instrument as an equity:
- `tracked_companies` is the only universe registry
- `financial_facts` / `kpi_facts` / `segment_facts` assume income statement + per-company KPIs + operating segments
- `dcf_runs` assumes equity valuation via discounted FCF + terminal multiple
- 19 of 19 report sections assume an issuer with quarterly disclosures

But the user holds (or will hold) a real portfolio: equities + ETFs (sector / index exposure) + cash + bonds + options + FX. A naïve "add asset_kind to tracked_companies" would leak equity assumptions everywhere. This doc designs the cross-asset extension before any code lands.

---

## 1. Asset taxonomy

The canonical kinds the system aims to support, and what's a starter MVP vs. a future stub:

| Kind | Label | MVP priority | Identifier | Native sources | Native facts | Native valuation | Native analytical lenses |
|---|---|---|---|---|---|---|---|
| `equity` | Common stock | Already supported | ticker (US: NYSE/Nasdaq) | FMP statements + SEC EDGAR + IR | financial_facts, kpi_facts, segment_facts | DCF (consolidated + segment) | Thesis, Say-Do, bear case, filing intelligence, 5-min reread, exec comp, insider txns |
| `adr` | American Depositary Receipt | Already supported (variant of equity) | ticker (NU, MELI, NVO, RIO) | FMP + 20-F filings | Same as equity (filing_regime='20-F') | DCF | Same as equity, with FX overlay on cashflows |
| `etf` | Exchange-traded fund | **Phase 2 MVP** | ticker (SOXX, SPY, FLKR) | FMP etf-holdings + etf-info + profile + price-chart | `etf_profile` + `etf_holdings` (new, this PR) | NAV / premium-discount | Sector-exposure read-through, rotation / rebalance lens |
| `cash` | Cash + equivalents | Future (positions-only) | account label (CCY) | portfolio-tracker | `positions` already covers it | Balance × FX | Allocation %, sweep-rate yield |
| `bond` | Fixed income | Future | ISIN / CUSIP | TreasuryDirect + FMP corporate-bond | `bond_profile` + `bond_yields_daily` (future) | YTM, duration | Credit thesis, duration sleeve, rate-move scenarios |
| `option` | Listed options | Future | OCC option_symbol | FMP options chains / CBOE | `option_chain_snapshots` + `option_positions` (future) | Greeks + IV, B-S | Assignment risk, decay-this-quarter, underlying read-through |
| `fx_position` | FX forward / spot | Future | ccy_pair | Already have `fx_rates` from macro pipeline | `fx_positions` (future) | Spot × notional | Currency overlay on ADRs / international assets |
| `commodity_position` | Metals / energy | Future | symbol (GC=F, CL=F) | Yahoo Finance / FMP commodities | `commodity_positions` (future) | Spot × notional | Inflation hedge lens |
| `private_equity` | Illiquid PE / VC | Future stub | fund name | Manual NAV statements | `nav_marks` (future) | NAV mark | Vintage / J-curve lens |
| `real_estate` | Direct RE / REITs (non-listed) | Future stub | property label | Manual | `nav_marks` reused | NAV × ownership | Cap-rate trend lens |

**MVP scope this PR**: `equity` (existing) + `etf` (new, end-to-end with one ticker — SOXX). Everything else gets enum slots in `InstrumentType` so future PRs slot in without a CHECK-constraint migration.

---

## 2. Schema proposal — adopted path: extend `tracked_companies` in place

### 2.1 Why neither Option A nor Option B from the brief

The brief proposed:
- **A**: Polymorphic `instruments` table + per-kind detail tables; rename `tracked_companies` → `instruments`; existing fact tables gain `instrument_id` FK.
- **B**: Single `instruments` table + JSON detail column.

Both rest on a wrong assumption — that `tracked_companies` is equity-coupled and fact tables FK to it. The actual state (confirmed via migration audit 0000-0043):

1. **`tracked_companies` already IS the polymorphic instruments table.** Migration `0001_companies_provenance` (2026-05-03) added `instrument_type VARCHAR` with values `equity | adr | etf` via the `InstrumentType` StrEnum in [`src/models/companies.py`](src/models/companies.py). The column is already populated for the 28 user-tracked tickers and NULL for the index-universe backfill.
2. **Fact tables do NOT FK to `tracked_companies.id`.** Every fact table (`financial_facts`, `kpi_facts`, `segment_facts`, `dcf_runs`, `thesis_state`, `thesis_evaluations`, `management_commitments`, `expected_earnings`, `earnings_surprises`, `predictions`, `insights`, `llm_calls`, `llm_artifacts`, `insider_transactions`, `exec_comp_packages`, …) uses **ticker as a string** as the join key. Adding a new instrument requires zero changes to fact-table DDL.
3. **Routing already dispatches on `instrument_type`.** [`src/pipeline/source_routing.py:89`](src/pipeline/source_routing.py:89) branches on `InstrumentType.ETF` to a smaller source set (`_ETF_SOURCES = {FMP}`). FMP fetcher [`execution/save_fmp_data.py:313`](execution/save_fmp_data.py:313) already does `skip_10k = list_type in ("index_member", "etf")`.

Renaming `tracked_companies` to `instruments` would touch >60 SQL string literals across every section builder, every pipeline module, every CLI, every migration, and ~30 tests. The cost is enormous and the gain is purely cosmetic. The 0028 table-recreation migration also recreates triggers — moving the table costs us 8 triggers from 0026 + 8 more from 0043, each of which has to be carefully resequenced.

### 2.2 Adopted path: Option C — keep `tracked_companies`, add per-kind detail tables

```
tracked_companies                       — unchanged; the polymorphic instruments table
  id, ticker, name, instrument_type     — instrument_type is the kind discriminator
  list_type, sec_validated, archived_at — universe-membership / lifecycle
  ...                                   — equity-leaning columns are nullable
                                          (fmp_data_upto, manual_data_quarters,
                                          publishes_*, fiscal_year_end, filing_regime
                                          — all already nullable; NULL = N/A for kind)

etf_profile(ticker PK, expense_ratio, aum_usd, inception_date,
            asset_class, issuer, benchmark_index,
            domicile, listed_exchange, distribution_yield,
            description, sector_label, profile_fetched_at, source)
            — one row per ETF; sparse where data is missing

etf_holdings(ticker, as_of_date, constituent_ticker, weight_pct,
             shares_held, market_value_usd, name, sector,
             rank_position, PRIMARY KEY(ticker, as_of_date, constituent_ticker))
             — point-in-time holdings; daily/weekly/monthly cadence per ETF
```

Future kinds extend the same pattern: `bond_profile` + `bond_yields_daily`, `option_chain_snapshots` + `option_positions`, etc. Each new kind is one or two `<kind>_*` tables; no further changes to `tracked_companies`.

**Fact tables stay equity-shaped.** A row in `financial_facts` for an ETF would be meaningless (an ETF has no income statement of its own), so we don't write one. The discriminator stays at the section-builder layer: each builder checks `instrument_type` and either runs its equity logic or returns `NOT_APPLICABLE` (renderers already handle this — see §4).

**No `instrument_id` FK proliferation.** Because fact tables join on ticker, an ETF is just a ticker that has rows in `etf_profile` + `etf_holdings` and no rows in `financial_facts` / `dcf_runs` / etc. The polymorphism is implicit in which side tables a ticker has rows in, plus the discriminator on `tracked_companies.instrument_type`.

### 2.3 Cleanup the existing overlap

`tracked_companies.list_type` currently allows `'etf'` as a value (added by an earlier FMP backfill — see [`src/models/companies.py:30`](src/models/companies.py:30) note). That's classification leakage — `list_type` should be about *universe membership* (portfolio / watchlist / evaluation / index_member / none) and `instrument_type` should be about *asset kind*. Keeping `'etf'` valid in `list_type` would cause confusion the moment we add an ETF to the portfolio (it'd be `list_type='portfolio'` AND `instrument_type='etf'`).

**Migration plan for the overlap**:
- Phase 2 (this PR): Add `etf_profile` / `etf_holdings`. Do NOT touch `list_type` CHECK constraint. Backfill SOXX as `list_type='portfolio'` (the user actually holds it) + `instrument_type='etf'`. Tolerate the FLKR-style legacy `list_type='etf'` rows by reading both into the ETF flow.
- Future PR: Reclassify FLKR (and any other `list_type='etf'`) into `list_type='watchlist'` (or `portfolio` if user holds), then drop `'etf'` from the `list_type` CHECK in a follow-up migration. Out of scope for this PR; flagged.

### 2.4 Why not put ETF data in JSON columns on `tracked_companies`

Option B's "stash everything in JSON" path was tempting for a one-shot ETF MVP but would:
- Block typed queries ("show me the top 10 SOXX holdings by weight" → JSON1 SQL gymnastics vs. an indexed table scan)
- Block bridge queries to fact tables ("how much portfolio AMZN exposure do I have including ETF look-through?" — needs a real `etf_holdings` table)
- Hide the schema growth from migrations (you'd see the columns drift inside JSON over time with no Alembic record)

Per-kind detail tables stay queryable, indexable, and migrate cleanly.

---

## 3. Per-asset-kind data sources

### Equity (existing — for reference)
- FMP: statements, profile, key-metrics, price-chart, earnings-calendar, transcripts
- SEC EDGAR: 10-K / 10-Q XBRL + filing text
- IR documents: manual + categorize_ir_uploads.py
- Transcripts: aggregator chain + fetch_audio_transcripts.py fallback

### ETF (this PR's MVP)
- FMP endpoints used:
  - `etf-holdings/{symbol}` — top constituents with weight + shares
  - `etf-info/{symbol}` — issuer, expense ratio, AUM, inception, asset class
  - `profile/{symbol}` — sector label, exchange, description (reuses existing equity profile fetcher)
  - `historical-price-eod/{symbol}` — price history (reuses existing price-chart fetcher)
- Cadence: holdings refresh weekly (or on-demand); info refresh monthly; price daily (rides existing pipeline)
- No SEC filings (ETFs file N-PORT/N-CSR, out of scope — could add later for full holdings)
- No IR docs, transcripts, audio

### Bond (future)
- TreasuryDirect for govvies (auction results, yield curve)
- FMP corporate-bond endpoint for issuer bonds
- New tables: `bond_profile` (ISIN, issuer, coupon, maturity, rating), `bond_yields_daily` (date, ytm, ytw, oas)
- Cadence: yield curve daily, issuer-specific weekly

### Option (future)
- FMP options chains (`stock-option-chain/{underlying}/{expiry}`)
- CBOE for IV surface data if FMP coverage is thin
- New tables: `option_chain_snapshots` (timestamp, strike, expiry, type, bid, ask, iv, delta, gamma, theta, vega, oi, vol), `option_positions` (account, position, qty, opened_at)
- Cadence: snapshots intraday during market hours for positions held; weekly otherwise

### Cash (future, positions-only)
- Source: portfolio-tracker integration (existing `portfolio_position` section reads from it for equity)
- No new tables; reuse `positions` from portfolio-tracker schema

### FX position (future)
- Already have `fx_rates` from migration 0042 + Week 3's macro_series feeder
- New table: `fx_positions` (account, ccy_pair, notional, direction, opened_at, settle_at)

---

## 4. Analytical adaptation

Each existing report section's behavior across the asset kinds we plan to support:

| Section | Equity | ETF | Bond | Option |
|---|---|---|---|---|
| §0 Portfolio position | OK | OK (cost basis, % of port, sector overlap via look-through) | OK (par, accrued, maturity, % at risk) | OK (contracts, value, decay, breakeven, position delta) |
| §1 Snapshot | OK (DCF card + verdict) | **N/A** — replaced by §1 ETF Profile (AUM, ER, premium/discount) | N/A — replaced by §1 Bond Profile (YTM, duration) | N/A — replaced by §1 Option Profile (Greeks, breakeven) |
| §1 Eval snapshot | OK | **N/A** | N/A | N/A |
| §2 Company description | OK | Adapted (Fund description + benchmark + asset class) | N/A | N/A |
| §3 Thesis & KPIs | OK | Adapted (allocation thesis, weight-target tier-1s) | Adapted (credit thesis, downgrade-risk tier-1s) | Adapted (trade thesis, breakeven tier-1) |
| §4 Financials | OK | **N/A** | N/A | N/A |
| §5 Segments | OK | **Replaced by §5 Holdings + Sector breakdown** | N/A | N/A |
| §6 Earnings | OK | **N/A** | N/A | N/A |
| §7 Say-Do | OK | N/A (ETFs don't make guidance) | N/A | N/A |
| §8 IR docs | OK | N/A | N/A | N/A |
| §9 Recent developments | OK | **OK — ETF news, rebalance announcements** | OK (issuer / rate news) | OK (underlying news) |
| §10 Bear case | OK | Adapted (concentration risk, tracking error, rate-sensitivity if duration matters) | Adapted (downgrade scenarios) | OK (assignment / pin risk) |
| §11 Valuation basis | OK (DCF + reverse DCF + multiples) | Adapted (premium-discount band, expense-ratio drag) | Adapted (YTM vs benchmark) | Adapted (IV vs realized) |
| §12 Filing intelligence | OK | N/A (N-PORT not in MVP) | N/A | N/A |
| §13 Exec comp | OK | N/A | N/A | N/A |
| §14 QA roster | OK | N/A | N/A | N/A |
| §15 Provenance | OK | **OK** (audits regardless of kind) | OK | OK |
| §16 Synthesis | OK | OK (cross-ticker theme lens applies) | OK | OK |
| §17 Appendix | OK | N/A (no transcripts) | N/A | N/A |

**Implementation contract for the MVP**: every section's `build()` returns a section with `status=SectionStatus.NOT_APPLICABLE` and `missing=None` when the asset kind doesn't apply. Renderers already silently hide `NOT_APPLICABLE` (see [`src/report/sections/portfolio_position.py`](src/report/sections/portfolio_position.py) pattern). Equity tests are unaffected because the dispatch only triggers when `tracked_companies.instrument_type == 'etf'`.

For this PR, only **§5 (replaced by ETF Holdings)** and **§1 (replaced by ETF Profile)** ship. Other sections silently emit `NOT_APPLICABLE` for ETF tickers and render as nothing.

---

## 5. Portfolio composition + position sizing

The existing [`portfolio_position` section](src/report/sections/portfolio_position.py) reads from the sibling portfolio-tracker DB and is **already kind-agnostic at the storage layer** — it joins by ticker, not by `tracked_companies.instrument_type`. The polymorphism it needs is on the display side:

- **Equity**: shares × price = market value, cost basis, unrealized P&L, % of portfolio (already implemented)
- **ETF**: same shape as equity (shares × price = MV) + ETF-specific overlay → "sector overlap with direct equity holdings via look-through" (this PR ships the data feed via `etf_holdings`; the section overlay is future work)
- **Bond**: par held, accrued interest, YTW, weighted-avg duration of sleeve, % at risk if 100bps move (future)
- **Option**: contracts, current value (last × 100), time decay this quarter, breakeven, position delta (future)
- **Cash**: balance per account, sweep rate, total % allocation (future)

For this PR we leave the `portfolio_position` section as-is. SOXX will render correctly because its shape (shares × price) is identical to equity. The ETF-specific look-through overlay is flagged for follow-up.

---

## 6. Cross-asset analytical questions enabled

The headline business reason for the schema work is that today the system can't answer questions that span asset kinds. With ETF holdings ingested:

1. **"What's my total semiconductor exposure including ETF look-through?"** — join SOXX holdings × my SOXX position weight + sum direct equity positions in NVDA / AVGO / etc.
2. **"What's my biggest single-name exposure when ETF look-through is included?"** — top-N across direct + look-through.
3. **"Am I doubly exposed to anything via both direct holding and an ETF?"** — find tickers appearing in both my equity portfolio and any ETF I hold.
4. **"What's my net cyclical / defensive sector split when ETF look-through is included?"** — sector tags from FMP profile + ETF look-through.
5. **"Which of my equity holdings are also top-10 holdings in a sector ETF I hold?"** (validates conviction overlap).
6. **"Did adding SOXX increase or decrease my portfolio's geographic concentration to US tech?"** (run before / after a hypothetical add).
7. *(future, with bonds)* **"What's my interest-rate-sensitive bucket?"** — bond duration sleeve + REITs + long-duration tech equity from existing sensitivities table.
8. *(future, with options)* **"What's my net delta-adjusted position in NVDA?"** — direct shares + option delta + ETF look-through.

The first 6 are answerable as soon as this PR's MVP lands. The schema is the unlock.

---

## 7. Migration strategy

Multi-quarter plan; this PR is **Phase A only**.

### Phase A (this PR — Migration 0044)
- Add `etf_profile` table (one row per ETF ticker)
- Add `etf_holdings` table (point-in-time, PK on ticker + as_of_date + constituent_ticker)
- Add NO changes to `tracked_companies` schema — it's already the polymorphic registry
- Add NO new `instrument_id` FK anywhere — ticker remains the join key
- Add NO bridge view / rename — `tracked_companies` keeps its name; cosmetic rename can ship in a Q3 PR if anyone cares
- `instrument_store.py` exposes a thin typed API: `get_instrument(ticker)`, `list_instruments_by_kind(kind)`, `upsert_etf_profile(...)`, `upsert_etf_holdings(...)`
- Section dispatch: `src/report/builder.py` reads `tracked_companies.instrument_type`; ETF-kind tickers route to a different builder set that returns `NOT_APPLICABLE` for the 14 equity sections and populates the 2 ETF sections (snapshot, holdings)
- Onboard `SOXX` end-to-end as the canary

### Phase B (future PR)
- Reclassify FLKR (`list_type='etf'`) into `list_type='watchlist'` or `portfolio`
- Drop `'etf'` from the `list_type` CHECK constraint via a 0028-style table-recreation migration (the orthogonality cleanup from §2.3)
- Cleanly separates "what kind of asset is this" (`instrument_type`) from "is the user actively tracking it" (`list_type`)

### Phase C (future PR — bonds)
- Add `bond_profile` + `bond_yields_daily`
- Add `instrument_type='bond'` to the `InstrumentType` StrEnum
- New section: §1 Bond Profile (replaces §1 Snapshot for `instrument_type='bond'`)
- Treasury feeds integration (probably via new `execution/fetch_treasury_yields.py`)

### Phase D (future PR — options)
- Add `option_chain_snapshots` + `option_positions`
- Add `instrument_type='option'` enum value
- New sections: §1 Option Profile + §5 Greeks
- Realized vs IV section (uses existing prices data)

### Phase E (future PR — view-layer cosmetic)
- IF AND ONLY IF a stakeholder wants `instruments`-vocabulary in SQL queries, add a `CREATE VIEW instruments AS SELECT * FROM tracked_companies`. The view costs nothing and gives external SQL clients a more honest name. Not required.

### What we explicitly are NOT doing
- We are NOT renaming `tracked_companies` to `instruments` in this PR or any planned PR. The rename is high-cost, low-value — `tracked_companies` already is the instruments table, just with a historical name. A follow-up VIEW (Phase E) gives the rename benefits at zero migration cost.
- We are NOT adding `instrument_id` FKs to fact tables. Ticker is the established join key and works for all kinds.
- We are NOT versioning `etf_holdings` rows via `superseded_by_id` — the natural key (ticker, as_of_date, constituent_ticker) gives us point-in-time queryability for free.

---

## Open questions / flagged follow-ups

1. **FLKR cleanup** — reclassify `list_type='etf'` rows in a follow-up PR + drop `'etf'` from the CHECK constraint. Pre-existing data; not blocking the MVP.
2. **Portfolio look-through overlay in §0** — once `etf_holdings` is populated, the `portfolio_position` section can render an "indirect holdings" sub-row showing sector / single-name overlap. Flagged for the next cross-asset PR.
3. **N-PORT ingestion** — for full ETF holdings (currently FMP gives ~top 50), eventually pull SEC N-PORT XML. Out of MVP scope.
4. **DEFER for now: the `instrument_id` FK question for `kpi_facts`**. Today equity-only. If/when we want ETF-specific KPIs (e.g. tracking error, NAV premium history), we can either (a) add an `instrument_id` column then or (b) keep ticker-string join + add `kpi_definitions` rows scoped to an ETF ticker. Don't preempt the decision.
5. **Trigger fanout** — the 8 self-update triggers in migration 0043 fire only on equity-shaped tables (financial_facts, kpi_facts, segment_facts, management_commitments, insider_transactions, exec_comp_packages, predictions). No changes needed for ETF MVP. When bond/option tables land, we'll add analogous triggers for their dirty propagation.

---

## Summary

The brief asked for a polymorphic instruments-first refactor. The codebase already had one — `tracked_companies` + `instrument_type` enum + ETF routing — installed in migration 0001 (2026-05-03) and never followed through with side-table support. This doc proposes finishing that 0001 work rather than starting a parallel architecture. Net effect: one migration (0044), two new tables, ~200 LOC of fetcher + section + store, one ETF (SOXX) ingested as canary. All 595 existing tests stay green because the dispatch only triggers when `instrument_type='etf'`, and no equity ticker has that value.
