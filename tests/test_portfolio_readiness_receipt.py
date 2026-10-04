from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

import pytest

import sqlite_snapshot
from execution import backup_restore_readiness_receipt as backup_receipt
from execution import portfolio_readiness_receipt as readiness
from sqlite_runtime import SQLiteConnectionRole
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
    assert receipt.source_db_file_token is not None
    assert receipt.restored_db_revision == readiness.ACTIVE_HEAD
    assert receipt.snapshot_sha256 == _sha(tmp_path / "snapshot.db")
    assert receipt.verifier_code_sha256 == backup_receipt.verifier_code_sha256()
    assert backup_receipt.evidence_id_is_valid(receipt)
    assert receipt.authorizes_downstream_write is False
    assert receipt.downstream_locked_revalidation_required is True


@pytest.mark.parametrize("hard_link", [False, True])
def test_backup_collector_rejects_alias_before_reading_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hard_link: bool
) -> None:
    source = _versioned_db(tmp_path / "source.db", revision=readiness.ACTIVE_HEAD)
    snapshot = tmp_path / "alias.db" if hard_link else source
    if hard_link:
        os.link(source, snapshot)

    def forbidden_read(_path: Path) -> str:
        pytest.fail("an aliased database must not be read or hashed")

    monkeypatch.setattr(backup_receipt, "_sha256", forbidden_read)
    monkeypatch.setattr(backup_receipt, "_revision_and_verification", forbidden_read)
    monkeypatch.setattr(backup_receipt, "_revision", forbidden_read, raising=False)
    receipt = backup_receipt.collect_backup_restore_receipt(
        source_db=source, snapshot_db=snapshot, manifest_path=tmp_path / "missing.json"
    )
    assert receipt.verified is False
    assert receipt.blocking_reasons == ("backup_restore_snapshot_source_alias",)
    assert receipt.source_db_file_token is None
    assert backup_receipt.evidence_id_is_valid(receipt)


@pytest.mark.parametrize("corruption", ["integrity", "foreign_key"])
def test_backup_collector_fresh_candidate_still_rejects_source_corruption(
    tmp_path: Path, corruption: str
) -> None:
    source = _versioned_db(tmp_path / "source.db", revision=readiness.ACTIVE_HEAD)
    with closing(sqlite3.connect(source)) as writer:
        writer.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        writer.execute("CREATE TABLE child (parent_id INTEGER REFERENCES parent(id))")
        writer.execute("CREATE TABLE payload (value TEXT)")
        writer.execute("INSERT INTO payload VALUES (?)", ("x" * 10000,))
        writer.commit()
        root_page = writer.execute(
            "SELECT rootpage FROM sqlite_master WHERE name = 'payload'"
        ).fetchone()[0]
        page_size = writer.execute("PRAGMA page_size").fetchone()[0]
    snapshot = tmp_path / "snapshot.db"
    create_snapshot(SnapshotRequest(source_path=source, destination_path=snapshot))
    original_stat = source.stat()
    if corruption == "integrity":
        with source.open("r+b") as stream:
            stream.seek((root_page - 1) * page_size)
            stream.write(b"\xff")
    else:
        with closing(sqlite3.connect(source)) as writer:
            writer.execute("INSERT INTO child VALUES (999)")
            writer.commit()
    os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    # The singular revision remains readable. Metadata cannot establish the
    # source's integrity or foreign-key validity; the fresh candidate must.
    with closing(sqlite3.connect(source)) as reader:
        assert reader.execute("SELECT version_num FROM alembic_version").fetchone() == (
            readiness.ACTIVE_HEAD,
        )
    receipt = backup_receipt.collect_backup_restore_receipt(source_db=source, snapshot_db=snapshot)
    assert receipt.verified is False
    assert "source_identity_changed_since_snapshot" in receipt.blocking_reasons
    assert receipt.integrity_check == ("ok",)
    assert receipt.foreign_key_violation_count == 0


def test_backup_collector_requires_exact_text_source_revision(tmp_path: Path) -> None:
    source = _versioned_db(tmp_path / "source.db", revision=readiness.ACTIVE_HEAD)
    snapshot = tmp_path / "snapshot.db"
    create_snapshot(SnapshotRequest(source_path=source, destination_path=snapshot))
    with closing(sqlite3.connect(source)) as writer:
        writer.execute("UPDATE alembic_version SET version_num = ?", (b"not-text",))
        writer.commit()
    receipt = backup_receipt.collect_backup_restore_receipt(source_db=source, snapshot_db=snapshot)
    assert receipt.verified is False
    assert receipt.source_db_revision is None
    assert "source_database_unreadable" in receipt.blocking_reasons


def test_backup_collector_rejects_artifact_changed_after_fresh_comparison(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _versioned_db(tmp_path / "source.db", revision=readiness.ACTIVE_HEAD)
    snapshot = tmp_path / "snapshot.db"
    create_snapshot(SnapshotRequest(source_path=source, destination_path=snapshot))
    verify = backup_receipt.verify_snapshot_matches_source

    def verify_then_change(
        request: SnapshotRequest, *, manifest_path: Path | None = None
    ) -> sqlite_snapshot.SnapshotResult:
        result = verify(request, manifest_path=manifest_path)
        with snapshot.open("ab") as stream:
            stream.write(b"changed after fresh comparison")
        return result

    monkeypatch.setattr(backup_receipt, "verify_snapshot_matches_source", verify_then_change)
    receipt = backup_receipt.collect_backup_restore_receipt(source_db=source, snapshot_db=snapshot)
    assert receipt.verified is False
    assert "backup_restore_snapshot_identity_mismatch" in receipt.blocking_reasons
    assert backup_receipt.evidence_id_is_valid(receipt)


def test_backup_collector_rejects_invalid_commitment_before_static_artifact_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _versioned_db(tmp_path / "source.db", revision=readiness.ACTIVE_HEAD)
    snapshot = tmp_path / "snapshot.db"
    create_snapshot(SnapshotRequest(source_path=source, destination_path=snapshot))
    commitment: Callable[[backup_receipt.BackupRestoreReadinessReceipt], str] = getattr(
        backup_receipt, "_evidence_id"
    )
    calls = 0

    def broken_first_seal(receipt: backup_receipt.BackupRestoreReadinessReceipt) -> str:
        nonlocal calls
        calls += 1
        return "0" * 64 if calls == 1 else commitment(receipt)

    monkeypatch.setattr(backup_receipt, "_evidence_id", broken_first_seal)
    receipt = backup_receipt.collect_backup_restore_receipt(source_db=source, snapshot_db=snapshot)
    assert receipt.verified is False
    assert "backup_restore_evidence_id_invalid" in receipt.blocking_reasons
    assert backup_receipt.evidence_id_is_valid(receipt)


def test_backup_collector_performs_one_fresh_proof_without_source_full_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _versioned_db(tmp_path / "source.db", revision=readiness.ACTIVE_HEAD)
    snapshot = tmp_path / "snapshot.db"
    checked: list[Path] = []
    verified: list[Path] = []
    backed_up: list[Path] = []
    collector_checks: Callable[[Path], tuple[str, tuple[str, ...], int]] = getattr(
        backup_receipt, "_revision_and_verification"
    )
    snapshot_checks: Callable[[Path], sqlite_snapshot.SnapshotVerification] = getattr(
        sqlite_snapshot, "_verify"
    )
    backup: Callable[[sqlite3.Connection, Path, sqlite_snapshot.SnapshotLogger | None], None] = (
        getattr(sqlite_snapshot, "_backup")
    )

    def collect_checks(path: Path) -> tuple[str, tuple[str, ...], int]:
        checked.append(path)
        return collector_checks(path)

    def full_checks(path: Path) -> sqlite_snapshot.SnapshotVerification:
        verified.append(path)
        return snapshot_checks(path)

    def backup_copy(
        connection: sqlite3.Connection, path: Path, logger: sqlite_snapshot.SnapshotLogger | None
    ) -> None:
        backed_up.append(path)
        backup(connection, path, logger)

    monkeypatch.setattr(backup_receipt, "_revision_and_verification", collect_checks)
    monkeypatch.setattr(sqlite_snapshot, "_verify", full_checks)
    monkeypatch.setattr(sqlite_snapshot, "_backup", backup_copy)
    create_snapshot(SnapshotRequest(source_path=source, destination_path=snapshot))
    receipt = backup_receipt.collect_backup_restore_receipt(source_db=source, snapshot_db=snapshot)
    assert receipt.verified is True
    assert checked == [snapshot.resolve()]
    # Creation verifies once. Fresh replay verifies both the artifact and its
    # new source-derived candidate. The collector also verifies the artifact.
    assert len(verified) == 3
    assert len(checked) + len(verified) == 4
    assert len(backed_up) == 2


def test_backup_collector_applies_static_receipt_guard_before_returning_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _versioned_db(tmp_path / "source.db", revision=readiness.ACTIVE_HEAD)
    snapshot = tmp_path / "snapshot.db"
    create_snapshot(SnapshotRequest(source_path=source, destination_path=snapshot))

    def static_guard(
        receipt: backup_receipt.BackupRestoreReadinessReceipt,
        *,
        source_db: Path,
        source_revision: str | None,
        require_current_identity: bool = True,
    ) -> tuple[str, ...]:
        assert backup_receipt.evidence_id_is_valid(receipt)
        assert source_db == source.resolve()
        assert source_revision == readiness.ACTIVE_HEAD
        assert require_current_identity is False
        return ("backup_restore_snapshot_identity_mismatch",)

    monkeypatch.setattr(backup_receipt, "validate_receipt_for_source", static_guard)
    receipt = backup_receipt.collect_backup_restore_receipt(source_db=source, snapshot_db=snapshot)
    assert receipt.verified is False
    assert receipt.blocking_reasons == ("backup_restore_snapshot_identity_mismatch",)
    assert backup_receipt.evidence_id_is_valid(receipt)


def test_backup_collector_rejects_verifier_change_during_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _versioned_db(tmp_path / "source.db", revision=readiness.ACTIVE_HEAD)
    snapshot = tmp_path / "snapshot.db"
    create_snapshot(SnapshotRequest(source_path=source, destination_path=snapshot))
    hashes = iter(("a" * 64, "b" * 64))
    monkeypatch.setattr(backup_receipt, "verifier_code_sha256", lambda: next(hashes))
    receipt = backup_receipt.collect_backup_restore_receipt(source_db=source, snapshot_db=snapshot)
    assert receipt.verified is False
    assert "backup_restore_verifier_code_changed" in receipt.blocking_reasons
    assert backup_receipt.evidence_id_is_valid(receipt)


def test_backup_collector_fences_wal_commit_during_static_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _versioned_db(tmp_path / "source.db", revision=readiness.ACTIVE_HEAD)
    writer = sqlite3.connect(source)
    try:
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("CREATE TABLE facts (value TEXT NOT NULL)")
        writer.execute("INSERT INTO facts VALUES ('old')")
        writer.commit()
        snapshot = tmp_path / "snapshot.db"
        create_snapshot(SnapshotRequest(source_path=source, destination_path=snapshot))
        validate = backup_receipt.validate_receipt_for_source
        main_identity = (source.stat().st_size, source.stat().st_mtime_ns)

        def static_guard(
            receipt: backup_receipt.BackupRestoreReadinessReceipt,
            *,
            source_db: Path,
            source_revision: str | None,
            require_current_identity: bool = True,
        ) -> tuple[str, ...]:
            reasons = validate(
                receipt,
                source_db=source_db,
                source_revision=source_revision,
                require_current_identity=require_current_identity,
            )
            writer.execute("UPDATE facts SET value = 'new'")
            writer.commit()
            assert (source.stat().st_size, source.stat().st_mtime_ns) == main_identity
            return reasons

        monkeypatch.setattr(backup_receipt, "validate_receipt_for_source", static_guard)
        receipt = backup_receipt.collect_backup_restore_receipt(
            source_db=source, snapshot_db=snapshot
        )
        assert receipt.verified is False
        assert "source_identity_changed_during_verification" in receipt.blocking_reasons
        assert backup_receipt.evidence_id_is_valid(receipt)
    finally:
        writer.close()


def test_backup_restore_receipt_rejects_a_later_wal_only_commit(tmp_path: Path) -> None:
    source = _versioned_db(tmp_path / "source.db", revision=readiness.ACTIVE_HEAD)
    writer = sqlite3.connect(source)
    try:
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("CREATE TABLE facts (value TEXT NOT NULL)")
        writer.execute("INSERT INTO facts(value) VALUES ('old')")
        writer.commit()

        snapshot = tmp_path / "snapshot.db"
        create_snapshot(SnapshotRequest(source_path=source, destination_path=snapshot))
        receipt = backup_receipt.collect_backup_restore_receipt(
            source_db=source,
            snapshot_db=snapshot,
        )
        assert receipt.verified is True
        main_identity = (source.stat().st_size, source.stat().st_mtime_ns)

        writer.execute("UPDATE facts SET value = 'new'")
        writer.commit()
        assert (source.stat().st_size, source.stat().st_mtime_ns) == main_identity
        assert source.with_name(source.name + "-wal").is_file()

        reasons = backup_receipt.validate_receipt_for_source(
            receipt,
            source_db=source,
            source_revision=readiness.ACTIVE_HEAD,
            require_current_identity=True,
        )
        assert "backup_restore_source_identity_stale" in reasons
        assert "backup_restore_source_content_stale" in reasons
    finally:
        writer.close()


def test_migration_readiness_accepts_reader_owned_empty_wal_after_receipt(
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

    Path(f"{db_path}-wal").touch()
    receipt = readiness.collect_readiness(
        checkout_root=checkout,
        runtime_root=runtime,
        db_path=db_path,
        backup_restore_receipt_path=restore_receipt,
        mode="migration",
        **_aligned_kwargs(checkout, runtime),
    )

    assert receipt.db_revision == prior_revision
    assert receipt.drift_state == "db_behind_code"
    assert receipt.ready is True
    assert receipt.blocking_reasons == ()


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


def test_migration_readiness_probes_restored_snapshot_before_source_revalidation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    restored_snapshot = tmp_path / "backup" / "snapshot.db"
    original_connect_sqlite = readiness.connect_sqlite
    probed_paths: list[Path] = []

    def record_probe(
        path: str | Path,
        *,
        role: SQLiteConnectionRole,
        schema_preflight: bool | None = None,
    ) -> sqlite3.Connection:
        resolved = Path(path).resolve()
        probed_paths.append(resolved)
        if resolved == db_path.resolve():
            stat = db_path.stat()
            os.utime(db_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        return original_connect_sqlite(
            path,
            role=role,
            schema_preflight=schema_preflight,
        )

    monkeypatch.setattr(readiness, "connect_sqlite", record_probe)

    receipt = readiness.collect_readiness(
        checkout_root=checkout,
        runtime_root=runtime,
        db_path=db_path,
        backup_restore_receipt_path=restore_receipt,
        mode="migration",
        **_aligned_kwargs(checkout, runtime),
    )

    assert probed_paths == [restored_snapshot.resolve()]
    assert receipt.db_revision == prior_revision
    assert receipt.drift_state == "db_behind_code"
    assert receipt.ready is True
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


@pytest.mark.parametrize(
    ("receipt_payload", "reason"),
    (
        (None, "backup_restore_receipt_required"),
        ("{not-json", "backup_restore_receipt_invalid"),
    ),
)
def test_migration_mode_does_not_probe_live_source_without_valid_restore_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    receipt_payload: str | None,
    reason: str,
) -> None:
    checkout = _revision_repo(tmp_path / "checkout", revision=readiness.ACTIVE_HEAD)
    runtime = _revision_repo(tmp_path / "runtime", revision=readiness.ACTIVE_HEAD)
    db_path = _versioned_db(runtime / "data" / "portfolio.db", revision=readiness.ACTIVE_HEAD)
    receipt_path = tmp_path / "backup-restore-receipt.json"
    if receipt_payload is not None:
        receipt_path.write_text(receipt_payload, encoding="utf-8")

    def reject_source_probe(
        _path: str | Path,
        *,
        role: SQLiteConnectionRole,
        schema_preflight: bool | None = None,
    ) -> sqlite3.Connection:
        del role, schema_preflight
        raise AssertionError("migration readiness must not open the live source without a receipt")

    monkeypatch.setattr(readiness, "connect_sqlite", reject_source_probe)
    receipt = readiness.collect_readiness(
        checkout_root=checkout,
        runtime_root=runtime,
        db_path=db_path,
        backup_restore_receipt_path=(receipt_path if receipt_payload is not None else None),
        mode="migration",
        **_aligned_kwargs(checkout, runtime),
    )

    assert receipt.db_revision is None
    assert receipt.drift_state == "unavailable"
    assert receipt.ready is False
    assert reason in receipt.blocking_reasons


def test_migration_mode_does_not_probe_schema_valid_receipt_with_rewritten_snapshot_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = _revision_repo(tmp_path / "checkout", revision=readiness.ACTIVE_HEAD)
    runtime = _revision_repo(tmp_path / "runtime", revision=readiness.ACTIVE_HEAD)
    db_path = _versioned_db(runtime / "data" / "portfolio.db", revision=readiness.ACTIVE_HEAD)
    receipt_path = _backup_receipt(db_path, tmp_path / "backup")
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload["snapshot_resolved_path"] = str(db_path.resolve())
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")

    def reject_database_probe(
        _path: str | Path,
        *,
        role: SQLiteConnectionRole,
        schema_preflight: bool | None = None,
    ) -> sqlite3.Connection:
        del role, schema_preflight
        raise AssertionError("provenance-invalid receipt must not open a SQLite database")

    monkeypatch.setattr(readiness, "connect_sqlite", reject_database_probe)
    receipt = readiness.collect_readiness(
        checkout_root=checkout,
        runtime_root=runtime,
        db_path=db_path,
        backup_restore_receipt_path=receipt_path,
        mode="migration",
        **_aligned_kwargs(checkout, runtime),
    )

    assert receipt.db_revision is None
    assert receipt.drift_state == "unavailable"
    assert receipt.ready is False
    assert "backup_restore_evidence_id_invalid" in receipt.blocking_reasons


def test_migration_mode_does_not_probe_receipt_whose_snapshot_aliases_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = _revision_repo(tmp_path / "checkout", revision=readiness.ACTIVE_HEAD)
    runtime = _revision_repo(tmp_path / "runtime", revision=readiness.ACTIVE_HEAD)
    db_path = _versioned_db(runtime / "data" / "portfolio.db", revision=readiness.ACTIVE_HEAD)
    receipt_path = _backup_receipt(db_path, tmp_path / "backup")
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload["snapshot_resolved_path"] = str(db_path.resolve())
    payload["snapshot_requested_path"] = str(db_path)
    payload["snapshot_byte_size"] = db_path.stat().st_size
    payload["snapshot_sha256"] = _sha(db_path)
    payload["evidence_id"] = "0" * 64
    draft = backup_receipt.BackupRestoreReadinessReceipt.model_validate(payload)
    canonical = json.dumps(
        draft.model_dump(mode="json", exclude={"evidence_id"}),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    payload["evidence_id"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")

    def reject_database_probe(
        _path: str | Path,
        *,
        role: SQLiteConnectionRole,
        schema_preflight: bool | None = None,
    ) -> sqlite3.Connection:
        del role, schema_preflight
        raise AssertionError("source-aliased snapshot must not open a SQLite database")

    monkeypatch.setattr(readiness, "connect_sqlite", reject_database_probe)
    receipt = readiness.collect_readiness(
        checkout_root=checkout,
        runtime_root=runtime,
        db_path=db_path,
        backup_restore_receipt_path=receipt_path,
        mode="migration",
        **_aligned_kwargs(checkout, runtime),
    )

    assert receipt.db_revision is None
    assert receipt.drift_state == "unavailable"
    assert receipt.ready is False
    assert "backup_restore_snapshot_source_alias" in receipt.blocking_reasons


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
