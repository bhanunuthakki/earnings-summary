# Document Table Extraction — Scoping & Design

**Status**: Phase 1 design. Reviewed before any implementation.

**Why this exists**: Roughly 80% of the analytical signal in 10-K / 10-Q / DEF 14A filings lives in *tables* (lease ladders, equity-award schedules, segment capex, customer concentration, goodwill rollforwards). Today the pipeline persists those tables only as flat prose passed to LLM extractors. This loses structure — which means cross-portfolio rollups, year-over-year delta detection, and concept-level reconciliation against `financial_facts` are all impossible.

**Scope of this doc**: Catalog the canonical table kinds we should extract, define how each maps onto the existing entity / concept / extraction graph, propose the schema additions needed, and lay out the extraction pipeline. Phase 2 (MVP implementation) ships two table kinds; the doc must specify enough about the remaining kinds that adding the next one is a 1-day task, not a re-scoping exercise.

---

## 0. Key insight from the 10-K survey

The brief assumed we would parse raw HTML `<table>` elements from SEC EDGAR. **We don't have to.** The FMP `form_10k` JSON files we already cache in `data/historical/fmp/{TICKER}_form_10k_{YEAR}.json` contain the filing's tables **already broken out as separate, key-named sections with structured rows**. Inspecting GOOG FY2024 alone:

- 95 top-level section keys.
- Each section is a list-of-dicts where the first dict is the header row (`['12 Months Ended']` or `['Dec. 31, 2024', 'Dec. 31, 2023']`), the second contains the period columns (`{'items': ['Dec. 31, 2024', 'Dec. 31, 2023', 'Dec. 31, 2022']}`), and subsequent dicts are either *dimension/axis markers* (all values are `\xa0` / unicode nbsp) or *concrete data rows* scoped by the most recent axis marker.
- The section title encodes the unit & currency: `'Revenues - Revenue by Geographic Location (Details) - USD ($) $ in Millions'`.
- This is the XBRL "contexts" model serialized to row-oriented form — and it survives unchanged across FMP refreshes.

This dramatically reframes the work. **For US-GAAP filers, the extractor is mostly a deterministic XBRL-axis parser.** We only need LLM extraction for:

1. **Narrative-only disclosures** (customer concentration, supplier concentration — these are sentences in Item 1 / Concentrations of Credit Risk footnote, never XBRL tables).
2. **Non-US-GAAP filers** (NVO under IFRS, NU under Brazilian banking norms): FMP coverage is sparser (NVO has ~86 keys vs GOOG's 95, with different naming conventions), so a fallback path uses LLM over the cached SEC text.
3. **DEF 14A proxy filings** — not currently in the document pipeline (0 rows; `fetch_latest_def14a_text` exists in `src/filing_text_fetcher.py` but is unused). Per-NEO outstanding equity awards live here, and FMP does not parse proxies.

This insight shapes everything below.

---

## 1. Table taxonomy

Each entry below was confirmed against the actual cached FMP JSONs for GOOG / META / AMZN / NOW / MELI / NVO / NU FY2024 (or latest available). The "FMP key pattern" column shows what to grep for in the JSON top-level keys.

### 1.1 Tables that are 100% deterministic from FMP JSON

| # | `table_kind` | Display label | Source | FMP key pattern | Row schema | Cardinality | Target table |
|---|---|---|---|---|---|---|---|
| 1 | `lease_commitments_ladder` | Future Minimum Lease Payments | 10-K Item 8 Leases footnote | `Leases - Future Minimum Lease P*` | `(ticker, fiscal_year, lease_type ∈ {operating, finance}, ladder_year ∈ {Y+1..Y+5, Thereafter, Total, ImputedInterest, LeaseLiability}, amount, currency, unit)` | One row per (ticker, FY, lease_type, ladder_year) | NEW: `lease_commitments` |
| 2 | `goodwill_rollforward` | Goodwill — Changes in Carrying Amount | 10-K Item 8 Goodwill footnote | `Goodwill - Changes in Carrying *` | `(ticker, fiscal_year, segment_label, segment_entity_id, period_kind ∈ {beginning, additions, fx_other, impairment, divestitures, ending}, amount)` | One row per (ticker, FY, segment, period_kind) | NEW: `goodwill_rollforward` |
| 3 | `share_repurchases` | Share Repurchases by Class | 10-K Stockholders' Equity footnote | `Stockholders' Equity - Share Re*` | `(ticker, fiscal_year, share_class, shares_repurchased, dollar_value, currency)` | One row per (ticker, FY, share_class) | Existing: `capital_actions` (action_kind='buyback_executed') |
| 4 | `geographic_revenue` | Revenue by Geographic Location | 10-K Item 8 Revenues note | `Revenues - Revenue by Geographi*` | `(ticker, fiscal_period, geography_label, geography_entity_id, amount, currency, pct_of_revenue)` | One row per (ticker, period, geography) | Existing: `segment_facts` (metric='revenue', segment_name=geography) + NEW: `geography_revenue_breakdown` |
| 5 | `geographic_long_lived_assets` | Long-Lived Assets by Geographic Area | 10-K Segment Information | `Information about Segments * Long-Lived` | `(ticker, fiscal_period_end, geography_label, geography_entity_id, amount)` | One row per (ticker, period, geography) | NEW: `geographic_assets` |
| 6 | `segment_revenue_operating_income` | Segment Revenue & Operating Income | 10-K Item 8 Segment Information | `Information about Segments * Revenue and Operating` | `(ticker, fiscal_period, segment_label, segment_entity_id, revenue, operating_income, depreciation, capex)` | One row per (ticker, period, segment) | Existing: `segment_facts` (one row per metric) |
| 7 | `debt_maturity_ladder` | Future Principal Payments on Borrowings | 10-K Item 8 Debt footnote | `Debt - Future Principal Payment*` | `(ticker, fiscal_year_end, ladder_year, principal_amount, currency)` | One row per (ticker, FY, ladder_year) | NEW: `debt_maturities` |
| 8 | `stock_based_award_activity` | Stock-Based Award Activity (RSU/Option rollforward) | 10-K Item 8 Compensation Plans | `Compensation Plans - Stock Ba*` (Activities) | `(ticker, fiscal_year, award_type ∈ {RSU, PSU, Option}, period_kind ∈ {unvested_begin, granted, vested, forfeited, unvested_end}, share_count, weighted_avg_grant_date_fv)` | One row per (ticker, FY, award_type, period_kind) | NEW: `stock_award_activity` |
| 9 | `unrecognized_sbc` | Unrecognized Stock-Based Compensation | 10-K Compensation Plans note | `Compensation Plans - Stock Based Compensation*` | `(ticker, fiscal_year, award_type, unrecognized_cost, weighted_avg_remaining_period_years)` | One row per (ticker, FY, award_type) | NEW: `unrecognized_sbc` |

### 1.2 Tables that require LLM extraction (narrative-only or filing-variant)

| # | `table_kind` | Display label | Source | Extraction approach | Row schema | Target table |
|---|---|---|---|---|---|---|
| 10 | `customer_concentration` | Customer Concentration of Revenue | 10-K Item 1, Concentrations of Credit Risk footnote, MD&A | LLM (Haiku) over `data/historical/fmp/{T}_form_10k_{Y}.json` `Summary of Significant Accounting Policies` + Item 1 narrative | `(ticker, fiscal_period, customer_label, customer_entity_id?, pct_of_revenue, revenue_amount?, anonymized: bool, source_excerpt)` | Existing: `customer_concentrations` (already created in 0040, 0 rows) |
| 11 | `supplier_concentration` | Supplier Concentration / Single-Source Dependencies | 10-K Item 1A risk factors, Item 8 footnotes | LLM (Haiku) over Item 1A + Notes narrative | `(ticker, fiscal_year, supplier_label, supplier_entity_id?, dependency_kind ∈ {single_source, primary, named}, pct_of_inputs?, source_excerpt)` | NEW: `supplier_concentrations` |
| 12 | `related_party_transactions` | Related Party Transactions | 10-K Item 8 Related Party footnote, DEF 14A | LLM (Haiku) over `Related Party*` sections + Commitments and Contingencies | `(ticker, fiscal_year, counterparty_label, counterparty_entity_id?, relationship_kind, amount, currency, description_md, source_excerpt)` | Existing: `footnote_facts` (fact_type='related_party_tx') |
| 13 | `contractual_obligations_ladder` | Long-Term Contractual Obligations | 10-K Item 7 MD&A (post-2024 SEC rule keeps this only sometimes; pre-2024 it was a required table) | LLM (Sonnet) — pre-2024 filings had a real table; post-2024 the same data is scattered in footnotes | `(ticker, fiscal_year, obligation_kind ∈ {debt, operating_lease, purchase, other}, ladder_year, amount)` | NEW: `contractual_obligations` |
| 14 | `restructuring_charges_by_program` | Restructuring Charges by Program | 10-K Item 8 Restructuring footnote | LLM (Haiku) — program-level granularity is not in XBRL | `(ticker, fiscal_year, program_label, severance, asset_impairment, lease_termination, other, total)` | NEW: `restructuring_programs` |
| 15 | `pipeline_drug_candidates` | Pharma Pipeline (Phase 1/2/3/Filed) | 10-K Item 1 (pharma-specific — NVO, drug companies) | LLM (Sonnet) over Item 1 + Item 7 | `(ticker, drug_label, indication, phase ∈ {Preclinical, Phase 1..3, Filed, Approved}, expected_milestone_date?, partner?)` | NEW: `drug_pipeline` |

### 1.3 Tables that require DEF 14A (not in pipeline yet — Phase 3 dependency)

| # | `table_kind` | Display label | Source | Target table |
|---|---|---|---|---|
| 16 | `outstanding_equity_awards_per_neo` | Outstanding Equity Awards at Fiscal Year-End | DEF 14A | NEW: `equity_awards` (per-grant detail) — different from #8 which is plan-level rollup |
| 17 | `option_exercises_and_stock_vested` | Option Exercises and Stock Vested | DEF 14A | NEW: `option_exercises_vested` |
| 18 | `pension_and_deferred_comp` | Pension Benefits / Nonqualified Deferred Compensation | DEF 14A | NEW: `pension_deferred_comp` |
| 19 | `grants_of_plan_based_awards` | Grants of Plan-Based Awards | DEF 14A | Existing: `exec_comp_packages.equity_grant_breakdown_json` already covers grant-year detail |

**MVP decision for Phase 2 (defended in §6):** ship **#10 customer_concentration** (LLM/narrative, populates existing 0-row `customer_concentrations` table) and **#1 lease_commitments_ladder** (deterministic FMP parse, demonstrates a new typed table + universal off-BS signal). This intentionally diverges from the brief, which suggested per-NEO outstanding equity awards (#16) — that path requires building the DEF 14A ingest pipeline first (0 docs cached today), which doubles the MVP scope and gives a less broadly useful first table.

---

## 2. Entity & concept linkage rules

### 2.1 Which `entities.kind` rows extracted cells reference

| `table_kind` | Linked entity kinds | Notes |
|---|---|---|
| `customer_concentration` | `customer` (named); also create proposals for `company` when the customer is itself listed | Anonymized labels ("Customer A", "a major hyperscaler") → create `entities(kind='customer', canonical_name='{TICKER} Customer A', meta={'anonymized': true})` scoped by issuer ticker. Never resolve "Customer A" globally — that label is local to one issuer. |
| `supplier_concentration` | `supplier` (named); also `company` when listed | Same anonymization rule as customers. |
| `lease_commitments_ladder` | None (lease lessor is rarely named in the ladder itself) | Pure numeric table. No entity link. |
| `goodwill_rollforward` | `segment` (the row axis) | `segment_entity_id` resolves via existing `segment_aliases` from migration 0042. New segments → mapping proposal. |
| `geographic_revenue`, `geographic_long_lived_assets` | `geography` | Bootstrap a controlled vocabulary of geography entities (US, EMEA, APAC, Other Americas, ROW, China, Japan, etc.) during the migration; resolve by canonical name. Unknown geographies → mapping proposal. |
| `related_party_transactions` | `person`, `company` | Counterparty resolved against the `entity_aliases` registry. |
| `restructuring_charges_by_program` | None (programs are named by management, e.g. "2023 Cost Optimization Plan") | The program_label is the natural key; store as text. |
| `pipeline_drug_candidates` | `drug` (new kind), `company` (partner) | Drug name → `entities(kind='drug', canonical_name=...)`. Partner companies → `company`. |

### 2.2 Which `concepts` numeric values resolve to

For each new table kind, we register one or more entries in the `concepts` table during the migration. Examples:

- `customer_concentration` → `concepts(canonical_name='customer_revenue_pct', unit_kind='pct', generic_definition_md='Pct of total revenue attributed to a single customer')`
- `lease_commitments_ladder` → `concepts(canonical_name='future_lease_payment', unit_kind='currency')`, `concepts(canonical_name='lease_imputed_interest', unit_kind='currency')`, `concepts(canonical_name='lease_liability', unit_kind='currency')`
- `goodwill_rollforward` → `concepts(canonical_name='goodwill_balance', unit_kind='currency')`, `concepts(canonical_name='goodwill_addition', unit_kind='currency')`, etc.

Registering concepts up front (rather than ad-hoc per row) keeps the LLM extractors from inventing concept names — they resolve against the registry and emit `propose_mapping(kind='new_concept_alias')` when they see a new alias.

### 2.3 Hierarchical disclosures

For customers like "Apple, including its subsidiary [...]", the LLM emits one parent customer row and N child-relationship `entity_relationships` edges (`relationship_kind='subsidiary_of'`). The parent is what `customer_concentrations.customer_entity_id` points to.

### 2.4 The mapping-proposal pathway

Every extractor follows the same low-confidence escape valve:

- High confidence (≥ 0.85) entity / concept resolution → write the FK directly.
- Mid confidence (0.50–0.85) → `propose_mapping(kind='new_alias', ...)` with status `pending_review`. The row in the typed table still lands, with the FK left NULL and `source_excerpt` populated so a human can review later.
- Low confidence (< 0.50) → log only; do not write a FK or proposal.

This mirrors the convention already in `src/entity_store.py:propose_mapping`.

---

## 3. Schema proposal

All new tables go in a single migration **0047_document_table_extractions.py** (Phase 2 ships migrations only for the tables the MVP needs; the rest are documented stubs in this doc until their own PRs).

### 3.1 Migration ordering

```
0043_self_update_triggers           (existing)
b79cec08ce5b_create_saydo_historical_metrics  (existing, SayDo)
0044_*                              (reserved — Week 2 work)
0045_*                              (reserved — Week 3 work)
0046_*                              (reserved — Week 4 work)
0047_document_table_extractions     ← this PR (MVP: lease_commitments only;
                                      customer_concentrations already exists in 0040)
0048+                               (subsequent table kinds, one migration per
                                      cluster of related tables)
```

### 3.2 New tables (full DDL for Phase 2 MVP)

```python
# lease_commitments — Future minimum lease payments (operating + finance)
op.create_table(
    "lease_commitments",
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column("ticker", sa.String(length=16), nullable=False),
    sa.Column("fiscal_year", sa.Integer(), nullable=False),
    sa.Column("as_of_date", sa.Date(), nullable=False),  # period_end of the 10-K balance sheet
    sa.Column("filing_doc_id", sa.Integer(), nullable=True),
    sa.Column("lease_type", sa.String(length=16), nullable=False),  # 'operating' | 'finance'
    sa.Column("ladder_year", sa.String(length=16), nullable=False),
    # ladder_year values:
    #   'Y1' through 'Y5' — fiscal years 1–5 after as_of_date
    #   'Thereafter'      — everything past Y5
    #   'TotalPayments'   — sum of Y1..Y5 + Thereafter (validation row)
    #   'ImputedInterest' — Less: imputed interest (negative)
    #   'LeaseLiability'  — Net lease liability balance
    sa.Column("ladder_calendar_year", sa.Integer(), nullable=True),  # absolute year for Y1..Y5 only
    sa.Column("amount", sa.Numeric(24, 6), nullable=False),
    sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
    sa.Column("unit", sa.String(length=16), nullable=False, server_default="millions"),
    sa.Column("source_section_key", sa.String(length=255), nullable=True),
    # The FMP JSON top-level key the row came from, e.g.
    # 'Leases - Future Minimum Lease Payments (Details) - USD ($) $ in Millions'.
    sa.Column("extracted_at", sa.DateTime(), nullable=False),
    sa.UniqueConstraint(
        "ticker", "fiscal_year", "lease_type", "ladder_year",
        name="uq_lease_commitments",
    ),
)
op.create_index("idx_lease_commitments_ticker_year", "lease_commitments", ["ticker", "fiscal_year"])
op.create_index("idx_lease_commitments_type", "lease_commitments", ["lease_type"])
```

`customer_concentrations` already exists in `0040_filing_detail_tables.py` (full DDL in §1 of that file). Phase 2 just populates it.

### 3.3 Stub tables (documented here; created in their own future PRs)

These DDLs are listed so that the next PR can adopt them verbatim. Each is intentionally narrow and analogous to the existing typed-table style (one row per natural-key tuple, no JSON blobs except where the underlying data is genuinely heterogeneous).

```python
# goodwill_rollforward
sa.Column("ticker", String(16), nullable=False)
sa.Column("fiscal_year", Integer, nullable=False)
sa.Column("segment_label", String(128), nullable=False)
sa.Column("segment_entity_id", Integer, nullable=True)  # FK to entities (loose)
sa.Column("period_kind", String(24), nullable=False)
# values: 'beginning' | 'additions' | 'fx_translation' | 'impairment' |
#         'divestitures' | 'segment_reorganization' | 'ending'
sa.Column("amount", Numeric(24, 6), nullable=False)
sa.Column("currency", String(3), nullable=False)
UniqueConstraint("ticker", "fiscal_year", "segment_label", "period_kind")

# debt_maturities
sa.Column("ticker", String(16), nullable=False)
sa.Column("fiscal_year", Integer, nullable=False)
sa.Column("ladder_year", String(16), nullable=False)  # 'Y1'..'Y5','Thereafter','Total'
sa.Column("ladder_calendar_year", Integer, nullable=True)
sa.Column("principal_amount", Numeric(24, 6), nullable=False)
sa.Column("currency", String(3), nullable=False)
UniqueConstraint("ticker", "fiscal_year", "ladder_year")

# stock_award_activity
sa.Column("ticker", String(16), nullable=False)
sa.Column("fiscal_year", Integer, nullable=False)
sa.Column("award_type", String(16), nullable=False)  # 'RSU' | 'PSU' | 'Option'
sa.Column("period_kind", String(24), nullable=False)
# values: 'unvested_begin' | 'granted' | 'vested' | 'forfeited' | 'unvested_end'
sa.Column("share_count", Numeric(20, 0), nullable=True)
sa.Column("weighted_avg_grant_date_fv", Numeric(20, 4), nullable=True)
UniqueConstraint("ticker", "fiscal_year", "award_type", "period_kind")

# unrecognized_sbc
sa.Column("ticker", String(16), nullable=False)
sa.Column("fiscal_year", Integer, nullable=False)
sa.Column("award_type", String(16), nullable=False)
sa.Column("unrecognized_cost", Numeric(24, 6), nullable=False)
sa.Column("currency", String(3), nullable=False)
sa.Column("weighted_avg_remaining_years", Numeric(8, 2), nullable=True)
UniqueConstraint("ticker", "fiscal_year", "award_type")

# geography_revenue_breakdown — used in addition to segment_facts for higher-fidelity geo splits
sa.Column("ticker", String(16), nullable=False)
sa.Column("fiscal_period", String(10), nullable=False)
sa.Column("fiscal_period_type", String(4), nullable=False)
sa.Column("geography_label", String(64), nullable=False)
sa.Column("geography_entity_id", Integer, nullable=True)
sa.Column("revenue", Numeric(24, 6), nullable=False)
sa.Column("pct_of_revenue", Float, nullable=True)
sa.Column("currency", String(3), nullable=False)
UniqueConstraint("ticker", "fiscal_period", "geography_label")

# geographic_assets
sa.Column("ticker", String(16), nullable=False)
sa.Column("as_of_date", Date, nullable=False)
sa.Column("geography_label", String(64), nullable=False)
sa.Column("geography_entity_id", Integer, nullable=True)
sa.Column("amount", Numeric(24, 6), nullable=False)
sa.Column("currency", String(3), nullable=False)
UniqueConstraint("ticker", "as_of_date", "geography_label")

# supplier_concentrations  (mirrors customer_concentrations exactly)
# contractual_obligations
# restructuring_programs
# drug_pipeline
# equity_awards               — per-NEO outstanding awards (DEF 14A)
# option_exercises_vested     — per-NEO exercises (DEF 14A)
# pension_deferred_comp       — per-NEO pension (DEF 14A)
```

### 3.4 Why typed tables, not a polymorphic blob

A single `extracted_table_rows(table_kind, key_json, value)` table would simplify the extractor but kills the analytics layer. Lenses, the dashboard, and synthesis queries all need typed columns (`amount` summable, `pct_of_revenue` orderable, `lease_type` indexable). The `extractions` table already provides the polymorphic provenance blob — every typed row carries a pointer back to its `extractions.id`, which carries the raw payload + source offsets. That's the right split: typed tables for analytics, `extractions` for audit.

---

## 4. Extraction pipeline architecture

### 4.1 Module layout

```
src/document_table_extractor.py        — top-level orchestrator (per-ticker entry point)
src/table_extractors/
    __init__.py
    base.py                            — ExtractorContract dataclass + shared helpers
                                          (FMP JSON loading, period parsing,
                                          axis-marker detection, currency/unit resolution)
    customer_concentration.py          — MVP. LLM (Haiku) over narrative.
    lease_commitments.py               — MVP. Deterministic FMP-section parser.
    goodwill_rollforward.py            — STUB (Phase 3+).
    debt_maturities.py                 — STUB.
    stock_award_activity.py            — STUB.
    geographic_revenue.py              — STUB.
    supplier_concentration.py          — STUB.
    related_party_transactions.py      — STUB.
    pipeline_drug_candidates.py        — STUB.
    outstanding_equity_awards.py       — STUB (blocked on DEF 14A ingest).
execution/extract_document_tables.py   — CLI: --ticker, --all-portfolio, --table-kind
```

### 4.2 The extractor contract

Each `src/table_extractors/<kind>.py` exports exactly one function:

```python
def extract(
    *,
    ticker: str,
    fiscal_year: int | None,
    fmp_payload: dict[str, object] | None,    # the parsed form_10k JSON (None for narrative-only kinds)
    sec_text: FilingTextResult | None,         # the cached SEC text (None when not needed)
    db_path: Path,
    repo_root: Path,
) -> ExtractionOutcome:
    ...
```

`ExtractionOutcome` is a dataclass with `n_rows_proposed`, `n_rows_inserted`, `n_extractions_logged`, `n_mapping_proposals`, `status ∈ {'ok','skipped','no_data','llm_failed','parse_failed'}`, `notes`.

The orchestrator `src/document_table_extractor.py` is responsible for:

1. Resolving the FMP JSON path and the cached SEC text path.
2. Looking up the `documents.id` for the source filing (used as `source_doc_id` in every typed row + extraction).
3. Loading both inputs once (so two extractors can share the same payload without re-reading).
4. Dispatching to the requested extractor(s).
5. Aggregating outcomes for the CLI.

### 4.3 Deterministic XBRL parsing (lease ladder pattern)

The shared `base.py` exposes:

```python
def iter_xbrl_table(
    section: list[dict[str, object]],
) -> Iterator[XbrlRow]:
    """Walk an FMP section, yielding XbrlRow(axis_path, label, values, period_labels).
    The axis_path is the chain of dimension markers (rows with all-\xa0 values)
    that scope this concrete row. The first item in `section` provides the header
    label + unit; the second (if present) provides `items` (period column labels).
    """

def parse_units(section_title: str) -> Units:
    """Returns Units(currency='USD', scale='millions') by regex over the title
    suffix '... USD ($) $ in Millions'."""
```

For lease_commitments specifically, the section is the single dict-list under the key `Leases - Future Minimum Lease Payments (Details) - USD ($) $ in Millions`. Each XbrlRow's `axis_path` is either `["Operating Leases"]` or `["Finance Leases"]`; the label is the ladder year (`'2025'`, `'2026'`, ..., `'Thereafter'`, `'Total future lease payments'`, `'Less imputed interest'`, `'Total lease liability balance'`); the value is the dollar amount in the per-period columns.

The extractor:

1. Iterates rows under each `lease_type` axis.
2. Maps each label to a normalized `ladder_year` enum value (`'Y1' .. 'Y5'`, `'Thereafter'`, `'TotalPayments'`, `'ImputedInterest'`, `'LeaseLiability'`).
3. Computes `ladder_calendar_year` for Y1..Y5 by adding the offset to `as_of_date.year`.
4. Emits one typed row per (lease_type, ladder_year) for the *current* fiscal year column (the one matching `as_of_date`).
5. Writes a single `extractions(extraction_kind='table_row', extractor_id='lease_commitments_v1', payload_json={...})` row per typed row for audit.

No LLM needed. Per-ticker runtime is sub-second.

### 4.4 LLM-driven extraction (customer_concentration pattern)

For narrative-only tables:

1. Load the FMP JSON and concatenate the most-likely-relevant sections into one prompt input. For customer concentration, that's the `Summary of Significant Accounting Policies`, `Revenues`, and `Commitments and Contingencies` keys (or whatever subset exists on this ticker). Cap at 80KB.
2. Classification + extraction is one combined Haiku call per ticker. The prompt asks the model to emit a JSON list of customer-concentration rows, or `[]` when none are disclosed.
3. Parse the response. For each row:
   - Try to resolve `customer_label` to an `entities(kind='customer')` row via `entity_store.resolve_entity(..., kind='customer')`.
   - If unresolved and the label is anonymized (regex: `Customer [A-Z]`, `a major [...]`, `a hyperscaler`), create `entities(kind='customer', canonical_name=f'{ticker} {label}', meta={'anonymized': True})` via `upsert_entity`.
   - If unresolved and the label looks named (proper noun), emit a `propose_mapping(kind='new_entity', confidence=0.6)` and leave `customer_entity_id` NULL.
   - Insert one `customer_concentrations` row + one `extractions` row.

This pattern is identical to `execution/extract_footnotes.py` — see lines 95–203 there for the template.

### 4.5 Cell-level provenance

Every typed row carries:

- `source_doc_id` (FK to `documents`) — the 10-K that yielded the row.
- `source_section_key` or `source_excerpt` (depending on kind) — the FMP JSON key or the verbatim narrative snippet.
- An `extractions` row with `extractor_id`, `extractor_version`, `extracted_at`, `payload_json` containing the structured raw cell data.

The `extractions.links_to_json` column carries `{"table": "lease_commitments", "id": 1234}` so re-extracted rows can be located.

### 4.6 Reconciliation against existing facts

When an extracted table value overlaps with an existing `financial_facts` / `segment_facts` row (e.g., extracted `geographic_revenue` for Google's US segment vs FMP-sourced `segment_facts` for US), the reconciliation rule is:

- **SEC filing (extracted) > FMP > derived.** SEC is the legal source of truth; FMP is a re-keying that occasionally has errors; derived (computed metrics) inherits its base data.
- Conflicts (>1% delta) flagged via `extractions.superseded_by_extraction_id` and a new `extraction_conflicts` row in a future migration. **Phase 2 ships the rule documented but does not implement the conflict detector** — that's a Phase 3 follow-up once we have a few months of overlap data to calibrate the tolerance.
- The synthesis layer reads extracted tables in preference to derived facts when both are available for the same `(ticker, period, concept)`.

### 4.7 Idempotency

Every typed table has a unique-constraint natural key chosen so that re-running the extractor over the same filing is a no-op. For lease_commitments: `(ticker, fiscal_year, lease_type, ladder_year)`. For customer_concentrations: `(ticker, fiscal_period, customer_label)` (already in 0040).

The extractor uses `INSERT ... ON CONFLICT DO NOTHING` (SQLite) — never `INSERT OR REPLACE`, which would orphan the `extractions` row.

A separate `--force` flag deletes all rows for `(ticker, fiscal_year)` before re-running, in case the underlying FMP JSON was refreshed with corrections.

---

## 5. Multi-period rollups

Per the brief — how does each table kind handle "this is a 5-year ladder, not a single value"?

| `table_kind` | Cardinality strategy |
|---|---|
| `lease_commitments_ladder` | **One row per (ticker, FY, lease_type, ladder_year).** The ladder_year is the natural sub-key. Querying "total off-BS lease obligation per ticker" is `SELECT SUM(amount) WHERE ladder_year NOT IN ('TotalPayments','ImputedInterest','LeaseLiability')`. |
| `debt_maturities` | Same pattern — one row per ladder_year. |
| `stock_award_activity` | One row per (ticker, FY, award_type, period_kind). The rollforward semantic (begin + grants − vests − forfeits = end) is encoded as separate rows; a view can reconstruct the math. |
| `goodwill_rollforward` | One row per (ticker, FY, segment, period_kind) — same pattern. |
| `customer_concentration` | One row per (ticker, fiscal_period, customer_label) — already correct in 0040. |
| `equity_awards` (DEF 14A) | One row per (ticker, fiscal_year, executive_entity_id, grant_date, vesting_tranche). Per-tranche granularity is required because vesting timing drives the cumulative-unvested-value lens. |
| `contractual_obligations_ladder` | One row per (ticker, FY, obligation_kind, ladder_year). |

**Strict rule: no JSON blobs for multi-year detail.** The one exception in existing schema is `exec_comp_packages.performance_metrics_json` (heterogeneous list of metrics with weight/threshold/target/actual that doesn't fit a single shape). New tables here all use row-level cardinality.

---

## 6. Cross-table queries enabled

For each table kind, 2–3 analytical questions that today can't be answered:

### customer_concentration
- *Across the portfolio, which holdings have >10% revenue concentration in a single named customer?* (`SELECT ticker, customer_label, pct_of_revenue FROM customer_concentrations WHERE pct_of_revenue >= 0.10 ORDER BY pct_of_revenue DESC`)
- *Which customers appear as a concentration for >1 portfolio company?* (Self-join on `customer_entity_id`; identifies shared dependencies.)
- *Year-over-year trend of customer concentration for a holding — concentrating or diversifying?* (Group by ticker + customer_label, plot pct_of_revenue across fiscal_period.)

### lease_commitments_ladder
- *Total off-balance-sheet lease obligation per ticker, sorted by lease/EBITDA ratio.* (Join `lease_commitments` with `financial_facts` for EBITDA.)
- *Which holdings have meaningfully higher lease obligations than disclosed lease liability — i.e. the imputed-interest unwind exposes a hidden expense?* (`amount(TotalPayments) - amount(LeaseLiability)` per ticker.)
- *Lease-payment ladder front-loading: holdings with >50% of obligations in the next 2 years are higher-risk.* (Y1+Y2 / TotalPayments ratio.)

### goodwill_rollforward (stub)
- *Which holdings impaired goodwill this year, and by how much per segment?*
- *Cumulative goodwill additions over 5 years, segment by segment, as a proxy for M&A-driven growth.*

### stock_award_activity (stub)
- *Net new dilution rate per ticker per year: granted − vested over total shares outstanding.*
- *Forfeiture-to-grant ratios as a turnover proxy across the portfolio.*

### geographic_revenue (stub)
- *Portfolio-wide exposure to China revenue.*
- *Which holdings have the most concentrated geographic revenue (max single-geo pct)?*

### contractual_obligations (stub)
- *Total contractual commitments due in the next 2 years across the portfolio.*

### outstanding_equity_awards (stub, DEF 14A)
- *Cumulative unvested equity value for NEOs across the portfolio at today's price.*
- *Which CEOs have the highest pay-at-risk to share-price changes?*

### supplier_concentration (stub)
- *Which holdings depend on a single named supplier — and is that supplier itself a portfolio holding?*

---

## 7. Integration with the existing analytical layer

### 7.1 Synthesis lenses

Phase 2 ships one new lens that exercises the MVP extracted data: **`customer_concentration_risk`** (scope='portfolio', model='claude-sonnet-4-6'). Context builder reads `customer_concentrations` for all portfolio tickers; prompt asks for:

1. Rank-ordered list of single-name customer exposures across the book.
2. Customers that appear as a concentration for >1 holding (correlated risk).
3. Year-over-year direction (concentrating vs diversifying) per holding.
4. Anonymized-customer disclosures the analyst should be tracking through subsequent filings.

Output is markdown, consumed by the dashboard's portfolio synthesis tab. Stored in `llm_artifacts` with `purpose='lens:customer_concentration_risk'`.

The lens registers in `LENSES` dict at the bottom of `src/synthesis_lenses.py`, follows the existing `Lens(...)` dataclass + `_ctx_<name>` + `_PROMPT_<NAME>` pattern (see `_PROMPT_FOOTNOTE_ANOMALY` at line 983 for the closest analogue).

Future lenses (one per cluster of related tables):

- `lease_obligation_risk` (consumes `lease_commitments`)
- `goodwill_quality_audit` (consumes `goodwill_rollforward`)
- `dilution_pace` (consumes `stock_award_activity` — net dilution as a multi-year trend)
- `geographic_concentration` (consumes `geography_revenue_breakdown`)
- `executive_pay_alignment_deep` (consumes `equity_awards` from DEF 14A — supersedes the current `exec_comp_packages`-only lens)

### 7.2 Workspace renderer integration

The workspace renderer at `src/report/workspace_html.py` currently has 8 tabs. The MVP adds **one new tab** — *"Filing Deep Tables"* — that renders four sub-panels:

1. *Customer Concentration* — sortable HTML table with columns (period, customer, pct, anonymized?, source). Highlighted rows for pct ≥ 0.10.
2. *Lease Ladder* — two side-by-side mini-tables (operating + finance) with ladder Y1..Thereafter and totals. Cross-ticker comparison link in the header.
3. *(placeholder)* "Goodwill, debt maturity, equity activity panels will land here as their extractors ship — see directives/document_tables_design.md §6."
4. The lens artifact (`lens:customer_concentration_risk`) rendered as the tab footer's portfolio-wide commentary.

Each panel reads via a new module `src/report/sections/document_tables.py`. The panel is gated on `lease_commitments` / `customer_concentrations` row count for the ticker; tab is hidden when both are empty.

### 7.3 Dashboard integration

The cross-portfolio dashboard at `src/report/dashboard.py` (assume the convention; verify path during implementation) gets one new section: *Portfolio-wide customer-concentration heat map*. SQL:

```sql
SELECT ticker, customer_label, pct_of_revenue, fiscal_period
FROM customer_concentrations
WHERE pct_of_revenue >= 0.05
ORDER BY pct_of_revenue DESC, ticker;
```

Same gate as the workspace tab — hidden when no rows exist.

### 7.4 Where the extractor fits in the DAG

Per `directives/data_pipeline_dag.md`, the 8 stages are INGEST → TRANSCRIBE → PARSE → VALIDATE → PERSIST → COMPUTE → SYNTHESIZE → PUBLISH. Document table extraction is a **new PARSE-adjacent stage** that runs after PERSIST (because typed rows reference `documents.id`) and before SYNTHESIZE (because lenses read from the typed tables). It doesn't fit cleanly into any existing stage — propose adding a 4.5th sub-stage `EXTRACT_TABLES` between PERSIST and COMPUTE in the next DAG revision. For Phase 2 MVP, the extractor is invoked as a standalone CLI (`execution/extract_document_tables.py`) rather than wired into the orchestrator; that wiring is a Phase 3 follow-up.

---

## 8. What ships in Phase 2 (MVP scope-lock)

- **Migration 0047**: creates `lease_commitments` table. (`customer_concentrations` is pre-existing.)
- **`src/document_table_extractor.py`**: orchestrator with the dispatch pattern in §4.2.
- **`src/table_extractors/`**: `base.py`, `customer_concentration.py`, `lease_commitments.py`, plus stub modules for the other 11+ kinds (each is a 20-line `NotImplementedError` stub that documents its `ExtractorContract` and `source_section_key` patterns).
- **`execution/extract_document_tables.py`**: CLI with `--ticker`, `--all-portfolio`, `--table-kind` flags.
- **`src/synthesis_lenses.py`**: new `customer_concentration_risk` lens (portfolio scope) registered.
- **Tests**: `tests/test_document_table_extractor.py` (15+ tests covering orchestrator dispatch, idempotency, the lease deterministic parse with multiple ticker fixtures, the customer-concentration LLM happy path + empty-disclosure path + anonymized customer path). Each table-kind module gets its own golden test file (`tests/test_lease_commitments_extractor.py`, `tests/test_customer_concentration_extractor.py`).

What does NOT ship in MVP (explicitly):

- DEF 14A ingest (and all extractors that depend on it — #16, #17, #18 in §1.3).
- The `EXTRACT_TABLES` DAG sub-stage wiring; the CLI suffices.
- The extraction-conflict detector in §4.6; rule is documented, implementation is Phase 3.
- The dashboard heat-map section in §7.3; the workspace panel ships, the dashboard wait for the second-extractor PR.
- Pyright debt cleanup beyond what touches new code; baseline tolerance unchanged.

---

## 9. Open questions (resolve during Phase 2)

1. **Anonymized customer scoping.** Today's draft says "scope anonymized labels by issuer ticker." Confirm during implementation that no upstream code assumes `entities.canonical_name` is globally meaningful. (`entity_store.upsert_entity` enforces uniqueness on `(kind, canonical_name)`, so this works as long as we include the ticker prefix in the canonical name. Reads via `resolve_entity` need to pass the ticker context — investigate if any consumer doesn't.)

2. **FMP refresh idempotency.** If FMP corrects a historical 10-K JSON (rare but happens — e.g., restatements), the extractor must detect a content change and re-extract. Proposal: hash the input section (`source_section_key` + sha256 of its serialized rows) and store the hash; skip re-extraction when the hash matches; re-extract + supersede previous rows when it diverges. Decision: implement the hash check; defer supersession to Phase 3 (for MVP, `--force` is the manual escape).

3. **Non-USD currency in lease ladders.** NVO reports in DKK, MELI in USD-equivalent of local. The section title encodes the currency (`'USD ($)'` or `'DKK (kr)'`). The parser must respect the per-section currency; the existing `fx_rates` table from migration 0042 handles conversion at the lens-rendering layer.

4. **Period type for the customer_concentrations table.** The existing schema uses `fiscal_period_type ∈ {Q1..Q4, FY}` and `fiscal_period` as a YYYY or YYYY-MM-DD string. For 10-K-sourced concentrations, this is always `('YYYY', 'FY')`. For 10-Q-sourced (future), it would be `('YYYY-MM-DD', 'Q1'..'Q4')`. The MVP extractor only handles the FY case; 10-Q extraction is a one-line generalization later.

---

## 10. Phase 1 → Phase 2 → Phase 3 staging

- **Phase 1** (this doc): scoping. Ship via its own commit on this branch.
- **Phase 2** (MVP): customer concentration + lease commitments. Ship via the same PR as Phase 1.
- **Phase 3** (follow-up PR, separate cadence): goodwill rollforward + debt maturities + stock award activity + geographic revenue. These all use the same deterministic XBRL parser as lease_commitments, so the marginal cost per table kind is small once the `base.py` parser is proven.
- **Phase 4** (DEF 14A pipeline): build `execution/fetch_def14a.py` + ingest + the three DEF-14A-only table kinds. Dependency: requires designing a per-ticker DEF 14A document-tracking model (proxies are annual, not quarterly, and the fiscal-year alignment is different from 10-Ks).
- **Phase 5** (IFRS / foreign filers): adapt the deterministic parser to NVO's IFRS section naming (Equity statement, Results for the year - Segment, Other disclosures - Share-based) and NU's banking-specific sections (Loans to customers (Details N), Customer crypto safeguarding).

The Phase 3+ work is intentionally not blocked on Phase 2 — the contract in §4.2 is stable enough that other contributors (or other parallel sessions) can add table-kind modules without renegotiating the architecture.

---

## Appendix A — Anatomy of an FMP XBRL section (worked example)

Input section in `data/historical/fmp/GOOG_form_10k_2024.json`:

```json
"Leases - Future Minimum Lease P": [
  {"Leases - Future Minimum Lease Payments (Details) - USD ($) $ in Millions":
     ["Dec. 31, 2024", "Dec. 31, 2023"]},
  {"Operating Leases":           [" ", " "]},
  {"2025":                       [3162, " "]},
  {"2026":                       [2824, " "]},
  {"2027":                       [2311, " "]},
  {"2028":                       [1838, " "]},
  {"2029":                       [1448, " "]},
  {"Thereafter":                 [5455, " "]},
  {"Total future lease payments":[17038, " "]},
  {"Less imputed interest":      [-2460, " "]},
  {"Total lease liability balance":[14578, 15251]},
  {"Finance Leases":             [" ", " "]},
  {"2025":                       [257, " "]},
  ... (mirrors operating leases) ...
  {"Total lease liability balance":[1677, 1666]}
]
```

Parsed into typed `lease_commitments` rows (Dec. 31, 2024 column only):

| ticker | fiscal_year | lease_type | ladder_year | ladder_calendar_year | amount | currency | unit |
|---|---|---|---|---|---|---|---|
| GOOG | 2024 | operating | Y1 | 2025 | 3162 | USD | millions |
| GOOG | 2024 | operating | Y2 | 2026 | 2824 | USD | millions |
| GOOG | 2024 | operating | Y3 | 2027 | 2311 | USD | millions |
| GOOG | 2024 | operating | Y4 | 2028 | 1838 | USD | millions |
| GOOG | 2024 | operating | Y5 | 2029 | 1448 | USD | millions |
| GOOG | 2024 | operating | Thereafter | NULL | 5455 | USD | millions |
| GOOG | 2024 | operating | TotalPayments | NULL | 17038 | USD | millions |
| GOOG | 2024 | operating | ImputedInterest | NULL | -2460 | USD | millions |
| GOOG | 2024 | operating | LeaseLiability | NULL | 14578 | USD | millions |
| GOOG | 2024 | finance | Y1 | 2025 | 257 | USD | millions |
| ... | ... | ... | ... | ... | ... | ... | ... |

Validation invariants the extractor checks after parse:

- `SUM(Y1..Y5 + Thereafter) == TotalPayments` (within rounding tolerance).
- `TotalPayments + ImputedInterest == LeaseLiability` (imputed interest is negative).

Violations log a `validation_issues` row but do not block the insert — the disclosed numbers are what they are; analyst follow-up may be warranted but the typed rows still ship.

---

*End of design doc. Ready for review before implementation begins.*
