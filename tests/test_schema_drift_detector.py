"""The loud half of the Alembic guard.

``require_current_for_write`` already refuses a drifted database on every
guarded writer connection.  This file covers the callers that CANNOT act on
that refusal: ``llm_call_ledger`` swallows it by design, so on 2026-08-02 a
one-revision lag ate seven cost rows while Task Scheduler recorded success and
the cron-health panel stayed green.  What is pinned here is the detector that
makes the same drift impossible to miss — a job that refuses to start, a
durable counter for rows that were lost anyway, and the two panel banners.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from llm_call_ledger import LlmCallRecord, record_call
from runtime.job_runtime import SCHEMA_DRIFT_EXIT_CODE, run_job
from schema_compat import (
    DRIFT_CHECKOUT_BEHIND_DB,
    DRIFT_DB_BEHIND_CODE,
    describe_drift,
    expected_head,
)
from telemetry_health import (
    DROPPED_LLM_LEDGER_WRITE,
    dropped_writes_since,
    record_dropped_write,
)


def _checkout(root: Path, chain: list[str]) -> Path:
    """Write a linear ``alembic/versions`` chain; the last entry is the head."""
    versions = root / "alembic" / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    previous = "None"
    for revision in chain:
        (versions / f"{revision}.py").write_text(
            f'revision = "{revision}"\ndown_revision = {previous}\n',
            encoding="utf-8",
        )
        previous = f'"{revision}"'
    return root


def _versioned_db(path: Path, revision: str | None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        if revision is not None:
            conn.execute("CREATE TABLE alembic_version (version_num TEXT NOT NULL)")
            conn.execute("INSERT INTO alembic_version VALUES (?)", (revision,))
        else:
            conn.execute("CREATE TABLE marker (id INTEGER)")
        conn.commit()
    finally:
        conn.close()
    return path


# --------------------------------------------------------------------------
# describe_drift: the verdict itself
# --------------------------------------------------------------------------


def test_aligned_database_is_cleared_to_proceed(tmp_path: Path) -> None:
    root = _checkout(tmp_path / "repo", ["0270_a", "0271_b"])
    db = _versioned_db(tmp_path / "data" / "portfolio.db", "0271_b")
    assert describe_drift(db, project_root=root) is None


def test_database_one_revision_behind_is_reported_with_the_upgrade_fix(tmp_path: Path) -> None:
    """The exact 2026-08-02 shape: code merged 0271, the database sat at 0270."""
    root = _checkout(tmp_path / "repo", ["0270_a", "0271_b"])
    db = _versioned_db(tmp_path / "data" / "portfolio.db", "0270_a")

    drift = describe_drift(db, project_root=root)

    assert drift is not None
    assert drift.reason == DRIFT_DB_BEHIND_CODE
    assert drift.db_revisions == ("0270_a",)
    assert drift.expected_revision == "0271_b"
    assert drift.fix_command == "alembic upgrade head"


def test_revision_unknown_to_the_checkout_blames_the_checkout_not_the_database(
    tmp_path: Path,
) -> None:
    """``alembic upgrade head`` is the WRONG fix when the checkout is the stale
    one — it would try to apply migrations that no longer lead anywhere. The
    detector has to say which side moved."""
    root = _checkout(tmp_path / "repo", ["0270_a"])
    db = _versioned_db(tmp_path / "data" / "portfolio.db", "0299_from_the_future")

    drift = describe_drift(db, project_root=root)

    assert drift is not None
    assert drift.reason == DRIFT_CHECKOUT_BEHIND_DB
    assert "upgrade" not in drift.fix_command
    assert "git pull" in drift.fix_command


def test_unversioned_and_absent_databases_produce_no_verdict(tmp_path: Path) -> None:
    root = _checkout(tmp_path / "repo", ["0270_a"])
    unversioned = _versioned_db(tmp_path / "data" / "fixture.db", None)
    assert describe_drift(unversioned, project_root=root) is None
    assert describe_drift(tmp_path / "data" / "nope.db", project_root=root) is None


def test_forked_checkout_is_drift_rather_than_an_exception(tmp_path: Path) -> None:
    """``expected_head`` raises on two heads. A preflight caller runs before any
    handler that would catch it, so the fork has to arrive as a verdict."""
    root = tmp_path / "repo"
    versions = root / "alembic" / "versions"
    versions.mkdir(parents=True)
    for name in ("a", "b"):
        (versions / f"{name}.py").write_text(
            f'revision = "{name}"\ndown_revision = None\n', encoding="utf-8"
        )
    db = _versioned_db(tmp_path / "data" / "portfolio.db", "a")

    drift = describe_drift(db, project_root=root)

    assert drift is not None
    assert drift.expected_revision is None
    assert "heads" in drift.detail


# --------------------------------------------------------------------------
# run_job: the scheduler-visible failure
# --------------------------------------------------------------------------


def _drifted_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    root = _checkout(tmp_path / "repo", ["0270_a", "0271_b"])
    db = _versioned_db(tmp_path / "db" / "portfolio.db", "0270_a")
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(db))
    return root, db


def _health_records(root: Path, job: str) -> list[dict[str, object]]:
    directory = root / ".tmp" / "job_health" / job
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(directory.glob("*.json"))]


def test_drifted_database_blocks_the_job_before_any_work_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _db = _drifted_repo(tmp_path, monkeypatch)
    witness = tmp_path / "ran.txt"

    code = run_job(
        repo_root=root,
        job_name="onboard-pending",
        write_sets=["portfolio-db"],
        command=[sys.executable, "-c", f"open({str(witness)!r}, 'w').write('ran')"],
    )

    assert code == SCHEMA_DRIFT_EXIT_CODE
    assert not witness.exists(), "the job body must not run against a drifted database"
    records = _health_records(root, "onboard-pending")
    assert [r["status"] for r in records] == ["blocked_schema_drift"]
    assert records[0]["exit_code"] == SCHEMA_DRIFT_EXIT_CODE
    assert "0270_a" in str(records[0]["detail"])


def test_blocked_exit_code_is_distinct_from_failure_and_lock_contention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task Scheduler shows only 'Last Result'. 78 has to mean schema drift and
    nothing else, or the whole detector collapses back into 'something broke'."""
    assert SCHEMA_DRIFT_EXIT_CODE not in {0, 1, 75}
    root, _db = _drifted_repo(tmp_path, monkeypatch)
    assert (
        run_job(
            repo_root=root,
            job_name="refresh-cache",
            write_sets=["portfolio-db"],
            command=[sys.executable, "-c", "raise SystemExit(1)"],
        )
        == SCHEMA_DRIFT_EXIT_CODE
    )


def test_backup_and_restore_still_run_while_drifted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A drifted database is exactly when a snapshot and a proven restore path
    matter most; blocking them would turn a schema lag into a data risk."""
    root, _db = _drifted_repo(tmp_path, monkeypatch)
    for job in ("backup_db", "restore-drill"):
        assert (
            run_job(
                repo_root=root,
                job_name=job,
                write_sets=["portfolio-db"],
                command=[sys.executable, "-c", "print('ok')"],
            )
            == 0
        )


def test_allow_schema_drift_is_an_explicit_escape_hatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _db = _drifted_repo(tmp_path, monkeypatch)
    assert (
        run_job(
            repo_root=root,
            job_name="onboard-pending",
            write_sets=["portfolio-db"],
            command=[sys.executable, "-c", "print('ok')"],
            allow_schema_drift=True,
        )
        == 0
    )


def test_aligned_database_does_not_block_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _checkout(tmp_path / "repo", ["0270_a", "0271_b"])
    db = _versioned_db(tmp_path / "db" / "portfolio.db", "0271_b")
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(db))

    code = run_job(
        repo_root=root,
        job_name="onboard-pending",
        write_sets=["portfolio-db"],
        command=[sys.executable, "-c", "print('ok')"],
    )

    assert code == 0
    assert [r["status"] for r in _health_records(root, "onboard-pending")] == ["ok"]


# --------------------------------------------------------------------------
# telemetry_health: rows that were lost anyway
# --------------------------------------------------------------------------


def test_dropped_writes_are_counted_and_windowed(tmp_path: Path) -> None:
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    db.touch()
    now = datetime.now(UTC)

    for index in range(3):
        record_dropped_write(
            DROPPED_LLM_LEDGER_WRITE,
            db_path=db,
            error=f"SchemaRevisionMismatch: attempt {index}",
            purpose="material_news_classification",
            ticker="NVO",
        )

    summary = dropped_writes_since(
        DROPPED_LLM_LEDGER_WRITE, db_path=db, since=now - timedelta(days=7)
    )
    assert summary is not None
    assert summary.count == 3
    assert "attempt 2" in summary.last_error

    future = dropped_writes_since(
        DROPPED_LLM_LEDGER_WRITE, db_path=db, since=now + timedelta(hours=1)
    )
    assert future is None, "events outside the window must not be reported"


def test_clean_history_reports_nothing(tmp_path: Path) -> None:
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    db.touch()
    assert (
        dropped_writes_since(
            DROPPED_LLM_LEDGER_WRITE, db_path=db, since=datetime.now(UTC) - timedelta(days=7)
        )
        is None
    )


def test_counter_lands_beside_the_database_not_inside_a_checkout(tmp_path: Path) -> None:
    """The dashboard and the cron fleet run from two different checkouts against
    one portfolio.db. A counter under ``<checkout>/.tmp`` would be written by
    one and read by neither."""
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    db.touch()
    record_dropped_write(DROPPED_LLM_LEDGER_WRITE, db_path=db, error="boom")
    assert list((db.parent / ".health").glob("dropped_*.jsonl"))


def test_ledger_write_refused_by_the_guard_leaves_a_counter(tmp_path: Path) -> None:
    """The actual 2026-08-02 gap, end to end: ``record_call`` still returns None
    without raising, but the loss is now countable instead of log-only."""
    db = _versioned_db(tmp_path / "data" / "portfolio.db", "0000_not_this_checkouts_head")
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE llm_calls (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()
    assert expected_head() != "0000_not_this_checkouts_head"

    result = record_call(
        LlmCallRecord(
            called_at=datetime.now(UTC).replace(tzinfo=None),
            model="claude-opus-5",
            prompt_sha256="0" * 64,
            prompt_chars=10,
            elapsed_ms=5,
            purpose="material_news_classification",
        ),
        db_path=db,
    )

    assert result is None, "the ledger must stay best-effort for the LLM call path"
    summary = dropped_writes_since(
        DROPPED_LLM_LEDGER_WRITE, db_path=db, since=datetime.now(UTC) - timedelta(minutes=5)
    )
    assert summary is not None
    assert summary.count == 1
    assert "SchemaRevisionMismatch" in summary.last_error


# --------------------------------------------------------------------------
# The panel: what an operator actually sees
# --------------------------------------------------------------------------


def test_panel_leads_with_the_drift_alarm_even_with_no_run_history(tmp_path: Path) -> None:
    from pipeline.cron_health_panel import render_cron_health_live_body

    db = _versioned_db(tmp_path / "data" / "portfolio.db", "0000_not_this_checkouts_head")

    body = render_cron_health_live_body(db)

    assert "k-well-bad" in body
    assert "Schema drift" in body
    assert "0000_not_this_checkouts_head" in body
    assert str(SCHEMA_DRIFT_EXIT_CODE) in body


def test_panel_reports_lost_cost_rows(tmp_path: Path) -> None:
    from pipeline.cron_health_panel import render_cron_health_live_body

    db = _versioned_db(tmp_path / "data" / "portfolio.db", expected_head())
    record_dropped_write(
        DROPPED_LLM_LEDGER_WRITE,
        db_path=db,
        error="SchemaRevisionMismatch: db=0270, code=0271",
        purpose="decision_conditions_extract",
    )

    body = render_cron_health_live_body(db)

    assert "Schema drift" not in body
    assert "k-well-warn" in body
    assert "1 LLM cost row lost" in body
    assert "SchemaRevisionMismatch" in body


def test_panel_is_quiet_when_everything_is_aligned(tmp_path: Path) -> None:
    from pipeline.cron_health_panel import render_cron_health_live_body

    db = _versioned_db(tmp_path / "data" / "portfolio.db", expected_head())

    body = render_cron_health_live_body(db)

    assert "ch-alarm" not in body
