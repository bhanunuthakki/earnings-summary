"""End-to-end tests for the decision-condition trigger (fund-grade S5, PR 1).

The sensor is deterministic — no LLM anywhere in the lifecycle — so the
contract under test is the break-rule-engine reuse:

  * Protocol conformance + kind/cadence
  * scan: a condition satisfied for ``for_periods`` consecutive periods →
    BREACH → candidate; satisfied-but-not-consecutive (WARN) → none;
    graded decisions skipped; unresolved (metric_source null) skipped
  * unit reconciliation through the shared ``convert_unit`` path: a fact
    stored in raw dollars satisfies a threshold declared in millions
    (the #317/#320 bug class), and a cross-family mismatch (percent fact vs
    millions threshold) is UNRESOLVED — skipped, never mis-compared
  * the financial_facts path (canonical line_item, per-period dedup)
  * signature_key_evidence keys (decision_id, condition_index, period_end)
    and matches the sha ``build_alert`` persists
  * build_alert: deterministic memo naming the decision + condition + latest
    observation
  * draft_actions: one thesis_update carrying the decision linkage
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from alerts.store import compute_signature_sha
from triggers import DecisionConditionTrigger, UserStateContext
from triggers.base import Cadence, Trigger, TriggerCandidate

# ---------------------------------------------------------------------------
# Fixture DB
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    recommendation_kind VARCHAR(32) NOT NULL,
    recommendation_value FLOAT,
    conviction VARCHAR(16),
    source_artifact_id INTEGER,
    source_memo_id INTEGER,
    source_lens VARCHAR(64),
    rationale_excerpt TEXT,
    made_at DATETIME NOT NULL,
    outcome_at DATETIME,
    outcome_label VARCHAR(16),
    decision_conditions TEXT,
    conditions_extracted_at DATETIME,
    decided_by VARCHAR(16),
    created_at DATETIME NOT NULL
);
CREATE TABLE tracked_companies (
    ticker TEXT PRIMARY KEY,
    list_type TEXT NOT NULL,
    archived_at DATETIME
);
CREATE TABLE kpi_definitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    name TEXT NOT NULL,
    unit TEXT
);
CREATE TABLE kpi_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    period_end TEXT NOT NULL,
    fiscal_period_type TEXT NOT NULL,
    kpi_definition_id INTEGER NOT NULL,
    value REAL NOT NULL,
    unit TEXT,
    source_doc_id INTEGER
);
CREATE TABLE financial_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    period_end TEXT NOT NULL,
    fiscal_period_type TEXT NOT NULL,
    line_item TEXT NOT NULL,
    value REAL NOT NULL,
    unit TEXT,
    source_doc_id INTEGER
);
"""

_NOW = datetime.now(UTC).replace(tzinfo=None).isoformat()
_MADE_AT = "2026-04-02T08:00:00"


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(str(tmp_path / "t.db"))
    c.executescript(_SCHEMA)
    # NU is a held name — advisor-authored conditions on it stay push-eligible
    # (the relevance gate; see the dedicated gate tests for the other lists).
    c.execute("INSERT INTO tracked_companies (ticker, list_type) VALUES ('NU', 'portfolio')")
    c.commit()
    return c


def _condition(**overrides: object) -> dict[str, object]:
    # baseline_period_end predates every observation the tests insert, so the
    # breach-freshness watermark passes deterministically (no dependence on
    # the wall clock via the legacy 135-day fallback).
    base: dict[str, object] = {
        "metric": "NPL 90d+",
        "metric_source": "kpi",
        "op": "gt",
        "threshold": 7.0,
        "unit": "percent",
        "for_periods": 2,
        "note": "90d+ NPLs above 7% for two straight quarters",
        "baseline_period_end": "2025-06-30",
    }
    base.update(overrides)
    return base


def _insert_decision(
    conn: sqlite3.Connection,
    conditions: list[dict[str, object]],
    *,
    ticker: str = "NU",
    outcome_at: str | None = None,
    decided_by: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO decisions (ticker, recommendation_kind, recommendation_value, "
        "source_artifact_id, source_lens, made_at, outcome_at, decision_conditions, "
        "decided_by, created_at) "
        "VALUES (?, 'trim', 20.0, 1, 'five_min_reread', ?, ?, ?, ?, ?)",
        (ticker, _MADE_AT, outcome_at, json.dumps(conditions), decided_by, _NOW),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def _insert_kpi_series(
    conn: sqlite3.Connection,
    *,
    name: str = "NPL 90d+",
    unit: str = "percent",
    values: list[tuple[str, float]],
    ticker: str = "NU",
) -> None:
    cur = conn.execute(
        "INSERT INTO kpi_definitions (ticker, name, unit) VALUES (?, ?, ?)", (ticker, name, unit)
    )
    def_id = int(cur.lastrowid or 0)
    for i, (period_end, value) in enumerate(values):
        quarter = f"Q{(i % 4) + 1}"
        conn.execute(
            "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, "
            "kpi_definition_id, value, unit, source_doc_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ticker, period_end, quarter, def_id, value, unit, i + 1),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


def test_protocol_conformance() -> None:
    trigger = DecisionConditionTrigger()
    assert isinstance(trigger, Trigger)
    assert DecisionConditionTrigger.kind == "decision_condition"
    assert DecisionConditionTrigger.cadence is Cadence.DAILY


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------


def test_scan_fires_on_consecutive_breach(conn: sqlite3.Connection) -> None:
    decision_id = _insert_decision(conn, [_condition()])
    _insert_kpi_series(
        conn,
        values=[("2025-12-31", 7.2), ("2026-03-31", 7.5)],  # both above 7
    )
    candidates = DecisionConditionTrigger().scan("NU", conn)
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand.kind == "decision_condition"
    assert cand.evidence["decision_id"] == decision_id
    assert cand.evidence["condition_index"] == 0
    assert cand.evidence["period_end"] == "2026-03-31"
    assert cand.evidence["latest_value"] == 7.5
    assert cand.evidence["recommendation_kind"] == "trim"


def test_scan_warn_not_consecutive_does_not_fire(conn: sqlite3.Connection) -> None:
    _insert_decision(conn, [_condition()])
    # Latest breaches but the prior period doesn't — WARN, not BREACH.
    _insert_kpi_series(conn, values=[("2025-12-31", 6.5), ("2026-03-31", 7.5)])
    assert DecisionConditionTrigger().scan("NU", conn) == []


def test_scan_skips_graded_unresolved_and_no_data(conn: sqlite3.Connection) -> None:
    # Graded decision — never resurfaced.
    _insert_decision(conn, [_condition()], outcome_at=_NOW)
    # Open but unresolved metric — display-only.
    _insert_decision(conn, [_condition(metric_source=None)])
    # Open + resolvable, but the metric has no kpi rows — UNRESOLVED, no fire.
    cur = conn.execute(
        "INSERT INTO decisions (ticker, recommendation_kind, source_artifact_id, made_at, "
        "decision_conditions, created_at) VALUES ('NU', 'add', 9, ?, ?, ?)",
        (_MADE_AT, json.dumps([_condition(metric="Ghost KPI")]), _NOW),
    )
    assert cur.lastrowid is not None
    conn.commit()
    assert DecisionConditionTrigger().scan("NU", conn) == []


def test_scan_unit_reconciliation_magnitude_family(conn: sqlite3.Connection) -> None:
    """The #317/#320 class: facts stored in raw dollars, threshold declared in
    millions. 80_000_000 actual < 100 millions must fire (not 8e7 < 100 raw)."""
    _insert_decision(
        conn,
        [
            _condition(
                metric="Quarterly FCF", op="lt", threshold=100.0, unit="millions", for_periods=1
            )
        ],
    )
    _insert_kpi_series(
        conn, name="Quarterly FCF", unit="actual", values=[("2026-03-31", 80_000_000.0)]
    )
    candidates = DecisionConditionTrigger().scan("NU", conn)
    assert len(candidates) == 1
    # Evidence snapshot reads in the condition's unit (millions), not raw.
    assert candidates[0].evidence["latest_value"] == 80.0


def test_scan_cross_family_unit_mismatch_never_fires(conn: sqlite3.Connection) -> None:
    """A percent-stored fact against a millions threshold has no valid
    conversion — UNRESOLVED, skipped, never blindly compared."""
    _insert_decision(
        conn,
        [_condition(metric="Margin-ish", op="lt", threshold=100.0, unit="millions", for_periods=1)],
    )
    _insert_kpi_series(conn, name="Margin-ish", unit="percent", values=[("2026-03-31", 12.0)])
    assert DecisionConditionTrigger().scan("NU", conn) == []


def test_scan_financial_line_item_path_with_dedup(conn: sqlite3.Connection) -> None:
    decision_id = _insert_decision(
        conn,
        [
            _condition(
                metric="free_cash_flow",
                metric_source="financial",
                op="lt",
                threshold=1.5,
                unit="billions",
                for_periods=1,
            )
        ],
    )
    # Two rows for the same period — the later-ingested (higher source_doc_id)
    # restatement must win: 1.2B (fires), superseding 1.8B (wouldn't).
    for doc_id, value in ((1, 1_800_000_000.0), (2, 1_200_000_000.0)):
        conn.execute(
            "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type, line_item, "
            "value, unit, source_doc_id) VALUES ('NU', '2026-03-31', 'Q1', 'free_cash_flow', ?, "
            "'actual', ?)",
            (value, doc_id),
        )
    conn.commit()
    candidates = DecisionConditionTrigger().scan("NU", conn)
    assert len(candidates) == 1
    assert candidates[0].evidence["decision_id"] == decision_id
    assert candidates[0].evidence["latest_value"] == 1.2  # reconciled to billions


# ---------------------------------------------------------------------------
# Relevance gate (alert-quality fix): owner falsifiers always push;
# advisor conditions push only on portfolio names.
# ---------------------------------------------------------------------------

_BREACHING = [("2025-12-31", 7.2), ("2026-03-31", 7.5)]


def test_scan_gates_advisor_decision_on_evaluation_ticker(conn: sqlite3.Connection) -> None:
    """The 2026-07-01 sweep class: an advisor-authored decision on an
    evaluation-list name breaches — evaluable on panels, but never a push."""
    conn.execute("INSERT INTO tracked_companies (ticker, list_type) VALUES ('FCX', 'evaluation')")
    _insert_decision(conn, [_condition()], ticker="FCX", decided_by="advisor")
    _insert_kpi_series(conn, values=_BREACHING, ticker="FCX")
    assert DecisionConditionTrigger().scan("FCX", conn) == []

    # The decision is still EVALUATED — the armed-falsifiers panel reads it.
    from decision_conditions import load_open_decisions

    conn.row_factory = sqlite3.Row
    assert len(load_open_decisions(conn, "FCX")) == 1


def test_scan_owner_decision_fires_on_any_list(conn: sqlite3.Connection) -> None:
    """The owner's own falsifier interrupts regardless of list membership."""
    conn.execute("INSERT INTO tracked_companies (ticker, list_type) VALUES ('FCX', 'evaluation')")
    _insert_decision(conn, [_condition()], ticker="FCX", decided_by="owner")
    _insert_kpi_series(conn, values=_BREACHING, ticker="FCX")
    candidates = DecisionConditionTrigger().scan("FCX", conn)
    assert len(candidates) == 1
    assert candidates[0].evidence["decided_by"] == "owner"


def test_scan_advisor_decision_on_portfolio_ticker_fires(conn: sqlite3.Connection) -> None:
    """Advisor conditions guarding a HELD name (the praised WIX-reeval shape)
    still push. Legacy rows with no decided_by (pre-0130 NULL) count as
    advisor and ride the same portfolio gate."""
    _insert_decision(conn, [_condition()], decided_by="advisor")  # NU = portfolio (fixture)
    _insert_kpi_series(conn, values=_BREACHING)
    candidates = DecisionConditionTrigger().scan("NU", conn)
    assert len(candidates) == 1
    assert candidates[0].evidence["decided_by"] == "advisor"


def test_scan_gates_advisor_decision_on_untracked_ticker(conn: sqlite3.Connection) -> None:
    """No tracked_companies row at all (or an archived one) is not a
    portfolio holding — advisor pushes stay gated."""
    _insert_decision(conn, [_condition()], ticker="GHST", decided_by="advisor")
    _insert_kpi_series(conn, values=_BREACHING, ticker="GHST")
    assert DecisionConditionTrigger().scan("GHST", conn) == []


# ---------------------------------------------------------------------------
# Breach-freshness watermark: a condition already true at attach time is
# display state, never news.
# ---------------------------------------------------------------------------


def test_scan_breach_at_or_before_baseline_never_fires(conn: sqlite3.Connection) -> None:
    """The FCX #13 class: the breaching observation predates (or equals) the
    attach-time baseline — the 'breach' was already known when the condition
    was written."""
    _insert_decision(conn, [_condition(baseline_period_end="2026-03-31")], decided_by="owner")
    _insert_kpi_series(conn, values=_BREACHING)  # latest obs = 2026-03-31
    assert DecisionConditionTrigger().scan("NU", conn) == []


def test_scan_breach_strictly_after_baseline_fires(conn: sqlite3.Connection) -> None:
    _insert_decision(conn, [_condition(baseline_period_end="2026-03-30")], decided_by="owner")
    _insert_kpi_series(conn, values=_BREACHING)  # latest obs = 2026-03-31
    candidates = DecisionConditionTrigger().scan("NU", conn)
    assert len(candidates) == 1
    assert candidates[0].evidence["baseline_period_end"] == "2026-03-30"


def test_scan_legacy_condition_stale_observation_silent(conn: sqlite3.Connection) -> None:
    """A pre-watermark condition (no baseline stamped) falls back to the
    135-day freshness window: a breach observed 10 months ago is history."""
    now = datetime.now(UTC).replace(tzinfo=None)
    old = [
        ((now - timedelta(days=390)).date().isoformat(), 7.2),
        ((now - timedelta(days=300)).date().isoformat(), 7.5),
    ]
    _insert_decision(conn, [_condition(baseline_period_end=None)], decided_by="owner")
    _insert_kpi_series(conn, values=old)
    assert DecisionConditionTrigger().scan("NU", conn) == []


def test_scan_legacy_condition_fresh_observation_fires(conn: sqlite3.Connection) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    fresh = [
        ((now - timedelta(days=120)).date().isoformat(), 7.2),
        ((now - timedelta(days=30)).date().isoformat(), 7.5),
    ]
    _insert_decision(conn, [_condition(baseline_period_end=None)], decided_by="owner")
    _insert_kpi_series(conn, values=fresh)
    assert len(DecisionConditionTrigger().scan("NU", conn)) == 1


# ---------------------------------------------------------------------------
# Milestone window (not_before): a forward-looking condition ("reaches $200M
# ARR in ~12 months") is not evaluated before its horizon opens.
# ---------------------------------------------------------------------------


def test_scan_skips_condition_with_future_not_before(conn: sqlite3.Connection) -> None:
    """The milestone-dated tripwire: breaching data exists TODAY (the milestone
    is trivially unmet the day it is written), but the window hasn't opened —
    no candidate. The condition is still displayed (load_open_decisions)."""
    future = (datetime.now(UTC).replace(tzinfo=None) + timedelta(days=200)).date().isoformat()
    _insert_decision(conn, [_condition(not_before=future)], decided_by="owner")
    _insert_kpi_series(conn, values=_BREACHING)
    assert DecisionConditionTrigger().scan("NU", conn) == []

    # Display surfaces still see the condition — only evaluation is deferred.
    from decision_conditions import load_open_decisions

    conn.row_factory = sqlite3.Row
    [decision] = load_open_decisions(conn, "NU")
    assert decision.conditions[0].not_before == future


def test_scan_evaluates_condition_with_past_not_before(conn: sqlite3.Connection) -> None:
    """Once the milestone window has opened (not_before in the past), the
    condition evaluates exactly like any other."""
    past = (datetime.now(UTC).replace(tzinfo=None) - timedelta(days=5)).date().isoformat()
    _insert_decision(conn, [_condition(not_before=past)], decided_by="owner")
    _insert_kpi_series(conn, values=_BREACHING)
    candidates = DecisionConditionTrigger().scan("NU", conn)
    assert len(candidates) == 1
    assert candidates[0].evidence["not_before"] == past


def test_scan_legacy_condition_without_not_before_unchanged(conn: sqlite3.Connection) -> None:
    """Legacy stored JSON (no not_before key) keeps firing as before — the
    gate only defers explicitly future-dated milestones."""
    _insert_decision(conn, [_condition()], decided_by="owner")
    _insert_kpi_series(conn, values=_BREACHING)
    candidates = DecisionConditionTrigger().scan("NU", conn)
    assert len(candidates) == 1
    assert candidates[0].evidence["not_before"] is None


# ---------------------------------------------------------------------------
# should_fire / signature / build_alert / draft_actions
# ---------------------------------------------------------------------------


def _fired_candidate(conn: sqlite3.Connection) -> TriggerCandidate:
    _insert_decision(conn, [_condition()])
    _insert_kpi_series(conn, values=[("2025-12-31", 7.2), ("2026-03-31", 7.5)])
    [candidate] = DecisionConditionTrigger().scan("NU", conn)
    return candidate


def _empty_user_state() -> UserStateContext:
    return UserStateContext(
        registered_kpis=[], sizing_intents=[], recent_dismissed_signatures=set()
    )


def test_should_fire_and_signature_contract(conn: sqlite3.Connection) -> None:
    trigger = DecisionConditionTrigger()
    candidate = _fired_candidate(conn)
    assert trigger.should_fire(candidate, _empty_user_state())

    key_evidence = trigger.signature_key_evidence(candidate)
    assert set(key_evidence) == {"decision_id", "condition_index", "period_end"}

    alert = trigger.build_alert(candidate, None)
    assert alert.signature_sha == compute_signature_sha("decision_condition", "NU", key_evidence)
    assert alert.fired_at.tzinfo is None  # naive-UTC convention


def test_build_alert_memo_and_evidence(conn: sqlite3.Connection) -> None:
    trigger = DecisionConditionTrigger()
    candidate = _fired_candidate(conn)
    alert = trigger.build_alert(candidate, None)

    assert alert.trigger_kind == "decision_condition"
    assert alert.memo_text is not None
    assert "Falsifiable condition met" in alert.memo_text
    assert "2026-04-02 TRIM 20% decision" in alert.memo_text
    assert "NPL 90d+ > 7 percent for 2 consecutive periods" in alert.memo_text
    assert "7.5 percent @ 2026-03-31" in alert.memo_text
    assert "Revisit that decision." in alert.memo_text
    assert '"90d+ NPLs above 7% for two straight quarters"' in alert.memo_text

    evidence = json.loads(alert.evidence_json)
    assert evidence["op"] == "gt"
    assert evidence["observed"] == [
        {"period_end": "2026-03-31", "value": 7.5},
        {"period_end": "2025-12-31", "value": 7.2},
    ]


def test_draft_actions_thesis_update(conn: sqlite3.Connection) -> None:
    trigger = DecisionConditionTrigger()
    candidate = _fired_candidate(conn)
    alert = trigger.build_alert(candidate, None)
    actions = trigger.draft_actions(alert, candidate)
    assert len(actions) == 1
    action = actions[0]
    assert action.action_kind == "thesis_update"
    assert action.payload["decision_id"] == candidate.evidence["decision_id"]
    assert action.payload["condition_index"] == 0
    body = action.payload["body"]
    assert isinstance(body, str)
    assert "Revisit that decision" in body
    assert "change your mind" in body
