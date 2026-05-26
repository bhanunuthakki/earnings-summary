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
    RuleTier,
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
        CREATE TABLE financial_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            period_end TIMESTAMP NOT NULL,
            fiscal_period_type TEXT NOT NULL,
            line_item TEXT NOT NULL,
            value NUMERIC(24, 6) NOT NULL,
            currency TEXT,
            unit TEXT NOT NULL,
            source_doc_id INTEGER NOT NULL,
            confidence REAL NOT NULL DEFAULT 1.0
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
            soft_rule_results_json TEXT,
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
    """JSON loader picks up break_rules and ignores extra fields.

    Also pins that rules from the `break_rules` array get tier=UNIVERSAL — array
    placement is the source of truth for the tier classification.
    """
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
    assert spec.break_rules[0].tier == RuleTier.UNIVERSAL
    assert spec.business_model_rules == []


def test_load_holdings_spec_tags_business_model_rules(tmp_path: Path) -> None:
    """Rules listed under `business_model_rules` get tier=BUSINESS_MODEL."""
    payload = {
        "ticker": "RBRK",
        "thesis": "...",
        "business_model_rules": [
            {
                "rule_id": "sub_arr_contribution_margin_reversal",
                "kpi_name": "Sub ARR Contribution Margin",
                "comparator": "lt",
                "threshold": 5,
                "unit": "percent",
                "consecutive_periods": 2,
                "narrative": "Contribution margin inflection reverses",
            }
        ],
    }
    (tmp_path / "RBRK.json").write_text(json.dumps(payload), encoding="utf-8")
    spec = load_holdings_spec(tmp_path, "RBRK")
    assert spec.break_rules == []
    assert len(spec.business_model_rules) == 1
    assert spec.business_model_rules[0].tier == RuleTier.BUSINESS_MODEL


def test_load_holdings_spec_array_placement_wins_over_explicit_tier(
    tmp_path: Path,
) -> None:
    """Even if a rule in `break_rules` has `"tier": "business_model"` in JSON,
    the array placement wins. This prevents a JSON-level mistake (wrong tier
    string) from producing a rule that lives in `break_rules` but renders under
    the per-ticker table."""
    payload = {
        "ticker": "TEST",
        "thesis": "...",
        "break_rules": [
            {
                "rule_id": "x",
                "kpi_name": "Foo",
                "comparator": "lt",
                "threshold": 0,
                "unit": "percent",
                "consecutive_periods": 1,
                "narrative": "Foo < 0",
                "tier": "business_model",  # explicit override should be ignored
            }
        ],
    }
    (tmp_path / "TEST.json").write_text(json.dumps(payload), encoding="utf-8")
    spec = load_holdings_spec(tmp_path, "TEST")
    assert spec.break_rules[0].tier == RuleTier.UNIVERSAL


def test_load_holdings_spec_handles_missing_break_rules(tmp_path: Path) -> None:
    """Missing arrays -> empty lists, no error."""
    p = tmp_path / "X.json"
    p.write_text(json.dumps({"ticker": "X", "thesis": "x"}), encoding="utf-8")
    spec = load_holdings_spec(tmp_path, "X")
    assert spec.break_rules == []
    assert spec.business_model_rules == []


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


def test_evaluate_ticker_thesis_iterates_both_arrays(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Both `break_rules` (universal) and `business_model_rules` are evaluated,
    in that order, in a single ThesisVerdict.

    Order matters because the §2 renderer relies on universal-first sequencing
    to keep catastrophic tripwires visually above per-ticker breakers.
    """
    payload = {
        "ticker": "RBRK",
        "thesis": "...",
        "break_rules": [
            {
                "rule_id": "universal_revenue_decline",
                "kpi_name": "Revenue YoY Growth (USD)",
                "comparator": "lt", "threshold": 0, "unit": "percent",
                "consecutive_periods": 1, "narrative": "Revenue declining YoY",
            },
        ],
        "business_model_rules": [
            {
                "rule_id": "rbrk_sub_arr_growth_below_25",
                "kpi_name": "Sub ARR YoY Growth",
                "comparator": "lt", "threshold": 25, "unit": "percent",
                "consecutive_periods": 1,
                "narrative": "Sub ARR YoY growth below 25% — deceleration",
            },
        ],
    }
    (tmp_path / "RBRK.json").write_text(json.dumps(payload), encoding="utf-8")
    _seed_kpi(conn, "RBRK", "Revenue YoY Growth (USD)", [("2026-01-31", 46)])  # OK
    _seed_kpi(conn, "RBRK", "Sub ARR YoY Growth", [("2026-01-31", 34)])  # OK

    verdict = evaluate_ticker_thesis(conn, ticker="RBRK", holdings_dir=tmp_path)
    assert len(verdict.rule_evaluations) == 2
    # Universal first, then business_model — order is a contract for the renderer.
    assert verdict.rule_evaluations[0].rule.tier == RuleTier.UNIVERSAL
    assert verdict.rule_evaluations[1].rule.tier == RuleTier.BUSINESS_MODEL


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
        narrative="NPL > 7", tier=RuleTier.BUSINESS_MODEL,
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
    assert parsed[0]["tier"] == "business_model"
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


# ---------------------------------------------------------------------------
# Soft rule integration — predicates from break_rules_soft drive YELLOW
# without escalating past WARN.
# ---------------------------------------------------------------------------


def _seed_financial_fact(
    conn: sqlite3.Connection,
    ticker: str,
    line_item: str,
    values: list[tuple[str, float]],
) -> None:
    """Insert one financial_facts row per (period_end, value) entry."""
    for i, (period_end, val) in enumerate(values):
        conn.execute(
            "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type, "
            "line_item, value, currency, unit, source_doc_id) "
            "VALUES (?, ?, ?, ?, ?, 'USD', 'actual', 1)",
            (ticker, period_end, f"Q{(i % 4) + 1}", line_item, str(val)),
        )
    conn.commit()


def test_load_holdings_spec_reads_break_rules_soft(tmp_path: Path) -> None:
    """`break_rules_soft` round-trips through load_holdings_spec into spec.soft_rules."""
    payload = {
        "ticker": "VEEV",
        "thesis": "...",
        "break_rules_soft": [
            {
                "name": "rev_decel_2q",
                "predicate": {
                    "type": "series_decel",
                    "params": {"metric": "revenue", "periods": 2, "threshold_bps": 200},
                },
                "evidence_template": "decel {first_bps}→{second_bps}bps",
            }
        ],
    }
    (tmp_path / "VEEV.json").write_text(json.dumps(payload), encoding="utf-8")
    spec = load_holdings_spec(tmp_path, "VEEV")
    assert len(spec.soft_rules) == 1
    assert spec.soft_rules[0].name == "rev_decel_2q"


def test_evaluate_ticker_thesis_yellow_when_soft_rule_fires(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """When all hard rules are OK and one soft rule fires, overall = WARN (YELLOW).

    This is the canonical user-visible flow: VEEV's revenue YoY collapses for
    2Q, no hard threshold is hit, but the soft rule surfaces it.
    """
    payload = {
        "ticker": "VEEV",
        "thesis": "vertical-SaaS compounder",
        "break_rules_soft": [
            {
                "name": "growth_decel_2q",
                "predicate": {
                    "type": "series_decel",
                    "params": {"metric": "revenue", "periods": 2, "threshold_bps": 200},
                },
                "evidence_template": "Revenue YoY decel {first_bps}→{second_bps} bps",
            }
        ],
    }
    (tmp_path / "VEEV.json").write_text(json.dumps(payload), encoding="utf-8")
    # 12 quarters: baseline year ~100, growth year +50%, then 3Q of decelerating growth.
    periods = [
        ("2022-03-31", 100.0), ("2022-06-30", 100.0),
        ("2022-09-30", 100.0), ("2022-12-31", 100.0),
        ("2023-03-31", 150.0), ("2023-06-30", 150.0),
        ("2023-09-30", 150.0), ("2023-12-31", 150.0),
        ("2024-03-31", 170.0),  # 13.3% YoY
        ("2024-06-30", 155.0),  # 3.3% YoY — decel 1000bps
        ("2024-09-30", 148.0),  # -1.3% YoY — decel 460bps
    ]
    _seed_financial_fact(conn, "VEEV", "revenue", periods)
    verdict = evaluate_ticker_thesis(conn, ticker="VEEV", holdings_dir=tmp_path)
    assert verdict.overall_status == BreachStatus.WARN
    assert len(verdict.soft_rule_results) == 1
    soft = verdict.soft_rule_results[0]
    assert soft.status.value == "yellow"
    assert "bps" in soft.evidence


def test_evaluate_ticker_thesis_hard_breach_outranks_soft_yellow(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Hard BREACH wins over soft YELLOW in the rollup."""
    payload = {
        "ticker": "VEEV",
        "thesis": "...",
        "break_rules": [
            {
                "rule_id": "rev_decline", "kpi_name": "Revenue YoY Growth (USD)",
                "comparator": "lt", "threshold": 0, "unit": "percent",
                "consecutive_periods": 1, "narrative": "Revenue YoY < 0",
            },
        ],
        "break_rules_soft": [
            {
                "name": "any_soft",
                "predicate": {
                    "type": "series_below",
                    "params": {"metric": "fcf_margin", "source": "kpi", "threshold": 100, "periods": 1},
                },
            }
        ],
    }
    (tmp_path / "VEEV.json").write_text(json.dumps(payload), encoding="utf-8")
    _seed_kpi(conn, "VEEV", "Revenue YoY Growth (USD)", [("2025-12-31", -5)])  # BREACH
    _seed_kpi(conn, "VEEV", "fcf_margin", [("2025-12-31", 5)])  # soft fires too
    verdict = evaluate_ticker_thesis(conn, ticker="VEEV", holdings_dir=tmp_path)
    assert verdict.overall_status == BreachStatus.BREACH


def test_evaluate_ticker_thesis_green_when_nothing_fires(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Soft rule defined but doesn't fire, no hard rules → overall OK (GREEN)."""
    payload = {
        "ticker": "VEEV",
        "thesis": "...",
        "break_rules_soft": [
            {
                "name": "margin_floor",
                "predicate": {
                    "type": "series_below",
                    "params": {"metric": "margin", "source": "kpi", "threshold": 40, "periods": 2},
                },
            }
        ],
    }
    (tmp_path / "VEEV.json").write_text(json.dumps(payload), encoding="utf-8")
    _seed_kpi(conn, "VEEV", "margin", [("2024-12-31", 42), ("2025-03-31", 41)])
    verdict = evaluate_ticker_thesis(conn, ticker="VEEV", holdings_dir=tmp_path)
    assert verdict.overall_status == BreachStatus.OK
    assert verdict.soft_rule_results[0].status.value == "green"


def test_persist_verdict_writes_soft_rule_results_json(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """soft_rule_results_json column populated with the rendered evidence."""
    payload = {
        "ticker": "VEEV",
        "thesis": "...",
        "break_rules_soft": [
            {
                "name": "m_below",
                "predicate": {
                    "type": "series_below",
                    "params": {"metric": "m", "source": "kpi", "threshold": 5, "periods": 1},
                },
            }
        ],
    }
    (tmp_path / "VEEV.json").write_text(json.dumps(payload), encoding="utf-8")
    _seed_kpi(conn, "VEEV", "m", [("2025-12-31", 1)])
    verdict = evaluate_ticker_thesis(conn, ticker="VEEV", holdings_dir=tmp_path)
    persist_verdict(conn, verdict, run_id="r1")
    row = conn.execute(
        "SELECT soft_rule_results_json FROM thesis_evaluations WHERE ticker='VEEV'"
    ).fetchone()
    raw = row["soft_rule_results_json"]
    assert raw is not None
    parsed = json.loads(raw)
    assert len(parsed) == 1
    assert parsed[0]["rule_name"] == "m_below"
    assert parsed[0]["status"] == "yellow"


def test_persist_verdict_soft_column_null_when_no_soft_rules(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """No soft rules in holdings → column stays NULL, distinguishable from
    "rules evaluated, all green"."""
    payload: dict[str, object] = {"ticker": "PURE", "thesis": "...", "break_rules_soft": []}
    (tmp_path / "PURE.json").write_text(json.dumps(payload), encoding="utf-8")
    verdict = evaluate_ticker_thesis(conn, ticker="PURE", holdings_dir=tmp_path)
    persist_verdict(conn, verdict, run_id="r1")
    row = conn.execute(
        "SELECT soft_rule_results_json FROM thesis_evaluations WHERE ticker='PURE'"
    ).fetchone()
    assert row["soft_rule_results_json"] is None
