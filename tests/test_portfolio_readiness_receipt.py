from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

import pytest

from execution import backup_restore_readiness_receipt as backup_receipt
from execution import portfolio_readiness_receipt as readiness
from sqlite_snapshot import SnapshotRequest, create_snapshot

NOW = datetime(2026, 8, 14, tzinfo=UTC)
SHA = "a" * 40


class _AlignedKwargs(TypedDict):
    git_sha_resolver: readiness.GitShaResolver
    git_status_resolver: readiness.GitStatusResolver
    origin_resolver: readiness.OriginResolver
    ancestry_resolver: readiness.GitAncestryResolver


def _revision_repo(root: Path, *, revision: str, prior: str | None = None) -> Path:
    versions = root / "alembic" / "versions"
    versions.mkdir(parents=True)
    (versions / "0001_test.py").write_text(
        f'revision = "{revision}"\ndown_revision = {prior!r}\n',
        encoding="utf-8",
    )
    if prior is not None:
        (versions / "0000_prior.py").write_text(
            f'revision = "{prior}"\ndown_revision = None\n',
            encoding="utf-8",
        )
    return root


def _versioned_db(path: Path, *, revision: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE alembic_version (version_num TEXT NOT NULL)")
        conn.execute("INSERT INTO alembic_version(version_num) VALUES (?)", (revision,))
        conn.commit()
    finally:
        conn.close()
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _backup_receipt(source: Path, root: Path) -> Path:
    snapshot = root / "snapshot.db"
    create_snapshot(SnapshotRequest(source_path=source, destination_path=snapshot))
    receipt = backup_receipt.collect_backup_restore_receipt(
        source_db=source,
        snapshot_db=snapshot,
    )
    path = root / "backup-restore-receipt.json"
    path.write_text(receipt.model_dump_json(indent=2), encoding="utf-8")
    return path


def _aligned_kwargs(checkout: Path, runtime: Path) -> _AlignedKwargs:
    del checkout, runtime

    def git_sha_resolver(_root: Path) -> str:
        return SHA

    def git_status_resolver(_root: Path) -> tuple[str, ...]:
        return ()

    def origin_resolver(_root: Path) -> readiness.OriginMainObservation:
        return readiness.OriginMainObservation(sha=SHA, fetched_at=NOW)

    def ancestry_resolver(_root: Path, _ancestor: str, _descendant: str) -> bool:
        return True

    return {
        "git_sha_resolver": git_sha_resolver,
        "git_status_resolver": git_status_resolver,
        "origin_resolver": origin_resolver,
        "ancestry_resolver": ancestry_resolver,
    }


def test_backup_restore_receipt_binds_source_snapshot_and_verifier(tmp_path: Path) -> None:
    source = _versioned_db(tmp_path / "source.db", revision=readiness.ACTIVE_HEAD)
    receipt_path = _backup_receipt(source, tmp_path)

    receipt = backup_receipt.BackupRestoreReadinessReceipt.model_validate_json(
        receipt_path.read_text(encoding="utf-8")
    )

    assert receipt.verified is True
    assert receipt.source_db_resolved_path == str(source.resolve())
    assert receipt.source_db_revision == readiness.ACTIVE_HEAD
    assert receipt.restored_db_revision == readiness.ACTIVE_HEAD
    assert receipt.snapshot_sha256 == _sha(tmp_path / "snapshot.db")
    assert receipt.verifier_code_sha256 == backup_receipt.verifier_code_sha256()
    assert backup_receipt.evidence_id_is_valid(receipt)
    assert receipt.authorizes_downstream_write is False
    assert receipt.downstream_locked_revalidation_required is True


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    (
        ("schema_version", "sqlite-reader-snapshot/v999", "snapshot_manifest_schema_unsupported"),
        ("code_config_version", "unknown-producer/v1", "snapshot_manifest_code_unsupported"),
    ),
)
def test_backup_restore_receipt_rejects_unsupported_manifest_contract(
    tmp_path: Path,
    field: str,
    value: str,
    reason: str,
) -> None:
    source = _versioned_db(tmp_path / "source.db", revision=readiness.ACTIVE_HEAD)
    snapshot = tmp_path / "snapshot.db"
    result = create_snapshot(SnapshotRequest(source_path=source, destination_path=snapshot))
    manifest_path = result.manifest_path
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[field] = value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    receipt = backup_receipt.collect_backup_restore_receipt(
        source_db=source,
        snapshot_db=snapshot,
    )

    assert receipt.verified is False
    assert reason in receipt.blocking_reasons


def test_collect_readiness_clears_only_for_aligned_lineage_and_restore(
    tmp_path: Path,
) -> None:
    checkout = _revision_repo(tmp_path / "checkout", revision=readiness.ACTIVE_HEAD)
    runtime = _revision_repo(tmp_path / "runtime", revision=readiness.ACTIVE_HEAD)
    db_path = _versioned_db(runtime / "data" / "portfolio.db", revision=readiness.ACTIVE_HEAD)
    restore_receipt = _backup_receipt(db_path, tmp_path / "backup")
    before = _sha(db_path)

    receipt = readiness.collect_readiness(
        checkout_root=checkout,
        runtime_root=runtime,
        db_path=db_path,
        backup_restore_receipt_path=restore_receipt,
        **_aligned_kwargs(checkout, runtime),
    )

    assert receipt.ready is True
    assert receipt.observed_at.tzinfo is not None
    assert len(receipt.evidence_id) == 64
    assert receipt.expected_active_alembic_head == readiness.ACTIVE_HEAD
    assert receipt.checkout_alembic_head == readiness.ACTIVE_HEAD
    assert receipt.runtime_alembic_head == readiness.ACTIVE_HEAD
    assert receipt.origin_main_sha == SHA
    assert receipt.checkout_is_ancestor_of_origin_main is True
    assert receipt.runtime_is_ancestor_of_origin_main is True
    assert receipt.db_path_requested == str(db_path)
    assert receipt.db_path_resolved == str(db_path.resolve())
    assert receipt.db_revision == readiness.ACTIVE_HEAD
    assert receipt.drift_state == "clear"
    assert receipt.backup_restore_evidence_id is not None
    assert receipt.operationally_aligned is True
    assert receipt.migration_preconditions_met is False
    assert receipt.blocking_reasons == ()
    assert receipt.point_in_time_only is True
    assert receipt.authorizes_downstream_write is False
    assert receipt.downstream_locked_revalidation_required is True
    assert _sha(db_path) == before


def test_collect_readiness_blocks_runtime_checkout_mismatch(tmp_path: Path) -> None:
    checkout = _revision_repo(tmp_path / "checkout", revision=readiness.ACTIVE_HEAD)
    runtime = _revision_repo(tmp_path / "runtime", revision=readiness.ACTIVE_HEAD)
    db_path = _versioned_db(runtime / "data" / "portfolio.db", revision=readiness.ACTIVE_HEAD)
    restore_receipt = _backup_receipt(db_path, tmp_path / "backup")

    kwargs = _aligned_kwargs(checkout, runtime)

    def mismatched_sha(root: Path) -> str:
        return SHA if root == checkout.resolve() else "b" * 40

    kwargs["git_sha_resolver"] = mismatched_sha
    receipt = readiness.collect_readiness(
        checkout_root=checkout,
        runtime_root=runtime,
        db_path=db_path,
        backup_restore_receipt_path=restore_receipt,
        **kwargs,
    )

    assert receipt.ready is False
    assert "runtime_checkout_sha_mismatch" in receipt.blocking_reasons


def test_collect_readiness_blocks_relevant_dirty_or_untracked_code(tmp_path: Path) -> None:
    checkout = _revision_repo(tmp_path / "checkout", revision=readiness.ACTIVE_HEAD)
    runtime = _revision_repo(tmp_path / "runtime", revision=readiness.ACTIVE_HEAD)
    db_path = _versioned_db(runtime / "data" / "portfolio.db", revision=readiness.ACTIVE_HEAD)
    restore_receipt = _backup_receipt(db_path, tmp_path / "backup")
    kwargs = _aligned_kwargs(checkout, runtime)

    def dirty_status(root: Path) -> tuple[str, ...]:
        return ("?? execution/untracked.py",) if root == checkout.resolve() else ()

    kwargs["git_status_resolver"] = dirty_status

    receipt = readiness.collect_readiness(
        checkout_root=checkout,
        runtime_root=runtime,
        db_path=db_path,
        backup_restore_receipt_path=restore_receipt,
        **kwargs,
    )

    assert receipt.ready is False
    assert receipt.checkout_relevant_changes == ("?? execution/untracked.py",)
    assert "checkout_relevant_changes_present" in receipt.blocking_reasons


def test_collect_readiness_independently_blocks_runtime_alembic_mismatch(
    tmp_path: Path,
) -> None:
    checkout = _revision_repo(tmp_path / "checkout", revision=readiness.ACTIVE_HEAD)
    runtime = _revision_repo(tmp_path / "runtime", revision="0009_runtime")
    db_path = _versioned_db(runtime / "data" / "portfolio.db", revision=readiness.ACTIVE_HEAD)
    restore_receipt = _backup_receipt(db_path, tmp_path / "backup")

    receipt = readiness.collect_readiness(
        checkout_root=checkout,
        runtime_root=runtime,
        db_path=db_path,
        backup_restore_receipt_path=restore_receipt,
        **_aligned_kwargs(checkout, runtime),
    )

    assert receipt.runtime_alembic_head == "0009_runtime"
    assert "runtime_checkout_alembic_head_mismatch" in receipt.blocking_reasons


def test_collect_readiness_requires_fresh_origin_main_identity(tmp_path: Path) -> None:
    checkout = _revision_repo(tmp_path / "checkout", revision=readiness.ACTIVE_HEAD)
    runtime = _revision_repo(tmp_path / "runtime", revision=readiness.ACTIVE_HEAD)
    db_path = _versioned_db(runtime / "data" / "portfolio.db", revision=readiness.ACTIVE_HEAD)
    restore_receipt = _backup_receipt(db_path, tmp_path / "backup")
    kwargs = _aligned_kwargs(checkout, runtime)

    def other_origin(_root: Path) -> readiness.OriginMainObservation:
        return readiness.OriginMainObservation(sha="b" * 40, fetched_at=NOW)

    kwargs["origin_resolver"] = other_origin

    receipt = readiness.collect_readiness(
        checkout_root=checkout,
        runtime_root=runtime,
        db_path=db_path,
        backup_restore_receipt_path=restore_receipt,
        **kwargs,
    )

    assert receipt.ready is False
    assert receipt.origin_main_sha == "b" * 40
    assert "checkout_not_at_fresh_origin_main" in receipt.blocking_reasons


def test_collect_readiness_reports_schema_drift(tmp_path: Path) -> None:
    prior_revision = "0000_prior"
    checkout = _revision_repo(
        tmp_path / "checkout",
        revision=readiness.ACTIVE_HEAD,
        prior=prior_revision,
    )
    runtime = _revision_repo(
        tmp_path / "runtime",
        revision=readiness.ACTIVE_HEAD,
        prior=prior_revision,
    )
    db_path = _versioned_db(runtime / "data" / "portfolio.db", revision=prior_revision)
    restore_receipt = _backup_receipt(db_path, tmp_path / "backup")

    receipt = readiness.collect_readiness(
        checkout_root=checkout,
        runtime_root=runtime,
        db_path=db_path,
        backup_restore_receipt_path=restore_receipt,
        **_aligned_kwargs(checkout, runtime),
    )

    assert receipt.ready is False
    assert receipt.db_revision == prior_revision
    assert receipt.drift_state == "db_behind_code"
    assert "schema_drift:db_behind_code" in receipt.blocking_reasons
    assert receipt.migration_preconditions_met is True


def test_collect_readiness_clears_migration_mode_only_with_exact_old_source(
    tmp_path: Path,
) -> None:
    prior_revision = "0000_prior"
    checkout = _revision_repo(
        tmp_path / "checkout",
        revision=readiness.ACTIVE_HEAD,
        prior=prior_revision,
    )
    runtime = _revision_repo(
        tmp_path / "runtime",
        revision=readiness.ACTIVE_HEAD,
        prior=prior_revision,
    )
    db_path = _versioned_db(runtime / "data" / "portfolio.db", revision=prior_revision)
    restore_receipt = _backup_receipt(db_path, tmp_path / "backup")

    receipt = readiness.collect_readiness(
        checkout_root=checkout,
        runtime_root=runtime,
        db_path=db_path,
        backup_restore_receipt_path=restore_receipt,
        mode="migration",
        **_aligned_kwargs(checkout, runtime),
    )

    assert receipt.ready is True
    assert receipt.migration_preconditions_met is True
    assert receipt.operationally_aligned is False
    assert receipt.blocking_reasons == ()


def test_collect_readiness_requires_backup_restore_receipt(tmp_path: Path) -> None:
    checkout = _revision_repo(tmp_path / "checkout", revision=readiness.ACTIVE_HEAD)
    runtime = _revision_repo(tmp_path / "runtime", revision=readiness.ACTIVE_HEAD)
    db_path = _versioned_db(runtime / "data" / "portfolio.db", revision=readiness.ACTIVE_HEAD)

    receipt = readiness.collect_readiness(
        checkout_root=checkout,
        runtime_root=runtime,
        db_path=db_path,
        **_aligned_kwargs(checkout, runtime),
    )

    assert receipt.ready is False
    assert "backup_restore_receipt_required" in receipt.blocking_reasons


def test_migration_mode_rejects_stale_source_bound_restore_receipt(
    tmp_path: Path,
) -> None:
    checkout = _revision_repo(tmp_path / "checkout", revision=readiness.ACTIVE_HEAD)
    runtime = _revision_repo(tmp_path / "runtime", revision=readiness.ACTIVE_HEAD)
    db_path = _versioned_db(runtime / "data" / "portfolio.db", revision=readiness.ACTIVE_HEAD)
    restore_receipt = _backup_receipt(db_path, tmp_path / "backup")
    stat = db_path.stat()
    os.utime(db_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

    receipt = readiness.collect_readiness(
        checkout_root=checkout,
        runtime_root=runtime,
        db_path=db_path,
        backup_restore_receipt_path=restore_receipt,
        mode="migration",
        **_aligned_kwargs(checkout, runtime),
    )

    assert receipt.ready is False
    assert "backup_restore_source_identity_stale" in receipt.blocking_reasons


def test_operational_mode_accepts_verified_ancestor_rollback_after_upgrade(
    tmp_path: Path,
) -> None:
    prior_revision = "0000_prior"
    checkout = _revision_repo(
        tmp_path / "checkout",
        revision=readiness.ACTIVE_HEAD,
        prior=prior_revision,
    )
    runtime = _revision_repo(
        tmp_path / "runtime",
        revision=readiness.ACTIVE_HEAD,
        prior=prior_revision,
    )
    db_path = _versioned_db(runtime / "data" / "portfolio.db", revision=prior_revision)
    restore_receipt = _backup_receipt(db_path, tmp_path / "backup")
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE alembic_version SET version_num=?",
            (readiness.ACTIVE_HEAD,),
        )
        connection.commit()

    receipt = readiness.collect_readiness(
        checkout_root=checkout,
        runtime_root=runtime,
        db_path=db_path,
        backup_restore_receipt_path=restore_receipt,
        **_aligned_kwargs(checkout, runtime),
    )

    assert receipt.ready is True
    assert receipt.operationally_aligned is True
    assert receipt.migration_preconditions_met is False


def test_collect_readiness_rejects_tampered_snapshot_artifact(tmp_path: Path) -> None:
    checkout = _revision_repo(tmp_path / "checkout", revision=readiness.ACTIVE_HEAD)
    runtime = _revision_repo(tmp_path / "runtime", revision=readiness.ACTIVE_HEAD)
    db_path = _versioned_db(runtime / "data" / "portfolio.db", revision=readiness.ACTIVE_HEAD)
    restore_receipt = _backup_receipt(db_path, tmp_path / "backup")
    with (tmp_path / "backup" / "snapshot.db").open("ab") as handle:
        handle.write(b"tamper")

    receipt = readiness.collect_readiness(
        checkout_root=checkout,
        runtime_root=runtime,
        db_path=db_path,
        backup_restore_receipt_path=restore_receipt,
        **_aligned_kwargs(checkout, runtime),
    )

    assert receipt.ready is False
    assert "backup_restore_snapshot_identity_mismatch" in receipt.blocking_reasons


def test_collect_readiness_fails_closed_on_multiple_database_heads(tmp_path: Path) -> None:
    checkout = _revision_repo(tmp_path / "checkout", revision=readiness.ACTIVE_HEAD)
    runtime = _revision_repo(tmp_path / "runtime", revision=readiness.ACTIVE_HEAD)
    db_path = _versioned_db(runtime / "data" / "portfolio.db", revision=readiness.ACTIVE_HEAD)
    with sqlite3.connect(db_path) as connection:
        connection.execute("INSERT INTO alembic_version(version_num) VALUES ('fork')")
        connection.commit()

    receipt = readiness.collect_readiness(
        checkout_root=checkout,
        runtime_root=runtime,
        db_path=db_path,
        **_aligned_kwargs(checkout, runtime),
    )

    assert receipt.ready is False
    assert "database_revision_not_single" in receipt.blocking_reasons


def test_collect_readiness_fails_closed_when_database_is_missing(tmp_path: Path) -> None:
    checkout = _revision_repo(tmp_path / "checkout", revision=readiness.ACTIVE_HEAD)
    runtime = _revision_repo(tmp_path / "runtime", revision=readiness.ACTIVE_HEAD)

    receipt = readiness.collect_readiness(
        checkout_root=checkout,
        runtime_root=runtime,
        db_path=runtime / "data" / "portfolio.db",
        **_aligned_kwargs(checkout, runtime),
    )

    assert receipt.ready is False
    assert receipt.db_revision is None
    assert receipt.drift_state == "unavailable"
    assert "database_missing" in receipt.blocking_reasons


def test_cli_emits_machine_readable_json_and_blocking_exit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class _BlockedReceipt:
        ready = False

        @staticmethod
        def model_dump_json(*, indent: int) -> str:
            assert indent == 2
            return json.dumps({"ready": False, "blocking_reasons": ["test"]}, indent=2)

    def blocked_collect(**_kwargs: object) -> _BlockedReceipt:
        return _BlockedReceipt()

    monkeypatch.setattr(readiness, "collect_readiness", blocked_collect)

    exit_code = readiness.main(
        [
            "--runtime-root",
            ".",
            "--backup-restore-receipt",
            "receipt.json",
        ]
    )

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out)["blocking_reasons"] == ["test"]
