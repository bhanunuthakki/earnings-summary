from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Protocol

import pytest
from pydantic import BaseModel, ValidationError

import provenance.population_document_processing as population
from provenance.evidence_ledger import ContentBlob, EvidenceLedger, SourceObservation
from provenance.issuer_registry import IssuerEntity, IssuerRegistry
from provenance.population_completeness import PopulationTemporalScope
from provenance.population_document_processing import (
    DocumentProcessingCheckpoint,
    DocumentProcessingOperationReceipt,
    DocumentProcessingPopulationRequest,
    DocumentProcessingPopulationResult,
    ReportingDocumentDecision,
    classify_reporting_document,
    document_processing_plan_commitment,
    populate_document_processing,
    verify_document_processing,
    verify_document_processing_receipt,
)
from provenance.population_document_processing import (
    build_document_processing_receipt as _build_document_processing_receipt,
)
from provenance.reporting_entity_registry import (
    ReportingEntity,
    ReportingEntityRegistry,
    SourceObligationRevision,
)
from provenance.source_coverage import (
    ExpectedDocument,
    SourceCoverageLedger,
    SourceInventorySnapshot,
)
from runtime.deterministic import sha256_joined_nul, sha256_joined_unit_sep
from sqlite_runtime import register_sqlite_integrity_functions


class _CheckpointFactory(Protocol):
    def __call__(
        self,
        *,
        bounded: bool,
        prior_cursor: str | None,
        processed: int,
        total: int,
        sealed: int,
        blocker_count: int = ...,
    ) -> DocumentProcessingCheckpoint: ...


class _RetryCursor(Protocol):
    def __call__(
        self, *, prior_cursor: str | None, attempted_id: str, succeeded: bool
    ) -> str | None: ...


model_sha: Callable[[BaseModel], str] = getattr(population, "_model_sha")
document_receipt_sha: Callable[[DocumentProcessingOperationReceipt], str] = getattr(
    population, "_document_receipt_sha"
)
document_checkpoint: _CheckpointFactory = getattr(population, "_document_checkpoint")
seal_complete_snapshots: Callable[
    [sqlite3.Connection, dict[str, tuple[str, ...]], datetime, datetime], None
] = getattr(population, "_seal_complete_snapshots")
reporting_entity_scope_rows: Callable[
    [sqlite3.Connection, tuple[ReportingDocumentDecision, ...]], list[list[object]]
] = getattr(population, "_reporting_entity_scope_rows")
document_blob_scope_rows: Callable[[sqlite3.Connection, tuple[str, ...]], list[list[object]]] = (
    getattr(population, "_document_blob_scope_rows")
)
retry_cursor_after_attempt: _RetryCursor = getattr(population, "_retry_cursor_after_attempt")
ensure_expected_document_binding: Callable[
    [sqlite3.Connection, ReportingDocumentDecision, datetime, datetime], bool
] = getattr(population, "_ensure_expected_document_binding")
ensure_document_family_obligations: Callable[
    [sqlite3.Connection, tuple[ReportingDocumentDecision, ...], datetime, datetime], int
] = getattr(population, "_ensure_document_family_obligations")


def build_test_document_processing_receipt(
    *,
    database_path: str,
    database_instance_id: str,
    alembic_revision: str,
    request: DocumentProcessingPopulationRequest,
    result: DocumentProcessingPopulationResult,
    prior_checkpoint_receipt_sha256: str | None,
    admission_receipt_sha256: str | None,
) -> DocumentProcessingOperationReceipt:
    """Build coherent synthetic evidence while production validates every commitment."""

    plan_sha = document_processing_plan_commitment(
        request,
        result.input_commitment_sha256,
        result.selection_commitment_sha256,
    )
    result = result.model_copy(update={"plan_commitment_sha256": plan_sha})
    if request.input_commitment_sha256 is not None:
        request = request.model_copy(
            update={
                "input_commitment_sha256": result.input_commitment_sha256,
                "plan_commitment_sha256": plan_sha,
            }
        )
    return _build_document_processing_receipt(
        database_path=database_path,
        database_instance_id=database_instance_id,
        alembic_revision=alembic_revision,
        request=request,
        result=result,
        prior_checkpoint_receipt_sha256=prior_checkpoint_receipt_sha256,
        admission_receipt_sha256=admission_receipt_sha256,
    )


build_document_processing_receipt = build_test_document_processing_receipt


def receipt_result(
    *,
    mode: Literal["dry_run", "apply"] = "dry_run",
    bounded: bool = False,
    remaining: int = 0,
) -> DocumentProcessingPopulationResult:
    return DocumentProcessingPopulationResult(
        mode=mode,
        phase="all",
        expected_document_count=1,
        missing_document_count=0,
        excluded_document_count=0,
        unresolved_document_count=0,
        incomplete_inventory_count=0,
        binding_count=1,
        binding_created_count=0,
        binding_failure_count=0,
        selection_reason_counts={"governed_periodic_filing": 1},
        source_obligation_count=1,
        source_obligation_created_count=0,
        expected_obligation_count=1,
        applicable_obligation_count=1,
        not_applicable_obligation_count=0,
        sealed_disposition_count=1 - remaining,
        failed_obligation_count=remaining,
        failed_reason_counts=(
            {} if remaining == 0 else {"unsealed_processing_obligation": remaining}
        ),
        processed_obligation_count=1 - remaining,
        last_processing_obligation_revision_id=("obligation-1" if bounded else None),
        expected_issuer_count=1,
        processing_snapshot_count=0 if bounded else 1,
        selection_commitment_sha256="a" * 64,
        input_commitment_sha256="b" * 64,
        post_state_commitment_sha256="b" * 64,
        plan_commitment_sha256="c" * 64,
        output_commitment_sha256="d" * 64,
        checkpoint=DocumentProcessingCheckpoint(
            bounded=bounded,
            safe_to_seal=not bounded and remaining == 0,
            last_processing_obligation_revision_id=("obligation-1" if bounded else None),
            processed_obligation_count=1 - remaining,
            remaining_obligation_count=remaining,
            can_resume=bounded and remaining > 0,
        ),
    )


def test_document_operation_receipt_binds_request_result_and_prior_evidence() -> None:
    cutoff = datetime(2026, 7, 29, tzinfo=UTC)
    request = DocumentProcessingPopulationRequest(
        cutoff_at=cutoff,
        operation_recorded_at=cutoff,
    )
    receipt = build_document_processing_receipt(
        database_path="C:/candidate.db",
        database_instance_id="database-instance:" + "1" * 32,
        alembic_revision="0263_ask_scope_identity",
        request=request,
        result=receipt_result(),
        prior_checkpoint_receipt_sha256=None,
        admission_receipt_sha256=None,
    )

    assert receipt.outcome == "planned"
    assert verify_document_processing_receipt(receipt)
    assert len(receipt.receipt_sha256) == 64
    tampered = receipt.model_copy(update={"database_path": "C:/other.db"})
    assert not verify_document_processing_receipt(tampered)
    with pytest.raises(ValidationError):
        DocumentProcessingOperationReceipt.model_validate(tampered.model_dump(mode="json"))


def test_document_receipt_rejects_self_rehashed_plan_tamper() -> None:
    cutoff = datetime(2026, 7, 29, tzinfo=UTC)
    valid = build_test_document_processing_receipt(
        database_path="C:/candidate.db",
        database_instance_id="database-instance:" + "1" * 32,
        alembic_revision="0263_ask_scope_identity",
        request=DocumentProcessingPopulationRequest(
            cutoff_at=cutoff,
            operation_recorded_at=cutoff,
            apply=True,
            input_commitment_sha256="b" * 64,
            plan_commitment_sha256="c" * 64,
        ),
        result=receipt_result(mode="apply"),
        prior_checkpoint_receipt_sha256=None,
        admission_receipt_sha256="e" * 64,
    )
    forged_plan = "f" * 64
    forged_request = valid.request.model_copy(update={"plan_commitment_sha256": forged_plan})
    forged_result = valid.result.model_copy(update={"plan_commitment_sha256": forged_plan})
    forged = valid.model_copy(
        update={
            "request": forged_request,
            "result": forged_result,
            "request_sha256": model_sha(forged_request),
            "result_sha256": model_sha(forged_result),
            "operation_id": population.document_processing_operation_id(
                database_instance_id=valid.database_instance_id,
                request=forged_request,
                admission_receipt_sha256=valid.admission_receipt_sha256,
                prior_checkpoint_receipt_sha256=valid.prior_checkpoint_receipt_sha256,
            ),
        }
    )
    forged = forged.model_copy(update={"receipt_sha256": document_receipt_sha(forged)})

    with pytest.raises(ValidationError, match="result plan commitment"):
        DocumentProcessingOperationReceipt.model_validate(forged.model_dump(mode="json"))


def test_apply_receipt_requires_admission_and_marks_bounded_work_checkpoint() -> None:
    cutoff = datetime(2026, 7, 29, tzinfo=UTC)
    request = DocumentProcessingPopulationRequest(
        cutoff_at=cutoff,
        operation_recorded_at=cutoff,
        apply=True,
        max_obligations=1,
        input_commitment_sha256="b" * 64,
        plan_commitment_sha256="c" * 64,
    )
    with pytest.raises(ValueError, match="admission"):
        build_document_processing_receipt(
            database_path="C:/candidate.db",
            database_instance_id="database-instance:" + "1" * 32,
            alembic_revision="0263_ask_scope_identity",
            request=request,
            result=receipt_result(mode="apply", bounded=True, remaining=1),
            prior_checkpoint_receipt_sha256=None,
            admission_receipt_sha256=None,
        )

    receipt = build_document_processing_receipt(
        database_path="C:/candidate.db",
        database_instance_id="database-instance:" + "1" * 32,
        alembic_revision="0263_ask_scope_identity",
        request=request,
        result=receipt_result(mode="apply", bounded=True, remaining=1),
        prior_checkpoint_receipt_sha256=None,
        admission_receipt_sha256="e" * 64,
    )
    assert receipt.outcome == "checkpoint"


def test_resume_apply_requires_dry_run_commitments() -> None:
    with pytest.raises(ValidationError, match="commitments"):
        DocumentProcessingPopulationRequest(
            cutoff_at=population.datetime(2026, 7, 29),
            operation_recorded_at=population.datetime(2026, 7, 29),
            apply=True,
            after_processing_obligation_revision_id="obligation-1",
        )


def test_bounded_apply_requires_dry_run_commitments() -> None:
    with pytest.raises(ValidationError, match="commitments"):
        DocumentProcessingPopulationRequest(
            cutoff_at=population.datetime(2026, 7, 29),
            operation_recorded_at=population.datetime(2026, 7, 29),
            apply=True,
            max_obligations=1,
        )


def test_bounded_checkpoint_never_claims_safe_to_seal() -> None:
    checkpoint = document_checkpoint(
        bounded=True,
        prior_cursor="obligation-1",
        processed=2,
        total=5,
        sealed=2,
    )

    assert checkpoint.safe_to_seal is False
    assert checkpoint.can_resume is True
    assert checkpoint.remaining_obligation_count == 3
    assert checkpoint.last_processing_obligation_revision_id == "obligation-1"


def test_first_item_failure_does_not_claim_resumable_checkpoint() -> None:
    checkpoint = document_checkpoint(
        bounded=True,
        prior_cursor=None,
        processed=0,
        total=5,
        sealed=0,
        blocker_count=1,
    )

    assert checkpoint.can_resume is False
    assert checkpoint.last_processing_obligation_revision_id is None


def test_first_item_failure_receipt_is_blocked_not_checkpoint() -> None:
    cutoff = datetime(2026, 7, 29, tzinfo=UTC)
    result = receipt_result(mode="apply", bounded=True, remaining=1)
    result = result.model_copy(
        update={
            "last_processing_obligation_revision_id": None,
            "processed_obligation_count": 0,
            "checkpoint": result.checkpoint.model_copy(
                update={
                    "last_processing_obligation_revision_id": None,
                    "processed_obligation_count": 0,
                    "can_resume": False,
                }
            ),
        }
    )
    receipt = build_document_processing_receipt(
        database_path="C:/candidate.db",
        database_instance_id="database-instance:" + "1" * 32,
        alembic_revision="0264_document_processing_operation_ledger",
        request=DocumentProcessingPopulationRequest(
            cutoff_at=cutoff,
            operation_recorded_at=cutoff,
            apply=True,
            max_obligations=1,
            input_commitment_sha256="b" * 64,
            plan_commitment_sha256="c" * 64,
        ),
        result=result,
        prior_checkpoint_receipt_sha256=None,
        admission_receipt_sha256="e" * 64,
    )

    assert receipt.outcome == "blocked"


def test_snapshot_batch_preflights_every_issuer_before_first_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE sealed_snapshots (issuer_id TEXT NOT NULL)")
    documents = {"issuer-a": ("document-a",), "issuer-b": ("document-b",)}

    def totals_stub(
        _conn: sqlite3.Connection,
        _cutoff: datetime,
        document_ids: tuple[str, ...],
        _recorded: datetime,
    ) -> dict[str, int]:
        return {"total": 1 if document_ids == ("document-a",) else 2}

    def sealed_stub(
        _conn: sqlite3.Connection,
        _cutoff: datetime,
        _document_ids: tuple[str, ...],
        _recorded: datetime,
    ) -> int:
        return 1

    monkeypatch.setattr(population, "_obligation_totals", totals_stub)
    monkeypatch.setattr(population, "_sealed_disposition_count", sealed_stub)

    def seal_stub(
        connection: sqlite3.Connection,
        **kwargs: object,
    ) -> None:
        connection.execute(
            "INSERT INTO sealed_snapshots VALUES (?)",
            (str(kwargs["processing_snapshot_id"]),),
        )

    monkeypatch.setattr(population, "seal_processing_snapshot", seal_stub)

    with pytest.raises(ValueError, match="issuer-b"):
        seal_complete_snapshots(
            conn,
            documents,
            datetime(2026, 7, 29, tzinfo=UTC),
            datetime(2026, 7, 29, tzinfo=UTC),
        )

    assert conn.execute("SELECT COUNT(*) FROM sealed_snapshots").fetchone()[0] == 0


def test_snapshot_batch_rolls_back_every_issuer_when_later_seal_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE sealed_snapshots (snapshot_id TEXT NOT NULL)")
    documents = {"issuer-a": ("document-a",), "issuer-b": ("document-b",)}

    def totals_stub(
        _conn: sqlite3.Connection,
        _cutoff: datetime,
        _document_ids: tuple[str, ...],
        _recorded: datetime,
    ) -> dict[str, int]:
        return {"total": 1}

    def sealed_stub(
        _conn: sqlite3.Connection,
        _cutoff: datetime,
        _document_ids: tuple[str, ...],
        _recorded: datetime,
    ) -> int:
        return 1

    calls = 0

    def seal_stub(
        connection: sqlite3.Connection,
        **kwargs: object,
    ) -> None:
        nonlocal calls
        calls += 1
        connection.execute(
            "INSERT INTO sealed_snapshots VALUES (?)",
            (str(kwargs["processing_snapshot_id"]),),
        )
        if calls == 2:
            raise ValueError("second issuer seal failed")

    monkeypatch.setattr(population, "_obligation_totals", totals_stub)
    monkeypatch.setattr(population, "_sealed_disposition_count", sealed_stub)
    monkeypatch.setattr(population, "seal_processing_snapshot", seal_stub)

    with pytest.raises(ValueError, match="second issuer"):
        seal_complete_snapshots(
            conn,
            documents,
            datetime(2026, 7, 29, tzinfo=UTC),
            datetime(2026, 7, 29, tzinfo=UTC),
        )

    assert conn.execute("SELECT COUNT(*) FROM sealed_snapshots").fetchone()[0] == 0


def test_read_set_binds_all_reporting_entity_fallback_candidates() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE reporting_entities ("
        "reporting_entity_id TEXT PRIMARY KEY,issuer_id TEXT NOT NULL,display_name TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO reporting_entities VALUES ('entity-1','issuer','first')")
    decision = ReportingDocumentDecision(
        expected_document_id="expected",
        issuer_id="issuer",
        outcome="governed_reporting",
        reason_code="governed_periodic_filing",
        document_family="operating_company_periodic",
        coverage_status="captured",
        document_version_id="document",
        reporting_entity_id=None,
    )

    before = reporting_entity_scope_rows(conn, (decision,))
    conn.execute("INSERT INTO reporting_entities VALUES ('entity-2','issuer','second')")
    after = reporting_entity_scope_rows(conn, (decision,))

    assert before != after
    assert len(before) == 1
    assert len(after) == 2


def test_read_set_binds_selected_blob_metadata_but_excludes_unrelated_blobs() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE evidence_document_versions (
            document_version_id TEXT PRIMARY KEY,
            blob_sha256 TEXT NOT NULL
        );
        CREATE TABLE evidence_content_blobs (
            sha256 TEXT PRIMARY KEY,
            media_type TEXT NOT NULL,
            byte_size INTEGER NOT NULL,
            recorded_at TEXT NOT NULL
        );
        INSERT INTO evidence_document_versions VALUES ('document','aaaaaaaa');
        INSERT INTO evidence_content_blobs VALUES
          ('aaaaaaaa','text/html',100,'2026-07-29T00:00:00+00:00'),
          ('bbbbbbbb','application/pdf',200,'2026-07-29T00:00:00+00:00');
        """
    )

    original = document_blob_scope_rows(conn, ("document",))
    conn.execute(
        "UPDATE evidence_content_blobs SET media_type='text/plain' WHERE sha256='aaaaaaaa'"
    )
    selected_change = document_blob_scope_rows(conn, ("document",))
    conn.execute("UPDATE evidence_content_blobs SET media_type='image/png' WHERE sha256='bbbbbbbb'")
    unrelated_change = document_blob_scope_rows(conn, ("document",))

    assert selected_change != original
    assert unrelated_change == selected_change


def test_bounded_all_returns_checkpoint_without_sealing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cutoff = datetime(2026, 7, 29, tzinfo=UTC)
    decision = ReportingDocumentDecision(
        expected_document_id="expected",
        issuer_id="issuer",
        outcome="governed_reporting",
        reason_code="governed_periodic_filing",
        document_family="operating_company_periodic",
        coverage_status="captured",
        document_version_id="document",
        reporting_entity_id="reporting",
    )
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE v_source_obligations_current (obligation_state TEXT NOT NULL)")
    conn.execute("INSERT INTO v_source_obligations_current VALUES ('required')")

    def scope_stub(
        *_args: object,
        **_kwargs: object,
    ) -> tuple[
        tuple[ReportingDocumentDecision, ...],
        dict[str, tuple[str, ...]],
        int,
    ]:
        return (decision,), {"issuer": ("document",)}, 0

    def input_stub(*_args: object, **_kwargs: object) -> str:
        return "a" * 64

    def created_stub(*_args: object, **_kwargs: object) -> int:
        return 1

    def bindings_stub(
        *_args: object,
        **_kwargs: object,
    ) -> tuple[int, dict[str, int]]:
        return 1, {}

    def derive_stub(*_args: object, **_kwargs: object) -> tuple[()]:
        return ()

    def obligation_rows_stub(
        *_args: object,
        **_kwargs: object,
    ) -> list[sqlite3.Row]:
        return []

    def totals_stub(*_args: object, **_kwargs: object) -> dict[str, int]:
        return {"total": 1, "applicable": 1, "not_applicable": 0}

    def zero_stub(*_args: object, **_kwargs: object) -> int:
        return 0

    def one_stub(*_args: object) -> int:
        return 1

    def output_stub(*_args: object, **_kwargs: object) -> str:
        return "b" * 64

    def must_not_seal(*_args: object) -> None:
        pytest.fail("bounded run must not seal snapshots")

    monkeypatch.setattr(
        population,
        "_document_scope",
        scope_stub,
    )
    monkeypatch.setattr(population, "_input_commitment", input_stub)
    monkeypatch.setattr(
        population,
        "_ensure_document_family_obligations",
        created_stub,
    )
    monkeypatch.setattr(
        population,
        "_ensure_expected_document_bindings",
        bindings_stub,
    )
    monkeypatch.setattr(population, "derive_obligations", derive_stub)
    monkeypatch.setattr(population, "_obligation_rows", obligation_rows_stub)
    monkeypatch.setattr(
        population,
        "_obligation_totals",
        totals_stub,
    )
    monkeypatch.setattr(population, "_sealed_disposition_count", zero_stub)
    monkeypatch.setattr(population, "_binding_count", one_stub)
    monkeypatch.setattr(population, "_processing_snapshot_count", zero_stub)
    monkeypatch.setattr(population, "_output_commitment", output_stub)
    monkeypatch.setattr(
        population,
        "_seal_complete_snapshots",
        must_not_seal,
    )

    preview = populate_document_processing(
        conn,
        DocumentProcessingPopulationRequest(
            cutoff_at=cutoff,
            operation_recorded_at=cutoff,
            max_obligations=1,
        ),
    )
    result = populate_document_processing(
        conn,
        DocumentProcessingPopulationRequest(
            cutoff_at=cutoff,
            operation_recorded_at=cutoff,
            apply=True,
            max_obligations=1,
            input_commitment_sha256=preview.input_commitment_sha256,
            plan_commitment_sha256=preview.plan_commitment_sha256,
        ),
    )

    assert result.checkpoint.bounded is True
    assert result.checkpoint.safe_to_seal is False
    assert result.checkpoint.remaining_obligation_count == 1
    assert result.processing_snapshot_count == 0


def test_request_accepts_later_operation_clock() -> None:
    cutoff = population.datetime(2026, 7, 29)
    request = DocumentProcessingPopulationRequest(
        cutoff_at=cutoff,
        operation_recorded_at=population.datetime(2026, 7, 30),
    )
    assert request.operation_recorded_at > request.cutoff_at


def test_document_verifier_ignores_snapshot_recorded_after_observation() -> None:
    cutoff = datetime(2026, 7, 29, tzinfo=UTC)
    recorded = cutoff + timedelta(hours=1)
    sha = "a" * 64
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE source_obligation_revisions (
            obligation_revision_id TEXT,obligation_key TEXT,revision INTEGER,
            issuer_id TEXT,reporting_entity_id TEXT,document_family TEXT,
            obligation_state TEXT,active_from TEXT,active_to TEXT,
            knowledge_at TEXT,recorded_at TEXT
        );
        CREATE TABLE document_processing_snapshot_headers (
            processing_snapshot_id TEXT,scope_sha256 TEXT,policy_sha256 TEXT,
            cutoff_at TEXT,recorded_at TEXT,scope_json TEXT DEFAULT '{}'
        );
        CREATE TABLE document_processing_snapshot_seals (
            processing_snapshot_id TEXT,member_set_sha256 TEXT,sealed_at TEXT
        );
        CREATE TABLE document_processing_snapshot_members (
            processing_snapshot_id TEXT,document_version_id TEXT
        );
        CREATE TABLE evidence_documents (
            document_version_id TEXT,issuer_id TEXT
        );
        CREATE VIEW v_evidence_document_versions_canonical AS
        SELECT document_version_id,issuer_id FROM evidence_documents;
        """
    )
    conn.execute(
        "INSERT INTO source_obligation_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "obligation",
            "obligation",
            1,
            "issuer",
            "entity",
            "operating_company_periodic",
            "required",
            cutoff.isoformat(),
            None,
            cutoff.isoformat(),
            cutoff.isoformat(),
        ),
    )
    conn.execute(
        "INSERT INTO document_processing_snapshot_headers "
        "(processing_snapshot_id,scope_sha256,policy_sha256,cutoff_at,recorded_at) VALUES (?,?,?,?,?)",
        ("snapshot", sha, sha, cutoff.isoformat(), recorded.isoformat()),
    )
    conn.execute(
        "INSERT INTO document_processing_snapshot_seals VALUES (?,?,?)",
        ("snapshot", sha, recorded.isoformat()),
    )
    conn.execute(
        "INSERT INTO document_processing_snapshot_members VALUES (?,?)",
        ("snapshot", "document"),
    )
    conn.execute("INSERT INTO evidence_documents VALUES (?,?)", ("document", "issuer"))

    before = verify_document_processing(
        conn,
        PopulationTemporalScope(
            knowledge_cutoff=cutoff,
            observed_through=cutoff,
        ),
    )
    after = verify_document_processing(
        conn,
        PopulationTemporalScope(
            knowledge_cutoff=cutoff,
            observed_through=recorded,
        ),
    )

    assert before.failed_count == 1
    assert after.materialized_count == 1


def test_failed_disposition_keeps_cursor_at_last_success() -> None:
    assert (
        retry_cursor_after_attempt(
            prior_cursor="obligation-1",
            attempted_id="obligation-2",
            succeeded=False,
        )
        == "obligation-1"
    )


@pytest.mark.parametrize("blocker", ["unresolved", "missing", "incomplete_inventory"])
def test_all_apply_preflights_snapshot_blockers_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
    blocker: str,
) -> None:
    cutoff = datetime(2026, 7, 29, tzinfo=UTC)
    captured = ReportingDocumentDecision(
        expected_document_id="expected-captured",
        issuer_id="issuer",
        outcome="governed_reporting",
        reason_code="governed_periodic_filing",
        document_family="operating_company_periodic",
        coverage_status="captured",
        document_version_id="document",
        reporting_entity_id="reporting",
    )
    decisions = [captured]
    incomplete_inventory_count = 0
    if blocker == "unresolved":
        decisions.append(
            ReportingDocumentDecision(
                expected_document_id="expected-unresolved",
                issuer_id="issuer",
                outcome="unresolved",
                reason_code="unclassified_ir_reporting_document",
                coverage_status="missing",
            )
        )
    elif blocker == "missing":
        decisions.append(
            ReportingDocumentDecision(
                expected_document_id="expected-missing",
                issuer_id="issuer",
                outcome="governed_reporting",
                reason_code="governed_periodic_filing",
                document_family="operating_company_periodic",
                coverage_status="missing",
                reporting_entity_id="reporting",
            )
        )
    else:
        incomplete_inventory_count = 1

    writes: list[str] = []

    def _scope_stub(
        *_args: object,
        **_kwargs: object,
    ) -> tuple[
        tuple[ReportingDocumentDecision, ...],
        dict[str, tuple[str, ...]],
        int,
    ]:
        return (
            tuple(decisions),
            {"issuer": ("document",)},
            incomplete_inventory_count,
        )

    def _input_stub(*_args: object, **_kwargs: object) -> str:
        return "a" * 64

    def _source_obligation_stub(*_args: object, **_kwargs: object) -> int:
        writes.append("source_obligation")
        return 1

    def _binding_stub(
        *_args: object,
        **_kwargs: object,
    ) -> tuple[int, dict[str, int]]:
        writes.append("binding")
        return 1, {}

    def _derive_stub(*_args: object, **_kwargs: object) -> tuple[()]:
        writes.append("processing_obligation")
        return ()

    def _obligation_rows_stub(
        *_args: object,
        **_kwargs: object,
    ) -> list[sqlite3.Row]:
        return []

    monkeypatch.setattr(
        population,
        "_document_scope",
        _scope_stub,
    )
    monkeypatch.setattr(population, "_input_commitment", _input_stub)
    monkeypatch.setattr(
        population,
        "_ensure_document_family_obligations",
        _source_obligation_stub,
    )
    monkeypatch.setattr(
        population,
        "_ensure_expected_document_bindings",
        _binding_stub,
    )
    monkeypatch.setattr(population, "derive_obligations", _derive_stub)
    monkeypatch.setattr(population, "_obligation_rows", _obligation_rows_stub)

    conn = sqlite3.connect(":memory:")
    try:
        with pytest.raises(ValueError, match="cannot seal document-processing snapshots"):
            populate_document_processing(
                conn,
                DocumentProcessingPopulationRequest(
                    cutoff_at=cutoff,
                    operation_recorded_at=cutoff,
                    apply=True,
                    phase="all",
                ),
            )
    finally:
        conn.close()

    assert writes == []


def test_existing_binding_requires_exact_immutable_replay() -> None:
    cutoff = datetime(2026, 7, 29, tzinfo=UTC)
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE source_obligation_revisions (
            obligation_revision_id TEXT PRIMARY KEY,
            obligation_key TEXT NOT NULL,
            revision INTEGER NOT NULL,
            issuer_id TEXT NOT NULL,
            reporting_entity_id TEXT,
            document_family TEXT NOT NULL,
            obligation_state TEXT NOT NULL,
            active_from TEXT NOT NULL,
            active_to TEXT,
            knowledge_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE expected_document_obligation_bindings (
            binding_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL,
            expected_document_id TEXT NOT NULL,
            source_obligation_revision_id TEXT NOT NULL,
            issuer_id TEXT NOT NULL,
            reporting_entity_id TEXT,
            document_family TEXT NOT NULL,
            canonical_binding_json TEXT NOT NULL,
            binding_sha256 TEXT NOT NULL,
            effective_at TEXT NOT NULL,
            knowledge_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO source_obligation_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "obligation-current",
            "obligation-key",
            1,
            "issuer",
            "reporting",
            "operating_company_periodic",
            "required",
            cutoff.isoformat(),
            None,
            cutoff.isoformat(),
            cutoff.isoformat(),
        ),
    )
    stale_payload = json.dumps(
        {
            "document_family": "continuous_disclosure",
            "expected_document_id": "expected",
            "issuer_id": "issuer",
            "reporting_entity_id": "reporting",
            "source_obligation_revision_id": "obligation-stale",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    conn.execute(
        "INSERT INTO expected_document_obligation_bindings VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "binding-stale",
            "binding-stale",
            "expected",
            "obligation-stale",
            "issuer",
            "reporting",
            "continuous_disclosure",
            stale_payload,
            population.hashlib.sha256(stale_payload.encode()).hexdigest(),
            cutoff.isoformat(),
            cutoff.isoformat(),
            cutoff.isoformat(),
        ),
    )
    decision = ReportingDocumentDecision(
        expected_document_id="expected",
        issuer_id="issuer",
        outcome="governed_reporting",
        reason_code="governed_periodic_filing",
        document_family="operating_company_periodic",
        coverage_status="captured",
        document_version_id="document",
        reporting_entity_id="reporting",
    )
    try:
        with pytest.raises(ValueError, match="binding replay changed immutable values"):
            ensure_expected_document_binding(
                conn,
                decision,
                cutoff,
                cutoff,
            )
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("source_kind", "document_type", "form_type", "family", "reason"),
    [
        (
            "sec_filing",
            "filing",
            "10-K",
            "operating_company_periodic",
            "governed_periodic_filing",
        ),
        (
            "sec_filing",
            "filing",
            "6-K",
            "continuous_disclosure",
            "governed_current_report",
        ),
        (
            "sec_filing",
            "filing",
            "8-K/A",
            "continuous_disclosure",
            "governed_current_report",
        ),
        (
            "ir_document",
            "financial_statement",
            None,
            "issuer_financial_statements",
            "governed_ir_reporting_document",
        ),
        (
            "ir_document",
            "supplement",
            "IR",
            "issuer_financial_statements",
            "governed_ir_reporting_document",
        ),
        (
            "ir_document",
            "presentation",
            "IR",
            "issuer_presentations",
            "governed_ir_reporting_document",
        ),
        (
            "ir_document",
            "press_release",
            "IR",
            "issuer_earnings_materials",
            "governed_ir_reporting_document",
        ),
        (
            "earnings_call",
            "transcript",
            None,
            "issuer_earnings_materials",
            "governed_earnings_call_transcript",
        ),
    ],
)
def test_governed_reporting_document_policy_is_closed(
    source_kind: str,
    document_type: str,
    form_type: str | None,
    family: str,
    reason: str,
) -> None:
    outcome, actual_family, actual_reason = classify_reporting_document(
        source_kind=source_kind,
        document_type=document_type,
        form_type=form_type,
    )

    assert outcome == "governed_reporting"
    assert actual_family == family
    assert actual_reason == reason


def test_sec_supporting_assets_remain_inventory_but_leave_reporting_surface() -> None:
    outcome, family, reason = classify_reporting_document(
        source_kind="sec_filing",
        document_type="filing_attachment",
        form_type="10-K",
    )

    assert outcome == "excluded_supporting"
    assert family is None
    assert reason == "sec_supporting_artifact"


def test_generated_xbrl_report_pages_do_not_duplicate_primary_filing_text() -> None:
    outcome, family, reason = classify_reporting_document(
        source_kind="sec_filing",
        document_type="sec_financial_report",
        form_type="10-K",
    )

    assert outcome == "excluded_supporting"
    assert family is None
    assert reason == "sec_xbrl_report_attachment"


def test_ambiguous_ir_artifact_blocks_instead_of_silent_inclusion_or_exclusion() -> None:
    outcome, family, reason = classify_reporting_document(
        source_kind="ir_document",
        document_type="ir_document",
        form_type="IR",
    )

    assert outcome == "unresolved"
    assert family is None
    assert reason == "unclassified_ir_reporting_document"


@pytest.mark.parametrize("form", ["10-12B", "10-12B/A"])
def test_registration_financial_processing_is_a_governed_registration_package(form: str) -> None:
    assert classify_reporting_document(
        source_kind="sec_filing", document_type="filing", form_type=form
    ) == (
        "governed_reporting",
        "issuer_financial_statements",
        "governed_registration_financial_package",
    )
    assert classify_reporting_document(
        source_kind="sec_filing", document_type="filing", form_type="10-12G"
    ) == ("excluded_supporting", None, "sec_form_outside_reporting_policy")


def test_registration_processing_duty_does_not_reuse_publisher_duty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from provenance.reporting_entity_registry import (
        PersistResult,
        RegistryRecord,
        ReportingEntityRegistry,
        SourceObligationRevision,
    )

    cutoff = datetime(2026, 10, 4, tzinfo=UTC)
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE issuer_entities (issuer_id TEXT,entity_kind TEXT);"
        "CREATE TABLE source_obligation_revisions (obligation_revision_id TEXT,"
        "obligation_key TEXT,revision INTEGER,issuer_id TEXT,reporting_entity_id TEXT,"
        "document_family TEXT,obligation_state TEXT,active_from TEXT,active_to TEXT,"
        "knowledge_at TEXT,recorded_at TEXT,authority_kind TEXT,completeness_rule TEXT);"
    )
    conn.execute("INSERT INTO issuer_entities VALUES ('issuer','operating_company')")
    conn.execute(
        "INSERT INTO source_obligation_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "publisher",
            "publisher",
            1,
            "issuer",
            "reporting",
            "issuer_financial_statements",
            "required",
            cutoff.isoformat(),
            None,
            cutoff.isoformat(),
            cutoff.isoformat(),
            "issuer_publisher",
            "publisher_surface_exhaustion",
        ),
    )
    written: list[SourceObligationRevision] = []

    def capture(_self: ReportingEntityRegistry, record: RegistryRecord) -> PersistResult:
        assert isinstance(record, SourceObligationRevision)
        written.append(record)
        return PersistResult(record_id=record.obligation_revision_id, created=True)

    monkeypatch.setattr(ReportingEntityRegistry, "persist", capture)
    decision = ReportingDocumentDecision(
        expected_document_id="expected-registration",
        issuer_id="issuer",
        outcome="governed_reporting",
        reason_code="governed_registration_financial_package",
        document_family="issuer_financial_statements",
        coverage_status="captured",
        document_version_id="document",
        reporting_entity_id="reporting",
    )
    try:
        assert ensure_document_family_obligations(conn, (decision,), cutoff, cutoff) == 1
        assert len(written) == 1
        assert written[0].authority_kind == "sec_edgar"
        assert written[0].document_family == "issuer_financial_statements"
        assert written[0].completeness_rule == "regulator_inventory"
        assert written[0].obligation_key == "reporting:sec_edgar:issuer_financial_statements"
        conn.execute("UPDATE issuer_entities SET entity_kind='fund'")
        with pytest.raises(ValueError, match="requires_operating_company"):
            ensure_document_family_obligations(conn, (decision,), cutoff, cutoff)
        assert len(written) == 1
    finally:
        conn.close()


def _source_binding_fixture(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> tuple[sqlite3.Connection, ReportingDocumentDecision, datetime]:
    stamp = datetime(2026, 7, 29, 12, 0, 0, 123456, tzinfo=UTC)
    conn = sqlite3.connect(migrated_db(tmp_path / "coverage-binding.db", target="head"))
    conn.execute("PRAGMA foreign_keys=ON")
    register_sqlite_integrity_functions(conn)
    IssuerRegistry(conn).persist(
        IssuerEntity(
            issuer_id="binding-issuer",
            idempotency_key="binding-issuer",
            entity_kind="operating_company",
            created_at=stamp,
        )
    )
    registry = ReportingEntityRegistry(conn)
    registry.persist(
        ReportingEntity(
            reporting_entity_id="binding-reporting",
            idempotency_key="binding-reporting",
            issuer_id="binding-issuer",
            reporting_entity_kind="legal_registrant",
            display_name="Synthetic binding fixture",
            created_at=stamp,
        )
    )
    registry.persist(
        SourceObligationRevision(
            obligation_revision_id="binding-obligation",
            idempotency_key="binding-obligation",
            obligation_key="binding-obligation",
            revision=1,
            issuer_id="binding-issuer",
            reporting_entity_id="binding-reporting",
            authority_kind="issuer_publisher",
            document_family="issuer_presentations",
            obligation_state="required",
            completeness_rule="publisher_surface_exhaustion",
            active_from=stamp,
            active_to=None,
            decision_kind="deterministic",
            reason_code="synthetic_binding",
            reason_details=(("fixture", "one immutable presentation binding"),),
            effective_at=stamp,
            knowledge_at=stamp,
            recorded_at=stamp,
        )
    )
    evidence = EvidenceLedger(conn)
    evidence.persist(
        ContentBlob(
            sha256="a" * 64,
            byte_size=1,
            media_type="text/plain",
            storage_uri="https://synthetic.invalid/inventory",
            recorded_at=stamp,
        )
    )
    evidence.persist(
        SourceObservation(
            observation_id="binding-observation",
            idempotency_key="binding-observation",
            source_kind="issuer",
            source_url="https://synthetic.invalid/inventory",
            blob_sha256="a" * 64,
            source_published_at=stamp,
            filing_at=None,
            accepted_at=None,
            observed_at=stamp,
            retrieved_at=stamp,
            retrieval_config_sha256="b" * 64,
            collector_code_version="synthetic-binding@1",
        )
    )
    ledger = SourceCoverageLedger(conn)
    ledger.persist(
        SourceInventorySnapshot(
            snapshot_id="binding-inventory",
            idempotency_key="binding-inventory",
            inventory_key="binding-inventory",
            revision=1,
            issuer_id="binding-issuer",
            source_kind="ir_crawl",
            source_url="https://synthetic.invalid/inventory",
            source_observation_id="binding-observation",
            outcome="succeeded",
            authoritative=True,
            retrieval_config_sha256="b" * 64,
            collector_code_version="synthetic-binding@1",
            started_at=stamp,
            completed_at=stamp,
            recorded_at=stamp,
        )
    )
    ledger.persist(
        ExpectedDocument(
            expected_document_id="binding-expected",
            idempotency_key="binding-expected",
            snapshot_id="binding-inventory",
            expected_document_key="binding-presentation",
            issuer_id="binding-issuer",
            source_kind="ir_document",
            document_type="investor_presentation",
            form_type="presentation",
            expectation_basis="authoritative",
            recorded_at=stamp,
        )
    )
    return (
        conn,
        ReportingDocumentDecision(
            expected_document_id="binding-expected",
            issuer_id="binding-issuer",
            outcome="governed_reporting",
            reason_code="governed_ir_reporting_document",
            document_family="issuer_presentations",
            coverage_status="captured",
            document_version_id="binding-document",
            reporting_entity_id="binding-reporting",
        ),
        stamp,
    )


def _binding_row(conn: sqlite3.Connection) -> tuple[object, ...]:
    return tuple(conn.execute("SELECT * FROM expected_document_obligation_bindings").fetchone())


def test_source_coverage_binding_replays_at_original_clock_in_later_population(
    tmp_path: Path, migrated_db: Callable[..., Path], caplog: pytest.LogCaptureFixture
) -> None:
    conn, decision, stamp = _source_binding_fixture(tmp_path, migrated_db)
    logger = logging.getLogger(population.__name__)
    logger.addHandler(caplog.handler)
    try:
        before = _binding_row(conn)
        assert before[0] == "expected-obligation-binding:" + sha256_joined_nul(
            decision.expected_document_id, "binding-obligation"
        )
        # SQLite's datetime adapter writes a space separator; requests use ISO T.
        assert " " in str(before[9])
        assert not ensure_expected_document_binding(conn, decision, stamp, stamp)
        cutoff = stamp + timedelta(days=1)
        recorded = cutoff + timedelta(hours=2)
        caplog.set_level("INFO", logger=population.__name__)
        assert not ensure_expected_document_binding(conn, decision, cutoff, recorded)
        assert caplog.records[-1].msg == {
            "event": "expected_document_binding_replay",
            "legacy_binding_id": False,
            "source_recording_clock_replay": True,
        }
        assert _binding_row(conn) == before
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        logger.removeHandler(caplog.handler)
        conn.close()


_BINDING_MUTATIONS = (
    (
        "UPDATE expected_document_obligation_bindings SET binding_id=?1,idempotency_key=?1",
        "expected-obligation-binding:" + "0" * 64,
    ),
    ("UPDATE expected_document_obligation_bindings SET binding_id=?", "arbitrary-binding"),
    ("UPDATE expected_document_obligation_bindings SET idempotency_key=?", "arbitrary-key"),
    ("UPDATE expected_document_obligation_bindings SET issuer_id=?", "other-issuer"),
    ("UPDATE expected_document_obligation_bindings SET reporting_entity_id=?", "other-entity"),
    ("UPDATE expected_document_obligation_bindings SET document_family=?", "continuous_disclosure"),
    (
        "UPDATE expected_document_obligation_bindings SET source_obligation_revision_id=?",
        "other-duty",
    ),
    ("UPDATE expected_document_obligation_bindings SET canonical_binding_json=?", "{}"),
    (
        "UPDATE expected_document_obligation_bindings SET canonical_binding_json=?1,"
        "binding_sha256=fact_sha256(?1)",
        "{}",
    ),
    ("UPDATE expected_document_obligation_bindings SET binding_sha256=?", "c" * 64),
    ("UPDATE expected_document_obligation_bindings SET effective_at=?", "nearby-clock"),
    ("UPDATE expected_document_obligation_bindings SET knowledge_at=?", "nearby-clock"),
    ("UPDATE expected_document_obligation_bindings SET recorded_at=?", "nearby-clock"),
)


@pytest.mark.parametrize(("statement", "mutation"), _BINDING_MUTATIONS)
def test_binding_replay_rejects_changed_identity_payload_or_each_clock(
    tmp_path: Path, migrated_db: Callable[..., Path], statement: str, mutation: str
) -> None:
    source, decision, stamp = _source_binding_fixture(tmp_path, migrated_db)
    conn = sqlite3.connect(":memory:")
    try:
        source.commit()
        source.backup(conn)
        register_sqlite_integrity_functions(conn)
        # Inject damaged storage only into a disposable copy, independently of
        # the real source ledger's append-only trigger and clock CHECK guard.
        conn.execute("DROP TRIGGER trg_expected_document_obligation_bindings_append_only")
        conn.execute("PRAGMA ignore_check_constraints=ON")
        value = (
            (stamp + timedelta(microseconds=1)).isoformat()
            if mutation == "nearby-clock"
            else mutation
        )
        conn.execute(statement, (value,))
        before = _binding_row(conn)
        with pytest.raises(ValueError, match="binding replay changed immutable values"):
            ensure_expected_document_binding(
                conn, decision, stamp + timedelta(days=1), stamp + timedelta(days=2)
            )
        assert _binding_row(conn) == before
        assert _binding_row(source) != before
    finally:
        conn.close()
        source.close()


def test_legacy_binding_retains_exact_historical_operation_clocks_and_id(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    source, decision, stamp = _source_binding_fixture(tmp_path, migrated_db)
    conn = sqlite3.connect(":memory:")
    try:
        source.commit()
        source.backup(conn)
        register_sqlite_integrity_functions(conn)
        conn.execute("DROP TRIGGER trg_expected_document_obligation_bindings_append_only")
        legacy_id = "expected-obligation-binding:" + sha256_joined_unit_sep(
            decision.expected_document_id, "binding-obligation"
        )
        cutoff = stamp + timedelta(days=1)
        recorded = cutoff + timedelta(hours=2)
        conn.execute(
            "UPDATE expected_document_obligation_bindings SET binding_id=?,idempotency_key=?,"
            "effective_at=?,knowledge_at=?,recorded_at=?",
            (legacy_id, legacy_id, cutoff.isoformat(), cutoff.isoformat(), recorded.isoformat()),
        )
        before = _binding_row(conn)
        assert not ensure_expected_document_binding(conn, decision, cutoff, recorded)
        assert _binding_row(conn) == before
        # A different old request cannot be inferred from clock ordering alone.
        with pytest.raises(ValueError, match="binding replay changed immutable values"):
            ensure_expected_document_binding(
                conn, decision, cutoff + timedelta(days=1), recorded + timedelta(days=1)
            )
        assert _binding_row(conn) == before
    finally:
        conn.close()
        source.close()


def test_source_binding_clock_visibility_and_equivalent_offsets_are_exact(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, decision, stamp = _source_binding_fixture(tmp_path, migrated_db)
    try:
        before = _binding_row(conn)
        offset = timezone(timedelta(hours=7))
        assert not ensure_expected_document_binding(
            conn, decision, stamp.astimezone(offset), stamp.astimezone(offset)
        )
        with pytest.raises(ValueError, match="binding replay changed immutable values"):
            ensure_expected_document_binding(
                conn, decision, stamp - timedelta(microseconds=1), stamp + timedelta(days=1)
            )
        assert _binding_row(conn) == before
    finally:
        conn.close()


def test_new_population_binding_uses_source_identity_and_preserves_operation_clocks(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    source, decision, stamp = _source_binding_fixture(tmp_path, migrated_db)
    conn = sqlite3.connect(":memory:")
    try:
        source.commit()
        source.backup(conn)
        register_sqlite_integrity_functions(conn)
        # Model a historical unbound expectation only in this disposable copy.
        conn.execute("DROP TRIGGER trg_expected_document_obligation_bindings_append_only_delete")
        conn.execute("DELETE FROM expected_document_obligation_bindings")
        cutoff = stamp + timedelta(days=1)
        recorded = cutoff + timedelta(hours=2)
        assert ensure_expected_document_binding(conn, decision, cutoff, recorded)
        before = _binding_row(conn)
        assert before[0] == "expected-obligation-binding:" + sha256_joined_nul(
            decision.expected_document_id, "binding-obligation"
        )
        assert before[9:] == (cutoff.isoformat(), cutoff.isoformat(), recorded.isoformat())
        assert not ensure_expected_document_binding(conn, decision, cutoff, recorded)
        assert _binding_row(conn) == before
        # New IDs retain the old population writer's exact-operation replay rule.
        with pytest.raises(ValueError, match="binding replay changed immutable values"):
            ensure_expected_document_binding(
                conn, decision, cutoff + timedelta(days=1), recorded + timedelta(days=1)
            )
        assert _binding_row(conn) == before
    finally:
        conn.close()
        source.close()
