"""Document = the provenance anchor for every fact in the system.

Per directives/data_provenance.md, every row in financial_facts, segment_facts,
kpi_facts, transcript_segments, and dcf_runs MUST reference a Document via
source_doc_id. Documents are written once per ingestion (sha256-keyed for
idempotence) and never mutated; replacements get a new row.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field


class SourceType(StrEnum):
    """Where the document originated.

    `llm_extracted` is for any structured payload an LLM produced from a primary
    document — derived, not primary. Starts in validation quarantine.
    """

    FMP = "fmp"
    SEC_XBRL = "sec_xbrl"
    IR_DOC = "ir_doc"
    TRANSCRIPT_AUDIO = "transcript_audio"
    MANUAL_CSV = "manual_csv"
    MANUAL_ENTRY = "manual_entry"
    LLM_EXTRACTED = "llm_extracted"


class SourceQualityTier(StrEnum):
    """Provenance tier — the read-side loader prefers higher tiers.

    Mirrors migration 0054's _BACKFILL_TIER_BY_SOURCE_TYPE mapping and the
    loaders' SOURCE_QUALITY_TIER_RANK. Keep both in sync when adding a tier.
    """

    SEC_OFFICIAL = "sec_official"
    FMP_NORMALIZED = "fmp_normalized"
    LLM_EXTRACTED = "llm_extracted"
    YFINANCE_FALLBACK = "yfinance_fallback"


_SOURCE_TYPE_TO_TIER: dict[SourceType, SourceQualityTier] = {
    SourceType.SEC_XBRL: SourceQualityTier.SEC_OFFICIAL,
    SourceType.FMP: SourceQualityTier.FMP_NORMALIZED,
    SourceType.IR_DOC: SourceQualityTier.FMP_NORMALIZED,
    SourceType.TRANSCRIPT_AUDIO: SourceQualityTier.FMP_NORMALIZED,
    SourceType.MANUAL_CSV: SourceQualityTier.FMP_NORMALIZED,
    SourceType.MANUAL_ENTRY: SourceQualityTier.FMP_NORMALIZED,
    SourceType.LLM_EXTRACTED: SourceQualityTier.LLM_EXTRACTED,
}


def tier_for_source_type(source_type: SourceType) -> SourceQualityTier:
    """Return the canonical SourceQualityTier for a SourceType.

    The mapping is closed over the SourceType enum — adding a new source
    type without updating _SOURCE_TYPE_TO_TIER raises KeyError loudly, which
    is the desired behavior: every source must declare its tier, no silent
    fallback to the default that lets a malformed insert pass through.
    """
    return _SOURCE_TYPE_TO_TIER[source_type]


class DocType(StrEnum):
    """Concrete artifact kinds. Add values when introducing a new endpoint or doc class."""

    FMP_INCOME_STATEMENT = "fmp_income_statement"
    FMP_BALANCE_SHEET = "fmp_balance_sheet"
    FMP_CASHFLOW = "fmp_cashflow"
    FMP_AS_REPORTED_INCOME = "fmp_as_reported_income"
    FMP_AS_REPORTED_BALANCE = "fmp_as_reported_balance"
    FMP_AS_REPORTED_CASHFLOW = "fmp_as_reported_cashflow"
    FMP_AS_REPORTED_FINANCIAL = "fmp_as_reported_financial"
    FMP_SEGMENT_PRODUCT = "fmp_segment_product"
    FMP_SEGMENT_GEOGRAPHIC = "fmp_segment_geographic"
    FMP_KEY_METRICS = "fmp_key_metrics"
    FMP_FINANCIAL_RATIOS = "fmp_financial_ratios"
    FMP_FINANCIAL_GROWTH = "fmp_financial_growth"
    FMP_OWNER_EARNINGS = "fmp_owner_earnings"
    FMP_ENTERPRISE_VALUES = "fmp_enterprise_values"
    FMP_DCF = "fmp_dcf"
    FMP_DCF_LEVERED = "fmp_dcf_levered"
    FMP_PROFILE = "fmp_profile"
    FMP_EARNINGS_CALENDAR = "fmp_earnings_calendar"
    FMP_HISTORICAL_PRICE = "fmp_historical_price"
    FMP_HISTORICAL_MARKET_CAP = "fmp_historical_market_cap"
    FMP_ANALYST_ESTIMATES = "fmp_analyst_estimates"
    FMP_PRICE_TARGET_CONSENSUS = "fmp_price_target_consensus"
    FMP_GRADES = "fmp_grades"
    FMP_PEERS = "fmp_peers"
    FMP_EXECUTIVES = "fmp_executives"
    FMP_HISTORICAL_EMPLOYEES = "fmp_historical_employees"
    FMP_FINANCIAL_REPORTS_DATES = "fmp_financial_reports_dates"
    FMP_10K_JSON = "fmp_10k_json"
    FMP_10Q_JSON = "fmp_10q_json"
    FMP_OTHER = "fmp_other"
    ETF_INFORMATION = "etf_information"
    ETF_HOLDINGS = "etf_holdings"
    ETF_SECTOR_WEIGHTING = "etf_sector_weighting"
    ETF_COUNTRY_WEIGHTING = "etf_country_weighting"
    SEC_10K = "sec_10k"
    SEC_10Q = "sec_10q"
    SEC_20F = "sec_20f"
    SEC_40F = "sec_40f"
    SEC_8K = "sec_8k"
    SEC_6K = "sec_6k"
    IR_PRESS_RELEASE = "ir_press_release"
    IR_PRESENTATION = "ir_presentation"
    IR_SUPPLEMENT = "ir_supplement"
    IR_INVESTOR_UPDATE = "ir_investor_update"
    IR_TRANSCRIPT = "ir_transcript"
    # Non-quarterly IR materials: investor days, AGMs, capital markets days, conference
    # decks, ad-hoc strategic announcements. Period_end on these rows is the event date,
    # not a fiscal-quarter end.
    IR_EVENT = "ir_event"
    EARNINGS_CALL_AUDIO = "earnings_call_audio"
    EARNINGS_CALL_TRANSCRIPT = "earnings_call_transcript"
    # Synthetic "document" backing a comment-driven KPI extraction. Each
    # extract_kpi comment lands one row of this type so kpi_facts retains its
    # NOT-NULL source_doc_id FK; the verbatim quote travels in
    # kpi_facts.source_excerpt.
    ANALYST_COMMENT = "analyst_comment"


class FetchStatus(StrEnum):
    PENDING = "pending"
    OK = "ok"
    HTTP_ERROR = "http_error"
    AUTH_ERROR = "auth_error"
    RATE_LIMITED = "rate_limited"
    NETWORK_ERROR = "network_error"
    PARSE_ERROR = "parse_error"


class Document(BaseModel):
    """One ingested artifact — a file on disk plus its provenance metadata."""

    id: int | None = None
    ticker: str
    source_type: SourceType
    doc_type: DocType
    period_start: datetime | None = None
    period_end: datetime | None = None
    file_path: Path
    sha256: str = Field(min_length=64, max_length=64)
    fetched_at: datetime
    fetch_status: FetchStatus
    http_code: int | None = None
    raw_bytes_size: int = Field(ge=0)
    source_url: str | None = None
    parent_document_id: int | None = None
