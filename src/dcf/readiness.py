"""Conservative read-only preflight for the evidence actually used by a DCF.

This composes existing persisted-DCF and sealed-fact readers. File hashes,
current quotes and unrelated current facts do not establish complete/current
financial inputs. Existing valuation amounts remain available for display.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict

from dcf.grade_evidence import DcfGradeEvidence, load_dcf_grade_evidence
from provenance.canonical_fact_resolution import CanonicalFactResolutionEngine
from provenance.fact_read_model import (
    FactAdmissionError,
    FactReadModel,
    ObservationUnavailableError,
)
from provenance.metric_ontology import MetricOntology
from sources.discovery_financials import GrowthFactReference, read_financial_history
from sources.market_price_policy import PRICE_STALE_DAYS


class FinancialInputEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    observation_id: str | None = None
    status: Literal["admitted", "missing", "failed", "semantic_gap"]
    reason_code: str | None = None
    period_end: str | None = None
    knowledge_at: str | None = None
    recorded_at: str | None = None
    document_version_id: str | None = None


class ValuationReadiness(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["valuation_readiness.v1"] = "valuation_readiness.v1"
    ticker: str
    evaluated_at: str
    ready: bool = False
    status: Literal["ready", "degraded", "missing", "failed"] = "degraded"
    reason_codes: tuple[str, ...] = ()
    run_id: int | None = None
    input_sha256: str | None = None
    valuation_date: str | None = None
    model_calculated_at: str | None = None
    legacy_input_cutoff: str | None = None
    market_observed_at: str | None = None
    market_status: Literal["current", "stale", "missing", "invalid"] = "missing"
    financial_period_end: str | None = None
    financial_inputs: tuple[FinancialInputEvidence, ...] = ()
    financial_input_completeness: Literal["unverified"] = "unverified"
    latest_reporting_period_status: Literal["unverified"] = "unverified"
    assumption_reviewed_at: str | None = None
    source_clocks: tuple[dict[str, object], ...] = ()


def _clock(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _mapping(value: object) -> dict[str, object]:
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def _items(value: object) -> list[object]:
    return cast("list[object]", value) if isinstance(value, list) else []


def _fact_references(evidence: DcfGradeEvidence) -> tuple[dict[str, object], ...]:
    overlay = _mapping((evidence.provenance or {}).get("primary_fact_overlay"))
    statements = _mapping(overlay.get("statements"))
    return tuple(
        _mapping(item)
        for statement in statements.values()
        for item in _items(_mapping(statement).get("applied_references"))
    )


def _admit_input(
    reader: FactReadModel,
    resolver: CanonicalFactResolutionEngine,
    ontology: MetricOntology,
    reference: dict[str, object],
    cutoff: datetime,
    canonical_inputs: dict[str, GrowthFactReference],
) -> FinancialInputEvidence:
    raw_id = reference.get("reported_observation_id")
    observation_id = raw_id if isinstance(raw_id, str) and raw_id else None
    if observation_id is None:
        return FinancialInputEvidence(status="missing", reason_code="run_input_observation_missing")
    try:
        bundle = reader.provenance_bundle(observation_id, cutoff=cutoff)
        canonical = canonical_inputs.get(observation_id)
        if canonical is None:
            return FinancialInputEvidence(
                observation_id=observation_id,
                status="semantic_gap",
                reason_code="run_input_outside_ticker_reported_financial_slice",
            )
        current = resolver.as_known(canonical.canonical_metric_cell_id, cutoff)
        definition = ontology.metric_definition_as_known(canonical.metric_id, cutoff)
        binding = ontology.binding_as_known(observation_id, cutoff)
        if (
            current is None
            or current.status != "resolved"
            or current.selected_observation_id != observation_id
            or current.canonical_resolution_revision_id
            != canonical.canonical_resolution_revision_id
            or definition is None
            or definition.metric_definition_revision_id != canonical.metric_definition_revision_id
            or binding is None
            or binding.binding_status != "bound"
            or binding.canonical_metric_cell_id != canonical.canonical_metric_cell_id
        ):
            return FinancialInputEvidence(
                observation_id=observation_id,
                status="semantic_gap",
                reason_code="run_input_no_longer_resolved",
            )
        claimed_period = reference.get("period_end")
        period = bundle.cell.period_end.date().isoformat()
        if claimed_period is not None and claimed_period != period:
            return FinancialInputEvidence(
                observation_id=observation_id,
                status="semantic_gap",
                reason_code="run_input_period_mismatch",
            )
        if bundle.cell.period_end > cutoff:
            return FinancialInputEvidence(
                observation_id=observation_id,
                status="semantic_gap",
                reason_code="run_input_period_after_cutoff",
            )
        return FinancialInputEvidence(
            observation_id=observation_id,
            status="admitted",
            period_end=period,
            knowledge_at=bundle.observation.knowledge_at.isoformat(),
            recorded_at=bundle.observation.recorded_at.isoformat(),
            document_version_id=bundle.evidence.document_version_id if bundle.evidence else None,
        )
    except ObservationUnavailableError:
        return FinancialInputEvidence(
            observation_id=observation_id,
            status="missing",
            reason_code="run_input_observation_unavailable_at_cutoff",
        )
    except FactAdmissionError as exc:
        return FinancialInputEvidence(
            observation_id=observation_id, status="semantic_gap", reason_code=exc.reason_code
        )
    except sqlite3.Error:
        return FinancialInputEvidence(
            observation_id=observation_id, status="failed", reason_code="run_input_query_failed"
        )
    except (ValueError, RuntimeError):
        return FinancialInputEvidence(
            observation_id=observation_id,
            status="semantic_gap",
            reason_code="run_input_evidence_invalid",
        )


def _assess(conn: sqlite3.Connection, ticker: str, cutoff: datetime) -> ValuationReadiness:
    evidence = load_dcf_grade_evidence(conn, ticker)
    if evidence.status != "available":
        return ValuationReadiness(
            ticker=ticker,
            evaluated_at=cutoff.isoformat(),
            status="missing" if evidence.status == "missing" else "failed",
            reason_codes=(
                ("dcf_missing",)
                if evidence.status == "missing"
                else ("dcf_evidence_invalid", evidence.invalid_reason or "dcf_schema_unavailable")
            ),
        )
    reasons: list[str] = []
    market_at = _clock(evidence.live_price_at)
    market_status: Literal["current", "stale", "missing", "invalid"] = "current"
    if evidence.live_price is None or evidence.live_price_at is None:
        market_status = "missing"
        reasons.append("market_price_or_timestamp_missing")
    elif market_at is None:
        market_status = "invalid"
        reasons.append("market_timestamp_invalid")
    elif market_at > cutoff:
        market_status = "invalid"
        reasons.append("market_timestamp_after_cutoff")
    elif cutoff - market_at > timedelta(days=PRICE_STALE_DAYS):
        market_status = "stale"
        reasons.append("market_price_stale")
    if evidence.projection_status == "bounded":
        reasons.append("dcf_evidence_projection_incomplete")
    if evidence.npv_per_share is None or evidence.npv_per_share <= 0:
        reasons.append("dcf_value_unavailable")
    if evidence.sanity_flag:
        reasons.append("dcf_sanity_flag")
    if evidence.checks is None or not evidence.checks.input_hash_valid:
        reasons.append("dcf_input_hash_unverified")
    if (
        evidence.valuation_date is not None
        and date.fromisoformat(evidence.valuation_date) > cutoff.date()
    ):
        reasons.append("model_valuation_after_cutoff")
    calculated_at = _clock(evidence.created_at)
    if calculated_at is None:
        reasons.append("model_calculation_timestamp_unverified")
    elif calculated_at > cutoff:
        reasons.append("model_calculation_after_cutoff")
    references = _fact_references(evidence)
    canonical_inputs: dict[str, GrowthFactReference] = {}
    if references:
        history = read_financial_history(
            conn,
            ticker,
            as_of=cutoff.date(),
            concepts=(
                "revenue",
                "gross_profit",
                "operating_income",
                "free_cash_flow",
                "net_income",
            ),
        )
        canonical_inputs = {item.observation_id: item for item in history.references}
        reasons.extend(history.reason_codes)
    reader = FactReadModel(conn)
    resolver = CanonicalFactResolutionEngine(conn)
    ontology = MetricOntology(conn)
    inputs = tuple(
        _admit_input(reader, resolver, ontology, item, cutoff, canonical_inputs)
        for item in references
    )
    if not inputs:
        reasons.append("financial_input_lineage_missing")
    reasons.extend(item.reason_code for item in inputs if item.reason_code is not None)
    # No existing DCF receipt proves the required population or latest issuer
    # filing coverage. Successful admission of a subset must not fill that gap.
    reasons.extend(
        (
            "financial_input_completeness_unverified",
            "latest_reporting_period_unverified",
        )
    )
    sources = (evidence.provenance or {}).get("sources")
    source_clocks: list[dict[str, object]] = []
    for raw_source in _items(sources):
        source = _mapping(raw_source)
        if source:
            source_clocks.append(
                {
                    key: source.get(key)
                    for key in ("role", "observed_at", "clock_kind", "influences_calculation")
                }
            )
    periods = [item.period_end for item in inputs if item.status == "admitted" and item.period_end]
    return ValuationReadiness(
        ticker=ticker,
        evaluated_at=cutoff.isoformat(),
        status="failed" if any(item.status == "failed" for item in inputs) else "degraded",
        reason_codes=tuple(dict.fromkeys(reasons)),
        run_id=evidence.run_id,
        input_sha256=evidence.input_sha256,
        valuation_date=evidence.valuation_date,
        model_calculated_at=evidence.created_at,
        legacy_input_cutoff=evidence.inputs_as_of,
        market_observed_at=evidence.live_price_at,
        market_status=market_status,
        financial_period_end=max(periods, default=None),
        financial_inputs=inputs,
        source_clocks=tuple(source_clocks),
    )


def load_valuation_readiness(
    conn: sqlite3.Connection, ticker: str, *, as_of: datetime
) -> ValuationReadiness:
    """Assess persisted evidence under one read snapshot; no writes/network/fallback.

    Current receipts cannot certify financial-input population completeness, so
    they are explicitly degraded rather than being made eligible by fresh quotes.
    This API is for consumption of an existing run, not permission to build its
    replacement. It does not erase or mutate the stored valuation.
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must have a timezone")
    cutoff = as_of.astimezone(UTC)
    ticker = ticker.upper()
    original_factory = conn.row_factory
    owns_snapshot = False
    try:
        owns_snapshot = not conn.in_transaction
        if owns_snapshot:
            conn.execute("BEGIN")
        return _assess(conn, ticker, cutoff)
    except sqlite3.Error:
        return ValuationReadiness(
            ticker=ticker,
            evaluated_at=cutoff.isoformat(),
            status="failed",
            reason_codes=("valuation_evidence_query_failed",),
        )
    finally:
        conn.row_factory = original_factory
        if owns_snapshot and conn.in_transaction:
            conn.rollback()
