# pyright: reportPrivateUsage=false
"""Tests for src/pipeline/validation_engine.py — range, magnitude_jump, source_disagreement."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from models.facts import Unit
from models.validation import Severity, ValidationRule
from pipeline.kpi_semantic_scope import ScopedKpiDefinition
from pipeline.validation_engine import (
    _check_financial_fact_ranges,
    _check_kpi_fact_ranges,
    _check_magnitude_jumps,
    _check_source_disagreement,
    _scan_series_for_jumps,
    check_kpi_semantic_coverage,
    run_all_checks,
)


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            source_type TEXT NOT NULL,
            doc_type TEXT NOT NULL,
            file_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            fetched_at TIMESTAMP NOT NULL,
            fetch_status TEXT NOT NULL,
            raw_bytes_size INTEGER NOT NULL
        );
        CREATE TABLE financial_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            period_end TIMESTAMP NOT NULL,
            fiscal_period_type TEXT NOT NULL,
            line_item TEXT NOT NULL,
            value NUMERIC(24,6) NOT NULL,
            currency TEXT,
            unit TEXT NOT NULL,
            source_doc_id INTEGER NOT NULL
        );
        CREATE TABLE kpi_definitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            name TEXT NOT NULL,
            unit TEXT NOT NULL,
            primary_source TEXT NOT NULL,
            UNIQUE(ticker, name)
        );
        CREATE TABLE kpi_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            period_end TIMESTAMP NOT NULL,
            fiscal_period_type TEXT NOT NULL,
            kpi_definition_id INTEGER NOT NULL,
            value NUMERIC(24,6) NOT NULL,
            unit TEXT NOT NULL,
            source_doc_id INTEGER NOT NULL
        );
        CREATE TABLE validation_issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            source_doc_id INTEGER,
            ticker TEXT,
            severity TEXT NOT NULL,
            rule TEXT NOT NULL,
            raw_value TEXT,
            expected TEXT,
            raised_at TIMESTAMP NOT NULL,
            resolved_at TIMESTAMP
        );
        """
    )
    conn.commit()


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _create_schema(c)
    return c


def _doc(conn: sqlite3.Connection, ticker: str, source_type: str = "fmp") -> int:
    cur = conn.execute(
        "INSERT INTO documents (ticker, source_type, doc_type, file_path, sha256, "
        "fetched_at, fetch_status, raw_bytes_size) VALUES (?, ?, 'x', 'p', ?, ?, 'ok', 1)",
        (ticker, source_type, "a" * 64, datetime.now()),
    )
    return int(cur.lastrowid) if cur.lastrowid is not None else 0


def _ff(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    line_item: str,
    value: float,
    period_end: datetime,
    fpt: str = "Q4",
    source_doc_id: int,
) -> None:
    conn.execute(
        "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type, "
        "line_item, value, currency, unit, source_doc_id) "
        "VALUES (?, ?, ?, ?, ?, 'USD', 'actual', ?)",
        (ticker, period_end, fpt, line_item, str(value), source_doc_id),
    )
    conn.commit()


def _kpi(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    name: str,
    unit: str,
    value: float,
    period_end: datetime,
    source_doc_id: int,
) -> None:
    cur = conn.execute(
        "INSERT INTO kpi_definitions (ticker, name, unit, primary_source) VALUES (?, ?, ?, 'fmp')",
        (ticker, name, unit),
    )
    kid = int(cur.lastrowid) if cur.lastrowid is not None else 0
    conn.execute(
        "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, "
        "value, unit, source_doc_id) VALUES (?, ?, 'Q4', ?, ?, ?, ?)",
        (ticker, period_end, kid, str(value), unit, source_doc_id),
    )
    conn.commit()


def test_range_check_skips_unbounded_line_items(conn: sqlite3.Connection) -> None:
    """Currency-agnostic design: revenue/op_income/net_income have no range bound, so no firings."""
    doc_id = _doc(conn, "X")
    _ff(
        conn,
        ticker="X",
        line_item="revenue",
        value=-1_000_000,
        period_end=datetime(2024, 12, 31),
        source_doc_id=doc_id,
    )
    _ff(
        conn,
        ticker="X",
        line_item="net_income",
        value=-50_000_000,
        period_end=datetime(2024, 12, 31),
        source_doc_id=doc_id,
    )
    outcome = _check_financial_fact_ranges(conn, run_id="r1", ticker="X")
    assert outcome.issues_inserted == 0


def test_range_check_fires_on_negative_total_assets(conn: sqlite3.Connection) -> None:
    doc_id = _doc(conn, "X")
    _ff(
        conn,
        ticker="X",
        line_item="total_assets",
        value=-1_000,
        period_end=datetime(2024, 12, 31),
        source_doc_id=doc_id,
    )
    outcome = _check_financial_fact_ranges(conn, run_id="r1", ticker="X")
    assert outcome.issues_inserted == 1
    issue = conn.execute("SELECT severity, rule FROM validation_issues WHERE ticker='X'").fetchone()
    assert dict(issue)["severity"] == Severity.WARN.value
    assert dict(issue)["rule"] == ValidationRule.PLAUSIBLE_RANGE.value


def test_financial_range_query_fetches_only_bounded_items(conn: sqlite3.Connection) -> None:
    doc_id = _doc(conn, "X")
    _ff(
        conn,
        ticker="X",
        line_item="revenue",
        value=100,
        period_end=datetime(2024, 12, 31),
        source_doc_id=doc_id,
    )
    _ff(
        conn,
        ticker="X",
        line_item="total_assets",
        value=200,
        period_end=datetime(2024, 12, 31),
        source_doc_id=doc_id,
    )

    outcome = _check_financial_fact_ranges(conn, run_id="r1", ticker="X")

    assert outcome.rows_examined == 1


def test_kpi_range_check_fires_on_out_of_band_percent(conn: sqlite3.Connection) -> None:
    """Op margin > 1000% should fire (FNV's 5258% case)."""
    doc_id = _doc(conn, "FNV")
    _kpi(
        conn,
        ticker="FNV",
        name="OpMargin",
        unit=Unit.PERCENT.value,
        value=5000,
        period_end=datetime(2024, 12, 31),
        source_doc_id=doc_id,
    )
    outcome = _check_kpi_fact_ranges(conn, run_id="r1", ticker="FNV")
    assert outcome.issues_inserted == 1


def test_kpi_range_query_fetches_only_bounded_units(conn: sqlite3.Connection) -> None:
    doc_id = _doc(conn, "FNV")
    _kpi(
        conn,
        ticker="FNV",
        name="NarrativeScore",
        unit="text",
        value=1,
        period_end=datetime(2024, 12, 31),
        source_doc_id=doc_id,
    )
    _kpi(
        conn,
        ticker="FNV",
        name="OpMargin",
        unit=Unit.PERCENT.value,
        value=50,
        period_end=datetime(2024, 12, 31),
        source_doc_id=doc_id,
    )

    outcome = _check_kpi_fact_ranges(conn, run_id="r1", ticker="FNV")

    assert outcome.rows_examined == 1


def test_magnitude_jump_fires_on_5x_sequential(conn: sqlite3.Connection) -> None:
    """Sequential same-key values differing by >5x get flagged."""
    doc_id = _doc(conn, "X")
    _ff(
        conn,
        ticker="X",
        line_item="revenue",
        value=100,
        period_end=datetime(2023, 12, 31),
        source_doc_id=doc_id,
    )
    _ff(
        conn,
        ticker="X",
        line_item="revenue",
        value=1000,
        period_end=datetime(2024, 12, 31),
        source_doc_id=doc_id,
    )
    outcome = _check_magnitude_jumps(conn, run_id="r1", ticker="X")
    assert outcome.issues_inserted == 1


def test_magnitude_jump_quiet_when_within_5x(conn: sqlite3.Connection) -> None:
    doc_id = _doc(conn, "X")
    _ff(
        conn,
        ticker="X",
        line_item="revenue",
        value=100,
        period_end=datetime(2023, 12, 31),
        source_doc_id=doc_id,
    )
    _ff(
        conn,
        ticker="X",
        line_item="revenue",
        value=300,
        period_end=datetime(2024, 12, 31),
        source_doc_id=doc_id,
    )
    outcome = _check_magnitude_jumps(conn, run_id="r1", ticker="X")
    assert outcome.issues_inserted == 0


def test_magnitude_jump_fires_on_balance_sheet_3x(conn: sqlite3.Connection) -> None:
    """The MELI case the old income-only 5x pass missed: FMP's cash row spikes
    to 15,141M for one quarter against a ~3.6B run-rate (4.1x). Balance-sheet
    stocks are scanned at the tighter 3x, so this fires."""
    doc_id = _doc(conn, "MELI")
    _ff(
        conn,
        ticker="MELI",
        line_item="cash_and_equivalents",
        value=3_677_000_000,
        period_end=datetime(2025, 12, 31),
        source_doc_id=doc_id,
    )
    _ff(
        conn,
        ticker="MELI",
        line_item="cash_and_equivalents",
        value=15_141_000_000,
        period_end=datetime(2026, 3, 31),
        source_doc_id=doc_id,
    )
    outcome = _check_magnitude_jumps(conn, run_id="r1", ticker="MELI")
    assert outcome.issues_inserted == 1
    issue = conn.execute(
        "SELECT rule, raw_value FROM validation_issues WHERE ticker='MELI'"
    ).fetchone()
    assert dict(issue)["rule"] == ValidationRule.MAGNITUDE_JUMP.value
    assert "cash_and_equivalents" in dict(issue)["raw_value"]


def test_magnitude_jump_balance_quiet_within_3x(conn: sqlite3.Connection) -> None:
    """A 2.5x balance-sheet move (below the 3x bar) stays quiet."""
    doc_id = _doc(conn, "X")
    _ff(
        conn,
        ticker="X",
        line_item="total_assets",
        value=1_000_000,
        period_end=datetime(2025, 12, 31),
        source_doc_id=doc_id,
    )
    _ff(
        conn,
        ticker="X",
        line_item="total_assets",
        value=2_500_000,
        period_end=datetime(2026, 3, 31),
        source_doc_id=doc_id,
    )
    outcome = _check_magnitude_jumps(conn, run_id="r1", ticker="X")
    assert outcome.issues_inserted == 0


def test_magnitude_jump_income_4x_does_not_fire_under_balance_bar(
    conn: sqlite3.Connection,
) -> None:
    """The tighter 3x bar applies ONLY to balance-sheet stocks. An income flow
    jumping 4x is under the income 5x bar and must stay quiet — proving the
    two passes don't cross-contaminate line-item scope."""
    doc_id = _doc(conn, "X")
    _ff(
        conn,
        ticker="X",
        line_item="revenue",
        value=100,
        period_end=datetime(2025, 12, 31),
        source_doc_id=doc_id,
    )
    _ff(
        conn,
        ticker="X",
        line_item="revenue",
        value=400,
        period_end=datetime(2026, 3, 31),
        source_doc_id=doc_id,
    )
    outcome = _check_magnitude_jumps(conn, run_id="r1", ticker="X")
    assert outcome.issues_inserted == 0


def test_magnitude_jump_excludes_zero_crossing_equity(conn: sqlite3.Connection) -> None:
    """Equity is deliberately out of scope (WIX runs negative book equity, so a
    sign flip would explode the ratio). Even a wild equity swing must not fire."""
    doc_id = _doc(conn, "WIX")
    _ff(
        conn,
        ticker="WIX",
        line_item="total_equity",
        value=-100_000_000,
        period_end=datetime(2025, 12, 31),
        source_doc_id=doc_id,
    )
    _ff(
        conn,
        ticker="WIX",
        line_item="total_equity",
        value=5_000_000_000,
        period_end=datetime(2026, 3, 31),
        source_doc_id=doc_id,
    )
    outcome = _check_magnitude_jumps(conn, run_id="r1", ticker="WIX")
    assert outcome.issues_inserted == 0


def test_scan_series_for_jumps_empty_line_items_is_noop(conn: sqlite3.Connection) -> None:
    """Guard: an empty line_items tuple short-circuits (no SQL with an empty
    IN() clause)."""
    inserted, examined = _scan_series_for_jumps(
        conn, run_id="r1", ticker=None, line_items=(), multiplier=Decimal(1)
    )
    assert (inserted, examined) == (0, 0)


def test_source_disagreement_only_across_distinct_source_types(conn: sqlite3.Connection) -> None:
    """Two FMP rows reporting the same period don't fire (intra-source aggregation noise)."""
    fmp1 = _doc(conn, "X", source_type="fmp")
    fmp2 = _doc(conn, "X", source_type="fmp")
    _ff(
        conn,
        ticker="X",
        line_item="revenue",
        value=100,
        period_end=datetime(2024, 12, 31),
        source_doc_id=fmp1,
    )
    _ff(
        conn,
        ticker="X",
        line_item="revenue",
        value=200,
        period_end=datetime(2024, 12, 31),
        source_doc_id=fmp2,
    )
    outcome = _check_source_disagreement(conn, run_id="r1", ticker="X")
    assert outcome.issues_inserted == 0


def test_source_disagreement_fires_when_fmp_vs_sec_diverge(conn: sqlite3.Connection) -> None:
    """FMP and SEC reporting different values for same (ticker, period_end, line_item) -> fire."""
    fmp = _doc(conn, "X", source_type="fmp")
    sec = _doc(conn, "X", source_type="sec_xbrl")
    _ff(
        conn,
        ticker="X",
        line_item="revenue",
        value=100,
        period_end=datetime(2024, 12, 31),
        source_doc_id=fmp,
    )
    _ff(
        conn,
        ticker="X",
        line_item="revenue",
        value=200,
        period_end=datetime(2024, 12, 31),
        source_doc_id=sec,
    )
    outcome = _check_source_disagreement(conn, run_id="r1", ticker="X")
    assert outcome.issues_inserted == 1


def test_owner_visible_legacy_unknown_kpi_halts_semantic_gate(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id = _doc(conn, "NU", source_type="ir_doc")
    _kpi(
        conn,
        ticker="NU",
        name="Total customers",
        unit="millions",
        value=114.2,
        period_end=datetime(2024, 12, 31),
        source_doc_id=doc_id,
    )

    observed_user_ids: list[str] = []

    def _scoped(
        _conn: sqlite3.Connection, *, repo_root: Path, user_id: str
    ) -> tuple[ScopedKpiDefinition, ...]:
        del repo_root
        observed_user_ids.append(user_id)
        if user_id != "bhanu":
            return ()
        return (
            ScopedKpiDefinition(
                ticker="NU",
                name="Total customers",
                kpi_definition_id=1,
                reasons=("owner_visible",),
                fact_count=1,
                admitted_context_count=0,
                missing_context_count=0,
                quarantined_context_count=0,
                legacy_unknown_context_count=1,
                current_actual_count=0,
                comparator_count=0,
                guidance_target_count=0,
                management_explanation_count=0,
                analyst_question_count=0,
            ),
        )

    monkeypatch.setattr("pipeline.validation_engine.scoped_kpi_definitions", _scoped)

    wrong_owner = check_kpi_semantic_coverage(
        conn,
        run_id="wrong-owner",
        ticker="NU",
        user_id="default",
    )
    outcome = check_kpi_semantic_coverage(
        conn,
        run_id="semantic-gate",
        ticker="NU",
        user_id="bhanu",
    )

    assert wrong_owner.rows_examined == 0
    assert wrong_owner.issues_inserted == 1
    assert outcome.issues_inserted == 1
    assert observed_user_ids == ["default", "bhanu"]
    wrong_owner_issue = conn.execute(
        "SELECT severity,raw_value FROM validation_issues WHERE run_id='wrong-owner'"
    ).fetchone()
    assert wrong_owner_issue is not None
    assert wrong_owner_issue[0] == Severity.HALT.value
    assert wrong_owner_issue[1] == "owner_scope_empty:user_id=default"
    issue = conn.execute(
        "SELECT severity,rule,raw_value FROM validation_issues WHERE run_id='semantic-gate'"
    ).fetchone()
    assert issue is not None
    assert issue[0] == Severity.HALT.value
    assert issue[1] == ValidationRule.KPI_SEMANTIC_CONTEXT.value
    assert "legacy_unknown=1" in str(issue[2])


def test_run_all_checks_executes_every_rule(conn: sqlite3.Connection) -> None:
    """Smoke test: empty KPI owner scope fails closed while other rules stay quiet."""
    report = run_all_checks(conn, run_id="r1", ticker=None, user_id="bhanu")
    assert len(report.outcomes) == 5
    assert [outcome.issues_inserted for outcome in report.outcomes] == [0, 0, 0, 0, 1]
