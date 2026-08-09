"""Unit tests for src/llm_budget.py — budget math + threshold gating + alert
dedupe + graceful degradation when the DB / tables aren't present.

The integration with src/llm_client._call_claude lives in
test_llm_client_budget_integration.py.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import llm_budget as budget_module
from llm_budget import (
    DEFAULT_BUDGET_KEY,
    BudgetCheck,
    budget_mode,
    check_budget,
    current_month_spend,
    list_budgets,
    month_report,
    record_alert,
    set_cap,
)
from sqlite_runtime import SQLiteConnectionRole

# ---------------------------------------------------------------------------
# Helpers — mirror migrations 0034 + 0052 inline so the tests stay
# independent from the alembic runner. A failure here flags a bug in the
# schema/code coupling, not the migration order.
# ---------------------------------------------------------------------------


_CREATE_LLM_CALLS = """
CREATE TABLE llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    called_at DATETIME NOT NULL,
    purpose VARCHAR(64),
    ticker VARCHAR(16),
    scope VARCHAR(64),
    model VARCHAR(64) NOT NULL,
    prompt_sha256 VARCHAR(64) NOT NULL,
    response_sha256 VARCHAR(64),
    prompt_chars INTEGER NOT NULL,
    response_chars INTEGER,
    input_tokens INTEGER,
    cache_creation_input_tokens INTEGER,
    cache_read_input_tokens INTEGER,
    output_tokens INTEGER,
    elapsed_ms INTEGER NOT NULL,
    cost_estimate_usd FLOAT,
    cache_hit BOOLEAN NOT NULL DEFAULT 0,
    fallback_used VARCHAR(16),
    artifact_id INTEGER,
    error TEXT,
    run_id VARCHAR(64)
);
"""

_CREATE_LLM_BUDGETS = """
CREATE TABLE llm_budgets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    purpose VARCHAR(64) NOT NULL,
    monthly_cap_usd NUMERIC(10,2) NOT NULL,
    warn_threshold_pct FLOAT NOT NULL DEFAULT 0.80,
    hard_block BOOLEAN NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    notes TEXT,
    CONSTRAINT uq_llm_budgets_purpose UNIQUE (purpose)
);
"""

_CREATE_LLM_BUDGET_ALERTS = """
CREATE TABLE llm_budget_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    purpose VARCHAR(64) NOT NULL,
    month VARCHAR(7) NOT NULL,
    threshold_pct FLOAT NOT NULL,
    alerted_at DATETIME NOT NULL,
    spend_at_alert_usd NUMERIC(10,4) NOT NULL,
    CONSTRAINT uq_alerts UNIQUE (purpose, month, threshold_pct)
);
"""


def _init_db(path: Path, *, with_budgets: bool = True) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_CREATE_LLM_CALLS)
        if with_budgets:
            conn.executescript(_CREATE_LLM_BUDGETS)
            conn.executescript(_CREATE_LLM_BUDGET_ALERTS)
        conn.commit()
    finally:
        conn.close()


def _insert_budget(
    path: Path,
    purpose: str,
    cap_usd: float,
    *,
    warn: float = 0.80,
    hard_block: bool = False,
) -> None:
    conn = sqlite3.connect(str(path))
    try:
        now = datetime.now(UTC).isoformat()
        conn.execute(
            """
            INSERT INTO llm_budgets
                (purpose, monthly_cap_usd, warn_threshold_pct, hard_block,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (purpose, cap_usd, warn, 1 if hard_block else 0, now, now),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_call(
    path: Path,
    *,
    purpose: str,
    cost: float,
    called_at: datetime | None = None,
) -> None:
    conn = sqlite3.connect(str(path))
    try:
        at = (called_at or datetime.now(UTC)).isoformat()
        conn.execute(
            """
            INSERT INTO llm_calls
                (called_at, purpose, model, prompt_sha256, prompt_chars,
                 elapsed_ms, cost_estimate_usd)
            VALUES (?, ?, 'claude-sonnet-4-6', 'x', 100, 1000, ?)
            """,
            (at, purpose, cost),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# current_month_spend — math correctness
# ---------------------------------------------------------------------------


def test_current_month_spend_sums_within_window(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    _init_db(db)
    now = datetime.now(UTC).replace(day=15)
    _insert_call(db, purpose="bear_case", cost=1.50, called_at=now)
    _insert_call(db, purpose="bear_case", cost=2.25, called_at=now)
    _insert_call(db, purpose="bear_case", cost=0.75, called_at=now)
    spend = current_month_spend("bear_case", db_path=db, now=now)
    assert spend == Decimal("4.5000")


def test_current_month_spend_filters_other_purposes(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    _init_db(db)
    now = datetime.now(UTC).replace(day=15)
    _insert_call(db, purpose="bear_case", cost=5.00, called_at=now)
    _insert_call(db, purpose="transcript_summary", cost=10.00, called_at=now)
    assert current_month_spend("bear_case", db_path=db, now=now) == Decimal("5.0000")
    assert current_month_spend("transcript_summary", db_path=db, now=now) == Decimal("10.0000")


def test_current_month_spend_filters_prior_month(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    _init_db(db)
    now = datetime(2026, 5, 15, tzinfo=UTC)
    last_month = datetime(2026, 4, 28, tzinfo=UTC)
    _insert_call(db, purpose="bear_case", cost=99.00, called_at=last_month)
    _insert_call(db, purpose="bear_case", cost=3.00, called_at=now)
    # Only the current-month row should count.
    assert current_month_spend("bear_case", db_path=db, now=now) == Decimal("3.0000")


def test_current_month_spend_returns_zero_when_no_rows(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    _init_db(db)
    assert current_month_spend("never_called", db_path=db) == Decimal("0")


# ---------------------------------------------------------------------------
# check_budget — threshold gating
# ---------------------------------------------------------------------------


def test_check_budget_allows_when_under_warn(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    _init_db(db)
    _insert_budget(db, "bear_case", cap_usd=50.00)
    _insert_call(db, purpose="bear_case", cost=10.00)
    check = check_budget("bear_case", db_path=db)
    assert check.allowed is True
    assert check.warn is False
    assert check.headroom_pct == pytest.approx(0.80, abs=0.001)


def test_check_budget_warns_at_80pct(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    _init_db(db)
    _insert_budget(db, "bear_case", cap_usd=50.00, warn=0.80)
    _insert_call(db, purpose="bear_case", cost=40.00)  # exactly 80%
    check = check_budget("bear_case", db_path=db)
    assert check.allowed is True
    assert check.warn is True
    assert check.headroom_pct == pytest.approx(0.20, abs=0.001)


def test_check_budget_blocks_when_over_cap_hard(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    _init_db(db)
    _insert_budget(db, "bear_case", cap_usd=50.00, hard_block=True)
    _insert_call(db, purpose="bear_case", cost=51.00)
    check = check_budget("bear_case", db_path=db)
    assert check.allowed is False
    assert check.hard_block is True
    assert check.headroom_pct < 0


def test_check_budget_blocks_when_over_cap_soft(tmp_path: Path) -> None:
    """Soft cap: allowed=False (caller decides to warn), hard_block=False."""
    db = tmp_path / "test.db"
    _init_db(db)
    _insert_budget(db, "bear_case", cap_usd=50.00, hard_block=False)
    _insert_call(db, purpose="bear_case", cost=51.00)
    check = check_budget("bear_case", db_path=db)
    assert check.allowed is False
    assert check.hard_block is False


def test_check_budget_falls_back_to_default(tmp_path: Path) -> None:
    """Unknown purpose → uses the __default__ row."""
    db = tmp_path / "test.db"
    _init_db(db)
    _insert_budget(db, DEFAULT_BUDGET_KEY, cap_usd=25.00, hard_block=True)
    _insert_call(db, purpose="unknown_purpose", cost=30.00)
    check = check_budget("unknown_purpose", db_path=db)
    assert check.allowed is False
    assert check.hard_block is True
    assert check.cap == Decimal("25.0000")


def test_check_budget_uses_one_read_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-call gate must not open the database twice per LLM call."""
    db = tmp_path / "test.db"
    _init_db(db)
    _insert_budget(db, "bear_case", cap_usd=50.00)
    calls = 0
    real_connect = budget_module.connect_sqlite

    def counted_connect(
        path: str | Path,
        *,
        role: SQLiteConnectionRole,
        schema_preflight: bool | None = None,
    ) -> sqlite3.Connection:
        nonlocal calls
        calls += 1
        return real_connect(path, role=role, schema_preflight=schema_preflight)

    monkeypatch.setattr(budget_module, "connect_sqlite", counted_connect)

    check_budget("bear_case", db_path=db)

    assert calls == 1


def test_check_budget_returns_allowed_for_none_purpose(tmp_path: Path) -> None:
    """purpose=None is a legacy caller — never block."""
    db = tmp_path / "test.db"
    _init_db(db)
    check = check_budget(None, db_path=db)
    assert check.allowed is True
    assert check.warn is False


# ---------------------------------------------------------------------------
# record_alert — idempotent dedupe
# ---------------------------------------------------------------------------


def test_record_alert_writes_once_per_threshold_per_month(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    _init_db(db)
    spend = Decimal("40.00")
    first = record_alert("bear_case", 0.80, spend, db_path=db)
    second = record_alert("bear_case", 0.80, spend, db_path=db)
    third = record_alert("bear_case", 0.80, spend, db_path=db)
    assert first is True
    assert second is False
    assert third is False

    # Confirm exactly 1 row landed.
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute("SELECT * FROM llm_budget_alerts").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1


def test_record_alert_separate_threshold_writes_separate_row(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    _init_db(db)
    record_alert("bear_case", 0.80, Decimal("40.00"), db_path=db)
    record_alert("bear_case", 1.00, Decimal("51.00"), db_path=db)
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute("SELECT COUNT(*) FROM llm_budget_alerts").fetchone()
    finally:
        conn.close()
    assert rows[0] == 2


def test_record_alert_separate_month_writes_separate_row(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    _init_db(db)
    may_15 = datetime(2026, 5, 15, tzinfo=UTC)
    jun_15 = datetime(2026, 6, 15, tzinfo=UTC)
    record_alert("bear_case", 0.80, Decimal("40.00"), db_path=db, now=may_15)
    record_alert("bear_case", 0.80, Decimal("40.00"), db_path=db, now=jun_15)
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute("SELECT month FROM llm_budget_alerts ORDER BY month").fetchall()
    finally:
        conn.close()
    assert [r[0] for r in rows] == ["2026-05", "2026-06"]


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------


def test_check_budget_returns_allowed_when_db_missing(tmp_path: Path) -> None:
    """No DB file at all → allowed=True (fail open)."""
    missing = tmp_path / "nope.db"
    assert not missing.exists()
    check = check_budget("bear_case", db_path=missing)
    assert check.allowed is True
    assert check.warn is False


def test_check_budget_returns_allowed_when_table_missing(tmp_path: Path) -> None:
    """DB exists but no llm_budgets → allowed=True (pre-migration repo)."""
    db = tmp_path / "empty.db"
    sqlite3.connect(str(db)).close()
    check = check_budget("bear_case", db_path=db)
    assert check.allowed is True


def test_check_budget_no_budget_row_allows(tmp_path: Path) -> None:
    """Tables exist but purpose has no row and no __default__ → allowed."""
    db = tmp_path / "test.db"
    _init_db(db)
    check = check_budget("bear_case", db_path=db)
    assert check.allowed is True


def test_current_month_spend_handles_missing_table(tmp_path: Path) -> None:
    """Missing llm_calls table → returns Decimal('0'), no raise."""
    db = tmp_path / "no_calls.db"
    sqlite3.connect(str(db)).close()
    assert current_month_spend("bear_case", db_path=db) == Decimal("0")


def test_record_alert_returns_false_when_db_missing(tmp_path: Path) -> None:
    missing = tmp_path / "nope.db"
    assert record_alert("bear_case", 0.80, Decimal("1.00"), db_path=missing) is False


def test_record_alert_returns_false_when_table_missing(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    sqlite3.connect(str(db)).close()
    assert record_alert("bear_case", 0.80, Decimal("1.00"), db_path=db) is False


# ---------------------------------------------------------------------------
# list_budgets — annotated read for CLI / dashboard
# ---------------------------------------------------------------------------


def test_list_budgets_annotates_with_spend_and_headroom(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    _init_db(db)
    _insert_budget(db, "bear_case", cap_usd=50.00)
    _insert_budget(db, "transcript_summary", cap_usd=80.00, hard_block=True)
    _insert_call(db, purpose="bear_case", cost=10.00)
    rows = list_budgets(db_path=db)
    by_purpose = {str(r["purpose"]): r for r in rows}
    bc = by_purpose["bear_case"]
    assert bc["current_spend_usd"] == Decimal("10.0000")
    assert bc["headroom_pct"] == pytest.approx(0.80, abs=0.001)
    ts = by_purpose["transcript_summary"]
    assert ts["current_spend_usd"] == Decimal("0")
    assert ts["hard_block"] is True


def test_list_budgets_uses_one_read_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dashboard budget loading must not perform one connection per row."""
    db = tmp_path / "test.db"
    _init_db(db)
    _insert_budget(db, "bear_case", cap_usd=50.00)
    _insert_budget(db, "transcript_summary", cap_usd=80.00)
    calls = 0
    real_connect = budget_module.connect_sqlite

    def counted_connect(
        path: str | Path,
        *,
        role: SQLiteConnectionRole,
        schema_preflight: bool | None = None,
    ) -> sqlite3.Connection:
        nonlocal calls
        calls += 1
        return real_connect(path, role=role, schema_preflight=schema_preflight)

    monkeypatch.setattr(budget_module, "connect_sqlite", counted_connect)

    rows = list_budgets(db_path=db)

    assert len(rows) == 2
    assert calls == 1


def test_list_budgets_returns_empty_when_no_tables(tmp_path: Path) -> None:
    missing = tmp_path / "no.db"
    assert list_budgets(db_path=missing) == []


# ---------------------------------------------------------------------------
# set_cap — write path used by the CLI
# ---------------------------------------------------------------------------


def test_set_cap_updates_existing_row(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    _init_db(db)
    _insert_budget(db, "bear_case", cap_usd=50.00)
    ok = set_cap("bear_case", 75.00, db_path=db)
    assert ok is True
    rows = list_budgets(db_path=db)
    bc = next(r for r in rows if r["purpose"] == "bear_case")
    assert bc["monthly_cap_usd"] == Decimal("75.0000")


def test_set_cap_returns_false_when_purpose_missing(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    _init_db(db)
    assert set_cap("never_seeded", 100.0, db_path=db) is False


# ---------------------------------------------------------------------------
# month_report — monthly cross-purpose roll-up
# ---------------------------------------------------------------------------


def test_month_report_combines_budgeted_and_unbudgeted_purposes(
    tmp_path: Path,
) -> None:
    """A purpose seen in llm_calls but missing from llm_budgets should
    still show in the report (cap=None) — operator might want to add a
    budget for it."""
    db = tmp_path / "test.db"
    _init_db(db)
    _insert_budget(db, "bear_case", cap_usd=50.00)
    now = datetime.now(UTC).replace(day=10)
    _insert_call(db, purpose="bear_case", cost=12.00, called_at=now)
    _insert_call(db, purpose="brand_new_purpose", cost=3.00, called_at=now)
    rows = month_report(db_path=db)
    by_purpose = {str(r["purpose"]): r for r in rows}
    assert by_purpose["bear_case"]["cap_usd"] == Decimal("50.0000")
    assert by_purpose["brand_new_purpose"]["cap_usd"] is None
    assert by_purpose["bear_case"]["spend_usd"] == Decimal("12.0000")
    assert by_purpose["brand_new_purpose"]["spend_usd"] == Decimal("3.0000")


def test_month_report_for_explicit_month_excludes_other_months(
    tmp_path: Path,
) -> None:
    db = tmp_path / "test.db"
    _init_db(db)
    _insert_budget(db, "bear_case", cap_usd=50.00)
    apr = datetime(2026, 4, 20, tzinfo=UTC)
    may = datetime(2026, 5, 20, tzinfo=UTC)
    _insert_call(db, purpose="bear_case", cost=15.00, called_at=apr)
    _insert_call(db, purpose="bear_case", cost=5.00, called_at=may)
    rows = month_report(month="2026-04", db_path=db)
    bc = next(r for r in rows if r["purpose"] == "bear_case")
    assert bc["spend_usd"] == Decimal("15.0000")


# ---------------------------------------------------------------------------
# BudgetCheck dataclass surface — quick smoke
# ---------------------------------------------------------------------------


def test_budget_check_is_frozen_and_slots() -> None:
    """Dataclass shape contract — frozen+slots gives us hashable, no
    accidental field-setting at call sites."""
    bc = BudgetCheck(
        allowed=True,
        warn=False,
        current_spend=Decimal("0"),
        cap=Decimal("0"),
        headroom_pct=1.0,
        hard_block=False,
        reason=None,
    )
    with pytest.raises((AttributeError, Exception)):  # FrozenInstanceError subclasses Exception
        bc.allowed = False  # type: ignore[misc]


def test_check_budget_fails_closed_when_existing_db_is_corrupt(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An existing unreadable ledger must never silently authorize spend."""
    db = tmp_path / "corrupt.db"
    db.write_text("not a sqlite db")
    with caplog.at_level("ERROR"):
        check = check_budget("bear_case", db_path=db)

    assert check.allowed is False
    assert check.hard_block is True
    assert check.on_exceed == "block"
    assert check.reason == "bear_case: budget data unavailable; blocking LLM call"
    assert "check_budget_read_failed" in caplog.text
    assert str(db) not in caplog.text
    assert "not a database" not in caplog.text


def test_check_budget_fails_closed_and_redacts_connection_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    db = tmp_path / "existing.db"
    _init_db(db)

    def failed_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        del args, kwargs
        raise sqlite3.OperationalError("credential=top-secret")

    monkeypatch.setattr(budget_module, "connect_sqlite", failed_connect)

    with caplog.at_level("ERROR"):
        check = check_budget("bear_case", db_path=db)

    assert check.allowed is False
    assert check.hard_block is True
    assert check.on_exceed == "block"
    assert "OperationalError" in caplog.text
    assert "top-secret" not in caplog.text


def test_budget_mode_fails_closed_when_existing_db_is_corrupt(tmp_path: Path) -> None:
    db = tmp_path / "corrupt.db"
    db.write_text("not a sqlite db")

    assert budget_mode("bear_case", db_path=db) == "block"


def test_current_month_spend_clean_window_boundary(tmp_path: Path) -> None:
    """A call placed exactly at the month start (00:00:00) IS counted —
    the >= comparison includes the boundary."""
    db = tmp_path / "test.db"
    _init_db(db)
    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    month_start = datetime(2026, 5, 1, 0, 0, 0, tzinfo=UTC)
    _insert_call(db, purpose="bear_case", cost=2.00, called_at=month_start)
    # And one a microsecond before — should NOT count.
    _insert_call(
        db,
        purpose="bear_case",
        cost=99.00,
        called_at=month_start - timedelta(microseconds=1),
    )
    spend = current_month_spend("bear_case", db_path=db, now=now)
    assert spend == Decimal("2.0000")
