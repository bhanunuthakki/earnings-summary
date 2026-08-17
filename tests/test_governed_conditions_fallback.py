"""Unit tests for Governed Conditions fallback and shadowing defense (BHA-68)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from pipeline.work_os_decisions import build_decision_projection


def _create_decisions_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE decisions (
            id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            recommendation_kind TEXT NOT NULL,
            recommendation_value REAL,
            conviction TEXT,
            source_lens TEXT,
            decided_by TEXT NOT NULL,
            decision_conditions TEXT,
            made_at TEXT NOT NULL,
            size_pct REAL
        )
        """
    )
    return conn


def test_build_decision_projection_owner_overrides_when_populated() -> None:
    conn = _create_decisions_db()
    owner_conds = json.dumps(
        [
            {
                "metric": "Services Gross Margin",
                "op": "ge",
                "threshold": 75.0,
                "unit": "percent",
                "for_periods": 1,
                "note": "Services GM stays >=75%",
            }
        ]
    )
    model_conds = json.dumps(
        [
            {
                "metric": "iPhone Revenue",
                "op": "ge",
                "threshold": 40.0,
                "unit": "billions",
                "for_periods": 1,
                "note": "iPhone revenue beats $40B",
            }
        ]
    )

    conn.execute(
        "INSERT INTO decisions (id, ticker, recommendation_kind, decided_by, conviction, size_pct, made_at, decision_conditions) "
        "VALUES (101, 'AAPL', 'add', 'owner', 'high', 5.0, '2026-08-01T12:00:00+00:00', ?)",
        (owner_conds,),
    )
    conn.execute(
        "INSERT INTO decisions (id, ticker, recommendation_kind, decided_by, conviction, size_pct, made_at, decision_conditions) "
        "VALUES (99, 'AAPL', 'add', 'advisor', 'medium', 3.0, '2026-07-20T12:00:00+00:00', ?)",
        (model_conds,),
    )

    _projection, conditions, _issues = build_decision_projection(
        conn, "AAPL", as_of=datetime(2026, 8, 15, tzinfo=UTC)
    )

    assert len(conditions) == 1
    assert conditions[0].metric == "Services Gross Margin"
    assert conditions[0].decision_id == 101
    assert conditions[0].stable_id == "decision:101:condition:0"
    assert conditions[0].origin == "owner"


def test_build_decision_projection_falls_back_to_model_when_owner_conditions_empty() -> None:
    # WIX exact reproduction case: Owner Decision 135 with empty conditions, Advisor Decision 98 with 4 active conditions
    conn = _create_decisions_db()
    model_conds = json.dumps(
        [
            {
                "metric": "Base44 ARR",
                "op": "lt",
                "threshold": 200.0,
                "unit": "millions",
                "for_periods": 1,
                "note": "Base44 ARR remains below $200M",
            },
            {
                "metric": "Creative Subscriptions CC growth",
                "op": "lt",
                "threshold": 5.0,
                "unit": "percent",
                "for_periods": 1,
                "note": "Creative Subs CC drops below 5%",
            },
            {
                "metric": "FCF margin",
                "op": "lt",
                "threshold": 20.0,
                "unit": "percent",
                "for_periods": 1,
                "note": "FCF margin guidance cut below 20%",
            },
            {
                "metric": "Base44 ARR",
                "op": "lt",
                "threshold": 150.0,
                "unit": "millions",
                "for_periods": 1,
                "note": "Base44 remains below $150M",
            },
        ]
    )

    conn.execute(
        "INSERT INTO decisions (id, ticker, recommendation_kind, decided_by, conviction, size_pct, made_at, decision_conditions) "
        "VALUES (135, 'WIX', 'sell', 'owner', NULL, 2.5444, '2026-08-14T08:46:19+00:00', '[]')"
    )
    conn.execute(
        "INSERT INTO decisions (id, ticker, recommendation_kind, decided_by, conviction, size_pct, made_at, decision_conditions) "
        "VALUES (98, 'WIX', 'sell', 'advisor', NULL, NULL, '2026-07-15T11:32:52+00:00', ?)",
        (model_conds,),
    )

    _projection, conditions, _issues = build_decision_projection(
        conn, "WIX", as_of=datetime(2026, 8, 15, tzinfo=UTC)
    )

    # Should NOT be shadowed to 0 conditions; must fall back to the 4 model conditions
    assert len(conditions) == 4
    assert conditions[0].metric == "Base44 ARR"
    assert conditions[0].decision_id == 98
    assert conditions[0].stable_id == "decision:98:condition:0"
    assert conditions[0].origin == "model"
    assert conditions[1].metric == "Creative Subscriptions CC growth"
    assert conditions[2].metric == "FCF margin"
    assert conditions[3].metric == "Base44 ARR"


def test_build_decision_projection_empty_when_both_empty() -> None:
    conn = _create_decisions_db()
    conn.execute(
        "INSERT INTO decisions (id, ticker, recommendation_kind, decided_by, made_at, decision_conditions) "
        "VALUES (135, 'WIX', 'sell', 'owner', '2026-08-14T08:46:19+00:00', '[]')"
    )
    conn.execute(
        "INSERT INTO decisions (id, ticker, recommendation_kind, decided_by, made_at, decision_conditions) "
        "VALUES (98, 'WIX', 'sell', 'advisor', '2026-07-15T11:32:52+00:00', '[]')"
    )

    _projection, conditions, _issues = build_decision_projection(
        conn, "WIX", as_of=datetime(2026, 8, 15, tzinfo=UTC)
    )

    assert len(conditions) == 0
    assert _issues == []
