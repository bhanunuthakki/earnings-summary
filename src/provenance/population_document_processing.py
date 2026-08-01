"""Close every governed document-processing obligation at one exact cutoff."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from typing import Literal, Self, cast

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from provenance.document_processing_evidence import (
    publish_document_processing_evidence,
    verify_document_processing_evidence,
)
from provenance.population_completeness import (
    PopulationArtifactSetCommitment,
    PopulationPlaneVerification,
    PopulationTemporalScope,
    canonical_json,
    digest_text,
    stream_population_artifact_set,
)
from provenance.reporting_entity_registry import (
    DocumentFamily,
    ReportingEntityRegistry,
    SourceObligationRevision,
)
from provenance.research_snapshot import (
    DocumentProcessingDisposition,
    DocumentProcessingPolicy,
    DocumentProcessingScope,
    ProcessingEvidenceReference,
    derive_obligations,
    record_disposition,
    seal_disposition,
    seal_processing_snapshot,
)

_POLICY = DocumentProcessingPolicy(
    policy_name="complete_reporting_document_processing",
    policy_version="1",
    include_optional_source_obligations=True,
)
_DOCUMENT_SELECTION_POLICY = "document-processing-terminal-at-k-observed-through-o.v1"
_REPORTING_FAMILIES = (
    "annual_securities_report",
    "continuous_disclosure",
    "investment_company_periodic",
    "issuer_earnings_materials",
    "issuer_financial_statements",
    "issuer_presentations",
    "operating_company_periodic",
)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DocumentProcessingPopulationRequest(_FrozenModel):
    cutoff_at: datetime
    operation_recorded_at: datetime = Field(
        validation_alias=AliasChoices("operation_recorded_at", "recorded_at")
    )
    apply: bool = False
    phase: Literal["obligations", "dispositions", "snapshots", "all"] = "all"
    after_processing_obligation_revision_id: str | None = None
    max_obligations: int | None = Field(default=None, ge=1)
    input_commitment_sha256: str | None = None
    plan_commitment_sha256: str | None = None

    @field_validator("input_commitment_sha256", "plan_commitment_sha256")
    @classmethod
    def _commitment_sha(cls, value: str | None) -> str | None:
        if value is not None and (
            len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError("population commitment must be a lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def _commitment_contract(self) -> Self:
        if (self.input_commitment_sha256 is None) != (self.plan_commitment_sha256 is None):
            raise ValueError("population commitments must be supplied together")
        bounded = (
            self.after_processing_obligation_revision_id is not None
            or self.max_obligations is not None
        )
        if self.apply and bounded and self.input_commitment_sha256 is None:
            raise ValueError("a bounded or resumed apply requires population commitments")
        if bounded and self.phase == "snapshots":
            raise ValueError("bounded population cannot enter the snapshot sealing phase")
        return self


class DocumentProcessingCheckpoint(_FrozenModel):
    bounded: bool
    safe_to_seal: bool
    last_processing_obligation_revision_id: str | None
    processed_obligation_count: int = Field(ge=0)
    remaining_obligation_count: int = Field(ge=0)
    can_resume: bool


class DocumentProcessingPopulationResult(_FrozenModel):
    mode: Literal["dry_run", "apply"]
    phase: str
    expected_document_count: int
    missing_document_count: int
    excluded_document_count: int
    unresolved_document_count: int
    incomplete_inventory_count: int
    binding_count: int
    binding_created_count: int
    binding_failure_count: int
    selection_reason_counts: dict[str, int]
    source_obligation_count: int
    source_obligation_created_count: int
    expected_obligation_count: int
    applicable_obligation_count: int
    not_applicable_obligation_count: int
    sealed_disposition_count: int
    failed_obligation_count: int
    failed_reason_counts: dict[str, int]
    processed_obligation_count: int
    last_processing_obligation_revision_id: str | None
    expected_issuer_count: int
    processing_snapshot_count: int
    selection_commitment_sha256: str
    input_commitment_sha256: str
    plan_commitment_sha256: str
    output_commitment_sha256: str
    checkpoint: DocumentProcessingCheckpoint


class ReportingDocumentDecision(_FrozenModel):
    """One explicit keep, safe exclusion, or unresolved classification."""

    expected_document_id: str
    issuer_id: str
    outcome: Literal["governed_reporting", "excluded_supporting", "unresolved"]
    reason_code: str
    document_family: str | None = None
    coverage_status: str
    document_version_id: str | None = None
    reporting_entity_id: str | None = None


def verify_document_processing(
    conn: sqlite3.Connection,
    scope: PopulationTemporalScope,
) -> PopulationPlaneVerification:
    """Verify exact persisted processing snapshots at K as observed through O."""

    knowledge, observed = _utc(scope.knowledge_cutoff), _utc(scope.observed_through)
    expected = _expected_reporting_issuer_count(conn, knowledge, observed)
    common_query = (
        " FROM document_processing_snapshot_headers header "
        "JOIN document_processing_snapshot_seals seal "
        "ON seal.processing_snapshot_id=header.processing_snapshot_id "
        "WHERE datetime(header.cutoff_at)=datetime(?) "
        "AND datetime(header.recorded_at)<=datetime(?) "
        "AND datetime(seal.sealed_at)<=datetime(?) "
        "ORDER BY header.processing_snapshot_id"
    )
    scope_set = stream_population_artifact_set(
        conn,
        table="document_processing_snapshot_headers",
        query=(
            "SELECT header.processing_snapshot_id AS artifact_id,"
            "header.scope_sha256 AS payload_sha256,"
            "seal.member_set_sha256 AS seal_sha256,"
            "header.cutoff_at AS knowledge_at,"
            "seal.sealed_at AS recorded_at" + common_query
        ),
        params=(_db_time(knowledge), _db_time(observed), _db_time(observed)),
        selection_policy_id=_DOCUMENT_SELECTION_POLICY + ".scope",
    )
    policy_set = stream_population_artifact_set(
        conn,
        table="document_processing_snapshot_headers",
        query=(
            "SELECT header.processing_snapshot_id AS artifact_id,"
            "header.policy_sha256 AS payload_sha256,"
            "seal.member_set_sha256 AS seal_sha256,"
            "header.cutoff_at AS knowledge_at,"
            "seal.sealed_at AS recorded_at" + common_query
        ),
        params=(_db_time(knowledge), _db_time(observed), _db_time(observed)),
        selection_policy_id=_DOCUMENT_SELECTION_POLICY + ".policy",
    )
    if scope_set.row_count != policy_set.row_count:
        raise ValueError("document processing artifact commitments disagree")
    duplicate = conn.execute(
        "SELECT 1 FROM ("
        "SELECT document.issuer_id,header.processing_snapshot_id "
        "FROM document_processing_snapshot_headers header "
        "JOIN document_processing_snapshot_seals seal "
        "ON seal.processing_snapshot_id=header.processing_snapshot_id "
        "JOIN document_processing_snapshot_members member "
        "ON member.processing_snapshot_id=header.processing_snapshot_id "
        "JOIN v_evidence_document_versions_canonical document "
        "ON document.document_version_id=member.document_version_id "
        "WHERE datetime(header.cutoff_at)=datetime(?) "
        "AND datetime(header.recorded_at)<=datetime(?) "
        "AND datetime(seal.sealed_at)<=datetime(?) "
        "GROUP BY document.issuer_id,header.processing_snapshot_id"
        ") GROUP BY issuer_id HAVING COUNT(*)<>1 LIMIT 1",
        (_db_time(knowledge), _db_time(observed), _db_time(observed)),
    ).fetchone()
    if duplicate is not None:
        raise ValueError("document processing artifact scope is ambiguous at K,O")
    return _document_plane_verification(
        scope=scope,
        expected=expected,
        artifacts=tuple(sorted((policy_set, scope_set), key=lambda item: item.selection_policy_id)),
    )


def populate_document_processing(
    conn: sqlite3.Connection,
    request: DocumentProcessingPopulationRequest,
) -> DocumentProcessingPopulationResult:
    """Prepare, close, and seal every positive source-coverage document."""

    cutoff = _utc(request.cutoff_at)
    recorded = _utc(request.operation_recorded_at)
    if recorded < cutoff:
        raise ValueError("document processing operation_recorded_at must not precede cutoff_at")
    original_row_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    conn.create_function(
        "fact_sha256",
        1,
        _sql_sha256,
        deterministic=True,
    )
    try:
        bounded = (
            request.after_processing_obligation_revision_id is not None
            or request.max_obligations is not None
        )
        decisions, documents_by_issuer, incomplete_inventories = _document_scope(
            conn,
            cutoff,
            recorded,
        )
        if not documents_by_issuer:
            raise ValueError("document processing requires a nonempty covered universe")
        input_sha = _input_commitment(conn, cutoff, recorded)
        selection_sha = _selection_commitment(decisions)
        plan_sha = _population_plan_commitment(request, input_sha, selection_sha)
        _verify_commitments(request, input_sha=input_sha, plan_sha=plan_sha)
        if request.apply and not bounded and request.phase in {"snapshots", "all"}:
            _raise_for_immutable_snapshot_blockers(
                decisions,
                incomplete_inventory_count=incomplete_inventories,
            )
        obligations_created = 0
        bindings_created = 0
        binding_failures: dict[str, int] = {}
        if request.apply and request.phase in {"obligations", "all"}:
            obligations_created = _ensure_document_family_obligations(
                conn,
                decisions,
                cutoff,
                recorded,
            )
            bindings_created, binding_failures = _ensure_expected_document_bindings(
                conn,
                decisions,
                cutoff,
                recorded,
            )
            for document_ids in documents_by_issuer.values():
                with conn:
                    derive_obligations(
                        conn,
                        DocumentProcessingScope(document_version_ids=document_ids),
                        cutoff,
                        _POLICY,
                        observed_through=recorded,
                        recorded_at=recorded,
                    )
        obligations = _obligation_rows(
            conn,
            cutoff,
            recorded,
            document_version_ids=tuple(
                sorted(
                    document_id
                    for document_ids in documents_by_issuer.values()
                    for document_id in document_ids
                )
            ),
            after=request.after_processing_obligation_revision_id,
            limit=request.max_obligations,
        )
        processed = 0
        last_id = request.after_processing_obligation_revision_id
        failures: dict[str, int] = dict(binding_failures)
        if request.apply and request.phase in {"dispositions", "all"}:
            for obligation in obligations:
                obligation_id = str(obligation["processing_obligation_revision_id"])
                if _has_sealed_disposition(conn, obligation_id, cutoff, recorded):
                    processed += 1
                    last_id = obligation_id
                    continue
                try:
                    _close_obligation(conn, obligation, cutoff, recorded)
                    conn.commit()
                except Exception as exc:
                    conn.rollback()
                    reason = _reason(exc)
                    failures[reason] = failures.get(reason, 0) + 1
                    break
                processed += 1
                last_id = _retry_cursor_after_attempt(
                    prior_cursor=last_id,
                    attempted_id=obligation_id,
                    succeeded=True,
                )
        if request.apply and not bounded and request.phase in {"snapshots", "all"}:
            governed_ids = tuple(
                item.expected_document_id
                for item in decisions
                if item.outcome == "governed_reporting"
            )
            missing_bindings = len(governed_ids) - _binding_count(conn, governed_ids)
            if binding_failures or missing_bindings:
                raise ValueError(
                    "cannot seal document-processing snapshots while source "
                    "inventory, classification, coverage, or binding blockers remain"
                )
            _seal_complete_snapshots(conn, documents_by_issuer, cutoff, recorded)
        scoped_document_ids = tuple(
            sorted(
                document_id
                for document_ids in documents_by_issuer.values()
                for document_id in document_ids
            )
        )
        totals = _obligation_totals(conn, cutoff, scoped_document_ids, recorded)
        sealed = _sealed_disposition_count(conn, cutoff, scoped_document_ids, recorded)
        failed = max(totals["total"] - sealed, 0)
        if failed and not failures:
            failures["unsealed_processing_obligation"] = failed
        selection_reasons: dict[str, int] = {}
        for decision in decisions:
            selection_reasons[decision.reason_code] = (
                selection_reasons.get(decision.reason_code, 0) + 1
            )
        missing = sum(
            item.outcome == "governed_reporting"
            and item.coverage_status not in {"captured", "extracted", "indexed"}
            for item in decisions
        )
        excluded = sum(item.outcome == "excluded_supporting" for item in decisions)
        unresolved = sum(item.outcome == "unresolved" for item in decisions)
        binding_count = _binding_count(
            conn,
            tuple(
                item.expected_document_id
                for item in decisions
                if item.outcome == "governed_reporting"
            ),
        )
        expected_bindings = sum(item.outcome == "governed_reporting" for item in decisions)
        binding_failure_count = max(expected_bindings - binding_count, 0)
        checkpoint = _document_checkpoint(
            bounded=bounded,
            prior_cursor=last_id,
            processed=processed,
            total=totals["total"],
            sealed=sealed,
            blocker_count=(
                missing
                + unresolved
                + incomplete_inventories
                + binding_failure_count
                + sum(failures.values())
            ),
        )
        return DocumentProcessingPopulationResult(
            mode="apply" if request.apply else "dry_run",
            phase=request.phase,
            expected_document_count=expected_bindings,
            missing_document_count=missing,
            excluded_document_count=excluded,
            unresolved_document_count=unresolved,
            incomplete_inventory_count=incomplete_inventories,
            binding_count=binding_count,
            binding_created_count=bindings_created,
            binding_failure_count=binding_failure_count,
            selection_reason_counts=dict(sorted(selection_reasons.items())),
            source_obligation_count=int(
                conn.execute(
                    "SELECT COUNT(*) FROM v_source_obligations_current "
                    "WHERE obligation_state IN ('required','optional')"
                ).fetchone()[0]
            ),
            source_obligation_created_count=obligations_created,
            expected_obligation_count=totals["total"],
            applicable_obligation_count=totals["applicable"],
            not_applicable_obligation_count=totals["not_applicable"],
            sealed_disposition_count=sealed,
            failed_obligation_count=failed,
            failed_reason_counts=dict(sorted(failures.items())),
            processed_obligation_count=processed,
            last_processing_obligation_revision_id=last_id,
            expected_issuer_count=len(documents_by_issuer),
            processing_snapshot_count=_processing_snapshot_count(conn, cutoff, recorded),
            selection_commitment_sha256=selection_sha,
            input_commitment_sha256=input_sha,
            plan_commitment_sha256=plan_sha,
            output_commitment_sha256=_output_commitment(
                conn,
                cutoff,
                decisions,
                recorded,
            ),
            checkpoint=checkpoint,
        )
    finally:
        conn.row_factory = original_row_factory


def _document_scope(
    conn: sqlite3.Connection,
    cutoff: datetime,
    observed_through: datetime,
) -> tuple[
    tuple[ReportingDocumentDecision, ...],
    dict[str, tuple[str, ...]],
    int,
]:
    rows = conn.execute(
        "SELECT expected.expected_document_id,expected.issuer_id,"
        "expected.source_kind,expected.document_type,expected.form_type,"
        "coverage.document_version_id,"
        "COALESCE(coverage.coverage_status,'unassessed'),"
        "canonical.reporting_entity_id,lifecycle.status,"
        "lifecycle.expected_document_id "
        "FROM expected_documents expected "
        "JOIN source_inventory_snapshots inventory "
        "ON inventory.snapshot_id=expected.snapshot_id "
        "LEFT JOIN expected_document_lifecycle_revisions lifecycle "
        "ON lifecycle.inventory_key=inventory.inventory_key "
        "AND lifecycle.expected_document_key=expected.expected_document_key "
        "AND datetime(lifecycle.knowledge_at)<=datetime(?) "
        "AND datetime(lifecycle.recorded_at)<=datetime(?) "
        "AND NOT EXISTS (SELECT 1 FROM expected_document_lifecycle_revisions newer_lifecycle "
        "WHERE newer_lifecycle.inventory_key=lifecycle.inventory_key "
        "AND newer_lifecycle.expected_document_key=lifecycle.expected_document_key "
        "AND newer_lifecycle.revision>lifecycle.revision "
        "AND datetime(newer_lifecycle.knowledge_at)<=datetime(?) "
        "AND datetime(newer_lifecycle.recorded_at)<=datetime(?)) "
        "LEFT JOIN source_coverage_assessments coverage "
        "ON coverage.expected_document_id=expected.expected_document_id "
        "AND datetime(coverage.knowledge_at)<=datetime(?) "
        "AND datetime(coverage.recorded_at)<=datetime(?) "
        "AND NOT EXISTS (SELECT 1 FROM source_coverage_assessments newer "
        "WHERE newer.expected_document_id=coverage.expected_document_id "
        "AND newer.revision>coverage.revision "
        "AND datetime(newer.knowledge_at)<=datetime(?) "
        "AND datetime(newer.recorded_at)<=datetime(?)) "
        "LEFT JOIN v_evidence_document_versions_canonical canonical "
        "ON canonical.document_version_id=coverage.document_version_id "
        "WHERE datetime(expected.recorded_at)<=datetime(?) "
        "ORDER BY expected.issuer_id,expected.expected_document_key",
        (
            _db_time(cutoff),
            _db_time(observed_through),
            _db_time(cutoff),
            _db_time(observed_through),
            _db_time(cutoff),
            _db_time(observed_through),
            _db_time(cutoff),
            _db_time(observed_through),
            _db_time(observed_through),
        ),
    ).fetchall()
    grouped: dict[str, list[str]] = {}
    decisions: list[ReportingDocumentDecision] = []
    for row in rows:
        lifecycle_status = None if row[8] is None else str(row[8])
        lifecycle_expected_id = None if row[9] is None else str(row[9])
        if lifecycle_status is None:
            outcome: Literal["governed_reporting", "excluded_supporting", "unresolved"] = (
                "unresolved"
            )
            family = None
            reason = "expected_document_lifecycle_missing"
        elif lifecycle_status != "expected" or lifecycle_expected_id != str(row[0]):
            outcome = "excluded_supporting"
            family = None
            reason = "expected_document_not_current"
        else:
            outcome, family, reason = classify_reporting_document(
                source_kind=str(row["source_kind"]),
                document_type=str(row["document_type"]),
                form_type=None if row["form_type"] is None else str(row["form_type"]),
            )
        coverage_status = str(row[6])
        document_version_id = None if row[5] is None else str(row[5])
        reporting_entity_id = None if row[7] is None else str(row[7])
        decision = ReportingDocumentDecision(
            expected_document_id=str(row[0]),
            issuer_id=str(row[1]),
            outcome=outcome,
            reason_code=reason,
            document_family=family,
            coverage_status=coverage_status,
            document_version_id=document_version_id,
            reporting_entity_id=reporting_entity_id,
        )
        decisions.append(decision)
        if outcome != "governed_reporting":
            continue
        if coverage_status not in {"captured", "extracted", "indexed"}:
            continue
        if document_version_id is None:
            raise ValueError("positive source coverage has no document version")
        if reporting_entity_id is None:
            raise ValueError("governed reporting document lacks a canonical reporting entity")
        grouped.setdefault(str(row[1]), []).append(document_version_id)
    incomplete_inventory_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM source_inventory_snapshots inventory "
            "WHERE datetime(inventory.recorded_at)<=datetime(?) "
            "AND NOT EXISTS (SELECT 1 FROM source_inventory_snapshots newer "
            "WHERE newer.inventory_key=inventory.inventory_key "
            "AND newer.revision>inventory.revision "
            "AND datetime(newer.recorded_at)<=datetime(?)) "
            "AND NOT EXISTS (SELECT 1 FROM v_source_inventory_sealed_complete complete "
            "WHERE complete.snapshot_id=inventory.snapshot_id)",
            (_db_time(observed_through), _db_time(observed_through)),
        ).fetchone()[0]
    )
    return (
        tuple(decisions),
        {
            issuer_id: tuple(sorted(set(document_ids)))
            for issuer_id, document_ids in sorted(grouped.items())
        },
        incomplete_inventory_count,
    )


def _close_obligation(
    conn: sqlite3.Connection,
    obligation: sqlite3.Row,
    cutoff: datetime,
    recorded_at: datetime,
) -> None:
    obligation_id = str(obligation["processing_obligation_revision_id"])
    disposition_id = "processing-disposition:" + _digest(obligation_id, _db_time(cutoff))
    if str(obligation["applicability"]) == "not_applicable":
        disposition = DocumentProcessingDisposition(
            processing_disposition_id=disposition_id,
            idempotency_key=disposition_id,
            processing_obligation_revision_id=obligation_id,
            terminal_status="not_applicable",
            reason_code="media_or_document_lane_not_applicable",
            reason_details={
                "policy_name": _POLICY.policy_name,
                "policy_version": _POLICY.policy_version,
            },
            knowledge_at=cutoff,
            recorded_at=recorded_at,
        )
    else:
        lane = str(obligation["processing_lane"])
        if lane == "filing_xbrl":
            reference = _filing_xbrl_reference(
                conn,
                str(obligation["document_version_id"]),
                cutoff,
                recorded_at,
            )
        else:
            receipt = publish_document_processing_evidence(
                conn,
                document_version_id=str(obligation["document_version_id"]),
                processing_lane=lane,
                cutoff_at=cutoff,
                recorded_at=recorded_at,
            )
            verified = verify_document_processing_evidence(
                conn,
                receipt.evidence_seal_id,
                document_version_id=receipt.document_version_id,
                processing_lane=receipt.processing_lane,
                cutoff_at=cutoff,
                observed_through=recorded_at,
            )
            reference = ProcessingEvidenceReference(
                evidence_table="document_processing_evidence_seals",
                evidence_id=verified.evidence_seal_id,
                evidence_commitment_sha256=verified.member_set_sha256,
                knowledge_at=verified.knowledge_at,
                recorded_at=verified.recorded_at,
            )
        disposition = DocumentProcessingDisposition(
            processing_disposition_id=disposition_id,
            idempotency_key=disposition_id,
            processing_obligation_revision_id=obligation_id,
            terminal_status="succeeded",
            reason_code="complete_native_lane_inventory_sealed",
            reason_details={
                "evidence_id": reference.evidence_id,
                "policy_name": _POLICY.policy_name,
                "policy_version": _POLICY.policy_version,
            },
            evidence=(reference,),
            knowledge_at=cutoff,
            recorded_at=recorded_at,
        )
    record_disposition(conn, disposition)
    seal_disposition(conn, disposition_id, sealed_at=recorded_at)


def _ensure_document_family_obligations(
    conn: sqlite3.Connection,
    decisions: tuple[ReportingDocumentDecision, ...],
    cutoff: datetime,
    recorded_at: datetime,
) -> int:
    registry = ReportingEntityRegistry(conn)
    created = 0
    scopes: set[tuple[str, str, str]] = set()
    for decision in decisions:
        if decision.outcome != "governed_reporting" or decision.document_family is None:
            continue
        entity_id = decision.reporting_entity_id
        if entity_id is None:
            entity_rows = conn.execute(
                "SELECT reporting_entity_id FROM reporting_entities "
                "WHERE issuer_id=? ORDER BY reporting_entity_id",
                (decision.issuer_id,),
            ).fetchall()
            if len(entity_rows) != 1:
                continue
            entity_id = str(entity_rows[0][0])
        scopes.add((decision.issuer_id, entity_id, decision.document_family))
    for issuer_id, reporting_entity_id, family in sorted(scopes):
        present = conn.execute(
            "SELECT 1 FROM source_obligation_revisions obligation "
            "WHERE issuer_id=? AND reporting_entity_id=? AND document_family=? "
            "AND obligation_state IN ('required','optional') "
            "AND datetime(active_from)<=datetime(?) "
            "AND (active_to IS NULL OR datetime(active_to)>datetime(?)) "
            "AND datetime(knowledge_at)<=datetime(?) "
            "AND datetime(recorded_at)<=datetime(?) "
            "AND NOT EXISTS (SELECT 1 FROM source_obligation_revisions newer "
            "WHERE newer.obligation_key=obligation.obligation_key "
            "AND newer.revision>obligation.revision "
            "AND datetime(newer.knowledge_at)<=datetime(?) "
            "AND datetime(newer.recorded_at)<=datetime(?)) LIMIT 1",
            (
                issuer_id,
                reporting_entity_id,
                family,
                _db_time(cutoff),
                _db_time(cutoff),
                _db_time(cutoff),
                _db_time(recorded_at),
                _db_time(cutoff),
                _db_time(recorded_at),
            ),
        ).fetchone()
        if present is not None:
            continue
        authority_kind = (
            "sec_edgar"
            if family in {"operating_company_periodic", "continuous_disclosure"}
            else "issuer_publisher"
        )
        completeness_rule = (
            "regulator_inventory"
            if authority_kind == "sec_edgar"
            else "publisher_surface_exhaustion"
        )
        obligation_key = f"{reporting_entity_id}:{authority_kind}:{family}"
        prior = conn.execute(
            "SELECT obligation_revision_id,revision "
            "FROM source_obligation_revisions WHERE obligation_key=? "
            "ORDER BY revision DESC LIMIT 1",
            (obligation_key,),
        ).fetchone()
        revision = 1 if prior is None else int(prior[1]) + 1
        supersedes = None if prior is None else str(prior[0])
        record_id = "source-obligation:" + _digest(obligation_key, str(revision))
        with conn:
            result = registry.persist(
                SourceObligationRevision(
                    obligation_revision_id=record_id,
                    idempotency_key=record_id,
                    obligation_key=obligation_key,
                    revision=revision,
                    issuer_id=issuer_id,
                    reporting_entity_id=reporting_entity_id,
                    authority_kind=authority_kind,
                    document_family=cast(DocumentFamily, family),
                    obligation_state="required",
                    completeness_rule=completeness_rule,
                    active_from=cutoff,
                    active_to=None,
                    decision_kind="deterministic",
                    reason_code="governed_reporting_document_family_present",
                    reason_details=(
                        (
                            "population_policy",
                            "complete_reporting_document_processing@1",
                        ),
                    ),
                    effective_at=cutoff,
                    knowledge_at=cutoff,
                    recorded_at=recorded_at,
                    supersedes_obligation_revision_id=supersedes,
                )
            )
        created += int(result.created)
    return created


def _ensure_expected_document_bindings(
    conn: sqlite3.Connection,
    decisions: tuple[ReportingDocumentDecision, ...],
    cutoff: datetime,
    recorded_at: datetime,
) -> tuple[int, dict[str, int]]:
    created = 0
    failures: dict[str, int] = {}
    for decision in decisions:
        if decision.outcome != "governed_reporting" or decision.document_family is None:
            continue
        try:
            created += int(
                _ensure_expected_document_binding(
                    conn,
                    decision,
                    cutoff,
                    recorded_at,
                )
            )
        except (ValueError, sqlite3.Error) as exc:
            reason = _reason(exc)
            failures[reason] = failures.get(reason, 0) + 1
    return created, failures


def _ensure_expected_document_binding(
    conn: sqlite3.Connection,
    decision: ReportingDocumentDecision,
    cutoff: datetime,
    recorded_at: datetime,
) -> bool:
    parameters: list[object] = [
        decision.issuer_id,
        decision.document_family,
        _db_time(cutoff),
        _db_time(cutoff),
        _db_time(cutoff),
        _db_time(recorded_at),
    ]
    entity_sql = ""
    if decision.reporting_entity_id is not None:
        entity_sql = "AND reporting_entity_id=? "
        parameters.append(decision.reporting_entity_id)
    rows = conn.execute(
        "SELECT obligation_revision_id,reporting_entity_id "
        "FROM source_obligation_revisions obligation "
        "WHERE issuer_id=? AND document_family=? "
        "AND obligation_state IN ('required','optional') "
        "AND datetime(active_from)<=datetime(?) "
        "AND (active_to IS NULL OR datetime(active_to)>datetime(?)) "
        "AND datetime(knowledge_at)<=datetime(?) "
        "AND datetime(recorded_at)<=datetime(?) "
        f"{entity_sql}"  # nosec B608 -- fixed optional predicate; values are bound
        "AND NOT EXISTS (SELECT 1 FROM source_obligation_revisions newer "
        "WHERE newer.obligation_key=obligation.obligation_key "
        "AND newer.revision>obligation.revision "
        "AND datetime(newer.knowledge_at)<=datetime(?) "
        "AND datetime(newer.recorded_at)<=datetime(?)) "
        "ORDER BY obligation_revision_id",
        (*parameters, _db_time(cutoff), _db_time(recorded_at)),
    ).fetchall()
    if len(rows) != 1 or rows[0][1] is None:
        raise ValueError("expected_document_binding_missing_or_ambiguous_obligation")
    obligation_revision_id = str(rows[0][0])
    reporting_entity_id = str(rows[0][1])
    payload = {
        "document_family": decision.document_family,
        "expected_document_id": decision.expected_document_id,
        "issuer_id": decision.issuer_id,
        "reporting_entity_id": reporting_entity_id,
        "source_obligation_revision_id": obligation_revision_id,
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    binding_id = "expected-obligation-binding:" + _digest(
        decision.expected_document_id,
        obligation_revision_id,
    )
    values = (
        binding_id,
        binding_id,
        decision.expected_document_id,
        obligation_revision_id,
        decision.issuer_id,
        reporting_entity_id,
        decision.document_family,
        canonical,
        hashlib.sha256(canonical.encode()).hexdigest(),
        _db_time(cutoff),
        _db_time(cutoff),
        _db_time(recorded_at),
    )
    existing = conn.execute(
        "SELECT binding_id,idempotency_key,expected_document_id,"
        "source_obligation_revision_id,issuer_id,reporting_entity_id,"
        "document_family,canonical_binding_json,binding_sha256,"
        "effective_at,knowledge_at,recorded_at "
        "FROM expected_document_obligation_bindings WHERE expected_document_id=?",
        (decision.expected_document_id,),
    ).fetchone()
    if existing is not None:
        if tuple(existing) != values:
            raise ValueError("expected document binding replay changed immutable values")
        return False
    with conn:
        conn.execute(
            "INSERT INTO expected_document_obligation_bindings "
            "(binding_id,idempotency_key,expected_document_id,"
            "source_obligation_revision_id,issuer_id,reporting_entity_id,"
            "document_family,canonical_binding_json,binding_sha256,"
            "effective_at,knowledge_at,recorded_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            values,
        )
    return True


def _raise_for_immutable_snapshot_blockers(
    decisions: tuple[ReportingDocumentDecision, ...],
    *,
    incomplete_inventory_count: int,
) -> None:
    unresolved = sum(item.outcome == "unresolved" for item in decisions)
    missing = sum(
        item.outcome == "governed_reporting"
        and item.coverage_status not in {"captured", "extracted", "indexed"}
        for item in decisions
    )
    if unresolved or missing or incomplete_inventory_count:
        raise ValueError(
            "cannot seal document-processing snapshots while source "
            "inventory, classification, coverage, or binding blockers remain"
        )


def classify_reporting_document(
    *,
    source_kind: str,
    document_type: str,
    form_type: str | None,
) -> tuple[
    Literal["governed_reporting", "excluded_supporting", "unresolved"],
    str | None,
    str,
]:
    """Classify the governed investor-reporting surface without deleting inventory."""

    source = source_kind.strip().lower()
    document = document_type.strip().lower()
    form = (form_type or "").strip().upper()
    if source == "sec_filing":
        if document == "sec_financial_report":
            return "excluded_supporting", None, "sec_xbrl_report_attachment"
        if document != "filing":
            return "excluded_supporting", None, "sec_supporting_artifact"
        if form in {
            "10-K",
            "10-K/A",
            "10-Q",
            "10-Q/A",
            "20-F",
            "20-F/A",
            "40-F",
            "40-F/A",
        }:
            return (
                "governed_reporting",
                "operating_company_periodic",
                "governed_periodic_filing",
            )
        if form in {"6-K", "6-K/A", "8-K", "8-K/A"}:
            return (
                "governed_reporting",
                "continuous_disclosure",
                "governed_current_report",
            )
        return "excluded_supporting", None, "sec_form_outside_reporting_policy"
    if source == "earnings_call":
        if document in {
            "earnings_call",
            "earnings_call_transcript",
            "earnings_transcript",
            "transcript",
        }:
            return (
                "governed_reporting",
                "issuer_earnings_materials",
                "governed_earnings_call_transcript",
            )
        return "unresolved", None, "unclassified_earnings_call_artifact"
    if source != "ir_document":
        return "unresolved", None, "unknown_expected_document_source_kind"
    ir_families = {
        "annual_report": "issuer_financial_statements",
        "financial_statement": "issuer_financial_statements",
        "supplement": "issuer_financial_statements",
        "earnings_material": "issuer_earnings_materials",
        "earnings_release": "issuer_earnings_materials",
        "press_release": "issuer_earnings_materials",
        "earnings_transcript": "issuer_earnings_materials",
        "transcript": "issuer_earnings_materials",
        "investor_presentation": "issuer_presentations",
        "presentation": "issuer_presentations",
        "investor_update": "issuer_presentations",
    }
    family = ir_families.get(document)
    if family is None:
        return "unresolved", None, "unclassified_ir_reporting_document"
    return "governed_reporting", family, "governed_ir_reporting_document"


def _filing_xbrl_reference(
    conn: sqlite3.Connection,
    document_version_id: str,
    cutoff: datetime,
    observed_through: datetime,
) -> ProcessingEvidenceReference:
    rows = conn.execute(
        "SELECT seal.disposition_seal_id,seal.disposition_set_sha256,"
        "seal.knowledge_at,seal.recorded_at "
        "FROM filing_xbrl_extraction_disposition_seals seal "
        "JOIN evidence_extraction_runs run "
        "ON run.extraction_run_id=seal.extraction_run_id "
        "WHERE run.document_version_id=? "
        "AND datetime(seal.knowledge_at)<=datetime(?) "
        "AND datetime(seal.recorded_at)<=datetime(?) "
        "ORDER BY datetime(seal.knowledge_at) DESC,seal.disposition_seal_id",
        (document_version_id, _db_time(cutoff), _db_time(observed_through)),
    ).fetchall()
    if len(rows) != 1:
        raise ValueError("filing_xbrl_exact_disposition_seal_missing_or_ambiguous")
    row = rows[0]
    return ProcessingEvidenceReference(
        evidence_table="filing_xbrl_extraction_disposition_seals",
        evidence_id=str(row[0]),
        evidence_commitment_sha256=str(row[1]),
        knowledge_at=_parse_time(row[2]),
        recorded_at=_parse_time(row[3]),
    )


def _seal_complete_snapshots(
    conn: sqlite3.Connection,
    documents_by_issuer: dict[str, tuple[str, ...]],
    cutoff: datetime,
    recorded_at: datetime,
) -> None:
    for issuer_id, document_ids in documents_by_issuer.items():
        totals = _obligation_totals(conn, cutoff, document_ids, recorded_at)
        if _sealed_disposition_count(conn, cutoff, document_ids, recorded_at) != totals["total"]:
            raise ValueError(
                f"cannot seal processing snapshot for {issuer_id} with unclosed obligations"
            )
        snapshot_id = "processing-snapshot:" + _digest(issuer_id, _db_time(cutoff))
        with conn:
            seal_processing_snapshot(
                conn,
                processing_snapshot_id=snapshot_id,
                idempotency_key=snapshot_id,
                scope=DocumentProcessingScope(document_version_ids=document_ids),
                cutoff_at=cutoff,
                policy=_POLICY,
                recorded_at=recorded_at,
            )


def _obligation_rows(
    conn: sqlite3.Connection,
    cutoff: datetime,
    observed_through: datetime,
    *,
    document_version_ids: tuple[str, ...],
    after: str | None,
    limit: int | None,
) -> list[sqlite3.Row]:
    if not document_version_ids:
        return []
    parameters: list[object] = [
        _db_time(cutoff),
        _db_time(observed_through),
        json.dumps(document_version_ids),
    ]
    after_sql = ""
    if after is not None:
        after_sql = "AND processing_obligation_revision_id>?"
        parameters.append(after)
    limit_sql = ""
    if limit is not None:
        limit_sql = " LIMIT ?"
        parameters.append(limit)
    return conn.execute(
        "SELECT * FROM document_processing_obligation_revisions "
        "WHERE datetime(knowledge_at)<=datetime(?) "
        "AND datetime(recorded_at)<=datetime(?) "
        "AND document_version_id IN (SELECT value FROM json_each(?)) "
        f"{after_sql} ORDER BY processing_obligation_revision_id{limit_sql}",  # nosec B608 -- fixed fragments; values are bound
        tuple(parameters),
    ).fetchall()


def _has_sealed_disposition(
    conn: sqlite3.Connection,
    obligation_id: str,
    cutoff: datetime,
    observed_through: datetime,
) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM document_processing_disposition_headers header "
            "JOIN document_processing_disposition_seals seal "
            "ON seal.processing_disposition_id=header.processing_disposition_id "
            "WHERE header.processing_obligation_revision_id=? "
            "AND datetime(header.knowledge_at)<=datetime(?) "
            "AND datetime(header.recorded_at)<=datetime(?) "
            "AND datetime(seal.sealed_at)<=datetime(?)",
            (
                obligation_id,
                _db_time(cutoff),
                _db_time(observed_through),
                _db_time(observed_through),
            ),
        ).fetchone()
        is not None
    )


def _obligation_totals(
    conn: sqlite3.Connection,
    cutoff: datetime,
    document_version_ids: tuple[str, ...],
    observed_through: datetime,
) -> dict[str, int]:
    counts = {"total": 0, "applicable": 0, "not_applicable": 0}
    if not document_version_ids:
        return counts
    for row in conn.execute(
        "SELECT applicability,COUNT(*) "
        "FROM document_processing_obligation_revisions "
        "WHERE datetime(knowledge_at)<=datetime(?) "
        "AND datetime(recorded_at)<=datetime(?) "
        "AND document_version_id IN (SELECT value FROM json_each(?)) "
        "GROUP BY applicability",
        (
            _db_time(cutoff),
            _db_time(observed_through),
            json.dumps(document_version_ids),
        ),
    ):
        counts[str(row[0])] = int(row[1])
        counts["total"] += int(row[1])
    return counts


def _sealed_disposition_count(
    conn: sqlite3.Connection,
    cutoff: datetime,
    document_version_ids: tuple[str, ...],
    observed_through: datetime,
) -> int:
    if not document_version_ids:
        return 0
    return int(
        conn.execute(
            "SELECT COUNT(DISTINCT header.processing_obligation_revision_id) "
            "FROM document_processing_disposition_headers header "
            "JOIN document_processing_obligation_revisions obligation "
            "ON obligation.processing_obligation_revision_id="
            "header.processing_obligation_revision_id "
            "JOIN document_processing_disposition_seals seal "
            "ON seal.processing_disposition_id=header.processing_disposition_id "
            "WHERE datetime(header.knowledge_at)<=datetime(?) "
            "AND datetime(header.recorded_at)<=datetime(?) "
            "AND datetime(seal.sealed_at)<=datetime(?) "
            "AND obligation.document_version_id "
            "IN (SELECT value FROM json_each(?))",
            (
                _db_time(cutoff),
                _db_time(observed_through),
                _db_time(observed_through),
                json.dumps(document_version_ids),
            ),
        ).fetchone()[0]
    )


def _binding_count(
    conn: sqlite3.Connection,
    expected_document_ids: tuple[str, ...],
) -> int:
    if not expected_document_ids:
        return 0
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM expected_document_obligation_bindings "
            "WHERE expected_document_id IN (SELECT value FROM json_each(?))",
            (json.dumps(expected_document_ids),),
        ).fetchone()[0]
    )


def _processing_snapshot_count(
    conn: sqlite3.Connection,
    cutoff: datetime,
    observed_through: datetime,
) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM document_processing_snapshot_headers header "
            "JOIN document_processing_snapshot_seals seal "
            "ON seal.processing_snapshot_id=header.processing_snapshot_id "
            "WHERE datetime(header.cutoff_at)=datetime(?) "
            "AND datetime(header.recorded_at)<=datetime(?) "
            "AND datetime(seal.sealed_at)<=datetime(?)",
            (
                _db_time(cutoff),
                _db_time(observed_through),
                _db_time(observed_through),
            ),
        ).fetchone()[0]
    )


def _input_commitment(
    conn: sqlite3.Connection,
    cutoff: datetime,
    observed_through: datetime,
) -> str:
    rows = conn.execute(
        "SELECT expected.expected_document_id,coverage.coverage_status,"
        "coverage.document_version_id,coverage.assessment_id "
        "FROM expected_documents expected "
        "JOIN source_coverage_assessments coverage "
        "ON coverage.expected_document_id=expected.expected_document_id "
        "WHERE datetime(expected.recorded_at)<=datetime(?) "
        "AND datetime(coverage.knowledge_at)<=datetime(?) "
        "AND datetime(coverage.recorded_at)<=datetime(?) "
        "AND NOT EXISTS (SELECT 1 FROM source_coverage_assessments newer "
        "WHERE newer.expected_document_id=coverage.expected_document_id "
        "AND newer.revision>coverage.revision "
        "AND datetime(newer.knowledge_at)<=datetime(?) "
        "AND datetime(newer.recorded_at)<=datetime(?)) "
        "ORDER BY expected.expected_document_id",
        (
            _db_time(observed_through),
            _db_time(cutoff),
            _db_time(observed_through),
            _db_time(cutoff),
            _db_time(observed_through),
        ),
    ).fetchall()
    return _sha_rows(rows)


def _population_plan_commitment(
    request: DocumentProcessingPopulationRequest,
    input_sha: str,
    selection_sha: str,
) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "after_processing_obligation_revision_id": (
                    request.after_processing_obligation_revision_id
                ),
                "cutoff_at": _db_time(request.cutoff_at),
                "input_commitment_sha256": input_sha,
                "max_obligations": request.max_obligations,
                "phase": request.phase,
                "operation_recorded_at": _db_time(request.operation_recorded_at),
                "selection_commitment_sha256": selection_sha,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _verify_commitments(
    request: DocumentProcessingPopulationRequest,
    *,
    input_sha: str,
    plan_sha: str,
) -> None:
    if request.input_commitment_sha256 is not None and request.input_commitment_sha256 != input_sha:
        raise ValueError("document processing input commitment changed")
    if request.plan_commitment_sha256 is not None and request.plan_commitment_sha256 != plan_sha:
        raise ValueError("document processing plan commitment changed")


def _retry_cursor_after_attempt(
    *,
    prior_cursor: str | None,
    attempted_id: str,
    succeeded: bool,
) -> str | None:
    return attempted_id if succeeded else prior_cursor


def _document_checkpoint(
    *,
    bounded: bool,
    prior_cursor: str | None,
    processed: int,
    total: int,
    sealed: int,
    blocker_count: int = 0,
) -> DocumentProcessingCheckpoint:
    remaining = max(total - sealed, 0)
    return DocumentProcessingCheckpoint(
        bounded=bounded,
        safe_to_seal=not bounded and remaining == 0 and blocker_count == 0,
        last_processing_obligation_revision_id=prior_cursor,
        processed_obligation_count=processed,
        remaining_obligation_count=remaining,
        can_resume=bounded and remaining > 0,
    )


def _expected_reporting_issuer_count(
    conn: sqlite3.Connection,
    knowledge: datetime,
    observed: datetime,
) -> int:
    placeholders = ",".join("?" for _ in _REPORTING_FAMILIES)
    return int(
        conn.execute(
            "SELECT COUNT(DISTINCT obligation.issuer_id) "
            "FROM source_obligation_revisions obligation "
            "WHERE obligation.obligation_state IN ('required','optional') "
            f"AND obligation.document_family IN ({placeholders}) "  # nosec B608
            "AND obligation.reporting_entity_id IS NOT NULL "
            "AND datetime(obligation.active_from)<=datetime(?) "
            "AND (obligation.active_to IS NULL OR datetime(obligation.active_to)>datetime(?)) "
            "AND datetime(obligation.knowledge_at)<=datetime(?) "
            "AND datetime(obligation.recorded_at)<=datetime(?) "
            "AND NOT EXISTS (SELECT 1 FROM source_obligation_revisions newer "
            "WHERE newer.obligation_key=obligation.obligation_key "
            "AND newer.revision>obligation.revision "
            "AND datetime(newer.knowledge_at)<=datetime(?) "
            "AND datetime(newer.recorded_at)<=datetime(?))",
            (
                *_REPORTING_FAMILIES,
                _db_time(knowledge),
                _db_time(knowledge),
                _db_time(knowledge),
                _db_time(observed),
                _db_time(knowledge),
                _db_time(observed),
            ),
        ).fetchone()[0]
    )


def _document_plane_verification(
    *,
    scope: PopulationTemporalScope,
    expected: int,
    artifacts: tuple[PopulationArtifactSetCommitment, ...],
) -> PopulationPlaneVerification:
    if expected <= 0:
        raise ValueError("document processing expected universe is empty at K,O")
    materialized = artifacts[0].row_count
    if materialized > expected:
        raise ValueError("document processing artifact set exceeds expected universe")
    failed = expected - materialized
    details = cast(
        dict[str, JsonValue],
        {
            "knowledge_cutoff": _db_time(scope.knowledge_cutoff),
            "observed_through": _db_time(scope.observed_through),
            "selection_policy_id": _DOCUMENT_SELECTION_POLICY,
        },
    )
    output_material = {
        "artifact_sets": [item.model_dump(mode="json") for item in artifacts],
        "details": details,
        "exclusion_counts": {},
        "expected_count": expected,
        "failed_count": failed,
        "materialized_count": materialized,
        "plane_name": "document_processing",
    }
    return PopulationPlaneVerification(
        plane_name="document_processing",
        expected_count=expected,
        materialized_count=materialized,
        excluded_count=0,
        failed_count=failed,
        exclusion_counts={},
        input_commitment_sha256=digest_text(
            canonical_json(
                {
                    "expected_count": expected,
                    "knowledge_cutoff": scope.knowledge_cutoff,
                    "observed_through": scope.observed_through,
                    "selection_policy_id": _DOCUMENT_SELECTION_POLICY,
                }
            )
        ),
        output_commitment_sha256=digest_text(canonical_json(output_material)),
        artifact_sets=artifacts,
        details=details,
    )


def _output_commitment(
    conn: sqlite3.Connection,
    cutoff: datetime,
    decisions: tuple[ReportingDocumentDecision, ...],
    observed_through: datetime,
) -> str:
    rows = conn.execute(
        "SELECT 'evidence',header.evidence_seal_id,seal.member_set_sha256 "
        "FROM document_processing_evidence_headers header "
        "JOIN document_processing_evidence_seals seal "
        "ON seal.evidence_seal_id=header.evidence_seal_id "
        "WHERE datetime(header.cutoff_at)=datetime(?) "
        "AND datetime(header.recorded_at)<=datetime(?) "
        "AND datetime(seal.sealed_at)<=datetime(?) "
        "UNION ALL "
        "SELECT 'disposition',header.processing_disposition_id,seal.member_set_sha256 "
        "FROM document_processing_disposition_headers header "
        "JOIN document_processing_disposition_seals seal "
        "ON seal.processing_disposition_id=header.processing_disposition_id "
        "WHERE datetime(header.knowledge_at)<=datetime(?) "
        "AND datetime(header.recorded_at)<=datetime(?) "
        "AND datetime(seal.sealed_at)<=datetime(?) "
        "UNION ALL "
        "SELECT 'snapshot',header.processing_snapshot_id,seal.member_set_sha256 "
        "FROM document_processing_snapshot_headers header "
        "JOIN document_processing_snapshot_seals seal "
        "ON seal.processing_snapshot_id=header.processing_snapshot_id "
        "WHERE datetime(header.cutoff_at)=datetime(?) "
        "AND datetime(header.recorded_at)<=datetime(?) "
        "AND datetime(seal.sealed_at)<=datetime(?) "
        "ORDER BY 1,2",
        (
            _db_time(cutoff),
            _db_time(observed_through),
            _db_time(observed_through),
            _db_time(cutoff),
            _db_time(observed_through),
            _db_time(observed_through),
            _db_time(cutoff),
            _db_time(observed_through),
            _db_time(observed_through),
        ),
    ).fetchall()
    payload = {
        "ledger_rows": [list(row) for row in rows],
        "selection_commitment_sha256": _selection_commitment(decisions),
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


def _selection_commitment(
    decisions: tuple[ReportingDocumentDecision, ...],
) -> str:
    payload = [
        decision.model_dump(mode="json")
        for decision in sorted(decisions, key=lambda item: item.expected_document_id)
    ]
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


def _sha_rows(rows: list[sqlite3.Row]) -> str:
    payload = json.dumps(
        [list(row) for row in rows],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def _sql_sha256(value: object) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()


def _reason(exc: Exception) -> str:
    reason = getattr(exc, "reason_code", None)
    return str(reason or str(exc) or type(exc).__name__)[:128]


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _parse_time(value: object) -> datetime:
    return _utc(datetime.fromisoformat(str(value)))


def _db_time(value: datetime) -> str:
    return _utc(value).isoformat()
