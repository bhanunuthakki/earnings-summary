from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from operations.journal import finish_operation

_REVISION_0011 = "0011_add_operations_journal"
_REVISION_0012 = "0012_close_operation_event_detail_reason"
_WITHHELD = "terminal_detail_withheld"


def _event_hash(
    operation_id: str,
    event_kind: str,
    status: str | None,
    exit_code: int | None,
    severity: str | None,
    detail_code: str | None,
    detail_reason: str | None,
) -> str:
    payload = "\n".join(
        (
            "operation-event/v2",
            operation_id,
            event_kind,
            status or "",
            "" if exit_code is None else str(exit_code),
            severity or "",
            detail_code or "",
            detail_reason or "",
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _config(db_path: Path) -> Config:
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return config


def _seed_0011_events(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    requests: list[str] = []
    for index, token in enumerate(("a", "b"), start=1):
        digest = token * 64
        operation_id = f"operation:{digest}"
        requests.append(operation_id)
        conn.execute(
            "INSERT INTO operation_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                operation_id,
                digest,
                str(index) * 64,
                "task_scheduler",
                "unit-job",
                "scheduled",
                token * 32,
                "unit-job",
                '{"job":"unit-job"}',
                str(index + 2) * 64,
                '["unit-lane"]',
                f"2026-08-13T12:0{index}:00.000000+00:00",
            ),
        )
    legacy_reason = "worker failed"
    conn.executemany(
        "INSERT INTO operation_events VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            (
                "operation-event:" + "c" * 64,
                requests[0],
                "started",
                "4" * 64,
                "2026-08-13T12:03:00.000000+00:00",
                None,
                None,
                None,
                None,
                None,
            ),
            (
                "operation-event:" + "d" * 64,
                requests[0],
                "terminal",
                _event_hash(
                    requests[0],
                    "terminal",
                    "failed",
                    1,
                    "error",
                    "job_failed",
                    legacy_reason,
                ),
                "2026-08-13T12:04:00.000000+00:00",
                "failed",
                1,
                "error",
                "job_failed",
                legacy_reason,
            ),
            (
                "operation-event:" + "e" * 64,
                requests[1],
                "terminal",
                "5" * 64,
                "2026-08-13T12:05:00.000000+00:00",
                "ok",
                0,
                "info",
                "job_ok",
                None,
            ),
        ),
    )
    conn.commit()
    conn.close()


def test_0012_closes_legacy_detail_and_preserves_event_contract(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = migrated_db(
        tmp_path / "detail-closure.db",
        upgrade_from=_REVISION_0011,
        before_upgrade=_seed_0011_events,
    )
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    rows = conn.execute(
        "SELECT event_id,operation_id,event_kind,event_sha256,occurred_at,status,exit_code,"
        "severity,detail_code,detail_reason FROM operation_events ORDER BY event_id"
    ).fetchall()

    assert len(rows) == 3
    assert [(row[0], row[1], row[2], row[4], row[5], row[6], row[7], row[8]) for row in rows] == [
        (
            "operation-event:" + "c" * 64,
            "operation:" + "a" * 64,
            "started",
            "2026-08-13T12:03:00.000000+00:00",
            None,
            None,
            None,
            None,
        ),
        (
            "operation-event:" + "d" * 64,
            "operation:" + "a" * 64,
            "terminal",
            "2026-08-13T12:04:00.000000+00:00",
            "failed",
            1,
            "error",
            "job_failed",
        ),
        (
            "operation-event:" + "e" * 64,
            "operation:" + "b" * 64,
            "terminal",
            "2026-08-13T12:05:00.000000+00:00",
            "ok",
            0,
            "info",
            "job_ok",
        ),
    ]
    assert rows[0][3] == "4" * 64
    assert rows[0][9] is None
    assert rows[1][9] == _WITHHELD
    assert rows[1][3] == _event_hash(
        rows[1][1], "terminal", "failed", 1, "error", "job_failed", _WITHHELD
    )
    replay = finish_operation(
        conn,
        operation_id=str(rows[1][1]),
        status="failed",
        exit_code=1,
        severity="error",
        occurred_at=datetime.fromisoformat(str(rows[1][4])),
        detail_reason="a different nonempty failure detail",
    )
    assert replay.event_id == rows[1][0]
    assert replay.detail_reason == _WITHHELD
    assert rows[2][3] == "5" * 64
    assert rows[2][9] is None
    table_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='operation_events'"
    ).fetchone()[0]
    assert "detail_reason IS NULL OR detail_reason='terminal_detail_withheld'" in table_sql
    assert tuple(
        row[2] for row in conn.execute("PRAGMA index_info(ix_operation_events_operation_id)")
    ) == ("operation_id", "occurred_at")
    foreign_keys = conn.execute("PRAGMA foreign_key_list(operation_events)").fetchall()
    assert len(foreign_keys) == 1
    assert tuple(foreign_keys[0][2:8]) == (
        "operation_requests",
        "operation_id",
        "operation_id",
        "NO ACTION",
        "NO ACTION",
        "NONE",
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE operation_events SET detail_reason=NULL WHERE event_id=?",
            (rows[1][0],),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM operation_events WHERE event_id=?", (rows[1][0],))
    conn.close()


def test_0012_downgrade_keeps_irreversibly_withheld_detail(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = migrated_db(
        tmp_path / "detail-closure-down.db",
        upgrade_from=_REVISION_0011,
        before_upgrade=_seed_0011_events,
    )
    before = (
        sqlite3.connect(db_path)
        .execute(
            "SELECT event_sha256,detail_reason FROM operation_events "
            "WHERE detail_reason IS NOT NULL"
        )
        .fetchone()
    )

    command.downgrade(_config(db_path), _REVISION_0011)

    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (_REVISION_0011,)
    assert (
        conn.execute(
            "SELECT event_sha256,detail_reason FROM operation_events WHERE detail_reason IS NOT NULL"
        ).fetchone()
        == before
    )
    conn.close()
