"""Validate freshly regenerated native caches and project them into the artifact store.

The report pipeline predates ``llm_artifacts`` for a handful of sections and
still writes typed JSON sidecars.  The dirty-artifact drain uses this module as
the explicit bridge: a sidecar must be newer than the queue observation, pass
its purpose-specific schema, and then be atomically inserted/superseded under
that exact purpose.  A missing, stale, or malformed sidecar never clears work.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from llm_artifact_store import UpsertRequest, upsert
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


class NativeArtifactProjectionError(RuntimeError):
    """A native cache cannot safely satisfy its queued artifact obligation."""


class _FailureMode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hypothesis: str = Field(min_length=1)
    evidence_in_data: str = Field(min_length=1)
    leading_indicator: str = Field(min_length=1)
    quantitative_impact: str = Field(min_length=1)
    refutation_criteria: str = Field(min_length=1)


class _BearCaseCache(BaseModel):
    model_config = ConfigDict(extra="forbid")

    failure_modes: list[_FailureMode] = Field(min_length=1)
    most_underweighted: str | None = None
    out_of_scope_flags: list[str] = Field(default_factory=list[str])


class _ValuationHistoryPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period_end: str
    value: float | None


class _ValuationBasisCache(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str
    multiple_name: str = Field(min_length=1)
    rationale: str | None = None
    target_band: str | None = None
    notes: str | None = None
    current_value: float | None = None
    current_value_display: str | None = None
    current_period_end: str | None = None
    history: list[_ValuationHistoryPoint]
    historical_min: float | None = None
    historical_max: float | None = None
    historical_median: float | None = None
    rich_cheap_verdict: str | None = None
    peg_ratio: float | None = None
    peg_growth_pct: float | None = None
    cache_sha256: str = Field(min_length=1)
    extracted_at: str = Field(min_length=1)
    model: str | None = None
    skipped_reason: None = None


class _QATopic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    tag: str = Field(min_length=1)


class _QATopicsCache(BaseModel):
    model_config = ConfigDict(extra="forbid")

    by_key: dict[str, list[_QATopic]]

    @model_validator(mode="after")
    def _require_at_least_one_generated_topic(self) -> _QATopicsCache:
        if not self.by_key or not any(items for items in self.by_key.values()):
            raise ValueError("qa_topics cache contains no generated topics")
        return self


class _SayDoFilterCache(BaseModel):
    model_config = ConfigDict(extra="forbid")

    by_key: dict[str, list[str]]

    @model_validator(mode="after")
    def _require_at_least_one_selected_commitment(self) -> _SayDoFilterCache:
        if (
            not self.by_key
            or any(not key for key in self.by_key)
            or not any(items for items in self.by_key.values())
            or any(not item for items in self.by_key.values() for item in items)
        ):
            raise ValueError("saydo_filter cache contains no selected commitments")
        return self


class _NamedDescription(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    description: str | None = None


class _CompanyDescriptionCache(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str
    fiscal_year: int | None = None
    source_path: str | None = None
    source_sha256: str = Field(min_length=1)
    extracted_at_start: str | None = None
    extracted_at_end: str = Field(min_length=1)
    elapsed_ms: int = 0
    model: str = Field(min_length=1)
    sector: str | None = None
    industry: str | None = None
    segment_names_requested: list[str] = Field(default_factory=list[str])
    geo_names_requested: list[str] = Field(default_factory=list[str])
    elevator_pitch: str | None = None
    business_overview: str | None = None
    revenue_model: str | None = None
    segments: list[_NamedDescription] = Field(default_factory=list[_NamedDescription])
    geographies: list[_NamedDescription] = Field(default_factory=list[_NamedDescription])
    skipped_reason: None = None

    @model_validator(mode="after")
    def _require_generated_description(self) -> _CompanyDescriptionCache:
        if not any(
            (
                self.elevator_pitch,
                self.business_overview,
                self.revenue_model,
                self.segments,
                self.geographies,
            )
        ):
            raise ValueError("company_description cache contains no generated description")
        return self


class _ChangeSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    has_changes: bool
    description: str | None = None


class _ExecutiveCompSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metrics_used: list[str] = Field(default_factory=list[str])
    targets_and_thresholds: str | None = None
    alignment_verdict: str | None = None


class _InvestmentSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signal_type: str
    severity: Literal["High", "Medium", "Low"]
    description: str


class _FilingSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str
    fiscal_year: int
    analyzed_at: str
    segment_changes: _ChangeSummary
    metric_redefinitions: _ChangeSummary
    executive_comp: _ExecutiveCompSummary
    investment_signals: list[_InvestmentSignal] = Field(default_factory=list[_InvestmentSignal])
    raw_synthesis_md: str = Field(min_length=1)


class _FilingIntelligenceCache(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str
    fiscal_year: int | None = None
    source_path: str | None = None
    source_sha256: str = Field(min_length=1)
    analyzed_at: str = Field(min_length=1)
    elapsed_ms: int = 0
    model: str = Field(min_length=1)
    summary: _FilingSummary
    skipped_reason: None = None


_NativePayload = Annotated[
    _BearCaseCache
    | _ValuationBasisCache
    | _QATopicsCache
    | _SayDoFilterCache
    | _CompanyDescriptionCache
    | _FilingIntelligenceCache,
    Field(union_mode="left_to_right"),
]


class _ProjectionSpec(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    directory: str
    adapter: TypeAdapter[_NativePayload]


_PROJECTION_SPECS: dict[str, _ProjectionSpec] = {
    "bear_case": _ProjectionSpec(
        directory="bear_case",
        adapter=TypeAdapter(_BearCaseCache),
    ),
    "valuation_basis": _ProjectionSpec(
        directory="valuation_basis",
        adapter=TypeAdapter(_ValuationBasisCache),
    ),
    "qa_topics": _ProjectionSpec(
        directory="qa_topics",
        adapter=TypeAdapter(_QATopicsCache),
    ),
    "saydo_filter": _ProjectionSpec(
        directory="saydo_filter",
        adapter=TypeAdapter(_SayDoFilterCache),
    ),
    "company_description": _ProjectionSpec(
        directory="company_description",
        adapter=TypeAdapter(_CompanyDescriptionCache),
    ),
    "filing_intelligence": _ProjectionSpec(
        directory="filing_intelligence",
        adapter=TypeAdapter(_FilingIntelligenceCache),
    ),
}

PROJECTABLE_NATIVE_PURPOSES: frozenset[str] = frozenset(_PROJECTION_SPECS)


def project_native_artifact(
    *,
    ticker: str,
    purpose: str,
    repo_root: Path,
    db_path: Path,
    queued_at: datetime,
    scope: str = "ticker",
    fiscal_period: str | None = None,
    obligation_ids: tuple[int, ...] = (),
) -> int:
    """Validate and atomically supersede the exact queued purpose.

    ``queued_at`` is captured before the child process starts.  This prevents
    a successful child that merely reuses an old sidecar from being treated as
    queue progress.
    """
    spec = _PROJECTION_SPECS.get(purpose)
    if spec is None:
        raise NativeArtifactProjectionError(f"purpose {purpose!r} has no native projection")
    normalized_ticker = ticker.upper()
    cache_path = repo_root / "data" / spec.directory / f"{normalized_ticker}.json"
    try:
        stat = cache_path.stat()
        raw = cache_path.read_bytes()
    except OSError as exc:
        raise NativeArtifactProjectionError(
            f"{purpose} native cache is unavailable: {type(exc).__name__}: {exc}"
        ) from exc

    effective_queued_at = (
        queued_at if queued_at.tzinfo is not None else queued_at.replace(tzinfo=UTC)
    )
    cache_written_at = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
    if cache_written_at <= effective_queued_at:
        raise NativeArtifactProjectionError(
            f"{purpose} native cache was not refreshed after queue time"
        )

    try:
        decoded: object = json.loads(raw)
        validated = spec.adapter.validate_python(decoded)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise NativeArtifactProjectionError(
            f"{purpose} native cache schema validation failed: {exc}"
        ) from exc

    payload = validated.model_dump(mode="json")
    payload_ticker = payload.get("ticker")
    if isinstance(payload_ticker, str) and payload_ticker.upper() != normalized_ticker:
        raise NativeArtifactProjectionError(
            f"{purpose} native cache ticker {payload_ticker!r} does not match {normalized_ticker!r}"
        )
    raw_model = payload.get("model")
    model = raw_model if isinstance(raw_model, str) else None
    artifact_id, _cache_hit = upsert(
        UpsertRequest(
            ticker=normalized_ticker,
            purpose=purpose,
            scope=scope,
            fiscal_period=fiscal_period,
            content_json=payload,
            model=model,
            prompt_version="native-cache-projection-v1",
            cache_inputs=[raw],
            supersede_artifact_ids=list(obligation_ids),
            force_new_version=True,
        ),
        db_path=db_path,
    )
    if artifact_id is None:
        raise NativeArtifactProjectionError(
            f"{purpose} native cache could not be persisted to llm_artifacts"
        )
    _verify_projection_readback(
        db_path=db_path,
        artifact_id=artifact_id,
        obligation_ids=obligation_ids,
        ticker=normalized_ticker,
        purpose=purpose,
        scope=scope,
        fiscal_period=fiscal_period,
        queued_at=effective_queued_at,
    )
    return artifact_id


def _verify_projection_readback(
    *,
    db_path: Path,
    artifact_id: int,
    obligation_ids: tuple[int, ...],
    ticker: str,
    purpose: str,
    scope: str,
    fiscal_period: str | None,
    queued_at: datetime,
) -> None:
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
        conn.row_factory = sqlite3.Row
        try:
            successor = conn.execute(
                """
                SELECT ticker, scope, purpose, fiscal_period, generated_at,
                       expires_at, superseded_by_id, dirty
                FROM llm_artifacts WHERE id = ?
                """,
                (artifact_id,),
            ).fetchone()
            predecessors: dict[int, object] = {}
            for obligation_id in obligation_ids:
                row = conn.execute(
                    "SELECT id, superseded_by_id FROM llm_artifacts WHERE id = ?",
                    (obligation_id,),
                ).fetchone()
                if row is not None:
                    predecessors[int(row["id"])] = row["superseded_by_id"]
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as exc:
        raise NativeArtifactProjectionError(
            f"{purpose} projection readback failed: {type(exc).__name__}: {exc}"
        ) from exc

    if successor is None:
        raise NativeArtifactProjectionError(f"{purpose} projection successor is missing")
    successor_period = successor["fiscal_period"]
    if (
        str(successor["ticker"]).upper() != ticker
        or str(successor["scope"]) != scope
        or str(successor["purpose"]) != purpose
        or successor_period != fiscal_period
        or successor["superseded_by_id"] is not None
        or bool(successor["dirty"])
    ):
        raise NativeArtifactProjectionError(f"{purpose} projection successor is not exact/current")
    try:
        generated_at = datetime.fromisoformat(str(successor["generated_at"]))
    except ValueError as exc:
        raise NativeArtifactProjectionError(
            f"{purpose} projection successor has an invalid generated_at"
        ) from exc
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=UTC)
    if generated_at <= queued_at:
        raise NativeArtifactProjectionError(
            f"{purpose} projection successor was not generated after queue time"
        )
    if successor["expires_at"] is None:
        raise NativeArtifactProjectionError(f"{purpose} projection successor has no expiry")
    try:
        expires_at = datetime.fromisoformat(str(successor["expires_at"]))
    except ValueError as exc:
        raise NativeArtifactProjectionError(
            f"{purpose} projection successor has an invalid expiry"
        ) from exc
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= datetime.now(UTC):
        raise NativeArtifactProjectionError(f"{purpose} projection successor is already expired")
    unresolved = [
        obligation_id
        for obligation_id in obligation_ids
        if predecessors.get(obligation_id) != artifact_id
    ]
    if unresolved:
        raise NativeArtifactProjectionError(
            f"{purpose} projection did not supersede obligations {unresolved}"
        )
