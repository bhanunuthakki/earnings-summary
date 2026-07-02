"""Tests for the PR6 Optimizer-panel extensions (steering health, pending
nominations, prompt experiments) — src/pipeline/model_eval_panel.py."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pipeline.model_eval_panel import (
    ExperimentRow,
    NominationRow,
    SteeringHealth,
    compose_model_eval_page,
    load_experiments,
    load_pending_nominations,
    load_steering_health,
)

_NOW = datetime.now(UTC).replace(tzinfo=None)


def _db(tmp_path: Path) -> Path:
    db_path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE llm_calls (
            id INTEGER PRIMARY KEY, called_at TEXT, purpose TEXT, ticker TEXT,
            scope TEXT, model TEXT DEFAULT 'm', prompt_sha256 TEXT DEFAULT 'x',
            prompt_chars INTEGER DEFAULT 0, cost_estimate_usd REAL,
            input_tokens INTEGER, output_tokens INTEGER
        );
        CREATE TABLE model_eval_verdicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, purpose TEXT NOT NULL,
            candidate TEXT NOT NULL, incumbent TEXT NOT NULL, verdict TEXT NOT NULL,
            run_id TEXT NOT NULL, parity_rate REAL, judge_agreement REAL,
            n_cases INTEGER, n_parity INTEGER, summary_json TEXT, recorded_at TEXT NOT NULL
        );
        CREATE TABLE optimizer_nominations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, nomination_run_id TEXT NOT NULL,
            purpose TEXT NOT NULL, kind TEXT NOT NULL, priority INTEGER NOT NULL,
            headroom_usd_30d REAL, cost_usd_30d REAL, calls_30d INTEGER,
            incumbent_model TEXT NOT NULL, candidates_json TEXT NOT NULL DEFAULT '[]',
            rationale TEXT NOT NULL, risk_tier TEXT NOT NULL, suggested_min_n INTEGER,
            source TEXT NOT NULL, ladder_sha TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', expires_at TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE candidate_models (
            model_id TEXT PRIMARY KEY, family TEXT NOT NULL,
            input_usd_per_mtok REAL NOT NULL, output_usd_per_mtok REAL NOT NULL,
            promise REAL NOT NULL DEFAULT 0.5, source TEXT NOT NULL DEFAULT 'seed',
            status TEXT NOT NULL DEFAULT 'active', source_url TEXT, notes TEXT,
            research_run_id TEXT, first_seen_at TEXT NOT NULL, verified_at TEXT NOT NULL
        );
        CREATE TABLE prompt_experiments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, experiment_id TEXT NOT NULL UNIQUE,
            purpose TEXT NOT NULL, baseline_prompt_version TEXT NOT NULL,
            variant_label TEXT NOT NULL, hypothesis TEXT NOT NULL,
            edits_json TEXT NOT NULL, frozen_model TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'proposed', decision TEXT,
            created_at TEXT NOT NULL, decided_at TEXT, notes TEXT
        );
        CREATE TABLE prompt_ab_verdicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, experiment_id TEXT NOT NULL,
            purpose TEXT NOT NULL, run_id TEXT NOT NULL, n_cases INTEGER,
            variant_wins INTEGER, baseline_wins INTEGER, ties INTEGER,
            win_rate REAL, judge_agreement REAL, recommendation TEXT NOT NULL,
            reason TEXT NOT NULL, summary_json TEXT, recorded_at TEXT NOT NULL
        );
        CREATE TABLE prompt_pin_overrides (
            id INTEGER PRIMARY KEY AUTOINCREMENT, purpose TEXT NOT NULL,
            edits_json TEXT NOT NULL, experiment_id TEXT NOT NULL,
            set_by TEXT NOT NULL, set_at TEXT NOT NULL, reason_json TEXT,
            active INTEGER NOT NULL DEFAULT 1
        );
        """
    )
    conn.commit()
    conn.close()
    return db_path


def _conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def test_load_pending_nominations(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    conn = _conn(db_path)
    conn.execute(
        "INSERT INTO optimizer_nominations (nomination_run_id, purpose, kind, priority,"
        " incumbent_model, candidates_json, rationale, risk_tier, source, ladder_sha,"
        " status, created_at, updated_at) VALUES ('r', 'bear_case', 'model_downgrade', 1,"
        " 'claude-sonnet-4-6', '[\"claude-haiku-4-5-20251001\"]', 'high headroom', 'risky',"
        " 'opus', 'sha', 'pending', ?, ?)",
        (_NOW.isoformat(), _NOW.isoformat()),
    )
    conn.execute(
        "INSERT INTO optimizer_nominations (nomination_run_id, purpose, kind, priority,"
        " incumbent_model, candidates_json, rationale, risk_tier, source, ladder_sha,"
        " status, created_at, updated_at) VALUES ('r', 'old', 'model_downgrade', 2,"
        " 'm', '[]', 'x', 'safe', 'opus', 'sha', 'swept', ?, ?)",
        (_NOW.isoformat(), _NOW.isoformat()),
    )
    conn.commit()
    noms = load_pending_nominations(conn)
    conn.close()
    assert [n.purpose for n in noms] == ["bear_case"]  # swept rows excluded
    assert noms[0].candidates == ["claude-haiku-4-5-20251001"]
    assert noms[0].risk_tier == "risky"


def test_steering_health_freshness_and_meta_cost(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    conn = _conn(db_path)
    stale = (_NOW - timedelta(days=60)).isoformat()
    fresh = (_NOW - timedelta(days=1)).isoformat()
    conn.execute(
        "INSERT INTO optimizer_nominations (nomination_run_id, purpose, kind, priority,"
        " incumbent_model, rationale, risk_tier, source, ladder_sha, status,"
        " created_at, updated_at) VALUES ('r', 'p', 'model_downgrade', 1, 'm', 'x',"
        " 'safe', 'opus', 'sha', 'expired', ?, ?)",
        (stale, stale),
    )
    conn.execute(
        "INSERT INTO model_eval_verdicts (purpose, candidate, incumbent, verdict,"
        " run_id, recorded_at) VALUES ('p', 'c', 'i', 'INSUFFICIENT_FRAME', 'r', ?)",
        (fresh,),
    )
    conn.execute(
        "INSERT INTO llm_calls (called_at, purpose, scope, cost_estimate_usd)"
        " VALUES (?, 'eval_judge', 'backend_judge', 2.5)",
        (fresh,),
    )
    conn.execute(
        "INSERT INTO llm_calls (called_at, purpose, scope, cost_estimate_usd)"
        " VALUES (?, 'bear_case', NULL, 50.0)",  # production — NOT meta cost
        (fresh,),
    )
    conn.execute(
        "INSERT INTO candidate_models (model_id, family, input_usd_per_mtok,"
        " output_usd_per_mtok, first_seen_at, verified_at)"
        " VALUES ('prov/m', 'openrouter', 0.1, 0.2, ?, ?)",
        (fresh, fresh),
    )
    conn.commit()
    health = load_steering_health(conn)
    conn.close()
    assert health.nomination_stale is True  # 60d > 45d
    assert health.sweep_stale is False  # 1d < 14d
    assert health.meta_cost_30d_usd == 2.5  # production line excluded
    assert health.meta_calls_30d == 1
    assert health.active_candidate_models == 1
    assert health.insufficient_frame_purposes == ["p"]


def test_load_experiments_with_override_flag(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    conn = _conn(db_path)
    conn.execute(
        "INSERT INTO prompt_experiments (experiment_id, purpose, baseline_prompt_version,"
        " variant_label, hypothesis, edits_json, frozen_model, status, created_at)"
        " VALUES ('exp1', 'bear_case', 'v2', 'exp-1', 'tighter format',"
        ' \'[{"find": "a", "replace": "b"}]\', \'claude-sonnet-4-6\', \'promoted\', ?)',
        (_NOW.isoformat(),),
    )
    conn.execute(
        "INSERT INTO prompt_ab_verdicts (experiment_id, purpose, run_id, n_cases,"
        " recommendation, reason, recorded_at) VALUES ('exp1', 'bear_case', 'r', 6,"
        " 'PROMOTE_VARIANT', 'won', ?)",
        (_NOW.isoformat(),),
    )
    conn.execute(
        "INSERT INTO prompt_pin_overrides (purpose, edits_json, experiment_id, set_by,"
        " set_at, active) VALUES ('bear_case', '[]', 'exp1', 'auto:prompt_ab_loop', ?, 1)",
        (_NOW.isoformat(),),
    )
    conn.commit()
    rows = load_experiments(conn)
    conn.close()
    assert len(rows) == 1
    e = rows[0]
    assert e.status == "promoted"
    assert e.latest_recommendation == "PROMOTE_VARIANT"
    assert e.n_edits == 1
    assert e.override_active is True


def test_compose_page_renders_new_sections() -> None:
    health = SteeringHealth(
        newest_nomination_at=None,
        newest_verdict_at=None,
        nomination_stale=True,
        sweep_stale=True,
        meta_cost_30d_usd=3.2,
        meta_calls_30d=7,
        active_candidate_models=2,
        insufficient_frame_purposes=["bear_case"],
    )
    nom = NominationRow(
        purpose="bear_case",
        kind="model_downgrade",
        priority=1,
        risk_tier="risky",
        source="opus",
        candidates=["claude-haiku-4-5-20251001"],
        rationale="high headroom",
    )
    exp = ExperimentRow(
        experiment_id="deadbeef99",
        purpose="bear_case",
        status="promoted",
        hypothesis="tighter format",
        n_edits=2,
        latest_recommendation="PROMOTE_VARIANT",
        runs=2,
        override_active=True,
    )
    html = compose_model_eval_page(
        [], [], [], [], nominations=[nom], health=health, experiments=[exp]
    )
    assert "Optimizer steering" in html
    assert "no nomination run in 45d" in html
    assert "no sweep verdict in 14d" in html
    assert "thin frame" in html
    assert "high headroom" in html
    assert "Prompt experiments" in html
    assert "override live" in html
    # Graceful empties still compose.
    html_empty = compose_model_eval_page([], [], [], [])
    assert "Optimizer steering" in html_empty
