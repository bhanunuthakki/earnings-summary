# pyright: reportPrivateUsage=false
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

import provenance.population_document_processing as population
from provenance.population_completeness import PopulationTemporalScope
from provenance.population_document_processing import (
    DocumentProcessingPopulationRequest,
    ReportingDocumentDecision,
    classify_reporting_document,
    populate_document_processing,
    verify_document_processing,
)


def test_resume_apply_requires_dry_run_commitments() -> None:
    with pytest.raises(ValidationError, match="commitments"):
        DocumentProcessingPopulationRequest(
            cutoff_at=population.datetime(2026, 7, 29),
            operation_recorded_at=population.datetime(2026, 7, 29),
            apply=True,
            after_processing_obligation_revision_id="obligation-1",
        )


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
