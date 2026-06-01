"""Unit tests for the shared KPI-label → definition-name resolver.

`compute.kpi_resolver` is the single resolver behind the §3 chart loader, the §2
break-rule ledger, and the break-rule evaluator. It guards the NU-class bug where
a holdings/break-rule label ("Monthly ARPAC") exact-matched a near-empty
*duplicate* definition (2 stray rows) instead of the fully-populated canonical
"Monthly ARPAC (USD)" (12 rows), so the consumer rendered / evaluated an almost
empty series even though the data was present one name-key away.

The resolver matches on a parenthetical-insensitive normalized name and prefers
the definition with the MOST observations, so a fragmented duplicate can never
shadow the canonical series. `period_types` scopes the richness count to the
buckets the caller will actually read (quarterly for the chart, every period for
the break-rule paths).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from compute.kpi_resolver import (  # noqa: E402
    QUARTERLY_FACT_PERIOD_TYPES,
    normalize_kpi_name,
    resolve_kpi_definition_name,
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
    conn: sqlite3.Connection,
    ticker: str,
    def_id: int,
    ends: list[str],
    *,
    period_type: str | None = None,
    unit: str = "actual",
) -> None:
    """Insert one fact per period_end. `period_type` overrides the derived 'Qn'."""
    for end in ends:
        fpt = period_type or f"Q{(int(end[5:7]) - 1) // 3 + 1}"
        conn.execute(
            "INSERT INTO kpi_facts (ticker, period_end, value, unit, kpi_definition_id, "
            "fiscal_period_type) VALUES (?, ?, ?, ?, ?, ?)",
            (ticker, end, 1.0, unit, def_id, fpt),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# normalize_kpi_name
# ---------------------------------------------------------------------------


def test_normalize_strips_trailing_parenthetical() -> None:
    assert normalize_kpi_name("Monthly ARPAC (USD)") == "monthly arpac"
    assert normalize_kpi_name("ROE (annualized, consolidated)") == "roe"
    assert normalize_kpi_name("Risk-adjusted NIM (NIM minus cost of risk)") == "risk-adjusted nim"


def test_normalize_collapses_whitespace_and_case() -> None:
    assert normalize_kpi_name("  Monthly   ARPAC  ") == "monthly arpac"


def test_normalize_strips_nested_trailing_parentheticals() -> None:
    # Two stacked trailing qualifiers both peel off.
    assert normalize_kpi_name("ROE (annualized) (consolidated)") == "roe"


def test_normalize_keeps_distinct_metrics_distinct() -> None:
    # "NIM" must NOT collapse onto "Risk-adjusted NIM ...".
    assert normalize_kpi_name("NIM") != normalize_kpi_name(
        "Risk-adjusted NIM (NIM minus cost of risk)"
    )


# ---------------------------------------------------------------------------
# resolve_kpi_definition_name
# ---------------------------------------------------------------------------


def test_short_label_resolves_to_richest_canonical_definition() -> None:
    conn = _make_db()
    canonical = _add_def(conn, "NU", "Monthly ARPAC (USD)")
    duplicate = _add_def(conn, "NU", "Monthly ARPAC")
    _add_facts(conn, "NU", canonical, _QUARTER_ENDS)  # 12 obs
    _add_facts(conn, "NU", duplicate, _QUARTER_ENDS[5:7])  # 2 stray obs

    # The short holdings label must reach the 12-obs canonical series.
    assert resolve_kpi_definition_name(conn, "NU", "Monthly ARPAC") == "Monthly ARPAC (USD)"
    # The canonical label resolves to itself.
    assert resolve_kpi_definition_name(conn, "NU", "Monthly ARPAC (USD)") == "Monthly ARPAC (USD)"


def test_richest_wins_even_when_exact_duplicate_present() -> None:
    """Exactness is only a tie-breaker — observation count dominates."""
    conn = _make_db()
    rich = _add_def(conn, "NU", "ROE (annualized, consolidated)")
    sparse_exact = _add_def(conn, "NU", "ROE")
    _add_facts(conn, "NU", rich, _QUARTER_ENDS)  # 12 obs
    _add_facts(conn, "NU", sparse_exact, _QUARTER_ENDS[:1])  # 1 obs, exact name
    assert resolve_kpi_definition_name(conn, "NU", "ROE") == "ROE (annualized, consolidated)"


def test_exactness_breaks_ties_at_equal_obs() -> None:
    conn = _make_db()
    exact = _add_def(conn, "NU", "Activity Rate")
    qualified = _add_def(conn, "NU", "Activity Rate (consolidated)")
    _add_facts(conn, "NU", exact, _QUARTER_ENDS[:3])
    _add_facts(conn, "NU", qualified, _QUARTER_ENDS[3:6])  # same count (3)
    assert resolve_kpi_definition_name(conn, "NU", "Activity Rate") == "Activity Rate"


def test_unrelated_label_does_not_match() -> None:
    conn = _make_db()
    nim = _add_def(conn, "NU", "Risk-adjusted NIM (NIM minus cost of risk)")
    _add_facts(conn, "NU", nim, _QUARTER_ENDS)
    # "NIM" is a different metric — must not borrow the risk-adjusted series.
    assert resolve_kpi_definition_name(conn, "NU", "NIM") is None


def test_resolution_is_ticker_scoped() -> None:
    conn = _make_db()
    other = _add_def(conn, "MELI", "Monthly ARPAC (USD)")
    _add_facts(conn, "MELI", other, _QUARTER_ENDS)
    assert resolve_kpi_definition_name(conn, "NU", "Monthly ARPAC") is None


def test_definition_without_facts_is_not_resolvable() -> None:
    """A label that only matches a factless definition resolves to None — the
    consumer then renders/evaluates an empty series, same as before."""
    conn = _make_db()
    _add_def(conn, "NU", "Monthly ARPAC (USD)")  # no facts inserted
    assert resolve_kpi_definition_name(conn, "NU", "Monthly ARPAC") is None


# ---------------------------------------------------------------------------
# period_types — richness is measured over the rows the caller will read.
# ---------------------------------------------------------------------------


def test_period_types_scopes_richness_count() -> None:
    """The quarterly chart loader and the all-period break-rule paths can pick
    different definitions for the same label when richness differs by cadence.

    "Revenue" has 4 quarterly facts; "Revenue (GAAP)" has 8 annual facts only.
    Counting every period (break-rule paths) picks the annual-rich definition;
    counting quarterly buckets (chart loader) picks the quarterly one — and the
    annual-only definition, having zero quarterly rows, is not even a candidate.
    """
    conn = _make_db()
    quarterly = _add_def(conn, "X", "Revenue")
    annual = _add_def(conn, "X", "Revenue (GAAP)")
    _add_facts(conn, "X", quarterly, _QUARTER_ENDS[:4])  # 4 quarterly
    _add_facts(
        conn,
        "X",
        annual,
        [f"{y}-12-31" for y in range(2016, 2024)],  # 8 annual
        period_type="FY",
    )

    # All-period count (default, break-rule paths): annual-rich def wins.
    assert resolve_kpi_definition_name(conn, "X", "Revenue") == "Revenue (GAAP)"
    # Quarterly-scoped count (chart loader): the quarterly def wins.
    assert (
        resolve_kpi_definition_name(conn, "X", "Revenue", period_types=QUARTERLY_FACT_PERIOD_TYPES)
        == "Revenue"
    )


def test_quarterly_scope_ignores_annual_only_definition() -> None:
    """With the quarterly scope, a definition that has only annual facts is never
    returned (it contributes zero quarterly observations)."""
    conn = _make_db()
    annual = _add_def(conn, "X", "Free Cash Flow (annual)")
    _add_facts(conn, "X", annual, [f"{y}-12-31" for y in range(2018, 2024)], period_type="FY")
    assert (
        resolve_kpi_definition_name(
            conn, "X", "Free Cash Flow", period_types=QUARTERLY_FACT_PERIOD_TYPES
        )
        is None
    )
    # …but the all-period break-rule scope still finds it.
    assert resolve_kpi_definition_name(conn, "X", "Free Cash Flow") == "Free Cash Flow (annual)"
