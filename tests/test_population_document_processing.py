# pyright: reportPrivateUsage=false
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from pydantic import ValidationError

import provenance.population_document_processing as population
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
            "request_sha256": population._model_sha(forged_request),
            "result_sha256": population._model_sha(forged_result),
            "operation_id": population.document_processing_operation_id(
                database_instance_id=valid.database_instance_id,
                request=forged_request,
                admission_receipt_sha256=valid.admission_receipt_sha256,
                prior_checkpoint_receipt_sha256=valid.prior_checkpoint_receipt_sha256,
            ),
        }
    )
    forged = forged.model_copy(update={"receipt_sha256": population._document_receipt_sha(forged)})

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
    checkpoint = population._document_checkpoint(
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
    checkpoint = population._document_checkpoint(
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
        population._seal_complete_snapshots(
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
        population._seal_complete_snapshots(
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

    before = population._reporting_entity_scope_rows(conn, (decision,))
    conn.execute("INSERT INTO reporting_entities VALUES ('entity-2','issuer','second')")
    after = population._reporting_entity_scope_rows(conn, (decision,))

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

    original = population._document_blob_scope_rows(conn, ("document",))
    conn.execute(
        "UPDATE evidence_content_blobs SET media_type='text/plain' WHERE sha256='aaaaaaaa'"
    )
    selected_change = population._document_blob_scope_rows(conn, ("document",))
    conn.execute("UPDATE evidence_content_blobs SET media_type='image/png' WHERE sha256='bbbbbbbb'")
    unrelated_change = population._document_blob_scope_rows(conn, ("document",))

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

    def input_stub(*_args: object) -> str:
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

    def zero_stub(*_args: object) -> int:
        return 0

    def one_stub(*_args: object) -> int:
        return 1

    def output_stub(*_args: object) -> str:
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
            cutoff_at TEXT,recorded_at TEXT
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
        "INSERT INTO document_processing_snapshot_headers VALUES (?,?,?,?,?)",
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
        population._retry_cursor_after_attempt(
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
            population._ensure_expected_document_binding(
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
