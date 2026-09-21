"""Field-specific ETF capture admission for deterministic investment labels.

Weekly acquisition cadence: directives/etf_data.md. This policy certifies only
recently observed source values. It does not invent an issuer publication date.
Legacy rows lacking a field receipt, ambiguous timestamps, and stale declared
source dates without a current-field policy remain unavailable. No current claim
can renew a different field.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from models.instruments import EtfFieldEvidence, EtfProfile

PROFILE_CAPTURE_POLICY = "etf_weekly_capture.v1"
PROFILE_CAPTURE_MAX_AGE = timedelta(days=7)
LABEL_FIELDS = (
    "asset_class",
    "benchmark_index",
    "sector_label",
    "expense_ratio",
    "distribution_yield",
)


def capture_profile_fields(profile: EtfProfile, *, source_as_of: date | None = None) -> EtfProfile:
    """Record values actually present in one full source payload."""
    fields: dict[str, EtfFieldEvidence] = {}
    for key in LABEL_FIELDS:
        value = getattr(profile, key)
        if isinstance(value, (str, float)):
            fields[key] = EtfFieldEvidence(
                value=value,
                source=profile.source,
                captured_at=profile.profile_fetched_at,
                source_as_of=source_as_of,
            )
    return profile.model_copy(update={"field_evidence": fields})


def admitted_profile_fields(
    profile: EtfProfile, *, now: datetime | None = None
) -> dict[str, EtfFieldEvidence]:
    """Return captured fields within the weekly acquisition window, failing closed.

    A source-declared as-of predating its capture is historical/unknown currency
    without an issuer-specific validity policy; recapture cannot renew it. Undated
    source
    observations may be recently captured but keep source_as_of=None explicitly.
    This is acquisition freshness, never proof of issuer archive completeness.
    """
    stamp = now or datetime.now(UTC)
    admitted: dict[str, EtfFieldEvidence] = {}
    for key, evidence in profile.field_evidence.items():
        if key not in LABEL_FIELDS or evidence.value != getattr(profile, key):
            continue
        if evidence.captured_at.tzinfo is None:
            continue
        age = stamp - evidence.captured_at
        if not timedelta(0) <= age <= PROFILE_CAPTURE_MAX_AGE:
            continue
        if evidence.source_as_of is not None and not (
            evidence.captured_at.date() <= evidence.source_as_of <= stamp.date()
        ):
            continue
        admitted[key] = evidence
    return admitted
