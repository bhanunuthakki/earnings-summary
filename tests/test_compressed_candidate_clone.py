from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

import execution.prepare_compressed_latest_state_clone as clone_cli
import provenance.compressed_candidate_clone as clone_module
from provenance.compressed_candidate_clone import (
    CompressedCloneRequest,
    prepare_compressed_clone,
    verify_compressed_clone_receipt,
)
from provenance.latest_state_activation import (
    LatestStateActivationError,
    audit_candidate_coverage,
    audit_governed_candidate,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _audited_source(tmp_path: Path) -> tuple[Path, Path, Path]:
    database = tmp_path / "source.db"
    with sqlite3.connect(database) as conn:
        conn.executescript(
            """
            CREATE TABLE alembic_version (version_num TEXT PRIMARY KEY);
            INSERT INTO alembic_version VALUES ('0259_source_definition_identity');
            CREATE TABLE source_fact_publications (publication_id TEXT PRIMARY KEY);
            INSERT INTO source_fact_publications VALUES ('publication-1');
            """
        )
    seal = tmp_path / "source-seal.json"
    seal.write_text(
        json.dumps(
            {
                "canonical_bindings": 0,
                "database": str(database.resolve()),
                "foreign_key_violations": 0,
                "quick_check": "ok",
                "revision": ["0259_source_definition_identity"],
                "sha256": _sha256(database),
                "size_bytes": database.stat().st_size,
                "source_taxonomy_components": 0,
            }
        ),
        encoding="utf-8",
    )
    report = audit_governed_candidate(
        database,
        seal_path=seal,
        expected_revision="0259_source_definition_identity",
    )
    audit_receipt = tmp_path / "candidate-audit.json"
    audit_receipt.write_text(report.model_dump_json(), encoding="utf-8")
    coverage = audit_candidate_coverage(
        database,
        candidate_audit_receipt=audit_receipt,
    )
    coverage_receipt = tmp_path / "candidate-coverage.json"
    coverage_receipt.write_text(coverage.model_dump_json(), encoding="utf-8")
    return database, audit_receipt, coverage_receipt


def _mock_compression(monkeypatch: pytest.MonkeyPatch) -> None:
    def enable(path: Path) -> None:
        del path

    def compressed(path: Path) -> bool:
        del path
        return True

    def compressed_size(path: Path) -> int:
        return path.stat().st_size

    def free_bytes(path: Path) -> int:
        del path
        return 10_000_000_000

    monkeypatch.setattr(clone_module, "_require_ntfs_host", lambda: None)
    monkeypatch.setattr(clone_module, "_enable_directory_compression", enable)
    monkeypatch.setattr(clone_module, "_directory_is_compressed", compressed)
    monkeypatch.setattr(clone_module, "_file_is_compressed", compressed)
    monkeypatch.setattr(clone_module, "_compressed_size", compressed_size)
    monkeypatch.setattr(clone_module, "_free_bytes", free_bytes)


def test_compressed_clone_is_receipt_bound_hash_exact_and_no_clobber(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, audit_receipt, coverage_receipt = _audited_source(tmp_path)
    _mock_compression(monkeypatch)
    destination = tmp_path / "clone" / "candidate.db"
    request = CompressedCloneRequest(
        source_database=source,
        candidate_audit_receipt=audit_receipt,
        candidate_coverage_receipt=coverage_receipt,
        destination_database=destination,
        operation_recorded_at=datetime(2026, 7, 31, tzinfo=UTC),
        minimum_free_bytes=clone_module.MINIMUM_SAFE_FREE_BYTES,
    )

    receipt = prepare_compressed_clone(request)

    assert destination.read_bytes() == source.read_bytes()
    assert receipt.destination_database_sha256 == _sha256(source)
    assert receipt.candidate_audit_file_sha256 == _sha256(audit_receipt)
    assert receipt.candidate_audit_identity_before == receipt.candidate_audit_identity_after
    assert receipt.candidate_coverage_identity_before == receipt.candidate_coverage_identity_after
    assert receipt.source_identity_before == receipt.source_identity_after
    assert verify_compressed_clone_receipt(receipt)
    assert list(destination.parent.glob(".*.tmp")) == []
    with pytest.raises(LatestStateActivationError, match="already exists"):
        prepare_compressed_clone(request)


def test_compressed_clone_refuses_low_headroom_or_tampered_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, audit_receipt, coverage_receipt = _audited_source(tmp_path)
    _mock_compression(monkeypatch)
    destination = tmp_path / "clone" / "candidate.db"
    request = CompressedCloneRequest(
        source_database=source,
        candidate_audit_receipt=audit_receipt,
        candidate_coverage_receipt=coverage_receipt,
        destination_database=destination,
        operation_recorded_at=datetime(2026, 7, 31, tzinfo=UTC),
        minimum_free_bytes=clone_module.MINIMUM_SAFE_FREE_BYTES,
    )

    def low_free_bytes(path: Path) -> int:
        del path
        return 100

    monkeypatch.setattr(clone_module, "_free_bytes", low_free_bytes)
    with pytest.raises(LatestStateActivationError, match="headroom"):
        prepare_compressed_clone(request)
    assert not destination.exists()

    payload = json.loads(audit_receipt.read_text(encoding="utf-8"))
    payload["report_sha256"] = "0" * 64
    audit_receipt.write_text(json.dumps(payload), encoding="utf-8")

    def enough_free_bytes(path: Path) -> int:
        del path
        return 1_000_000_000

    monkeypatch.setattr(clone_module, "_free_bytes", enough_free_bytes)
    with pytest.raises(LatestStateActivationError, match="commitment"):
        prepare_compressed_clone(request)
    assert not destination.exists()


def test_compressed_clone_refuses_wal_created_after_candidate_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, audit_receipt, coverage_receipt = _audited_source(tmp_path)
    _mock_compression(monkeypatch)
    Path(f"{source}-wal").write_bytes(b"late-writer")
    destination = tmp_path / "clone" / "candidate.db"

    with pytest.raises(LatestStateActivationError, match="non-empty WAL"):
        prepare_compressed_clone(
            CompressedCloneRequest(
                source_database=source,
                candidate_audit_receipt=audit_receipt,
                candidate_coverage_receipt=coverage_receipt,
                destination_database=destination,
                operation_recorded_at=datetime(2026, 7, 31, tzinfo=UTC),
                minimum_free_bytes=clone_module.MINIMUM_SAFE_FREE_BYTES,
            )
        )

    assert not destination.exists()


def test_compressed_clone_refuses_source_change_at_publication_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, audit_receipt, coverage_receipt = _audited_source(tmp_path)
    _mock_compression(monkeypatch)
    destination = tmp_path / "clone" / "candidate.db"
    checkpoint_calls = 0
    original_checkpoint = clone_module.require_checkpointed_sidecars

    def mutate_before_publication(path: Path) -> None:
        nonlocal checkpoint_calls
        checkpoint_calls += 1
        original_checkpoint(path)
        if checkpoint_calls == 3:
            with path.open("r+b") as handle:
                handle.seek(0)
                first = handle.read(1)
                handle.seek(0)
                handle.write(bytes([first[0] ^ 1]))

    monkeypatch.setattr(
        clone_module,
        "require_checkpointed_sidecars",
        mutate_before_publication,
    )

    with pytest.raises(LatestStateActivationError, match=r"before.*publication"):
        prepare_compressed_clone(
            CompressedCloneRequest(
                source_database=source,
                candidate_audit_receipt=audit_receipt,
                candidate_coverage_receipt=coverage_receipt,
                destination_database=destination,
                operation_recorded_at=datetime(2026, 7, 31, tzinfo=UTC),
                minimum_free_bytes=clone_module.MINIMUM_SAFE_FREE_BYTES,
            )
        )

    assert not destination.exists()


def test_compressed_clone_refuses_admission_receipt_change_during_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, audit_receipt, coverage_receipt = _audited_source(tmp_path)
    _mock_compression(monkeypatch)
    destination = tmp_path / "clone" / "candidate.db"
    checkpoint_calls = 0
    original_checkpoint = clone_module.require_checkpointed_sidecars

    def mutate_receipt_before_publication(path: Path) -> None:
        nonlocal checkpoint_calls
        checkpoint_calls += 1
        original_checkpoint(path)
        if checkpoint_calls == 3:
            audit_receipt.write_text(
                audit_receipt.read_text(encoding="utf-8") + " ",
                encoding="utf-8",
            )

    monkeypatch.setattr(
        clone_module,
        "require_checkpointed_sidecars",
        mutate_receipt_before_publication,
    )

    with pytest.raises(LatestStateActivationError, match="admission receipt changed"):
        prepare_compressed_clone(
            CompressedCloneRequest(
                source_database=source,
                candidate_audit_receipt=audit_receipt,
                candidate_coverage_receipt=coverage_receipt,
                destination_database=destination,
                operation_recorded_at=datetime(2026, 7, 31, tzinfo=UTC),
                minimum_free_bytes=clone_module.MINIMUM_SAFE_FREE_BYTES,
            )
        )

    assert not destination.exists()
    assert not destination.parent.exists()


@pytest.mark.parametrize("failure_point", ("compressed_size", "free_after"))
def test_compressed_clone_cleans_staging_without_publishing_on_late_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    source, audit_receipt, coverage_receipt = _audited_source(tmp_path)
    _mock_compression(monkeypatch)
    destination = tmp_path / "clone" / "candidate.db"
    if failure_point == "compressed_size":

        def fail_compressed_size(path: Path) -> int:
            del path
            raise OSError("injected")

        monkeypatch.setattr(
            clone_module,
            "_compressed_size",
            fail_compressed_size,
        )
    else:
        free_calls = 0

        def fail_after_copy(path: Path) -> int:
            nonlocal free_calls
            free_calls += 1
            if free_calls >= 3:
                raise OSError("injected")
            return 10_000_000_000

        monkeypatch.setattr(clone_module, "_free_bytes", fail_after_copy)

    with pytest.raises(LatestStateActivationError, match="filesystem operation"):
        prepare_compressed_clone(
            CompressedCloneRequest(
                source_database=source,
                candidate_audit_receipt=audit_receipt,
                candidate_coverage_receipt=coverage_receipt,
                destination_database=destination,
                operation_recorded_at=datetime(2026, 7, 31, tzinfo=UTC),
                minimum_free_bytes=clone_module.MINIMUM_SAFE_FREE_BYTES,
            )
        )

    assert not destination.exists()
    assert not destination.parent.exists()
    assert list(destination.parent.glob(".*.tmp")) == []


def test_compressed_clone_unlinks_published_alias_when_identity_check_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, audit_receipt, coverage_receipt = _audited_source(tmp_path)
    _mock_compression(monkeypatch)
    destination = tmp_path / "clone" / "candidate.db"

    def not_same_file(
        left: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        right: str | bytes | os.PathLike[str] | os.PathLike[bytes],
    ) -> bool:
        del left, right
        return False

    monkeypatch.setattr(clone_module.os.path, "samefile", not_same_file)

    with pytest.raises(LatestStateActivationError, match="identity differs"):
        prepare_compressed_clone(
            CompressedCloneRequest(
                source_database=source,
                candidate_audit_receipt=audit_receipt,
                candidate_coverage_receipt=coverage_receipt,
                destination_database=destination,
                operation_recorded_at=datetime(2026, 7, 31, tzinfo=UTC),
                minimum_free_bytes=clone_module.MINIMUM_SAFE_FREE_BYTES,
            )
        )

    assert not destination.exists()
    assert not destination.parent.exists()


def test_clone_cli_refuses_receipt_at_source_sidecar(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    audit = tmp_path / "audit.json"
    destination = tmp_path / "clone.db"
    args = clone_cli.build_parser().parse_args(
        [
            "--source-database",
            str(source),
            "--candidate-audit-receipt",
            str(audit),
            "--candidate-coverage-receipt",
            str(tmp_path / "coverage.json"),
            "--destination-database",
            str(destination),
            "--operation-recorded-at",
            "2026-07-31T00:00:00Z",
            "--minimum-free-bytes",
            str(clone_cli.MINIMUM_SAFE_FREE_BYTES),
            "--receipt",
            f"{source}-wal",
        ]
    )

    with pytest.raises(LatestStateActivationError, match="protected artifact"):
        clone_cli.receipt_path_for(args)


@pytest.mark.parametrize("publication_error", (LatestStateActivationError, OSError))
def test_clone_cli_removes_owned_clone_when_receipt_publication_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    publication_error: type[Exception],
) -> None:
    source, audit_receipt, coverage_receipt = _audited_source(tmp_path)
    _mock_compression(monkeypatch)
    destination = tmp_path / "clone" / "candidate.db"
    receipt = tmp_path / "clone-receipt.json"

    def fail_receipt(path: Path, payload: str) -> bool:
        del path, payload
        raise publication_error("injected receipt failure")

    monkeypatch.setattr(
        clone_cli,
        "publish_text_no_clobber",
        fail_receipt,
    )

    result = clone_cli.main(
        [
            "--source-database",
            str(source),
            "--candidate-audit-receipt",
            str(audit_receipt),
            "--candidate-coverage-receipt",
            str(coverage_receipt),
            "--destination-database",
            str(destination),
            "--operation-recorded-at",
            "2026-07-31T00:00:00Z",
            "--minimum-free-bytes",
            str(clone_cli.MINIMUM_SAFE_FREE_BYTES),
            "--receipt",
            str(receipt),
        ]
    )

    assert result == 2
    assert not destination.exists()
    assert not receipt.exists()


def test_clone_cli_refuses_headroom_floor_below_five_gib() -> None:
    with pytest.raises(SystemExit):
        clone_cli.build_parser().parse_args(
            [
                "--source-database",
                "source.db",
                "--candidate-audit-receipt",
                "audit.json",
                "--candidate-coverage-receipt",
                "coverage.json",
                "--destination-database",
                "destination.db",
                "--operation-recorded-at",
                "2026-07-31T00:00:00Z",
                "--minimum-free-bytes",
                "1",
                "--receipt",
                "receipt.json",
            ]
        )


def test_clone_request_refuses_headroom_floor_below_five_gib(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="greater than or equal"):
        CompressedCloneRequest(
            source_database=tmp_path / "source.db",
            candidate_audit_receipt=tmp_path / "audit.json",
            candidate_coverage_receipt=tmp_path / "coverage.json",
            destination_database=tmp_path / "destination.db",
            operation_recorded_at=datetime(2026, 7, 31, tzinfo=UTC),
            minimum_free_bytes=1,
        )


@pytest.mark.skipif(os.name != "nt", reason="NTFS compression is Windows-specific")
def test_compressed_clone_uses_real_ntfs_compression(tmp_path: Path) -> None:
    source, audit_receipt, coverage_receipt = _audited_source(tmp_path)
    destination = tmp_path / "compressed" / "candidate.db"

    receipt = prepare_compressed_clone(
        CompressedCloneRequest(
            source_database=source,
            candidate_audit_receipt=audit_receipt,
            candidate_coverage_receipt=coverage_receipt,
            destination_database=destination,
            operation_recorded_at=datetime(2026, 7, 31, tzinfo=UTC),
            minimum_free_bytes=clone_module.MINIMUM_SAFE_FREE_BYTES,
        )
    )

    assert destination.read_bytes() == source.read_bytes()
    assert receipt.compressed_size_bytes <= receipt.logical_size_bytes
