"""Tests for src/compute/soft_rule_evaluator.py — predicate primitives + evaluator."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

import pytest

from compute.soft_rule_evaluator import (
    FactSource,
    PredicateType,
    SoftRule,
    SoftRulePredicate,
    SoftRuleStatus,
    evaluate_soft_rules,
    load_soft_rules,
)


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
        """
    )
    conn.commit()


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _create_schema(c)
    return c


def _quarter_end(idx: int, *, start: str = "2022-03-31") -> str:
    """Return the period_end string for the idx-th quarter starting from `start`.

    Spacing of 91 days is close enough — the evaluator only cares about order
    and at least 4Q of separation, not the exact calendar date.
    """
    base = datetime.fromisoformat(start)
    return (base + timedelta(days=91 * idx)).strftime("%Y-%m-%d %H:%M:%S")


def _seed_financial(
    conn: sqlite3.Connection,
    ticker: str,
    line_item: str,
    values: Sequence[float],
    *,
    start: str = "2022-03-31",
) -> None:
    for i, v in enumerate(values):
        quarter = f"Q{(i % 4) + 1}"
        conn.execute(
            "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type, "
            "line_item, value, currency, unit, source_doc_id) "
            "VALUES (?, ?, ?, ?, ?, 'USD', 'actual', 1)",
            (ticker.upper(), _quarter_end(i, start=start), quarter, line_item, float(v)),
        )
    conn.commit()


def _seed_kpi(
    conn: sqlite3.Connection,
    ticker: str,
    kpi_name: str,
    values: Sequence[float],
    *,
    start: str = "2022-03-31",
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO kpi_definitions (ticker, name, unit, primary_source) "
        "VALUES (?, ?, 'percent', 'ir_doc')",
        (ticker.upper(), kpi_name),
    )
    kpi_id = conn.execute(
        "SELECT id FROM kpi_definitions WHERE ticker = ? AND name = ?",
        (ticker.upper(), kpi_name),
    ).fetchone()["id"]
    for i, v in enumerate(values):
        quarter = f"Q{(i % 4) + 1}"
        conn.execute(
            "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, "
            "kpi_definition_id, value, unit, source_doc_id) "
            "VALUES (?, ?, ?, ?, ?, 'percent', 1)",
            (ticker.upper(), _quarter_end(i, start=start), quarter, kpi_id, float(v)),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# load_soft_rules
# ---------------------------------------------------------------------------


def test_load_soft_rules_empty_input() -> None:
    """None and [] both → empty list, no errors."""
    assert load_soft_rules(None) == []
    assert load_soft_rules([]) == []


def test_load_soft_rules_skips_malformed_entries() -> None:
    """Bad entries are dropped, good ones survive — one rule's typo can't
    block the rest of the holdings file from loading."""
    raw: list[Any] = [
        {"name": "good", "predicate": {"type": "series_below", "params": {}}},
        "not a dict",
        {"name": "bad", "predicate": {"type": "not_a_real_type"}},
    ]
    rules = load_soft_rules(raw)
    assert [r.name for r in rules] == ["good"]


def test_load_soft_rules_parses_full_entry() -> None:
    """Round-trip a fully-specified rule with template."""
    raw = [
        {
            "name": "rev_decel_2q",
            "predicate": {
                "type": "series_decel",
                "params": {"metric": "revenue", "periods": 2, "threshold_bps": 200},
            },
            "evidence_template": "Revenue YoY decel {first_bps}→{second_bps}bps",
        }
    ]
    rules = load_soft_rules(raw)
    assert len(rules) == 1
    assert rules[0].predicate.type == PredicateType.SERIES_DECEL
    assert rules[0].predicate.params["periods"] == 2
    assert rules[0].evidence_template is not None


# ---------------------------------------------------------------------------
# series_below
# ---------------------------------------------------------------------------


def test_series_below_fires_when_all_periods_under_threshold(
    conn: sqlite3.Connection,
) -> None:
    """Last 2 values both < threshold → YELLOW."""
    _seed_kpi(conn, "VEEV", "Non-GAAP operating margin", [42, 41, 39, 37])
    rule = SoftRule(
        name="margin_floor_2q",
        predicate=SoftRulePredicate(
            type=PredicateType.SERIES_BELOW,
            params={
                "metric": "Non-GAAP operating margin",
                "source": "kpi",
                "threshold": 40,
                "periods": 2,
            },
        ),
    )
    [result] = evaluate_soft_rules("VEEV", [rule], conn)
    assert result.status == SoftRuleStatus.YELLOW
    assert "39" in result.evidence or "37" in result.evidence


def test_series_below_green_when_one_period_recovered(conn: sqlite3.Connection) -> None:
    """Most-recent value back above threshold → GREEN (not all periods match)."""
    _seed_kpi(conn, "VEEV", "Non-GAAP operating margin", [42, 41, 37, 41])
    rule = SoftRule(
        name="margin_floor_2q",
        predicate=SoftRulePredicate(
            type=PredicateType.SERIES_BELOW,
            params={
                "metric": "Non-GAAP operating margin",
                "source": "kpi",
                "threshold": 40,
                "periods": 2,
            },
        ),
    )
    [result] = evaluate_soft_rules("VEEV", [rule], conn)
    assert result.status == SoftRuleStatus.GREEN


def test_series_below_unresolved_on_insufficient_data(conn: sqlite3.Connection) -> None:
    """Fewer observations than periods → UNRESOLVED (never silently GREEN) with
    explanatory evidence — red-team PR2's "never silently green" contract."""
    _seed_kpi(conn, "VEEV", "Non-GAAP operating margin", [37])
    rule = SoftRule(
        name="margin_floor_2q",
        predicate=SoftRulePredicate(
            type=PredicateType.SERIES_BELOW,
            params={
                "metric": "Non-GAAP operating margin",
                "source": "kpi",
                "threshold": 40,
                "periods": 2,
            },
        ),
    )
    [result] = evaluate_soft_rules("VEEV", [rule], conn)
    assert result.status == SoftRuleStatus.UNRESOLVED
    assert "insufficient" in result.evidence.lower()


# ---------------------------------------------------------------------------
# series_above
# ---------------------------------------------------------------------------


def test_series_above_fires_for_consecutive_breaches(conn: sqlite3.Connection) -> None:
    """All last-N values above threshold → YELLOW."""
    _seed_kpi(conn, "NU", "NPL 15-90d", [3, 4, 5.2, 5.7, 6.1])
    rule = SoftRule(
        name="early_npl_drift",
        predicate=SoftRulePredicate(
            type=PredicateType.SERIES_ABOVE,
            params={
                "metric": "NPL 15-90d",
                "source": "kpi",
                "threshold": 5,
                "periods": 3,
            },
        ),
    )
    [result] = evaluate_soft_rules("NU", [rule], conn)
    assert result.status == SoftRuleStatus.YELLOW


# ---------------------------------------------------------------------------
# series_decel
# ---------------------------------------------------------------------------


def test_series_decel_fires_when_yoy_growth_collapses(conn: sqlite3.Connection) -> None:
    """Revenue YoY growth drops sharply for 2 consecutive Q → YELLOW.

    Series: 8Q of revenue with prior-year baseline ~100 and recent quarters
    showing accelerating then decelerating YoY growth. The decel for the last
    2Q is engineered to be > 5000bps (well above the 200bps threshold).
    """
    # 12 values: first 4 = baseline year, next 4 = growth year, next 4 = decel year
    # baseline year Q1-Q4: 100
    # growth year:        150 (50% YoY)
    # decel year Q1:      170 (13.3% YoY)
    # decel year Q2:      155 (3.3% YoY)
    # decel year Q3:      148 (-1.3% YoY)
    revenue = [100, 100, 100, 100, 150, 150, 150, 150, 170, 155, 148]
    _seed_financial(conn, "TEST", "revenue", revenue)
    rule = SoftRule(
        name="revenue_decel_2q",
        predicate=SoftRulePredicate(
            type=PredicateType.SERIES_DECEL,
            params={"metric": "revenue", "periods": 2, "threshold_bps": 200},
        ),
        evidence_template="Revenue YoY decelerated {first_bps}→{second_bps}bps",
    )
    [result] = evaluate_soft_rules("TEST", [rule], conn)
    assert result.status == SoftRuleStatus.YELLOW
    # Template rendered with the deceleration bps values.
    assert "decelerated" in result.evidence
    assert "bps" in result.evidence


def test_series_decel_green_when_growth_steady(conn: sqlite3.Connection) -> None:
    """Steady 50% YoY across the window → no deceleration → GREEN."""
    revenue = [100, 100, 100, 100, 150, 150, 150, 150, 225, 225, 225, 225]
    _seed_financial(conn, "TEST", "revenue", revenue)
    rule = SoftRule(
        name="revenue_decel_2q",
        predicate=SoftRulePredicate(
            type=PredicateType.SERIES_DECEL,
            params={"metric": "revenue", "periods": 2, "threshold_bps": 200},
        ),
    )
    [result] = evaluate_soft_rules("TEST", [rule], conn)
    assert result.status == SoftRuleStatus.GREEN


def test_series_decel_unresolved_when_insufficient_history(conn: sqlite3.Connection) -> None:
    """Series too short for periods + 5 quarters of YoY → UNRESOLVED, not crash,
    never a silent GREEN."""
    _seed_financial(conn, "TEST", "revenue", [100, 100, 100, 100, 150])
    rule = SoftRule(
        name="revenue_decel_2q",
        predicate=SoftRulePredicate(
            type=PredicateType.SERIES_DECEL,
            params={"metric": "revenue", "periods": 2, "threshold_bps": 200},
        ),
    )
    [result] = evaluate_soft_rules("TEST", [rule], conn)
    assert result.status == SoftRuleStatus.UNRESOLVED
    assert "insufficient" in result.evidence.lower()


# ---------------------------------------------------------------------------
# ratio_breach
# ---------------------------------------------------------------------------


def test_ratio_breach_fires_when_ratio_under_threshold_for_n_periods(
    conn: sqlite3.Connection,
) -> None:
    """fcf/revenue < 15% for 2 consecutive Q → YELLOW."""
    _seed_financial(conn, "TEST", "revenue", [100, 100, 100, 100])
    _seed_financial(conn, "TEST", "free_cash_flow", [20, 18, 12, 10])
    rule = SoftRule(
        name="fcf_margin_below_15",
        predicate=SoftRulePredicate(
            type=PredicateType.RATIO_BREACH,
            params={
                "numerator": "free_cash_flow",
                "denominator": "revenue",
                "threshold": 0.15,
                "direction": "below",
                "periods": 2,
            },
        ),
        evidence_template="FCF margin {last_ratio_pct}% (threshold {threshold_pct}%)",
    )
    [result] = evaluate_soft_rules("TEST", [rule], conn)
    assert result.status == SoftRuleStatus.YELLOW
    assert "10" in result.evidence or "12" in result.evidence


def test_ratio_breach_green_when_above_threshold(conn: sqlite3.Connection) -> None:
    """fcf/revenue 18% / 20% — well above 15% threshold → GREEN."""
    _seed_financial(conn, "TEST", "revenue", [100, 100, 100, 100])
    _seed_financial(conn, "TEST", "free_cash_flow", [20, 18, 18, 20])
    rule = SoftRule(
        name="fcf_margin_below_15",
        predicate=SoftRulePredicate(
            type=PredicateType.RATIO_BREACH,
            params={
                "numerator": "free_cash_flow",
                "denominator": "revenue",
                "threshold": 0.15,
                "direction": "below",
                "periods": 2,
            },
        ),
    )
    [result] = evaluate_soft_rules("TEST", [rule], conn)
    assert result.status == SoftRuleStatus.GREEN


# ---------------------------------------------------------------------------
# compound
# ---------------------------------------------------------------------------


def test_compound_and_requires_all_children_to_fire(conn: sqlite3.Connection) -> None:
    """AND: only fires when every child predicate fires."""
    _seed_kpi(conn, "TEST", "metric_a", [9, 9])
    _seed_kpi(conn, "TEST", "metric_b", [11, 11])
    # a < 10 (fires), b > 10 (fires) → AND fires
    rule = SoftRule(
        name="combo",
        predicate=SoftRulePredicate(
            type=PredicateType.COMPOUND,
            params={
                "op": "and",
                "predicates": [
                    {
                        "type": "series_below",
                        "params": {
                            "metric": "metric_a",
                            "source": "kpi",
                            "threshold": 10,
                            "periods": 2,
                        },
                    },
                    {
                        "type": "series_above",
                        "params": {
                            "metric": "metric_b",
                            "source": "kpi",
                            "threshold": 10,
                            "periods": 2,
                        },
                    },
                ],
            },
        ),
    )
    [result] = evaluate_soft_rules("TEST", [rule], conn)
    assert result.status == SoftRuleStatus.YELLOW


def test_compound_or_fires_on_any_child(conn: sqlite3.Connection) -> None:
    """OR: one fired child is enough."""
    _seed_kpi(conn, "TEST", "metric_a", [9, 9])
    _seed_kpi(conn, "TEST", "metric_b", [5, 5])  # not > 10, doesn't fire
    rule = SoftRule(
        name="combo_or",
        predicate=SoftRulePredicate(
            type=PredicateType.COMPOUND,
            params={
                "op": "or",
                "predicates": [
                    {
                        "type": "series_below",
                        "params": {
                            "metric": "metric_a",
                            "source": "kpi",
                            "threshold": 10,
                            "periods": 2,
                        },
                    },
                    {
                        "type": "series_above",
                        "params": {
                            "metric": "metric_b",
                            "source": "kpi",
                            "threshold": 10,
                            "periods": 2,
                        },
                    },
                ],
            },
        ),
    )
    [result] = evaluate_soft_rules("TEST", [rule], conn)
    assert result.status == SoftRuleStatus.YELLOW


def test_compound_and_green_when_one_child_misses(conn: sqlite3.Connection) -> None:
    """AND: one child not firing → whole rule GREEN."""
    _seed_kpi(conn, "TEST", "metric_a", [9, 9])
    _seed_kpi(conn, "TEST", "metric_b", [5, 5])  # not > 10
    rule = SoftRule(
        name="combo_and",
        predicate=SoftRulePredicate(
            type=PredicateType.COMPOUND,
            params={
                "op": "and",
                "predicates": [
                    {
                        "type": "series_below",
                        "params": {
                            "metric": "metric_a",
                            "source": "kpi",
                            "threshold": 10,
                            "periods": 2,
                        },
                    },
                    {
                        "type": "series_above",
                        "params": {
                            "metric": "metric_b",
                            "source": "kpi",
                            "threshold": 10,
                            "periods": 2,
                        },
                    },
                ],
            },
        ),
    )
    [result] = evaluate_soft_rules("TEST", [rule], conn)
    assert result.status == SoftRuleStatus.GREEN


# ---------------------------------------------------------------------------
# Evidence template rendering + error tolerance
# ---------------------------------------------------------------------------


def test_evidence_template_renders_with_predicate_keys(conn: sqlite3.Connection) -> None:
    """Template is filled with the predicate's evidence keys."""
    _seed_kpi(conn, "TEST", "x", [1, 1, 1])
    rule = SoftRule(
        name="x_below_5",
        predicate=SoftRulePredicate(
            type=PredicateType.SERIES_BELOW,
            params={"metric": "x", "source": "kpi", "threshold": 5, "periods": 2},
        ),
        evidence_template="x={last_value} < threshold {threshold}",
    )
    [result] = evaluate_soft_rules("TEST", [rule], conn)
    assert result.status == SoftRuleStatus.YELLOW
    assert "x=1" in result.evidence
    assert "threshold 5" in result.evidence


def test_evidence_template_falls_back_when_key_missing(conn: sqlite3.Connection) -> None:
    """Template referencing a missing key falls back to the predicate's
    generated description — never empty in the brief."""
    _seed_kpi(conn, "TEST", "x", [1, 1])
    rule = SoftRule(
        name="x_below_5",
        predicate=SoftRulePredicate(
            type=PredicateType.SERIES_BELOW,
            params={"metric": "x", "source": "kpi", "threshold": 5, "periods": 2},
        ),
        evidence_template="bogus {does_not_exist}",
    )
    [result] = evaluate_soft_rules("TEST", [rule], conn)
    assert "bogus" not in result.evidence
    # Falls back to description; "for 2 consecutive Q" comes from the description string.
    assert "consecutive" in result.evidence.lower()


def test_evaluator_tolerates_invalid_predicate_params(conn: sqlite3.Connection) -> None:
    """Missing required param → rule is UNRESOLVED (not silently GREEN) with the
    error in evidence (logged, not raised). Other rules in the same call still
    evaluate."""
    _seed_kpi(conn, "TEST", "x", [1, 1])
    bad = SoftRule(
        name="bad_rule",
        predicate=SoftRulePredicate(
            type=PredicateType.SERIES_BELOW,
            params={"metric": "x", "source": "kpi"},  # missing threshold, periods
        ),
    )
    good = SoftRule(
        name="good_rule",
        predicate=SoftRulePredicate(
            type=PredicateType.SERIES_BELOW,
            params={"metric": "x", "source": "kpi", "threshold": 5, "periods": 2},
        ),
    )
    results = evaluate_soft_rules("TEST", [bad, good], conn)
    assert len(results) == 2
    assert results[0].status == SoftRuleStatus.UNRESOLVED
    assert "did not evaluate" in results[0].evidence.lower()
    assert results[1].status == SoftRuleStatus.YELLOW


# ---------------------------------------------------------------------------
# Default source = financial
# ---------------------------------------------------------------------------


def test_default_source_is_financial(conn: sqlite3.Connection) -> None:
    """Predicate with no `source` param reads from financial_facts."""
    _seed_financial(conn, "TEST", "revenue", [50, 40])
    rule = SoftRule(
        name="rev_below",
        predicate=SoftRulePredicate(
            type=PredicateType.SERIES_BELOW,
            params={"metric": "revenue", "threshold": 100, "periods": 2},
        ),
    )
    [result] = evaluate_soft_rules("TEST", [rule], conn)
    assert result.status == SoftRuleStatus.YELLOW


def test_source_enum_round_trips() -> None:
    """Sanity check: the FactSource enum accepts the strings we document."""
    assert FactSource("financial") is FactSource.FINANCIAL
    assert FactSource("kpi") is FactSource.KPI


# ---------------------------------------------------------------------------
# trajectory — red-team PR2 (directives/monthly_red_team.md Phase 1
# "Trajectory WARN"): linear-fit the last lookback_prints, project whether the
# trend crosses `threshold` within horizon_prints future prints.
# ---------------------------------------------------------------------------


def _trajectory_rule(**params: Any) -> SoftRule:
    return SoftRule(
        name="traj",
        predicate=SoftRulePredicate(type=PredicateType.TRAJECTORY, params=params),
        evidence_template="{kpi_name} projects {comparator}{threshold} by {trip_period}",
    )


def test_trajectory_fires_when_approaching_threshold(conn: sqlite3.Connection) -> None:
    """MELI NIMAL-shaped glide: 4 declining prints project a crossing of the
    15% floor within 2 future prints → YELLOW, with a named trip period."""
    _seed_kpi(conn, "MELI", "nimal", [23.0, 21.0, 19.0, 17.8])
    rule = _trajectory_rule(
        kpi_name="nimal",
        source="kpi",
        comparator="lt",
        threshold=15,
        lookback_prints=4,
        horizon_prints=2,
    )
    [result] = evaluate_soft_rules("MELI", [rule], conn)
    assert result.status == SoftRuleStatus.YELLOW
    assert result.details["trip_h"] in (1, 2)
    assert result.details["trip_period"] is not None
    assert "projects" in result.evidence


def test_trajectory_green_when_receding_from_threshold(conn: sqlite3.Connection) -> None:
    """Trend moving AWAY from the threshold never crosses within the horizon
    → GREEN (evaluated, didn't fire), not UNRESOLVED."""
    _seed_kpi(conn, "MELI", "nimal", [10.0, 12.0, 14.0, 16.0])
    rule = _trajectory_rule(
        kpi_name="nimal",
        source="kpi",
        comparator="lt",
        threshold=5,
        lookback_prints=4,
        horizon_prints=2,
    )
    [result] = evaluate_soft_rules("MELI", [rule], conn)
    assert result.status == SoftRuleStatus.GREEN
    assert result.details["trip_h"] is None


def test_trajectory_green_when_flat(conn: sqlite3.Connection) -> None:
    """Zero-slope series never projects a crossing → GREEN, not a crash and
    not UNRESOLVED (a flat trend is a resolvable, real answer: "not trending
    toward the floor")."""
    _seed_kpi(conn, "MELI", "nimal", [20.0, 20.0, 20.0, 20.0])
    rule = _trajectory_rule(
        kpi_name="nimal",
        source="kpi",
        comparator="lt",
        threshold=15,
        lookback_prints=4,
        horizon_prints=2,
    )
    [result] = evaluate_soft_rules("MELI", [rule], conn)
    assert result.status == SoftRuleStatus.GREEN
    assert result.details["slope_per_period"] == 0.0


def test_trajectory_unresolved_when_thin(conn: sqlite3.Connection) -> None:
    """Fewer prints than lookback_prints → UNRESOLVED, never a silent GREEN."""
    _seed_kpi(conn, "MELI", "nimal", [23.0, 21.0])
    rule = _trajectory_rule(
        kpi_name="nimal",
        source="kpi",
        comparator="lt",
        threshold=15,
        lookback_prints=4,
        horizon_prints=2,
    )
    [result] = evaluate_soft_rules("MELI", [rule], conn)
    assert result.status == SoftRuleStatus.UNRESOLVED
    assert "insufficient" in result.evidence.lower()


def test_trajectory_rejects_lookback_below_minimum(conn: sqlite3.Connection) -> None:
    """lookback_prints < 3 is an invalid predicate param → UNRESOLVED (raised,
    caught, surfaced — not silently ignored)."""
    _seed_kpi(conn, "MELI", "nimal", [23.0, 21.0, 19.0])
    rule = _trajectory_rule(
        kpi_name="nimal",
        source="kpi",
        comparator="lt",
        threshold=15,
        lookback_prints=2,
    )
    [result] = evaluate_soft_rules("MELI", [rule], conn)
    assert result.status == SoftRuleStatus.UNRESOLVED
    assert "did not evaluate" in result.evidence.lower()


# ---------------------------------------------------------------------------
# derived: "delta" — consecutive-print differences over a cumulative series,
# with the monotonicity + unit-jump data-quality guard (red-team PR2 item 2).
# ---------------------------------------------------------------------------


def test_series_below_derived_delta_fires_on_low_net_adds(conn: sqlite3.Connection) -> None:
    """delta(Total customers) < 7 for the latest print → YELLOW. Levels
    80 -> 85 -> 90 -> 96 give a last delta of 6, which is < 7."""
    _seed_kpi(conn, "NU", "Total customers (millions)", [80.0, 85.0, 90.0, 96.0])
    rule = SoftRule(
        name="net_adds_below_7",
        predicate=SoftRulePredicate(
            type=PredicateType.SERIES_BELOW,
            params={
                "metric": "Total customers (millions)",
                "source": "kpi",
                "derived": "delta",
                "threshold": 7,
                "periods": 1,
            },
        ),
    )
    [result] = evaluate_soft_rules("NU", [rule], conn)
    assert result.status == SoftRuleStatus.YELLOW
    assert result.details["last_value"] == pytest.approx(6.0)


def test_derived_delta_unresolved_on_non_monotonic_series(conn: sqlite3.Connection) -> None:
    """A cumulative series that decreases (80 -> 85 -> 82 -> 90) can't produce
    a trustworthy delta → UNRESOLVED with a data-quality reason, never a
    garbage delta."""
    _seed_kpi(conn, "NU", "Total customers (millions)", [80.0, 85.0, 82.0, 90.0])
    rule = SoftRule(
        name="net_adds_below_7",
        predicate=SoftRulePredicate(
            type=PredicateType.SERIES_BELOW,
            params={
                "metric": "Total customers (millions)",
                "source": "kpi",
                "derived": "delta",
                "threshold": 7,
                "periods": 1,
            },
        ),
    )
    [result] = evaluate_soft_rules("NU", [rule], conn)
    assert result.status == SoftRuleStatus.UNRESOLVED
    assert "non-monotonic" in result.evidence.lower()


def test_derived_delta_unresolved_on_unit_discontinuity(conn: sqlite3.Connection) -> None:
    """A raw-count row (114,000,000) landing inside a millions-scale series —
    the exact NU 'Total customers' def 641 corruption the red-team audit
    found — trips the >1000x jump guard → UNRESOLVED, never a garbage delta."""
    _seed_kpi(conn, "NU", "Total customers (millions)", [119.0, 114_000_000.0, 110.0, 114.0])
    rule = SoftRule(
        name="net_adds_below_7",
        predicate=SoftRulePredicate(
            type=PredicateType.SERIES_BELOW,
            params={
                "metric": "Total customers (millions)",
                "source": "kpi",
                "derived": "delta",
                "threshold": 7,
                "periods": 1,
            },
        ),
    )
    [result] = evaluate_soft_rules("NU", [rule], conn)
    assert result.status == SoftRuleStatus.UNRESOLVED
    assert "unit discontinuity" in result.evidence.lower()


def test_trajectory_derived_delta_projects_net_adds_crossing(conn: sqlite3.Connection) -> None:
    """trajectory + derived:"delta" together: net-adds decelerating toward 0
    projects a future crossing → YELLOW with a named trip period."""
    # Levels giving deltas [8, 6, 4, 2, 1, 0.5, 0.2] — last 4 deltas [2, 1, 0.5, 0.2].
    _seed_kpi(
        conn,
        "NU",
        "Total customers (millions)",
        [100.0, 108.0, 114.0, 118.0, 120.0, 121.0, 121.5, 121.7],
    )
    rule = _trajectory_rule(
        kpi_name="Total customers (millions)",
        source="kpi",
        derived="delta",
        comparator="lt",
        threshold=0,
        lookback_prints=4,
        horizon_prints=2,
    )
    [result] = evaluate_soft_rules("NU", [rule], conn)
    assert result.status == SoftRuleStatus.YELLOW
    assert result.details["already_violating"] is False
    assert result.details["trip_h"] is not None


# ---------------------------------------------------------------------------
# compound — three-valued (Kleene) logic so an UNRESOLVED child never gets
# silently absorbed into a plain GREEN/YELLOW verdict (red-team PR2 item 3:
# NU's "net adds <5M/Q AND Brazil penetration declining QoQ" must stay
# visible-amber when the penetration leg has no data).
# ---------------------------------------------------------------------------


def _below_rule_params(metric: str, *, threshold: float, periods: int = 1) -> dict[str, Any]:
    return {
        "type": "series_below",
        "params": {"metric": metric, "source": "kpi", "threshold": threshold, "periods": periods},
    }


def test_compound_and_unresolved_when_one_child_has_no_data(conn: sqlite3.Connection) -> None:
    """AND(fired=True, unresolved) = UNRESOLVED — a definitively-fired leg
    can't be laundered into GREEN just because its sibling has no data, and it
    can't silently read as a full BREACH-adjacent fire either. Mirrors NU's
    net-adds (fires) AND Brazil-penetration (def 639, zero rows) compound."""
    _seed_kpi(conn, "NU", "net_adds", [3.0])  # < 5 → fires
    # "brazil_penetration" has no seeded facts at all → insufficient data.
    rule = SoftRule(
        name="net_adds_and_penetration",
        predicate=SoftRulePredicate(
            type=PredicateType.COMPOUND,
            params={
                "op": "and",
                "predicates": [
                    _below_rule_params("net_adds", threshold=5),
                    _below_rule_params("brazil_penetration", threshold=0),
                ],
            },
        ),
    )
    [result] = evaluate_soft_rules("NU", [rule], conn)
    assert result.status == SoftRuleStatus.UNRESOLVED
    assert result.details["children"][0]["fired"] is True
    assert result.details["children"][1]["fired"] is None


def test_compound_and_false_wins_over_unresolved_sibling(conn: sqlite3.Connection) -> None:
    """AND(False, unresolved) = False — a leg that definitively did NOT fire
    still resolves the AND to False even though its sibling is unresolved."""
    _seed_kpi(conn, "NU", "net_adds", [9.0])  # not < 5 → does not fire
    rule = SoftRule(
        name="net_adds_and_penetration",
        predicate=SoftRulePredicate(
            type=PredicateType.COMPOUND,
            params={
                "op": "and",
                "predicates": [
                    _below_rule_params("net_adds", threshold=5),
                    _below_rule_params("brazil_penetration", threshold=0),
                ],
            },
        ),
    )
    [result] = evaluate_soft_rules("NU", [rule], conn)
    assert result.status == SoftRuleStatus.GREEN


def test_compound_or_true_wins_over_unresolved_sibling(conn: sqlite3.Connection) -> None:
    """OR(True, unresolved) = True — one definitively-fired leg is enough,
    regardless of a sibling with no data."""
    _seed_kpi(conn, "NU", "net_adds", [3.0])  # < 5 → fires
    rule = SoftRule(
        name="net_adds_or_penetration",
        predicate=SoftRulePredicate(
            type=PredicateType.COMPOUND,
            params={
                "op": "or",
                "predicates": [
                    _below_rule_params("net_adds", threshold=5),
                    _below_rule_params("brazil_penetration", threshold=0),
                ],
            },
        ),
    )
    [result] = evaluate_soft_rules("NU", [rule], conn)
    assert result.status == SoftRuleStatus.YELLOW


def test_compound_or_unresolved_when_no_true_and_one_unresolved(conn: sqlite3.Connection) -> None:
    """OR(False, unresolved) = UNRESOLVED — no leg has fired, but one leg
    couldn't be checked, so the compound can't be declared clean either."""
    _seed_kpi(conn, "NU", "net_adds", [9.0])  # not < 5 → does not fire
    rule = SoftRule(
        name="net_adds_or_penetration",
        predicate=SoftRulePredicate(
            type=PredicateType.COMPOUND,
            params={
                "op": "or",
                "predicates": [
                    _below_rule_params("net_adds", threshold=5),
                    _below_rule_params("brazil_penetration", threshold=0),
                ],
            },
        ),
    )
    [result] = evaluate_soft_rules("NU", [rule], conn)
    assert result.status == SoftRuleStatus.UNRESOLVED
