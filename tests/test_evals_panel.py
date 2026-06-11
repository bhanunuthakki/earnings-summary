"""Evals dashboard panel (src/pipeline/evals_panel.py, llm_evals_plan §2.6):
latest-run selection + llm_calls cost join, failed-case drawers, the
prompt-version A/B strip, the §5.3 call-health rollup, empty-DB degradation,
and the run-bar ⇄ runner purpose-list lockstep.

Pure reads — no LLM, no jobs; the action route is exercised via the Flask app
in test_comments_server_actions-style fashion (route validation only).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pipeline.evals_panel import (
    RUNNABLE_PURPOSES,
    load_call_health,
    load_failed_cases,
    load_latest_runs,
    render_evals_panel,
)

_DDL = """
CREATE TABLE eval_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL, purpose TEXT NOT NULL, mode TEXT NOT NULL,
    prompt_version TEXT NOT NULL, model TEXT NOT NULL, judge_model TEXT,
    golden_set_sha TEXT, n_cases INTEGER NOT NULL DEFAULT 0,
    n_pass INTEGER NOT NULL DEFAULT 0, avg_score REAL,
    started_at TEXT NOT NULL, finished_at TEXT, git_sha TEXT, notes TEXT
);
CREATE TABLE eval_case_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    eval_run_id INTEGER NOT NULL, case_id TEXT NOT NULL, question TEXT NOT NULL,
    expected_json TEXT, actual_json TEXT, passed INTEGER NOT NULL,
    score REAL NOT NULL, failure_stage TEXT, judge_verdict TEXT,
    judge_rationale TEXT, prompt_text TEXT, response_text TEXT,
    latency_ms INTEGER, created_at TEXT NOT NULL
);
CREATE TABLE llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    called_at TEXT NOT NULL, purpose TEXT, ticker TEXT, scope TEXT,
    model TEXT, prompt_sha256 TEXT, response_sha256 TEXT,
    prompt_chars INTEGER, response_chars INTEGER, input_tokens INTEGER,
    cache_creation_input_tokens INTEGER, cache_read_input_tokens INTEGER,
    output_tokens INTEGER, elapsed_ms INTEGER, cost_estimate_usd REAL,
    cache_hit INTEGER NOT NULL DEFAULT 0, fallback_used TEXT,
    artifact_id INTEGER, error TEXT, run_id TEXT
);
CREATE TABLE prompt_calibration_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    purpose TEXT NOT NULL, prompt_version TEXT NOT NULL, ticker TEXT,
    score REAL NOT NULL, reason TEXT,
    scored_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    scored_by TEXT, artifact_id INTEGER
);
"""


def _seed(db: Path) -> None:
    conn = sqlite3.connect(db)
    conn.executescript(_DDL)
    now = datetime.now(UTC).replace(tzinfo=None)
    # Two runs for viewspec_compile — only the newer (id 2) may surface.
    conn.execute(
        "INSERT INTO eval_runs (run_id, purpose, mode, prompt_version, model, judge_model,"
        " n_cases, n_pass, avg_score, started_at, git_sha)"
        " VALUES ('rid_old', 'viewspec_compile', 'live', 'v1', 'haiku', 'haiku',"
        " 16, 13, 0.8125, '2026-06-10T01:00:00', 'aaa111')"
    )
    conn.execute(
        "INSERT INTO eval_runs (run_id, purpose, mode, prompt_version, model, judge_model,"
        " n_cases, n_pass, avg_score, started_at, git_sha)"
        " VALUES ('rid_new', 'viewspec_compile', 'live', 'v2', 'haiku', 'haiku',"
        " 16, 16, 1.0, '2026-06-11T01:00:00', 'bbb222')"
    )
    conn.execute(
        "INSERT INTO eval_runs (run_id, purpose, mode, prompt_version, model, judge_model,"
        " n_cases, n_pass, avg_score, started_at)"
        " VALUES ('rid_bear', 'bear_case', 'audit', 'v1', 'sonnet', 'haiku',"
        " 2, 1, 0.675, '2026-06-11T02:00:00')"
    )
    # Failed case on the bear audit (run id 3) + one on the OLD viewspec run
    # (id 1) that must NOT surface.
    conn.execute(
        "INSERT INTO eval_case_results (eval_run_id, case_id, question, expected_json,"
        " actual_json, passed, score, failure_stage, judge_rationale, created_at)"
        " VALUES (3, 'MELI', 'bear_case/MELI (data/bear_case/MELI.json)',"
        ' \'{"facets": ["x"]}\', \'{"facet_scores": {"x": 0.4}}\', 0, 0.4,'
        " 'below_threshold', 'generic risks, no math chain', '2026-06-11T02:01:00')"
    )
    conn.execute(
        "INSERT INTO eval_case_results (eval_run_id, case_id, question, passed, score,"
        " failure_stage, judge_rationale, created_at)"
        " VALUES (1, 'vs-002', 'stale failed case from the OLD run', 0, 0.0,"
        " 'mismatch', 'yoy-of-yoy stacking', '2026-06-10T01:01:00')"
    )
    # llm_calls: cost join for rid_new + health-window rows for two purposes.
    conn.execute(
        "INSERT INTO llm_calls (called_at, purpose, model, elapsed_ms, cost_estimate_usd,"
        " run_id) VALUES (?, 'viewspec_compile', 'haiku', 900, 0.0021, 'rid_new')",
        (now.isoformat(),),
    )
    conn.execute(
        "INSERT INTO llm_calls (called_at, purpose, model, elapsed_ms, cost_estimate_usd,"
        " run_id) VALUES (?, 'eval_judge', 'haiku', 700, 0.0009, 'rid_new')",
        (now.isoformat(),),
    )
    for i in range(8):
        conn.execute(
            "INSERT INTO llm_calls (called_at, purpose, model, elapsed_ms,"
            " cost_estimate_usd, error, fallback_used) VALUES (?, 'bear_case', 'sonnet',"
            " 2000, 0.05, ?, ?)",
            (
                now.isoformat(),
                "TimeoutExpired" if i < 2 else None,
                "gemini" if i == 7 else None,
            ),
        )
    # Outside the 30d window — must not count.
    conn.execute(
        "INSERT INTO llm_calls (called_at, purpose, model, elapsed_ms, error)"
        " VALUES (?, 'bear_case', 'sonnet', 1, 'ancient')",
        ((now - timedelta(days=90)).isoformat(),),
    )
    # Version strip rows.
    conn.execute(
        "INSERT INTO prompt_calibration_scores (purpose, prompt_version, score, scored_by)"
        " VALUES ('viewspec_compile', 'v1', 0.8125, 'auto:eval_harness')"
    )
    conn.execute(
        "INSERT INTO prompt_calibration_scores (purpose, prompt_version, score, scored_by)"
        " VALUES ('viewspec_compile', 'v2', 1.0, 'auto:eval_harness')"
    )
    conn.commit()
    conn.close()


def _conn(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def test_latest_runs_pick_newest_per_purpose_with_cost_join(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    _seed(db)
    conn = _conn(db)
    try:
        runs = load_latest_runs(conn)
    finally:
        conn.close()
    by_purpose = {r.purpose: r for r in runs}
    assert set(by_purpose) == {"viewspec_compile", "bear_case"}
    vs = by_purpose["viewspec_compile"]
    assert vs.run_id == "rid_new" and vs.prompt_version == "v2"
    assert vs.n_pass == 16
    assert vs.cost_usd == 0.003  # both rid_new rows (compile + judge)
    assert vs.call_count == 2
    assert by_purpose["bear_case"].mode == "audit"


def test_failed_cases_only_from_latest_runs(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    _seed(db)
    conn = _conn(db)
    try:
        runs = load_latest_runs(conn)
        failed = load_failed_cases(conn, runs)
    finally:
        conn.close()
    assert set(failed) == {"bear_case"}  # the old viewspec run's failure is stale
    case = failed["bear_case"][0]
    assert case.case_id == "MELI"
    assert case.judge_rationale is not None and "generic risks" in case.judge_rationale


def test_call_health_rates_and_window(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    _seed(db)
    conn = _conn(db)
    try:
        health = {h.purpose: h for h in load_call_health(conn)}
    finally:
        conn.close()
    bear = health["bear_case"]
    assert bear.calls == 8  # the 90-day-old row is outside the window
    assert bear.errors == 2 and bear.error_rate == 0.25
    assert bear.fallbacks == 1 and bear.fallback_rate == 0.125
    assert bear.cost_usd == 8 * 0.05
    assert health["viewspec_compile"].error_rate == 0.0
    # Worst error rate sorts first.
    conn = _conn(db)
    try:
        ordered = load_call_health(conn)
    finally:
        conn.close()
    assert ordered[0].purpose == "bear_case"


def test_render_full_panel(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    _seed(db)
    html = render_evals_panel(db)
    # Run bar: one button per runnable purpose, wired to /actions/run-eval.
    for p in RUNNABLE_PURPOSES:
        assert f'data-purpose="{p}"' in html
    assert "/actions/run-eval" in html
    # Latest runs: v2 surfaced with pass-rate + cost; audit mode pill.
    assert "16/16" in html
    assert "$0.0030" in html
    assert "ev-mode-audit" in html
    # Failed-case drawer carries the judge's evidence.
    assert "generic risks, no math chain" in html
    assert "facet_scores" in html
    # Version strip shows both version chips; health table shows the rates.
    assert html.count('class="ev-vchip"') == 2
    assert "25%" in html  # bear_case error rate
    assert "12%" in html  # bear_case fallback rate (12.5% under banker's rounding)


def test_render_empty_db_degrades(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()
    html = render_evals_panel(db)
    assert "No eval runs recorded yet" in html
    assert "/actions/run-eval" in html  # the run bar still works
    missing = render_evals_panel(tmp_path / "nope.db")
    assert "No eval runs recorded yet" in missing


def test_runnable_purposes_match_runner_cli() -> None:
    """The run bar and execution/run_llm_evals.py must offer the same
    purposes — a purpose added to one without the other is a wiring bug."""
    import importlib.util
    import sys

    src = Path(__file__).resolve().parents[1] / "execution" / "run_llm_evals.py"
    spec = importlib.util.spec_from_file_location("run_llm_evals_panel_check", src)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_llm_evals_panel_check"] = mod
    spec.loader.exec_module(mod)
    assert set(RUNNABLE_PURPOSES) == set(mod.PURPOSES)
