"""Field-specific ETF capture admission for deterministic investment labels.

Weekly acquisition cadence: directives/etf_data.md. This policy certifies only
recently observed source values. It does not invent an issuer publication date.
Legacy rows lacking a field receipt, ambiguous timestamps, and stale declared
source dates without a current-field policy remain unavailable. No current claim
can renew a different field.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, cast

from models.instruments import EtfFieldEvidence, EtfProfile

if TYPE_CHECKING:
    from pipeline.fmp_doc_index import EtfCaptureReceipt

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
        if evidence.source == "fmp" and not evidence.has_capture_linkage:
            continue
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


def fmp_text_value(v: object) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def fmp_float_value(v: object) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if not isinstance(v, (str, int, float)):
        return None
    if isinstance(v, str) and not v.strip():
        return None
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return f if math.isfinite(f) else None


def fmp_percent_decimal(v: object) -> float | None:
    """Coerce '7.42', 7.42, or '7.42%' to 0.0742.

    FMP returns weights, expense ratios, yields, and premium/discount in
    *percent units* (e.g. `weightPercentage: 9.42` means 9.42%, not 9.42).
    We always divide by 100 to produce a canonical decimal in [0, 1ish].

    Trailing '%' is tolerated for the string-shaped inputs FMP occasionally
    returns for descriptive fields.
    """
    f = (
        fmp_float_value(v)
        if not (isinstance(v, str) and v.endswith("%"))
        else fmp_float_value(v[:-1].strip())
    )
    if f is None:
        return None
    return f / 100.0


def bind_fmp_profile_fields(
    profile: EtfProfile,
    receipt: EtfCaptureReceipt,
    payload: object,
) -> EtfProfile:
    if profile.source != "fmp":
        raise ValueError("profile source must be fmp")
    if profile.profile_fetched_at != receipt.captured_at:
        raise ValueError("profile fetched_at must equal receipt captured_at")
    if profile.profile_fetched_at.tzinfo is None or profile.profile_fetched_at.utcoffset() is None:
        raise ValueError("profile fetched_at must be timezone aware")
    expected_url = (
        f"https://financialmodelingprep.com/stable/etf/info?symbol={profile.ticker.upper()}"
    )
    if receipt.source_url != expected_url:
        raise ValueError("receipt source_url does not match profile ticker")
    if isinstance(payload, dict):
        source = cast(dict[str, object], payload)
        prefix = ""
    elif isinstance(payload, list):
        rows = cast("list[object]", payload)
        if len(rows) != 1 or not isinstance(rows[0], dict):
            raise ValueError("payload must contain one FMP object")
        source = cast("dict[str, object]", rows[0])
        prefix = "/0"
    else:
        raise ValueError("payload must be a dict or a single-element list of dicts")
    choices: dict[str, tuple[str, ...]] = {
        "asset_class": ("assetClass",),
        "benchmark_index": ("etfIndex", "benchmark"),
        "sector_label": ("sectorsList", "sector"),
        "expense_ratio": ("expenseRatio",),
        "distribution_yield": ("yield", "dividendYield"),
    }
    fields: dict[str, EtfFieldEvidence] = {}
    for field in LABEL_FIELDS:
        chosen_key = ""
        chosen_value: str | float | None = None
        for key in choices[field]:
            raw = source.get(key)
            value = (
                fmp_text_value(raw)
                if field in ("asset_class", "benchmark_index", "sector_label")
                else fmp_percent_decimal(raw)
            )
            if value is not None:
                chosen_key = key
                chosen_value = value
                break
        if chosen_value != getattr(profile, field):
            raise ValueError(f"payload {field} does not reconstruct profile")
        if chosen_value is None:
            continue
        existing = profile.field_evidence.get(field)
        if existing is None:
            continue
        if existing.value != chosen_value:
            raise ValueError(f"existing evidence for {field} has wrong value")
        if existing.source != "fmp":
            raise ValueError(f"existing evidence for {field} has wrong source")
        if existing.captured_at != receipt.captured_at:
            raise ValueError(f"existing evidence for {field} has wrong captured_at")
        fields[field] = EtfFieldEvidence(
            document_id=receipt.document_id,
            document_version_id=receipt.document_version_id,
            observation_id=receipt.observation_id,
            content_sha256=receipt.content_sha256,
            source_url=receipt.source_url,
            value=chosen_value,
            source="fmp",
            captured_at=receipt.captured_at,
            source_as_of=existing.source_as_of,
            json_pointer=prefix + "/" + chosen_key,
            parser_version="etf-fmp-profile.v1",
        )
    return profile.model_copy(update={"field_evidence": fields})
