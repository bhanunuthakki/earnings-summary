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
    score REAL, failure_stage TEXT, judge_verdict TEXT,
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
    # Latest runs: v2 surfaced with pass-rate + cost; audit mode pill (kit).
    assert "16/16" in html
    assert "$0.0030" in html
    assert '<span class="k-pill">audit</span>' in html
    # Failed-case drawer carries the judge's evidence, rendered through the kit.
    assert "k-prov-drawer" in html and "k-prov-case" in html
    assert "generic risks, no math chain" in html
    assert "facet_scores" in html
    # Version strip shows both version chips; health table shows the rates.
    assert html.count('class="k-chip k-chip-mono ev-vchip"') == 2
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


def test_failed_drawer_renders_through_prov_kit_and_escapes(tmp_path: Path) -> None:
    """The failed-case drawer renders through the shared provenance kit
    (prov_drawer/prov_case; design_language §10), not the panel's old hand-rolled
    <details>/CSS — and still escapes the untrusted case id / question / rationale
    / expected / actual text (model + golden-set content, never markup)."""
    db = tmp_path / "p.db"
    conn = sqlite3.connect(db)
    conn.executescript(_DDL)
    conn.execute(
        "INSERT INTO eval_runs (run_id, purpose, mode, prompt_version, model, judge_model,"
        " n_cases, n_pass, avg_score, started_at)"
        " VALUES ('rid_x', 'bear_case', 'audit', 'v1', 'sonnet', 'haiku',"
        " 1, 0, 0.1, '2026-06-11T02:00:00')"
    )
    conn.execute(
        "INSERT INTO eval_case_results (eval_run_id, case_id, question, expected_json,"
        " actual_json, passed, score, failure_stage, judge_rationale, created_at)"
        " VALUES (1, '<b>CID</b>', '<i>q</i>', '<script>x</script>',"
        " '<img src=x onerror=alert(1)>', 0, 0.1, 'mismatch',"
        " '<u>why</u>', '2026-06-11T02:01:00')"
    )
    conn.commit()
    conn.close()
    html = render_evals_panel(db)
    # Through the kit, not the retired per-panel drawer/pill classes.
    assert "k-prov-drawer" in html and "k-prov-case" in html
    assert "ev-case" not in html and "ev-score-bad" not in html
    assert "k-pill-bad" in html  # the failing score rides the kit's bad pill
    # Untrusted text is escaped — no live markup reaches the page.
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;x&lt;/script&gt;" in html  # expected JSON, escaped
    assert "<img src=x onerror=alert(1)>" not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html  # actual JSON, escaped
    assert "&lt;i&gt;q&lt;/i&gt;" in html  # the question (drawer meta), escaped
    assert "&lt;u&gt;why&lt;/u&gt;" in html  # the judge rationale, escaped
    assert "&lt;b&gt;CID&lt;/b&gt;" in html  # the case id (drawer title), escaped


def test_failed_drawer_survives_null_score_infra_case(tmp_path: Path) -> None:
    """judge_infra cases (July-2026 transport hardening) persist score=NULL —
    'not measured', distinct from a real 0.0. The drawer previously cast
    float(r['score']) unconditionally, so the FIRST infra case crashed the
    whole System→Evals panel render (adversarial-review finding, 2026-07-25).
    prov_case already omits the score pill for None; the panel must pass it
    through instead of crashing or faking a 0.00."""
    db = tmp_path / "p.db"
    conn = sqlite3.connect(db)
    conn.executescript(_DDL)
    conn.execute(
        "INSERT INTO eval_runs (run_id, purpose, mode, prompt_version, model, judge_model,"
        " n_cases, n_pass, avg_score, started_at)"
        " VALUES ('rid_i', 'bear_case', 'audit', 'v2', 'sonnet', 'haiku',"
        " 1, 0, NULL, '2026-07-25T02:00:00')"
    )
    conn.execute(
        "INSERT INTO eval_case_results (eval_run_id, case_id, question, expected_json,"
        " actual_json, passed, score, failure_stage, judge_rationale, created_at)"
        " VALUES (1, 'NU', 'q', NULL, NULL, 0, NULL, 'judge_infra',"
        " 'judge call failed: LLMQuotaExhausted: window exhausted', '2026-07-25T02:01:00')"
    )
    conn.commit()
    conn.close()
    html = render_evals_panel(db)  # must not raise
    assert "judge_infra" in html  # the stage is visible in the drawer meta
    assert "judge call failed" in html
    assert "0.00" not in html  # never fabricate a measured-zero pill


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
