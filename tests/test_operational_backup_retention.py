"""Synthetic filesystem and sealed-receipt tests; no SQLite connections."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from execution.backup_restore_readiness_receipt import BackupRestoreReadinessReceipt
from src.operations import artifact_retention, operational_backup_retention
from src.operations.artifact_retention import run_retention
from src.operations.kpi_repair_receipts import (
    canonical_sha256,
    seal_attempt,
    seal_disposition_attempt,
)
from src.operations.operational_backup_retention import discover_operational_backups
from src.sqlite_snapshot import SnapshotManifest

NOW = datetime(2026, 10, 8, tzinfo=UTC)
HASH = "a" * 64


@pytest.fixture(autouse=True)
def no_database_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("discovery may not open SQLite")

    monkeypatch.setattr(sqlite3, "connect", forbidden)


def write_json(path: Path, payload: object, *, utf16: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-16" if utf16 else "utf-8")


def backup(
    root: Path,
    name: str,
    *,
    days: int = 20,
    source: Path | None = None,
    state: str = "applied",
    mode: str = "apply",
    logical: str = HASH,
    utf16: bool = False,
    purpose: str = "repair",
) -> tuple[Path, Path, Path, dict[str, object]]:
    source = source or root / "data/portfolio.db"
    path = root / "data/backups" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(name.encode())
    created = NOW - timedelta(days=days)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path = Path(str(path) + ".manifest.json")
    manifest = SnapshotManifest.model_validate(
        {
            "schema_version": "sqlite-reader-snapshot/v1",
            "code_config_version": "sqlite-reader-snapshot/v1",
            "created_at": created,
            "source": {
                "path": str(source),
                "byte_size": 1234,
                "mtime_ns": 123,
                "observed_at": created,
                "alembic_revision": "0032",
            },
            "snapshot": {"path": str(path), "sha256": digest, "byte_size": path.stat().st_size},
            "verification": {"integrity_check": ["ok"], "foreign_key_check": []},
        }
    )
    write_json(manifest_path, manifest.model_dump(mode="json"), utf16=utf16)
    directory = "kpi_repairs" if purpose == "repair" else "kpi_dispositions"
    operations = root / "data/operations" / directory
    readiness = BackupRestoreReadinessReceipt.model_validate(
        {
            "evidence_id": "0" * 64,
            "observed_at": created + timedelta(minutes=1),
            "source_db_requested_path": str(source),
            "source_db_resolved_path": str(source),
            "source_db_revision": "0032",
            "source_db_byte_size": 1234,
            "source_db_mtime_ns": 123,
            "snapshot_requested_path": str(path),
            "snapshot_resolved_path": str(path),
            "snapshot_manifest_resolved_path": str(manifest_path),
            "snapshot_sha256": digest,
            "snapshot_byte_size": path.stat().st_size,
            "restored_db_revision": "0032",
            "integrity_check": ["ok"],
            "foreign_key_violation_count": 0,
            "verifier_code_sha256": HASH,
            "verified": True,
            "blocking_reasons": [],
        }
    )
    readiness = readiness.model_copy(
        update={
            "evidence_id": canonical_sha256(
                readiness.model_dump(mode="json", exclude={"evidence_id"})
            )
        }
    )
    readiness_path = operations / f"{name}-readiness.json"
    write_json(readiness_path, readiness.model_dump(mode="json"), utf16=utf16)
    values: dict[str, object] = {
        "attempt_id": hashlib.md5(name.encode(), usedforsecurity=False).hexdigest(),
        "logical_idempotency_key_sha256": logical,
        "manifest_sha256": HASH,
        "review_bundle_sha256": HASH,
        "backup_restore_evidence_id": readiness.evidence_id,
        "executor_code_sha256": HASH,
        "mode": mode,
        "state": state,
        "started_at": created + timedelta(minutes=2),
        "completed_at": created + timedelta(minutes=3),
        "blocker_codes": ("held",) if state in {"failed", "blocked"} else (),
    }
    if purpose == "repair":
        values.update(
            validated_entries=1,
            inserted_fact_rows=0,
            inserted_context_rows=0,
            result_fact_head_ids=(),
        )
        attempt = seal_attempt(**values)
    else:
        values.update(
            validated_fact_dispositions=1,
            validated_reference_dispositions=0,
            inserted_context_rows=0,
            replayed_context_rows=0,
            inserted_reference_rows=0,
            replayed_reference_rows=0,
        )
        attempt = seal_disposition_attempt(**values)
    attempt_path = operations / "attempts" / f"{attempt.attempt_id}.json"
    payload = attempt.model_dump(mode="json")
    write_json(attempt_path, payload, utf16=utf16)
    return path, readiness_path, attempt_path, payload


def source_env(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(root / "data/portfolio.db"))


def test_closed_backups_share_source_purpose_family_and_keep_latest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_env(tmp_path, monkeypatch)
    older, _, _, _ = backup(tmp_path, "older.db", days=40, logical="b" * 64)
    newer, _, _, _ = backup(tmp_path, "newer.db", days=20, logical="c" * 64)
    found = discover_operational_backups(tmp_path)
    assert len(found.catalog.artifacts) == 2
    assert len({item.family for item in found.catalog.artifacts}) == 1
    assert all(len(item.retirement_proofs) == 3 for item in found.catalog.artifacts)
    assert all(report.family and report.source and report.purpose for report in found.reports)
    decisions = run_retention(tmp_path, now=NOW, catalog=found.catalog).decisions
    assert {row.path: (row.action, row.reason) for row in decisions} == {
        str(older): ("retire", "superseded_verified_backup"),
        str(newer): ("keep", "latest_verified"),
    }


@pytest.mark.parametrize(
    "state,mode", [("passed", "dry_run"), ("failed", "apply"), ("blocked", "apply")]
)
def test_noncompleted_operation_never_admits_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str, mode: str
) -> None:
    source_env(tmp_path, monkeypatch)
    path, _, _, _ = backup(tmp_path, "held.db", state=state, mode=mode)
    found = discover_operational_backups(tmp_path)
    assert not found.catalog.artifacts
    assert found.reports[-1].path == path
    assert found.reports[-1].status != "ready"
    assert path.exists()


def test_later_compatible_completion_self_heals_without_rewriting_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_env(tmp_path, monkeypatch)
    _, _, failed_path, payload = backup(tmp_path, "retry.db", state="failed")
    original = failed_path.read_bytes()
    assert not discover_operational_backups(tmp_path).catalog.artifacts
    payload.pop("content_sha256")
    payload.update(
        attempt_id="f" * 32,
        state="applied",
        blocker_codes=[],
        started_at=NOW - timedelta(days=1, minutes=1),
        completed_at=NOW - timedelta(days=1),
    )
    recovered = seal_attempt(**payload)
    write_json(
        failed_path.parent / f"{recovered.attempt_id}.json", recovered.model_dump(mode="json")
    )
    found = discover_operational_backups(tmp_path)
    assert len(found.catalog.artifacts) == 1
    assert not found.catalog.artifacts[0].pins
    assert failed_path.read_bytes() == original
    assert len(found.catalog.artifacts[0].retirement_proofs) == 4


def test_incompatible_later_success_does_not_clear_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_env(tmp_path, monkeypatch)
    _, _, failed_path, payload = backup(tmp_path, "different-retry.db", state="failed")
    payload.pop("content_sha256")
    payload.update(
        attempt_id="f" * 32,
        state="applied",
        blocker_codes=[],
        logical_idempotency_key_sha256="b" * 64,
        completed_at=NOW - timedelta(days=1),
    )
    recovered = seal_attempt(**payload)
    write_json(
        failed_path.parent / f"{recovered.attempt_id}.json", recovered.model_dump(mode="json")
    )
    found = discover_operational_backups(tmp_path)
    assert not found.catalog.artifacts
    assert found.reports[-1].reason == "unresolved_operation_attempt"


def test_different_source_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source_env(tmp_path, monkeypatch)
    backup(tmp_path, "other.db", source=tmp_path / "other.db")
    found = discover_operational_backups(tmp_path)
    assert not found.catalog.artifacts
    assert any(report.status == "unclassified" for report in found.reports)


def test_utf16_metadata_joins_exact_raw_proofs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_env(tmp_path, monkeypatch)
    backup(tmp_path, "utf16.db", utf16=True)
    found = discover_operational_backups(tmp_path)
    assert len(found.catalog.artifacts) == 1
    assert all(
        proof.sha256 == hashlib.sha256(proof.path.read_bytes()).hexdigest()
        for proof in found.catalog.artifacts[0].retirement_proofs
    )


def test_forged_receipt_keeps_last_good_and_reports_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_env(tmp_path, monkeypatch)
    older, _, _, _ = backup(tmp_path, "old.db", days=40)
    latest, _, attempt, payload = backup(tmp_path, "bad-latest.db", days=10)
    payload["state"] = "failed"
    write_json(attempt, payload)  # The original content hash does not match the edited state.
    found = discover_operational_backups(tmp_path)
    assert [item.path for item in found.catalog.artifacts] == [older]
    assert found.catalog.artifacts[0].pins
    assert any(report.path == older and report.pins for report in found.reports)
    assert all(
        row.action == "keep"
        for row in run_retention(tmp_path, now=NOW, catalog=found.catalog).decisions
    )
    assert latest.exists()


def test_unmanifested_active_backup_preserves_last_good(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_env(tmp_path, monkeypatch)
    backup(tmp_path, "closed.db")
    unknown = tmp_path / "data/backups/active.db"
    unknown.write_bytes(b"unfinished")
    found = discover_operational_backups(tmp_path)
    assert found.catalog.artifacts[0].pins
    assert any(
        report.path == unknown and report.reason == "snapshot_manifest_missing"
        for report in found.reports
    )


def test_missing_snapshot_is_reported_without_blocking_existing_survivor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_env(tmp_path, monkeypatch)
    missing, _, _, _ = backup(tmp_path, "removed.db")
    missing.unlink()
    backup(tmp_path, "survivor.db")
    found = discover_operational_backups(tmp_path)
    assert any(report.path == missing and report.status == "missing" for report in found.reports)
    assert len(found.catalog.artifacts) == 1


def test_unknown_backup_does_not_block_old_proved_closed_retirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_env(tmp_path, monkeypatch)
    older, _, _, _ = backup(tmp_path, "old-closed.db", days=40)
    latest, _, _, _ = backup(tmp_path, "latest-closed.db", days=20)
    unknown = tmp_path / "data/backups/unknown.db"
    unknown.write_bytes(b"unknown retained bytes")
    found = discover_operational_backups(tmp_path)
    decisions = {
        item.path: item.action
        for item in run_retention(tmp_path, now=NOW, catalog=found.catalog).decisions
    }
    assert decisions == {str(older): "retire", str(latest): "keep"}
    assert unknown.exists()
    assert any(
        report.path == unknown and report.status == "unclassified" for report in found.reports
    )


def test_source_configuration_is_required(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EARNINGS_SUMMARY_DB_PATH", raising=False)
    backup(tmp_path, "needs-config.db")
    found = discover_operational_backups(tmp_path)
    assert not found.catalog.artifacts
    assert found.reports[0].reason == "configured_source_missing_or_invalid"


def test_empty_scope_is_healthy_without_source_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EARNINGS_SUMMARY_DB_PATH", raising=False)
    found = discover_operational_backups(tmp_path)
    assert not found.catalog.artifacts and not found.reports


def test_unrelated_migration_remains_held_without_pinning_kpi_family(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_env(tmp_path, monkeypatch)
    backup(tmp_path, "closed.db")
    migration = tmp_path / "data/backups/portfolio_pre_0032_managed_20260828T103732.db"
    migration.write_bytes(b"migration rollback evidence")
    found = discover_operational_backups(tmp_path)
    assert len(found.catalog.artifacts) == 1
    assert not found.catalog.artifacts[0].pins
    assert any(
        report.path == migration and report.reason == "migration_closure_not_supported"
        for report in found.reports
    )


def test_dispositions_have_separate_producer_purpose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_env(tmp_path, monkeypatch)
    backup(tmp_path, "repair.db")
    backup(tmp_path, "disposition.db", purpose="disposition")
    found = discover_operational_backups(tmp_path)
    assert len(found.catalog.artifacts) == 2
    assert len({item.family for item in found.catalog.artifacts}) == 2


@pytest.mark.parametrize(
    "publication", ["changed_latest", "new_failed", "new_malformed", "created_directory"]
)
def test_operation_publication_after_plan_prevents_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, publication: str
) -> None:
    source_env(tmp_path, monkeypatch)
    older, _, attempt_path, payload = backup(tmp_path, "old-proof.db", days=40)
    latest, _, _, _ = backup(tmp_path, "latest-proof.db", days=20)
    latest_record = attempt_path.parent.parent / "latest.json"
    write_json(latest_record, payload)
    found = discover_operational_backups(tmp_path)
    assert len(found.catalog.artifacts) == 2
    assert all(len(item.retirement_evidence_sets) == 4 for item in found.catalog.artifacts)
    before = {
        proof.path: proof.path.read_bytes()
        for item in found.catalog.artifacts
        for proof in item.retirement_proofs
    }
    original_write = cast(
        "Callable[[Path, dict[str, object]], None]", getattr(artifact_retention, "_write_receipt")
    )
    published = False

    def publish_after_plan(path: Path, value: dict[str, object]) -> None:
        nonlocal published
        original_write(path, value)
        if value.get("state") != "planned" or published:
            return
        published = True
        if publication == "changed_latest":
            write_json(latest_record, {"schema_version": "unknown-new-state"})
        elif publication == "new_failed":
            changed = payload.copy()
            changed.pop("content_sha256")
            changed.update(attempt_id="e" * 32, state="failed", blocker_codes=["new-failure"])
            failed = seal_attempt(**changed)
            write_json(
                attempt_path.parent / f"{failed.attempt_id}.json", failed.model_dump(mode="json")
            )
        elif publication == "new_malformed":
            (attempt_path.parent / "new-malformed.json").write_text("{broken")
        else:
            write_json(
                tmp_path / "data/operations/kpi_dispositions/attempts/new.json",
                {"new": "unclassified"},
            )

    monkeypatch.setattr(artifact_retention, "_write_receipt", publish_after_plan)
    result = run_retention(tmp_path, now=NOW, apply=True, catalog=found.catalog)
    assert published
    assert result.deleted == 0 and result.errors == 1
    assert older.exists() and latest.exists()
    assert all(proof.read_bytes() == raw for proof, raw in before.items())
    assert any(row.reason == "retirement_evidence_set_changed" for row in result.decisions)


def test_operation_publication_during_discovery_yields_no_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_env(tmp_path, monkeypatch)
    backup(tmp_path, "closed.db")
    original_read = cast(
        "Callable[[Path], tuple[dict[str, object], object]]",
        getattr(operational_backup_retention, "_metadata"),
    )
    published = False

    def publish_during_read(path: Path) -> tuple[dict[str, object], object]:
        nonlocal published
        result = original_read(path)
        if not published:
            published = True
            write_json(
                tmp_path / "data/operations/kpi_repairs/new-unknown.json", {"new": "unknown"}
            )
        return result

    monkeypatch.setattr(operational_backup_retention, "_metadata", publish_during_read)
    found = discover_operational_backups(tmp_path)
    assert published and not found.catalog.artifacts
    assert found.reports[-1].reason == "operation_evidence_set_unavailable_or_changed"
    assert all(report.status != "ready" for report in found.reports)
