"""KPI definitions, thresholds, and the per-name source-routing registry.

The registry encodes which KPIs come from FMP vs. IR PDFs vs. transcripts vs.
manual entry per ticker. Persistence runs through `src/pipeline/kpi_persistence.py`;
INGEST-stage source routing is described in `directives/data_pipeline_dag.md` §Routing.
See `directives/data_provenance.md` for the source-of-truth taxonomy.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from .documents import SourceType
from .facts import Unit


class ThesisTier(StrEnum):
    """Severity of a KPI threshold breach vs. the thesis."""

    TIER_1_BREAK = "tier_1_break"
    TIER_2_MONITOR = "tier_2_monitor"


class BreachStatus(StrEnum):
    OK = "ok"
    WARN = "warn"
    BREACH = "breach"


class KpiDefinition(BaseModel):
    """A tracked KPI for a given ticker, with sourcing and threshold rules."""

    id: int | None = None
    ticker: str
    name: str
    unit: Unit
    primary_source: SourceType
    fallback_source: SourceType | None
    ir_url: str | None
    threshold_tier: ThesisTier | None
    threshold_low: float | None
    threshold_high: float | None
    notes: str | None
