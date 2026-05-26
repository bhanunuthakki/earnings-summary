"""Documented stubs for the remaining table-kind extractors.

Each stub's docstring is the spec the next PR adopts. The extractor
contract (see base.ExtractorContract) is identical across stubs; only
the source-section discovery and row-schema vary.

The orchestrator imports these names but never invokes their `extract`
functions until they're filled in (the registry only exposes implemented
extractors). They are listed here purely as documentation in a single
file rather than 11 near-empty files, to keep src/table_extractors/
tidy.
"""

from __future__ import annotations

from pathlib import Path

from table_extractors.base import ExtractionOutcome


def _not_implemented(table_kind: str, ticker: str, fiscal_year: int | None) -> ExtractionOutcome:
    return ExtractionOutcome(
        table_kind=table_kind,
        ticker=ticker,
        fiscal_year=fiscal_year,
        status="skipped",
        notes=f"extractor for table_kind={table_kind!r} not implemented yet",
    )


def extract_goodwill_rollforward(
    *,
    ticker: str,
    fiscal_year: int | None,
    fmp_payload: dict[str, object] | None = None,
    sec_text: object | None = None,
    db_path: Path | None = None,
    repo_root: Path | None = None,
    filing_doc_id: int | None = None,
) -> ExtractionOutcome:
    """STUB: deterministic FMP-section parser.

    Source section: top-level key matching r'Goodwill.*Changes in Carrying'.
    Row schema per row: (ticker, fiscal_year, segment_label, segment_entity_id,
    period_kind ∈ {'beginning', 'additions', 'fx_translation', 'impairment',
    'divestitures', 'segment_reorganization', 'ending'}, amount).
    Axis is the segment label; period_kind is the row label normalized.
    Validation: SUM(beginning + additions ± fx ± impairment ± divestitures) ≈ ending.
    """
    del fmp_payload, sec_text, db_path, repo_root, filing_doc_id
    return _not_implemented("goodwill_rollforward", ticker, fiscal_year)


def extract_debt_maturities(
    *,
    ticker: str,
    fiscal_year: int | None,
    fmp_payload: dict[str, object] | None = None,
    sec_text: object | None = None,
    db_path: Path | None = None,
    repo_root: Path | None = None,
    filing_doc_id: int | None = None,
) -> ExtractionOutcome:
    """STUB: deterministic FMP-section parser, parallels lease_commitments.

    Source section: r'Debt.*Future Principal Payment'.
    Row schema: (ticker, fiscal_year, ladder_year ∈ {Y1..Y5, Thereafter, Total},
    ladder_calendar_year, principal_amount, currency).
    """
    del fmp_payload, sec_text, db_path, repo_root, filing_doc_id
    return _not_implemented("debt_maturities", ticker, fiscal_year)


def extract_stock_award_activity(
    *,
    ticker: str,
    fiscal_year: int | None,
    fmp_payload: dict[str, object] | None = None,
    sec_text: object | None = None,
    db_path: Path | None = None,
    repo_root: Path | None = None,
    filing_doc_id: int | None = None,
) -> ExtractionOutcome:
    """STUB: deterministic FMP-section parser.

    Source section: r'Compensation Plans.*Stock.*Award Activit' (Activities, not Activity).
    Row schema: (ticker, fiscal_year, award_type ∈ {RSU, PSU, Option},
    period_kind ∈ {unvested_begin, granted, vested, forfeited, unvested_end},
    share_count, weighted_avg_grant_date_fv).
    Axis is the award_type.
    """
    del fmp_payload, sec_text, db_path, repo_root, filing_doc_id
    return _not_implemented("stock_award_activity", ticker, fiscal_year)


def extract_geographic_revenue(
    *,
    ticker: str,
    fiscal_year: int | None,
    fmp_payload: dict[str, object] | None = None,
    sec_text: object | None = None,
    db_path: Path | None = None,
    repo_root: Path | None = None,
    filing_doc_id: int | None = None,
) -> ExtractionOutcome:
    """STUB: deterministic FMP-section parser.

    Source section: r'Revenue.*by Geograph'.
    Row schema: (ticker, fiscal_period, geography_label, geography_entity_id,
    revenue, pct_of_revenue, currency).
    Axis is the geography label (US, EMEA, APAC, Other Americas, etc.).
    Entity linkage: geography_entity_id resolves against a controlled
    vocabulary of geography entities (seed during the migration).
    """
    del fmp_payload, sec_text, db_path, repo_root, filing_doc_id
    return _not_implemented("geographic_revenue", ticker, fiscal_year)


def extract_supplier_concentration(
    *,
    ticker: str,
    fiscal_year: int | None,
    fmp_payload: dict[str, object] | None = None,
    sec_text: object | None = None,
    db_path: Path | None = None,
    repo_root: Path | None = None,
    filing_doc_id: int | None = None,
) -> ExtractionOutcome:
    """STUB: LLM extractor (mirrors customer_concentration).

    Source: Item 1A risk factors + Item 8 supplier/commitments narrative.
    Row schema: (ticker, fiscal_year, supplier_label, supplier_entity_id,
    dependency_kind ∈ {single_source, primary, named}, pct_of_inputs?,
    source_excerpt).
    Anonymized labels ('a single supplier', 'a foundry') get
    ticker-scoped entity rows analogous to anonymized customers.
    """
    del fmp_payload, sec_text, db_path, repo_root, filing_doc_id
    return _not_implemented("supplier_concentration", ticker, fiscal_year)


def extract_related_party_transactions(
    *,
    ticker: str,
    fiscal_year: int | None,
    fmp_payload: dict[str, object] | None = None,
    sec_text: object | None = None,
    db_path: Path | None = None,
    repo_root: Path | None = None,
    filing_doc_id: int | None = None,
) -> ExtractionOutcome:
    """STUB: LLM extractor; persists into footnote_facts (existing table).

    Source: r'Related Party.*' + r'Commitments and Contingencies'.
    fact_type='related_party_tx'. counterparty_entity_id resolves against
    person + company entities.
    """
    del fmp_payload, sec_text, db_path, repo_root, filing_doc_id
    return _not_implemented("related_party_transactions", ticker, fiscal_year)


def extract_contractual_obligations(
    *,
    ticker: str,
    fiscal_year: int | None,
    fmp_payload: dict[str, object] | None = None,
    sec_text: object | None = None,
    db_path: Path | None = None,
    repo_root: Path | None = None,
    filing_doc_id: int | None = None,
) -> ExtractionOutcome:
    """STUB: hybrid. Pre-2024 filings had a structured table in Item 7;
    post-2024 the same data is scattered across footnotes (SEC rule change
    eliminated the required tabular disclosure).

    Source: Item 7 narrative + Notes Details sections for Debt, Leases,
    Purchase Commitments.
    Row schema: (ticker, fiscal_year, obligation_kind ∈ {debt, operating_lease,
    purchase, other}, ladder_year, amount).
    """
    del fmp_payload, sec_text, db_path, repo_root, filing_doc_id
    return _not_implemented("contractual_obligations", ticker, fiscal_year)


def extract_restructuring_programs(
    *,
    ticker: str,
    fiscal_year: int | None,
    fmp_payload: dict[str, object] | None = None,
    sec_text: object | None = None,
    db_path: Path | None = None,
    repo_root: Path | None = None,
    filing_doc_id: int | None = None,
) -> ExtractionOutcome:
    """STUB: LLM extractor (program-level granularity is not in XBRL).

    Source: r'Restructuring' sections + Item 7 narrative.
    Row schema: (ticker, fiscal_year, program_label, severance,
    asset_impairment, lease_termination, other, total).
    """
    del fmp_payload, sec_text, db_path, repo_root, filing_doc_id
    return _not_implemented("restructuring_programs", ticker, fiscal_year)


def extract_pipeline_drug_candidates(
    *,
    ticker: str,
    fiscal_year: int | None,
    fmp_payload: dict[str, object] | None = None,
    sec_text: object | None = None,
    db_path: Path | None = None,
    repo_root: Path | None = None,
    filing_doc_id: int | None = None,
) -> ExtractionOutcome:
    """STUB: LLM extractor (pharma-specific, e.g. NVO).

    Source: Item 1 (Business) + Item 7 R&D narrative.
    Row schema: (ticker, drug_label, indication,
    phase ∈ {Preclinical, Phase 1..3, Filed, Approved},
    expected_milestone_date, partner).
    Drug entities are kind='drug' (new); partners are kind='company'.
    """
    del fmp_payload, sec_text, db_path, repo_root, filing_doc_id
    return _not_implemented("pipeline_drug_candidates", ticker, fiscal_year)


def extract_outstanding_equity_awards(
    *,
    ticker: str,
    fiscal_year: int | None,
    fmp_payload: dict[str, object] | None = None,
    sec_text: object | None = None,
    db_path: Path | None = None,
    repo_root: Path | None = None,
    filing_doc_id: int | None = None,
) -> ExtractionOutcome:
    """STUB: LLM extractor — BLOCKED on DEF 14A ingest.

    Source: DEF 14A 'Outstanding Equity Awards at Fiscal Year-End' table.
    Today the project has 0 DEF 14A documents in the documents table; the
    fetcher exists (src/filing_text_fetcher.fetch_latest_def14a_text) but
    no execution/fetch_def14a.py wires it into the ingest pipeline.
    This extractor blocks on that work.

    Row schema: (ticker, fiscal_year, executive_entity_id, grant_date,
    vesting_tranche, unvested_share_count, exercise_price?, expiration_date?).
    Per-grant granularity is required for the cumulative-unvested-value lens.
    """
    del fmp_payload, sec_text, db_path, repo_root, filing_doc_id
    return _not_implemented("outstanding_equity_awards", ticker, fiscal_year)
