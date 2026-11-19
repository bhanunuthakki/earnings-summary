"""Admitted KPI evidence for the DCF assumption prompt.

The assumption call needs the business drivers a company reports, not only its
income statement. This module reads every KPI defined for a ticker through the
provenance-aware loader (semantic admission applies), keeps the series with the
most admitted observations, and lists defined KPIs that have none as capture
gaps. It is read-only and calls no model.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from timeseries.loaders import SourcedObservation, load_kpi_series_with_provenance

_ALL_PERIOD_TYPES = ("Q1", "Q2", "Q3", "Q4", "FY", "H1", "H2", "TTM")
_NAME_LIMIT = 80


@dataclass(frozen=True, slots=True)
class KpiEvidence:
    lines: tuple[str, ...]  # one line per admitted series, most observations first
    capture_gaps: tuple[str, ...]  # KPI names defined for the ticker with no admitted fact
    omitted: int  # admitted series left out by the series limit


def _number(value: float) -> str:
    if not math.isfinite(value):
        return "?"
    magnitude = abs(value)
    if magnitude >= 1e9:
        return f"{value / 1e9:,.2f}bn"
    if magnitude >= 1e6:
        return f"{value / 1e6:,.2f}m"
    if magnitude >= 100:
        return f"{value:,.0f}"
    return f"{value:,.4g}"


def _source(obs: SourcedObservation) -> str:
    prov = obs.provenance
    doc = str(prov.get("doc_type") or prov.get("source") or "unknown source")
    filed = prov.get("filing_date")
    return f"{doc} {str(filed)[:10]}" if filed else doc


def render_series(name: str, series: Sequence[SourcedObservation], *, max_points: int) -> str:
    recent = list(series)[-max_points:]
    unit = recent[-1].unit or ""
    points = "; ".join(f"{str(o.period_end)[:10]} {_number(o.value)}" for o in recent)
    label = name[:_NAME_LIMIT] + (f" ({unit})" if unit else "")
    return f"  {label}: {points} [latest: {_source(recent[-1])}]"


def _defined_kpis(db_path: Path, ticker: str) -> list[str]:
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    try:
        rows = conn.execute(
            "SELECT name FROM kpi_definitions WHERE UPPER(ticker) = ? ORDER BY name",
            (ticker.upper(),),
        ).fetchall()
    except sqlite3.OperationalError:
        return []  # no KPI table in this database: no KPI evidence, not an error
    finally:
        conn.close()
    return [str(row[0]) for row in rows]


def kpi_evidence(
    db_path: Path,
    ticker: str,
    *,
    repo_root: Path | None = None,
    max_series: int = 15,
    max_points: int = 8,
) -> KpiEvidence:
    """Admitted KPI series for ``ticker`` and the defined KPIs that have none."""
    if not db_path.exists():
        return KpiEvidence((), (), 0)
    loaded: list[tuple[str, list[SourcedObservation]]] = []
    gaps: list[str] = []
    for name in _defined_kpis(db_path, ticker):
        series = load_kpi_series_with_provenance(
            ticker.upper(), name, repo_root, db_path=db_path, period_types=_ALL_PERIOD_TYPES
        )
        if series:
            loaded.append((name, series))
        else:
            gaps.append(name[:_NAME_LIMIT])
    loaded.sort(key=lambda item: (-len(item[1]), item[0]))
    kept = loaded[:max_series]
    lines = tuple(render_series(n, s, max_points=max_points) for n, s in kept)
    return KpiEvidence(lines, tuple(gaps), len(loaded) - len(kept))
