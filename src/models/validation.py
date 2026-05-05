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


class ValidationRule(StrEnum):
    """Closed enum of validation rules. Never freeform-classify a violation."""

    PLAUSIBLE_RANGE = "plausible_range"
    CURRENCY_REQUIRED = "currency_required"
    UNIT_REQUIRED = "unit_required"
    PERIOD_ORDER = "period_order"
    FISCAL_YEAR_MISMATCH = "fiscal_year_mismatch"
    MAGNITUDE_JUMP = "magnitude_jump"
    MISSING_FIELD = "missing_field"
    SCHEMA_DRIFT = "schema_drift"
    DUPLICATE_FACT = "duplicate_fact"
    SOURCE_DISAGREEMENT = "source_disagreement"


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
    resolved_at: datetime | None
