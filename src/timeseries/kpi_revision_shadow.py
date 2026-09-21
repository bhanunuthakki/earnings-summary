"""Sourced KPI reader preparation; comparison never grants activation authority."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from compute.kpi_resolver import (
    KpiRevisionSeriesResolution,
    KpiRevisionSeriesStatus,
    resolve_revision_aware_kpi_series,
)
from compute.kpi_revision_shadow_census import SnapshotEvidenceState
from provenance.financial_fact_resolution import canonical_fact_selections_as_known


class KpiRevisionReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str = Field(min_length=1)
    kpi_definition_id: int = Field(gt=0)
    effective_at: datetime
    known_at: datetime
    period_types: tuple[str, ...] = ("Q1", "Q2", "Q3", "Q4")

    @model_validator(mode="after")
    def validate_cutoffs(self) -> Self:
        if self.effective_at.tzinfo is None or self.known_at.tzinfo is None:
            raise ValueError("reader cutoffs must be timezone-aware")
        if self.effective_at > self.known_at:
            raise ValueError("effective cutoff cannot follow knowledge cutoff")
        if not self.period_types or len(set(self.period_types)) != len(self.period_types):
            raise ValueError("period types must be nonempty and unique")
        return self


class RevisionSourcedKpiPoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    period_end: datetime
    fiscal_period_type: str
    value: Decimal = Field(allow_inf_nan=False)
    unit: str
    fact_id: int
    fact_revision: int
    observation_id: str
    resolution_id: str
    resolution_revision: int
    source_document_id: int
    locator_json: str | None
    definition_revision_id: str
    semantic_context_id: int
    semantic_context_revision: int


class RevisionKpiRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request: KpiRevisionReadRequest
    resolution: KpiRevisionSeriesResolution
    points: tuple[RevisionSourcedKpiPoint, ...] = ()
    blocking_reasons: tuple[str, ...] = ()


def read_revision_kpi_points(
    conn: sqlite3.Connection, *, request: KpiRevisionReadRequest
) -> RevisionKpiRead:
    """Project exact resolver selections in one caller-preserving read snapshot.

    Names never widen membership. The source locator is the immutable fact link,
    not a currently edited document label. Comparability breaks remain explicit.
    """
    old_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN")
    try:
        identity = conn.execute(
            "SELECT ticker FROM kpi_definitions WHERE id=?", (request.kpi_definition_id,)
        ).fetchone()
        if identity is None or str(identity[0]).upper() != request.ticker.upper():
            raise ValueError("definition identity does not belong to requested issuer")
        resolution = resolve_revision_aware_kpi_series(
            conn,
            kpi_definition_id=request.kpi_definition_id,
            effective_at=request.effective_at,
            known_at=request.known_at,
        )
        if resolution.status not in {
            KpiRevisionSeriesStatus.ELIGIBLE,
            KpiRevisionSeriesStatus.ELIGIBLE_WITH_BREAK,
        }:
            return RevisionKpiRead(
                request=request, resolution=resolution, blocking_reasons=(resolution.status.value,)
            )
        if not resolution.eligible_fact_ids:
            return RevisionKpiRead(
                request=request, resolution=resolution, blocking_reasons=("empty_admitted_series",)
            )
        # Root identities come from exact admitted fact IDs, including explicitly
        # comparable renamed definitions; they are never inferred from labels.
        roots: set[int] = set()
        for fact_id in resolution.eligible_fact_ids:
            fact = conn.execute(
                "SELECT ticker,kpi_definition_id FROM kpi_facts WHERE id=?", (fact_id,)
            ).fetchone()
            if fact is None or str(fact[0]).upper() != request.ticker.upper():
                raise ValueError("resolver selected a fact outside the requested issuer")
            roots.add(int(fact[1]))
        selections = {
            selection.fact_row_id: selection
            for selection in canonical_fact_selections_as_known(
                conn,
                fact_table="kpi_facts",
                effective_at=request.effective_at,
                known_at=request.known_at,
                concept_keys=tuple(f"kpi_definition:{root}" for root in sorted(roots)),
            )
        }
        points: list[RevisionSourcedKpiPoint] = []
        cutoff = request.known_at.isoformat()
        for fact_id in resolution.eligible_fact_ids:
            rows = conn.execute(
                "SELECT fact.period_end,fact.fiscal_period_type,fact.value,fact.unit,"
                "context.id,context.revision,context.kpi_definition_revision_id "
                "FROM kpi_facts fact JOIN kpi_fact_semantic_contexts context "
                "ON context.kpi_fact_id=fact.id WHERE fact.id=? "
                "AND datetime(context.knowledge_at)<=datetime(?) "
                "AND datetime(context.created_at)<=datetime(?) "
                "AND NOT EXISTS (SELECT 1 FROM kpi_fact_semantic_contexts successor "
                "WHERE successor.supersedes_context_id=context.id "
                "AND datetime(successor.knowledge_at)<=datetime(?) "
                "AND datetime(successor.created_at)<=datetime(?))",
                (fact_id, cutoff, cutoff, cutoff, cutoff),
            ).fetchall()
            if len(rows) != 1 or fact_id not in selections:
                raise ValueError("exact source or semantic head disappeared from reader snapshot")
            row = rows[0]
            if str(row[1]) not in request.period_types:
                continue
            selection = selections[fact_id]
            period = datetime.fromisoformat(str(row[0]))
            if period.tzinfo is None:
                period = period.replace(tzinfo=UTC)
            points.append(
                RevisionSourcedKpiPoint(
                    period_end=period,
                    fiscal_period_type=str(row[1]),
                    value=Decimal(str(row[2])),
                    unit=str(row[3]),
                    fact_id=fact_id,
                    fact_revision=selection.fact_revision,
                    observation_id=selection.observation_id,
                    resolution_id=selection.resolution_id,
                    resolution_revision=selection.resolution_revision,
                    source_document_id=selection.source_document_id,
                    locator_json=selection.locator_json,
                    definition_revision_id=str(row[6]),
                    semantic_context_id=int(row[4]),
                    semantic_context_revision=int(row[5]),
                )
            )
        blockers: set[str] = set()
        if resolution.breaks:
            blockers.add("comparability_break_requires_segmented_consumer")
        if not points:
            blockers.add("empty_requested_period_series")
        # Existing time-series values are keyed only by period end. Do not copy
        # its last-row-wins behavior across separate admitted definitions/periods.
        if len({point.period_end for point in points}) != len(points):
            blockers.add("ambiguous_period_requires_explicit_selection")
        return RevisionKpiRead(
            request=request,
            resolution=resolution,
            points=tuple(sorted(points, key=lambda point: (point.period_end, point.fact_id))),
            blocking_reasons=tuple(sorted(blockers)),
        )
    finally:
        if owns_transaction:
            conn.rollback()
        conn.row_factory = old_factory


class LegacyKpiReaderPoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    period_end: datetime
    value: float = Field(allow_inf_nan=False)
    unit: str | None
    fact_id: int
    source_document_id: int
    locator_json: str | None


class KpiReaderShadowComparison(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["kpi-reader-shadow-comparison/v1"] = "kpi-reader-shadow-comparison/v1"
    comparison_contract: Literal["legacy_current_sourced_vs_revision_as_known"] = (
        "legacy_current_sourced_vs_revision_as_known"
    )
    snapshot_evidence: SnapshotEvidenceState
    revision: RevisionKpiRead
    legacy_points: tuple[LegacyKpiReaderPoint, ...]
    differences: tuple[str, ...]
    scoped_value_source_parity: bool
    activation_state: Literal["hold"] = "hold"
    authorizes_reader_activation: Literal[False] = False
    verifier_code_sha256: str
    receipt_sha256: str

    @model_validator(mode="after")
    def verify_receipt(self) -> Self:
        if self.receipt_sha256 != comparison_hash(
            self.model_dump(mode="json", exclude={"receipt_sha256"})
        ):
            raise ValueError("reader comparison receipt hash mismatch")
        if self.scoped_value_source_parity != (not self.differences):
            raise ValueError("parity must match complete difference disposition")
        return self


def comparison_hash(payload: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
