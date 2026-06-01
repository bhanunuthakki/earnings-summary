"""Chart-priority → kpi_definition resolution in the §3 financials builder.

Regression guard for the NU bug where the holdings `chart_priorities` label
("Monthly ARPAC") resolved by exact name to a near-empty *duplicate* definition
(2 stray rows) instead of the fully-populated canonical "Monthly ARPAC (USD)"
(12 rows), so the chart rendered as an empty "sparse series" placeholder even
though the data was present one name-key away.

The resolver now matches on a parenthetical-insensitive normalized name and
prefers the definition with the MOST observations, so a fragmented duplicate can
never shadow the canonical series.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from report.sections.financials import (  # noqa: E402
    _kpi_series_for,
    _normalize_kpi_name,
    _resolve_kpi_definition_name,
)

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
    conn: sqlite3.Connection, ticker: str, def_id: int, ends: list[str], unit: str = "actual"
) -> None:
    for end in ends:
        quarter = (int(end[5:7]) - 1) // 3 + 1
        conn.execute(
            "INSERT INTO kpi_facts (ticker, period_end, value, unit, kpi_definition_id, "
            "fiscal_period_type) VALUES (?, ?, ?, ?, ?, ?)",
            (ticker, end, 1.0, unit, def_id, f"Q{quarter}"),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# _normalize_kpi_name
# ---------------------------------------------------------------------------


def test_normalize_strips_trailing_parenthetical() -> None:
    assert _normalize_kpi_name("Monthly ARPAC (USD)") == "monthly arpac"
    assert _normalize_kpi_name("ROE (annualized, consolidated)") == "roe"
    assert _normalize_kpi_name("Risk-adjusted NIM (NIM minus cost of risk)") == "risk-adjusted nim"


def test_normalize_collapses_whitespace_and_case() -> None:
    assert _normalize_kpi_name("  Monthly   ARPAC  ") == "monthly arpac"


def test_normalize_keeps_distinct_metrics_distinct() -> None:
    # "NIM" must NOT collapse onto "Risk-adjusted NIM ...".
    assert _normalize_kpi_name("NIM") != _normalize_kpi_name(
        "Risk-adjusted NIM (NIM minus cost of risk)"
    )


# ---------------------------------------------------------------------------
# _resolve_kpi_definition_name
# ---------------------------------------------------------------------------


def test_short_label_resolves_to_richest_canonical_definition() -> None:
    conn = _make_db()
    canonical = _add_def(conn, "NU", "Monthly ARPAC (USD)")
    duplicate = _add_def(conn, "NU", "Monthly ARPAC")
    _add_facts(conn, "NU", canonical, _QUARTER_ENDS)  # 12 obs
    _add_facts(conn, "NU", duplicate, _QUARTER_ENDS[5:7])  # 2 stray obs

    # The short holdings label must reach the 12-obs canonical series.
    assert _resolve_kpi_definition_name(conn, "NU", "Monthly ARPAC") == "Monthly ARPAC (USD)"
    # The canonical label resolves to itself.
    assert _resolve_kpi_definition_name(conn, "NU", "Monthly ARPAC (USD)") == "Monthly ARPAC (USD)"


def test_richest_wins_even_when_exact_duplicate_present() -> None:
    """Exactness is only a tie-breaker — observation count dominates."""
    conn = _make_db()
    rich = _add_def(conn, "NU", "ROE (annualized, consolidated)")
    sparse_exact = _add_def(conn, "NU", "ROE")
    _add_facts(conn, "NU", rich, _QUARTER_ENDS)  # 12 obs
    _add_facts(conn, "NU", sparse_exact, _QUARTER_ENDS[:1])  # 1 obs, exact name
    assert _resolve_kpi_definition_name(conn, "NU", "ROE") == "ROE (annualized, consolidated)"


def test_exactness_breaks_ties_at_equal_obs() -> None:
    conn = _make_db()
    exact = _add_def(conn, "NU", "Activity Rate")
    qualified = _add_def(conn, "NU", "Activity Rate (consolidated)")
    _add_facts(conn, "NU", exact, _QUARTER_ENDS[:3])
    _add_facts(conn, "NU", qualified, _QUARTER_ENDS[3:6])  # same count (3)
    assert _resolve_kpi_definition_name(conn, "NU", "Activity Rate") == "Activity Rate"


def test_unrelated_label_does_not_match() -> None:
    conn = _make_db()
    nim = _add_def(conn, "NU", "Risk-adjusted NIM (NIM minus cost of risk)")
    _add_facts(conn, "NU", nim, _QUARTER_ENDS)
    # "NIM" is a different metric — must not borrow the risk-adjusted series.
    assert _resolve_kpi_definition_name(conn, "NU", "NIM") is None


def test_resolution_is_ticker_scoped() -> None:
    conn = _make_db()
    other = _add_def(conn, "MELI", "Monthly ARPAC (USD)")
    _add_facts(conn, "MELI", other, _QUARTER_ENDS)
    assert _resolve_kpi_definition_name(conn, "NU", "Monthly ARPAC") is None


# ---------------------------------------------------------------------------
# _kpi_series_for (end-to-end)
# ---------------------------------------------------------------------------


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
