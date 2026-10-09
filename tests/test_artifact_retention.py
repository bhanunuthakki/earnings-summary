"""Recovery and boundary tests for explicit filesystem artifact retirement."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from src.operations import artifact_retention
from src.operations.artifact_retention import (
    CATALOG_RELATIVE_PATH,
    Artifact,
    ArtifactCatalog,
    run_retention,
)

NOW = datetime(2026, 10, 8, tzinfo=UTC)


def artifact(
    root: Path,
    name: str,
    *,
    days: int = 31,
    family: str = "db",
    kind: str = "backup",
    **changes: object,
) -> Artifact:
    path = root / ".tmp" / "closed-rehearsal" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(name.encode())
    payload: dict[str, object] = {
        "path": path,
        "allowed_root": path.parent,
        "family": family,
        "created_at": NOW - timedelta(days=days),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size": path.stat().st_size,
        "kind": kind,
        "status": "completed",
        "verified": True,
        "pins": [],
        **changes,
    }
    return Artifact.model_validate(payload)


def catalog(*items: Artifact) -> ArtifactCatalog:
    return ArtifactCatalog(schema_version=1, artifacts=list(items))


@pytest.mark.parametrize("change_survivor", [False, True])
def test_backup_closure_proof_change_prevents_retirement(
    tmp_path: Path, change_survivor: bool
) -> None:
    proof = tmp_path / ".tmp/closed-rehearsal/closure.json"
    older = artifact(tmp_path, "old-proof.db", days=40)
    latest = artifact(tmp_path, "latest-proof.db", days=20)
    proof.write_text('{"status":"completed"}')
    bound = (latest if change_survivor else older).model_dump()
    bound["retirement_proofs"] = [
        {"path": proof, "sha256": hashlib.sha256(proof.read_bytes()).hexdigest()}
    ]
    protected = Artifact.model_validate(bound)
    if change_survivor:
        latest = protected
    else:
        older = protected
    proof.write_text('{"status":"failed"}')

    result = run_retention(tmp_path, now=NOW, apply=True, catalog=catalog(older, latest))

    assert result.deleted == 0
    assert older.path.exists() and latest.path.exists()
    assert any(item.reason == "retirement_proof_changed" for item in result.decisions)


def test_backup_closure_proof_is_rechecked_after_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    older = artifact(tmp_path, "old-race.db", days=40)
    latest = artifact(tmp_path, "latest-race.db", days=20)
    proof = older.allowed_root / "closure.json"
    proof.write_bytes(b"closed")
    payload = older.model_dump()
    payload["retirement_proofs"] = [
        {"path": proof, "sha256": hashlib.sha256(proof.read_bytes()).hexdigest()}
    ]
    older = Artifact.model_validate(payload)
    writer: Callable[[Path, dict[str, object]], None] = getattr(
        artifact_retention, "_write_receipt"
    )

    def change_proof(path: Path, receipt: dict[str, object]) -> None:
        writer(path, receipt)
        proof.write_bytes(b"recovery required")

    monkeypatch.setattr(artifact_retention, "_write_receipt", change_proof)
    result = run_retention(tmp_path, now=NOW, apply=True, catalog=catalog(older, latest))

    assert result.deleted == 0 and result.errors == 1
    assert result.decisions[0].reason == "retirement_proof_changed"
    assert older.path.exists() and latest.path.exists()


def test_missing_closure_proof_is_a_hold_not_a_missing_backup(tmp_path: Path) -> None:
    older = artifact(tmp_path, "old-missing-proof.db", days=40)
    latest = artifact(tmp_path, "latest-missing-proof.db", days=20)
    payload = older.model_dump()
    payload["retirement_proofs"] = [
        {"path": older.allowed_root / "missing-closure.json", "sha256": "0" * 64}
    ]
    older = Artifact.model_validate(payload)

    result = run_retention(tmp_path, now=NOW, apply=True, catalog=catalog(older, latest))

    assert result.deleted == 0
    assert result.decisions[0].action == "keep"
    assert result.decisions[0].reason == "retirement_proof_unavailable"
    assert older.path.exists() and latest.path.exists()


@pytest.mark.parametrize("hold", ["pin", "failed", "unverified"])
def test_operator_catalog_cannot_remove_current_producer_hold(tmp_path: Path, hold: str) -> None:
    older = artifact(tmp_path, "operator-old.db", days=40)
    latest = artifact(tmp_path, "operator-latest.db", days=20)
    registered = tmp_path / CATALOG_RELATIVE_PATH
    registered.parent.mkdir(parents=True)
    original = catalog(older).model_dump_json()
    registered.write_text(original)
    updates: dict[str, object] = (
        {"pins": ["current failure"]}
        if hold == "pin"
        else {"status": "failed"}
        if hold == "failed"
        else {"verified": False}
    )
    producer = older.model_copy(update=updates)

    result = run_retention(tmp_path, now=NOW, apply=True, catalog=catalog(producer, latest))

    assert result.deleted == 0
    assert older.path.exists() and latest.path.exists()
    assert result.decisions[0].reason == "pinned"
    assert registered.read_text() == original


def test_newest_failed_registration_cannot_replace_last_good_survivor(tmp_path: Path) -> None:
    good = artifact(tmp_path, "last-good.db", days=40)
    failed = artifact(tmp_path, "newest-failed.db", days=20)
    registered = tmp_path / CATALOG_RELATIVE_PATH
    registered.parent.mkdir(parents=True)
    registered.write_text(catalog(good, failed).model_dump_json())
    producer = failed.model_copy(update={"status": "failed", "verified": False})

    result = run_retention(tmp_path, now=NOW, apply=True, catalog=catalog(producer))

    assert result.deleted == 0
    assert good.path.exists() and failed.path.exists()
    assert any(
        item.path == str(good.path) and item.reason == "latest_verified"
        for item in result.decisions
    )


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        ("active", "unfinished_lifecycle_manifest"),
        ("failed", "unfinished_lifecycle_manifest"),
        ("pinned", "pinned_lifecycle_manifest"),
        ("malformed", "unverified_lifecycle_manifest"),
        ("completed", "completed_test"),
    ],
)
def test_legacy_catalog_cannot_bypass_outer_run_ownership(
    tmp_path: Path, state: str, reason: str
) -> None:
    item = artifact(tmp_path, "inner/fixture.bin", kind="disposable_test")
    outer = item.allowed_root.parent
    manifest = outer / ".earnings-temp-run.json"
    payload = {
        "schema_version": "earnings-temp-run/v1",
        "owner": "earnings-summary",
        "run_id": "outer-session",
        "run_root": str(outer),
        "started_at": (NOW - timedelta(days=40)).isoformat(),
        "completed_at": (NOW - timedelta(days=31)).isoformat(),
        "status": state if state in {"active", "failed"} else "completed",
        "pins": ["restore pending"] if state == "pinned" else [],
        "files": [],
    }
    manifest.write_text("{" if state == "malformed" else json.dumps(payload), encoding="utf-8")
    (item.allowed_root / "state.json").write_text('{"status":"completed"}', encoding="utf-8")

    result = run_retention(tmp_path, now=NOW, apply=True, catalog=catalog(item))

    assert result.decisions[0].reason == reason
    assert item.path.exists() == (state != "completed")
    assert result.deleted == (1 if state == "completed" else 0)
    assert manifest.exists()


def test_latest_verified_per_family_and_failure_pins_survive(tmp_path: Path) -> None:
    older = artifact(tmp_path, "old.db", days=40)
    latest = artifact(tmp_path, "latest.db", days=20)
    failed = artifact(tmp_path, "failed.db", days=1, status="failed")
    active = artifact(tmp_path, "active.db", days=0, status="active")
    unknown = artifact(tmp_path, "unknown.db", days=0, verified=False)
    pinned = artifact(tmp_path, "pinned.db", days=50, pins=["restore pending"])
    other = artifact(tmp_path, "tracker.db", family="tracker")
    plan = run_retention(
        tmp_path, now=NOW, catalog=catalog(older, latest, failed, active, unknown, pinned, other)
    )
    assert plan.would_delete == 1
    assert all(
        item.path.exists() for item in (older, latest, failed, active, unknown, pinned, other)
    )
    applied = run_retention(
        tmp_path,
        now=NOW,
        apply=True,
        catalog=catalog(older, latest, failed, active, unknown, pinned, other),
    )
    assert applied.deleted == 1
    assert not older.path.exists()
    assert all(item.path.exists() for item in (latest, failed, active, unknown, pinned, other))
    assert applied.receipt_path is not None
    receipt = json.loads(Path(applied.receipt_path).read_text())
    assert receipt["state"] == "completed"
    assert receipt["result"]["catalog_sha256"] == applied.catalog_sha256
    repeated = run_retention(tmp_path, now=NOW, apply=True, catalog=catalog(older, latest, failed))
    assert repeated.deleted == 0


def test_only_verified_completed_test_files_older_than_7_days_retire(tmp_path: Path) -> None:
    old = artifact(tmp_path, "old.bin", kind="disposable_test")
    fresh = artifact(tmp_path, "fresh.bin", days=7, kind="disposable_test")
    failed = artifact(tmp_path, "failed.bin", kind="disposable_test", status="failed")
    unknown = artifact(tmp_path, "unknown.bin", kind="disposable_test", verified=False)
    result = run_retention(
        tmp_path, now=NOW, apply=True, catalog=catalog(old, fresh, failed, unknown)
    )
    assert result.deleted == 1
    assert not old.path.exists()
    assert all(item.path.exists() for item in (fresh, failed, unknown))


def test_changed_latest_cannot_retire_last_good_backup(tmp_path: Path) -> None:
    old = artifact(tmp_path, "old.db", days=40)
    new = artifact(tmp_path, "new.db", days=1)
    new.path.write_bytes(b"broken")
    result = run_retention(tmp_path, now=NOW, apply=True, catalog=catalog(old, new))
    assert result.deleted == 0
    assert old.path.exists()
    assert {item.reason for item in result.decisions} == {"latest_verified", "digest_changed"}


@pytest.mark.parametrize("protection", ["live", "sidecar", "archive", "scope_escape", "link"])
def test_protected_targets_cannot_be_retired(tmp_path: Path, protection: str) -> None:
    item = artifact(
        tmp_path,
        "portfolio_gc_archive.db" if protection == "archive" else "fixture.db",
        kind="disposable_test",
    )
    live_db = item.path if protection == "live" else None
    if protection == "sidecar":
        Path(str(item.path) + "-wal").write_bytes(b"pending")
    elif protection == "scope_escape":
        item = item.model_copy(update={"allowed_root": tmp_path / ".tmp" / "other"})
    elif protection == "link":
        target = tmp_path / "original.db"
        item.path.rename(target)
        try:
            item.path.symlink_to(target)
        except OSError:
            pytest.skip("symlink creation unavailable")
    result = run_retention(tmp_path, now=NOW, apply=True, live_db=live_db, catalog=catalog(item))
    assert result.deleted == 0
    assert item.path.exists()


def test_receipt_is_durable_before_unlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = artifact(tmp_path, "fixture.db", kind="disposable_test")
    original = Path.unlink
    observed: list[str] = []

    def inspect_unlink(path: Path, missing_ok: bool = False) -> None:
        if path == item.path:
            receipts = list(
                (tmp_path / "data/operations/artifact-retention-receipts").glob("*.json")
            )
            assert len(receipts) == 1
            receipt = json.loads(receipts[0].read_text())
            assert receipt["state"] == "planned"
            assert receipt["result"]["decisions"][0]["sha256"] == item.sha256
            observed.append(str(path))
        original(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", inspect_unlink)
    result = run_retention(tmp_path, now=NOW, apply=True, catalog=catalog(item))
    assert result.deleted == 1
    assert observed == [str(item.path)]


def test_unavailable_or_malformed_catalog_fails_closed(tmp_path: Path) -> None:
    assert run_retention(tmp_path, now=NOW).deleted == 0
    path = tmp_path / CATALOG_RELATIVE_PATH
    path.parent.mkdir(parents=True)
    path.write_text('{"schema_version":1,"artifacts":[{"path":"../escape"}]}')
    with pytest.raises(ValueError):
        run_retention(tmp_path, now=NOW, apply=True)


def test_failed_unlink_keeps_a_failed_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = artifact(tmp_path, "fixture.db", kind="disposable_test")
    original = Path.unlink

    def fail_unlink(path: Path, missing_ok: bool = False) -> None:
        if path == item.path:
            raise PermissionError("in use")
        original(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    result = run_retention(tmp_path, now=NOW, apply=True, catalog=catalog(item))
    assert result.errors == 1
    assert item.path.exists()
    assert result.receipt_path is not None
    assert json.loads(Path(result.receipt_path).read_text())["state"] == "failed"


def test_completed_inner_scope_preserves_failed_outer_checkpoint(tmp_path: Path) -> None:
    item = artifact(tmp_path, "fixture.db", kind="disposable_test")
    (tmp_path / ".tmp/state.json").write_text('{"status":"failed"}')
    (item.allowed_root / "state.json").write_text('{"status":"completed"}')
    result = run_retention(tmp_path, now=NOW, apply=True, catalog=catalog(item))
    assert result.deleted == 0
    assert item.path.exists()
    assert result.decisions[0].reason == "unfinished_checkpoint"


def test_nonzero_wal_is_not_a_disposable_test_file(tmp_path: Path) -> None:
    item = artifact(tmp_path, "fixture.db-wal", kind="disposable_test")
    result = run_retention(tmp_path, now=NOW, apply=True, catalog=catalog(item))
    assert result.deleted == 0
    assert result.decisions[0].reason == "nonzero_database_wal"


def test_live_database_sidecars_are_protected(tmp_path: Path) -> None:
    item = artifact(tmp_path, "fixture.db-shm", kind="disposable_test")
    result = run_retention(
        tmp_path,
        now=NOW,
        apply=True,
        live_db=item.path.with_name("fixture.db"),
        catalog=catalog(item),
    )
    assert result.deleted == 0
    assert result.decisions[0].reason == "live_database"


def test_explicit_zero_day_override_retires_recent_verified_fixture(tmp_path: Path) -> None:
    item = artifact(tmp_path, "fixture.db", days=1, kind="disposable_test")
    result = run_retention(
        tmp_path, now=NOW, apply=True, catalog=catalog(item), test_retention_days=0
    )
    assert result.deleted == 1
    assert not item.path.exists()


def test_windows_readonly_fixture_is_preserved_without_changing_attributes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    from types import SimpleNamespace

    item = artifact(tmp_path, "fixture.bin", kind="disposable_test")
    original_lstat = Path.lstat

    def windows_lstat(path: Path) -> os.stat_result | SimpleNamespace:
        metadata = original_lstat(path)
        if path == item.path:
            return SimpleNamespace(
                st_mode=metadata.st_mode,
                st_nlink=metadata.st_nlink,
                st_size=metadata.st_size,
                st_ino=metadata.st_ino,
                st_mtime_ns=metadata.st_mtime_ns,
                st_file_attributes=33,
            )
        return metadata

    monkeypatch.setattr(Path, "lstat", windows_lstat)
    result = run_retention(tmp_path, now=NOW, apply=True, catalog=catalog(item))
    assert result.deleted == 0
    assert result.errors == 0
    assert result.decisions[0].reason == "readonly_attribute"
    assert item.path.exists()
    # Removing ReadOnly is a separate approved operator action. Retirement
    # itself neither changes attributes nor suppresses genuine unlink failures.
    monkeypatch.undo()
    retried = run_retention(tmp_path, now=NOW, apply=True, catalog=catalog(item))
    assert retried.deleted == 1


@pytest.mark.parametrize("change", ["pin", "failed", "remove", "malformed", "new_catalog"])
def test_catalog_changes_after_plan_preserve_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    item = artifact(tmp_path, "fixture.bin", kind="disposable_test")
    path = tmp_path / CATALOG_RELATIVE_PATH
    path.parent.mkdir(parents=True)
    original_catalog = catalog(item)
    if change != "new_catalog":
        path.write_text(original_catalog.model_dump_json(), encoding="utf-8")
    writer = getattr(artifact_retention, "_write_receipt")
    assert callable(writer)
    original_write = cast("Callable[[Path, dict[str, object]], None]", writer)
    changed = False

    def mutate_after_plan(receipt: Path, payload: dict[str, object]) -> None:
        nonlocal changed
        original_write(receipt, payload)
        if changed:
            return
        changed = True
        if change == "remove":
            path.unlink()
        elif change == "malformed":
            path.write_text("{", encoding="utf-8")
        else:
            held = item.model_copy(
                update={"status": "failed"} if change == "failed" else {"pins": ["restore pending"]}
            )
            path.write_text(catalog(held).model_dump_json(), encoding="utf-8")

    monkeypatch.setattr(artifact_retention, "_write_receipt", mutate_after_plan)
    result = run_retention(tmp_path, now=NOW, apply=True, catalog=original_catalog)
    assert item.path.exists()
    assert result.deleted == 0
    assert result.errors == 1
    assert result.decisions[0].reason in {"catalog_changed", "catalog_unavailable"}
    assert result.receipt_path is not None
    assert json.loads(Path(result.receipt_path).read_text())["state"] == "failed"


def test_state_catalog_pin_overrides_runtime_and_producer_registration(tmp_path: Path) -> None:
    item = artifact(tmp_path, "fixture.bin", kind="disposable_test")
    runtime = tmp_path / "runtime"
    for root, declaration in (
        (runtime, catalog(item)),
        (tmp_path, catalog(item.model_copy(update={"pins": ["state recovery hold"]}))),
    ):
        path = root / CATALOG_RELATIVE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(declaration.model_dump_json(), encoding="utf-8")
    result = run_retention(
        tmp_path,
        now=NOW,
        apply=True,
        catalog=catalog(item),
        catalog_roots=(runtime, tmp_path),
    )
    assert result.deleted == 0
    assert result.decisions[0].reason == "pinned"
    assert item.path.exists()


@pytest.mark.parametrize("change", ["active", "failed", "new_checkpoint"])
def test_checkpoint_change_during_target_hash_preserves_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    item = artifact(tmp_path, "fixture.bin", kind="disposable_test")
    checkpoint = item.allowed_root / "state.json"
    if change != "new_checkpoint":
        checkpoint.write_text('{"status": "completed"}', encoding="utf-8")
    hash_function = getattr(artifact_retention, "_hash")
    assert callable(hash_function)
    original_hash = cast("Callable[[Path], str]", hash_function)

    def mutate_during_hash(path: Path) -> str:
        digest = original_hash(path)
        if path == item.path:
            checkpoint.write_text(
                json.dumps({"status": "active" if change == "new_checkpoint" else change}),
                encoding="utf-8",
            )
        return digest

    monkeypatch.setattr(artifact_retention, "_hash", mutate_during_hash)
    result = run_retention(tmp_path, now=NOW, apply=True, catalog=catalog(item))
    assert result.deleted == 0
    assert item.path.exists()
    assert result.decisions[0].reason == "checkpoint_changed"


@pytest.mark.parametrize("linked_component", ["data", "data/operations"])
@pytest.mark.parametrize("catalog_exists", [False, True])
def test_catalog_rejects_linked_ancestor_before_descendant_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    linked_component: str,
    catalog_exists: bool,
) -> None:
    import os
    import stat
    from types import SimpleNamespace

    path = tmp_path / CATALOG_RELATIVE_PATH
    path.parent.mkdir(parents=True)
    if catalog_exists:
        path.write_text(catalog().model_dump_json(), encoding="utf-8")
    linked = tmp_path / linked_component
    original_lstat = Path.lstat
    original_exists = Path.exists

    def linked_lstat(current: Path) -> os.stat_result | SimpleNamespace:
        if current == linked:
            return SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0x400)
        return original_lstat(current)

    def no_descendant_probe(current: Path) -> bool:
        if linked in current.parents:
            raise AssertionError("catalog descendant was probed through linked ancestor")
        return original_exists(current)

    monkeypatch.setattr(Path, "lstat", linked_lstat)
    monkeypatch.setattr(Path, "exists", no_descendant_probe)
    with pytest.raises(ValueError, match="catalog may not traverse links"):
        artifact_retention.load_catalog(tmp_path)
