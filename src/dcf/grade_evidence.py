"""Typed, bounded evidence projection for one persisted DCF run.

This is deliberately a read model, not a grader.  It exposes the exact latest
top-level ``dcf_runs`` row and the receipts already persisted with that row so a
human or judge can apply a rubric without inferring evidence from UI copy.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import date, datetime
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict

MAX_SERIALIZED_EVIDENCE_BYTES = 100_000


class DcfEvidenceChecks(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_hash_valid: bool
    workbook_hash_valid: bool
    snapshot_status: Literal["valid", "missing", "invalid"]
    provenance_status: Literal["valid", "missing", "invalid"]
    source_count: int
    scenario_receipt_present: bool
    reverse_receipt_present: bool
    primary_fact_overlay_status: str
    equity_bridge_status: str
    country_risk_authority: str | None
    market_price_consistent: bool


class DcfGradeEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["dcf_grade_evidence.v1"] = "dcf_grade_evidence.v1"
    status: Literal["available", "missing", "invalid"]
    ticker: str
    missing_columns: tuple[str, ...] = ()
    invalid_reason: str | None = None
    run_id: int | None = None
    created_at: str | None = None
    valuation_date: str | None = None
    engine_version: str | None = None
    input_sha256: str | None = None
    workbook_sha256: str | None = None
    inputs_as_of: str | None = None
    live_price: float | None = None
    live_price_at: str | None = None
    npv_per_share: float | None = None
    over_under_pct: float | None = None
    sanity_flag: str | None = None
    assumption_snapshot: dict[str, object] | None = None
    provenance: dict[str, object] | None = None
    checks: DcfEvidenceChecks | None = None


_REQUIRED_COLUMNS = frozenset(
    {
        "id",
        "ticker",
        "created_at",
        "valuation_date",
        "engine_version",
        "input_sha256",
        "workbook_sha256",
        "inputs_as_of",
        "live_price",
        "live_price_at",
        "npv_per_share",
        "over_under_pct",
        "sanity_flag",
        "assumption_snapshot_json",
        "provenance_json",
        "is_latest",
        "segment_name",
    }
)


def _json_object(value: object) -> tuple[dict[str, object] | None, str]:
    if not isinstance(value, str) or not value.strip():
        return None, "missing"
    try:
        parsed: object = json.loads(value)
    except json.JSONDecodeError:
        return None, "invalid"
    if not isinstance(parsed, dict):
        return None, "invalid"
    parsed_mapping = cast("dict[str, object]", parsed)
    if not _json_numbers_are_finite(parsed_mapping):
        return None, "invalid"
    return parsed_mapping, "valid"


def _json_numbers_are_finite(value: object) -> bool:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return True
    if isinstance(value, (int, float)):
        return math.isfinite(float(value))
    if isinstance(value, list):
        return all(_json_numbers_are_finite(item) for item in cast("list[object]", value))
    if isinstance(value, dict):
        mapping = cast("dict[object, object]", value)
        return all(
            isinstance(key, str) and _json_numbers_are_finite(item) for key, item in mapping.items()
        )
    return False


def _valid_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )


def _optional_text(value: object) -> bool:
    return value is None or isinstance(value, str)


def _valid_iso_datetime(value: object) -> bool:
    if value is None:
        return True
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _valid_iso_date(value: object) -> bool:
    if value is None:
        return True
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _nested_mapping(value: object, key: str) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    child = cast("dict[str, object]", value).get(key)
    return cast("dict[str, object]", child) if isinstance(child, dict) else None


def _status_from(detail: dict[str, object] | None, key: str, *, fallback: str) -> str:
    block = _nested_mapping(detail, key)
    status = block.get("status") if block is not None else None
    return str(status) if isinstance(status, str) and status else fallback


def _project_primary_fact_overlay(value: object) -> dict[str, object] | None:
    """Keep status/counts only; historical fact rows belong to the source ledger."""
    if not isinstance(value, dict):
        return None
    overlay = cast("dict[str, object]", value)
    projected: dict[str, object] = {}
    for key in ("status", "degraded_reason", "reasons"):
        if key in overlay:
            projected[key] = overlay[key]
    statements = overlay.get("statements")
    if isinstance(statements, dict):
        statement_summary: dict[str, object] = {}
        for name, raw in cast("dict[str, object]", statements).items():
            if not isinstance(raw, dict):
                continue
            item = cast("dict[str, object]", raw)
            summary: dict[str, object] = {}
            for key in ("status", "degraded_reason"):
                if key in item:
                    summary[key] = item[key]
            for key in ("applied", "conflicts", "rejected"):
                entries = item.get(key)
                if isinstance(entries, list):
                    summary[f"{key}_count"] = len(cast("list[object]", entries))
            statement_summary[name] = summary
        projected["statements"] = statement_summary
    return projected


def _project_snapshot(snapshot: dict[str, object]) -> dict[str, object]:
    """Retain model assumptions and receipts, excluding historical overlays."""
    return {
        key: value
        for key, value in snapshot.items()
        if key not in {"primary_fact_overlay", "historical_primary_fact_overlay"}
    }


def _project_provenance(provenance: dict[str, object]) -> dict[str, object]:
    """Bound provenance to conclusion-driving receipts and source digests."""
    projected: dict[str, object] = {}
    for key in ("ticker", "inputs_as_of_status", "market_price", "country_risk_context"):
        if key in provenance:
            projected[key] = provenance[key]

    raw_sources = provenance.get("sources")
    if isinstance(raw_sources, list):
        sources: list[dict[str, object]] = []
        for raw in cast("list[object]", raw_sources):
            if not isinstance(raw, dict):
                continue
            source = cast("dict[str, object]", raw)
            summary = {
                key: source[key]
                for key in (
                    "role",
                    "path",
                    "locator",
                    "url",
                    "sha256",
                    "bytes",
                    "observed_at",
                    "influences_calculation",
                )
                if key in source
            }
            sources.append(summary)
        projected["sources"] = sources

    bridge = provenance.get("equity_bridge_receipt")
    if isinstance(bridge, dict):
        # The receipt's component lineage is conclusion-driving. It is bounded
        # already by the receipt schema and deliberately excludes the overlay.
        projected["equity_bridge_receipt"] = bridge
    overlay = _project_primary_fact_overlay(provenance.get("primary_fact_overlay"))
    if overlay is not None:
        projected["primary_fact_overlay"] = overlay
    return projected


def _serialized_size(value: object) -> int:
    return len(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )


_BOUNDED_TEXT_CHARS = 512
_BOUNDED_MAX_ITEMS = 256
_BOUNDED_MAX_DEPTH = 8
_BOUNDED_PRIORITY_KEYS: dict[str, tuple[str, ...]] = {
    "assumption_snapshot": ("scenarios", "priced_in", "reverse_valuation"),
    "scenarios": ("bull", "base", "bear"),
    "provenance": (
        "equity_bridge_receipt",
        "market_price",
        "country_risk_context",
        "sources",
        "primary_fact_overlay",
    ),
    "equity_bridge_receipt": (
        "schema_version",
        "ticker",
        "status",
        "arithmetic_status",
        "operating_value_usd_m",
        "cash_m",
        "total_debt_m",
        "diluted_shares_m",
        "fx_to_usd",
        "stored_value_per_share_usd",
        "recomputed_value_per_share_usd",
        "arithmetic_delta",
        "reporting_currency",
        "bridge_period_end",
        "bridge_fiscal_period_type",
        "bridge_context",
        "cash_lineage",
        "total_debt_lineage",
        "reasons",
    ),
    "market_price": ("price", "observed_at", "source"),
    "country_risk_context": ("authority", "country", "rate"),
}


def _bounded_text(value: str, max_bytes: int) -> str:
    """Return a deterministic, UTF-8-safe prefix fitting ``max_bytes``."""
    candidate = value[:_BOUNDED_TEXT_CHARS]
    while candidate and _serialized_size(candidate) > max_bytes:
        candidate = candidate[: max(1, len(candidate) // 2)]
    return candidate if _serialized_size(candidate) <= max_bytes else ""


def _bounded_value(
    value: object,
    max_bytes: int,
    *,
    container_name: str | None = None,
    depth: int = 0,
) -> object:
    """Project arbitrary JSON into a deterministic byte-bounded JSON value.

    Priority keys are visited first so a tight budget cannot hide the receipt
    fields that explain the conclusion behind arbitrary payloads.
    """
    if max_bytes < 2 or depth > _BOUNDED_MAX_DEPTH:
        return None
    if isinstance(value, str):
        return _bounded_text(value, max_bytes)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, list):
        projected_list: list[object] = []
        for item in cast("list[object]", value)[:_BOUNDED_MAX_ITEMS]:
            remaining = max_bytes - _serialized_size(projected_list) - 2
            if remaining < 2:
                break
            projected_item = _bounded_value(item, remaining, depth=depth + 1)
            candidate = [*projected_list, projected_item]
            if _serialized_size(candidate) > max_bytes:
                break
            projected_list.append(projected_item)
        return projected_list
    if isinstance(value, dict):
        mapping = cast("dict[str, object]", value)
        priority = _BOUNDED_PRIORITY_KEYS.get(container_name or "", ())
        ordered_keys = [key for key in priority if key in mapping]
        ordered_keys.extend(sorted(key for key in mapping if key not in ordered_keys))
        projected_mapping: dict[str, object] = {}
        for key in ordered_keys[:_BOUNDED_MAX_ITEMS]:
            remaining = max_bytes - _serialized_size(projected_mapping) - 2
            if remaining < 2:
                break
            projected_key = _bounded_text(key, min(_BOUNDED_TEXT_CHARS, max(2, remaining // 2)))
            child_budget = max(2, remaining - _serialized_size({projected_key: None}))
            projected_value = _bounded_value(
                mapping[key],
                child_budget,
                container_name=key,
                depth=depth + 1,
            )
            candidate = {**projected_mapping, projected_key: projected_value}
            if _serialized_size(candidate) > max_bytes:
                continue
            projected_mapping[projected_key] = projected_value
        return projected_mapping
    return None


def _bounded_available_evidence(evidence: DcfGradeEvidence) -> DcfGradeEvidence:
    """Keep an oversized result available while retaining its audit anchors."""
    data = cast("dict[str, object]", evidence.model_dump(mode="json"))
    # Scalar columns are untrusted too (for example, an accidentally repeated
    # workbook error can be megabytes long), so bound them before allocating the
    # remaining budget to structured receipts.
    for key, value in tuple(data.items()):
        if isinstance(value, str):
            data[key] = _bounded_text(value, _BOUNDED_TEXT_CHARS * 4)
    checks = data.get("checks")
    if isinstance(checks, dict):
        checks_mapping = cast("dict[str, object]", checks)
        for key, value in tuple(checks_mapping.items()):
            if isinstance(value, str):
                checks_mapping[key] = _bounded_text(value, _BOUNDED_TEXT_CHARS)

    data["assumption_snapshot"] = {}
    data["provenance"] = {}
    fixed_size = _serialized_size(data)
    available = max(2, MAX_SERIALIZED_EVIDENCE_BYTES - fixed_size - 1)
    snapshot_budget = max(2, available * 3 // 10)
    provenance_budget = max(2, available - snapshot_budget)
    data["assumption_snapshot"] = _bounded_value(
        evidence.assumption_snapshot,
        snapshot_budget,
        container_name="assumption_snapshot",
    )
    data["provenance"] = _bounded_value(
        evidence.provenance,
        provenance_budget,
        container_name="provenance",
    )
    projected = DcfGradeEvidence.model_validate(data)
    # The budgets above are additive with the fixed envelope. This assertion is
    # intentionally executable: a future schema field cannot silently re-open
    # the oversized invalid-shell regression.
    if _serialized_size(cast("object", projected.model_dump(mode="json"))) >= (
        MAX_SERIALIZED_EVIDENCE_BYTES
    ):
        raise ValueError("bounded DCF evidence projection exceeded byte budget")
    return projected


def _market_price_consistent(
    provenance: dict[str, object] | None,
    *,
    live_price: float | None,
    live_price_at: str | None,
) -> bool:
    market = _nested_mapping(provenance, "market_price")
    if market is None:
        return False
    price = market.get("price")
    observed_at = market.get("observed_at")
    price_matches = (
        live_price is None
        if price is None
        else isinstance(price, (int, float))
        and not isinstance(price, bool)
        and live_price is not None
        and abs(float(price) - live_price) <= 1e-9
    )
    clock_matches = (live_price_at is None and observed_at is None) or (
        isinstance(observed_at, str)
        and live_price_at is not None
        and observed_at.replace("Z", "+00:00") == live_price_at.replace("Z", "+00:00")
    )
    return price_matches and clock_matches


def load_dcf_grade_evidence(conn: sqlite3.Connection, ticker: str) -> DcfGradeEvidence:
    """Read exactly one latest, consolidated row and fail closed on schema drift."""

    normalized_ticker = ticker.upper()
    try:
        columns: set[str] = {str(row[1]) for row in conn.execute("PRAGMA table_info(dcf_runs)")}
    except sqlite3.Error:
        columns = set()
    missing_columns = tuple(sorted(_REQUIRED_COLUMNS - columns))
    if missing_columns:
        return DcfGradeEvidence(
            status="invalid",
            ticker=normalized_ticker,
            missing_columns=missing_columns,
        )

    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT id, ticker, created_at, valuation_date, engine_version,
                   input_sha256, workbook_sha256, inputs_as_of,
                   live_price, live_price_at, npv_per_share, over_under_pct,
                   sanity_flag, assumption_snapshot_json, provenance_json
            FROM dcf_runs
            WHERE UPPER(ticker) = ?
              AND COALESCE(is_latest, 1) = 1
              AND COALESCE(segment_name, '') = ''
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (normalized_ticker,),
        ).fetchone()
    except sqlite3.Error:
        return DcfGradeEvidence(
            status="invalid",
            ticker=normalized_ticker,
            invalid_reason="row_query_failed",
        )
    if row is None:
        return DcfGradeEvidence(status="missing", ticker=normalized_ticker)

    snapshot, snapshot_status = _json_object(row["assumption_snapshot_json"])
    provenance, provenance_status = _json_object(row["provenance_json"])
    invalid_json = [
        f"{field}_{status}"
        for field, status in (
            ("assumption_snapshot", snapshot_status),
            ("provenance", provenance_status),
        )
        if status != "valid"
    ]
    if invalid_json:
        return DcfGradeEvidence(
            status="invalid",
            ticker=normalized_ticker,
            invalid_reason=",".join(invalid_json),
        )
    assert snapshot is not None and provenance is not None
    invalid_scalar = not isinstance(row["id"], int) or any(
        row[field] is not None
        and (
            not isinstance(row[field], (int, float))
            or isinstance(row[field], bool)
            or not math.isfinite(float(row[field]))
        )
        for field in ("live_price", "npv_per_share", "over_under_pct")
    )
    raw_live_price = row["live_price"]
    invalid_price = (
        isinstance(raw_live_price, (int, float))
        and not isinstance(raw_live_price, bool)
        and raw_live_price <= 0
    )
    invalid_text = not isinstance(row["ticker"], str) or any(
        not _optional_text(row[field])
        for field in (
            "engine_version",
            "input_sha256",
            "workbook_sha256",
            "sanity_flag",
        )
    )
    invalid_clock = (
        not _valid_iso_datetime(row["created_at"])
        or not _valid_iso_date(row["valuation_date"])
        or not _valid_iso_datetime(row["inputs_as_of"])
        or not _valid_iso_datetime(row["live_price_at"])
    )
    if invalid_scalar or invalid_price or invalid_text or invalid_clock:
        return DcfGradeEvidence(
            status="invalid",
            ticker=normalized_ticker,
            invalid_reason="row_decode_failed",
        )
    engine_version = str(row["engine_version"]) if row["engine_version"] is not None else None
    specialized = engine_version is not None and engine_version != "redesign_fcff_v1"
    scenarios = _nested_mapping(snapshot, "scenarios")
    reverse = _nested_mapping(snapshot, "priced_in") or _nested_mapping(
        snapshot, "reverse_valuation"
    )
    raw_sources = provenance.get("sources")
    sources = cast("list[object]", raw_sources) if isinstance(raw_sources, list) else []
    country_risk = _nested_mapping(provenance, "country_risk_context")
    country_authority = country_risk.get("authority") if country_risk is not None else None
    live_price = float(row["live_price"]) if row["live_price"] is not None else None
    live_price_at = str(row["live_price_at"]) if row["live_price_at"] is not None else None
    checks = DcfEvidenceChecks(
        input_hash_valid=_valid_sha256(row["input_sha256"]),
        workbook_hash_valid=_valid_sha256(row["workbook_sha256"]),
        snapshot_status=cast("Literal['valid', 'missing', 'invalid']", snapshot_status),
        provenance_status=cast("Literal['valid', 'missing', 'invalid']", provenance_status),
        source_count=len(sources),
        scenario_receipt_present=scenarios is not None,
        reverse_receipt_present=reverse is not None,
        primary_fact_overlay_status=_status_from(
            provenance,
            "primary_fact_overlay",
            fallback="not_applicable" if specialized else "missing",
        ),
        equity_bridge_status=_status_from(
            provenance,
            "equity_bridge_receipt",
            fallback="not_applicable" if specialized else "missing",
        ),
        country_risk_authority=(
            str(country_authority) if isinstance(country_authority, str) else None
        ),
        market_price_consistent=_market_price_consistent(
            provenance,
            live_price=live_price,
            live_price_at=live_price_at,
        ),
    )
    evidence = DcfGradeEvidence(
        status="available",
        ticker=normalized_ticker,
        run_id=int(row["id"]),
        created_at=str(row["created_at"]) if row["created_at"] is not None else None,
        valuation_date=(str(row["valuation_date"]) if row["valuation_date"] is not None else None),
        engine_version=engine_version,
        input_sha256=str(row["input_sha256"]) if row["input_sha256"] is not None else None,
        workbook_sha256=(
            str(row["workbook_sha256"]) if row["workbook_sha256"] is not None else None
        ),
        inputs_as_of=str(row["inputs_as_of"]) if row["inputs_as_of"] is not None else None,
        live_price=live_price,
        live_price_at=live_price_at,
        npv_per_share=(float(row["npv_per_share"]) if row["npv_per_share"] is not None else None),
        over_under_pct=(
            float(row["over_under_pct"]) if row["over_under_pct"] is not None else None
        ),
        sanity_flag=str(row["sanity_flag"]) if row["sanity_flag"] is not None else None,
        assumption_snapshot=_project_snapshot(snapshot),
        provenance=_project_provenance(provenance),
        checks=checks,
    )
    if _serialized_size(cast("object", evidence.model_dump(mode="json"))) >= (
        MAX_SERIALIZED_EVIDENCE_BYTES
    ):
        return _bounded_available_evidence(evidence)
    return evidence


__all__ = [
    "MAX_SERIALIZED_EVIDENCE_BYTES",
    "DcfEvidenceChecks",
    "DcfGradeEvidence",
    "load_dcf_grade_evidence",
]
