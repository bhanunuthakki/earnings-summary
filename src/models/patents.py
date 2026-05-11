"""Pydantic models for patent + market-signal data.

Used by the NVO external-source pipeline (see directives/nvo_external_sources.md):
  - execution/fetch_drug_patent_status.py
  - execution/extract_nvo_patent_timeline.py
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, Field


class Jurisdiction(str, Enum):
    US = "US"
    EU = "EU"
    CN = "CN"
    IN = "IN"
    BR = "BR"
    CA = "CA"


class ExtensionStatus(str, Enum):
    ORIGINAL = "original"
    EXTENDED = "extended"
    PENDING_EXTENSION = "pending_extension"
    EXPIRED = "expired"
    UNKNOWN = "unknown"
    MANUAL_CHECK_REQUIRED = "manual_check_required"


class FetchSource(str, Enum):
    FDA_ORANGE_BOOK = "fda_orange_book"
    EPO_PUBLIC = "epo_public"
    WIPO_PATENTSCOPE = "wipo_patentscope"
    BR_INPI = "br_inpi"
    CN_CNIPA = "cn_cnipa"
    IN_IPO = "in_ipo"
    CA_CIPO = "ca_cipo"
    NVO_ANNUAL_REPORT = "nvo_annual_report"


class PatentRecord(BaseModel):
    """A single patent record from a jurisdiction register."""

    molecule: str
    jurisdiction: Jurisdiction
    patent_number: str
    title: str | None = None
    grant_date: date | None = None
    expiry_date: date | None = None
    extension_status: ExtensionStatus
    extension_basis: str | None = None
    source: FetchSource
    source_url: str
    pulled_at: datetime
    notes: str | None = None


class PatentFetchSummary(BaseModel):
    """Run-summary written alongside each fetch."""

    molecule: str
    jurisdictions_requested: list[Jurisdiction]
    jurisdictions_succeeded: list[Jurisdiction]
    jurisdictions_manual_only: list[Jurisdiction]
    record_count: int
    pulled_at: datetime
    output_file: str


class NvoSelfDisclosedPatent(BaseModel):
    """A patent expiry entry extracted from NVO's own annual report disclosure."""

    molecule: str
    jurisdictions: list[str]
    expected_loe_year: int | None = None
    extension_strategy_text: str | None = None
    raw_text: str
    source_filename: str
    extracted_at: datetime


class NvoSelfDisclosedTimeline(BaseModel):
    """Full timeline extracted from one annual report."""

    fiscal_year: int
    source_filename: str
    extracted_at: datetime
    patents: list[NvoSelfDisclosedPatent]


class SignalType(str, Enum):
    SCRIPT_VOLUME = "script_volume"
    PRICE_PER_UNIT = "price_per_unit"
    MARKET_SHARE = "market_share"
    MANUFACTURING_CAPACITY = "manufacturing_capacity"
    PATENT_COMMENTARY = "patent_commentary"
    PIPELINE_MILESTONE = "pipeline_milestone"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class MarketSignal(BaseModel):
    """A structured signal extracted from an earnings call transcript."""

    ticker: str = Field(pattern=r"^[A-Z]{1,5}$")
    period: str = Field(pattern=r"^\d{4}Q[1-4]$")
    signal_type: SignalType
    molecule: str | None = None
    geography: str | None = None
    metric_text: str
    parsed_value: float | None = None
    parsed_unit: str | None = None
    direction: str | None = None
    confidence: Confidence
    transcript_filename: str
    extracted_at: datetime


class MarketSignalBundle(BaseModel):
    """All signals extracted from one transcript."""

    ticker: str
    period: str
    transcript_filename: str
    extracted_at: datetime
    signals: list[MarketSignal]
