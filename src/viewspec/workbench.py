"""Deterministic compact projection for the Fact & Metric workbench.

Callers provide already-governed tier/cache candidates.  This module uses one
level ViewSpec to attach the latest value, change, period, and source; it never
calls an LLM, invents a value, or writes state.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from report.models import CellSource
from viewspec.engine import execute_view
from viewspec.glossary import metric_display_label
from viewspec.spec import ViewSpec

_MAX_ROWS = 8
WorkbenchState = Literal["ready", "empty", "stale", "unavailable"]


@dataclass(frozen=True, slots=True)
class RankedMetricCandidate:
    token: str
    rank_source: Literal["tier", "llm"]
    why: str


@dataclass(frozen=True, slots=True)
class RankedMetricRow:
    token: str
    label: str
    ticker: str
    rank_source: Literal["tier", "llm"]
    why: str
    value: float
    prior_value: float | None
    change_pct: float | None
    unit: str | None
    as_of: str
    source: CellSource | None


@dataclass(frozen=True, slots=True)
class RankedMetricWorkbench:
    state: WorkbenchState
    rows: tuple[RankedMetricRow, ...] = ()


def build_ranked_metric_workbench(
    db_path: Path,
    tickers: list[str],
    candidates: list[RankedMetricCandidate],
) -> RankedMetricWorkbench:
    """Attach current facts to at most eight already-ranked metric candidates."""
    if not db_path.exists():
        return RankedMetricWorkbench(state="unavailable")
    selected = candidates[:_MAX_ROWS]
    if not selected:
        return RankedMetricWorkbench(state="empty")
    symbols = [ticker.strip().upper() for ticker in tickers if ticker.strip()]
    if not symbols:
        return RankedMetricWorkbench(state="empty")
    try:
        spec = ViewSpec.from_dict(
            {
                "tickers": symbols,
                "metrics": [candidate.token for candidate in selected],
                "transform": "level",
                "cadence": "quarterly",
                "periods": 8,
            }
        )
        result = execute_view(spec, db_path=db_path)
    except (OSError, sqlite3.Error, ValueError):
        return RankedMetricWorkbench(state="unavailable")

    candidate_by_token = {candidate.token: candidate for candidate in selected}
    rows: list[RankedMetricRow] = []
    for view_row in result.rows:
        candidate = candidate_by_token.get(view_row.metric.token())
        if candidate is None:
            continue
        latest_index = next(
            (
                i
                for i in range(len(view_row.cells) - 1, -1, -1)
                if view_row.cells[i].raw is not None
            ),
            None,
        )
        if latest_index is None:
            continue
        latest = view_row.cells[latest_index]
        assert latest.raw is not None
        # ``execute_view`` is a typed boundary, but its database-backed cells
        # can still carry legacy runtime data. Never let malformed provenance
        # escape the compact projection as if it were a verified CellSource.
        source = latest.source if isinstance(latest.source, CellSource) else None
        prior_index = next(
            (i for i in range(latest_index - 1, -1, -1) if view_row.cells[i].raw is not None),
            None,
        )
        prior_value = view_row.cells[prior_index].raw if prior_index is not None else None
        change_pct = (
            (latest.raw / prior_value - 1) * 100
            if prior_value is not None and prior_value != 0
            else None
        )
        rows.append(
            RankedMetricRow(
                token=candidate.token,
                label=metric_display_label(view_row.metric),
                ticker=view_row.ticker,
                rank_source=candidate.rank_source,
                why=candidate.why,
                value=latest.raw,
                prior_value=prior_value,
                change_pct=change_pct,
                unit=view_row.unit,
                as_of=result.period_labels[latest_index],
                source=source,
            )
        )
    if not rows:
        return RankedMetricWorkbench(state="stale")
    return RankedMetricWorkbench(state="ready", rows=tuple(rows[:_MAX_ROWS]))


__all__ = [
    "RankedMetricCandidate",
    "RankedMetricRow",
    "RankedMetricWorkbench",
    "WorkbenchState",
    "build_ranked_metric_workbench",
]
