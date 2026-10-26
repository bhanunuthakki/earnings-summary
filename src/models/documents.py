"""Document = the provenance anchor for every fact in the system.

Per directives/data_provenance.md, every row in financial_facts, segment_facts,
kpi_facts, transcript_segments, and dcf_runs MUST reference a Document via
source_doc_id. Documents are written once per ingestion, use SHA-256 as their
Content Identity, and are never mutated; a changed source observation gets a new
row. Logical Idempotency Key and Attempt Identity are owned by the ingest/run contract;
source metadata supplies the Observation Version.
"""

from __future__ import annotations

import sqlite3
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
    # Audited statements parsed out of an S-1 prospectus — a provisional anchor
    # for recently-IPO'd issuers with no 10-K and empty FMP statement endpoints.
    SEC_S1 = "sec_s1"
    IR_DOC = "ir_doc"
    TRANSCRIPT_AUDIO = "transcript_audio"
    MANUAL_CSV = "manual_csv"
    MANUAL_ENTRY = "manual_entry"
    LLM_EXTRACTED = "llm_extracted"


class SourceQualityTier(StrEnum):
    """Provenance tier — the read-side loader prefers higher tiers.

    Mirrors migration 0054's _BACKFILL_TIER_BY_SOURCE_TYPE mapping and the
    SOURCE_QUALITY_TIER_RANK below. Keep both in sync when adding a tier.
    """

    SEC_OFFICIAL = "sec_official"
    FMP_NORMALIZED = "fmp_normalized"
    LLM_EXTRACTED = "llm_extracted"
    YFINANCE_FALLBACK = "yfinance_fallback"
    # Lowest tier: audited figures lifted from an S-1 before the issuer has filed
    # any 10-Q/10-K. The same numbers will be re-reported (and possibly recast)
    # in real filings, so any FMP/SEC row for the period must win. Maps to rank 0
    # in SOURCE_QUALITY_TIER_RANK (below yfinance_fallback).
    S1_PROVISIONAL = "s1_provisional"


# Higher rank wins. Mirrors the documents.source_quality_tier enum
# in migration 0053. Unknown tiers fall back to 0 (lowest).
#
# `s1_provisional` is audited data lifted from an S-1 before the issuer files
# any 10-Q/10-K; it sits at rank 0 so a real FMP/SEC row always supersedes it
# for the same logical key. (Rank 0 also happens to be the unknown-tier
# fallback, but it's listed explicitly so the intent survives a refactor.)
SOURCE_QUALITY_TIER_RANK: dict[str, int] = {
    "sec_official": 4,
    "fmp_normalized": 3,
    "llm_extracted": 2,
    "yfinance_fallback": 1,
    "s1_provisional": 0,
}


def tier_rank_case_sql(column_alias: str) -> str:
    """Build a CASE expression mapping source_quality_tier text -> int rank.

    Public so the reader families that can't route through the Series loaders
    (cockpit_fundamentals, report §3 financials, fmp_derived_kpis, ask.grounding)
    can replicate the loaders' EXACT ``(tier_rank, id)`` winner ordering in their
    own SQL rather than diverging into a source-agnostic ``source_doc_id DESC``
    pick. ``column_alias`` is the SQL reference to ``documents.source_quality_tier``
    in the caller's query (e.g. ``"d.source_quality_tier"``). Unknown/NULL tiers
    fall to rank 0, matching :data:`SOURCE_QUALITY_TIER_RANK`'s fallback."""
    cases = " ".join(
        f"WHEN '{tier}' THEN {rank}" for tier, rank in SOURCE_QUALITY_TIER_RANK.items()
    )
    return f"CASE {column_alias} {cases} ELSE 0 END"


def _probe_has_table(conn: sqlite3.Connection, table: str) -> bool:
    """row_factory-independent table probe for the reader helpers.

    The Series loaders' ``_has_table``/``_has_column`` read ``r["name"]``, which
    needs ``conn.row_factory = sqlite3.Row``. The reader families call the
    ``reader_tier_*`` helpers with whatever connection they hold — research
    cockpit / the refresh script open a bare (tuple-row) connection — so these
    probes use positional access and work regardless of row_factory."""
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _probe_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """row_factory-independent column probe (see :func:`_probe_has_table`).
    PRAGMA table_info returns (cid, name, type, ...); name is index 1."""
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return False
    return any(r[1] == column for r in rows)


def reader_tier_rank_sql(
    conn: sqlite3.Connection,
    *,
    tier_column: str = "d.source_quality_tier",
    documents_table: str = "documents",
) -> str:
    """Tier-rank ORDER-BY fragment for a reader query, guarded for legacy DBs.

    Returns :func:`tier_rank_case_sql` when ``documents.source_quality_tier``
    exists, else the constant ``"NULL"`` so the caller's ``ORDER BY <this> DESC,
    <table>.id DESC`` collapses to the pre-0053 ``id``-only pick on fixture DBs
    that predate the tier column (or lack the ``documents`` table entirely). The
    same degrade the Series loaders apply via their ``has_tier`` branch — factored
    out so the four reader families that replicate the ordering in raw SQL
    (cockpit_fundamentals, report §3 financials, fmp_derived_kpis, ask.grounding)
    stay column-safe without duplicating the PRAGMA probe.

    The degraded value is ``"NULL"``, NOT ``"0"``: a bare integer in an
    ``ORDER BY`` is a POSITIONAL COLUMN REFERENCE in SQLite (``ORDER BY 0`` →
    "term out of range"), whereas ``NULL`` is a constant that sorts all rows
    equal so the ``id`` tiebreak takes over cleanly."""
    if _probe_has_table(conn, documents_table) and _probe_has_column(
        conn, documents_table, "source_quality_tier"
    ):
        return tier_rank_case_sql(tier_column)
    return "NULL"


def issuer_origin_rank_sql(source_type_column: str) -> str:
    """Stable same-tier tie-break for issuer-originated document facts.

    Reader quality is always decided by :func:`reader_tier_rank_sql` first.
    This expression deliberately has no opinion across tiers: it only keeps an
    issuer document ahead of a vendor normalization when their quality tier is
    equal.
    """
    return f"CASE {source_type_column} WHEN 'ir_doc' THEN 1 WHEN 'sec_xbrl' THEN 1 ELSE 0 END"


def reader_source_order_sql(
    conn: sqlite3.Connection, *, source_type_column: str = "d.source_type"
) -> str:
    """Canonical reader ordering: quality tier, issuer tie-break, stable id.

    Callers append their fact-id tie-break so this helper remains usable for
    different fact tables.  Keeping the two policy components together avoids
    a report surface silently elevating issuer origin above a higher-tier fact.
    """
    return f"{reader_tier_rank_sql(conn)} DESC, {issuer_origin_rank_sql(source_type_column)} DESC"


def reader_tier_join_sql(
    conn: sqlite3.Connection,
    *,
    fact_alias: str = "ff",
    doc_alias: str = "d",
    documents_table: str = "documents",
) -> str:
    """The ``LEFT JOIN documents`` clause a tier-aware reader query needs, or
    ``""`` when the ``documents`` table is absent.

    Paired with :func:`reader_tier_rank_sql`: when documents is missing the rank
    fragment is the constant ``"NULL"`` AND this join is empty, so the query
    never references the ``d`` alias it can't resolve — it degrades cleanly to
    the id-only pick over ``financial_facts`` alone (the pre-0053 behavior). When
    documents exists but the tier column doesn't, the join is still emitted
    (harmless) and the rank fragment is ``"NULL"``."""
    if _probe_has_table(conn, documents_table):
        return f"LEFT JOIN {documents_table} {doc_alias} ON {doc_alias}.id = {fact_alias}.source_doc_id"
    return ""


_SOURCE_TYPE_TO_TIER: dict[SourceType, SourceQualityTier] = {
    SourceType.SEC_XBRL: SourceQualityTier.SEC_OFFICIAL,
    SourceType.FMP: SourceQualityTier.FMP_NORMALIZED,
    SourceType.SEC_S1: SourceQualityTier.S1_PROVISIONAL,
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
    SEC_S1 = "sec_s1"
    SEC_20F = "sec_20f"
    SEC_40F = "sec_40f"
    SEC_8K = "sec_8k"
    SEC_6K = "sec_6k"
    # Aggregate SEC API response. This is an evidence snapshot, not a filing;
    # native filing document types are reserved for separately captured bytes.
    SEC_COMPANYFACTS_SNAPSHOT = "sec_companyfacts_snapshot"
    IR_PRESS_RELEASE = "ir_press_release"
    # Supporting calendar notice. period_end is its publication date, not a fiscal quarter.
    IR_EARNINGS_ANNOUNCEMENT = "ir_earnings_announcement"
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
    # Filing identity (alembic 0075) — SEC EDGAR accession in canonical
    # dashed form ("0001628280-21-010389") and the ISO YYYY-MM-DD date the
    # filing hit EDGAR. NULL for non-filing documents (FMP endpoint dumps,
    # IR PDFs). Backfilled by execution/backfill_document_accessions.py.
    accession_number: str | None = None
    filing_date: str | None = None
