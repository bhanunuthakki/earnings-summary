"""Backfill entrypoint ownership and incomplete-result contracts."""

from __future__ import annotations

import json
import sqlite3
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path

import pytest

from execution import backfill_evidence_ledger as cli
from provenance.evidence_backfill import BackfillRequest, BackfillSummary
from run_lock import hold_run_lock
from runtime.job_runtime import JobLock
from sqlite_runtime import SQLiteConnectionRole


def _stub_batch(
    monkeypatch: pytest.MonkeyPatch, *, quarantined: int = 0
) -> list[tuple[SQLiteConnectionRole, bool | None]]:
    connections: list[tuple[SQLiteConnectionRole, bool | None]] = []

    def connect(
        _path: Path, *, role: SQLiteConnectionRole, schema_preflight: bool | None = None
    ) -> sqlite3.Connection:
        connections.append((role, schema_preflight))
        return sqlite3.connect(":memory:")

    def backfill(_conn: sqlite3.Connection, request: BackfillRequest) -> BackfillSummary:
        return BackfillSummary(
            task_id=request.task_id,
            mode="apply" if request.apply else "dry_run",
            dry_run=not request.apply,
            batch_size=request.batch_size,
            run_at=datetime.now(UTC),
            last_document_id_before=0,
            last_document_id_after=1,
            has_more=False,
            documents_considered=1,
            documents_quarantined=quarantined,
        )

    monkeypatch.setattr(cli, "connect_sqlite", connect)
    monkeypatch.setattr(cli, "backfill_legacy_evidence", backfill)
    return connections


@pytest.mark.parametrize("resource", ["database", "checkpoint"])
def test_apply_cannot_open_database_with_owned_write_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    resource: str,
) -> None:
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    connections = _stub_batch(monkeypatch)
    database = tmp_path / "evidence.db"
    checkpoint = tmp_path / ".tmp" / "shared-task" / "state.json"
    with ExitStack() as held:
        if resource == "database":
            held.enter_context(hold_run_lock(database, owner="other-owner", timeout_s=0))
        else:
            held.enter_context(
                JobLock(tmp_path, "other-owner", [f"artifact:{checkpoint}"], wait_s=0)
            )
        result = cli.main(["--db", str(database), "--task-id", "shared-task", "--apply"])

    assert result == 75
    assert connections == []
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err)["event"] == "evidence_ledger_backfill_locked"


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("quarantined", [0, 1])
def test_batch_json_preserves_quarantine_and_returns_incomplete_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    apply: bool,
    quarantined: int,
) -> None:
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    connections = _stub_batch(monkeypatch, quarantined=quarantined)
    args = ["--db", str(tmp_path / "evidence.db")]
    if apply:
        args.append("--apply")

    assert cli.main(args) == (2 if quarantined else 0)

    result = json.loads(capsys.readouterr().out)
    assert result["documents_quarantined"] == quarantined
    assert result["dry_run"] is (not apply)
    assert connections == [
        (SQLiteConnectionRole.WRITER if apply else SQLiteConnectionRole.READ_ONLY, apply)
    ]
    if not apply:
        assert not (tmp_path / ".tmp").exists()
    else:
        assert not list((tmp_path / ".tmp" / "job_locks").glob("*.lock"))


def test_apply_contends_with_canonical_lock_from_another_code_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_root = tmp_path / "first-code"
    second_root = tmp_path / "second-code"
    state_root = tmp_path / "state"
    database = state_root / "evidence.db"
    monkeypatch.setattr(cli, "PROJECT_ROOT", second_root)
    connections = _stub_batch(monkeypatch)
    with hold_run_lock(database, owner=str(first_root), timeout_s=0):
        assert cli.main(["--db", str(database), "--repo-root", str(state_root), "--apply"]) == 75
    assert connections == []


@pytest.mark.parametrize("same_database", [False, True])
def test_inherited_lock_is_reused_only_for_exact_explicit_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, same_database: bool
) -> None:
    database = (tmp_path / "actual.db").resolve()
    configured = database if same_database else tmp_path / "different.db"
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path / "code")

    def configured_database(_root: Path) -> Path:
        return configured

    def inherited(_root: Path, _name: str) -> bool:
        return True

    monkeypatch.setattr(cli, "portfolio_db_path", configured_database)
    monkeypatch.setattr(cli, "inherited_lock_is_valid", inherited)
    connections = _stub_batch(monkeypatch)
    with hold_run_lock(database, owner="parent", timeout_s=0):
        result = cli.main(
            ["--db", str(database), "--repo-root", str(tmp_path / "state"), "--apply"]
        )
    assert result == (0 if same_database else 75)
    assert bool(connections) is same_database


def test_checkpoint_ownership_is_shared_across_code_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    checkpoint = state_root / ".tmp" / "shared" / "state.json"
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path / "other-code")
    connections = _stub_batch(monkeypatch)
    with JobLock(state_root, "first-code-owner", [f"artifact:{checkpoint}"], wait_s=0):
        result = cli.main(
            [
                "--db",
                str(tmp_path / "actual.db"),
                "--repo-root",
                str(state_root),
                "--task-id",
                "shared",
                "--apply",
            ]
        )
    assert result == 75
    assert connections == []
