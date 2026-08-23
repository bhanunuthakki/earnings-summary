# pyright: reportPrivateUsage=false
"""Adversarial regressions for the managed staging/publish trust boundary."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path, PureWindowsPath
from typing import cast

import pytest

import pipeline.managed_ir_sources as managed_ir_sources
from models.documents import DocType
from models.ir_uploads import CategorizationResult, Confidence
from pipeline import issuer_document_inventory
from pipeline.issuer_document_inventory import (
    ExpectedIssuerDocument,
    IssuerDocumentInventoryReceipt,
    IssuerDocumentInventoryRequest,
)
from pipeline.managed_ir_sources import (
    IssuerDocumentStagingReceipt,
    IssuerDocumentStagingRequest,
    PreparedIssuerDocumentPublisherError,
    StagedIssuerDocument,
    classification_evidence,
    classifier_code_identity,
    publish_prepared_issuer_documents,
    validate_prepared_staging,
    verifier_code_identity,
)
from runtime.job_runtime import JobLock


def _sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def _request() -> IssuerDocumentStagingRequest:
    inventory = IssuerDocumentInventoryRequest(
        ticker="MELI",
        fiscal_year=2026,
        fiscal_quarter=2,
        period_end=date(2026, 6, 30),
        expected_documents=(
            ExpectedIssuerDocument(
                source_url="https://issuer.test/q2.pdf", document_type="ir_presentation"
            ),
        ),
    )
    return IssuerDocumentStagingRequest(
        attempt_id="attempt-0001",
        inventory_request=inventory,
        inventory_request_sha256=inventory.request_sha256,
    )


def _outcome() -> CategorizationResult:
    return CategorizationResult(
        ticker="MELI",
        doc_type=DocType.IR_PRESENTATION,
        period_end=date(2026, 6, 30),
        period_label="Q2 2026",
        confidence=Confidence.HIGH,
        ticker_evidence=["issuer"],
        doc_type_evidence=["slides"],
        period_evidence=["q2"],
    )


def _press_outcome() -> CategorizationResult:
    return CategorizationResult(
        ticker="MELI",
        doc_type=DocType.IR_PRESS_RELEASE,
        period_end=date(2026, 6, 30),
        period_label="Q2 2026",
        confidence=Confidence.HIGH,
        ticker_evidence=["issuer"],
        doc_type_evidence=["release"],
        period_evidence=["q2"],
    )


def _two_request() -> IssuerDocumentStagingRequest:
    inventory = IssuerDocumentInventoryRequest(
        ticker="MELI",
        fiscal_year=2026,
        fiscal_quarter=2,
        period_end=date(2026, 6, 30),
        expected_documents=(
            ExpectedIssuerDocument(
                source_url="https://issuer.test/q2.pdf", document_type="ir_presentation"
            ),
            ExpectedIssuerDocument(
                source_url="https://issuer.test/q2b.pdf", document_type="ir_press_release"
            ),
        ),
    )
    return IssuerDocumentStagingRequest(
        attempt_id="attempt-0002",
        inventory_request=inventory,
        inventory_request_sha256=inventory.request_sha256,
    )


def _two_receipt(root: Path, request: IssuerDocumentStagingRequest) -> IssuerDocumentStagingReceipt:
    staging = root / ".tmp" / "managed_ir_staging" / request.attempt_id
    staging.joinpath("objects").mkdir(parents=True)
    contents = (("q2.pdf", b"presentation", _outcome()), ("q2b.pdf", b"release", _press_outcome()))
    documents: list[StagedIssuerDocument] = []
    for name, payload, outcome in contents:
        (staging / "objects" / name).write_bytes(payload)
        documents.append(
            StagedIssuerDocument(
                source_url=f"https://issuer.test/{name}",
                document_type=outcome.doc_type.value,
                object_path=f"objects/{name}",
                sha256=hashlib.sha256(payload).hexdigest(),
                byte_size=len(payload),
                fetched_at=datetime(2026, 8, 22, tzinfo=UTC),
                media_type="application/pdf",
                ticker="MELI",
                period_end="2026-06-30",
                classification_confidence="high",
                classification_evidence_sha256=classification_evidence(outcome),
            )
        )
    unsigned = {
        "schema_version": "issuer_document_staging_receipt.v1",
        "request": request.model_dump(mode="json"),
        "documents": [item.model_dump(mode="json") for item in documents],
        "classifier_code_sha256": classifier_code_identity(),
        "verifier_code_sha256": verifier_code_identity(),
        "canonical_mutations": False,
    }
    receipt = IssuerDocumentStagingReceipt.model_validate(
        {**unsigned, "receipt_sha256": _sha(unsigned)}
    )
    (staging / "staging_receipt.json").write_text(receipt.canonical_json + "\n", encoding="utf-8")
    return receipt


def _no_policy(_request: IssuerDocumentStagingRequest, _db_path: Path) -> None:
    return None


def _classify(
    _path: Path,
    *,
    ticker_hint: str | None = None,
    calendar_override: str | None = None,
) -> CategorizationResult:
    del ticker_hint, calendar_override
    return _outcome()


def _receipt(root: Path, request: IssuerDocumentStagingRequest) -> IssuerDocumentStagingReceipt:
    source = root / ".tmp" / "managed_ir_staging" / request.attempt_id / "objects" / "q2.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"presentation")
    outcome = _outcome()
    doc = StagedIssuerDocument(
        source_url="https://issuer.test/q2.pdf",
        document_type="ir_presentation",
        object_path="objects/q2.pdf",
        sha256=hashlib.sha256(b"presentation").hexdigest(),
        byte_size=12,
        fetched_at=datetime(2026, 8, 22, tzinfo=UTC),
        media_type="application/pdf",
        ticker="MELI",
        period_end="2026-06-30",
        classification_confidence=outcome.confidence.value,
        classification_evidence_sha256=classification_evidence(outcome),
    )
    unsigned = {
        "schema_version": "issuer_document_staging_receipt.v1",
        "request": request.model_dump(mode="json"),
        "documents": [doc.model_dump(mode="json")],
        "classifier_code_sha256": classifier_code_identity(),
        "verifier_code_sha256": verifier_code_identity(),
        "canonical_mutations": False,
    }
    receipt = IssuerDocumentStagingReceipt.model_validate(
        {**unsigned, "receipt_sha256": _sha(unsigned)}
    )
    (source.parent.parent / "staging_receipt.json").write_text(
        receipt.canonical_json + "\n", encoding="utf-8"
    )
    return receipt


def test_staging_receipt_rejects_tampering(tmp_path: Path) -> None:
    request = _request()
    root = tmp_path / "state"
    root.mkdir()
    receipt = _receipt(root, request)
    with pytest.raises(ValueError, match="receipt_sha256"):
        IssuerDocumentStagingReceipt.model_validate(
            {**receipt.model_dump(mode="json"), "receipt_sha256": "c" * 64}
        )


def test_managed_json_publish_preserves_installer_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "intent.json"
    residue = tmp_path / ".intent.json.retained.tmp"

    def fail_install(*_args: object, **_kwargs: object) -> object:
        raise managed_ir_sources.SecureFileInstallError(
            "secure_install_failed", residue_paths=(residue,)
        )

    monkeypatch.setattr(managed_ir_sources, "install_bytes_no_clobber", fail_install)
    with pytest.raises(PreparedIssuerDocumentPublisherError) as exc:
        managed_ir_sources._publish_managed_text(target, "{}")

    assert exc.value.code == "managed_artifact_publish_failed"
    assert exc.value.remaining_paths == (str(residue),)
    assert exc.value.owned_artifacts == (str(residue),)


def test_validate_staging_rejects_tampered_bytes_and_request(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    (root / "data").mkdir(parents=True)
    db_path = migrated_db(root / "data" / "portfolio.db")
    monkeypatch.setattr(managed_ir_sources, "_policy", _no_policy)
    monkeypatch.setattr(managed_ir_sources, "classify_ir_file", _classify)
    request = _request()
    _receipt(root, request)
    assert validate_prepared_staging(request, state_root=root, db_path=db_path).request == request
    with pytest.raises(PreparedIssuerDocumentPublisherError, match="staging_receipt_invalid"):
        validate_prepared_staging(
            request.model_copy(update={"attempt_id": "attempt-0002"}),
            state_root=root,
            db_path=db_path,
        )
    (root / ".tmp" / "managed_ir_staging" / request.attempt_id / "objects" / "q2.pdf").write_bytes(
        b"tampered"
    )
    with pytest.raises(PreparedIssuerDocumentPublisherError, match="staged_bytes_mismatch"):
        validate_prepared_staging(request, state_root=root, db_path=db_path)


@pytest.mark.parametrize("extra_kind", ["file", "directory"])
def test_validate_staging_rejects_undeclared_object_entries(
    migrated_db: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra_kind: str,
) -> None:
    root = tmp_path / "state"
    (root / "data").mkdir(parents=True)
    db_path = migrated_db(root / "data" / "portfolio.db")
    monkeypatch.setattr(managed_ir_sources, "_policy", _no_policy)
    monkeypatch.setattr(managed_ir_sources, "classify_ir_file", _classify)
    request = _request()
    _receipt(root, request)
    extra = root / ".tmp" / "managed_ir_staging" / request.attempt_id / "objects" / "extra.tmp"
    if extra_kind == "file":
        extra.write_bytes(b"retained residue")
    else:
        extra.mkdir()

    with pytest.raises(
        PreparedIssuerDocumentPublisherError, match="staged_objects_directory_invalid"
    ):
        validate_prepared_staging(request, state_root=root, db_path=db_path)


def test_validate_staging_rejects_symlinked_objects_directory(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    (root / "data").mkdir(parents=True)
    db_path = migrated_db(root / "data" / "portfolio.db")
    monkeypatch.setattr(managed_ir_sources, "_policy", _no_policy)
    monkeypatch.setattr(managed_ir_sources, "classify_ir_file", _classify)
    request = _request()
    _receipt(root, request)
    objects = root / ".tmp" / "managed_ir_staging" / request.attempt_id / "objects"
    outside = tmp_path / "outside-objects"
    objects.rename(outside)
    objects.symlink_to(outside, target_is_directory=True)

    with pytest.raises(
        PreparedIssuerDocumentPublisherError, match="staged_objects_directory_invalid"
    ):
        validate_prepared_staging(request, state_root=root, db_path=db_path)


def test_publisher_rejects_forged_public_authority_and_replays(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    (root / "data").mkdir(parents=True)
    db_path = migrated_db(root / "data" / "portfolio.db")
    monkeypatch.setattr(managed_ir_sources, "_policy", _no_policy)
    monkeypatch.setattr(managed_ir_sources, "classify_ir_file", _classify)
    request = _request()
    _receipt(root, request)
    # Public API has no authority argument; a forged capability cannot enter.
    with pytest.raises(TypeError):
        getattr(publish_prepared_issuer_documents, "__call__")(
            request, state_root=root, db_path=db_path, authority=object()
        )
    result = publish_prepared_issuer_documents(request, state_root=root, db_path=db_path)
    assert result.committed and result.inserted_document_ids
    assert result.inventory_receipt_sha256
    staging_object = (
        root / ".tmp" / "managed_ir_staging" / request.attempt_id / "objects" / "q2.pdf"
    )
    assert (root / result.created_paths[0]).stat().st_ino != staging_object.stat().st_ino
    replay = publish_prepared_issuer_documents(request, state_root=root, db_path=db_path)
    assert replay == result


def test_completed_publication_replays_after_attempt_tmp_is_deleted(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    (root / "data").mkdir(parents=True)
    db_path = migrated_db(root / "data" / "portfolio.db")
    monkeypatch.setattr(managed_ir_sources, "_policy", _no_policy)
    monkeypatch.setattr(managed_ir_sources, "classify_ir_file", _classify)
    request = _request()
    _receipt(root, request)

    result = publish_prepared_issuer_documents(request, state_root=root, db_path=db_path)
    durable = root / "data" / "managed_ir_publications" / request.attempt_id
    assert {
        "staging_receipt.json",
        "inventory_receipt.json",
        "publication_result.json",
    } == {path.name for path in durable.iterdir()}
    assert result.receipt_path.startswith("data/managed_ir_publications/")
    assert result.inventory_receipt_path.startswith("data/managed_ir_publications/")

    shutil.rmtree(root / ".tmp" / "managed_ir_staging" / request.attempt_id)
    assert publish_prepared_issuer_documents(request, state_root=root, db_path=db_path) == result


class _CommitRaisesAfterDurability:
    """A driver wrapper that loses only the commit acknowledgment."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def __getattr__(self, name: str) -> object:
        return getattr(self._connection, name)

    def commit(self) -> None:
        self._connection.commit()
        raise sqlite3.OperationalError("commit acknowledgment lost")


def test_commit_acknowledgment_fault_reconciles_durable_publication(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    (root / "data").mkdir(parents=True)
    db_path = migrated_db(root / "data" / "portfolio.db")
    monkeypatch.setattr(managed_ir_sources, "_policy", _no_policy)
    monkeypatch.setattr(managed_ir_sources, "classify_ir_file", _classify)
    request = _request()
    _receipt(root, request)
    original_connect = managed_ir_sources.connect_sqlite
    injected = False

    def connect_with_lost_acknowledgment(
        path: str | os.PathLike[str],
        *,
        role: managed_ir_sources.SQLiteConnectionRole,
        schema_preflight: bool | None = None,
    ) -> sqlite3.Connection:
        nonlocal injected
        connection = original_connect(path, role=role, schema_preflight=schema_preflight)
        if not injected and role is managed_ir_sources.SQLiteConnectionRole.WRITER:
            injected = True
            return cast(sqlite3.Connection, _CommitRaisesAfterDurability(connection))
        return connection

    monkeypatch.setattr(managed_ir_sources, "connect_sqlite", connect_with_lost_acknowledgment)
    first = publish_prepared_issuer_documents(request, state_root=root, db_path=db_path)
    assert injected and first.committed and first.inserted_document_ids
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM managed_ir_publications").fetchone() == (1,)
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone() == (1,)
    assert publish_prepared_issuer_documents(request, state_root=root, db_path=db_path) == first


@pytest.mark.parametrize("reconciliation", ("durable", "canonical"))
def test_commit_acknowledgment_reconciliation_failure_preserves_durable_publication(
    migrated_db: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reconciliation: str,
) -> None:
    """A lost commit acknowledgement never licenses rollback or file cleanup."""
    root = tmp_path / "state"
    (root / "data").mkdir(parents=True)
    db_path = migrated_db(root / "data" / "portfolio.db")
    monkeypatch.setattr(managed_ir_sources, "_policy", _no_policy)
    monkeypatch.setattr(managed_ir_sources, "classify_ir_file", _classify)
    request = _request()
    receipt = _receipt(root, request)
    original_connect = managed_ir_sources.connect_sqlite
    injected = False

    def connect_with_lost_acknowledgment(
        path: str | os.PathLike[str],
        *,
        role: managed_ir_sources.SQLiteConnectionRole,
        schema_preflight: bool | None = None,
    ) -> sqlite3.Connection:
        nonlocal injected
        connection = original_connect(path, role=role, schema_preflight=schema_preflight)
        if not injected and role is managed_ir_sources.SQLiteConnectionRole.WRITER:
            injected = True
            return cast(sqlite3.Connection, _CommitRaisesAfterDurability(connection))
        return connection

    monkeypatch.setattr(managed_ir_sources, "connect_sqlite", connect_with_lost_acknowledgment)
    if reconciliation == "durable":
        original_recovery = cast(Callable[..., object], managed_ir_sources._durable_recovery)

        def fail_fresh_recovery(*args: object, **kwargs: object) -> object:
            if injected:
                raise PreparedIssuerDocumentPublisherError("fresh_reconciliation_query_failed")
            return original_recovery(*args, **kwargs)

        monkeypatch.setattr(managed_ir_sources, "_durable_recovery", fail_fresh_recovery)
    else:
        original_rows = cast(Callable[..., object], managed_ir_sources._canonical_rows)

        def fail_fresh_rows(*args: object, **kwargs: object) -> object:
            if injected:
                raise PreparedIssuerDocumentPublisherError("fresh_reconciliation_query_failed")
            return original_rows(*args, **kwargs)

        monkeypatch.setattr(managed_ir_sources, "_canonical_rows", fail_fresh_rows)

    with pytest.raises(PreparedIssuerDocumentPublisherError) as raised:
        publish_prepared_issuer_documents(request, state_root=root, db_path=db_path)

    assert raised.value.code == "publication_commit_outcome_unknown"
    assert raised.value.committed
    target = managed_ir_sources._target(root, receipt.documents[0])
    assert target.read_bytes() == b"presentation"
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone() == (1,)
        assert conn.execute("SELECT COUNT(*) FROM managed_ir_publications").fetchone() == (1,)


def test_private_held_publisher_requires_exact_claims_and_composes(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    (root / "data").mkdir(parents=True)
    db_path = migrated_db(root / "data" / "portfolio.db")
    monkeypatch.setattr(managed_ir_sources, "_policy", _no_policy)
    monkeypatch.setattr(managed_ir_sources, "classify_ir_file", _classify)
    request = _request()
    _receipt(root, request)
    held = getattr(managed_ir_sources, "_publish_prepared_issuer_documents_held")
    with pytest.raises(PreparedIssuerDocumentPublisherError, match="managed_lock_claim_missing"):
        held(request, state_root=root, db_path=db_path)
    with JobLock(root, "outer-managed-admission", ["ir-discovery", "portfolio-db"], wait_s=0):
        result = held(request, state_root=root, db_path=db_path)
    assert result.committed and result.inserted_document_ids


def test_publisher_recovers_committed_inventory_failure_and_rejects_replay_drift(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    (root / "data").mkdir(parents=True)
    db_path = migrated_db(root / "data" / "portfolio.db")
    monkeypatch.setattr(managed_ir_sources, "_policy", _no_policy)
    monkeypatch.setattr(managed_ir_sources, "classify_ir_file", _classify)
    request = _request()
    _receipt(root, request)
    original = getattr(managed_ir_sources, "_published_inventory")

    def fail_once(*args: object, **kwargs: object) -> tuple[Path, str]:
        del args, kwargs
        raise RuntimeError("receipt disk full")

    monkeypatch.setattr("pipeline.managed_ir_sources._published_inventory", fail_once)
    with pytest.raises(PreparedIssuerDocumentPublisherError) as partial:
        publish_prepared_issuer_documents(request, state_root=root, db_path=db_path)
    assert partial.value.committed
    assert partial.value.inserted
    monkeypatch.setattr("pipeline.managed_ir_sources._published_inventory", original)
    recovered = publish_prepared_issuer_documents(request, state_root=root, db_path=db_path)
    assert recovered.committed
    (root / recovered.created_paths[0]).write_bytes(b"drift")
    with pytest.raises(PreparedIssuerDocumentPublisherError, match="canonical_file_drift"):
        publish_prepared_issuer_documents(request, state_root=root, db_path=db_path)


def test_replay_preserves_original_inventory_after_unrelated_database_write(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    (root / "data").mkdir(parents=True)
    db_path = migrated_db(root / "data" / "portfolio.db")
    monkeypatch.setattr(managed_ir_sources, "_policy", _no_policy)
    monkeypatch.setattr(managed_ir_sources, "classify_ir_file", _classify)
    request = _request()
    _receipt(root, request)
    original = publish_prepared_issuer_documents(request, state_root=root, db_path=db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE unrelated_later_write (marker TEXT NOT NULL)")
        conn.execute("INSERT INTO unrelated_later_write VALUES ('later')")
    assert publish_prepared_issuer_documents(request, state_root=root, db_path=db_path) == original


def test_replay_rejects_missing_durable_publication_record(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    (root / "data").mkdir(parents=True)
    db_path = migrated_db(root / "data" / "portfolio.db")
    monkeypatch.setattr(managed_ir_sources, "_policy", _no_policy)
    monkeypatch.setattr(managed_ir_sources, "classify_ir_file", _classify)
    request = _request()
    _receipt(root, request)
    publish_prepared_issuer_documents(request, state_root=root, db_path=db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TRIGGER managed_ir_publications_append_only_delete")
        conn.execute("DELETE FROM managed_ir_publications")
    with pytest.raises(PreparedIssuerDocumentPublisherError, match="publication_record_missing"):
        publish_prepared_issuer_documents(request, state_root=root, db_path=db_path)


def test_copy_no_replace_returns_created_token_and_reused_none(tmp_path: Path) -> None:
    source = tmp_path / "stage.pdf"
    target = tmp_path / "target.pdf"
    source.write_bytes(b"same bytes")
    digest = hashlib.sha256(b"same bytes").hexdigest()
    created = managed_ir_sources._copy_no_replace(source, target, digest, len(b"same bytes"))
    assert created is not None and created.path == target
    assert managed_ir_sources._copy_no_replace(source, target, digest, len(b"same bytes")) is None


def test_owned_cleanup_retains_a_replacement_at_the_created_canonical_path(tmp_path: Path) -> None:
    source = tmp_path / "stage.pdf"
    target = tmp_path / "canonical.pdf"
    source.write_bytes(b"authorized")
    artifact = managed_ir_sources._copy_no_replace(
        source,
        target,
        hashlib.sha256(b"authorized").hexdigest(),
        len(b"authorized"),
    )
    assert artifact is not None
    target.unlink()
    target.write_bytes(b"replacement survives")
    removed, remaining = managed_ir_sources._cleanup_owned_artifacts([artifact])
    assert removed == ()
    assert remaining == (str(target),)
    assert target.read_bytes() == b"replacement survives"


def test_canonical_relative_path_is_windows_separator_independent() -> None:
    assert (
        managed_ir_sources._canonical_relative_path(
            PureWindowsPath("C:/state"),
            PureWindowsPath("C:/state/ir_documents/MELI/2026-06-30/report.pdf"),
        )
        == "ir_documents/MELI/2026-06-30/report.pdf"
    )


def test_preexisting_canonical_hardlink_blocks_before_any_row_or_episode(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    (root / "data").mkdir(parents=True)
    db_path = migrated_db(root / "data" / "portfolio.db")
    monkeypatch.setattr(managed_ir_sources, "_policy", _no_policy)
    monkeypatch.setattr(managed_ir_sources, "classify_ir_file", _classify)
    request = _request()
    receipt = _receipt(root, request)
    stage = root / ".tmp" / "managed_ir_staging" / request.attempt_id / "objects" / "q2.pdf"
    target = managed_ir_sources._target(root, receipt.documents[0])
    target.parent.mkdir(parents=True)
    os.link(stage, target)
    with pytest.raises(
        PreparedIssuerDocumentPublisherError, match="staged_objects_directory_invalid"
    ):
        publish_prepared_issuer_documents(request, state_root=root, db_path=db_path)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM managed_ir_publications").fetchone() == (0,)


def test_replay_rejects_tampered_durable_record(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    (root / "data").mkdir(parents=True)
    db_path = migrated_db(root / "data" / "portfolio.db")
    monkeypatch.setattr(managed_ir_sources, "_policy", _no_policy)
    monkeypatch.setattr(managed_ir_sources, "classify_ir_file", _classify)
    request = _request()
    _receipt(root, request)
    publish_prepared_issuer_documents(request, state_root=root, db_path=db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TRIGGER managed_ir_publications_append_only_update")
        conn.execute("UPDATE managed_ir_publications SET payload_sha256=?", ("0" * 64,))
    with pytest.raises(PreparedIssuerDocumentPublisherError, match="publication_record_invalid"):
        publish_prepared_issuer_documents(request, state_root=root, db_path=db_path)


def test_publisher_rejects_wrong_configured_database_and_code_identity_drift(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    (root / "data").mkdir(parents=True)
    db_path = migrated_db(root / "data" / "portfolio.db")
    other_db = migrated_db(tmp_path / "other.db")
    request = _request()
    _receipt(root, request)
    with pytest.raises(PreparedIssuerDocumentPublisherError, match="configured_database_mismatch"):
        publish_prepared_issuer_documents(request, state_root=root, db_path=other_db)
    monkeypatch.setattr(managed_ir_sources, "_policy", _no_policy)
    monkeypatch.setattr(managed_ir_sources, "classify_ir_file", _classify)
    monkeypatch.setattr(managed_ir_sources, "classifier_code_identity", lambda: "0" * 64)
    with pytest.raises(PreparedIssuerDocumentPublisherError, match="staging_code_identity_changed"):
        publish_prepared_issuer_documents(request, state_root=root, db_path=db_path)


def test_held_seam_rejects_partial_claims(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    (root / "data").mkdir(parents=True)
    db_path = migrated_db(root / "data" / "portfolio.db")
    monkeypatch.setattr(managed_ir_sources, "_policy", _no_policy)
    monkeypatch.setattr(managed_ir_sources, "classify_ir_file", _classify)
    request = _request()
    _receipt(root, request)
    original = managed_ir_sources.current_lock_claim

    def missing_db_claim(path: Path, write_set: str) -> tuple[int, str, str | None] | None:
        if write_set == "portfolio-db":
            return None
        return original(path, write_set)

    monkeypatch.setattr(managed_ir_sources, "current_lock_claim", missing_db_claim)
    with (
        JobLock(root, "outer-managed-admission", ["ir-discovery", "portfolio-db"], wait_s=0),
        pytest.raises(PreparedIssuerDocumentPublisherError, match="managed_lock_claim_missing"),
    ):
        managed_ir_sources._publish_prepared_issuer_documents_held(
            request, state_root=root, db_path=db_path
        )


def test_two_document_rollback_removes_all_new_rows_files_and_episode(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    (root / "data").mkdir(parents=True)
    db_path = migrated_db(root / "data" / "portfolio.db")
    monkeypatch.setattr(managed_ir_sources, "_policy", _no_policy)
    request = _two_request()
    receipt = _two_receipt(root, request)

    def classify(path: Path, **_kwargs: object) -> CategorizationResult:
        return _press_outcome() if path.name == "q2b.pdf" else _outcome()

    monkeypatch.setattr(managed_ir_sources, "classify_ir_file", classify)
    original = managed_ir_sources._preflight_existing_targets
    calls = 0

    def fail_final(items: list[tuple[StagedIssuerDocument, Path, Path]]) -> None:
        nonlocal calls
        calls += 1
        original(items)
        if calls == 3:
            raise PreparedIssuerDocumentPublisherError("forced_after_two_rows")

    monkeypatch.setattr(managed_ir_sources, "_preflight_existing_targets", fail_final)
    with pytest.raises(PreparedIssuerDocumentPublisherError) as raised:
        publish_prepared_issuer_documents(request, state_root=root, db_path=db_path)
    assert raised.value.inserted and len(raised.value.inserted) == 2
    assert raised.value.reused == ()
    assert len(raised.value.created) == 2
    assert raised.value.code == "publication_cleanup_partial"
    assert raised.value.removed_paths == ()
    assert raised.value.remaining_paths == tuple(reversed(raised.value.created))
    assert raised.value.owned_artifacts == raised.value.created
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM managed_ir_publications").fetchone() == (0,)
    assert all(Path(path).exists() for path in raised.value.created)
    assert receipt.documents
    with pytest.raises(PreparedIssuerDocumentPublisherError, match="publication_outcome_ambiguous"):
        publish_prepared_issuer_documents(request, state_root=root, db_path=db_path)


def test_mixed_reuse_insert_rollback_preserves_reuse_and_reports_dispositions(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    (root / "data").mkdir(parents=True)
    db_path = migrated_db(root / "data" / "portfolio.db")
    monkeypatch.setattr(managed_ir_sources, "_policy", _no_policy)
    request = _two_request()
    receipt = _two_receipt(root, request)

    def classify(path: Path, **_kwargs: object) -> CategorizationResult:
        return _press_outcome() if path.name == "q2b.pdf" else _outcome()

    monkeypatch.setattr(managed_ir_sources, "classify_ir_file", classify)
    reused_item = receipt.documents[0]
    reused_target = managed_ir_sources._target(root, reused_item)
    reused_target.parent.mkdir(parents=True)
    reused_target.write_bytes(b"presentation")
    expected = (
        reused_item.ticker,
        "ir_doc",
        reused_item.document_type,
        reused_item.period_end,
        managed_ir_sources._relative_path(root, reused_target),
        reused_item.sha256,
        reused_item.fetched_at.isoformat(),
        "ok",
        reused_item.byte_size,
        reused_item.source_url,
    )
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO documents (ticker,source_type,doc_type,period_end,file_path,sha256,fetched_at,fetch_status,raw_bytes_size,source_url) VALUES (?,?,?,?,?,?,?,?,?,?)",
            expected,
        )
        assert cursor.lastrowid is not None
        reused_id = int(cursor.lastrowid)
    original = managed_ir_sources._preflight_existing_targets
    calls = 0

    def fail_final(items: list[tuple[StagedIssuerDocument, Path, Path]]) -> None:
        nonlocal calls
        calls += 1
        original(items)
        if calls == 3:
            raise PreparedIssuerDocumentPublisherError("forced_after_reuse_and_insert")

    monkeypatch.setattr(managed_ir_sources, "_preflight_existing_targets", fail_final)
    with pytest.raises(PreparedIssuerDocumentPublisherError) as raised:
        publish_prepared_issuer_documents(request, state_root=root, db_path=db_path)
    assert raised.value.reused == (reused_id,)
    assert len(raised.value.inserted) == 1
    assert len(raised.value.created) == 1
    assert raised.value.code == "publication_cleanup_partial"
    assert raised.value.removed_paths == ()
    assert raised.value.remaining_paths == raised.value.created
    assert raised.value.owned_artifacts == raised.value.created
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT id FROM documents").fetchall() == [(reused_id,)]
        assert conn.execute("SELECT COUNT(*) FROM managed_ir_publications").fetchone() == (0,)
    assert reused_target.exists()
    assert Path(raised.value.created[0]).exists()


def test_duplicate_source_url_blocks_before_canonical_mutation(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    (root / "data").mkdir(parents=True)
    db_path = migrated_db(root / "data" / "portfolio.db")
    monkeypatch.setattr(managed_ir_sources, "_policy", _no_policy)
    monkeypatch.setattr(managed_ir_sources, "classify_ir_file", _classify)
    request = _request()
    _receipt(root, request)
    with sqlite3.connect(db_path) as conn:
        for marker in ("one", "two"):
            conn.execute(
                "INSERT INTO documents (ticker,source_type,doc_type,period_end,file_path,sha256,fetched_at,fetch_status,raw_bytes_size,source_url) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    "MELI",
                    "legacy",
                    marker,
                    "2026-06-30",
                    f"legacy/{marker}",
                    ("a" if marker == "one" else "b") * 64,
                    "2026-08-22T00:00:00+00:00",
                    "ok",
                    1,
                    "https://issuer.test/q2.pdf",
                ),
            )
    with pytest.raises(
        PreparedIssuerDocumentPublisherError, match="document_source_url_cardinality"
    ):
        publish_prepared_issuer_documents(request, state_root=root, db_path=db_path)
    assert not (root / "ir_documents").exists()
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM managed_ir_publications").fetchone() == (0,)


def test_insert_planned_partial_state_is_ambiguous_not_reused(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    (root / "data").mkdir(parents=True)
    db_path = migrated_db(root / "data" / "portfolio.db")
    monkeypatch.setattr(managed_ir_sources, "_policy", _no_policy)
    monkeypatch.setattr(managed_ir_sources, "classify_ir_file", _classify)
    request = _request()
    receipt = _receipt(root, request)

    def stop_after_intent(*_args: object, **_kwargs: object) -> bool:
        raise PreparedIssuerDocumentPublisherError("forced_after_intent")

    original_copy = managed_ir_sources._copy_no_replace
    monkeypatch.setattr(managed_ir_sources, "_copy_no_replace", stop_after_intent)
    with pytest.raises(PreparedIssuerDocumentPublisherError, match="forced_after_intent"):
        publish_prepared_issuer_documents(request, state_root=root, db_path=db_path)
    intent = root / ".tmp" / "managed_ir_staging" / request.attempt_id / "publication_intent.json"
    assert intent.exists()
    item = receipt.documents[0]
    target = managed_ir_sources._target(root, item)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"presentation")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO documents (ticker,source_type,doc_type,period_end,file_path,sha256,fetched_at,fetch_status,raw_bytes_size,source_url) VALUES (?,?,?,?,?,?,?,?,?,?)",
            managed_ir_sources._expected_row(item, root, target),
        )
    monkeypatch.setattr(managed_ir_sources, "_copy_no_replace", original_copy)
    with pytest.raises(PreparedIssuerDocumentPublisherError, match="publication_outcome_ambiguous"):
        publish_prepared_issuer_documents(request, state_root=root, db_path=db_path)


def test_inventory_evidence_is_required_for_result_replay(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    (root / "data").mkdir(parents=True)
    db_path = migrated_db(root / "data" / "portfolio.db")
    monkeypatch.setattr(managed_ir_sources, "_policy", _no_policy)
    monkeypatch.setattr(managed_ir_sources, "classify_ir_file", _classify)
    request = _request()
    _receipt(root, request)
    publish_prepared_issuer_documents(request, state_root=root, db_path=db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TRIGGER managed_ir_inventory_evidence_append_only_delete")
        conn.execute("DELETE FROM managed_ir_inventory_evidence")
    with pytest.raises(PreparedIssuerDocumentPublisherError, match="inventory_receipt_drift"):
        publish_prepared_issuer_documents(request, state_root=root, db_path=db_path)


def test_evidence_less_inventory_must_equal_current_pinned_snapshot(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    (root / "data").mkdir(parents=True)
    db_path = migrated_db(root / "data" / "portfolio.db")
    monkeypatch.setattr(managed_ir_sources, "_policy", _no_policy)
    monkeypatch.setattr(managed_ir_sources, "classify_ir_file", _classify)
    request = _request()
    _receipt(root, request)

    def stop_evidence(*_args: object, **_kwargs: object) -> object:
        raise PreparedIssuerDocumentPublisherError("forced_evidence_gap")

    original = managed_ir_sources._seal_inventory_evidence
    monkeypatch.setattr(managed_ir_sources, "_seal_inventory_evidence", stop_evidence)
    with pytest.raises(PreparedIssuerDocumentPublisherError) as partial:
        publish_prepared_issuer_documents(request, state_root=root, db_path=db_path)
    assert partial.value.committed
    path = root / "data" / "managed_ir_publications" / request.attempt_id / "inventory_receipt.json"
    persisted = IssuerDocumentInventoryReceipt.model_validate_json(path.read_bytes())
    unsigned = persisted.model_dump(mode="json", exclude={"receipt_sha256"})
    unsigned["verifier_code_sha256"] = "0" * 64
    forged = IssuerDocumentInventoryReceipt.model_validate(
        {
            **unsigned,
            "receipt_sha256": issuer_document_inventory._sha256_text(
                issuer_document_inventory._canonical_json(unsigned)
            ),
        }
    )
    # This is an isolated adversarial fixture: overwrite bytes in place without
    # unlinking/replacing the artifact pathname under test.
    path.chmod(0o600)
    descriptor = os.open(path, os.O_WRONLY | os.O_TRUNC)
    try:
        os.write(descriptor, (forged.canonical_json + "\n").encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
        path.chmod(0o400)
    monkeypatch.setattr(managed_ir_sources, "_seal_inventory_evidence", original)
    with pytest.raises(
        PreparedIssuerDocumentPublisherError, match="publication_committed_partial"
    ) as raised:
        publish_prepared_issuer_documents(request, state_root=root, db_path=db_path)
    assert raised.value.committed and raised.value.inventory_state == "failed"
