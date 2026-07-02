"""Tests for the deterministic workload inventory (meta_eval_governance.md §1.1, PR1).

Pure ledger reads — no LLM calls. Seeds a temp SQLite DB and asserts the leverage
ranking, the headroom math (computed from the real model_ladder, not hardcoded),
eval-scope exclusion, incumbent-resolution precedence, the web/budget flags, the
CANDIDATE_ERRORED exclusion, and the META_PURPOSES belt-and-braces filter.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from llm.cli import DEFAULT_MODEL
from llm.eval_scopes import EVAL_SCOPES
from llm.model_ladder import model_cost
from llm.workload_inventory import (
    PurposeWorkload,
    build_workload_inventory,
    render_inventory_text,
    risk_note_for,
)

_RECENT = (datetime.now(UTC) - timedelta(days=1)).replace(tzinfo=None).isoformat()
_ZZZ = "zzz_fake_purpose_xyz"  # deliberately absent from LLM_MODELS → DEFAULT_MODEL


def _schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE llm_calls (
            id INTEGER PRIMARY KEY, called_at TEXT NOT NULL, purpose TEXT, ticker TEXT,
            scope TEXT, model TEXT NOT NULL, prompt_sha256 TEXT NOT NULL,
            prompt_chars INTEGER NOT NULL, cost_estimate_usd REAL,
            input_tokens INTEGER, output_tokens INTEGER
        );
        CREATE TABLE model_pin_overrides (
            id INTEGER PRIMARY KEY, purpose TEXT NOT NULL, model TEXT NOT NULL, active INTEGER
        );
        CREATE TABLE model_eval_verdicts (
            id INTEGER PRIMARY KEY, purpose TEXT NOT NULL, candidate TEXT NOT NULL,
            verdict TEXT NOT NULL, recorded_at TEXT NOT NULL
        );
        CREATE TABLE llm_budgets (
            id INTEGER PRIMARY KEY, purpose TEXT NOT NULL, on_exceed TEXT NOT NULL
        );
        """
    )


def _call(
    conn: sqlite3.Connection,
    *,
    purpose: str,
    scope: str | None,
    cost: float,
    sha: str,
    chars: int = 1000,
    model: str = "claude-sonnet-4-6",
) -> None:
    conn.execute(
        """INSERT INTO llm_calls
           (called_at, purpose, ticker, scope, model, prompt_sha256, prompt_chars,
            cost_estimate_usd, input_tokens, output_tokens)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (_RECENT, purpose, "META", scope, model, sha, chars, cost, 100, 50),
    )


@pytest.fixture
def inv_db(tmp_path: Path) -> Path:
    """A seeded portfolio.db: alpha (pinned to Opus, $100 prod + a $999 eval-scope row
    that must be excluded), zzz (no pin → DEFAULT_MODEL, $10), web_p (web-scoped, $20),
    and eval_judge (a META purpose that must never appear)."""
    db_path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(db_path))
    try:
        _schema(conn)
        _call(conn, purpose="alpha", scope=None, cost=60.0, sha="a1", model="claude-opus-4-8")
        _call(conn, purpose="alpha", scope=None, cost=40.0, sha="a2", model="claude-opus-4-8")
        _call(conn, purpose="alpha", scope="model_eval", cost=999.0, sha="aX", chars=10)
        _call(conn, purpose=_ZZZ, scope=None, cost=10.0, sha="z1")
        _call(conn, purpose="web_p", scope="web", cost=20.0, sha="w1")
        _call(conn, purpose="eval_judge", scope=None, cost=50.0, sha="e1")  # META → excluded
        conn.execute(
            "INSERT INTO model_pin_overrides (purpose, model, active) VALUES (?, ?, 1)",
            ("alpha", "claude-opus-4-8"),
        )
        conn.executemany(
            "INSERT INTO model_eval_verdicts (purpose, candidate, verdict, recorded_at) "
            "VALUES (?, ?, ?, ?)",
            [
                ("alpha", "gemini-3-flash-preview", "SWITCH_DOWN", "2026-06-01T00:00:00"),
                ("alpha", "claude-haiku-4-5-20251001", "CANDIDATE_ERRORED", "2026-07-01T00:00:00"),
            ],
        )
        conn.executemany(
            "INSERT INTO llm_budgets (purpose, on_exceed) VALUES (?, ?)",
            [("alpha", "block"), (_ZZZ, "warn"), ("__default__", "warn")],
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _by_purpose(rows: list[PurposeWorkload]) -> dict[str, PurposeWorkload]:
    return {r.purpose: r for r in rows}


def test_eval_scopes_is_the_union_including_eval() -> None:
    # §10.2: the canonical set must include 'eval' (the panel already treats it as
    # measurement) plus the two reserved-ahead scopes.
    assert {"model_eval", "backend_judge", "eval", "prompt_ab", "meta_eval"} <= EVAL_SCOPES


def test_risk_note_for_exact_and_family() -> None:
    assert risk_note_for("bear_case") is not None
    assert risk_note_for("advisor_next_dollar") is not None  # advisor_* family prefix
    assert risk_note_for("company_description") is None


def test_ranking_orders_by_headroom(inv_db: Path) -> None:
    rows = build_workload_inventory(inv_db)
    # eval_judge (META) is excluded; the three remaining rank alpha > zzz > web_p.
    assert [r.purpose for r in rows] == ["alpha", _ZZZ, "web_p"]


def test_headroom_matches_ladder_math(inv_db: Path) -> None:
    alpha = _by_purpose(build_workload_inventory(inv_db))["alpha"]
    inc = model_cost("claude-opus-4-8")
    cand = model_cost("gemini-3-flash-preview")
    assert inc is not None and cand is not None
    expected = 100.0 * (1.0 - cand.blended_usd_per_mtok / inc.blended_usd_per_mtok)
    assert alpha.cheapest_candidate == "gemini-3-flash-preview"
    assert alpha.headroom_usd_30d == pytest.approx(expected)


def test_eval_scope_rows_excluded_from_cost(inv_db: Path) -> None:
    alpha = _by_purpose(build_workload_inventory(inv_db))["alpha"]
    # The $999 scope='model_eval' row must not inflate alpha's production cost.
    assert alpha.cost_usd_30d == pytest.approx(100.0)
    assert alpha.calls_30d == 2
    assert alpha.distinct_prompts_30d == 2


def test_meta_purpose_excluded(inv_db: Path) -> None:
    assert "eval_judge" not in _by_purpose(build_workload_inventory(inv_db))


def test_incumbent_resolution_precedence(inv_db: Path) -> None:
    rows = _by_purpose(build_workload_inventory(inv_db))
    assert rows["alpha"].incumbent_model == "claude-opus-4-8"  # active override wins
    assert rows[_ZZZ].incumbent_model == DEFAULT_MODEL  # no pin/override → default


def test_web_scoped_is_zero_headroom(inv_db: Path) -> None:
    web = _by_purpose(build_workload_inventory(inv_db))["web_p"]
    assert web.web_scoped is True
    assert web.headroom_usd_30d == 0.0


def test_last_verdicts_excludes_candidate_errored(inv_db: Path) -> None:
    alpha = _by_purpose(build_workload_inventory(inv_db))["alpha"]
    assert alpha.last_verdicts == ("SWITCH_DOWN",)
    assert "CANDIDATE_ERRORED" not in alpha.last_verdicts


def test_budget_capped_from_on_exceed(inv_db: Path) -> None:
    rows = _by_purpose(build_workload_inventory(inv_db))
    assert rows["alpha"].budget_capped is True  # on_exceed='block'
    assert rows[_ZZZ].budget_capped is False  # on_exceed='warn'
    assert rows["web_p"].budget_capped is False  # no row → __default__ ('warn')


def test_render_text_smoke(inv_db: Path) -> None:
    text = render_inventory_text(build_workload_inventory(inv_db))
    assert "WORKLOAD INVENTORY" in text
    assert "alpha" in text
    assert "RISK" not in text or "RISKY purposes" in text  # RISK flag only with an appendix


def test_missing_db_returns_empty(tmp_path: Path) -> None:
    assert build_workload_inventory(tmp_path / "nope.db") == []
    # A DB that exists but has no llm_calls table also degrades to [].
    empty = tmp_path / "empty.db"
    sqlite3.connect(str(empty)).close()
    assert build_workload_inventory(empty) == []


def test_render_empty_is_graceful() -> None:
    assert "No production LLM purposes" in render_inventory_text([])
