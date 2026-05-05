# Per-ticker segment extraction notes

Inventory of how each ticker reports segment-level data in its 10-K JSON,
so future per-ticker passes can pick up where previous work left off.

**Currently extracted** by `compute/segment_oi_10k.py` (the heuristic walker):

| Ticker | Segments captured | Method |
|---|---|---|
| GOOG | Google Services / Cloud / Other Bets / Reconciling items | rev - costs OR direct OI |
| AMZN | AWS / North America / International | "Net sales" + "Operating expenses" + "Operating Income (Loss)" |
| META | Family of Apps / Reality Labs | "Income (loss) from operations" direct |
| FCX | 22 segments incl. Indonesia ops, Cerro Verde, Morenci | various |
| AMAT | 7 segments incl. Semiconductor Systems / AGS / Display | various |
| ABNB | Single Reportable Segment (correctly) | direct OI |
| VALE | 2 segments | partial |

## Tickers that need custom per-ticker extractors

### VEEV — gross-profit-only at segment level

**Section**: `Segment Reporting - Schedule of Segment Reporting Information, by Segment (Details)`.

VEEV reports two segments (Subscription services, Professional services) but only
**revenue + cost of revenues per segment**. R&D / S&M / G&A are reported only at the
"Operating Segments" rollup, not per segment. So per-segment "OI" doesn't exist in the
10-K — only segment **gross profit** does.

**Approach**: write a VEEV-specific extractor that emits:
- `metric='revenue'` per segment
- `metric='cost_of_revenue'` per segment
- `metric='gross_profit'` per segment (computed)

Don't emit `metric='operating_income'` for VEEV — there's no truthful value to use.

### MELI — 20-F, segment-by-country

**Section**: typically `Segment Information - Reconciliation of Segment Income`.

MELI reports two segments (Commerce, Fintech) and breaks them out by country
(Brazil, Argentina, Mexico, Other). Currency is USD (MELI reports in USD).

**Approach**: same heuristic as GOOG should mostly work, but the 2025 10-K has
revenue + opex per country/segment combination. Two passes needed: one per
geographic-segment, one per business-segment (Commerce vs Fintech).

### ASML — 20-F, EUR with string-encoded values

**Section**: `Segment disclosure - Net Sales for New and Used Systems`.

Values come as strings with the euro symbol: `'€ 32,667.3'`. Currency is EUR; scale
is millions per the section title.

**Approach**: write a value-parser that strips `€ ` prefix and `,` thousands
separators before converting to float. Set `currency=EUR`, `unit=ACTUAL` (after
×1e6 scaling). Segments are by system type (New / Used / Service / Field options).

### NU — 20-F, IFRS, geographic only

**Section**: `Segment information (Details)`.

NU is an IFRS-reporting Brazilian neobank (20-F). Their segment information has
**only revenue + non-current assets per geography**. No segment OI, no segment
costs.

**No 10-K segment OI to extract.** NU's actual KPIs (NPL buckets, NIM, NIMAL,
ARPAC) live in the quarterly Investor Update PDF on `investors.nu`. KPI extraction
must come from the IR doc fetcher (Phase 4), not the 10-K.

### NVO — 20-F, drug-level revenue

NVO reports drug-level revenue (Wegovy, Ozempic, Rybelsus, insulins, etc.) instead
of business-segment OI. Different shape entirely.

**Approach**: new extractor `compute/drug_revenue.py` that walks the 20-F's
"Therapeutic area" or "Product family" tables. Emits to segment_facts with
`metric='product_revenue'` and `segment_name='Wegovy'` etc.

### LLY — 10-K, drug-level revenue (similar to NVO)

Same pattern as NVO: revenue by drug (Mounjaro, Zepbound, Trulicity, Verzenio,
Taltz, etc.). Use the same drug_revenue extractor.

### JPM — bank, segment OI exists but unusual shape

JPM reports 4 segments (CCB, CIB, AWM, Corporate). The 10-K has segment net income
+ segment revenue + segment expenses. **Bank-specific lines** (net interest income,
non-interest revenue) replace the standard "revenue / costs / OI".

**Approach**: write `compute/segment_oi_bank.py` that handles bank-style segment
reporting (NII + non-interest rev = total rev; provision for credit losses is a
distinct line; segment net income is reported directly).

### SOFI — bank-fintech, similar to JPM

Three segments: Lending, Technology Platform, Financial Services. Bank-style
reporting. Use the bank extractor with SOFI-specific labels.

### TOL — homebuilder, region segments

Quarterly breakout (Feb / Apr / Jul / Oct ends per fiscal year). Segments are
North / Mid-Atlantic / South / Mountain / Pacific. The 10-K has "Income (loss)
before income taxes" per segment (not strictly operating income, but close).

**Approach**: extend the heuristic to recognize "Income (loss) before income taxes"
as a near-OI proxy. Or write a TOL-specific module; their fiscal-year-end (Oct 31)
also needs special handling.

### BHP / RIO / VALE — quarterly production reports, not 10-K

These miners' segment data lives in **quarterly production reports** on their IR
sites — separate documents from the 20-F annual. Production volumes (Mt of iron
ore, kt of copper, kbbl/d of oil), AISC, capex by project.

**Approach**: dedicated PDF parser fed by IR doc fetcher (Phase 4). Not in the
FMP 10-K JSON.

### BN / CNQ / FNV — Canadian 40-F

**BN**: Brookfield's segment reporting is by asset class (Asset Management, Wealth
Solutions, Real Estate, Renewables, Infrastructure, Private Equity). Their
quarterly **Supplemental Information** PDF has the detailed breakdown — much
richer than the 40-F.

**CNQ**: Production by basin (Oil Sands, Conventional, Offshore). Realized prices
(AECO, WCS). Different shape.

**FNV**: Royalty assets by mine + commodity. Geographic distribution.

All three: defer to per-ticker custom extractors with IR-doc fallback.

### NOW / RBRK / LMND — single operating segment (no breakout)

These companies are single-reportable-segment per ASC 280. Product-tier breakouts
(ITSM/CRM for NOW, subscription products for RBRK, insurance products for LMND)
are in IR investor presentations and earnings transcripts only, not in 10-K
financial statements.

**No 10-K segment OI to extract.** Route to IR doc fetcher.

## Tickers blocked on the IR doc fetcher (Phase 4)

These need IR-PDF ingestion before any further KPI extraction:

- **NU**: investor update PDF (NPL buckets)
- **HDB**: BSE/NSE filings (Indian regulatory portal, not SEC)
- **NOW / RBRK / LMND**: investor presentations (product-tier rev)
- **BHP / RIO / VALE**: quarterly production reports
- **BN**: quarterly supplemental + letter to shareholders
- **NVO**: annual report PDF (drug-level breakdown beyond what 20-F has)

## Recommended next-pass order

1. **MELI** (similar to GOOG, expected easy) — Commerce/Fintech segments
2. **VEEV** (custom — gross-profit-only) — Subscription/Professional
3. **ASML** (currency parsing) — system-type segments
4. **JPM + SOFI** (bank shape) — share extractor
5. **LLY + NVO** (drug-level shape) — share extractor
6. **TOL** (homebuilder) — single-purpose extractor
7. **BN / CNQ / FNV** (Canadian) — three separate extractors

Tickers whose **only** path is the IR doc fetcher (NU/HDB/RBRK/LMND/BHP/RIO/VALE
investor presentations) should wait for Phase 4 rather than getting stub
extractors.
