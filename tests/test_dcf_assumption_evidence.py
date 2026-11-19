"""KPI evidence for the DCF assumption prompt. Synthetic values only."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from dcf.assumption_evidence import kpi_evidence, render_series
from timeseries.loaders import SourcedObservation


def _obs(day: str, value: float) -> SourcedObservation:
    return SourcedObservation(
        period_end=datetime.fromisoformat(day),
        value=value,
        unit="percent",
        provenance={"doc_type": "10-Q", "filing_date": "2026-10-29"},
    )


def test_series_line_keeps_recent_points_and_source() -> None:
    series = [_obs(f"2026-0{m}-15", float(m)) for m in range(1, 10)]
    line = render_series("Paid clicks change", series, max_points=3)
    assert line.startswith("  Paid clicks change (percent): 2026-07-15 7;")
    assert "2026-01-15" not in line
    assert line.endswith("[latest: 10-Q 2026-10-29]")


def test_defined_kpi_without_admitted_facts_is_a_capture_gap(
    tmp_path: Path, migrated_db: Callable[[Path], Path]
) -> None:
    database = migrated_db(tmp_path / "evidence.sqlite")
    with sqlite3.connect(database) as conn:
        conn.execute(
            "INSERT INTO kpi_definitions (ticker, name, unit, primary_source) "
            "VALUES ('SYN', 'Paid clicks change', 'percent', 'filing')"
        )
    evidence = kpi_evidence(database, "syn", repo_root=tmp_path)
    assert evidence.lines == ()
    assert evidence.capture_gaps == ("Paid clicks change",)


def test_missing_database_gives_no_evidence(tmp_path: Path) -> None:
    evidence = kpi_evidence(tmp_path / "absent.sqlite", "SYN")
    assert (evidence.lines, evidence.capture_gaps, evidence.omitted) == ((), (), 0)
