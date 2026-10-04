"""The reviewed SEC command keeps dry planning and exact apply separate."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from execution import publish_reviewed_sec_financial_tables as command
from runtime.job_runtime import JobAlreadyRunningError
from sqlite_runtime import register_sqlite_integrity_functions
from tests.test_reviewed_sec_financial_tables import synthetic_sec_request


def test_cli_readonly_plan_and_exact_apply(
    migrated_db: Callable[..., Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = migrated_db(tmp_path / "source.db")
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        register_sqlite_integrity_functions(conn)
        request = synthetic_sec_request(conn, tmp_path)
    path = tmp_path / "review.json"
    path.write_text(request.model_dump_json())
    args = ["--db", str(db), "--request", str(path)]
    assert command.main(args) == 0
    planned = json.loads(capsys.readouterr().out)
    assert planned["mode"] == "dry_run"
    assert planned["publication_id"] is None
    assert command.main([*args, "--apply", "--expected-plan-sha256", planned["plan_sha256"]]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["mode"] == "apply"
    assert applied["captured_count"] == 2
    assert applied["plan_sha256"] == planned["plan_sha256"]
    assert applied["canonical_admission"] == "not_performed"
    assert command.main([*args, "--apply", "--expected-plan-sha256", planned["plan_sha256"]]) == 0
    assert json.loads(capsys.readouterr().out)["exact_replay"] is True


def test_cli_rejects_missing_plan_before_opening_database(
    migrated_db: Callable[..., Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = migrated_db(tmp_path / "seed.db")
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        register_sqlite_integrity_functions(conn)
        request = synthetic_sec_request(conn, tmp_path)
    path = tmp_path / "review.json"
    path.write_text(request.model_dump_json())
    absent = tmp_path / "absent.db"
    assert command.main(["--db", str(absent), "--request", str(path), "--apply"]) == 2
    assert not absent.exists()
    assert json.loads(capsys.readouterr().out)["outcome"] == "blocked"


def test_cli_busy_writer_does_not_load_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class BusyLock:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> None:
            raise JobAlreadyRunningError("busy")

        def __exit__(self, *_args: object) -> None:
            pass

    monkeypatch.setattr(command, "JobLock", BusyLock)
    assert (
        command.main(
            [
                "--db",
                str(tmp_path / "absent.db"),
                "--request",
                str(tmp_path / "absent.json"),
                "--apply",
            ]
        )
        == 75
    )
    assert json.loads(capsys.readouterr().out)["reason_code"] == "database_writer_busy"
