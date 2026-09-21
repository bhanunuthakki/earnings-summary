"""Resolve a prediction target to one admitted KPI observation, without guessing."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from compute.kpi_resolver import normalize_kpi_name
from pipeline.kpi_definition_revisions import (
    IssuerKpiDefinitionRevision,
    kpi_definition_revision_by_id,
)
from timeseries.kpi_revision_shadow import (
    KpiRevisionReadRequest,
    RevisionSourcedKpiPoint,
    read_revision_kpi_points,
)


@dataclass(frozen=True)
class PredictionEvidence:
    reason: str
    point: RevisionSourcedKpiPoint | None = None
    definition_name: str | None = None
    currency: str | None = None
    definition: IssuerKpiDefinitionRevision | None = None


def prediction_evidence(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    kpi_name: str,
    target_period: datetime,
    target_unit: str | None,
    as_of: datetime,
    window_days: int,
    kpi_concept_id: int | None = None,
) -> PredictionEvidence:
    """Preserve the existing date window; ambiguous identities and units stay pending.

    A currency code target means unscaled currency. A scale without a currency
    cannot safely identify a monetary target. No implicit conversion is applied.
    The caller owns the read snapshot across resolution and manifest creation.
    """
    requested_key = normalize_kpi_name(kpi_name)
    # Names are a lookup hint, not identity. Richness or exact spelling cannot
    # eliminate another normalized candidate before immutable admission.
    identities = [
        row
        for row in conn.execute(
            "SELECT id,name FROM kpi_definitions WHERE ticker=? ORDER BY id",
            (ticker.upper(),),
        ).fetchall()
        if normalize_kpi_name(str(row[1])) == requested_key
    ]
    if not identities:
        return PredictionEvidence("no_kpi")
    if len(identities) != 1:
        return PredictionEvidence("ambiguous_definition")
    definition_name = str(identities[0][1])
    read = read_revision_kpi_points(
        conn,
        request=KpiRevisionReadRequest(
            ticker=ticker,
            kpi_definition_id=int(identities[0][0]),
            effective_at=as_of,
            known_at=as_of,
            period_types=("Q1", "Q2", "Q3", "Q4", "FY", "TTM"),
        ),
    )
    if read.blocking_reasons:
        return PredictionEvidence(";".join(read.blocking_reasons))
    candidates = [
        point
        for point in read.points
        if abs((point.period_end.date() - target_period.date()).days) <= window_days
    ]
    if not candidates:
        return PredictionEvidence("no_fact")
    distance = min(
        abs((point.period_end.date() - target_period.date()).days) for point in candidates
    )
    nearest = [
        point
        for point in candidates
        if abs((point.period_end.date() - target_period.date()).days) == distance
    ]
    if len(nearest) != 1:
        return PredictionEvidence("ambiguous_period")
    point = nearest[0]
    if kpi_concept_id is not None:
        # The optional mutable tag is not an immutable definition-to-concept
        # binding. Do not query it or substitute equality of unrelated identity
        # namespaces for the required immutable binding.
        return PredictionEvidence("concept_binding_unavailable")
    definition = kpi_definition_revision_by_id(
        conn, kpi_definition_revision_id=point.definition_revision_id
    )
    if definition is None:
        return PredictionEvidence("definition_revision_unavailable")
    unit = (target_unit or "").strip().lower()
    currency = None if definition.currency is None else definition.currency.value
    if currency is not None:
        if unit != currency.lower() or point.unit != "actual":
            return PredictionEvidence("target_currency_or_scale_unavailable")
    elif not unit or unit != point.unit:
        return PredictionEvidence("target_unit_mismatch")
    return PredictionEvidence("resolved", point, definition_name, currency, definition)
