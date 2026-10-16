"""The ONON role command owns its transaction and exact apply commitment."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from dcf.reviewed_input_roles import InputRoleManifest
from execution import review_onon_input_roles as command
from runtime.job_runtime import JobAlreadyRunningError
from tests.test_reviewed_input_roles import reviewed_source as reviewed_source


def test_role_cli_dry_apply_replay_and_failed_plan(
    reviewed_source: tuple[sqlite3.Connection, InputRoleManifest],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    conn, manifest = reviewed_source
    db = str(conn.execute("PRAGMA database_list").fetchone()[2])
    path = tmp_path / "review.json"
    path.write_text(manifest.model_dump_json())
    args = ["--db", db, "--request", str(path)]
    before = conn.execute("SELECT COUNT(*) FROM canonical_metric_definition_revisions").fetchone()[
        0
    ]
    assert command.main(args) == 0
    planned = json.loads(capsys.readouterr().out)
    assert planned["mode"] == "dry_run"
    assert (
        conn.execute("SELECT COUNT(*) FROM canonical_metric_definition_revisions").fetchone()[0]
        == before
    )
    assert command.main([*args, "--apply", "--expected-plan-sha256", "0" * 64]) == 2
    capsys.readouterr()
    assert (
        conn.execute("SELECT COUNT(*) FROM canonical_metric_definition_revisions").fetchone()[0]
        == before
    )
    apply_args = [*args, "--apply", "--expected-plan-sha256", planned["plan_sha256"]]
    assert command.main(apply_args) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["definitions_created"] > 0
    assert applied["attribution"] == "analyst"
    assert applied["requires_new_ontology_and_research_snapshots"] is True
    assert command.main(apply_args) == 0
    assert json.loads(capsys.readouterr().out)["exact_replay"] is True


def test_role_cli_missing_plan_does_not_open_database(tmp_path: Path) -> None:
    absent = tmp_path / "absent.db"
    assert (
        command.main(["--db", str(absent), "--request", str(tmp_path / "absent.json"), "--apply"])
        == 2
    )
    assert not absent.exists()


def test_role_cli_busy_writer_does_not_load_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
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
