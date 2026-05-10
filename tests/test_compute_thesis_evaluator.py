"""Tests for src/compute/thesis_evaluator.py — break-rule loading, evaluation, persistence."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from compute.thesis_evaluator import (
    BreakRule,
    Comparator,
    KpiObservation,
    ThesisVerdict,
    evaluate_rule,
    evaluate_ticker_thesis,
    load_holdings_spec,
    persist_verdict,
)
from models.facts import Unit
from models.kpis import BreachStatus


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
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
            value NUMERIC(24, 6) NOT NULL,
            unit TEXT NOT NULL,
            source_doc_id INTEGER NOT NULL
        );
        CREATE TABLE thesis_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL UNIQUE,
            thesis TEXT,
            last_updated TIMESTAMP,
            breach_status TEXT,
            raw_json TEXT NOT NULL,
            ingested_at TIMESTAMP NOT NULL
        );
        CREATE TABLE thesis_evaluations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            evaluated_at TIMESTAMP NOT NULL,
            overall_status TEXT NOT NULL,
            rule_evaluations_json TEXT NOT NULL,
            run_id TEXT
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


def _seed_kpi(
    conn: sqlite3.Connection, ticker: str, kpi_name: str, values: list[tuple[str, float]]
) -> None:
    """Insert a kpi_definitions row + N kpi_facts rows for it."""
    conn.execute(
        "INSERT INTO kpi_definitions (ticker, name, unit, primary_source) "
        "VALUES (?, ?, 'percent', 'ir_doc')",
        (ticker, kpi_name),
    )
    kpi_id = conn.execute(
        "SELECT id FROM kpi_definitions WHERE ticker = ? AND name = ?",
        (ticker, kpi_name),
    ).fetchone()["id"]
    for period_end_iso, val in values:
        conn.execute(
            "INSERT INTO kpi_facts "
            "(ticker, period_end, fiscal_period_type, kpi_definition_id, value, unit, source_doc_id) "
            "VALUES (?, ?, 'Q4', ?, ?, 'percent', 1)",
            (ticker, period_end_iso, kpi_id, str(val)),
        )
    conn.commit()


def test_evaluate_rule_ok_when_no_observations() -> None:
    """No observations -> OK with detail noting no data."""
    rule = BreakRule(
        rule_id="r1", kpi_name="X", comparator=Comparator.LT,
        threshold=Decimal("20"), unit=Unit.PERCENT, consecutive_periods=1,
        narrative="X < 20",
    )
    result = evaluate_rule(rule, [])
    assert result.status == BreachStatus.OK
    assert "no kpi_facts" in result.detail


def test_evaluate_rule_ok_when_value_above_threshold() -> None:
    """Single observation 30 vs threshold-lt-20 -> OK (no match)."""
    rule = BreakRule(
        rule_id="r1", kpi_name="X", comparator=Comparator.LT,
        threshold=Decimal("20"), unit=Unit.PERCENT, consecutive_periods=1,
        narrative="X < 20",
    )
    obs = [KpiObservation(period_end=datetime(2025, 12, 31), value=Decimal("30"), unit=Unit.PERCENT)]
    result = evaluate_rule(rule, obs)
    assert result.status == BreachStatus.OK


def test_evaluate_rule_breach_single_period() -> None:
    """consecutive_periods=1 + matching observation -> BREACH."""
    rule = BreakRule(
        rule_id="r1", kpi_name="X", comparator=Comparator.LT,
        threshold=Decimal("20"), unit=Unit.PERCENT, consecutive_periods=1,
        narrative="X < 20",
    )
    obs = [KpiObservation(period_end=datetime(2025, 12, 31), value=Decimal("15"), unit=Unit.PERCENT)]
    result = evaluate_rule(rule, obs)
    assert result.status == BreachStatus.BREACH
    assert "breach" in result.detail.lower()


def test_evaluate_rule_warn_when_only_one_of_two_matches() -> None:
    """consecutive_periods=2: only newest matches -> WARN, not BREACH."""
    rule = BreakRule(
        rule_id="r1", kpi_name="X", comparator=Comparator.LT,
        threshold=Decimal("20"), unit=Unit.PERCENT, consecutive_periods=2,
        narrative="X < 20 for 2 quarters",
    )
    obs = [
        KpiObservation(period_end=datetime(2025, 12, 31), value=Decimal("15"), unit=Unit.PERCENT),
        KpiObservation(period_end=datetime(2025, 9, 30), value=Decimal("25"), unit=Unit.PERCENT),
    ]
    result = evaluate_rule(rule, obs)
    assert result.status == BreachStatus.WARN


def test_evaluate_rule_breach_when_both_consecutive_match() -> None:
    """Both newest obs match the rule -> BREACH."""
    rule = BreakRule(
        rule_id="r1", kpi_name="X", comparator=Comparator.LT,
        threshold=Decimal("20"), unit=Unit.PERCENT, consecutive_periods=2,
        narrative="X < 20 for 2 quarters",
    )
    obs = [
        KpiObservation(period_end=datetime(2025, 12, 31), value=Decimal("15"), unit=Unit.PERCENT),
        KpiObservation(period_end=datetime(2025, 9, 30), value=Decimal("18"), unit=Unit.PERCENT),
    ]
    result = evaluate_rule(rule, obs)
    assert result.status == BreachStatus.BREACH


def test_evaluate_rule_supports_gt_comparator() -> None:
    """Comparator.GT: value > threshold fires the rule."""
    rule = BreakRule(
        rule_id="r1", kpi_name="NPL", comparator=Comparator.GT,
        threshold=Decimal("7"), unit=Unit.PERCENT, consecutive_periods=1,
        narrative="NPL > 7",
    )
    obs = [KpiObservation(period_end=datetime(2025, 12, 31), value=Decimal("8.5"), unit=Unit.PERCENT)]
    result = evaluate_rule(rule, obs)
    assert result.status == BreachStatus.BREACH


def test_load_holdings_spec_reads_break_rules(tmp_path: Path) -> None:
    """JSON loader picks up break_rules and ignores extra fields."""
    payload = {
        "ticker": "TEST",
        "thesis": "...",
        "irrelevant_field": "ignored",
        "break_rules": [
            {
                "rule_id": "x",
                "kpi_name": "Foo",
                "comparator": "lt",
                "threshold": 10,
                "unit": "percent",
                "consecutive_periods": 1,
                "narrative": "Foo < 10",
            }
        ],
    }
    p = tmp_path / "TEST.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    spec = load_holdings_spec(tmp_path, "TEST")
    assert spec.ticker == "TEST"
    assert len(spec.break_rules) == 1
    assert spec.break_rules[0].kpi_name == "Foo"


def test_load_holdings_spec_handles_missing_break_rules(tmp_path: Path) -> None:
    """Missing break_rules -> empty list, no error."""
    p = tmp_path / "X.json"
    p.write_text(json.dumps({"ticker": "X", "thesis": "x"}), encoding="utf-8")
    spec = load_holdings_spec(tmp_path, "X")
    assert spec.break_rules == []


def test_load_holdings_spec_raises_on_missing_file(tmp_path: Path) -> None:
    """Missing file -> FileNotFoundError, no silent fallback."""
    with pytest.raises(FileNotFoundError):
        load_holdings_spec(tmp_path, "NOPE")


def test_evaluate_ticker_thesis_end_to_end(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """Full path: holdings JSON + kpi_facts -> ThesisVerdict with rolled-up status."""
    payload = {
        "ticker": "MELI",
        "thesis": "growth compounder",
        "break_rules": [
            {
                "rule_id": "rev_below_20", "kpi_name": "Revenue Growth (FXN)",
                "comparator": "lt", "threshold": 20, "unit": "percent",
                "consecutive_periods": 1, "narrative": "Revenue < 20",
            },
            {
                "rule_id": "gmv_below_15", "kpi_name": "GMV Growth (FXN)",
                "comparator": "lt", "threshold": 15, "unit": "percent",
                "consecutive_periods": 1, "narrative": "GMV < 15",
            },
        ],
    }
    (tmp_path / "MELI.json").write_text(json.dumps(payload), encoding="utf-8")
    _seed_kpi(conn, "MELI", "Revenue Growth (FXN)", [("2024-12-31", 96)])  # 96 > 20 -> OK
    _seed_kpi(conn, "MELI", "GMV Growth (FXN)", [("2024-12-31", 56)])      # 56 > 15 -> OK

    verdict = evaluate_ticker_thesis(conn, ticker="MELI", holdings_dir=tmp_path)
    assert isinstance(verdict, ThesisVerdict)
    assert verdict.ticker == "MELI"
    assert verdict.overall_status == BreachStatus.OK
    assert len(verdict.rule_evaluations) == 2


def test_evaluate_ticker_thesis_bubbles_breach(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """One BREACH rule rolls up to overall_status=BREACH."""
    payload = {
        "ticker": "NU",
        "thesis": "...",
        "break_rules": [
            {
                "rule_id": "npl_above_7", "kpi_name": "NPL 90d+",
                "comparator": "gt", "threshold": 7, "unit": "percent",
                "consecutive_periods": 1, "narrative": "NPL > 7",
            },
        ],
    }
    (tmp_path / "NU.json").write_text(json.dumps(payload), encoding="utf-8")
    _seed_kpi(conn, "NU", "NPL 90d+", [("2025-12-31", 8.5)])

    verdict = evaluate_ticker_thesis(conn, ticker="NU", holdings_dir=tmp_path)
    assert verdict.overall_status == BreachStatus.BREACH
    assert verdict.rule_evaluations[0].status == BreachStatus.BREACH


def test_persist_verdict_updates_thesis_state(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """persist_verdict writes breach_status into thesis_state."""
    conn.execute(
        "INSERT INTO thesis_state (ticker, thesis, raw_json, ingested_at) "
        "VALUES ('TEST', 't', '{}', ?)",
        (datetime.now(),),
    )
    conn.commit()

    verdict = ThesisVerdict(
        ticker="TEST",
        thesis="t",
        overall_status=BreachStatus.BREACH,
        rule_evaluations=(),
        evaluated_at=datetime(2026, 5, 3, 12, 0, 0),
    )
    persist_verdict(conn, verdict)

    row = conn.execute(
        "SELECT breach_status, last_updated FROM thesis_state WHERE ticker='TEST'"
    ).fetchone()
    assert dict(row)["breach_status"] == "breach"


def test_persist_verdict_appends_to_thesis_evaluations(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """persist_verdict appends one row per call to thesis_evaluations (history)."""
    conn.execute(
        "INSERT INTO thesis_state (ticker, thesis, raw_json, ingested_at) "
        "VALUES ('TEST', 't', '{}', ?)",
        (datetime.now(),),
    )
    conn.commit()

    v1 = ThesisVerdict(
        ticker="TEST", thesis="t", overall_status=BreachStatus.OK,
        rule_evaluations=(), evaluated_at=datetime(2026, 4, 1, 12, 0, 0),
    )
    v2 = ThesisVerdict(
        ticker="TEST", thesis="t", overall_status=BreachStatus.BREACH,
        rule_evaluations=(), evaluated_at=datetime(2026, 5, 1, 12, 0, 0),
    )
    persist_verdict(conn, v1, run_id="r1")
    persist_verdict(conn, v2, run_id="r2")

    rows = conn.execute(
        "SELECT ticker, evaluated_at, overall_status, run_id "
        "FROM thesis_evaluations WHERE ticker='TEST' ORDER BY evaluated_at"
    ).fetchall()
    assert len(rows) == 2
    assert dict(rows[0])["overall_status"] == "ok"
    assert dict(rows[0])["run_id"] == "r1"
    assert dict(rows[1])["overall_status"] == "breach"
    assert dict(rows[1])["run_id"] == "r2"


def test_persist_verdict_serializes_rule_evaluations(
    conn: sqlite3.Connection,
) -> None:
    """rule_evaluations_json captures rule + observations for time-series analysis."""
    conn.execute(
        "INSERT INTO thesis_state (ticker, thesis, raw_json, ingested_at) "
        "VALUES ('NU', 't', '{}', ?)",
        (datetime.now(),),
    )
    conn.commit()
    rule = BreakRule(
        rule_id="r1", kpi_name="NPL", comparator=Comparator.GT,
        threshold=Decimal("7"), unit=Unit.PERCENT, consecutive_periods=1,
        narrative="NPL > 7",
    )
    obs = KpiObservation(
        period_end=datetime(2025, 12, 31), value=Decimal("8.5"), unit=Unit.PERCENT
    )
    eval_result = evaluate_rule(rule, [obs])
    verdict = ThesisVerdict(
        ticker="NU", thesis="t", overall_status=BreachStatus.BREACH,
        rule_evaluations=(eval_result,), evaluated_at=datetime(2026, 5, 1),
    )
    persist_verdict(conn, verdict, run_id="x")

    payload = conn.execute(
        "SELECT rule_evaluations_json FROM thesis_evaluations WHERE ticker='NU'"
    ).fetchone()["rule_evaluations_json"]
    parsed = json.loads(payload)
    assert len(parsed) == 1
    assert parsed[0]["rule_id"] == "r1"
    assert parsed[0]["status"] == "breach"
    assert parsed[0]["observations"][0]["value"] == "8.5"


def test_break_rule_rejects_zero_consecutive_periods() -> None:
    """consecutive_periods must be >= 1 (Pydantic enforces)."""
    with pytest.raises(ValueError):
        BreakRule(
            rule_id="x", kpi_name="X", comparator=Comparator.LT,
            threshold=Decimal("1"), unit=Unit.PERCENT, consecutive_periods=0,
            narrative="x",
        )


def test_persist_verdict_upserts_when_thesis_state_row_missing(
    conn: sqlite3.Connection,
) -> None:
    """A thesis_state row is created on first persist_verdict for an unseen ticker.

    Regression: previously persist_verdict only UPDATEd, so tickers added via
    raw SQL inserts (bypassing track_company's onboard hook) ended up with
    thesis_evaluations rows but NO thesis_state row, breaking the dashboard's
    breach_status read.
    """
    # No INSERT INTO thesis_state — row should be missing.
    pre = conn.execute("SELECT COUNT(*) FROM thesis_state WHERE ticker='FRESH'").fetchone()[0]
    assert pre == 0

    verdict = ThesisVerdict(
        ticker="FRESH",
        thesis="some thesis",
        overall_status=BreachStatus.WARN,
        rule_evaluations=(),
        evaluated_at=datetime(2026, 5, 9, 12, 0, 0),
    )
    persist_verdict(conn, verdict, run_id="upsert-run")

    row = conn.execute(
        "SELECT ticker, thesis, breach_status, last_updated, raw_json, ingested_at "
        "FROM thesis_state WHERE ticker='FRESH'"
    ).fetchone()
    assert row is not None
    d = dict(row)
    assert d["ticker"] == "FRESH"
    assert d["thesis"] == "some thesis"
    assert d["breach_status"] == "warn"
    assert d["raw_json"] == "{}"

    # And a thesis_evaluations row was also created (existing behavior preserved).
    evals = conn.execute(
        "SELECT overall_status, run_id FROM thesis_evaluations WHERE ticker='FRESH'"
    ).fetchall()
    assert len(evals) == 1
    assert dict(evals[0])["overall_status"] == "warn"
    assert dict(evals[0])["run_id"] == "upsert-run"


def test_persist_verdict_preserves_existing_raw_json(
    conn: sqlite3.Connection,
) -> None:
    """When thesis_state already has a row, raw_json is preserved across upsert."""
    seeded_raw = '{"thesis": "original holdings JSON content here"}'
    conn.execute(
        "INSERT INTO thesis_state (ticker, thesis, raw_json, ingested_at) "
        "VALUES ('KEEP', 't', ?, ?)",
        (seeded_raw, datetime(2026, 1, 1)),
    )
    conn.commit()

    verdict = ThesisVerdict(
        ticker="KEEP",
        thesis="t",
        overall_status=BreachStatus.BREACH,
        rule_evaluations=(),
        evaluated_at=datetime(2026, 5, 9, 12, 0, 0),
    )
    persist_verdict(conn, verdict)

    # raw_json is the seeded value, NOT '{}' — upsert must not clobber it.
    raw = conn.execute(
        "SELECT raw_json, breach_status FROM thesis_state WHERE ticker='KEEP'"
    ).fetchone()
    assert raw["raw_json"] == seeded_raw
    assert raw["breach_status"] == "breach"
