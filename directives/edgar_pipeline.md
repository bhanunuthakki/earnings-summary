# EDGAR statement pipeline — continuous free freshness for financial_facts

**Status**: Active (built 2026-07-02, PR chain #767+). Owner decision, verbatim: *"Build pipeline for EDGAR; I will also get FMP every 6 months or so for a full backpopulation from last FMP update."*

**Why this exists**: the 2026-07-02 full-program review found `financial_facts` 97.5% `extracted_by='fmp'` — a single point of failure on a paid key. EDGAR companyfacts is the issuer's own filed data, free, and continuous. Division of labor: **EDGAR = weekly freshness; FMP = ~6-monthly paid bulk backpopulation** (see `directives/fmp_backpop.md`).

## Architecture

- `src/pipeline/sec_xbrl.py` — fetch + parse. `TAG_LADDERS` maps ~45 canonical `line_item` names (verbatim the FMP extractor names, so every reader / `source_disagreement` check / tier-dedup works unchanged) to ordered GAAP+IFRS tag ladders. First rung with data wins per logical period; the winning tag is recorded in `FactLocator.json_path`. One `documents` row per SEC accession (provenance), raw payloads at `data/historical/sec/{T}_companyfacts.json`.
- `execution/fetch_sec_xbrl.py` — CLI. Every SEC request is bound to the active stored `tracked_companies` identity and `list_type`: portfolio is automatic, evaluation requires explicit `--ticker T`, and watchlist/index/ETF/unknown names fail closed. `--all-mapped` is compatibility syntax and cannot widen automatic scope. Tracked authorized names missing a CIK emit `sec_cik_map_stale` on stderr.
- Tier precedence: SEC facts score confidence 1.00 (`sec_official` + deterministic) vs FMP 0.94, and `SOURCE_QUALITY_TIER_RANK` puts `sec_official` first — **where the two disagree, the filed SEC number wins by design**.

## Cadence

Weekly Windows scheduled task `\earnings-summary\fetch_sec_xbrl`, Saturday 02:00 (`cron/fetch_sec_xbrl.task.xml` + `cron/run_fetch_sec_xbrl.bat`). Also runnable inside the quarterly refresh DAG via `execution/quarterly_refresh.py --fetch-sec`.

- **Logical Idempotency Key:** SEC accession plus canonical fact locator and period.
- **Content Identity:** SHA-256 of the exact SEC response/document bytes.
- **Observation Version:** accession/filing identity, source filing time, fetched-at
  knowledge time, and Content Identity.
- **Attempt Identity:** unique scheduled or interactive ingestion invocation and receipt.

Rate limit: 0.2s between authorized portfolio tickers (~5 req/sec, half SEC's 10 req/sec cap) with the identifying User-Agent in `_HEADERS`. Missing/ambiguous identity, an invalid role, or denied depth is skipped before network access. At the direct CompanyFacts boundary, HTTP 401/403 becomes a typed auth denial and halts the current job without retry; ordinary non-auth transport failures and per-ticker schema failures remain visible and isolated.

Register (or re-register after editing the XML) from the MAIN checkout — editing the XML alone does NOT update the live task:

```
schtasks /create /tn "\earnings-summary\fetch_sec_xbrl" /xml "C:\Users\bhanu\.gemini\antigravity\scratch\earnings-summary\cron\fetch_sec_xbrl.task.xml" /f
```

## Conventions the ladders enforce (do not regress)

- **Sign**: FMP stores cash outflows negative. GAAP/IFRS `Payments*`/`Purchase*` elements are positive payment amounts, so those ladders carry `sign=-1` (capex, investments_in_ppe, buybacks, dividends, acquisitions). Verified against prod FMP rows 2026-07-02.
- **Units**: eps/eps_diluted = `actual` + currency (from `USD/shares`-style unit keys); weighted_avg_shares* = `count` + NULL currency. Matches FMP rows exactly.
- **Currency**: one modal currency per tag (the code with the most entries; ties alphabetical) — keeps dual-tagging filers (TSM: TWD + USD) on the FMP-consistent local-currency series. TWD added to the `Currency` enum for TSM.
- **Periods**: duration facts use SEC `fp` when it names a quarter, else an FYE-relative month partition (FYE month inferred per payload from annual filings — handles MU Aug, VEEV Jan, BHP Jun). 6M/9M YTD aggregations are skipped. Instant (balance-sheet) facts resolve purely by the partition (`fp` names the *filing's* period, not the snapshot's); FYE snapshots dual-write **FY and Q4**, mirroring FMP's annual + quarterly endpoints.
- **Equity semantics**: `StockholdersEquity` (parent-only) → `total_stockholders_equity`; the NCI-inclusive tag → `total_equity`. IFRS `ProfitLossAttributableToOwnersOfParent` outranks total `ProfitLoss` for `net_income`.

## Coverage & honest degradation (FMP keeps filling the gaps)

- **No SEC registration** (`NO_SEC_FILERS`): FLKR (ETF), IVN (TSX-only), NTDOY (unsponsored OTC ADR). EDGAR can never cover these.
- **Deliberately unmapped line_items** (no faithful XBRL tag): `property_plant_equipment_net` (FMP folds operating-lease ROU assets in — AMZN FY24 252.7B tag vs 328.8B FMP), `ebit`/`ebitda`/`free_cash_flow`/`total_debt`/`net_debt`/`operating_expenses` (FMP-derived aggregates), FMP's `*_cf` duplicate names, working-capital detail lines.
- **Per-company tag gaps are honest**: META tags no gross profit, AMZN no standard-tagged R&D (its "technology and infrastructure" is FMP's normalization), VEEV/AMZN pay no dividends. No approximation — absent tag, absent row.
- **IFRS filers** (NVO/TSM/NU/BHP/HDB/SE/STNE...) get the ifrs-full rungs; NU lacks cost_of_revenue/operating_income tags (bank income-statement shape). **BHP half-year (H1/H2) durations are skipped** (the 6-month YTD guard); its FY + instant facts land.
- **CFLT** is pinned to its historical CIK (dropped from `company_tickers.json` mid-acquisition; companyfacts still serves history).

## Refreshing CIK_MAP

Re-query `https://www.sec.gov/files/company_tickers.json` (identifying User-Agent required) and reverse-lookup new tickers; keep entries 10-digit zero-padded. Tracked-but-unmapped names surface as `sec_cik_map_stale` events in the weekly cron log. A genuinely unregistered name goes into `NO_SEC_FILERS` instead (the tests enforce the two sets stay disjoint and jointly cover the tracked universe).
