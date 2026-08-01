# pyright: reportPrivateUsage=false
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

import provenance.cutover_preflight as cutover
from provenance.compressed_candidate_clone import (
    MINIMUM_SAFE_FREE_BYTES,
    CompressedCloneReceipt,
)
from provenance.cutover_preflight import (
    CheckoutIdentity,
    CutoverPreflightError,
    ExistingCloneUpgradeRequest,
    MigrationFileDigest,
    MigrationPlan,
    upgrade_existing_isolated_clone,
    verify_existing_clone_upgrade_receipt,
)
from provenance.latest_state_activation import candidate_file_identity

SOURCE_REVISION = "0261_latest_governed_state"
TARGET_REVISION = "0269_latest_governed_population_receipt_v2"
NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _clone_receipt(path: Path, database: Path) -> CompressedCloneReceipt:
    identity = candidate_file_identity(database)
    fields: dict[str, object] = {
        "source_database": str((path.parent / "source.db").resolve()),
        "source_database_sha256": _sha(database),
        "source_identity_before": identity.model_dump(mode="json"),
        "source_identity_after": identity.model_dump(mode="json"),
        "candidate_audit_receipt": str((path.parent / "audit.json").resolve()),
        "candidate_audit_report_sha256": "a" * 64,
        "candidate_audit_file_sha256": "b" * 64,
        "candidate_audit_identity_before": identity.model_dump(mode="json"),
        "candidate_audit_identity_after": identity.model_dump(mode="json"),
        "candidate_coverage_receipt": str((path.parent / "coverage.json").resolve()),
        "candidate_coverage_report_sha256": "c" * 64,
        "candidate_coverage_file_sha256": "d" * 64,
        "candidate_coverage_identity_before": identity.model_dump(mode="json"),
        "candidate_coverage_identity_after": identity.model_dump(mode="json"),
        "destination_database": str(database.resolve()),
        "destination_database_sha256": _sha(database),
        "logical_size_bytes": database.stat().st_size,
        "compressed_size_bytes": database.stat().st_size,
        "free_bytes_before": 10_000_000_000,
        "free_bytes_after": 9_000_000_000,
        "minimum_free_bytes": MINIMUM_SAFE_FREE_BYTES,
        "operation_recorded_at": NOW,
        "schema_version": "latest-governed-compressed-clone/v1",
    }
    draft = CompressedCloneReceipt.model_validate(fields | {"receipt_sha256": "0" * 64})
    payload = draft.model_dump(mode="json", exclude={"receipt_sha256"})
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    receipt = draft.model_copy(update={"receipt_sha256": digest})
    path.write_text(receipt.model_dump_json(), encoding="utf-8")
    return receipt


def _request(tmp_path: Path, database: Path, receipt: Path) -> ExistingCloneUpgradeRequest:
    return ExistingCloneUpgradeRequest(
        repo_root=tmp_path,
        database_path=database,
        compressed_clone_receipt=receipt,
        receipt_path=tmp_path / "upgrade.json",
        expected_source_revision=SOURCE_REVISION,
        expected_target_revision=TARGET_REVISION,
        operation_recorded_at=NOW,
        minimum_free_bytes=MINIMUM_SAFE_FREE_BYTES,
    )


def _patch_environment(
    monkeypatch: pytest.MonkeyPatch,
    *,
    introduces_runtime_identity: bool = False,
) -> None:
    checkout = CheckoutIdentity(commit_sha="a" * 40, clean=True)
    migration_files = (
        (
            MigrationFileDigest(
                ordinal=1,
                revision="0264_document_processing_operation_ledger",
                down_revisions=(SOURCE_REVISION,),
                relative_path="alembic/versions/0264_document_processing_operation_ledger.py",
                sha256="d" * 64,
            ),
        )
        if introduces_runtime_identity
        else ()
    )
    plan = MigrationPlan(
        expected_alembic_head=TARGET_REVISION,
        ordered_migration_files=migration_files,
    )

    def checkout_identity(_root: Path) -> CheckoutIdentity:
        return checkout

    def migration_plan(_root: Path) -> MigrationPlan:
        return plan

    def require_apply(
        _root: Path,
        _checkout: CheckoutIdentity,
        _plan: MigrationPlan,
    ) -> None:
        return None

    def compression_metrics(path: Path) -> tuple[bool, int]:
        return True, path.stat().st_size

    def available_free_bytes(_path: Path) -> int:
        return 10_000_000_000

    monkeypatch.setattr(cutover, "_checkout_identity", checkout_identity)
    monkeypatch.setattr(cutover, "_migration_plan", migration_plan)
    monkeypatch.setattr(cutover, "_require_apply_checkout", require_apply)
    monkeypatch.setattr(cutover, "compressed_file_metrics", compression_metrics)
    monkeypatch.setattr(cutover, "_available_free_bytes", available_free_bytes)

    def upgrade(*, repo_root: Path, destination_path: Path, expected_head: str) -> None:
        del repo_root
        assert expected_head == TARGET_REVISION
        with sqlite3.connect(destination_path) as conn:
            conn.execute("UPDATE alembic_version SET version_num=?", (expected_head,))
            if introduces_runtime_identity:
                conn.executescript(
                    "CREATE TABLE database_runtime_identity ("
                    "singleton INTEGER PRIMARY KEY,database_instance_id TEXT NOT NULL);"
                    "INSERT INTO database_runtime_identity VALUES "
                    "(1,'database-instance:11111111111111111111111111111111');"
                )
        conn.close()

    monkeypatch.setattr(cutover, "_upgrade_clone", upgrade)


def test_existing_compressed_clone_upgrade_is_exact_receipted_and_single_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.db"
    with sqlite3.connect(database) as conn:
        conn.executescript(
            "CREATE TABLE alembic_version (version_num TEXT PRIMARY KEY);"
            f"INSERT INTO alembic_version VALUES ('{SOURCE_REVISION}');"
        )
    conn.close()
    clone_path = tmp_path / "compressed-clone.json"
    clone = _clone_receipt(clone_path, database)
    _patch_environment(monkeypatch)

    receipt = upgrade_existing_isolated_clone(_request(tmp_path, database, clone_path))

    assert receipt.database_before.alembic_revision == SOURCE_REVISION
    assert receipt.database_after.alembic_revision == TARGET_REVISION
    assert receipt.compressed_clone_receipt_sha256 == clone.receipt_sha256
    assert receipt.compressed_before and receipt.compressed_after
    assert receipt.sqlite_before.clean and receipt.sqlite_after.clean
    assert verify_existing_clone_upgrade_receipt(receipt)
    assert Path(receipt.upgrade_intent_path).is_file()
    assert (tmp_path / "upgrade.json").is_file()
    assert clone_path.read_text(encoding="utf-8") == clone.model_dump_json()


def test_existing_clone_upgrade_refuses_database_drift_from_clone_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.db"
    with sqlite3.connect(database) as conn:
        conn.executescript(
            "CREATE TABLE alembic_version (version_num TEXT PRIMARY KEY);"
            f"INSERT INTO alembic_version VALUES ('{SOURCE_REVISION}');"
        )
    clone_path = tmp_path / "compressed-clone.json"
    _clone_receipt(clone_path, database)
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE drifted (id INTEGER PRIMARY KEY)")
    _patch_environment(monkeypatch)

    with pytest.raises(CutoverPreflightError, match="differs"):
        upgrade_existing_isolated_clone(_request(tmp_path, database, clone_path))


def test_existing_clone_upgrade_exact_replay_returns_the_immutable_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.db"
    with sqlite3.connect(database) as conn:
        conn.executescript(
            "CREATE TABLE alembic_version (version_num TEXT PRIMARY KEY);"
            f"INSERT INTO alembic_version VALUES ('{SOURCE_REVISION}');"
        )
    clone_path = tmp_path / "compressed-clone.json"
    _clone_receipt(clone_path, database)
    _patch_environment(monkeypatch)
    request = _request(tmp_path, database, clone_path)

    first = upgrade_existing_isolated_clone(request)
    second = upgrade_existing_isolated_clone(request)

    assert second == first
    assert verify_existing_clone_upgrade_receipt(second)


def test_existing_clone_upgrade_recovers_after_migration_before_receipt_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.db"
    with sqlite3.connect(database) as conn:
        conn.executescript(
            "CREATE TABLE alembic_version (version_num TEXT PRIMARY KEY);"
            f"INSERT INTO alembic_version VALUES ('{SOURCE_REVISION}');"
        )
    clone_path = tmp_path / "compressed-clone.json"
    _clone_receipt(clone_path, database)
    _patch_environment(monkeypatch, introduces_runtime_identity=True)
    request = _request(tmp_path, database, clone_path)
    publish = cutover.publish_text_no_clobber
    failed = False

    def fail_final_receipt(path: Path, payload: str) -> bool:
        nonlocal failed
        if path == request.receipt_path and not failed:
            failed = True
            raise OSError("injected final receipt failure")
        return publish(path, payload)

    monkeypatch.setattr(cutover, "publish_text_no_clobber", fail_final_receipt)
    with pytest.raises(OSError, match="injected"):
        upgrade_existing_isolated_clone(request)
    assert not request.receipt_path.exists()
    assert Path(f"{request.receipt_path}.intent.json").is_file()
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            TARGET_REVISION,
        )

    receipt = upgrade_existing_isolated_clone(request)

    assert request.receipt_path.is_file()
    assert receipt.database_after.alembic_revision == TARGET_REVISION
    assert verify_existing_clone_upgrade_receipt(receipt)


def test_existing_clone_upgrade_recovery_refuses_replacement_target_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.db"
    with sqlite3.connect(database) as conn:
        conn.executescript(
            "CREATE TABLE alembic_version (version_num TEXT PRIMARY KEY);"
            f"INSERT INTO alembic_version VALUES ('{SOURCE_REVISION}');"
        )
    conn.close()
    clone_path = tmp_path / "compressed-clone.json"
    _clone_receipt(clone_path, database)
    _patch_environment(monkeypatch, introduces_runtime_identity=True)
    request = _request(tmp_path, database, clone_path)
    publish = cutover.publish_text_no_clobber
    failed = False

    def fail_final_receipt(path: Path, payload: str) -> bool:
        nonlocal failed
        if path == request.receipt_path and not failed:
            failed = True
            raise OSError("injected final receipt failure")
        return publish(path, payload)

    monkeypatch.setattr(cutover, "publish_text_no_clobber", fail_final_receipt)
    with pytest.raises(OSError, match="injected"):
        upgrade_existing_isolated_clone(request)
    database.unlink()
    with sqlite3.connect(database) as conn:
        conn.executescript(
            "CREATE TABLE alembic_version (version_num TEXT PRIMARY KEY);"
            f"INSERT INTO alembic_version VALUES ('{TARGET_REVISION}');"
        )

    with pytest.raises(CutoverPreflightError, match="replacement database"):
        upgrade_existing_isolated_clone(request)


def test_existing_clone_upgrade_recovery_requires_durable_database_identity() -> None:
    plan = MigrationPlan(
        expected_alembic_head=TARGET_REVISION,
        ordered_migration_files=(),
    )

    assert not cutover._recovery_runtime_identity_is_valid(
        source_identity=None,
        recovered_identity=None,
        migration_plan=plan,
    )


def test_existing_clone_upgrade_intermediate_revision_requires_clone_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.db"
    with sqlite3.connect(database) as conn:
        conn.executescript(
            "CREATE TABLE alembic_version (version_num TEXT PRIMARY KEY);"
            f"INSERT INTO alembic_version VALUES ('{SOURCE_REVISION}');"
        )
    clone_path = tmp_path / "compressed-clone.json"
    _clone_receipt(clone_path, database)
    _patch_environment(monkeypatch)

    def partial_upgrade(*, repo_root: Path, destination_path: Path, expected_head: str) -> None:
        del repo_root, expected_head
        with sqlite3.connect(destination_path) as conn:
            conn.execute("UPDATE alembic_version SET version_num='0265_intermediate'")
        raise RuntimeError("injected mid-chain migration failure")

    monkeypatch.setattr(cutover, "_upgrade_clone", partial_upgrade)
    request = _request(tmp_path, database, clone_path)
    with pytest.raises(RuntimeError, match="mid-chain"):
        upgrade_existing_isolated_clone(request)

    with pytest.raises(CutoverPreflightError, match="restore the admitted clone"):
        upgrade_existing_isolated_clone(request)
