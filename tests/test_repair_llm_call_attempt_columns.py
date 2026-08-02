"""Tests for the 0212 attempt-attribution column repair.

The important cases are the two real-world starting states — a database that
never received 0212 (production) and one where it applied normally (every fresh
clone) — plus the guarantee that re-running changes nothing.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "repair_llm_call_attempt_columns",
    PROJECT_ROOT / "execution" / "repair_llm_call_attempt_columns.py",
)
assert _spec is not None and _spec.loader is not None
repair_mod = importlib.util.module_from_spec(_spec)
sys.modules["repair_llm_call_attempt_columns"] = repair_mod
_spec.loader.exec_module(repair_mod)

_ALL_0212 = (
    "auth_class",
    "attempt_count",
    "retry_count",
    "fallback_from_provider",
    "fallback_from_transport",
)


def _columns(conn: sqlite3.Connection) -> set[str]:
    return {str(r[1]) for r in conn.execute('PRAGMA table_info("llm_calls")')}


@pytest.fixture
def prod_shape(tmp_path: Path) -> Path:
    """Production: 0210 counters present, all five 0212 columns absent."""
    p = tmp_path / "prod.db"
    conn = sqlite3.connect(p)
    conn.execute(
        "CREATE TABLE llm_calls (id INTEGER PRIMARY KEY, purpose TEXT, "
        "attempts INTEGER, retries INTEGER)"
    )
    conn.executemany(
        "INSERT INTO llm_calls(purpose, attempts, retries) VALUES (?,?,?)",
        [("a", 3, 2), ("b", 1, 0), ("c", None, None)],
    )
    conn.commit()
    conn.close()
    return p


@pytest.fixture
def applied_shape(tmp_path: Path) -> Path:
    """A database where 0212 ran normally: all five present and backfilled."""
    p = tmp_path / "applied.db"
    conn = sqlite3.connect(p)
    conn.execute(
        "CREATE TABLE llm_calls (id INTEGER PRIMARY KEY, purpose TEXT, "
        "attempts INTEGER, retries INTEGER, auth_class TEXT, attempt_count INTEGER, "
        "retry_count INTEGER, fallback_from_provider TEXT, fallback_from_transport TEXT)"
    )
    conn.execute(
        "INSERT INTO llm_calls(purpose, attempts, retries, auth_class, attempt_count, "
        "retry_count) VALUES ('a', 3, 2, 'subscription', 9, 8)"
    )
    conn.commit()
    conn.close()
    return p


def test_prod_shape_gains_all_five_columns_and_recovers_the_counts(prod_shape: Path) -> None:
    conn = sqlite3.connect(prod_shape)
    try:
        result = repair_mod.repair(conn)
        assert set(result["added"]) == set(_ALL_0212)
        assert _ALL_0212[0] in _columns(conn)
        # attempt_count/retry_count recovered from the 0210 counters...
        rows = conn.execute(
            "SELECT purpose, attempt_count, retry_count FROM llm_calls ORDER BY purpose"
        ).fetchall()
        assert rows == [("a", 3, 2), ("b", 1, 0), ("c", None, None)]
        # ...and the unrecoverable facts stay NULL rather than being invented.
        auth = conn.execute("SELECT DISTINCT auth_class FROM llm_calls").fetchall()
        assert auth == [(None,)]
    finally:
        conn.close()


def test_row_with_null_counters_is_not_defaulted_to_zero(prod_shape: Path) -> None:
    """A NULL attempts must stay NULL. Defaulting it to 0 would fabricate a
    measurement — the silent-degradation failure this repair exists to undo."""
    conn = sqlite3.connect(prod_shape)
    try:
        repair_mod.repair(conn)
        row = conn.execute(
            "SELECT attempt_count, retry_count FROM llm_calls WHERE purpose='c'"
        ).fetchone()
        assert row == (None, None)
    finally:
        conn.close()


def test_rerunning_is_a_no_op(prod_shape: Path) -> None:
    conn = sqlite3.connect(prod_shape)
    try:
        repair_mod.repair(conn)
        before = _columns(conn)
        second = repair_mod.repair(conn)
        assert second["added"] == []
        assert all(n == 0 for n in second["backfilled"].values())
        assert _columns(conn) == before
    finally:
        conn.close()


def test_already_applied_database_is_untouched(applied_shape: Path) -> None:
    """The common case — every fresh clone. The repair must not disturb values
    0212 legitimately wrote."""
    conn = sqlite3.connect(applied_shape)
    try:
        plan = repair_mod.inspect(conn)
        assert plan["missing"] == []
        result = repair_mod.repair(conn)
        assert result["added"] == []
        # The pre-existing 9/8 must survive: backfill only touches NULL targets.
        assert conn.execute("SELECT attempt_count, retry_count FROM llm_calls").fetchone() == (9, 8)
    finally:
        conn.close()


def test_inspect_reports_the_gap_without_writing(prod_shape: Path) -> None:
    conn = sqlite3.connect(prod_shape)
    try:
        plan = repair_mod.inspect(conn)
        assert plan["table_present"] is True
        assert set(plan["missing"]) == set(_ALL_0212)
        assert _columns(conn).isdisjoint(_ALL_0212), "inspect must not write"
    finally:
        conn.close()


def test_missing_source_columns_skip_backfill_rather_than_erroring(tmp_path: Path) -> None:
    """A database without 0210's counters still gets its columns; the counts
    stay NULL and the skip is reported, not swallowed."""
    p = tmp_path / "no0210.db"
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE llm_calls (id INTEGER PRIMARY KEY, purpose TEXT)")
    conn.execute("INSERT INTO llm_calls(purpose) VALUES ('a')")
    conn.commit()
    try:
        result = repair_mod.repair(conn)
        assert set(result["added"]) == set(_ALL_0212)
        assert result["backfilled"] == {}
        assert conn.execute("SELECT attempt_count FROM llm_calls").fetchone() == (None,)
    finally:
        conn.close()


def test_repair_never_touches_alembic_version(prod_shape: Path) -> None:
    """The revision pointer is already correct in claiming 0212 applied; this
    makes the schema match the claim, never the other way round."""
    conn = sqlite3.connect(prod_shape)
    conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
    conn.execute("INSERT INTO alembic_version VALUES ('0269_some_revision')")
    conn.commit()
    try:
        repair_mod.repair(conn)
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0269_some_revision",
        )
    finally:
        conn.close()
