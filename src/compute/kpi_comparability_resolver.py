"""Strict priority-ordered ``comparable_yoy`` KPI resolution across comparability bridges."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from compute.kpi_comparability_read import (
    KpiAsReportedObservation as KpiAsReportedObservation,
)
from compute.kpi_comparability_read import (
    KpiAsReportedSeries as KpiAsReportedSeries,
)
from compute.kpi_comparability_read import (
    KpiSeriesReadStatus as KpiSeriesReadStatus,
)
from compute.kpi_comparability_read import (
    anchor_status_check,
    aware_utc,
    collect_related_graph,
    iso_date_str,
    load_admitted_observations,
    revision_visible,
    select_observation,
)
from compute.kpi_comparability_read import (
    resolve_cumulative_diff_standalone as resolve_cumulative_diff_standalone,
)
from compute.kpi_comparability_read import (
    resolve_kpi_as_reported as resolve_kpi_as_reported,
)
from models.facts import Currency, FiscalPeriodType, Unit
from pipeline.kpi_comparability_bridges import (
    AdditiveCompositeBridge,
    KpiComparabilityBridgeSpec,
    KpiPeriodCadenceKind,
    RecastComparativeBridge,
    RenameOverlapBridge,
    SplitScaleBridge,
    classify_period_cadence,
)
from pipeline.kpi_definition_revisions import (
    IssuerKpiDefinitionRevision,
    KpiDefinitionComparabilityDisposition,
    KpiDefinitionComparabilityRevision,
    KpiDefinitionRelationKind,
    kpi_definition_revision_by_id,
)
from provenance.financial_fact_resolution import HistoricalFactAuthorityUnavailableError


class KpiComparableYoYStatus(StrEnum):
    COMPARABLE = "comparable"
    COMPARABLE_WITH_BRIDGE = "comparable_with_bridge"
    NOT_COMPARABLE = "not_comparable"
    RECAST_COMPARATIVE_MISSING = "recast_comparative_missing"
    COMPOSITE_MEMBER_MISSING = "composite_member_missing"
    PERIOD_KIND_MISMATCH = "period_kind_mismatch"
    NO_EXPLICIT_COMPARABILITY = "no_explicit_comparability"
    CURRENT_PERIOD_MISSING = "current_period_missing"
    PRIOR_PERIOD_MISSING = "prior_period_missing"
    LEGACY_UNBOUND = "legacy_unbound"
    QUARANTINED_DEFINITION = "quarantined_definition"
    DISCONTINUED = "discontinued"
    HISTORICAL_FACT_AUTHORITY_UNAVAILABLE = "historical_fact_authority_unavailable"


class KpiComparableYoYResult(BaseModel):
    """Strict period-over-period comparison with explicit bridge provenance or fail-closed status."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: KpiComparableYoYStatus
    kpi_definition_id: int = Field(gt=0)
    anchor_definition_revision_id: str | None = None
    current_period_end: str = Field(min_length=10, max_length=10)
    prior_period_end: str | None = None
    current_fiscal_period_type: str | None = None
    prior_fiscal_period_type: str | None = None
    cadence_kind: KpiPeriodCadenceKind = KpiPeriodCadenceKind.UNKNOWN
    current_fact_id: int | None = None
    current_component_fact_ids: tuple[int, ...] = ()
    current_definition_revision_id: str | None = None
    current_value: Decimal | None = None
    prior_definition_revision_ids: tuple[str, ...] = ()
    prior_value_raw: Decimal | None = None
    prior_value_adjusted: Decimal | None = None
    yoy_delta: Decimal | None = None
    yoy_pct: Decimal | None = None
    unit: Unit | None = None
    currency: Currency | None = None
    bridge_kind: str | None = None
    scale_factor: Decimal | None = None
    component_fact_ids: tuple[int, ...] = ()
    component_values: dict[str, Decimal] = Field(default_factory=dict)
    comparability_revision_ids: tuple[str, ...] = ()
    bridge_spec: KpiComparabilityBridgeSpec | None = None
    source_locators: tuple[dict[str, object], ...] = ()
    reason: str | None = None


def _default_prior_year_date(period_end: date) -> date:
    try:
        return period_end.replace(year=period_end.year - 1)
    except ValueError:
        return period_end.replace(year=period_end.year - 1, day=28)


def _yoy_deltas(current_value: Decimal, prior_value: Decimal) -> tuple[Decimal, Decimal | None]:
    delta = current_value - prior_value
    return (delta, None) if prior_value == Decimal("0") else (delta, delta / abs(prior_value))


def _yoy_fail(
    status: KpiComparableYoYStatus,
    *,
    kpi_definition_id: int,
    anchor_id: str | None,
    cur_iso: str,
    prior_iso: str | None,
    reason: str,
    current_obs: KpiAsReportedObservation | None = None,
    prior_fpt: str | None = None,
    prior_rev_ids: tuple[str, ...] = (),
    bridge_kind: str | None = None,
    comp_ids: tuple[str, ...] = (),
    bridge_spec: KpiComparabilityBridgeSpec | None = None,
    locators: tuple[dict[str, object], ...] = (),
) -> KpiComparableYoYResult:
    return KpiComparableYoYResult(
        status=status,
        kpi_definition_id=kpi_definition_id,
        anchor_definition_revision_id=anchor_id,
        current_period_end=cur_iso if current_obs is None else current_obs.period_end,
        prior_period_end=prior_iso,
        current_fiscal_period_type=None if current_obs is None else current_obs.fiscal_period_type,
        prior_fiscal_period_type=prior_fpt,
        cadence_kind=(
            KpiPeriodCadenceKind.UNKNOWN if current_obs is None else current_obs.cadence_kind
        ),
        current_fact_id=None if current_obs is None else current_obs.fact_id,
        current_component_fact_ids=() if current_obs is None else (current_obs.fact_id,),
        current_definition_revision_id=(
            None if current_obs is None else current_obs.kpi_definition_revision_id
        ),
        current_value=None if current_obs is None else current_obs.value,
        prior_definition_revision_ids=prior_rev_ids,
        unit=None if current_obs is None else current_obs.unit,
        currency=None if current_obs is None else current_obs.currency,
        bridge_kind=bridge_kind,
        comparability_revision_ids=comp_ids,
        bridge_spec=bridge_spec,
        source_locators=locators,
        reason=reason,
    )


def _continuous_predecessors(
    target_revision_id: str,
    edges: Sequence[KpiDefinitionComparabilityRevision],
) -> dict[str, tuple[KpiDefinitionComparabilityRevision, ...]]:
    paths: dict[str, tuple[KpiDefinitionComparabilityRevision, ...]] = {}
    frontier: list[tuple[str, tuple[KpiDefinitionComparabilityRevision, ...]]] = [
        (target_revision_id, ())
    ]
    visited = {target_revision_id}
    while frontier:
        rev_id, path = frontier.pop(0)
        for edge in edges:
            if edge.disposition is not KpiDefinitionComparabilityDisposition.CONTINUOUS:
                continue
            if edge.relation_kind not in {
                KpiDefinitionRelationKind.RENAMED,
                KpiDefinitionRelationKind.SAME_DEFINITION,
            }:
                continue
            neighbor: str | None = None
            if edge.successor_definition_revision_id == rev_id:
                neighbor = edge.predecessor_definition_revision_id
            elif edge.predecessor_definition_revision_id == rev_id:
                neighbor = edge.successor_definition_revision_id
            if neighbor is not None and neighbor not in visited:
                visited.add(neighbor)
                next_path = (*path, edge)
                paths[neighbor] = next_path
                frontier.append((neighbor, next_path))
    return paths


def resolve_kpi_comparable_yoy(
    conn: sqlite3.Connection,
    *,
    kpi_definition_id: int,
    period_end: date | datetime | str,
    effective_at: datetime,
    known_at: datetime,
    fiscal_period_type: FiscalPeriodType | str | None = None,
    prior_period_end: date | datetime | str | None = None,
    prior_fiscal_period_type: FiscalPeriodType | str | None = None,
) -> KpiComparableYoYResult:
    """Resolve comparable YoY in strict priority order, failing closed without proof."""
    eff = aware_utc(effective_at, "effective_at")
    known = aware_utc(known_at, "known_at")
    cur_iso = iso_date_str(period_end)
    cur_date = date.fromisoformat(cur_iso)
    exact_prior = prior_period_end is not None
    prior_date = (
        date.fromisoformat(iso_date_str(prior_period_end))
        if prior_period_end is not None
        else _default_prior_year_date(cur_date)
    )
    prior_iso = prior_date.isoformat()
    req_fpt = (
        fiscal_period_type.value
        if isinstance(fiscal_period_type, FiscalPeriodType)
        else fiscal_period_type
    )
    req_prior_fpt = (
        prior_fiscal_period_type.value
        if isinstance(prior_fiscal_period_type, FiscalPeriodType)
        else prior_fiscal_period_type
    )

    owns_snapshot = not conn.in_transaction
    if owns_snapshot:
        conn.execute("BEGIN")
    try:
        early_status, anchor = anchor_status_check(
            conn, kpi_definition_id=kpi_definition_id, effective_at=eff, known_at=known
        )
        if early_status is not None or anchor is None:
            mapped = {
                KpiSeriesReadStatus.LEGACY_UNBOUND: KpiComparableYoYStatus.LEGACY_UNBOUND,
                KpiSeriesReadStatus.QUARANTINED_DEFINITION: (
                    KpiComparableYoYStatus.QUARANTINED_DEFINITION
                ),
                KpiSeriesReadStatus.DISCONTINUED: KpiComparableYoYStatus.DISCONTINUED,
            }.get(
                early_status or KpiSeriesReadStatus.LEGACY_UNBOUND,
                KpiComparableYoYStatus.LEGACY_UNBOUND,
            )
            return _yoy_fail(
                mapped,
                kpi_definition_id=kpi_definition_id,
                anchor_id=None if anchor is None else anchor.kpi_definition_revision_id,
                cur_iso=cur_iso,
                prior_iso=prior_iso,
                reason=f"anchor definition status is {mapped.value}",
            )

        revisions, edges = collect_related_graph(
            conn, anchor=anchor, effective_at=eff, known_at=known
        )
        same_root_rows = conn.execute(
            "SELECT kpi_definition_revision_id FROM kpi_definition_revisions "
            "WHERE kpi_definition_id=? AND datetime(effective_at)<=datetime(?) "
            "AND datetime(knowledge_at)<=datetime(?) AND datetime(recorded_at)<=datetime(?)",
            (kpi_definition_id, eff.isoformat(), known.isoformat(), known.isoformat()),
        ).fetchall()
        for row in same_root_rows:
            rev_id = str(row[0])
            if rev_id not in revisions:
                rev = revision_visible(
                    kpi_definition_revision_by_id(conn, kpi_definition_revision_id=rev_id),
                    effective_at=eff,
                    known_at=known,
                )
                if rev is not None:
                    revisions[rev_id] = rev

        anchor_id = anchor.kpi_definition_revision_id
        try:
            observations, _ = load_admitted_observations(
                conn,
                revisions=revisions,
                extra_roots=(kpi_definition_id,),
                effective_at=eff,
                known_at=known,
            )
        except HistoricalFactAuthorityUnavailableError:
            return _yoy_fail(
                KpiComparableYoYStatus.HISTORICAL_FACT_AUTHORITY_UNAVAILABLE,
                kpi_definition_id=kpi_definition_id,
                anchor_id=anchor_id,
                cur_iso=cur_iso,
                prior_iso=prior_iso,
                reason="historical fact authority is unavailable",
            )

        req_cadence = None if req_fpt is None else classify_period_cadence(req_fpt)
        current_obs, current_cadence_mismatch = select_observation(
            observations,
            revision_ids={anchor_id},
            target_period=cur_date,
            exact_date_only=True,
            cadence_kind=req_cadence,
            fiscal_period_type=req_fpt,
            prefer_current_actual=True,
        )
        if current_obs is None and not current_cadence_mismatch:
            split_result = _resolve_split_reaggregation_yoy(
                kpi_definition_id=kpi_definition_id,
                anchor=anchor,
                observations=observations,
                edges=edges,
                cur_date=cur_date,
                prior_date=prior_date,
                exact_prior=exact_prior,
                req_fpt=req_fpt,
                req_prior_fpt=req_prior_fpt,
            )
            if split_result is not None:
                return split_result

        if current_obs is None:
            return _yoy_fail(
                (
                    KpiComparableYoYStatus.PERIOD_KIND_MISMATCH
                    if current_cadence_mismatch
                    else KpiComparableYoYStatus.CURRENT_PERIOD_MISSING
                ),
                kpi_definition_id=kpi_definition_id,
                anchor_id=anchor_id,
                cur_iso=cur_iso,
                prior_iso=prior_iso,
                reason=(
                    "requested fiscal_period_type does not match available cadence at current period"
                    if current_cadence_mismatch
                    else f"no admitted observation for {anchor_id} at {cur_iso}"
                ),
            )

        cadence = current_obs.cadence_kind
        if cadence is KpiPeriodCadenceKind.UNKNOWN:
            return _yoy_fail(
                KpiComparableYoYStatus.PERIOD_KIND_MISMATCH,
                kpi_definition_id=kpi_definition_id,
                anchor_id=anchor_id,
                cur_iso=cur_iso,
                prior_iso=prior_iso,
                current_obs=current_obs,
                reason="current observation has unknown reporting cadence",
            )

        effective_prior_fpt = req_prior_fpt or current_obs.fiscal_period_type
        if req_prior_fpt is not None and classify_period_cadence(req_prior_fpt) is not cadence:
            return _yoy_fail(
                KpiComparableYoYStatus.PERIOD_KIND_MISMATCH,
                kpi_definition_id=kpi_definition_id,
                anchor_id=anchor_id,
                cur_iso=cur_iso,
                prior_iso=prior_iso,
                current_obs=current_obs,
                prior_fpt=req_prior_fpt,
                reason=(
                    f"cadence mismatch: current is {current_obs.fiscal_period_type} ({cadence.value}) "
                    f"vs requested prior {req_prior_fpt} "
                    f"({classify_period_cadence(req_prior_fpt).value})"
                ),
            )

        incoming_edges = [
            edge for edge in edges if edge.successor_definition_revision_id == anchor_id
        ]

        # Priority 1: same kpi_definition_revision_id (normal YoY or recast comparative).
        same_prior, same_cadence_mismatch = select_observation(
            observations,
            revision_ids={anchor_id},
            target_period=prior_date,
            exact_date_only=exact_prior,
            cadence_kind=cadence,
            fiscal_period_type=effective_prior_fpt,
            prefer_current_actual=True,
        )
        if same_prior is not None:
            recast_edges = [
                e for e in incoming_edges if isinstance(e.bridge_spec, RecastComparativeBridge)
            ]
            delta, pct = _yoy_deltas(current_obs.value, same_prior.value)
            is_recast_comp = bool(recast_edges) and same_prior.publication_lane == "comparator"
            return KpiComparableYoYResult(
                status=KpiComparableYoYStatus.COMPARABLE,
                kpi_definition_id=kpi_definition_id,
                anchor_definition_revision_id=anchor_id,
                current_period_end=current_obs.period_end,
                prior_period_end=same_prior.period_end,
                current_fiscal_period_type=current_obs.fiscal_period_type,
                prior_fiscal_period_type=same_prior.fiscal_period_type,
                cadence_kind=cadence,
                current_fact_id=current_obs.fact_id,
                current_component_fact_ids=(current_obs.fact_id,),
                current_definition_revision_id=anchor_id,
                current_value=current_obs.value,
                prior_definition_revision_ids=(anchor_id,),
                prior_value_raw=same_prior.value,
                prior_value_adjusted=same_prior.value,
                yoy_delta=delta,
                yoy_pct=pct,
                unit=current_obs.unit,
                currency=current_obs.currency,
                bridge_kind="recast_comparative_only" if is_recast_comp else "same_revision",
                component_fact_ids=(same_prior.fact_id,),
                comparability_revision_ids=(
                    (recast_edges[0].comparability_revision_id,) if is_recast_comp else ()
                ),
                bridge_spec=recast_edges[0].bridge_spec if is_recast_comp else None,
                source_locators=(recast_edges[0].source_locator,) if is_recast_comp else (),
            )
        if same_cadence_mismatch:
            return _yoy_fail(
                KpiComparableYoYStatus.PERIOD_KIND_MISMATCH,
                kpi_definition_id=kpi_definition_id,
                anchor_id=anchor_id,
                cur_iso=cur_iso,
                prior_iso=prior_iso,
                current_obs=current_obs,
                reason="prior period exists on the same revision only under a different cadence",
            )

        # Priority 2: RENAMED / SAME_DEFINITION + CONTINUOUS.
        cadence_mismatch_seen = False
        for pred_rev_id, edge_path in _continuous_predecessors(anchor_id, edges).items():
            pred_obs, mismatch = select_observation(
                observations,
                revision_ids={pred_rev_id},
                target_period=prior_date,
                exact_date_only=exact_prior,
                cadence_kind=cadence,
                fiscal_period_type=effective_prior_fpt,
                prefer_current_actual=True,
            )
            if mismatch:
                cadence_mismatch_seen = True
                continue
            if pred_obs is not None:
                delta, pct = _yoy_deltas(current_obs.value, pred_obs.value)
                first_bridge = next(
                    (
                        e.bridge_spec
                        for e in edge_path
                        if isinstance(e.bridge_spec, RenameOverlapBridge)
                    ),
                    None,
                )
                return KpiComparableYoYResult(
                    status=KpiComparableYoYStatus.COMPARABLE,
                    kpi_definition_id=kpi_definition_id,
                    anchor_definition_revision_id=anchor_id,
                    current_period_end=current_obs.period_end,
                    prior_period_end=pred_obs.period_end,
                    current_fiscal_period_type=current_obs.fiscal_period_type,
                    prior_fiscal_period_type=pred_obs.fiscal_period_type,
                    cadence_kind=cadence,
                    current_fact_id=current_obs.fact_id,
                    current_component_fact_ids=(current_obs.fact_id,),
                    current_definition_revision_id=anchor_id,
                    current_value=current_obs.value,
                    prior_definition_revision_ids=(pred_rev_id,),
                    prior_value_raw=pred_obs.value,
                    prior_value_adjusted=pred_obs.value,
                    yoy_delta=delta,
                    yoy_pct=pct,
                    unit=current_obs.unit,
                    currency=current_obs.currency,
                    bridge_kind="rename_overlap" if first_bridge is not None else "continuous",
                    component_fact_ids=(pred_obs.fact_id,),
                    comparability_revision_ids=tuple(
                        e.comparability_revision_id for e in edge_path
                    ),
                    bridge_spec=first_bridge,
                    source_locators=tuple(e.source_locator for e in edge_path),
                )

        # Priority 3: RESTATED + COMPARABLE_WITH_BREAK with SplitScaleBridge.
        for edge in incoming_edges:
            if (
                edge.relation_kind is KpiDefinitionRelationKind.RESTATED
                and edge.disposition is KpiDefinitionComparabilityDisposition.COMPARABLE_WITH_BREAK
                and isinstance(edge.bridge_spec, SplitScaleBridge)
            ):
                pred_obs, mismatch = select_observation(
                    observations,
                    revision_ids={edge.predecessor_definition_revision_id},
                    target_period=prior_date,
                    exact_date_only=exact_prior,
                    cadence_kind=cadence,
                    fiscal_period_type=effective_prior_fpt,
                    prefer_current_actual=True,
                )
                if mismatch:
                    cadence_mismatch_seen = True
                    continue
                if pred_obs is not None:
                    adjusted = edge.bridge_spec.apply_to_predecessor(pred_obs.value)
                    delta, pct = _yoy_deltas(current_obs.value, adjusted)
                    return KpiComparableYoYResult(
                        status=KpiComparableYoYStatus.COMPARABLE_WITH_BRIDGE,
                        kpi_definition_id=kpi_definition_id,
                        anchor_definition_revision_id=anchor_id,
                        current_period_end=current_obs.period_end,
                        prior_period_end=pred_obs.period_end,
                        current_fiscal_period_type=current_obs.fiscal_period_type,
                        prior_fiscal_period_type=pred_obs.fiscal_period_type,
                        cadence_kind=cadence,
                        current_fact_id=current_obs.fact_id,
                        current_component_fact_ids=(current_obs.fact_id,),
                        current_definition_revision_id=anchor_id,
                        current_value=current_obs.value,
                        prior_definition_revision_ids=(edge.predecessor_definition_revision_id,),
                        prior_value_raw=pred_obs.value,
                        prior_value_adjusted=adjusted,
                        yoy_delta=delta,
                        yoy_pct=pct,
                        unit=current_obs.unit,
                        currency=current_obs.currency,
                        bridge_kind="scale_ratio",
                        scale_factor=edge.bridge_spec.scale_factor,
                        component_fact_ids=(pred_obs.fact_id,),
                        comparability_revision_ids=(edge.comparability_revision_id,),
                        bridge_spec=edge.bridge_spec,
                        source_locators=(edge.source_locator,),
                    )

        # Priority 4: COMBINED + COMPARABLE_WITH_BREAK with AdditiveCompositeBridge.
        combine_groups: dict[tuple[str, ...], list[KpiDefinitionComparabilityRevision]] = {}
        for edge in incoming_edges:
            if (
                edge.relation_kind is KpiDefinitionRelationKind.COMBINED
                and edge.disposition is KpiDefinitionComparabilityDisposition.COMPARABLE_WITH_BREAK
                and isinstance(edge.bridge_spec, AdditiveCompositeBridge)
                and edge.bridge_spec.direction == "combine"
                and edge.bridge_spec.composite_definition_revision_id == anchor_id
            ):
                key = tuple(sorted(edge.bridge_spec.member_definition_revision_ids))
                combine_groups.setdefault(key, []).append(edge)

        missing_composite_members: list[str] = []
        for member_key, group_edges in combine_groups.items():
            bridge_spec = cast(AdditiveCompositeBridge, group_edges[0].bridge_spec)
            persisted_preds = {e.predecessor_definition_revision_id for e in group_edges}
            if persisted_preds != set(member_key):
                missing_composite_members.extend(sorted(set(member_key) - persisted_preds))
                continue
            member_obs_map: dict[str, KpiAsReportedObservation] = {}
            group_missing: list[str] = []
            for member_id in bridge_spec.member_definition_revision_ids:
                obs_m, mismatch = select_observation(
                    observations,
                    revision_ids={member_id},
                    target_period=prior_date,
                    exact_date_only=exact_prior,
                    cadence_kind=cadence,
                    fiscal_period_type=effective_prior_fpt,
                    prefer_current_actual=True,
                )
                if mismatch:
                    cadence_mismatch_seen = True
                if obs_m is None:
                    group_missing.append(member_id)
                else:
                    member_obs_map[member_id] = obs_m
            if group_missing:
                missing_composite_members.extend(group_missing)
                continue
            total_prior = sum((o.value for o in member_obs_map.values()), Decimal("0"))
            delta, pct = _yoy_deltas(current_obs.value, total_prior)
            ordered_members = bridge_spec.member_definition_revision_ids
            sample_prior = member_obs_map[ordered_members[0]]
            return KpiComparableYoYResult(
                status=KpiComparableYoYStatus.COMPARABLE_WITH_BRIDGE,
                kpi_definition_id=kpi_definition_id,
                anchor_definition_revision_id=anchor_id,
                current_period_end=current_obs.period_end,
                prior_period_end=sample_prior.period_end,
                current_fiscal_period_type=current_obs.fiscal_period_type,
                prior_fiscal_period_type=sample_prior.fiscal_period_type,
                cadence_kind=cadence,
                current_fact_id=current_obs.fact_id,
                current_component_fact_ids=(current_obs.fact_id,),
                current_definition_revision_id=anchor_id,
                current_value=current_obs.value,
                prior_definition_revision_ids=ordered_members,
                prior_value_raw=None,
                prior_value_adjusted=total_prior,
                yoy_delta=delta,
                yoy_pct=pct,
                unit=current_obs.unit,
                currency=current_obs.currency,
                bridge_kind="sum_of_revisions",
                component_fact_ids=tuple(member_obs_map[m].fact_id for m in ordered_members),
                component_values={m: member_obs_map[m].value for m in ordered_members},
                comparability_revision_ids=tuple(
                    sorted(e.comparability_revision_id for e in group_edges)
                ),
                bridge_spec=bridge_spec,
                source_locators=(group_edges[0].source_locator,),
            )

        # Priority 5: Fail closed with exact typed status.
        if cadence_mismatch_seen:
            return _yoy_fail(
                KpiComparableYoYStatus.PERIOD_KIND_MISMATCH,
                kpi_definition_id=kpi_definition_id,
                anchor_id=anchor_id,
                cur_iso=cur_iso,
                prior_iso=prior_iso,
                current_obs=current_obs,
                reason="prior period observation exists only under an incompatible reporting cadence",
            )
        if missing_composite_members:
            return _yoy_fail(
                KpiComparableYoYStatus.COMPOSITE_MEMBER_MISSING,
                kpi_definition_id=kpi_definition_id,
                anchor_id=anchor_id,
                cur_iso=cur_iso,
                prior_iso=prior_iso,
                current_obs=current_obs,
                reason=(
                    "additive composite bridge missing prior-period member(s): "
                    + ", ".join(sorted(set(missing_composite_members)))
                ),
            )
        for edge in incoming_edges:
            if edge.relation_kind is KpiDefinitionRelationKind.RECAST:
                return _yoy_fail(
                    KpiComparableYoYStatus.RECAST_COMPARATIVE_MISSING,
                    kpi_definition_id=kpi_definition_id,
                    anchor_id=anchor_id,
                    cur_iso=cur_iso,
                    prior_iso=prior_iso,
                    current_obs=current_obs,
                    bridge_kind=(
                        "recast_comparative_only"
                        if isinstance(edge.bridge_spec, RecastComparativeBridge)
                        else None
                    ),
                    comp_ids=(edge.comparability_revision_id,),
                    bridge_spec=edge.bridge_spec,
                    locators=(edge.source_locator,),
                    reason=(
                        "prior period only exists on pre-recast predecessor; management recast "
                        "comparative is required for YoY"
                    ),
                )
        for edge in incoming_edges:
            if edge.relation_kind is KpiDefinitionRelationKind.SPLIT:
                return _yoy_fail(
                    KpiComparableYoYStatus.NOT_COMPARABLE,
                    kpi_definition_id=kpi_definition_id,
                    anchor_id=anchor_id,
                    cur_iso=cur_iso,
                    prior_iso=prior_iso,
                    current_obs=current_obs,
                    comp_ids=(edge.comparability_revision_id,),
                    bridge_spec=edge.bridge_spec,
                    locators=(edge.source_locator,),
                    reason=(
                        "split child revision cannot be compared directly to unsplit predecessor "
                        "without a restated child comparative"
                    ),
                )
        for edge in incoming_edges:
            pred_obs, _ = select_observation(
                observations,
                revision_ids={edge.predecessor_definition_revision_id},
                target_period=prior_date,
                exact_date_only=exact_prior,
                cadence_kind=cadence,
                fiscal_period_type=effective_prior_fpt,
                prefer_current_actual=True,
            )
            if pred_obs is not None:
                return _yoy_fail(
                    KpiComparableYoYStatus.NOT_COMPARABLE,
                    kpi_definition_id=kpi_definition_id,
                    anchor_id=anchor_id,
                    cur_iso=cur_iso,
                    prior_iso=pred_obs.period_end,
                    current_obs=current_obs,
                    prior_fpt=pred_obs.fiscal_period_type,
                    prior_rev_ids=(edge.predecessor_definition_revision_id,),
                    comp_ids=(edge.comparability_revision_id,),
                    locators=(edge.source_locator,),
                    reason=(
                        f"predecessor relation is {edge.relation_kind.value}/"
                        f"{edge.disposition.value} without a verified deterministic bridge"
                    ),
                )
        other_prior, _ = select_observation(
            observations,
            revision_ids=set(revisions.keys()) - {anchor_id},
            target_period=prior_date,
            exact_date_only=exact_prior,
            cadence_kind=cadence,
            fiscal_period_type=effective_prior_fpt,
            prefer_current_actual=True,
        )
        if other_prior is not None:
            return _yoy_fail(
                KpiComparableYoYStatus.NO_EXPLICIT_COMPARABILITY,
                kpi_definition_id=kpi_definition_id,
                anchor_id=anchor_id,
                cur_iso=cur_iso,
                prior_iso=other_prior.period_end,
                current_obs=current_obs,
                prior_fpt=other_prior.fiscal_period_type,
                prior_rev_ids=(other_prior.kpi_definition_revision_id,),
                reason="prior period exists on another definition revision without explicit comparability",
            )
        return _yoy_fail(
            KpiComparableYoYStatus.PRIOR_PERIOD_MISSING,
            kpi_definition_id=kpi_definition_id,
            anchor_id=anchor_id,
            cur_iso=cur_iso,
            prior_iso=prior_iso,
            current_obs=current_obs,
            reason=f"no admitted prior-period observation found near {prior_iso}",
        )
    finally:
        if owns_snapshot:
            conn.rollback()


def _resolve_split_reaggregation_yoy(
    *,
    kpi_definition_id: int,
    anchor: IssuerKpiDefinitionRevision,
    observations: Sequence[KpiAsReportedObservation],
    edges: Sequence[KpiDefinitionComparabilityRevision],
    cur_date: date,
    prior_date: date,
    exact_prior: bool,
    req_fpt: str | None,
    req_prior_fpt: str | None,
) -> KpiComparableYoYResult | None:
    anchor_id = anchor.kpi_definition_revision_id
    split_groups: dict[tuple[str, ...], list[KpiDefinitionComparabilityRevision]] = {}
    for edge in edges:
        if (
            edge.predecessor_definition_revision_id == anchor_id
            and edge.relation_kind is KpiDefinitionRelationKind.SPLIT
            and edge.disposition is KpiDefinitionComparabilityDisposition.COMPARABLE_WITH_BREAK
            and isinstance(edge.bridge_spec, AdditiveCompositeBridge)
            and edge.bridge_spec.direction == "split"
            and edge.bridge_spec.composite_definition_revision_id == anchor_id
        ):
            key = tuple(sorted(edge.bridge_spec.member_definition_revision_ids))
            split_groups.setdefault(key, []).append(edge)
    if not split_groups:
        return None
    req_cadence = None if req_fpt is None else classify_period_cadence(req_fpt)
    for member_key, group_edges in split_groups.items():
        bridge_spec = cast(AdditiveCompositeBridge, group_edges[0].bridge_spec)
        if {e.successor_definition_revision_id for e in group_edges} != set(member_key):
            continue
        ordered_members = bridge_spec.member_definition_revision_ids
        cur_member_obs: dict[str, KpiAsReportedObservation] = {}
        missing_cur: list[str] = []
        for member_id in ordered_members:
            obs_m, _ = select_observation(
                observations,
                revision_ids={member_id},
                target_period=cur_date,
                exact_date_only=True,
                cadence_kind=req_cadence,
                fiscal_period_type=req_fpt,
                prefer_current_actual=True,
            )
            if obs_m is None:
                missing_cur.append(member_id)
            else:
                cur_member_obs[member_id] = obs_m
        if missing_cur:
            return _yoy_fail(
                KpiComparableYoYStatus.COMPOSITE_MEMBER_MISSING,
                kpi_definition_id=kpi_definition_id,
                anchor_id=anchor_id,
                cur_iso=cur_date.isoformat(),
                prior_iso=prior_date.isoformat(),
                reason=(
                    "split composite bridge missing current-period member(s): "
                    + ", ".join(sorted(missing_cur))
                ),
            )
        cadences = {o.cadence_kind for o in cur_member_obs.values()}
        if len(cadences) != 1 or KpiPeriodCadenceKind.UNKNOWN in cadences:
            return _yoy_fail(
                KpiComparableYoYStatus.PERIOD_KIND_MISMATCH,
                kpi_definition_id=kpi_definition_id,
                anchor_id=anchor_id,
                cur_iso=cur_date.isoformat(),
                prior_iso=prior_date.isoformat(),
                reason="split member observations have inconsistent reporting cadences",
            )
        cadence = next(iter(cadences))
        sample_cur = cur_member_obs[ordered_members[0]]
        eff_prior_fpt = req_prior_fpt or sample_cur.fiscal_period_type
        prior_obs, prior_mismatch = select_observation(
            observations,
            revision_ids={anchor_id},
            target_period=prior_date,
            exact_date_only=exact_prior,
            cadence_kind=cadence,
            fiscal_period_type=eff_prior_fpt,
            prefer_current_actual=True,
        )
        if prior_mismatch or prior_obs is None:
            return _yoy_fail(
                (
                    KpiComparableYoYStatus.PERIOD_KIND_MISMATCH
                    if prior_mismatch
                    else KpiComparableYoYStatus.PRIOR_PERIOD_MISSING
                ),
                kpi_definition_id=kpi_definition_id,
                anchor_id=anchor_id,
                cur_iso=cur_date.isoformat(),
                prior_iso=prior_date.isoformat(),
                reason=(
                    "split predecessor prior period has incompatible cadence"
                    if prior_mismatch
                    else "split predecessor prior period is missing"
                ),
            )
        total_current = sum((o.value for o in cur_member_obs.values()), Decimal("0"))
        delta, pct = _yoy_deltas(total_current, prior_obs.value)
        return KpiComparableYoYResult(
            status=KpiComparableYoYStatus.COMPARABLE_WITH_BRIDGE,
            kpi_definition_id=kpi_definition_id,
            anchor_definition_revision_id=anchor_id,
            current_period_end=sample_cur.period_end,
            prior_period_end=prior_obs.period_end,
            current_fiscal_period_type=sample_cur.fiscal_period_type,
            prior_fiscal_period_type=prior_obs.fiscal_period_type,
            cadence_kind=cadence,
            current_fact_id=None,
            current_component_fact_ids=tuple(cur_member_obs[m].fact_id for m in ordered_members),
            current_definition_revision_id=anchor_id,
            current_value=total_current,
            prior_definition_revision_ids=(anchor_id,),
            prior_value_raw=prior_obs.value,
            prior_value_adjusted=prior_obs.value,
            yoy_delta=delta,
            yoy_pct=pct,
            unit=prior_obs.unit,
            currency=prior_obs.currency,
            bridge_kind="sum_of_revisions",
            component_fact_ids=(prior_obs.fact_id,),
            component_values={m: cur_member_obs[m].value for m in ordered_members},
            comparability_revision_ids=tuple(
                sorted(e.comparability_revision_id for e in group_edges)
            ),
            bridge_spec=bridge_spec,
            source_locators=(group_edges[0].source_locator,),
        )
    return None
