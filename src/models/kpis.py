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


class ReportingCadence(StrEnum):
    """A KPI's native reporting frequency — the authoritative declaration of how
    often the issuer discloses it.

    Most KPIs are ``QUARTERLY`` (every 10-Q/earnings release). Some are disclosed
    ONLY ``ANNUAL``ly — bank regulatory capital ratios (NU's Basel III CET1/CAR in
    the 20-F), other 10-K/20-F-only figures — and must not be shoehorned onto the
    quarterly axis (gappy chart, mis-counted "consecutive periods"). ``TTM`` is
    admitted to the vocabulary for forward-compat but is treated like quarterly
    until a consumer special-cases it.

    Stored on ``kpi_definitions.reporting_cadence`` (migration 0072). Annual facts
    are additionally tagged ``fiscal_period_type='FY'`` with a fiscal-year-end
    ``period_end`` (see ``compute.kpi_resolver.ANNUAL_FACT_PERIOD_TYPES``).
    """

    QUARTERLY = "quarterly"
    ANNUAL = "annual"
    TTM = "ttm"


class BreachStatus(StrEnum):
    OK = "ok"
    WARN = "warn"
    BREACH = "breach"
    # The rule's KPI couldn't be resolved to any kpi_facts definition (a derived
    # series the pipeline hasn't materialized, or a metric never extracted for
    # this ticker). Distinct from OK so an unevaluable breaker can't masquerade
    # as a passing rule.
    UNRESOLVED = "unresolved"


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
    # Native reporting frequency. Defaults to QUARTERLY for every existing
    # definition; set ANNUAL for issuer-annual-only metrics (bank capital ratios)
    # so rendering / break-rule period-counting / DCF reads switch to the annual
    # axis. See migration 0072.
    reporting_cadence: ReportingCadence = ReportingCadence.QUARTERLY
