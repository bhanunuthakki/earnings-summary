"""Shared comparability-aware KPI projection helpers for report sections.

Bridges ``resolve_kpi_chart_series`` and ``resolve_kpi_comparable_yoy`` onto
``report.models`` structures (``KpiSeries``, ``AnnualKpiSeries``, and
``KpiLedgerRow``) without introducing any raw ``kpi_facts`` SQL or altering
legacy unbound fixtures.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from compute.kpi_resolver import (
    KpiChartPoint,
    KpiChartSeries,
    KpiComparableYoYResult,
    KpiComparableYoYStatus,
    KpiSeriesReadStatus,
    resolve_kpi_chart_series,
    resolve_kpi_comparable_yoy,
)
from pipeline.kpi_comparability_bridges import KpiPeriodCadenceKind
from report.models import KpiSeriesBreak, KpiSeriesSegment

_COMPARABLE_YOY_STATUSES = frozenset(
    {
        KpiComparableYoYStatus.COMPARABLE,
        KpiComparableYoYStatus.COMPARABLE_WITH_BRIDGE,
    }
)


@dataclass(frozen=True, slots=True)
class AlignedKpiComparability:
    """Axis-aligned comparability segments, breaks, and strict YoY overrides."""

    kpi_definition_id: int
    status: str
    segments: list[KpiSeriesSegment]
    breaks: list[KpiSeriesBreak]
    values_by_label: dict[str, float]
    yoy_pct_full: list[float | None]
    yoy_status_full: list[str | None]
    yoy_reasons_full: list[str | None]
    comparison_eligible_edges: list[bool]


@dataclass(frozen=True, slots=True)
class LedgerKpiComparability:
    """Ledger-shaped comparability metadata and latest-period comparable YoY."""

    status: str
    segments: list[KpiSeriesSegment]
    breaks: list[KpiSeriesBreak]
    history: list[tuple[str, float | None]]
    comparable_yoy_delta: float | None
    comparable_yoy_pct: float | None
    comparable_yoy_status: str | None
    comparable_yoy_bridge_kind: str | None
    comparable_yoy_reason: str | None


def _quarter_label(period_end_iso: str) -> str | None:
    try:
        year = int(period_end_iso[:4])
        month = int(period_end_iso[5:7])
    except ValueError:
        return None
    return f"{year} Q{(month - 1) // 3 + 1}"


def _axis_label(period_end_iso: str, axis_kind: Literal["quarterly", "annual"]) -> str | None:
    if axis_kind == "annual":
        try:
            return str(int(period_end_iso[:4]))
        except ValueError:
            return None
    return _quarter_label(period_end_iso)


def _format_yoy_reason(res: KpiComparableYoYResult) -> str | None:
    if res.status is KpiComparableYoYStatus.COMPARABLE_WITH_BRIDGE:
        if res.bridge_kind == "scale_ratio" and res.scale_factor is not None:
            return f"Comparable via split-scale bridge (factor {res.scale_factor})"
        if res.bridge_kind:
            return f"Comparable via {res.bridge_kind} bridge"
        return "Comparable via admitted bridge"
    if res.status is KpiComparableYoYStatus.COMPARABLE:
        if res.bridge_kind == "recast_comparative_only":
            return "Comparable via management recast comparative"
        if res.bridge_kind == "rename_overlap":
            return "Comparable via verified rename overlap"
        return None
    if res.status in {
        KpiComparableYoYStatus.NOT_COMPARABLE,
        KpiComparableYoYStatus.RECAST_COMPARATIVE_MISSING,
        KpiComparableYoYStatus.COMPOSITE_MEMBER_MISSING,
        KpiComparableYoYStatus.PERIOD_KIND_MISMATCH,
        KpiComparableYoYStatus.NO_EXPLICIT_COMPARABILITY,
    }:
        detail = f": {res.reason}" if res.reason else ""
        return f"YoY unavailable ({res.status.value}){detail}"
    return None


def _resolve_point_yoy(
    conn: sqlite3.Connection,
    *,
    chart: KpiChartSeries,
    point: KpiChartPoint,
    effective_at: datetime,
    known_at: datetime,
) -> KpiComparableYoYResult:
    if (
        point.kpi_definition_id == chart.kpi_definition_id
        and point.kpi_definition_revision_id == chart.anchor_definition_revision_id
    ):
        pt_eff = effective_at
    else:
        pt_eff = datetime.fromisoformat(f"{point.period_end}T23:59:59+00:00")
    return resolve_kpi_comparable_yoy(
        conn,
        kpi_definition_id=point.kpi_definition_id,
        period_end=point.period_end,
        effective_at=pt_eff,
        known_at=known_at,
        fiscal_period_type=point.fiscal_period_type,
    )


def _to_model_segments(chart: KpiChartSeries) -> list[KpiSeriesSegment]:
    return [
        KpiSeriesSegment(
            segment_index=seg.segment_index,
            reported_label=seg.reported_label,
            cadence_kind=seg.cadence_kind.value,
            definition_revision_ids=list(seg.definition_revision_ids),
            is_transformed=seg.is_transformed,
            bridge_kind=seg.bridge_kind,
            points=[(pt.period_end, float(pt.value)) for pt in seg.points],
        )
        for seg in chart.segments
    ]


def _to_model_breaks(
    chart: KpiChartSeries,
    *,
    axis_kind: Literal["quarterly", "annual"] | None = None,
) -> list[KpiSeriesBreak]:
    return [
        KpiSeriesBreak(
            period_end=brk.period_end,
            period_label=(None if axis_kind is None else _axis_label(brk.period_end, axis_kind)),
            relation_kind=brk.relation_kind.value,
            disposition=brk.disposition.value,
            bridge_kind=brk.bridge_kind,
            label=brk.label,
            predecessor_revision_id=brk.predecessor_revision_id,
            successor_revision_id=brk.successor_revision_id,
            comparability_revision_id=brk.comparability_revision_id,
            source_locator=dict(brk.source_locator),
        )
        for brk in chart.breaks
    ]


def resolve_aligned_kpi_comparability(
    conn: sqlite3.Connection,
    *,
    kpi_definition_id: int,
    axis_labels: list[str],
    axis_kind: Literal["quarterly", "annual"],
    effective_at: datetime | None = None,
    known_at: datetime | None = None,
) -> AlignedKpiComparability | None:
    """Resolve revision-aware chart segments, breaks, and per-period comparable YoY.

    Returns ``None`` when the KPI has no ``kpi_definition_revisions`` binding
    (``LEGACY_UNBOUND``) or no eligible points, preserving legacy readers unchanged.
    """
    now = datetime.now(UTC)
    eff = effective_at or now
    known = known_at or now
    cadence = (
        KpiPeriodCadenceKind.ANNUAL
        if axis_kind == "annual"
        else KpiPeriodCadenceKind.STANDALONE_QUARTER
    )
    try:
        chart = resolve_kpi_chart_series(
            conn,
            kpi_definition_id=kpi_definition_id,
            effective_at=eff,
            known_at=known,
            cadence_kind=cadence,
            apply_split_adjustment=True,
            apply_composite_bridge=True,
            include_comparators=True,
        )
    except (sqlite3.Error, ValueError):
        return None
    if (
        chart.status
        not in {
            KpiSeriesReadStatus.ELIGIBLE,
            KpiSeriesReadStatus.ELIGIBLE_WITH_BREAK,
        }
        or not chart.segments
    ):
        return None

    segments = _to_model_segments(chart)
    breaks = _to_model_breaks(chart, axis_kind=axis_kind)

    # Map each axis label to its winning plotted point and segment index.
    # Later segments win on overlapping comparative dates (e.g. recast comparator
    # in the anchor segment supersedes the pre-recast reported row on the same date).
    point_by_label: dict[str, tuple[int, KpiChartPoint]] = {}
    values_by_label: dict[str, float] = {}
    for seg in chart.segments:
        for pt in seg.points:
            lbl = _axis_label(pt.period_end, axis_kind)
            if lbl is None:
                continue
            point_by_label[lbl] = (seg.segment_index, pt)
            values_by_label[lbl] = float(pt.value)

    yoy_pct_full: list[float | None] = []
    yoy_status_full: list[str | None] = []
    yoy_reasons_full: list[str | None] = []
    seg_indices: list[int | None] = []
    period_ends: list[str | None] = []

    for lbl in axis_labels:
        entry = point_by_label.get(lbl)
        if entry is None:
            yoy_pct_full.append(None)
            yoy_status_full.append(None)
            yoy_reasons_full.append(None)
            seg_indices.append(None)
            period_ends.append(None)
            continue
        seg_idx, pt = entry
        seg_indices.append(seg_idx)
        period_ends.append(pt.period_end)
        yoy_res = _resolve_point_yoy(
            conn,
            chart=chart,
            point=pt,
            effective_at=eff,
            known_at=known,
        )
        yoy_status_full.append(yoy_res.status.value)
        yoy_reasons_full.append(_format_yoy_reason(yoy_res))
        if yoy_res.status in _COMPARABLE_YOY_STATUSES and yoy_res.yoy_pct is not None:
            yoy_pct_full.append(float(yoy_res.yoy_pct * 100))
        else:
            yoy_pct_full.append(None)

    # Build comparison_eligible_edges for CAGR gating: an edge (i-1 -> i) is
    # eligible only when consecutive populated points stay in the same contiguous
    # segment (or are split-adjusted continuously across a scale_ratio bridge) and
    # do not cross an unbridged break.
    non_continuous_break_dates = [
        brk.period_end
        for brk in chart.breaks
        if brk.bridge_kind != "scale_ratio" and brk.disposition.value != "continuous"
    ]
    edges: list[bool] = []
    last_seg: int | None = None
    last_pe: str | None = None
    for i in range(len(axis_labels)):
        if i == 0:
            edges.append(False)
        else:
            cur_seg = seg_indices[i]
            cur_pe = period_ends[i]
            if cur_seg is None or last_seg is None or cur_pe is None or last_pe is None:
                edges.append(True)
            else:
                crossed_unbridged = any(
                    last_pe < b_date <= cur_pe for b_date in non_continuous_break_dates
                )
                same_seg_or_scaled = cur_seg == last_seg or (
                    chart.segments[last_seg].bridge_kind == "scale_ratio" and not crossed_unbridged
                )
                edges.append(same_seg_or_scaled and not crossed_unbridged)
        if seg_indices[i] is not None:
            last_seg = seg_indices[i]
            last_pe = period_ends[i]

    return AlignedKpiComparability(
        kpi_definition_id=kpi_definition_id,
        status=chart.status.value,
        segments=segments,
        breaks=breaks,
        values_by_label=values_by_label,
        yoy_pct_full=yoy_pct_full,
        yoy_status_full=yoy_status_full,
        yoy_reasons_full=yoy_reasons_full,
        comparison_eligible_edges=edges,
    )


def resolve_ledger_kpi_comparability(
    conn: sqlite3.Connection,
    *,
    kpi_definition_id: int,
    is_annual: bool = False,
    effective_at: datetime | None = None,
    known_at: datetime | None = None,
) -> LedgerKpiComparability | None:
    """Resolve segmented history and latest-period comparable YoY for ``KpiLedgerRow``.

    Returns ``None`` for legacy unbound KPIs so existing ledger behavior stays unchanged.
    """
    now = datetime.now(UTC)
    eff = effective_at or now
    known = known_at or now
    cadence = KpiPeriodCadenceKind.ANNUAL if is_annual else KpiPeriodCadenceKind.STANDALONE_QUARTER
    try:
        chart = resolve_kpi_chart_series(
            conn,
            kpi_definition_id=kpi_definition_id,
            effective_at=eff,
            known_at=known,
            cadence_kind=cadence,
            apply_split_adjustment=True,
            apply_composite_bridge=True,
            include_comparators=True,
        )
    except (sqlite3.Error, ValueError):
        return None
    if (
        chart.status
        not in {
            KpiSeriesReadStatus.ELIGIBLE,
            KpiSeriesReadStatus.ELIGIBLE_WITH_BREAK,
        }
        or not chart.segments
    ):
        return None

    segments = _to_model_segments(chart)
    breaks = _to_model_breaks(chart, axis_kind="annual" if is_annual else "quarterly")

    # Chronological deduplicated history (later segment wins on duplicate dates).
    by_period: dict[str, tuple[float, KpiChartPoint]] = {}
    for seg in chart.segments:
        for pt in seg.points:
            by_period[pt.period_end] = (float(pt.value), pt)
    ordered_periods = sorted(by_period)
    if not ordered_periods:
        return None
    history: list[tuple[str, float | None]] = [(p, by_period[p][0]) for p in ordered_periods]
    latest_point = by_period[ordered_periods[-1]][1]
    yoy_res = _resolve_point_yoy(
        conn,
        chart=chart,
        point=latest_point,
        effective_at=eff,
        known_at=known,
    )
    is_comparable = yoy_res.status in _COMPARABLE_YOY_STATUSES
    return LedgerKpiComparability(
        status=chart.status.value,
        segments=segments,
        breaks=breaks,
        history=history,
        comparable_yoy_delta=(
            float(yoy_res.yoy_delta) if is_comparable and yoy_res.yoy_delta is not None else None
        ),
        comparable_yoy_pct=(
            float(yoy_res.yoy_pct) if is_comparable and yoy_res.yoy_pct is not None else None
        ),
        comparable_yoy_status=yoy_res.status.value,
        comparable_yoy_bridge_kind=yoy_res.bridge_kind,
        comparable_yoy_reason=_format_yoy_reason(yoy_res),
    )
