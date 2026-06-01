"""on_exceed budget modes (migration 0066) + the skip pre-flight gate.

Covers the budget-engine half of PR1 (dashboard-managed budgets + "forgone due
to budget"): the new `on_exceed` {skip|block|warn} mode, `set_mode`/`budget_mode`
round-trip, `check_budget` deriving `hard_block` from the mode, and
`should_skip_for_budget` — the pre-flight gate that tells a section to forgo its
LLM call (no spend) and render a forgone-due-to-budget banner. Also pins the
migration's ADD COLUMN + backfill SQL and the pre-0066 fallback (derive the mode
from the legacy hard_block bool).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from llm_budget import (
    budget_mode,
    check_budget,
    list_budgets,
    set_mode,
    should_skip_for_budget,
)

# Migrated (0066) schema — llm_budgets WITH on_exceed.
_CREATE_MIGRATED = """
CREATE TABLE llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, called_at DATETIME NOT NULL,
    purpose VARCHAR(64), model VARCHAR(64) NOT NULL, prompt_sha256 VARCHAR(64) NOT NULL,
    prompt_chars INTEGER NOT NULL, elapsed_ms INTEGER NOT NULL, cost_estimate_usd FLOAT);
CREATE TABLE llm_budgets (
    id INTEGER PRIMARY KEY AUTOINCREMENT, purpose VARCHAR(64) NOT NULL,
    monthly_cap_usd NUMERIC(10,2) NOT NULL, warn_threshold_pct FLOAT NOT NULL DEFAULT 0.80,
    hard_block BOOLEAN NOT NULL DEFAULT 0,
    on_exceed TEXT NOT NULL DEFAULT 'warn' CHECK (on_exceed IN ('skip', 'block', 'warn')),
    created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, notes TEXT,
    CONSTRAINT uq_llm_budgets_purpose UNIQUE (purpose));
"""

# Pre-0066 schema — llm_budgets WITHOUT on_exceed (the hand-rolled legacy shape).
_CREATE_LEGACY = """
CREATE TABLE llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, called_at DATETIME NOT NULL,
    purpose VARCHAR(64), model VARCHAR(64) NOT NULL, prompt_sha256 VARCHAR(64) NOT NULL,
    prompt_chars INTEGER NOT NULL, elapsed_ms INTEGER NOT NULL, cost_estimate_usd FLOAT);
CREATE TABLE llm_budgets (
    id INTEGER PRIMARY KEY AUTOINCREMENT, purpose VARCHAR(64) NOT NULL,
    monthly_cap_usd NUMERIC(10,2) NOT NULL, warn_threshold_pct FLOAT NOT NULL DEFAULT 0.80,
    hard_block BOOLEAN NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, notes TEXT,
    CONSTRAINT uq_llm_budgets_purpose UNIQUE (purpose));
"""

_NOW = datetime(2026, 6, 15, tzinfo=UTC)
_CALL_AT = datetime(2026, 6, 5, tzinfo=UTC)


def _db(tmp_path: Path, *, migrated: bool = True) -> Path:
    path = tmp_path / "t.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_CREATE_MIGRATED if migrated else _CREATE_LEGACY)
        conn.commit()
    finally:
        conn.close()
    return path


def _insert_budget(
    path: Path,
    purpose: str,
    cap: float,
    *,
    on_exceed: str | None = "warn",
    hard_block: bool = False,
) -> None:
    """Insert a budget row. ``on_exceed=None`` targets the legacy (pre-0066)
    schema, which has no such column."""
    now = datetime.now(UTC).isoformat()
    conn = sqlite3.connect(str(path))
    try:
        if on_exceed is None:
            conn.execute(
                "INSERT INTO llm_budgets (purpose, monthly_cap_usd, warn_threshold_pct, "
                "hard_block, created_at, updated_at) VALUES (?, ?, 0.80, ?, ?, ?)",
                (purpose, cap, 1 if hard_block else 0, now, now),
            )
        else:
            conn.execute(
                "INSERT INTO llm_budgets (purpose, monthly_cap_usd, warn_threshold_pct, "
                "hard_block, on_exceed, created_at, updated_at) VALUES (?, ?, 0.80, ?, ?, ?, ?)",
                (purpose, cap, 1 if hard_block else 0, on_exceed, now, now),
            )
        conn.commit()
    finally:
        conn.close()


def _insert_call(path: Path, purpose: str, cost: float) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "INSERT INTO llm_calls (called_at, purpose, model, prompt_sha256, prompt_chars, "
            "elapsed_ms, cost_estimate_usd) VALUES (?, ?, 'claude-sonnet-4-6', 'x', 100, 1000, ?)",
            (_CALL_AT.isoformat(), purpose, cost),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# budget_mode / set_mode
# ---------------------------------------------------------------------------


def test_budget_mode_reads_on_exceed(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _insert_budget(db, "bear_case", 50, on_exceed="skip")
    _insert_budget(db, "__default__", 25, on_exceed="warn")
    assert budget_mode("bear_case", db_path=db) == "skip"
    # Unknown purpose falls back to __default__'s mode.
    assert budget_mode("not_listed", db_path=db) == "warn"


def test_budget_mode_defaults_to_warn_when_no_row(tmp_path: Path) -> None:
    db = _db(tmp_path)  # no rows, no __default__
    assert budget_mode("bear_case", db_path=db) == "warn"


def test_set_mode_round_trip_and_syncs_hard_block(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _insert_budget(db, "bear_case", 50, on_exceed="warn")
    assert set_mode("bear_case", "skip", db_path=db) is True
    assert budget_mode("bear_case", db_path=db) == "skip"
    # 'block' mode syncs the legacy hard_block bool to 1.
    assert set_mode("bear_case", "block", db_path=db) is True
    row = (
        sqlite3.connect(str(db))
        .execute("SELECT on_exceed, hard_block FROM llm_budgets WHERE purpose='bear_case'")
        .fetchone()
    )
    assert row == ("block", 1)


def test_set_mode_rejects_invalid_mode(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _insert_budget(db, "bear_case", 50)
    with pytest.raises(ValueError, match="invalid on_exceed mode"):
        set_mode("bear_case", "ignore", db_path=db)


# ---------------------------------------------------------------------------
# check_budget derives hard_block from the mode
# ---------------------------------------------------------------------------


def test_check_budget_derives_hard_block_from_mode(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _insert_budget(db, "bear_case", 10, on_exceed="block")
    _insert_call(db, "bear_case", 20)  # over the $10 cap
    blocked = check_budget("bear_case", db_path=db, now=_NOW)
    assert blocked.allowed is False
    assert blocked.on_exceed == "block"
    assert blocked.hard_block is True  # derived from on_exceed == 'block'

    set_mode("bear_case", "skip", db_path=db)
    skipped = check_budget("bear_case", db_path=db, now=_NOW)
    assert skipped.allowed is False
    assert skipped.on_exceed == "skip"
    assert skipped.hard_block is False  # skip never hard-blocks at the gate


# ---------------------------------------------------------------------------
# should_skip_for_budget — the pre-flight gate
# ---------------------------------------------------------------------------


def test_skip_when_over_cap_and_skip_mode(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _insert_budget(db, "bear_case", 10, on_exceed="skip")
    _insert_call(db, "bear_case", 20)
    check = should_skip_for_budget("bear_case", db_path=db, now=_NOW)
    assert check is not None
    assert check.cap == 10
    assert check.current_spend == 20
    assert check.headroom_pct < 0


def test_no_skip_under_cap(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _insert_budget(db, "bear_case", 100, on_exceed="skip")
    _insert_call(db, "bear_case", 5)
    assert should_skip_for_budget("bear_case", db_path=db, now=_NOW) is None


@pytest.mark.parametrize("mode", ["block", "warn"])
def test_no_skip_for_non_skip_modes(tmp_path: Path, mode: str) -> None:
    # 'block' raises at the call gate (not pre-flight); 'warn' overspends. Only
    # 'skip' forgoes the call pre-flight.
    db = _db(tmp_path)
    _insert_budget(db, "bear_case", 10, on_exceed=mode)
    _insert_call(db, "bear_case", 20)
    assert should_skip_for_budget("bear_case", db_path=db, now=_NOW) is None


def test_bypass_never_skips(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _insert_budget(db, "bear_case", 10, on_exceed="skip")
    _insert_call(db, "bear_case", 20)
    assert should_skip_for_budget("bear_case", db_path=db, now=_NOW, bypass=True) is None


def test_list_budgets_includes_on_exceed(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _insert_budget(db, "bear_case", 50, on_exceed="skip")
    rows = {r["purpose"]: r for r in list_budgets(db_path=db, now=_NOW)}
    assert rows["bear_case"]["on_exceed"] == "skip"


# ---------------------------------------------------------------------------
# Migration 0066: ADD COLUMN + backfill, and the pre-0066 fallback
# ---------------------------------------------------------------------------


def test_migration_adds_column_and_backfills(tmp_path: Path) -> None:
    db = _db(tmp_path, migrated=False)  # legacy schema, no on_exceed
    _insert_budget(db, "bear_case", 50, on_exceed=None, hard_block=True)
    _insert_budget(db, "__default__", 25, on_exceed=None, hard_block=False)
    # Apply the exact migration 0066 upgrade SQL.
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "ALTER TABLE llm_budgets ADD COLUMN on_exceed TEXT NOT NULL DEFAULT 'warn' "
            "CHECK (on_exceed IN ('skip', 'block', 'warn'))"
        )
        conn.execute("UPDATE llm_budgets SET on_exceed = 'block' WHERE hard_block = 1")
        conn.commit()
    finally:
        conn.close()
    assert budget_mode("bear_case", db_path=db) == "block"  # backfilled from hard_block=1
    assert budget_mode("__default__", db_path=db) == "warn"  # column default


def test_pre_0066_fallback_derives_mode_from_hard_block(tmp_path: Path) -> None:
    # Before the migration runs, the engine must still enforce — deriving the
    # mode from the legacy hard_block bool so existing behavior is unchanged.
    db = _db(tmp_path, migrated=False)
    _insert_budget(db, "bear_case", 10, on_exceed=None, hard_block=True)
    _insert_call(db, "bear_case", 20)
    assert budget_mode("bear_case", db_path=db) == "block"
    blocked = check_budget("bear_case", db_path=db, now=_NOW)
    assert blocked.allowed is False
    assert blocked.hard_block is True
