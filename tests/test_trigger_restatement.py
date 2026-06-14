"""Tests for the Restatement trigger (Close-the-Loops L10 PR2).

Drives the sensor lifecycle (scan -> should_fire -> signature -> build_alert ->
draft_actions) on a synthetic schema, plus the held-name gate, the recency
window, and the materiality threshold. All deterministic — the sensor makes no
LLM/network call.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from triggers.base import UserStateContext
from triggers.restatement import RestatementTrigger


def _make_schema(db_path: Path, *, with_roster: bool = True) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT, source_type TEXT, doc_type TEXT, period_end TEXT,
                file_path TEXT, fetched_at TEXT, source_quality_tier TEXT DEFAULT 'fmp_normalized',
                accession_number TEXT, filing_date TEXT
            );
            CREATE TABLE financial_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL, period_end TEXT NOT NULL, fiscal_period_type TEXT NOT NULL,
                line_item TEXT NOT NULL, value NUMERIC NOT NULL, unit TEXT NOT NULL,
                source_doc_id INTEGER NOT NULL, supersedes_id INTEGER
            );
            CREATE TABLE kpi_definitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, name TEXT, unit TEXT,
                primary_source TEXT
            );
            CREATE TABLE kpi_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL, period_end TEXT NOT NULL, fiscal_period_type TEXT NOT NULL,
                kpi_definition_id INTEGER NOT NULL, value NUMERIC NOT NULL, unit TEXT NOT NULL,
                source_doc_id INTEGER NOT NULL, supersedes_id INTEGER
            );
            """
        )
        if with_roster:
            conn.executescript(
                """
                CREATE TABLE tracked_companies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, list_type TEXT,
                    archived_at TEXT
                );
                """
            )
        conn.commit()
    finally:
        conn.close()


def _doc(conn: sqlite3.Connection, *, fetched_at: str, doc_type: str = "sec_10k") -> int:
    cur = conn.execute(
        "INSERT INTO documents (ticker, source_type, doc_type, file_path, fetched_at, "
        "source_quality_tier, accession_number, filing_date) "
        "VALUES ('X', 'sec_xbrl', ?, '/x', ?, 'sec_official', '0001-23', '2024-02-15')",
        (doc_type, fetched_at),
    )
    return int(cur.lastrowid or 0)


def _chain_financial(
    conn: sqlite3.Connection, *, ticker: str, old_value: str, new_value: str, new_doc: int
) -> int:
    """Insert an old fact + a superseding fact; return the new fact id."""
    old = conn.execute(
        "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type, line_item, value, "
        "unit, source_doc_id) VALUES (?, '2023-03-31', 'Q1', 'revenue', ?, 'actual', 1)",
        (ticker, old_value),
    )
    old_id = int(old.lastrowid or 0)
    new = conn.execute(
        "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type, line_item, value, "
        "unit, source_doc_id, supersedes_id) VALUES (?, '2023-03-31', 'Q1', 'revenue', ?, "
        "'actual', ?, ?)",
        (ticker, new_value, new_doc, old_id),
    )
    return int(new.lastrowid or 0)


def _track(conn: sqlite3.Connection, ticker: str, list_type: str) -> None:
    conn.execute(
        "INSERT INTO tracked_companies (ticker, list_type, archived_at) VALUES (?, ?, NULL)",
        (ticker, list_type),
    )


def _recent() -> str:
    return (datetime.now(UTC).replace(tzinfo=None) - timedelta(days=2)).isoformat(sep=" ")


def _stale() -> str:
    return (datetime.now(UTC).replace(tzinfo=None) - timedelta(days=200)).isoformat(sep=" ")


_USER_STATE = UserStateContext(
    registered_kpis=[], sizing_intents=[], recent_dismissed_signatures=set()
)


@pytest.fixture
def trig() -> RestatementTrigger:
    return RestatementTrigger()


def test_scan_finds_material_held_restatement(tmp_path: Path, trig: RestatementTrigger) -> None:
    p = tmp_path / "db.sqlite"
    _make_schema(p)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    try:
        _track(conn, "NU", "portfolio")
        doc = _doc(conn, fetched_at=_recent())
        new_id = _chain_financial(conn, ticker="NU", old_value="100", new_value="88", new_doc=doc)
        conn.commit()

        candidates = trig.scan("NU", conn)
        assert len(candidates) == 1
        c = candidates[0]
        assert c.evidence["new_fact_id"] == new_id
        assert c.evidence["delta_pct"] == pytest.approx(12.0)
        assert trig.should_fire(c, _USER_STATE)

        alert = trig.build_alert(c, None)
        assert alert.trigger_kind == "restatement"
        assert alert.memo_text is not None
        assert "restated from" in alert.memo_text
        assert "NU" in alert.memo_text
        assert alert.signature_sha  # non-empty

        actions = trig.draft_actions(alert, c)
        assert len(actions) == 1
        assert actions[0].action_kind == "thesis_update"
        assert actions[0].payload["restatement"] is True
    finally:
        conn.close()


def test_scan_skips_non_held(tmp_path: Path, trig: RestatementTrigger) -> None:
    p = tmp_path / "db.sqlite"
    _make_schema(p)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    try:
        _track(conn, "NU", "watchlist")  # watched, not held
        doc = _doc(conn, fetched_at=_recent())
        _chain_financial(conn, ticker="NU", old_value="100", new_value="88", new_doc=doc)
        conn.commit()
        assert trig.scan("NU", conn) == []
    finally:
        conn.close()


def test_scan_skips_stale_restatement(tmp_path: Path, trig: RestatementTrigger) -> None:
    p = tmp_path / "db.sqlite"
    _make_schema(p)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    try:
        _track(conn, "NU", "portfolio")
        doc = _doc(conn, fetched_at=_stale())  # outside the recency window
        _chain_financial(conn, ticker="NU", old_value="100", new_value="88", new_doc=doc)
        conn.commit()
        assert trig.scan("NU", conn) == []
    finally:
        conn.close()


def test_should_fire_immaterial_delta(tmp_path: Path, trig: RestatementTrigger) -> None:
    p = tmp_path / "db.sqlite"
    _make_schema(p)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    try:
        _track(conn, "NU", "portfolio")
        doc = _doc(conn, fetched_at=_recent())
        # 100 -> 100.3 is 0.3% — below the 1% push bar.
        _chain_financial(conn, ticker="NU", old_value="100", new_value="100.3", new_doc=doc)
        conn.commit()
        candidates = trig.scan("NU", conn)
        assert len(candidates) == 1  # scan surfaces any value change
        assert not trig.should_fire(candidates[0], _USER_STATE)  # gate rejects immaterial
    finally:
        conn.close()


def test_scan_permissive_without_roster(tmp_path: Path, trig: RestatementTrigger) -> None:
    p = tmp_path / "db.sqlite"
    _make_schema(p, with_roster=False)  # no tracked_companies table
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    try:
        doc = _doc(conn, fetched_at=_recent())
        _chain_financial(conn, ticker="NU", old_value="100", new_value="88", new_doc=doc)
        conn.commit()
        assert len(trig.scan("NU", conn)) == 1  # permissive: surfaces rather than dropping
    finally:
        conn.close()


def test_signature_is_stable_and_specific(tmp_path: Path, trig: RestatementTrigger) -> None:
    p = tmp_path / "db.sqlite"
    _make_schema(p)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    try:
        _track(conn, "NU", "portfolio")
        doc = _doc(conn, fetched_at=_recent())
        _chain_financial(conn, ticker="NU", old_value="100", new_value="88", new_doc=doc)
        conn.commit()
        a = trig.build_alert(trig.scan("NU", conn)[0], None)
        b = trig.build_alert(trig.scan("NU", conn)[0], None)
        assert a.signature_sha == b.signature_sha  # same supersede → same signature
    finally:
        conn.close()


def test_scan_finds_kpi_restatement(tmp_path: Path, trig: RestatementTrigger) -> None:
    p = tmp_path / "db.sqlite"
    _make_schema(p)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    try:
        _track(conn, "NU", "portfolio")
        conn.execute(
            "INSERT INTO kpi_definitions (id, ticker, name, unit, primary_source) "
            "VALUES (5, 'NU', 'Net Margin', 'percent', 'fmp')"
        )
        doc = _doc(conn, fetched_at=_recent())
        old = conn.execute(
            "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, "
            "value, unit, source_doc_id) VALUES ('NU', '2023-03-31', 'Q1', 5, '20', 'percent', 1)"
        )
        old_id = int(old.lastrowid or 0)
        conn.execute(
            "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, "
            "value, unit, source_doc_id, supersedes_id) VALUES ('NU', '2023-03-31', 'Q1', 5, "
            "'17', 'percent', ?, ?)",
            (doc, old_id),
        )
        conn.commit()
        candidates = trig.scan("NU", conn)
        assert len(candidates) == 1
        c = candidates[0]
        assert c.evidence["fact_table"] == "kpi_facts"
        assert c.evidence["line_item"] == "Net Margin"
        assert trig.should_fire(c, _USER_STATE)
    finally:
        conn.close()
