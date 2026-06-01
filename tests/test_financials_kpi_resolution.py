"""End-to-end §3 chart series resolution in the financials builder.

Regression guard for the NU bug where the holdings `chart_priorities` label
("Monthly ARPAC") resolved by exact name to a near-empty *duplicate* definition
(2 stray rows) instead of the fully-populated canonical "Monthly ARPAC (USD)"
(12 rows), so the chart rendered as an empty "sparse series" placeholder even
though the data was present one name-key away.

`_kpi_series_for` now delegates name resolution to the shared
`compute.kpi_resolver` (parenthetical-insensitive, richest-definition-wins) — the
resolver's own unit tests live in `tests/test_kpi_resolver.py`. These tests pin
the financials-specific wiring: that the builder pulls the full history through
the resolver and keeps deduping coexisting per-period sources to the latest.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from report.sections.financials import _kpi_series_for  # noqa: E402

_QUARTER_ENDS = [
    "2023-03-31",
    "2023-06-30",
    "2023-09-30",
    "2023-12-31",
    "2024-03-31",
    "2024-06-30",
    "2024-09-30",
    "2024-12-31",
    "2025-03-31",
    "2025-06-30",
    "2025-09-30",
    "2025-12-31",
]


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE kpi_definitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker VARCHAR NOT NULL,
            name VARCHAR NOT NULL,
            unit VARCHAR
        );
        CREATE TABLE kpi_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker VARCHAR NOT NULL,
            period_end VARCHAR NOT NULL,
            value NUMERIC,
            unit VARCHAR,
            kpi_definition_id INTEGER NOT NULL,
            fiscal_period_type VARCHAR NOT NULL,
            source_doc_id INTEGER NOT NULL DEFAULT 1
        );
        """
    )
    return conn


def _add_def(conn: sqlite3.Connection, ticker: str, name: str, unit: str = "actual") -> int:
    cur = conn.execute(
        "INSERT INTO kpi_definitions (ticker, name, unit) VALUES (?, ?, ?)",
        (ticker, name, unit),
    )
    row_id = cur.lastrowid
    assert row_id is not None
    return row_id


def _add_facts(
    conn: sqlite3.Connection,
    ticker: str,
    def_id: int,
    ends: list[str],
    *,
    value: float = 1.0,
    source_doc_id: int = 1,
    unit: str = "actual",
) -> None:
    for end in ends:
        quarter = (int(end[5:7]) - 1) // 3 + 1
        conn.execute(
            "INSERT INTO kpi_facts (ticker, period_end, value, unit, kpi_definition_id, "
            "fiscal_period_type, source_doc_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ticker, end, value, unit, def_id, f"Q{quarter}", source_doc_id),
        )
    conn.commit()


def test_series_pulls_full_history_through_resolver() -> None:
    conn = _make_db()
    canonical = _add_def(conn, "NU", "Monthly ARPAC (USD)", unit="actual")
    duplicate = _add_def(conn, "NU", "Monthly ARPAC", unit="actual")
    _add_facts(conn, "NU", canonical, _QUARTER_ENDS)
    _add_facts(conn, "NU", duplicate, _QUARTER_ENDS[5:7])

    labels = [f"{e[:4]} Q{(int(e[5:7]) - 1) // 3 + 1}" for e in _QUARTER_ENDS]
    series = _kpi_series_for(conn, "NU", "Monthly ARPAC", labels[-12:], labels)
    assert series is not None
    assert series.name == "Monthly ARPAC (USD)"
    # Full 12-quarter history, not the 2-row duplicate → chartable, not "sparse".
    assert sum(1 for v in series.levels_full if v is not None) == 12


def test_series_dedups_coexisting_sources_to_latest() -> None:
    """An LLM-brief value and a later IR-spreadsheet restatement coexist as two
    rows for one period; the series must surface the latest-ingested (highest
    source_doc_id) value and still show exactly one observation per quarter —
    the dedup behavior the refactor had to preserve."""
    conn = _make_db()
    canonical = _add_def(conn, "NU", "Monthly ARPAC (USD)")
    _add_facts(conn, "NU", canonical, _QUARTER_ENDS, value=10.0, source_doc_id=1)
    # Restate the most recent quarter from a higher-id source.
    _add_facts(conn, "NU", canonical, _QUARTER_ENDS[-1:], value=20.0, source_doc_id=2)

    labels = [f"{e[:4]} Q{(int(e[5:7]) - 1) // 3 + 1}" for e in _QUARTER_ENDS]
    series = _kpi_series_for(conn, "NU", "Monthly ARPAC", labels[-12:], labels)
    assert series is not None
    assert series.levels_full[-1] == 20.0  # restated value wins
    assert sum(1 for v in series.levels_full if v is not None) == 12  # one obs/quarter
