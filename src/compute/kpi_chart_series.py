"""Segmented KPI chart-series projection across comparability and cadence boundaries."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from compute.kpi_comparability_read import (
    KpiAsReportedObservation,
    KpiSeriesReadStatus,
    anchor_status_check,
    aware_utc,
    collect_related_graph,
    load_admitted_observations,
)
from models.facts import Currency, Unit
from pipeline.kpi_comparability_bridges import (
    AdditiveCompositeBridge,
    KpiComparabilityBridgeSpec,
    KpiPeriodCadenceKind,
    RecastComparativeBridge,
    SplitScaleBridge,
)
from pipeline.kpi_definition_revisions import (
    IssuerKpiDefinitionRevision,
    KpiDefinitionComparabilityDisposition,
    KpiDefinitionComparabilityRevision,
    KpiDefinitionRelationKind,
)
from provenance.financial_fact_resolution import HistoricalFactAuthorityUnavailableError


class KpiChartPoint(BaseModel):
    """One plotted point inside a contiguous chart segment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_id: int | None = None
    component_fact_ids: tuple[int, ...] = ()
    kpi_definition_id: int = Field(gt=0)
    kpi_definition_revision_id: str = Field(min_length=1, max_length=128)
    period_end: str = Field(min_length=10, max_length=10)
    fiscal_period_type: str | None = None
    cadence_kind: KpiPeriodCadenceKind
    value: Decimal
    raw_value: Decimal | None = None
    unit: Unit
    currency: Currency | None = None
    reported_label: str = Field(min_length=1)
    source_doc_id: int | None = None
    is_adjusted: bool = False
    bridge_kind: str | None = None
    scale_factor: Decimal | None = None


class KpiChartSegment(BaseModel):
    """One contiguous, single-cadence run of continuous revisions for charting."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    segment_index: int = Field(ge=0)
    cadence_kind: KpiPeriodCadenceKind
    definition_revision_ids: tuple[str, ...] = Field(min_length=1)
    reported_label: str = Field(min_length=1)
    is_transformed: bool = False
    bridge_kind: str | None = None
    points: tuple[KpiChartPoint, ...] = ()


class KpiChartBreak(BaseModel):
    """Typed comparability boundary marker between chart segments."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    period_end: str = Field(min_length=10, max_length=10)
    relation_kind: KpiDefinitionRelationKind
    disposition: KpiDefinitionComparabilityDisposition
    bridge_kind: str | None = None
    label: str = Field(min_length=1)
    predecessor_revision_id: str = Field(min_length=1, max_length=128)
    successor_revision_id: str = Field(min_length=1, max_length=128)
    comparability_revision_id: str = Field(min_length=1, max_length=128)
    source_locator: dict[str, object] = Field(min_length=1)


class KpiChartSeries(BaseModel):
    """Segmented chart projection that never silently bridges semantic or cadence breaks."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kpi_definition_id: int = Field(gt=0)
    status: KpiSeriesReadStatus
    anchor_definition_revision_id: str | None = None
    cadence_kinds_present: tuple[KpiPeriodCadenceKind, ...] = ()
    segments: tuple[KpiChartSegment, ...] = ()
    breaks: tuple[KpiChartBreak, ...] = ()
    excluded_fact_ids: tuple[int, ...] = ()
    excluded_cadence_fact_ids: tuple[int, ...] = ()


def _break_label(
    edge: KpiDefinitionComparabilityRevision,
    revisions: dict[str, IssuerKpiDefinitionRevision],
) -> str:
    pred = revisions.get(edge.predecessor_definition_revision_id)
    succ = revisions.get(edge.successor_definition_revision_id)
    pred_label = (
        pred.reported_label if pred is not None else edge.predecessor_definition_revision_id
    )
    succ_label = succ.reported_label if succ is not None else edge.successor_definition_revision_id
    if isinstance(edge.bridge_spec, SplitScaleBridge):
        return (
            f"{edge.bridge_spec.scale_numerator}:{edge.bridge_spec.scale_denominator} "
            f"stock split ({edge.bridge_spec.applies_to})"
        )
    if isinstance(edge.bridge_spec, AdditiveCompositeBridge):
        count = len(edge.bridge_spec.member_definition_revision_ids)
        if edge.bridge_spec.direction == "combine":
            return f"Combined {count} segments into {succ_label}"
        return f"Split {pred_label} into {count} segments"
    if isinstance(edge.bridge_spec, RecastComparativeBridge):
        return f"Recast ({edge.bridge_spec.recast_reason}): {pred_label} -> {succ_label}"
    if edge.relation_kind is KpiDefinitionRelationKind.RENAMED:
        return f"Renamed (unverified): {pred_label} -> {succ_label}"
    return f"{edge.relation_kind.value} ({edge.disposition.value}): {pred_label} -> {succ_label}"


def _bridge_kind_name(spec: KpiComparabilityBridgeSpec | None) -> str | None:
    return None if spec is None else spec.kind


def resolve_kpi_chart_series(
    conn: sqlite3.Connection,
    *,
    kpi_definition_id: int,
    effective_at: datetime,
    known_at: datetime,
    cadence_kind: KpiPeriodCadenceKind | None = None,
    apply_split_adjustment: bool = False,
    apply_composite_bridge: bool = False,
    include_comparators: bool = True,
) -> KpiChartSeries:
    """Resolve ordered contiguous chart segments and typed break markers."""
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
            return KpiChartSeries(
                kpi_definition_id=kpi_definition_id,
                status=early_status or KpiSeriesReadStatus.LEGACY_UNBOUND,
                anchor_definition_revision_id=(
                    None if anchor is None else anchor.kpi_definition_revision_id
                ),
            )
        revisions, edges = collect_related_graph(
            conn, anchor=anchor, effective_at=eff, known_at=known
        )
        anchor_id = anchor.kpi_definition_revision_id
        try:
            observations, excluded_facts = load_admitted_observations(
                conn,
                revisions=revisions,
                extra_roots=(kpi_definition_id,),
                effective_at=eff,
                known_at=known,
            )
        except HistoricalFactAuthorityUnavailableError:
            return KpiChartSeries(
                kpi_definition_id=kpi_definition_id,
                status=KpiSeriesReadStatus.HISTORICAL_FACT_AUTHORITY_UNAVAILABLE,
                anchor_definition_revision_id=anchor_id,
            )

        parent_map: dict[str, str] = {rev_id: rev_id for rev_id in revisions}

        def find(rev_id: str) -> str:
            while parent_map[rev_id] != rev_id:
                parent_map[rev_id] = parent_map[parent_map[rev_id]]
                rev_id = parent_map[rev_id]
            return rev_id

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent_map[rb] = ra

        for edge in edges:
            if (
                edge.disposition is KpiDefinitionComparabilityDisposition.CONTINUOUS
                and edge.predecessor_definition_revision_id in parent_map
                and edge.successor_definition_revision_id in parent_map
            ):
                union(
                    edge.predecessor_definition_revision_id,
                    edge.successor_definition_revision_id,
                )

        anchor_component = find(anchor_id)
        comparable_components: set[str] = {anchor_component}
        split_bridge_by_component: dict[str, SplitScaleBridge] = {}
        combine_bridges: list[AdditiveCompositeBridge] = []
        seen_combine_keys: set[tuple[str, ...]] = set()
        break_markers: list[KpiChartBreak] = []
        seen_break_keys: set[tuple[str, str]] = set()

        for edge in edges:
            pred_id = edge.predecessor_definition_revision_id
            succ_id = edge.successor_definition_revision_id
            if edge.disposition is KpiDefinitionComparabilityDisposition.CONTINUOUS:
                continue
            touches_anchor = (pred_id in parent_map and find(pred_id) == anchor_component) or (
                succ_id in parent_map and find(succ_id) == anchor_component
            )
            if not touches_anchor:
                continue

            if isinstance(edge.bridge_spec, AdditiveCompositeBridge):
                if (
                    edge.bridge_spec.direction == "combine"
                    and edge.bridge_spec.composite_definition_revision_id == anchor_id
                ):
                    key = tuple(sorted(edge.bridge_spec.member_definition_revision_ids))
                    if key not in seen_combine_keys:
                        seen_combine_keys.add(key)
                        combine_bridges.append(edge.bridge_spec)
                        break_markers.append(
                            KpiChartBreak(
                                period_end=edge.effective_at.date().isoformat(),
                                relation_kind=edge.relation_kind,
                                disposition=edge.disposition,
                                bridge_kind=edge.bridge_spec.kind,
                                label=_break_label(edge, revisions),
                                predecessor_revision_id=pred_id,
                                successor_revision_id=succ_id,
                                comparability_revision_id=edge.comparability_revision_id,
                                source_locator=edge.source_locator,
                            )
                        )
                continue

            pair_key = (pred_id, succ_id)
            if pair_key not in seen_break_keys:
                seen_break_keys.add(pair_key)
                break_markers.append(
                    KpiChartBreak(
                        period_end=edge.effective_at.date().isoformat(),
                        relation_kind=edge.relation_kind,
                        disposition=edge.disposition,
                        bridge_kind=_bridge_kind_name(edge.bridge_spec),
                        label=_break_label(edge, revisions),
                        predecessor_revision_id=pred_id,
                        successor_revision_id=succ_id,
                        comparability_revision_id=edge.comparability_revision_id,
                        source_locator=edge.source_locator,
                    )
                )
            if edge.disposition is KpiDefinitionComparabilityDisposition.COMPARABLE_WITH_BREAK:
                other_id = pred_id if find(succ_id) == anchor_component else succ_id
                other_comp = find(other_id)
                comparable_components.add(other_comp)
                if (
                    isinstance(edge.bridge_spec, SplitScaleBridge)
                    and find(succ_id) == anchor_component
                ):
                    split_bridge_by_component[other_comp] = edge.bridge_spec

        all_cadences: set[KpiPeriodCadenceKind] = set()
        excluded_cadence: list[int] = []
        grouped: dict[tuple[str, KpiPeriodCadenceKind], list[KpiAsReportedObservation]] = {}
        extra_excluded: list[int] = list(excluded_facts)

        for obs in observations:
            if not include_comparators and obs.publication_lane == "comparator":
                continue
            comp = find(obs.kpi_definition_revision_id)
            if comp not in comparable_components:
                extra_excluded.append(obs.fact_id)
                continue
            all_cadences.add(obs.cadence_kind)
            if cadence_kind is not None and obs.cadence_kind is not cadence_kind:
                excluded_cadence.append(obs.fact_id)
                continue
            grouped.setdefault((comp, obs.cadence_kind), []).append(obs)

        raw_segments: list[tuple[str, KpiChartSegment]] = []
        for (comp, seg_cadence), obs_list in grouped.items():
            by_period: dict[str, KpiAsReportedObservation] = {}
            for obs in obs_list:
                incumbent = by_period.get(obs.period_end)
                if incumbent is None or (
                    incumbent.publication_lane == "comparator"
                    and obs.publication_lane != "comparator"
                ):
                    by_period[obs.period_end] = obs
            ordered_obs = [by_period[p] for p in sorted(by_period)]
            split_bridge = split_bridge_by_component.get(comp) if apply_split_adjustment else None
            seg_revs = tuple(sorted({o.kpi_definition_revision_id for o in ordered_obs}))
            latest_rev = revisions[ordered_obs[-1].kpi_definition_revision_id]
            points = tuple(
                KpiChartPoint(
                    fact_id=obs.fact_id,
                    component_fact_ids=(obs.fact_id,),
                    kpi_definition_id=obs.kpi_definition_id,
                    kpi_definition_revision_id=obs.kpi_definition_revision_id,
                    period_end=obs.period_end,
                    fiscal_period_type=obs.fiscal_period_type,
                    cadence_kind=obs.cadence_kind,
                    value=(
                        split_bridge.apply_to_predecessor(obs.value)
                        if split_bridge is not None
                        else obs.value
                    ),
                    raw_value=obs.value,
                    unit=obs.unit,
                    currency=obs.currency,
                    reported_label=obs.reported_label,
                    source_doc_id=obs.source_doc_id,
                    is_adjusted=split_bridge is not None,
                    bridge_kind="scale_ratio" if split_bridge is not None else None,
                    scale_factor=split_bridge.scale_factor if split_bridge is not None else None,
                )
                for obs in ordered_obs
            )
            raw_segments.append(
                (
                    ordered_obs[0].period_end,
                    KpiChartSegment(
                        segment_index=0,
                        cadence_kind=seg_cadence,
                        definition_revision_ids=seg_revs,
                        reported_label=latest_rev.reported_label,
                        is_transformed=split_bridge is not None,
                        bridge_kind="scale_ratio" if split_bridge is not None else None,
                        points=points,
                    ),
                )
            )

        if apply_composite_bridge and combine_bridges:
            for bridge in combine_bridges:
                member_ids = bridge.member_definition_revision_ids
                by_cadence_period: dict[
                    tuple[KpiPeriodCadenceKind, str], dict[str, KpiAsReportedObservation]
                ] = {}
                for obs in observations:
                    if obs.kpi_definition_revision_id not in member_ids:
                        continue
                    if not include_comparators and obs.publication_lane == "comparator":
                        continue
                    all_cadences.add(obs.cadence_kind)
                    if cadence_kind is not None and obs.cadence_kind is not cadence_kind:
                        excluded_cadence.append(obs.fact_id)
                        continue
                    bucket = by_cadence_period.setdefault((obs.cadence_kind, obs.period_end), {})
                    bucket.setdefault(obs.kpi_definition_revision_id, obs)
                points_by_cadence: dict[KpiPeriodCadenceKind, list[KpiChartPoint]] = {}
                for (seg_cadence, p_end), members_present in sorted(by_cadence_period.items()):
                    if set(members_present.keys()) != set(member_ids):
                        continue
                    sample = members_present[member_ids[0]]
                    total = sum((members_present[m].value for m in member_ids), Decimal("0"))
                    points_by_cadence.setdefault(seg_cadence, []).append(
                        KpiChartPoint(
                            fact_id=None,
                            component_fact_ids=tuple(
                                members_present[m].fact_id for m in member_ids
                            ),
                            kpi_definition_id=anchor.kpi_definition_id,
                            kpi_definition_revision_id=anchor_id,
                            period_end=p_end,
                            fiscal_period_type=sample.fiscal_period_type,
                            cadence_kind=seg_cadence,
                            value=total,
                            raw_value=None,
                            unit=sample.unit,
                            currency=sample.currency,
                            reported_label=anchor.reported_label,
                            source_doc_id=None,
                            is_adjusted=True,
                            bridge_kind="sum_of_revisions",
                        )
                    )
                for seg_cadence, comp_points in points_by_cadence.items():
                    raw_segments.append(
                        (
                            comp_points[0].period_end,
                            KpiChartSegment(
                                segment_index=0,
                                cadence_kind=seg_cadence,
                                definition_revision_ids=tuple(sorted(member_ids)),
                                reported_label=anchor.reported_label,
                                is_transformed=True,
                                bridge_kind="sum_of_revisions",
                                points=tuple(comp_points),
                            ),
                        )
                    )

        raw_segments.sort(
            key=lambda pair: (pair[0], pair[1].cadence_kind.value, pair[1].reported_label)
        )
        numbered_segments = tuple(
            seg.model_copy(update={"segment_index": idx})
            for idx, (_, seg) in enumerate(raw_segments)
        )
        ordered_breaks = tuple(
            sorted(
                break_markers,
                key=lambda b: (b.period_end, b.predecessor_revision_id, b.successor_revision_id),
            )
        )
        return KpiChartSeries(
            kpi_definition_id=kpi_definition_id,
            status=(
                KpiSeriesReadStatus.ELIGIBLE_WITH_BREAK
                if ordered_breaks
                else KpiSeriesReadStatus.ELIGIBLE
            ),
            anchor_definition_revision_id=anchor_id,
            cadence_kinds_present=tuple(sorted(all_cadences, key=lambda c: c.value)),
            segments=numbered_segments,
            breaks=ordered_breaks,
            excluded_fact_ids=tuple(sorted(set(extra_excluded))),
            excluded_cadence_fact_ids=tuple(sorted(set(excluded_cadence))),
        )
    finally:
        if owns_snapshot:
            conn.rollback()
