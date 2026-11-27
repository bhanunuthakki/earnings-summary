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

    from pipeline.cron_health_panel import render_cron_health_live_body

    html = render_cron_health_live_body(db_path)
    assert "No pipeline run rows yet" in html
    assert "data-refresh-ms" not in html
    assert "setInterval" not in html
    assert "Observed by server" in html


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

    from pipeline.cron_health_panel import render_cron_health_live_body

    html = render_cron_health_live_body(db_path)
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

    from pipeline.cron_health_panel import render_cron_health_live_body

    html = render_cron_health_live_body(db_path)
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

    from pipeline.cron_health_panel import render_cron_health_live_body

    html = render_cron_health_live_body(db_path)
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

    from pipeline.cron_health_panel import render_cron_health_live_body

    html = render_cron_health_live_body(db_path)
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

    from pipeline.cron_health_panel import render_cron_health_live_body

    html = render_cron_health_live_body(db_path)
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


def _write_backup_receipt(
    backup_dir: Path,
    *,
    stamp: str,
    source: Path,
    verified_at: datetime,
    source_size_bytes: int | None,
    ciphertext_size: int = 128,
    family: str = "portfolio-db",
) -> Path:
    snapshot = backup_dir / f"portfolio.db.{stamp}.gz.enc"
    receipt_file = snapshot.with_name(snapshot.name + ".receipt.json")
    payload: dict[str, object] = {
        "version": 1,
        "family": family,
        "source": str(source.resolve()),
        "snapshot_name": snapshot.name,
        "snapshot_sha256": "a" * 64,
        "ciphertext_sha256": "b" * 64,
        "size_bytes": ciphertext_size,
        "verified_at_utc": verified_at.isoformat(),
    }
    if source_size_bytes is not None:
        payload["source_size_bytes"] = source_size_bytes
    receipt_file.write_text(json.dumps(payload), encoding="utf-8")
    return receipt_file


def test_db_size_health_flags_absolute_warn_and_critical_thresholds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pipeline.cron_health_panel import operational_alarms
    from pipeline.operational_health import db_size_health

    now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    db_path = tmp_path / "data" / "portfolio.db"
    db_path.parent.mkdir()
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE eval_runs (started_at TEXT, finished_at TEXT)")
    conn.execute("INSERT INTO eval_runs VALUES (?, ?)", (now.isoformat(), now.isoformat()))
    conn.commit()
    conn.close()
    actual_size = db_path.stat().st_size

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    snapshot = backup_dir / "portfolio.db.20261126_024500_000000.gz.enc"
    snapshot.write_bytes(b"encrypted")
    os.utime(snapshot, (now.timestamp(), now.timestamp()))
    monkeypatch.setenv("ES_DB_BACKUP_DIR", str(backup_dir))

    monkeypatch.setenv("ES_DB_SIZE_WARN_BYTES", str(actual_size))
    monkeypatch.setenv("ES_DB_SIZE_CRIT_BYTES", str(actual_size * 2))
    warn_health = db_size_health(db_path, directory=backup_dir, now=now)
    assert warn_health.size_exceeded == "warn"
    assert warn_health.severity == "warn"
    warn_html = operational_alarms(db_path, now=now)
    assert "SQLite database size is elevated" in warn_html
    assert "SQLite database size is critical" not in warn_html

    monkeypatch.setenv("ES_DB_SIZE_CRIT_BYTES", str(actual_size))
    crit_health = db_size_health(db_path, directory=backup_dir, now=now)
    assert crit_health.size_exceeded == "critical"
    assert crit_health.severity == "critical"
    crit_html = operational_alarms(db_path, now=now)
    assert "SQLite database size is critical" in crit_html
    assert "k-well-bad" in crit_html


def test_db_size_health_flags_growth_jump_across_overnight_backup_and_stale_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pipeline.cron_health_panel import operational_alarms
    from pipeline.operational_health import db_size_health

    now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    db_path = tmp_path / "data" / "portfolio.db"
    db_path.parent.mkdir()
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE eval_runs (started_at TEXT, finished_at TEXT)")
    conn.execute("INSERT INTO eval_runs VALUES (?, ?)", (now.isoformat(), now.isoformat()))
    conn.commit()
    conn.close()
    current_size = db_path.stat().st_size

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    snapshot = backup_dir / "portfolio.db.20261126_024500_000000.gz.enc"
    snapshot.write_bytes(b"encrypted")
    os.utime(snapshot, (now.timestamp(), now.timestamp()))

    # Retired receipt from 30h ago at half size + current receipt from 9h ago at full size.
    # Growth must still fire against the 30h baseline even after the 02:45 backup ran.
    baseline_size = current_size // 2
    _write_backup_receipt(
        backup_dir,
        stamp="20261125_060000_000000",
        source=db_path,
        verified_at=now - timedelta(hours=30),
        source_size_bytes=baseline_size,
    )
    _write_backup_receipt(
        backup_dir,
        stamp="20261126_024500_000000",
        source=db_path,
        verified_at=now - timedelta(hours=9),
        source_size_bytes=current_size,
    )
    # Foreign-source and corrupt receipts must be ignored.
    _write_backup_receipt(
        backup_dir,
        stamp="20261125_010000_000000",
        source=tmp_path / "other.db",
        verified_at=now - timedelta(hours=35),
        source_size_bytes=1,
    )
    (backup_dir / "portfolio.db.20261126_030000_000000.gz.enc.receipt.json").write_text(
        "{not-json", encoding="utf-8"
    )

    monkeypatch.setenv("ES_DB_BACKUP_DIR", str(backup_dir))
    monkeypatch.setenv("ES_DB_GROWTH_WARN_PCT", "20")
    monkeypatch.setenv("ES_DB_GROWTH_WARN_BYTES", str(baseline_size))
    health = db_size_health(db_path, directory=backup_dir, now=now)
    assert health.baseline_bytes == baseline_size
    assert health.growth_bytes == current_size - baseline_size
    assert health.growth_exceeded is True
    assert health.severity == "warn"
    html = operational_alarms(db_path, now=now)
    assert "SQLite database grew sharply" in html

    # Explicit percentage-only trigger: 25% growth below byte jump threshold.
    pct_db = tmp_path / "data" / "pct.db"
    pct_db.write_bytes(b"p" * 12_500)
    pct_dir = tmp_path / "pct_backups"
    pct_dir.mkdir()
    _write_backup_receipt(
        pct_dir,
        stamp="20261125_060000_000000",
        source=pct_db,
        verified_at=now - timedelta(hours=12),
        source_size_bytes=10_000,
    )
    monkeypatch.setenv("ES_DB_GROWTH_WARN_BYTES", "5000")
    monkeypatch.setenv("ES_DB_GROWTH_WARN_PCT", "20")
    pct_health = db_size_health(pct_db, directory=pct_dir, now=now)
    assert pct_health.growth_bytes == 2_500
    assert pct_health.growth_pct == pytest.approx(25.0)
    assert pct_health.growth_exceeded is True

    # When all receipts are older than 48h (e.g. backups started timing out),
    # fall back to the newest historical receipt with source_size_bytes.
    stale_dir = tmp_path / "stale_backups"
    stale_dir.mkdir()
    _write_backup_receipt(
        stale_dir,
        stamp="20261120_024500_000000",
        source=db_path,
        verified_at=now - timedelta(days=6),
        source_size_bytes=baseline_size,
    )
    monkeypatch.setenv("ES_DB_GROWTH_WARN_BYTES", str(baseline_size))
    stale_health = db_size_health(db_path, directory=stale_dir, now=now)
    assert stale_health.baseline_bytes == baseline_size
    assert stale_health.growth_exceeded is True


def test_db_size_health_is_quiet_for_shrinking_legacy_or_steady_db_and_validates_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pipeline.operational_health import db_size_health

    now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    db_path = tmp_path / "portfolio.db"
    db_path.write_bytes(b"x" * 2048)

    legacy_dir = tmp_path / "legacy_backups"
    legacy_dir.mkdir()
    _write_backup_receipt(
        legacy_dir,
        stamp="20261125_024500_000000",
        source=db_path,
        verified_at=now - timedelta(hours=10),
        source_size_bytes=None,
    )
    legacy_health = db_size_health(db_path, directory=legacy_dir, now=now)
    assert legacy_health.baseline_bytes is None
    assert legacy_health.growth_exceeded is False
    assert legacy_health.severity == "ok"

    shrunk_dir = tmp_path / "shrunk_backups"
    shrunk_dir.mkdir()
    _write_backup_receipt(
        shrunk_dir,
        stamp="20261125_024500_000000",
        source=db_path,
        verified_at=now - timedelta(hours=10),
        source_size_bytes=4096,
    )
    monkeypatch.setenv("ES_DB_GROWTH_WARN_BYTES", "512")
    shrunk_health = db_size_health(db_path, directory=shrunk_dir, now=now)
    assert shrunk_health.growth_bytes == -2048
    assert shrunk_health.growth_exceeded is False
    assert shrunk_health.severity == "ok"

    monkeypatch.setenv("ES_DB_SIZE_WARN_BYTES", "5000")
    monkeypatch.setenv("ES_DB_SIZE_CRIT_BYTES", "4000")
    with pytest.raises(ValueError, match="ES_DB_SIZE_CRIT_BYTES"):
        db_size_health(db_path, directory=shrunk_dir, now=now)


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
    assert result["latest_run_id"] == "rmp_today"


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
    assert result["error_summary"] == "stage_1 timed out"


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
