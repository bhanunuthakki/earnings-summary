"""Determine which sources to ingest for a given ticker.

Per directives/data_pipeline_dag.md routing rules:
- ETF: ETF endpoints only (logically `fmp` source, but ETF doc_types).
- Equity / ADR with no IR-override KPIs: just `fmp`.
- Equity / ADR with at least one KPI whose primary_source != 'fmp':
  union of {fmp, ir_doc, sec_xbrl} (the latter two get pulled in
  addition to the FMP base set).

Routing is data-driven from the kpi_definitions table — adding a row
automatically opts a ticker into IR / SEC ingestion on the next run.
"""

from __future__ import annotations

import sqlite3
from typing import Literal

from pydantic import BaseModel

from models.companies import Company, FilingRegime, InstrumentType, ListType
from models.documents import SourceType

_BASE_EQUITY_SOURCES: frozenset[SourceType] = frozenset({SourceType.FMP})
_ETF_SOURCES: frozenset[SourceType] = frozenset({SourceType.FMP})

# segment_quarterly_framework.md §1.1: which quarterly-segment pipeline serves
# a ticker, keyed EXCLUSIVELY off filing_regime — never off instrument_type
# (an ADR can be 20-F or 40-F) and never off a ticker allowlist scattered
# across scripts.
SegmentQuarterlyPipeline = Literal["tenq_10k_regime", "fpi_6k", "mjds_ir_pdf", "unsupported"]

_REGIME_TO_SEGMENT_PIPELINE: dict[FilingRegime, SegmentQuarterlyPipeline] = {
    FilingRegime.FORM_10K: "tenq_10k_regime",
    FilingRegime.FORM_20F: "fpi_6k",
    FilingRegime.FORM_40F: "mjds_ir_pdf",
}


def segment_pipeline_for_regime(filing_regime: FilingRegime | None) -> SegmentQuarterlyPipeline:
    """Pure mapping: filing_regime -> which quarterly-segment pipeline applies.

    10-K -> ``tenq_10k_regime`` (Phase 1, this framework). 20-F -> ``fpi_6k``
    and 40-F -> ``mjds_ir_pdf`` are both Phase-3-spike-gated (per the design
    doc's §7 risk #1 — do not build extraction code against these before the
    spike confirms ``financial-reports-json`` returns anything for a 20-F/40-F
    filer's Q1..Q3). ``None``/unset -> ``unsupported`` — logged by callers,
    never silently skipped.
    """
    if filing_regime is None:
        return "unsupported"
    return _REGIME_TO_SEGMENT_PIPELINE.get(filing_regime, "unsupported")


class SourcePlan(BaseModel):
    """Sources to ingest for a single ticker on the next pipeline run."""

    ticker: str
    instrument_type: InstrumentType | None
    filing_regime: FilingRegime | None
    sources: set[SourceType]
    ir_urls: list[str]
    primary_kpi_names: list[str]
    # Populated by segment_pipeline_for_regime(filing_regime) — every
    # orchestrator that needs to route the quarterly-segment pipeline choice
    # reads this instead of re-deriving the regime->pipeline mapping.
    segment_quarterly_pipeline: SegmentQuarterlyPipeline | None = None


def _company_for(conn: sqlite3.Connection, ticker: str) -> Company | None:
    """Load the tracked_companies row for ticker (or None if not tracked)."""
    cur = conn.cursor()
    cur.execute(
        "SELECT id, user_id, ticker, name, list_type, added_at, sec_validated, "
        "       ir_url, instrument_type, filing_regime, fiscal_year_end, "
        "       fmp_data_saved, fmp_data_upto "
        "FROM tracked_companies WHERE ticker = ? LIMIT 1",
        (ticker.upper(),),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return Company(
        id=row["id"],
        user_id=row["user_id"],
        ticker=row["ticker"],
        name=row["name"],
        list_type=ListType(row["list_type"]),
        sec_validated=bool(row["sec_validated"]),
        ir_url=row["ir_url"],
        instrument_type=(
            InstrumentType(row["instrument_type"]) if row["instrument_type"] else None
        ),
        filing_regime=(FilingRegime(row["filing_regime"]) if row["filing_regime"] else None),
        fiscal_year_end=row["fiscal_year_end"],
        fmp_data_saved=bool(row["fmp_data_saved"]),
        fmp_data_upto=row["fmp_data_upto"],
    )


def _kpi_rows(conn: sqlite3.Connection, ticker: str) -> list[sqlite3.Row]:
    cur = conn.cursor()
    cur.execute(
        "SELECT name, primary_source, fallback_source, ir_url "
        "FROM kpi_definitions WHERE ticker = ?",
        (ticker.upper(),),
    )
    return cur.fetchall()


def plan_for_ticker(conn: sqlite3.Connection, ticker: str) -> SourcePlan:
    """Build a SourcePlan for `ticker` from the DB.

    Raises ValueError if the ticker is not in tracked_companies. Callers should
    add the ticker first (db.track_company) before requesting a routing plan.
    """
    company = _company_for(conn, ticker)
    if company is None:
        raise ValueError(f"Ticker {ticker!r} is not in tracked_companies")

    if company.instrument_type is InstrumentType.ETF:
        return SourcePlan(
            ticker=company.ticker,
            instrument_type=company.instrument_type,
            filing_regime=company.filing_regime,
            sources=set(_ETF_SOURCES),
            ir_urls=[],
            primary_kpi_names=[],
            segment_quarterly_pipeline=segment_pipeline_for_regime(company.filing_regime),
        )

    sources: set[SourceType] = set(_BASE_EQUITY_SOURCES)
    ir_urls: list[str] = []
    kpi_names: list[str] = []
    for row in _kpi_rows(conn, ticker):
        kpi_names.append(row["name"])
        primary = SourceType(row["primary_source"])
        if primary is not SourceType.FMP:
            sources.add(primary)
        if row["fallback_source"]:
            fallback = SourceType(row["fallback_source"])
            if fallback is not SourceType.FMP:
                sources.add(fallback)
        if row["ir_url"]:
            ir_urls.append(row["ir_url"])

    return SourcePlan(
        ticker=company.ticker,
        instrument_type=company.instrument_type,
        filing_regime=company.filing_regime,
        sources=sources,
        ir_urls=ir_urls,
        primary_kpi_names=kpi_names,
        segment_quarterly_pipeline=segment_pipeline_for_regime(company.filing_regime),
    )
