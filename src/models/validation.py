"""Validation issues — the quarantine layer between Parse and Persist.

Severity HALT terminates the run before PERSIST. Severity WARN is recorded but
allows the run to continue; the row in question is flagged for review.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel


class Severity(StrEnum):
    WARN = "warn"
    HALT = "halt"


#: Canonical severity priority, **most severe first** — the quarantine/sort
#: order. Derived consumers (the report Sources ``ORDER BY`` CASE, the prov_row
#: severity tick) build their mapping from this/the enum so a renamed or added
#: severity can't silently sort last or render the wrong color. The writer emits
#: ``halt``/``warn`` (``record_validation_issue``); a reader that hard-codes a
#: different vocabulary (the shipped ``error``/``warning`` bug) is exactly the
#: drift ``test_provenance_severity_contract`` guards against — it asserts this
#: tuple covers every ``Severity`` member.
SEVERITY_ORDER: tuple[Severity, ...] = (Severity.HALT, Severity.WARN)


class ValidationRule(StrEnum):
    """Closed enum of validation rules. Never freeform-classify a violation."""

    PLAUSIBLE_RANGE = "plausible_range"
    CURRENCY_REQUIRED = "currency_required"
    UNIT_REQUIRED = "unit_required"
    # An extracted value's unit can't be reconciled to the KPI's canonical
    # (break-rule-declared) unit because the two belong to different dimensional
    # families — a likely extraction error or rule misconfiguration. The value is
    # persisted as-extracted and flagged rather than rescaled across dimensions.
    UNIT_MISMATCH = "unit_mismatch"
    PERIOD_ORDER = "period_order"
    FISCAL_YEAR_MISMATCH = "fiscal_year_mismatch"
    MAGNITUDE_JUMP = "magnitude_jump"
    MISSING_FIELD = "missing_field"
    SCHEMA_DRIFT = "schema_drift"
    DUPLICATE_FACT = "duplicate_fact"
    SOURCE_DISAGREEMENT = "source_disagreement"
    # A fact-reader materialized a value that is NOT the tier-winner the
    # canonical loader (timeseries.loaders.load_financial_series) picks for a
    # duplicated (ticker, period_end, fiscal_period_type, line_item) key — i.e.
    # a reader regressed off the (source_quality_tier, id) contract. Raised by
    # the reader-tier audit (pipeline.reader_tier_audit), never at ingest.
    READER_TIER_MISMATCH = "reader_tier_mismatch"
    # A KPI marked cumulative (pipeline.kpi_persistence._is_cumulative_kpi —
    # e.g. "Total customers") decreased chronologically. Cumulative series
    # should be non-decreasing; a decrease is a genuine data-quality signal
    # (restatement, extraction error) — the persist-time guard REJECTS the
    # write and raises this rather than silently storing a broken series.
    NON_MONOTONIC_CUMULATIVE = "non_monotonic_cumulative"
    # A writer explicitly opted out of a renderable FactLocator via
    # pipeline.locators.LegacyEscapeHatch instead of populating one — logged
    # (severity=WARN, per this module's only two levels) with the writer's
    # grep-able reason string, so execution/provenance_coverage_report.py can
    # count deliberate gaps separately from silent ones
    # (docs/design/provenance_clickthrough.md §4.1).
    LOCATOR_ESCAPE_HATCH = "locator_escape_hatch"
    # An LLM extractor returned a grounding ``anchor_quote``/``source_excerpt``
    # for a persisted value that pipeline.locators.verify_quote_in_source could
    # NOT find verbatim (whitespace/case-normalized) in the actual source
    # document text. The value is still persisted (never dropped for this
    # reason alone) but its locator is demoted to a LegacyEscapeHatch rather
    # than the kind the quote would otherwise have earned — this rule makes
    # that demotion visible rather than silent (docs/design/
    # provenance_clickthrough.md §3.3, Phase C).
    HALLUCINATED_ANCHOR = "hallucinated_anchor"


class ValidationIssue(BaseModel):
    """One violation; severity drives whether the run halts."""

    id: int | None = None
    run_id: str
    source_doc_id: int | None
    ticker: str | None
    severity: Severity
    rule: ValidationRule
    raw_value: str | None
    expected: str | None
    raised_at: datetime
    resolved_at: datetime | None = None
    # Who resolved it + their note (alembic 0094) — the actionable-provenance
    # audit trail. None on an open issue and on legacy rows raised before the
    # resolve writer existed.
    resolved_by: str | None = None
    resolution_note: str | None = None
