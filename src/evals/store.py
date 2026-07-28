"""eval_runs / eval_case_results writes (alembic 0083).

Unlike the production telemetry writers (llm_call_ledger, calibration), this
store is LOUD: an eval run you can't persist should fail the harness with a
pointer at the migration, not silently drop scores. ``--no-persist`` on the
CLI is the deliberate way to run without a migrated DB.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from evals.harness import CaseResult, EvalRunSummary
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

_RUN_INSERT = """
INSERT INTO eval_runs(
    run_id, purpose, mode, prompt_version, model, judge_model,
    golden_set_sha, n_cases, n_pass, avg_score,
    started_at, finished_at, git_sha, notes
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""

_CASE_INSERT = """
INSERT INTO eval_case_results(
    eval_run_id, case_id, question, expected_json, actual_json,
    passed, score, failure_stage, judge_verdict, judge_rationale,
    prompt_text, response_text, latency_ms, created_at
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


def write_run(summary: EvalRunSummary, *, db_path: Path) -> int:
    """Insert the run row + every case row in one transaction. Returns the
    eval_runs id. Raises RuntimeError with a migration hint when the tables
    are missing; lets other sqlite errors propagate (loud by design)."""
    if not Path(db_path).exists():
        raise RuntimeError(f"eval store: DB not found at {db_path}")
    conn = connect_sqlite(
        db_path,
        role=SQLiteConnectionRole.WRITER,
        # The eval store is intentionally compatible with its introducing
        # 0083 schema so historical/replay fixtures remain writable. Table
        # presence below is the narrow contract this module enforces.
        schema_preflight=False,
    )
    try:
        conn.execute("PRAGMA busy_timeout = 10000")
        try:
            cur = conn.execute(
                _RUN_INSERT,
                (
                    summary.run_id,
                    summary.purpose,
                    summary.mode,
                    summary.prompt_version,
                    summary.model,
                    summary.judge_model,
                    summary.golden_set_sha,
                    summary.n_cases,
                    summary.n_pass,
                    summary.avg_score,
                    summary.started_at.isoformat(),
                    summary.finished_at.isoformat() if summary.finished_at else None,
                    summary.git_sha,
                    summary.notes,
                ),
            )
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                raise RuntimeError(
                    "eval store: eval_runs table missing — run `alembic upgrade head` "
                    "(migration 0083_eval_runs), or pass --no-persist."
                ) from exc
            raise
        run_db_id = int(cur.lastrowid or 0)
        for case in summary.cases:
            _insert_case(conn, run_db_id, case, summary)
        conn.commit()
        return run_db_id
    finally:
        conn.close()


def _insert_case(
    conn: sqlite3.Connection,
    run_db_id: int,
    case: CaseResult,
    summary: EvalRunSummary,
) -> None:
    conn.execute(
        _CASE_INSERT,
        (
            run_db_id,
            case.case_id,
            case.question,
            case.expected_json,
            case.actual_json,
            1 if case.passed else 0,
            # NULL, not 0.0, for judge-infra cases: "not measured" must stay
            # distinguishable from a real measured zero in the DB.
            float(case.score) if case.score is not None else None,
            case.failure_stage,
            case.judge_verdict,
            case.judge_rationale,
            case.prompt_text,
            case.response_text,
            case.latency_ms,
            (summary.finished_at or summary.started_at).isoformat(),
        ),
    )
