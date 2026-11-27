"""Shared KPI comparability read loaders, ``as_reported`` series, and ``cumulative_diff`` reader."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from models.facts import Currency, FiscalPeriodType, Unit
from pipeline.kpi_comparability_bridges import (
    AdditiveCompositeBridge,
    CumulativeDiffInput,
    DerivedCumulativeDiffObservation,
    KpiPeriodCadenceKind,
    classify_period_cadence,
    derive_standalone_from_cumulative,
)
from pipeline.kpi_definition_revisions import (
    IssuerKpiDefinitionRevision,
    KpiDefinitionComparabilityDisposition,
    KpiDefinitionComparabilityRevision,
    KpiDefinitionLifecycle,
    KpiDefinitionStatus,
    kpi_definition_comparability_as_known,
    kpi_definition_revision_as_known,
    kpi_definition_revision_by_id,
)
from provenance.financial_fact_resolution import (
    HistoricalFactAuthorityUnavailableError,
    canonical_fact_relation,
    canonical_fact_row_ids_as_known,
)


class KpiSeriesReadStatus(StrEnum):
    ELIGIBLE = "eligible"
    ELIGIBLE_WITH_BREAK = "eligible_with_break"
    LEGACY_UNBOUND = "legacy_unbound"
    QUARANTINED_DEFINITION = "quarantined_definition"
    DISCONTINUED = "discontinued"
    HISTORICAL_FACT_AUTHORITY_UNAVAILABLE = "historical_fact_authority_unavailable"


class KpiAsReportedObservation(BaseModel):
    """One untouched historical KPI observation with exact revision metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_id: int = Field(gt=0)
    ticker: str = Field(min_length=1, max_length=32)
    kpi_definition_id: int = Field(gt=0)
    kpi_definition_revision_id: str = Field(min_length=1, max_length=128)
    period_end: str = Field(min_length=10, max_length=10)
    fiscal_period_type: str | None = None
    cadence_kind: KpiPeriodCadenceKind
    period_role: str | None = None
    publication_lane: str | None = None
    value: Decimal
    unit: Unit
    currency: Currency | None = None
    reported_label: str = Field(min_length=1)
    accounting_basis: str = Field(min_length=1)
    consolidation_scope: str = Field(min_length=1)
    dimensions: dict[str, str] = Field(default_factory=dict)
    unit_scale: str = Field(min_length=1)
    stock_flow_behavior: str = Field(min_length=1)
    source_doc_id: int | None = None
    locator: str | None = None


class KpiAsReportedSeries(BaseModel):
    """Raw untouched observation series across an anchor definition and its admitted lineage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kpi_definition_id: int = Field(gt=0)
    status: KpiSeriesReadStatus
    anchor_definition_revision_id: str | None = None
    included_definition_revision_ids: tuple[str, ...] = ()
    observations: tuple[KpiAsReportedObservation, ...] = ()
    excluded_fact_ids: tuple[int, ...] = ()


def _table_columns(conn: sqlite3.Connection, table: str) -> frozenset[str]:
    return frozenset(str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})"))


def iso_date_str(value: date | datetime | str) -> str:
    """Normalize a date, datetime, or ISO timestamp to ``YYYY-MM-DD``."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()[:10]


def aware_utc(value: datetime, field_name: str) -> datetime:
    """Require a timezone-aware datetime and normalize it to UTC."""
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def revision_visible(
    definition: IssuerKpiDefinitionRevision | None,
    *,
    effective_at: datetime,
    known_at: datetime,
) -> IssuerKpiDefinitionRevision | None:
    """Return ``definition`` only when admitted, active, and within both cutoffs."""
    if (
        definition is None
        or definition.status is not KpiDefinitionStatus.ADMITTED
        or definition.lifecycle is not KpiDefinitionLifecycle.ACTIVE
        or definition.effective_at > effective_at
        or definition.knowledge_at > known_at
        or definition.recorded_at > known_at
    ):
        return None
    return definition


def collect_related_graph(
    conn: sqlite3.Connection,
    *,
    anchor: IssuerKpiDefinitionRevision,
    effective_at: datetime,
    known_at: datetime,
) -> tuple[dict[str, IssuerKpiDefinitionRevision], tuple[KpiDefinitionComparabilityRevision, ...]]:
    """Collect the anchor, directly/continuously linked revisions, and their active edges."""
    revisions: dict[str, IssuerKpiDefinitionRevision] = {anchor.kpi_definition_revision_id: anchor}
    edges_by_pair: dict[tuple[str, str], KpiDefinitionComparabilityRevision] = {}
    frontier: list[str] = [anchor.kpi_definition_revision_id]
    visited: set[str] = set()

    while frontier:
        current_id = frontier.pop()
        if current_id in visited:
            continue
        visited.add(current_id)
        pair_rows = conn.execute(
            "SELECT DISTINCT predecessor_definition_revision_id,successor_definition_revision_id "
            "FROM kpi_definition_comparability_revisions "
            "WHERE predecessor_definition_revision_id=? OR successor_definition_revision_id=?",
            (current_id, current_id),
        ).fetchall()
        for pair in pair_rows:
            pred_id, succ_id = str(pair[0]), str(pair[1])
            pair_key = (pred_id, succ_id)
            if pair_key in edges_by_pair:
                continue
            relation = kpi_definition_comparability_as_known(
                conn,
                first_definition_revision_id=pred_id,
                second_definition_revision_id=succ_id,
                effective_at=effective_at,
                known_at=known_at,
            )
            if relation is None:
                continue
            other_id = succ_id if pred_id == current_id else pred_id
            other = revision_visible(
                kpi_definition_revision_by_id(conn, kpi_definition_revision_id=other_id),
                effective_at=effective_at,
                known_at=known_at,
            )
            if other is None:
                continue
            edges_by_pair[pair_key] = relation
            revisions[other_id] = other
            if (
                current_id == anchor.kpi_definition_revision_id
                or relation.disposition is KpiDefinitionComparabilityDisposition.CONTINUOUS
            ) and other_id not in visited:
                frontier.append(other_id)
            if isinstance(relation.bridge_spec, AdditiveCompositeBridge):
                for member_id in (
                    relation.bridge_spec.composite_definition_revision_id,
                    *relation.bridge_spec.member_definition_revision_ids,
                ):
                    if member_id not in revisions:
                        member = revision_visible(
                            kpi_definition_revision_by_id(
                                conn, kpi_definition_revision_id=member_id
                            ),
                            effective_at=effective_at,
                            known_at=known_at,
                        )
                        if member is not None:
                            revisions[member_id] = member

    edges = tuple(
        sorted(
            edges_by_pair.values(),
            key=lambda item: (
                item.effective_at,
                item.predecessor_definition_revision_id,
                item.successor_definition_revision_id,
            ),
        )
    )
    return revisions, edges


def load_admitted_observations(
    conn: sqlite3.Connection,
    *,
    revisions: dict[str, IssuerKpiDefinitionRevision],
    extra_roots: Sequence[int],
    effective_at: datetime,
    known_at: datetime,
) -> tuple[list[KpiAsReportedObservation], tuple[int, ...]]:
    """Load canonical admitted observations bound to any revision in ``revisions``."""
    roots = sorted({rev.kpi_definition_id for rev in revisions.values()} | set(extra_roots))
    if not roots:
        return [], ()
    canonical_fact_ids = canonical_fact_row_ids_as_known(
        conn,
        fact_table="kpi_facts",
        effective_at=effective_at,
        known_at=known_at,
        concept_keys=tuple(f"kpi_definition:{root_id}" for root_id in roots),
    )
    if not canonical_fact_ids:
        return [], ()

    ctx_cols = _table_columns(conn, "kpi_fact_semantic_contexts")
    fact_cols = _table_columns(conn, "kpi_facts")
    period_role_expr = "context.period_role" if "period_role" in ctx_cols else "NULL"
    pub_lane_expr = "context.publication_lane" if "publication_lane" in ctx_cols else "NULL"
    locator_expr = "fact.locator" if "locator" in fact_cols else "NULL"
    fpt_expr = "fact.fiscal_period_type" if "fiscal_period_type" in fact_cols else "NULL"

    fact_relation = canonical_fact_relation(conn, "kpi_facts").sql
    fact_marks = ",".join("?" for _ in canonical_fact_ids)
    root_marks = ",".join("?" for _ in roots)
    rows = conn.execute(
        "SELECT fact.id, UPPER(fact.ticker), fact.kpi_definition_id, fact.period_end, "  # nosec B608
        f"{fpt_expr}, fact.value, fact.unit, fact.currency, fact.source_doc_id, {locator_expr}, "
        "context.status, context.metric_name_as_reported, context.reported_period_end, "
        "context.accounting_basis, context.consolidation_scope, context.dimensions_json, "
        f"context.unit_scale, context.kpi_definition_revision_id, {period_role_expr}, {pub_lane_expr} "
        f"FROM {fact_relation} fact "
        "LEFT JOIN kpi_fact_semantic_contexts context ON context.kpi_fact_id = fact.id "
        "AND datetime(context.knowledge_at) <= datetime(?) "
        "AND datetime(context.created_at) <= datetime(?) "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM kpi_fact_semantic_contexts nxt "
        "  WHERE nxt.supersedes_context_id = context.id "
        "  AND datetime(nxt.knowledge_at) <= datetime(?) "
        "  AND datetime(nxt.created_at) <= datetime(?)"
        ") "
        f"WHERE fact.id IN ({fact_marks}) "
        "AND datetime(fact.period_end) <= datetime(?) "
        f"AND fact.kpi_definition_id IN ({root_marks}) "
        "ORDER BY date(fact.period_end) ASC, fact.id ASC",
        (
            known_at.isoformat(),
            known_at.isoformat(),
            known_at.isoformat(),
            known_at.isoformat(),
            *canonical_fact_ids,
            effective_at.isoformat(),
            *roots,
        ),
    ).fetchall()

    observations: list[KpiAsReportedObservation] = []
    excluded: list[int] = []
    for row in rows:
        fact_id = int(row[0])
        ctx_status = row[10]
        rev_id = None if row[17] is None else str(row[17])
        if ctx_status != "admitted" or rev_id is None or rev_id not in revisions:
            excluded.append(fact_id)
            continue
        definition = revisions[rev_id]
        fact_period = str(row[3])[:10]
        ctx_period = "" if row[12] is None else str(row[12])[:10]
        fact_unit = str(row[6])
        fact_currency = None if row[7] is None else str(row[7])
        raw_dims_obj: object = json.loads(str(row[15]))
        dims = (
            {str(k): str(v) for k, v in cast("dict[object, object]", raw_dims_obj).items()}
            if isinstance(raw_dims_obj, dict)
            else {}
        )
        expected_currency_disp = "explicit" if fact_currency is not None else "not_applicable"
        def_currency = None if definition.currency is None else definition.currency.value
        matches = (
            definition.kpi_definition_id == int(row[2])
            and ctx_period == fact_period
            and definition.reported_label == str(row[11])
            and definition.accounting_basis.value == str(row[13])
            and definition.consolidation_scope.value == str(row[14])
            and definition.dimensions == dims
            and definition.unit_scale.value == str(row[16])
            and str(definition.unit_key) == fact_unit
            and def_currency == fact_currency
            and definition.currency_disposition.value == expected_currency_disp
        )
        if not matches:
            excluded.append(fact_id)
            continue
        raw_fpt = None if row[4] is None else str(row[4])
        observations.append(
            KpiAsReportedObservation(
                fact_id=fact_id,
                ticker=str(row[1]),
                kpi_definition_id=int(row[2]),
                kpi_definition_revision_id=rev_id,
                period_end=fact_period,
                fiscal_period_type=raw_fpt,
                cadence_kind=classify_period_cadence(raw_fpt),
                period_role=None if row[18] is None else str(row[18]),
                publication_lane=None if row[19] is None else str(row[19]),
                value=Decimal(str(row[5])),
                unit=Unit(fact_unit),
                currency=None if fact_currency is None else Currency(fact_currency),
                reported_label=str(row[11]),
                accounting_basis=str(row[13]),
                consolidation_scope=str(row[14]),
                dimensions=dims,
                unit_scale=str(row[16]),
                stock_flow_behavior=definition.stock_flow_behavior.value,
                source_doc_id=None if row[8] is None else int(row[8]),
                locator=None if row[9] is None else str(row[9]),
            )
        )
    return observations, tuple(sorted(set(excluded)))


def anchor_status_check(
    conn: sqlite3.Connection,
    *,
    kpi_definition_id: int,
    effective_at: datetime,
    known_at: datetime,
) -> tuple[KpiSeriesReadStatus | None, IssuerKpiDefinitionRevision | None]:
    """Return an early blocking status when the anchor revision cannot be read."""
    if not _table_columns(conn, "kpi_definition_revisions"):
        return KpiSeriesReadStatus.LEGACY_UNBOUND, None
    anchor = kpi_definition_revision_as_known(
        conn,
        kpi_definition_id=kpi_definition_id,
        effective_at=effective_at,
        known_at=known_at,
    )
    if anchor is None:
        return KpiSeriesReadStatus.LEGACY_UNBOUND, None
    if anchor.status is KpiDefinitionStatus.QUARANTINED:
        return KpiSeriesReadStatus.QUARANTINED_DEFINITION, anchor
    if anchor.lifecycle is KpiDefinitionLifecycle.DISCONTINUED:
        return KpiSeriesReadStatus.DISCONTINUED, anchor
    return None, anchor


def select_observation(
    observations: Sequence[KpiAsReportedObservation],
    *,
    revision_ids: set[str],
    target_period: date,
    exact_date_only: bool,
    cadence_kind: KpiPeriodCadenceKind | None = None,
    fiscal_period_type: str | None = None,
    prefer_current_actual: bool = True,
) -> tuple[KpiAsReportedObservation | None, bool]:
    """Select one matching observation at ``target_period``, reporting cadence mismatches."""
    iso_target = target_period.isoformat()
    same_period: list[KpiAsReportedObservation] = []
    near_period: list[KpiAsReportedObservation] = []
    for obs in observations:
        if obs.kpi_definition_revision_id not in revision_ids:
            continue
        if obs.period_end == iso_target:
            same_period.append(obs)
        elif (
            not exact_date_only
            and abs((date.fromisoformat(obs.period_end) - target_period).days) <= 7
        ):
            near_period.append(obs)

    pool = same_period if same_period else near_period
    if not pool:
        return None, False

    cadence_matched = [
        obs
        for obs in pool
        if (cadence_kind is None or obs.cadence_kind is cadence_kind)
        and (
            fiscal_period_type is None
            or (obs.fiscal_period_type or "").upper() == fiscal_period_type.upper()
        )
    ]
    if not cadence_matched:
        return None, True

    cadence_matched.sort(
        key=lambda obs: (
            0 if (prefer_current_actual and obs.publication_lane != "comparator") else 1,
            -obs.fact_id,
        )
    )
    return cadence_matched[0], False


def resolve_kpi_as_reported(
    conn: sqlite3.Connection,
    *,
    kpi_definition_id: int,
    effective_at: datetime,
    known_at: datetime,
    cadence_kind: KpiPeriodCadenceKind | None = None,
    include_comparators: bool = False,
    include_related_revisions: bool = True,
) -> KpiAsReportedSeries:
    """Return untouched historical KPI observations with their original revision metadata."""
    eff = aware_utc(effective_at, "effective_at")
    known = aware_utc(known_at, "known_at")
    owns_snapshot = not conn.in_transaction
    if owns_snapshot:
        conn.execute("BEGIN")
    try:
        early_status, anchor = anchor_status_check(
            conn,
            kpi_definition_id=kpi_definition_id,
            effective_at=eff,
            known_at=known,
        )
        if early_status is not None or anchor is None:
            return KpiAsReportedSeries(
                kpi_definition_id=kpi_definition_id,
                status=early_status or KpiSeriesReadStatus.LEGACY_UNBOUND,
                anchor_definition_revision_id=(
                    None if anchor is None else anchor.kpi_definition_revision_id
                ),
            )
        revisions, edges = collect_related_graph(
            conn, anchor=anchor, effective_at=eff, known_at=known
        )
        included_ids = {anchor.kpi_definition_revision_id}
        has_break = False
        if include_related_revisions:
            for edge in edges:
                if edge.disposition in {
                    KpiDefinitionComparabilityDisposition.CONTINUOUS,
                    KpiDefinitionComparabilityDisposition.COMPARABLE_WITH_BREAK,
                }:
                    if edge.predecessor_definition_revision_id in revisions:
                        included_ids.add(edge.predecessor_definition_revision_id)
                    if edge.successor_definition_revision_id in revisions:
                        included_ids.add(edge.successor_definition_revision_id)
                if edge.disposition is KpiDefinitionComparabilityDisposition.COMPARABLE_WITH_BREAK:
                    has_break = True
        scoped_revisions = {r_id: r for r_id, r in revisions.items() if r_id in included_ids}
        try:
            observations, excluded = load_admitted_observations(
                conn,
                revisions=scoped_revisions,
                extra_roots=(kpi_definition_id,),
                effective_at=eff,
                known_at=known,
            )
        except HistoricalFactAuthorityUnavailableError:
            return KpiAsReportedSeries(
                kpi_definition_id=kpi_definition_id,
                status=KpiSeriesReadStatus.HISTORICAL_FACT_AUTHORITY_UNAVAILABLE,
                anchor_definition_revision_id=anchor.kpi_definition_revision_id,
                included_definition_revision_ids=tuple(sorted(included_ids)),
            )
        filtered = [
            obs
            for obs in observations
            if (include_comparators or obs.publication_lane != "comparator")
            and (cadence_kind is None or obs.cadence_kind is cadence_kind)
        ]
        return KpiAsReportedSeries(
            kpi_definition_id=kpi_definition_id,
            status=(
                KpiSeriesReadStatus.ELIGIBLE_WITH_BREAK
                if has_break
                else KpiSeriesReadStatus.ELIGIBLE
            ),
            anchor_definition_revision_id=anchor.kpi_definition_revision_id,
            included_definition_revision_ids=tuple(sorted(included_ids)),
            observations=tuple(filtered),
            excluded_fact_ids=excluded,
        )
    finally:
        if owns_snapshot:
            conn.rollback()


def _to_diff_input(obs: KpiAsReportedObservation, fpt: FiscalPeriodType) -> CumulativeDiffInput:
    return CumulativeDiffInput(
        fact_id=obs.fact_id,
        ticker=obs.ticker,
        kpi_definition_id=obs.kpi_definition_id,
        kpi_definition_revision_id=obs.kpi_definition_revision_id,
        period_end=obs.period_end,
        fiscal_period_type=fpt,
        value=obs.value,
        unit=obs.unit,
        currency=obs.currency,
        accounting_basis=obs.accounting_basis,
        consolidation_scope=obs.consolidation_scope,
        dimensions=obs.dimensions,
        unit_scale=obs.unit_scale,
        stock_flow_behavior=obs.stock_flow_behavior,
        source_doc_id=obs.source_doc_id,
    )


def resolve_cumulative_diff_standalone(
    conn: sqlite3.Connection,
    *,
    kpi_definition_id: int,
    minuend_period_end: date | datetime | str,
    minuend_fiscal_period_type: FiscalPeriodType,
    subtrahend_period_end: date | datetime | str,
    subtrahend_fiscal_period_type: FiscalPeriodType,
    effective_at: datetime,
    known_at: datetime,
) -> DerivedCumulativeDiffObservation:
    """Resolve two admitted same-revision DB observations and derive their standalone diff."""
    eff = aware_utc(effective_at, "effective_at")
    known = aware_utc(known_at, "known_at")
    minuend_date = date.fromisoformat(iso_date_str(minuend_period_end))
    subtrahend_date = date.fromisoformat(iso_date_str(subtrahend_period_end))
    owns_snapshot = not conn.in_transaction
    if owns_snapshot:
        conn.execute("BEGIN")
    try:
        early_status, anchor = anchor_status_check(
            conn, kpi_definition_id=kpi_definition_id, effective_at=eff, known_at=known
        )
        if early_status is not None or anchor is None:
            raise ValueError(
                f"cumulative_diff unavailable: anchor status is "
                f"{(early_status or KpiSeriesReadStatus.LEGACY_UNBOUND).value}"
            )
        revisions: dict[str, IssuerKpiDefinitionRevision] = {}
        same_root_rows = conn.execute(
            "SELECT kpi_definition_revision_id FROM kpi_definition_revisions "
            "WHERE kpi_definition_id=? AND datetime(effective_at)<=datetime(?) "
            "AND datetime(knowledge_at)<=datetime(?) AND datetime(recorded_at)<=datetime(?)",
            (kpi_definition_id, eff.isoformat(), known.isoformat(), known.isoformat()),
        ).fetchall()
        for row in same_root_rows:
            rev = revision_visible(
                kpi_definition_revision_by_id(conn, kpi_definition_revision_id=str(row[0])),
                effective_at=eff,
                known_at=known,
            )
            if rev is not None:
                revisions[rev.kpi_definition_revision_id] = rev
        observations, _ = load_admitted_observations(
            conn,
            revisions=revisions,
            extra_roots=(kpi_definition_id,),
            effective_at=eff,
            known_at=known,
        )
        all_rev_ids = set(revisions.keys())
        minuend_obs, _ = select_observation(
            observations,
            revision_ids=all_rev_ids,
            target_period=minuend_date,
            exact_date_only=True,
            cadence_kind=classify_period_cadence(minuend_fiscal_period_type),
            fiscal_period_type=minuend_fiscal_period_type.value,
            prefer_current_actual=True,
        )
        subtrahend_obs, _ = select_observation(
            observations,
            revision_ids=all_rev_ids,
            target_period=subtrahend_date,
            exact_date_only=True,
            cadence_kind=classify_period_cadence(subtrahend_fiscal_period_type),
            fiscal_period_type=subtrahend_fiscal_period_type.value,
            prefer_current_actual=True,
        )
        if minuend_obs is None or subtrahend_obs is None:
            raise ValueError(
                "cumulative_diff requires admitted minuend and subtrahend observations"
            )
        return derive_standalone_from_cumulative(
            _to_diff_input(minuend_obs, minuend_fiscal_period_type),
            _to_diff_input(subtrahend_obs, subtrahend_fiscal_period_type),
        )
    finally:
        if owns_snapshot:
            conn.rollback()
