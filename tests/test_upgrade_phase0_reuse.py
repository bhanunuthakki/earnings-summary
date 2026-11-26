from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import upgrade_database as owner
from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine

from execution import portfolio_readiness_receipt as readiness
from execution.backup_restore_readiness_receipt import (
    BackupRestoreReadinessReceipt,
    collect_backup_restore_receipt,
)
from schema_compat import expected_head
from sqlite_snapshot import SnapshotRequest, create_snapshot

ROOT = Path(__file__).resolve().parents[1]
OLD = "0061_bulk_lexical_projection_coverage"
NEW = "0062_reviewed_pdf_pages"
HEAD = expected_head()


def setup_live(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path]:
    db = migrated_db(tmp_path / "synthetic.db", target=OLD)
    snapshot = tmp_path / "supported.db"
    created = create_snapshot(SnapshotRequest(source_path=db, destination_path=snapshot))
    receipt = collect_backup_restore_receipt(
        source_db=db, snapshot_db=snapshot, manifest_path=created.manifest_path
    )
    assert receipt.verified
    receipt_path = tmp_path / "phase0.json"
    receipt_path.write_text(receipt.model_dump_json())
    monkeypatch.setattr(owner, "authoritative_managed_runtime_root", lambda: ROOT)

    def managed_paths(_root: Path) -> tuple[Path, ...]:
        return (db,)

    monkeypatch.setattr(owner, "authoritative_managed_database_paths", managed_paths)

    def fetch_origin(_root: Path) -> readiness.OriginMainObservation:
        return readiness.OriginMainObservation(sha="a" * 40, fetched_at=datetime.now(UTC))

    def ready_receipt(**_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            ready=True,
            db_revision=OLD,
            blocking_reasons=(),
            backup_restore_evidence_id=receipt.evidence_id,
            backup_restore_receipt_path=str(receipt_path.resolve()),
        )

    monkeypatch.setattr(readiness, "fetch_origin_main", fetch_origin)
    monkeypatch.setattr(readiness, "collect_readiness", ready_receipt)
    return db, snapshot, receipt_path


def test_reuse_phase0_does_not_allocate_second_snapshot(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    db, snapshot, receipt = setup_live(tmp_path, migrated_db, monkeypatch)
    original = snapshot.read_bytes()

    def reject_copy(*_args: object) -> None:
        raise AssertionError("second snapshot allocation")

    monkeypatch.setattr(owner, "_backup_database", reject_copy)
    result = owner.upgrade_database(
        db,
        repo_root=ROOT,
        runtime_root=ROOT,
        phase0_backup_restore_receipt=receipt,
        reuse_phase0_snapshot=True,
    )
    assert result.to_revision == HEAD
    assert result.backup_reused is True
    assert result.backup_path == str(snapshot.resolve())
    assert result.backup_restore_evidence_id == json.loads(receipt.read_text())["evidence_id"]
    assert snapshot.read_bytes() == original


def test_reuse_phase0_with_real_readiness_validates_source_only_once_under_lock(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    db = migrated_db(tmp_path / "synthetic-real.db", target=OLD)
    snapshot = tmp_path / "supported-real.db"
    created = create_snapshot(SnapshotRequest(source_path=db, destination_path=snapshot))
    receipt = collect_backup_restore_receipt(
        source_db=db, snapshot_db=snapshot, manifest_path=created.manifest_path
    )
    assert receipt.verified
    receipt_path = tmp_path / "phase0-real.json"
    receipt_path.write_text(receipt.model_dump_json(), encoding="utf-8")

    def managed_paths(_root: Path) -> tuple[Path, ...]:
        return (db,)

    def fetch_origin(_root: Path) -> readiness.OriginMainObservation:
        return origin

    def git_sha(_root: Path) -> str:
        return sha

    def clean_status(_root: Path) -> tuple[str, ...]:
        return ()

    def is_ancestor(_root: Path, _ancestor: str, _descendant: str) -> bool:
        return True

    monkeypatch.setattr(owner, "authoritative_managed_runtime_root", lambda: ROOT)
    monkeypatch.setattr(owner, "authoritative_managed_database_paths", managed_paths)

    sha = "c" * 40
    origin = readiness.OriginMainObservation(sha=sha, fetched_at=datetime.now(UTC))
    real_collect_readiness = readiness.collect_readiness

    def real_collect_with_clean_git(
        *,
        checkout_root: Path,
        runtime_root: Path,
        db_path: Path | None = None,
        backup_restore_receipt_path: Path | None = None,
        mode: readiness.ReadinessMode = "operational",
        origin_resolver: readiness.OriginResolver = readiness.fetch_origin_main,
    ) -> readiness.PortfolioReadinessReceipt:
        return real_collect_readiness(
            checkout_root=checkout_root,
            runtime_root=runtime_root,
            db_path=db_path,
            backup_restore_receipt_path=backup_restore_receipt_path,
            mode=mode,
            git_sha_resolver=git_sha,
            git_status_resolver=clean_status,
            origin_resolver=origin_resolver,
            ancestry_resolver=is_ancestor,
        )

    monkeypatch.setattr(readiness, "fetch_origin_main", fetch_origin)
    monkeypatch.setattr(readiness, "collect_readiness", real_collect_with_clean_git)

    readiness_calls: list[bool] = []
    owner_calls: list[bool] = []
    real_readiness_validate = readiness.validate_receipt_for_source
    real_owner_validate = owner.validate_receipt_for_source

    def count_readiness_validate(
        rec: BackupRestoreReadinessReceipt,
        *,
        source_db: Path,
        source_revision: str | None,
        require_current_identity: bool = True,
    ) -> tuple[str, ...]:
        readiness_calls.append(require_current_identity)
        return real_readiness_validate(
            rec,
            source_db=source_db,
            source_revision=source_revision,
            require_current_identity=require_current_identity,
        )

    def count_owner_validate(
        rec: BackupRestoreReadinessReceipt,
        *,
        source_db: Path,
        source_revision: str | None,
        require_current_identity: bool = True,
    ) -> tuple[str, ...]:
        owner_calls.append(require_current_identity)
        return real_owner_validate(
            rec,
            source_db=source_db,
            source_revision=source_revision,
            require_current_identity=require_current_identity,
        )

    monkeypatch.setattr(readiness, "validate_receipt_for_source", count_readiness_validate)
    monkeypatch.setattr(owner, "validate_receipt_for_source", count_owner_validate)

    def reject_copy(*_args: object) -> None:
        raise AssertionError("second snapshot allocation")

    monkeypatch.setattr(owner, "_backup_database", reject_copy)

    result = owner.upgrade_database(
        db,
        repo_root=ROOT,
        runtime_root=ROOT,
        phase0_backup_restore_receipt=receipt_path,
        reuse_phase0_snapshot=True,
    )
    assert result.to_revision == HEAD
    assert result.backup_reused is True
    assert result.backup_path == str(snapshot.resolve())
    assert readiness_calls == [True]
    assert owner_calls == []


@pytest.mark.parametrize("fault", ["source", "snapshot", "receipt", "seal"])
def test_reuse_phase0_rejects_stale_or_rewritten_evidence_before_migration(
    fault: str, tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    db, snapshot, receipt = setup_live(tmp_path, migrated_db, monkeypatch)
    if fault == "source":
        with sqlite3.connect(db) as connection:
            connection.execute("PRAGMA user_version=27")
    elif fault == "snapshot":
        with snapshot.open("r+b") as stream:
            stream.seek(100)
            stream.write(b"changed!")
    elif fault == "receipt":
        body = json.loads(receipt.read_text())
        body["snapshot_resolved_path"] = str(db)
        receipt.write_text(json.dumps(body))
    else:

        def wrong_seal(**_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(
                ready=True,
                db_revision=OLD,
                blocking_reasons=(),
                backup_restore_evidence_id="b" * 64,
                backup_restore_receipt_path=str(receipt.resolve()),
            )

        monkeypatch.setattr(readiness, "collect_readiness", wrong_seal)
    with pytest.raises(owner.UpgradeDatabaseError, match=r"reuse|Phase-0"):
        owner.upgrade_database(
            db,
            repo_root=ROOT,
            runtime_root=ROOT,
            phase0_backup_restore_receipt=receipt,
            reuse_phase0_snapshot=True,
        )
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (OLD,)


def test_reviewed_page_migration_rolls_back_first_ddl_failure(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    db = migrated_db(tmp_path / "ddl-failure.db", target=OLD)

    def fail_after_first_ddl(
        _connection: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if statement.startswith("CREATE TABLE IF NOT EXISTS reviewed_pdf_page_revisions"):
            raise RuntimeError("injected_after_first_reviewed_page_ddl")

    event.listen(Engine, "after_cursor_execute", fail_after_first_ddl)
    try:
        with pytest.raises(RuntimeError, match="injected_after_first_reviewed_page_ddl"):
            migrated_db(db, target=NEW, upgrade_existing=True)
    finally:
        event.remove(Engine, "after_cursor_execute", fail_after_first_ddl)
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (OLD,)
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE '%reviewed_pdf_page%'"
            ).fetchall()
            == []
        )
