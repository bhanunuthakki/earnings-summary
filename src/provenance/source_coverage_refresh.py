"""Promote source coverage from immutable evidence lineage without recrawling.

Authority discovery owns the expected-document universe.  Capture, extraction,
and indexing happen later and must be able to advance coverage independently;
otherwise a truthful coverage view goes stale until the publisher is crawled
again.  This module appends only monotonic, lineage-proven transitions.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from provenance.fulltext_extractor_identity import (
    FULLTEXT_EXTRACTOR_NAME,
    resolve_fulltext_extractor_identity,
)
from provenance.image_ocr_extraction import IMAGE_OCR_EXTRACTOR_NAME
from provenance.search_index_lineage import IndexKind, sealed_index_lineage
from provenance.source_coverage import CoverageAssessment, SourceCoverageLedger

_POLICY_NAME = "evidence-lineage-coverage-promotion"
_POLICY_VERSION = "2"
_Mode = Literal["dry_run", "apply"]


class CoverageRefreshRequest(BaseModel):
    """Bounded controls for one monotonic coverage-promotion batch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    inventory_keys: tuple[str, ...] = Field(min_length=1)
    recorded_at: datetime
    extractor_names: tuple[str, ...] = Field(
        default=(
            "fulltext-evidence-backfill",
            "governed-pdf-ocr",
            "governed-image-ocr",
        ),
        min_length=1,
    )
    index_kinds: tuple[IndexKind, ...] = Field(
        default=("lexical", "vector"),
        min_length=1,
    )
    batch_size: int = Field(default=500, ge=1, le=5_000)
    apply: bool = False

    @field_validator("inventory_keys", "extractor_names", "index_kinds")
    @classmethod
    def _unique_nonblank(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip() for item in value)
        if any(not item for item in normalized):
            raise ValueError("values must not be blank")
        if len(normalized) != len(set(normalized)):
            raise ValueError("values must be unique")
        return normalized


class CoverageRefreshResult(BaseModel):
    """Closed accounting for a read-only plan or append-only promotion."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: _Mode
    dry_run: bool
    inventory_keys: tuple[str, ...]
    assessments_considered: int = Field(ge=0)
    assessments_planned: int = Field(ge=0)
    assessments_created: int = Field(ge=0)
    assessments_replayed: int = Field(ge=0)
    target_status_counts: dict[str, int]
    has_more: bool


class _Promotion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    assessment: CoverageAssessment
    target_status: Literal["extracted", "indexed", "unsupported"]


def refresh_source_coverage(
    conn: sqlite3.Connection,
    request: CoverageRefreshRequest,
    *,
    caller_owns_transaction: bool = False,
) -> CoverageRefreshResult:
    """Plan or append bounded monotonic extraction and indexing revisions."""

    _require_schema(conn)
    if "filing-native-xbrl" in request.extractor_names:
        _require_xbrl_schema(conn)
    index_promotions, index_considered, index_has_more = _plan_index_promotions(
        conn,
        request,
        limit=request.batch_size,
    )
    remaining = request.batch_size - len(index_promotions)
    zero_fact_promotions: list[_Promotion] = []
    zero_fact_considered = 0
    zero_fact_has_more = False
    if remaining > 0 and not index_has_more:
        (
            zero_fact_promotions,
            zero_fact_considered,
            zero_fact_has_more,
        ) = _plan_xbrl_zero_fact_promotions(conn, request, limit=remaining)
        remaining -= len(zero_fact_promotions)
    extraction_promotions: list[_Promotion] = []
    extraction_considered = 0
    extraction_has_more = False
    if remaining > 0 and not index_has_more and not zero_fact_has_more:
        (
            extraction_promotions,
            extraction_considered,
            extraction_has_more,
        ) = _plan_extraction_promotions(conn, request, limit=remaining)
    promotions = [
        *index_promotions,
        *zero_fact_promotions,
        *extraction_promotions,
    ]
    considered = index_considered + zero_fact_considered + extraction_considered
    has_more = index_has_more or zero_fact_has_more or extraction_has_more
    created = 0
    replayed = 0
    if request.apply and promotions:
        if caller_owns_transaction and not conn.in_transaction:
            raise RuntimeError("caller-owned source coverage refresh requires a transaction")
        if not caller_owns_transaction and conn.in_transaction:
            raise RuntimeError("source coverage refresh requires an idle SQLite connection")
        if not caller_owns_transaction:
            conn.execute("BEGIN IMMEDIATE")
        try:
            ledger = SourceCoverageLedger(conn)
            for promotion in promotions:
                persisted = ledger.persist(promotion.assessment)
                created += int(persisted.created)
                replayed += int(not persisted.created)
            if not caller_owns_transaction:
                conn.commit()
        except Exception:
            if not caller_owns_transaction and conn.in_transaction:
                conn.rollback()
            raise
    counts = Counter(promotion.target_status for promotion in promotions)
    return CoverageRefreshResult(
        mode="apply" if request.apply else "dry_run",
        dry_run=not request.apply,
        inventory_keys=request.inventory_keys,
        assessments_considered=considered,
        assessments_planned=len(promotions),
        assessments_created=created,
        assessments_replayed=replayed,
        target_status_counts=dict(sorted(counts.items())),
        has_more=has_more,
    )


def _plan_index_promotions(
    conn: sqlite3.Connection,
    request: CoverageRefreshRequest,
    *,
    limit: int,
) -> tuple[list[_Promotion], int, bool]:
    if limit < 1:
        return [], 0, False
    inventory_placeholders = ", ".join("?" for _ in request.inventory_keys)
    index_placeholders = ", ".join("?" for _ in request.index_kinds)
    rows = conn.execute(
        "SELECT coverage.assessment_id, coverage.expected_document_id, "  # nosec B608 -- trusted internal SQL shape; values remain bound
        "coverage.revision, coverage.document_version_id, "
        "coverage.extraction_run_id, expected.expected_document_key "
        "FROM v_source_coverage_current AS coverage "
        "JOIN v_expected_documents_current AS expected "
        "ON expected.expected_document_id = coverage.expected_document_id "
        "JOIN v_source_inventory_current AS inventory "
        "ON inventory.snapshot_id = expected.snapshot_id "
        f"WHERE inventory.inventory_key IN ({inventory_placeholders}) "
        "AND coverage.coverage_status = 'extracted' "
        "AND EXISTS (SELECT 1 "
        "FROM search_corpus_document_memberships AS membership "
        "JOIN search_corpus_manifest_seals AS seal "
        "ON seal.manifest_id = membership.manifest_id "
        "JOIN v_search_index_successful AS run "
        "ON run.manifest_id = membership.manifest_id "
        "JOIN search_chunks AS chunk ON chunk.manifest_id = run.manifest_id "
        "JOIN evidence_nodes AS node ON node.node_id = chunk.evidence_node_id "
        "WHERE membership.document_version_id = coverage.document_version_id "
        "AND membership.membership_status = 'included' "
        "AND seal.completion_status = 'complete' "
        "AND node.extraction_run_id = coverage.extraction_run_id "
        f"AND run.index_kind IN ({index_placeholders})) "
        "ORDER BY coverage.expected_document_id LIMIT ?",
        (*request.inventory_keys, *request.index_kinds, limit + 1),
    ).fetchall()
    promotions: list[_Promotion] = []
    has_more = len(rows) > limit
    for row in rows[:limit]:
        document_version_id = _text(row[3], "document_version_id")
        extraction_run_id = _text(row[4], "extraction_run_id")
        lineage = sealed_index_lineage(
            conn,
            document_version_id=document_version_id,
            extraction_run_id=extraction_run_id,
            index_kinds=request.index_kinds,
        )
        if lineage is None:
            continue
        if _timeline(request.recorded_at) < _timeline(lineage.completed_at):
            raise ValueError("coverage refresh recorded_at precedes index completion")
        expected_document_id = _text(row[1], "expected_document_id")
        revision = _integer(row[2], "coverage revision") + 1
        semantic = {
            "expected_document_id": expected_document_id,
            "target_status": "indexed",
            "document_version_id": document_version_id,
            "extraction_run_id": extraction_run_id,
            "manifest_id": lineage.manifest_id,
            "index_run_id": lineage.index_run_id,
            "recorded_at": request.recorded_at,
            "policy_name": _POLICY_NAME,
            "policy_version": _POLICY_VERSION,
        }
        fingerprint = _sha_json(semantic)
        assessment = CoverageAssessment(
            assessment_id=(
                f"coverage-assessment:{_sha_text(fingerprint + chr(0) + str(revision))}"
            ),
            idempotency_key=f"coverage-assessment:{fingerprint}",
            expected_document_id=expected_document_id,
            revision=revision,
            coverage_status="indexed",
            document_version_id=document_version_id,
            extraction_run_id=extraction_run_id,
            manifest_id=lineage.manifest_id,
            index_run_id=lineage.index_run_id,
            reason_code="sealed_successful_index",
            reason_details=(
                ("document_version_id", document_version_id),
                ("expected_document_key", _text(row[5], "expected_document_key")),
                ("extraction_run_id", extraction_run_id),
                ("index_code_version", lineage.code_version),
                ("index_config_sha256", lineage.config_sha256),
                ("index_kind", lineage.index_kind),
                ("index_run_id", lineage.index_run_id),
                ("manifest_id", lineage.manifest_id),
            ),
            decision_kind="deterministic",
            policy_name=_POLICY_NAME,
            policy_version=_POLICY_VERSION,
            policy_config_sha256=_policy_sha(
                request.extractor_names,
                request.index_kinds,
                transition="extracted_to_indexed",
            ),
            effective_at=lineage.completed_at,
            knowledge_at=request.recorded_at,
            recorded_at=request.recorded_at,
            supersedes_assessment_id=_text(row[0], "assessment_id"),
            material_dissent=False,
        )
        promotions.append(_Promotion(assessment=assessment, target_status="indexed"))
    return promotions, len(rows), has_more


def _plan_extraction_promotions(
    conn: sqlite3.Connection,
    request: CoverageRefreshRequest,
    *,
    limit: int,
) -> tuple[list[_Promotion], int, bool]:
    if limit < 1:
        return [], 0, False
    inventory_placeholders = ", ".join("?" for _ in request.inventory_keys)
    extractor_placeholders = ", ".join("?" for _ in request.extractor_names)
    rows = conn.execute(
        "SELECT coverage.assessment_id, "  # nosec B608 -- trusted internal SQL shape; values remain bound
        "coverage.expected_document_id, coverage.revision, coverage.coverage_status, "
        "coverage.document_version_id, coverage.extraction_run_id, "
        "expected.expected_document_key, "
        "run.extraction_run_id, run.extractor_name, "
        "run.extractor_code_version, run.completed_at, "
        "run.extractor_config_sha256, observation.source_url, blob.media_type "
        "FROM v_source_coverage_current AS coverage "
        "JOIN v_expected_documents_current AS expected "
        "ON expected.expected_document_id = coverage.expected_document_id "
        "JOIN v_source_inventory_current AS inventory "
        "ON inventory.snapshot_id = expected.snapshot_id "
        "JOIN evidence_extraction_runs AS run "
        "ON run.document_version_id = coverage.document_version_id "
        "JOIN evidence_document_versions AS document "
        "ON document.document_version_id = coverage.document_version_id "
        "JOIN evidence_content_blobs AS blob ON blob.sha256 = document.blob_sha256 "
        "JOIN evidence_source_observations AS observation "
        "ON observation.observation_id = document.observation_id "
        f"WHERE inventory.inventory_key IN ({inventory_placeholders}) "
        "AND coverage.coverage_status IN ('captured', 'extracted', 'indexed') "
        "AND run.outcome = 'succeeded' "
        f"AND run.extractor_name IN ({extractor_placeholders}) "
        "AND EXISTS (SELECT 1 FROM v_evidence_current AS node "
        "WHERE node.extraction_run_id = run.extraction_run_id "
        "AND node.node_kind <> 'document' AND length(trim(node.text)) > 0) "
        "ORDER BY coverage.expected_document_id, run.completed_at DESC, "
        "run.extraction_run_id DESC",
        (*request.inventory_keys, *request.extractor_names),
    ).fetchall()
    approved_by_expected: dict[str, list[sqlite3.Row | tuple[object, ...]]] = {}
    approved_names_by_expected: dict[str, set[str]] = {}
    considered_expected_ids: set[str] = set()
    for row in rows:
        expected_document_id = _text(row[1], "expected_document_id")
        considered_expected_ids.add(expected_document_id)
        if not _approved_extraction_run(conn, row):
            continue
        extractor_name = _text(row[8], "extractor_name")
        approved_names = approved_names_by_expected.setdefault(expected_document_id, set())
        if extractor_name in approved_names:
            raise ValueError(
                "source coverage found multiple approved runs for one extractor "
                f"and expected document: {expected_document_id}, {extractor_name}"
            )
        approved_names.add(extractor_name)
        approved_by_expected.setdefault(expected_document_id, []).append(row)
    approved_rows: list[sqlite3.Row | tuple[object, ...]] = []
    for expected_document_id in sorted(approved_by_expected):
        candidates = approved_by_expected[expected_document_id]
        current_run_id = (
            None if candidates[0][5] is None else _text(candidates[0][5], "current run")
        )
        selected = next(
            (
                candidate
                for candidate in candidates
                if current_run_id is not None
                and _text(candidate[7], "extraction_run_id") == current_run_id
            ),
            candidates[0],
        )
        if current_run_id != _text(selected[7], "extraction_run_id"):
            approved_rows.append(selected)
    promotions: list[_Promotion] = []
    has_more = len(approved_rows) > limit
    for row in approved_rows[:limit]:
        expected_document_id = _text(row[1], "expected_document_id")
        completed_at = _datetime(row[10], "extraction completed_at")
        if _timeline(request.recorded_at) < _timeline(completed_at):
            raise ValueError("coverage refresh recorded_at precedes extraction completion")
        document_version_id = _text(row[4], "document_version_id")
        extraction_run_id = _text(row[7], "extraction_run_id")
        prior_extraction_run_id = (
            None if row[5] is None else _text(row[5], "prior extraction_run_id")
        )
        revision = _integer(row[2], "coverage revision") + 1
        semantic = {
            "expected_document_id": expected_document_id,
            "target_status": "extracted",
            "document_version_id": document_version_id,
            "extraction_run_id": extraction_run_id,
            "recorded_at": request.recorded_at,
            "policy_name": _POLICY_NAME,
            "policy_version": _POLICY_VERSION,
        }
        fingerprint = _sha_json(semantic)
        assessment = CoverageAssessment(
            assessment_id=f"coverage-assessment:{_sha_text(fingerprint + chr(0) + str(revision))}",
            idempotency_key=f"coverage-assessment:{fingerprint}",
            expected_document_id=expected_document_id,
            revision=revision,
            coverage_status="extracted",
            document_version_id=document_version_id,
            extraction_run_id=extraction_run_id,
            manifest_id=None,
            index_run_id=None,
            reason_code=(
                "succeeded_approved_extraction"
                if prior_extraction_run_id is None
                else "authoritative_extraction_lineage"
            ),
            reason_details=(
                ("document_version_id", document_version_id),
                ("expected_document_key", _text(row[6], "expected_document_key")),
                ("extraction_run_id", extraction_run_id),
                ("extractor_code_version", _text(row[9], "extractor_code_version")),
                ("extractor_name", _text(row[8], "extractor_name")),
                ("prior_extraction_run_id", prior_extraction_run_id or "none"),
            ),
            decision_kind="deterministic",
            policy_name=_POLICY_NAME,
            policy_version=_POLICY_VERSION,
            policy_config_sha256=_policy_sha(
                request.extractor_names,
                request.index_kinds,
                transition="authoritative_extraction_lineage",
            ),
            effective_at=completed_at,
            knowledge_at=request.recorded_at,
            recorded_at=request.recorded_at,
            supersedes_assessment_id=_text(row[0], "assessment_id"),
            material_dissent=False,
        )
        promotions.append(_Promotion(assessment=assessment, target_status="extracted"))
    return promotions, len(considered_expected_ids), has_more


def _plan_xbrl_zero_fact_promotions(
    conn: sqlite3.Connection,
    request: CoverageRefreshRequest,
    *,
    limit: int,
) -> tuple[list[_Promotion], int, bool]:
    if limit < 1 or "filing-native-xbrl" not in request.extractor_names:
        return [], 0, False
    rows = conn.execute(
        "SELECT coverage.assessment_id,coverage.expected_document_id,"
        "coverage.revision,coverage.document_version_id,"
        "expected.expected_document_key,run.extraction_run_id,run.completed_at "
        "FROM v_source_coverage_current coverage "
        "JOIN v_expected_documents_current expected "
        "ON expected.expected_document_id=coverage.expected_document_id "
        "JOIN v_source_inventory_current inventory "
        "ON inventory.snapshot_id=expected.snapshot_id "
        "JOIN evidence_extraction_runs run "
        "ON run.document_version_id=coverage.document_version_id "
        "JOIN filing_xbrl_extraction_input_seals input_seal "
        "ON input_seal.extraction_run_id=run.extraction_run_id "
        "JOIN filing_xbrl_extraction_disposition_seals disposition_seal "
        "ON disposition_seal.extraction_run_id=run.extraction_run_id "
        "WHERE inventory.inventory_key IN (SELECT value FROM json_each(?)) "
        "AND coverage.coverage_status='captured' "
        "AND run.extractor_name='filing-native-xbrl' "
        "AND run.outcome='succeeded' "
        "AND input_seal.raw_fact_count=0 "
        "AND input_seal.zero_fact_disposition='verified_no_inline_xbrl' "
        "AND disposition_seal.entry_count=0 "
        "AND disposition_seal.extraction_output_sha256=run.output_sha256 "
        "ORDER BY coverage.expected_document_id LIMIT ?",
        (json.dumps(list(request.inventory_keys)), limit + 1),
    ).fetchall()
    has_more = len(rows) > limit
    promotions: list[_Promotion] = []
    for row in rows[:limit]:
        expected_document_id = _text(row[1], "expected_document_id")
        document_version_id = _text(row[3], "document_version_id")
        extraction_run_id = _text(row[5], "extraction_run_id")
        if not _approved_filing_xbrl_run(
            conn,
            extraction_run_id=extraction_run_id,
            document_version_id=document_version_id,
            require_facts=False,
        ):
            raise ValueError(
                "zero-fact filing-XBRL extraction lacks qualified terminal closure"
            )
        completed_at = _datetime(row[6], "extraction completed_at")
        if _timeline(request.recorded_at) < _timeline(completed_at):
            raise ValueError("coverage refresh recorded_at precedes extraction completion")
        revision = _integer(row[2], "coverage revision") + 1
        semantic = {
            "expected_document_id": expected_document_id,
            "target_status": "unsupported",
            "document_version_id": document_version_id,
            "extraction_run_id": extraction_run_id,
            "recorded_at": request.recorded_at,
            "policy_name": _POLICY_NAME,
            "policy_version": _POLICY_VERSION,
        }
        fingerprint = _sha_json(semantic)
        assessment = CoverageAssessment(
            assessment_id=(
                f"coverage-assessment:{_sha_text(fingerprint + chr(0) + str(revision))}"
            ),
            idempotency_key=f"coverage-assessment:{fingerprint}",
            expected_document_id=expected_document_id,
            revision=revision,
            coverage_status="unsupported",
            document_version_id=document_version_id,
            extraction_run_id=extraction_run_id,
            manifest_id=None,
            index_run_id=None,
            reason_code="verified_no_inline_xbrl",
            reason_details=(
                ("document_version_id", document_version_id),
                ("expected_document_key", _text(row[4], "expected_document_key")),
                ("extraction_run_id", extraction_run_id),
                ("zero_fact_disposition", "verified_no_inline_xbrl"),
            ),
            decision_kind="deterministic",
            policy_name=_POLICY_NAME,
            policy_version=_POLICY_VERSION,
            policy_config_sha256=_policy_sha(
                request.extractor_names,
                request.index_kinds,
                transition="verified_no_inline_xbrl",
            ),
            effective_at=completed_at,
            knowledge_at=request.recorded_at,
            recorded_at=request.recorded_at,
            supersedes_assessment_id=_text(row[0], "assessment_id"),
            material_dissent=False,
        )
        promotions.append(
            _Promotion(assessment=assessment, target_status="unsupported")
        )
    return promotions, len(rows), has_more


def _approved_extraction_run(
    conn: sqlite3.Connection,
    row: sqlite3.Row | tuple[object, ...],
) -> bool:
    extractor_name = _text(row[8], "extractor_name")
    document_version_id = _text(row[4], "document_version_id")
    extraction_run_id = _text(row[7], "extraction_run_id")
    if extractor_name == FULLTEXT_EXTRACTOR_NAME:
        identity = resolve_fulltext_extractor_identity(
            _text(row[12], "source_url"),
            _text(row[13], "media_type"),
        )
        return (
            _text(row[9], "extractor_code_version") == identity.code_version
            and _text(row[11], "extractor_config_sha256") == identity.config_sha256
        )
    if extractor_name == IMAGE_OCR_EXTRACTOR_NAME:
        return (
            conn.execute(
                "SELECT 1 FROM image_ocr_results AS result "
                "JOIN image_ocr_extraction_governance AS governance "
                "ON governance.extraction_run_id = result.extraction_run_id "
                "JOIN image_ocr_assessments AS assessment "
                "ON assessment.assessment_id = governance.assessment_id "
                "WHERE result.extraction_run_id = ? AND result.outcome = 'accepted' "
                "AND result.node_id IS NOT NULL AND assessment.document_version_id = ? "
                "AND assessment.outcome = 'ocr_required' "
                "AND assessment.assessment_id = ("
                "SELECT current.assessment_id FROM image_ocr_assessments AS current "
                "WHERE current.document_version_id = ? "
                "ORDER BY current.assessed_at DESC, current.assessment_id DESC LIMIT 1)",
                (
                    extraction_run_id,
                    document_version_id,
                    document_version_id,
                ),
            ).fetchone()
            is not None
        )
    if extractor_name == "governed-pdf-ocr":
        return (
            conn.execute(
                "SELECT 1 FROM ocr_page_results AS result "
                "JOIN ocr_extraction_governance AS governance "
                "ON governance.extraction_run_id = result.extraction_run_id "
                "WHERE result.extraction_run_id = ? AND result.outcome = 'accepted' "
                "AND result.node_id IS NOT NULL "
                "AND governance.assessment_id = ("
                "SELECT current.assessment_id FROM ocr_document_assessments AS current "
                "WHERE current.document_version_id = ? "
                "ORDER BY current.assessed_at DESC, current.assessment_id DESC LIMIT 1)",
                (extraction_run_id, document_version_id),
            ).fetchone()
            is not None
        )
    if extractor_name == "filing-native-xbrl":
        return _approved_filing_xbrl_run(
            conn,
            extraction_run_id=extraction_run_id,
            document_version_id=document_version_id,
            require_facts=True,
        )
    return True


def _approved_filing_xbrl_run(
    conn: sqlite3.Connection,
    *,
    extraction_run_id: str,
    document_version_id: str,
    require_facts: bool,
) -> bool:
    fact_predicate = (
        "input_seal.raw_fact_count>0"
        if require_facts
        else (
            "input_seal.raw_fact_count=0 "
            "AND input_seal.zero_fact_disposition='verified_no_inline_xbrl'"
        )
    )
    return (
        conn.execute(
            "SELECT 1 FROM filing_xbrl_extraction_input_seals input_seal "
            "JOIN filing_xbrl_processor_artifacts artifact "
            "ON artifact.processor_artifact_id=input_seal.processor_artifact_id "
            "JOIN filing_xbrl_extraction_disposition_seals disposition_seal "
            "ON disposition_seal.extraction_run_id=input_seal.extraction_run_id "
            "JOIN evidence_extraction_runs run "
            "ON run.extraction_run_id=input_seal.extraction_run_id "
            "WHERE input_seal.extraction_run_id=? "
            "AND run.document_version_id=? "
            "AND run.extractor_name='filing-native-xbrl' AND run.outcome='succeeded' "
            f"AND {fact_predicate} "  # nosec B608 -- closed internal predicate
            "AND artifact.arelle_version='2.39.8' AND artifact.edgar_version='26.1' "
            "AND artifact.xule_version='30052' "
            "AND artifact.bridge_protocol_version='filing-xbrl-bridge.v1' "
            "AND json_extract(artifact.canonical_manifest_json,"
            "'$.qualification.profile')='sec-inline-xbrl-investor-grade.v1' "
            "AND json_extract(artifact.canonical_manifest_json,"
            "'$.qualification.require_os_network_denial')=1 "
            "AND json_extract(input_seal.canonical_execution_evidence_json,"
            "'$.internet_connectivity')='os_denied' "
            "AND json_extract(input_seal.canonical_execution_evidence_json,"
            "'$.network_requests_observed')=0 "
            "AND json_extract(input_seal.canonical_execution_evidence_json,"
            "'$.accession_number')=input_seal.accession_number "
            "AND json_extract(input_seal.canonical_execution_evidence_json,"
            "'$.expected_cik')=input_seal.expected_cik "
            "AND EXISTS (SELECT 1 FROM issuer_identifier_resolution_outcomes resolution "
            "JOIN issuer_identifier_assertions assertion "
            "ON assertion.assertion_id=resolution.selected_assertion_id "
            "WHERE resolution.resolution_key='sec_cik:'||input_seal.expected_cik "
            "AND resolution.outcome='selected' "
            "AND assertion.issuer_id=input_seal.issuer_id "
            "AND resolution.knowledge_at<=input_seal.recorded_at "
            "AND assertion.knowledge_at<=input_seal.recorded_at "
            "AND NOT EXISTS (SELECT 1 FROM issuer_identifier_resolution_outcomes newer "
            "WHERE newer.resolution_key=resolution.resolution_key "
            "AND newer.knowledge_at<=input_seal.recorded_at "
            "AND newer.revision>resolution.revision)) "
            "AND json_extract(input_seal.canonical_execution_evidence_json,"
            "'$.package_member_set_sha256')=input_seal.member_set_sha256 "
            "AND json_extract(input_seal.canonical_execution_evidence_json,"
            "'$.runtime_artifact_sha256')=artifact.artifact_sha256 "
            "AND disposition_seal.entry_count=input_seal.raw_fact_count "
            "AND disposition_seal.extraction_output_sha256=run.output_sha256 "
            "AND (SELECT COUNT(*) FROM filing_xbrl_extraction_input_members member "
            "WHERE member.extraction_run_id=input_seal.extraction_run_id)"
            "=input_seal.member_count "
            "AND (SELECT COUNT(*) FROM filing_xbrl_raw_fact_commitments raw "
            "WHERE raw.extraction_run_id=input_seal.extraction_run_id)"
            "=input_seal.raw_fact_count "
            "AND (SELECT COUNT(*) FROM filing_xbrl_footnote_commitments footnote "
            "WHERE footnote.extraction_run_id=input_seal.extraction_run_id)"
            "=input_seal.footnote_count "
            "AND (SELECT COUNT(*) FROM filing_xbrl_extraction_dispositions disposition "
            "WHERE disposition.extraction_run_id=input_seal.extraction_run_id)"
            "=input_seal.raw_fact_count "
            "AND (SELECT COUNT(*) FROM evidence_nodes node "
            "WHERE node.extraction_run_id=input_seal.extraction_run_id)"
            "=input_seal.raw_fact_count "
            "AND EXISTS (SELECT 1 FROM filing_xbrl_extraction_input_members primary_member "
            "WHERE primary_member.extraction_run_id=input_seal.extraction_run_id "
            "AND primary_member.member_ordinal=0 "
            "AND primary_member.member_role='primary_document' "
            "AND primary_member.document_version_id=run.document_version_id "
            "AND EXISTS (SELECT 1 FROM evidence_document_versions document "
            "WHERE document.document_version_id=primary_member.document_version_id "
            "AND document.issuer_id=input_seal.issuer_id))",
            (extraction_run_id, document_version_id),
        ).fetchone()
        is not None
    )


def _require_schema(conn: sqlite3.Connection) -> None:
    required = {
        "evidence_extraction_runs",
        "evidence_nodes",
        "expected_documents",
        "search_chunks",
        "search_corpus_document_memberships",
        "search_corpus_manifest_seals",
        "search_index_runs",
        "search_lexical_chunks",
        "source_coverage_assessments",
        "source_inventory_snapshots",
    }
    present = {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if missing := sorted(required - present):
        raise RuntimeError("source coverage refresh schema is incomplete: " + ", ".join(missing))


def _require_xbrl_schema(conn: sqlite3.Connection) -> None:
    required = {
        "filing_xbrl_extraction_input_seals",
        "filing_xbrl_extraction_disposition_seals",
    }
    present = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if missing := sorted(required - present):
        raise RuntimeError(
            "filing-XBRL coverage refresh schema is incomplete: "
            + ", ".join(missing)
        )


def _policy_sha(
    extractor_names: tuple[str, ...],
    index_kinds: tuple[IndexKind, ...],
    *,
    transition: Literal[
        "authoritative_extraction_lineage",
        "captured_to_extracted",
        "extracted_to_indexed",
        "verified_no_inline_xbrl",
    ],
) -> str:
    return _sha_json(
        {
            "policy_name": _POLICY_NAME,
            "policy_version": _POLICY_VERSION,
            "extractor_names": sorted(extractor_names),
            "index_kinds": sorted(index_kinds),
            "transition": transition,
        }
    )


def _sha_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, default=str, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{name} must be non-empty text")
    return value


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RuntimeError(f"{name} must be a positive integer")
    return value


def _datetime(value: object, name: str) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise RuntimeError(f"{name} must be ISO-8601") from error
    raise RuntimeError(f"{name} must be a datetime")


def _timeline(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
