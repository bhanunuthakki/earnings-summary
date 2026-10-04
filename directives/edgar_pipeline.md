# EDGAR statement pipeline — continuous free freshness for financial_facts

**Status**: Active (built 2026-07-02, PR chain #767+). Owner decision, verbatim: *"Build pipeline for EDGAR; I will also get FMP every 6 months or so for a full backpopulation from last FMP update."*

**Why this exists**: the 2026-07-02 full-program review found `financial_facts` 97.5% `extracted_by='fmp'` — a single point of failure on a paid key. EDGAR companyfacts is the issuer's own filed data, free, and continuous. Division of labor: **EDGAR = weekly freshness; FMP = ~6-monthly paid bulk backpopulation** (see `directives/fmp_backpop.md`).

## Architecture

- `src/pipeline/sec_xbrl.py` — fetch + parse. `TAG_LADDERS` maps canonical `line_item` names to ordered GAAP+IFRS tag ladders. First rung with data wins per logical period; the winning tag is recorded in `FactLocator.json_path`. Exact CompanyFacts response bytes are retained as immutable snapshot documents; the conventional `{T}_companyfacts.json` is a mutable working cache, not historical evidence. CompanyFacts is a fact feed, not a retained copy of every native filing.
- `execution/fetch_sec_xbrl.py` — CLI. Every SEC request is bound to active stored identity, role, and instrument. Portfolio, evaluation, and watchlist receive automatic full acquisition; corporate sources require equity/ADR identity and valid CIK. Index/ETF/unknown names fail closed for this lane. `--all-mapped` cannot widen automatic scope. CompanyFacts resolves the canonical ticker and verified SEC source authority before HTTP; the static CIK map retains historical compatibility pins, not new-company eligibility.
- Tier precedence: SEC facts score confidence 1.00 (`sec_official` + deterministic) vs FMP 0.94, and `SOURCE_QUALITY_TIER_RANK` puts `sec_official` first — **where the two disagree, the filed SEC number wins by design**.

## Cadence

Weekly Windows scheduled task `\earnings-summary\fetch_sec_xbrl`, Saturday 02:00 (`cron/fetch_sec_xbrl.task.xml` + `cron/run_fetch_sec_xbrl.bat`). Also runnable inside the quarterly refresh DAG via `execution/quarterly_refresh.py --fetch-sec`.

- **Logical Idempotency Key:** SEC accession plus canonical fact locator and period.
- **Content Identity:** SHA-256 of the exact SEC response/document bytes.
- **Observation Version:** accession/filing identity, source filing time, fetched-at
  knowledge time, and Content Identity.
- **Attempt Identity:** unique scheduled or interactive ingestion invocation and receipt.

Rate limit: 0.2s between authorized tickers with the identifying User-Agent in `_HEADERS`. Missing/ambiguous identity, an invalid role/instrument, or denied depth is skipped before network access. At the direct CompanyFacts boundary, HTTP 401/403 becomes a typed auth denial and halts the current job without retry; ordinary non-auth transport failures and per-ticker schema failures remain visible and isolated.

Legacy `#accn=` document rows may have SHA-256(accession text) as their old key.
The evidence backfill reports `legacy_sec_accession_identity` and routes repair to
the existing CompanyFacts snapshot/binding and legacy fact-match revision owners.
Do not change those keys or treat a fresh response as recovered historical bytes.
Source-inventory, document-processing, and semantic receipts remain separate:
a successful CompanyFacts fetch does not prove a complete issuer filing archive.

Register (or re-register after editing the XML) from the MAIN checkout — editing the XML alone does NOT update the live task:

```
schtasks /create /tn "\earnings-summary\fetch_sec_xbrl" /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\fetch_sec_xbrl.task.xml" /f
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

For an active new issuer, establish the canonical ticker/CIK identity and one
verified SEC submissions authority through the issuer registry. CompanyFacts
resolves that authority before HTTP. Unresolved identity, material dissent or a
conflict with a retained historical pin fails closed and requires evidence-based
identity reconciliation. Do not append a guessed CIK merely to allow network work.

For retained static pins or a deployed version that still emits
`sec_cik_map_stale`, re-query `https://www.sec.gov/files/company_tickers.json`
with the configured identifying User-Agent and reverse-lookup the ticker. Keep
pins 10-digit zero-padded and consistent with canonical identity. Repair or update
the supported resolver, then resume the selected ticker. A missing map entry is
not proof that the issuer is unregistered. A genuinely unregistered name receives
the documented `NO_SEC_FILERS` disposition; do not infer it from a stale map.

## Initiating evaluation coverage without FMP

Use the investing skill's [new-company flow](../src/advisor/skills/earnings-summary-investing/references/analysis-paths.md#earnings-and-new-company-evaluation)
for a preliminary report or full memo. An active evaluation equity/ADR has the
same automatic SEC acquisition scope as a portfolio company. Missing owner thesis,
industry template, FMP access or native archive completeness does not block the
independent CompanyFacts lane.

Run the selected-ticker command with `--db <configured-database>` and
`--project-root <configured-state-root>`. The selectors are independent. Verify the
state root used for immutable bytes. `ingest_for_ticker` also accepts `project_root`
explicitly; a database selector alone does not retarget file outputs.
If identity resolution fails or an older selection reports `sec_cik_map_stale`,
repair the identity/resolver above and resume.
Inspect the row receipt and admitted-reader result, not only the exit status.

CompanyFacts ingestion validates the response CIK, retains exact response bytes,
registers the immutable snapshot, binds issuer/document evidence, matches exact
fact locators and records observation resolution under the installed schema.
It can proceed before native filing capture and does not invoke the external
filing-XBRL processor bundle. The bundle approval belongs to the separate
`execution/ingest_sec_filing_xbrl.py` branch.

Run native submissions/package inventory and capture separately for acquisition
coverage. Use native filing XBRL or issuer document intake for facts outside the
CompanyFacts tag/period support. Neither successful CompanyFacts ingestion nor
native capture alone certifies the memo: extraction population, semantic admission,
reader parity, valuation evidence and reconstruction checks remain separate.

## Reviewed financial continuation and registration history

The legacy CompanyFacts parser keeps its existing period support. The typed
`execution/continue_companyfacts_statements.py` continuation can admit retained
annual and YTD source entries without turning YTD into a quarter. Each entry must
prove its exact raw snapshot bytes, JSON locator and hash, matching issuer and
filing accession, source-backed fiscal span, reporting entity, basis, scope and
reviewed native concept role. Definition and binding repositories retain prior
versions. Missing context and conflicting semantics remain rejected.

Exact SEC forms `10-12B` and `10-12B/A` now enter financial-package intake under
`governed-reporting-package-scope@6`. Their original forms remain unchanged. They
use `issuer_financial_statements` and the registered operating issuer's SEC
`regulator_inventory` duty. A publisher duty cannot satisfy SEC archive coverage.
Registration statements remain outside periodic reporting anchors. Processing
uses `complete_reporting_document_processing` version 2 and
`document-processing-terminal-at-k-observed-through-o.v2`; the research snapshot
selection remains `research-snapshot-terminal-at-k-observed-through-o.v1`.

Run these entrypoints through `execution/sqlite_bootstrap.py` with the approved
database and retained-state selectors. Native processor installation has a typed,
read-only preflight. Missing, template, unapproved, drifted or unqualified bundles
remain separate blockers. Capture completeness does not establish extraction or
memo qualification.
