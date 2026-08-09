"""Tests for pipeline/cron_health_panel.py and execution/verify_daily_chain.py."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path: Path) -> sqlite3.Connection:
    """In-memory SQLite DB with a minimal ingestion_runs schema."""
    conn = sqlite3.connect(str(tmp_path / "test.db"))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE ingestion_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL UNIQUE,
            started_at DATETIME NOT NULL,
            ended_at DATETIME,
            directive TEXT NOT NULL,
            ticker_scope TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL,
            error_summary TEXT
        )
        """
    )
    conn.commit()
    return conn


def _insert_run(
    conn: sqlite3.Connection,
    directive: str,
    status: str,
    started_at: datetime,
    ended_at: datetime | None = None,
    error_summary: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO ingestion_runs "
        "(run_id, started_at, ended_at, directive, ticker_scope, status, error_summary) "
        "VALUES (?, ?, ?, ?, '[]', ?, ?)",
        (
            f"{directive}_{started_at.isoformat()}",
            started_at,
            ended_at,
            directive,
            status,
            error_summary,
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# cron_health_panel
# ---------------------------------------------------------------------------


def test_panel_empty_state(tmp_path: Path) -> None:
    """No ingestion_runs rows → empty-state message."""
    db_path = tmp_path / "empty.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE ingestion_runs "
        "(id INTEGER PRIMARY KEY, run_id TEXT, started_at DATETIME, ended_at DATETIME, "
        "directive TEXT, ticker_scope TEXT, status TEXT, error_summary TEXT)"
    )
    conn.commit()
    conn.close()

    from pipeline.cron_health_panel import render_cron_health_panel

    html = render_cron_health_panel(db_path)
    assert "Cron health" in html
    assert "No pipeline run rows yet" in html


def test_panel_shows_ok_run(tmp_path: Path) -> None:
    db_path = tmp_path / "ok.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE ingestion_runs "
        "(id INTEGER PRIMARY KEY, run_id TEXT, started_at DATETIME, ended_at DATETIME, "
        "directive TEXT, ticker_scope TEXT, status TEXT, error_summary TEXT)"
    )
    conn.commit()
    today = datetime.now().replace(hour=4, minute=0, second=0, microsecond=0)
    conn.execute(
        "INSERT INTO ingestion_runs VALUES (1,?,?,?,?,?,?,?)",
        (f"run_{today.isoformat()}", today, today, "run_morning_pipeline", "[]", "ok", None),
    )
    conn.commit()
    conn.close()

    from pipeline.cron_health_panel import render_cron_health_panel

    html = render_cron_health_panel(db_path)
    assert "Cron health" in html
    assert "Morning pipeline" in html
    assert "k-dot-ok" in html
    assert "OK" in html


def test_panel_shows_failed_run(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE ingestion_runs "
        "(id INTEGER PRIMARY KEY, run_id TEXT, started_at DATETIME, ended_at DATETIME, "
        "directive TEXT, ticker_scope TEXT, status TEXT, error_summary TEXT)"
    )
    today = datetime.now().replace(hour=4, minute=0, second=0, microsecond=0)
    conn.execute(
        "INSERT INTO ingestion_runs VALUES (1,?,?,?,?,?,?,?)",
        (f"run_{today.isoformat()}", today, today, "run_morning_pipeline", "[]", "failed", "boom"),
    )
    conn.commit()
    conn.close()

    from pipeline.cron_health_panel import render_cron_health_panel

    html = render_cron_health_panel(db_path)
    assert "k-dot-bad" in html
    assert "failed" in html


def test_panel_shows_abandoned_run_as_unhealthy(tmp_path: Path) -> None:
    db_path = tmp_path / "abandoned.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE ingestion_runs "
        "(id INTEGER PRIMARY KEY, run_id TEXT, started_at DATETIME, ended_at DATETIME, "
        "directive TEXT, ticker_scope TEXT, status TEXT, error_summary TEXT)"
    )
    today = datetime.now().replace(hour=4, minute=0, second=0, microsecond=0)
    conn.execute(
        "INSERT INTO ingestion_runs VALUES (1,?,?,?,?,?,?,?)",
        (
            f"run_{today.isoformat()}",
            today,
            today,
            "run_morning_pipeline",
            "[]",
            "abandoned",
            "stale attempt reaped",
        ),
    )
    conn.commit()
    conn.close()

    from pipeline.cron_health_panel import render_cron_health_panel

    html = render_cron_health_panel(db_path)
    assert 'k-dot-bad" title="abandoned"' in html
    assert 'class="ch-status-fail">abandoned<' in html


def test_panel_streak_count(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE ingestion_runs "
        "(id INTEGER PRIMARY KEY, run_id TEXT, started_at DATETIME, ended_at DATETIME, "
        "directive TEXT, ticker_scope TEXT, status TEXT, error_summary TEXT)"
    )
    today = datetime.now().replace(hour=4, minute=0, second=0, microsecond=0)
    for i in range(1, 4):
        d = today - timedelta(days=i)
        conn.execute(
            "INSERT INTO ingestion_runs VALUES (?,?,?,?,?,?,?,?)",
            (i, f"run_{d.isoformat()}", d, d, "run_morning_pipeline", "[]", "ok", None),
        )
    conn.commit()
    conn.close()

    from pipeline.cron_health_panel import render_cron_health_panel

    html = render_cron_health_panel(db_path)
    assert "3d" in html


def test_panel_backup_directive_shown(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE ingestion_runs "
        "(id INTEGER PRIMARY KEY, run_id TEXT, started_at DATETIME, ended_at DATETIME, "
        "directive TEXT, ticker_scope TEXT, status TEXT, error_summary TEXT)"
    )
    today = datetime.now().replace(hour=2, minute=45, second=0, microsecond=0)
    conn.execute(
        "INSERT INTO ingestion_runs VALUES (1,?,?,?,?,?,?,?)",
        (f"run_{today.isoformat()}", today, today, "backup_db", "[]", "ok", None),
    )
    conn.commit()
    conn.close()

    from pipeline.cron_health_panel import render_cron_health_panel

    html = render_cron_health_panel(db_path)
    assert "DB backup" in html


def test_operational_alarms_flag_stale_backup_large_wal_and_stale_eval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pipeline.cron_health_panel import operational_alarms

    db_path = tmp_path / "data" / "portfolio.db"
    db_path.parent.mkdir()
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE eval_runs (started_at TEXT, finished_at TEXT)")
    conn.execute("INSERT INTO eval_runs VALUES ('2026-07-01T00:00:00+00:00', NULL)")
    conn.commit()
    conn.close()
    wal = Path(f"{db_path}-wal")
    wal.write_bytes(b"oversized")
    monkeypatch.setenv("ES_WAL_WARN_BYTES", "1")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    monkeypatch.setenv("ES_DB_BACKUP_DIR", str(backup_dir))

    html = operational_alarms(db_path, now=datetime(2026, 8, 8, tzinfo=UTC))

    assert "Database backup is stale" in html
    assert "SQLite WAL is oversized" in html
    assert "LLM evaluations are stale" in html


def test_wal_probe_surfaces_io_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pipeline.operational_health import wal_size

    original_stat = Path.stat
    wal = Path(f"{tmp_path / 'portfolio.db'}-wal")

    def fail_for_wal(path: Path, *args: object, **kwargs: object) -> os.stat_result:
        if path == wal:
            raise PermissionError("WAL permission denied")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fail_for_wal)
    with pytest.raises(PermissionError, match="WAL permission denied"):
        wal_size(tmp_path / "portfolio.db")


def test_operational_alarms_are_quiet_for_fresh_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pipeline.cron_health_panel import operational_alarms

    now = datetime.now(UTC)
    db_path = tmp_path / "data" / "portfolio.db"
    db_path.parent.mkdir()
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE eval_runs (started_at TEXT, finished_at TEXT)")
    conn.execute("INSERT INTO eval_runs VALUES (?, ?)", (now.isoformat(), now.isoformat()))
    conn.commit()
    conn.close()
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    snapshot = backup_dir / "portfolio.db.20260808_000000_000000.gz.enc"
    snapshot.write_bytes(b"encrypted")
    os.utime(snapshot, (now.timestamp(), now.timestamp()))
    monkeypatch.setenv("ES_DB_BACKUP_DIR", str(backup_dir))

    html = operational_alarms(db_path, now=now)

    assert "Database backup is stale" not in html
    assert "SQLite WAL is oversized" not in html
    assert "LLM evaluations are stale" not in html
    assert "freshness is unavailable" not in html


def test_operational_alarms_require_current_gc_archive_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pipeline.cron_health_panel import operational_alarms

    now = datetime.now(UTC)
    db_path = tmp_path / "data" / "portfolio.db"
    archive = tmp_path / "data" / "archive" / "portfolio_gc_archive.db"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"rows")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE eval_runs (started_at TEXT, finished_at TEXT)")
    conn.execute("INSERT INTO eval_runs VALUES (?, ?)", (now.isoformat(), now.isoformat()))
    conn.commit()
    conn.close()
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    primary = backup_dir / "portfolio.db.20260808_000000_000000.gz.enc"
    primary.write_bytes(b"encrypted")
    os.utime(primary, (now.timestamp(), now.timestamp()))
    monkeypatch.setenv("ES_DB_BACKUP_DIR", str(backup_dir))

    html = operational_alarms(db_path, now=now)

    assert "GC archive is not backed up" in html


# ---------------------------------------------------------------------------
# verify_daily_chain
# ---------------------------------------------------------------------------


def test_check_missing_when_no_run_today(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE ingestion_runs "
        "(id INTEGER PRIMARY KEY, run_id TEXT, started_at DATETIME, ended_at DATETIME, "
        "directive TEXT, ticker_scope TEXT, status TEXT, error_summary TEXT)"
    )
    conn.commit()
    conn.close()

    from execution.verify_daily_chain import check

    result = check(db_path)
    assert result["verdict"] == "missing"
    assert result["runs_today"] == 0


def test_check_ok_when_successful_run_today(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE ingestion_runs "
        "(id INTEGER PRIMARY KEY, run_id TEXT, started_at DATETIME, ended_at DATETIME, "
        "directive TEXT, ticker_scope TEXT, status TEXT, error_summary TEXT)"
    )
    today = datetime.now().replace(hour=4, minute=10, second=0, microsecond=0)
    conn.execute(
        "INSERT INTO ingestion_runs VALUES (1,?,?,?,?,?,?,?)",
        ("rmp_today", today, today, "run_morning_pipeline", "[]", "ok", None),
    )
    conn.commit()
    conn.close()

    from execution.verify_daily_chain import check

    result = check(db_path)
    assert result["verdict"] == "ok"
    assert result["runs_today"] == 1
    assert result["latest_run_id"] == "rmp_today"  # type: ignore[literal-required]


def test_check_failed_when_latest_run_failed(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE ingestion_runs "
        "(id INTEGER PRIMARY KEY, run_id TEXT, started_at DATETIME, ended_at DATETIME, "
        "directive TEXT, ticker_scope TEXT, status TEXT, error_summary TEXT)"
    )
    today = datetime.now().replace(hour=4, minute=10, second=0, microsecond=0)
    conn.execute(
        "INSERT INTO ingestion_runs VALUES (1,?,?,?,?,?,?,?)",
        ("rmp_fail", today, today, "run_morning_pipeline", "[]", "failed", "stage_1 timed out"),
    )
    conn.commit()
    conn.close()

    from execution.verify_daily_chain import check

    result = check(db_path)
    assert result["verdict"] == "failed"
    assert result["error_summary"] == "stage_1 timed out"  # type: ignore[literal-required]


def test_check_db_error_on_bad_path(tmp_path: Path) -> None:
    from execution.verify_daily_chain import check

    result = check(tmp_path / "nonexistent.db")
    assert result["verdict"] == "db_error"


def test_main_exit_0_on_ok_run(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE ingestion_runs "
        "(id INTEGER PRIMARY KEY, run_id TEXT, started_at DATETIME, ended_at DATETIME, "
        "directive TEXT, ticker_scope TEXT, status TEXT, error_summary TEXT)"
    )
    today = datetime.now().replace(hour=4, minute=0, second=0, microsecond=0)
    conn.execute(
        "INSERT INTO ingestion_runs VALUES (1,?,?,?,?,?,?,?)",
        ("rmp_ok", today, today, "run_morning_pipeline", "[]", "ok", None),
    )
    conn.commit()
    conn.close()

    status_file = tmp_path / "status.json"
    from execution.verify_daily_chain import main

    rc = main(["--quiet", "--db-path", str(db_path), "--status-file", str(status_file)])
    assert rc == 0
    payload = json.loads(status_file.read_text())
    assert payload["verdict"] == "ok"


def test_main_exit_1_on_missing(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE ingestion_runs "
        "(id INTEGER PRIMARY KEY, run_id TEXT, started_at DATETIME, ended_at DATETIME, "
        "directive TEXT, ticker_scope TEXT, status TEXT, error_summary TEXT)"
    )
    conn.commit()
    conn.close()

    status_file = tmp_path / "status.json"
    from execution.verify_daily_chain import main

    rc = main(["--quiet", "--db-path", str(db_path), "--status-file", str(status_file)])
    assert rc == 1
    payload = json.loads(status_file.read_text())
    assert payload["verdict"] == "missing"


def test_main_exit_2_on_db_error(tmp_path: Path) -> None:
    from execution.verify_daily_chain import main

    status_file = tmp_path / "status.json"
    rc = main(
        ["--quiet", "--db-path", str(tmp_path / "nope.db"), "--status-file", str(status_file)]
    )
    assert rc == 2
