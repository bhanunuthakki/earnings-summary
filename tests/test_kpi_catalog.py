"""Unit tests for src/user_state/kpi_catalog.py (auto-seed correctness #1).

allowed_kpi_names returns the closed set of exact kpi_definitions.name strings
with >= min_facts quarterly kpi_facts rows. Uses a plain sqlite tmp DB (no
documents table — the catalog query doesn't need one). Covers:
  * a name with >= 8 quarterly facts is included;
  * a name with 4 facts is excluded (below the fireable floor);
  * FY-only facts are excluded (only Q1-Q4 count);
  * ticker isolation;
  * exact, case-sensitive string membership;
  * best-effort empty set on a missing DB / missing tables.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from user_state.kpi_catalog import allowed_kpi_names

_DDL_KPI_DEFINITIONS = "CREATE TABLE kpi_definitions (id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, name TEXT NOT NULL)"
_DDL_KPI_FACTS = "CREATE TABLE kpi_facts (id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, period_end TEXT NOT NULL, fiscal_period_type TEXT NOT NULL, kpi_definition_id INTEGER NOT NULL, value REAL NOT NULL, unit TEXT)"
_INSERT_KPI_FACT = "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, value, unit) VALUES (?, ?, ?, ?, ?, ?)"


def _create_schema(conn: sqlite3.Connection) -> None:
    _ = conn.execute(_DDL_KPI_DEFINITIONS)
    _ = conn.execute(_DDL_KPI_FACTS)


def _seed(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    kpi_name: str,
    n_quarters: int,
    fiscal_period_type: str = "Q",
) -> None:
    """Insert one kpi_definitions row + ``n_quarters`` kpi_facts rows.

    When ``fiscal_period_type == 'Q'`` the rows cycle Q1..Q4 (real quarterly
    data); pass an explicit type (e.g. ``'FY'``) to seed non-quarterly rows.
    """
    cur = conn.execute(
        "INSERT INTO kpi_definitions (ticker, name) VALUES (?, ?)", (ticker, kpi_name)
    )
    kd_id = int(cur.lastrowid or 0)
    for i in range(n_quarters):
        fpt = f"Q{(i % 4) + 1}" if fiscal_period_type == "Q" else fiscal_period_type
        _ = conn.execute(
            _INSERT_KPI_FACT,
            (ticker, f"2024-{((i % 4) + 1) * 3:02d}-28", fpt, kd_id, 10.0 + i, "%"),
        )
    conn.commit()


def _make_db(tmp_path: Path) -> Path:
    path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(path))
    try:
        _create_schema(conn)
        conn.commit()
    finally:
        conn.close()
    return path


def test_includes_name_with_at_least_eight_quarterly_facts(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        _seed(conn, ticker="NU", kpi_name="NIM", n_quarters=8)
    finally:
        conn.close()
    assert allowed_kpi_names("NU", db) == {"NIM"}


def test_excludes_name_with_only_four_facts(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        _seed(conn, ticker="NU", kpi_name="NIM", n_quarters=8)
        _seed(conn, ticker="NU", kpi_name="Sparse KPI", n_quarters=4)
    finally:
        conn.close()
    names = allowed_kpi_names("NU", db)
    assert "NIM" in names
    assert "Sparse KPI" not in names


def test_excludes_fy_only_facts(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        # 10 FY rows — plenty of rows, but none are quarterly, so it must
        # not pass the fireable-fact floor.
        _seed(conn, ticker="NU", kpi_name="Annual-only KPI", n_quarters=10, fiscal_period_type="FY")
    finally:
        conn.close()
    assert allowed_kpi_names("NU", db) == set()


def test_ticker_isolation(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        _seed(conn, ticker="NU", kpi_name="NIM", n_quarters=8)
        _seed(conn, ticker="MELI", kpi_name="GMV", n_quarters=8)
    finally:
        conn.close()
    assert allowed_kpi_names("NU", db) == {"NIM"}
    assert allowed_kpi_names("MELI", db) == {"GMV"}


def test_exact_case_sensitive_membership(tmp_path: Path) -> None:
    """The returned strings are byte-exact — this is what load_kpi_series joins
    on (BINARY collation), so a case variant is NOT a member."""
    db = _make_db(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        _seed(conn, ticker="NU", kpi_name="Net Interest Margin", n_quarters=8)
    finally:
        conn.close()
    names = allowed_kpi_names("NU", db)
    assert "Net Interest Margin" in names
    assert "net interest margin" not in names
    assert "NET INTEREST MARGIN" not in names


def test_min_facts_override(tmp_path: Path) -> None:
    """A lower min_facts widens the set; the boundary is inclusive (>=)."""
    db = _make_db(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        _seed(conn, ticker="NU", kpi_name="Four-Q KPI", n_quarters=4)
    finally:
        conn.close()
    assert allowed_kpi_names("NU", db) == set()  # default floor of 8 excludes it
    assert allowed_kpi_names("NU", db, min_facts=4) == {"Four-Q KPI"}


def test_missing_db_returns_empty_set(tmp_path: Path) -> None:
    # Best-effort: a path that doesn't exist, and None, both degrade to set().
    assert allowed_kpi_names("NU", tmp_path / "does_not_exist.db") == set()
    assert allowed_kpi_names("NU", None) == set()


def test_missing_tables_returns_empty_set(tmp_path: Path) -> None:
    # DB exists but has neither kpi_facts nor kpi_definitions → set(), no raise.
    path = tmp_path / "empty.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.commit()
    finally:
        conn.close()
    assert allowed_kpi_names("NU", path) == set()
