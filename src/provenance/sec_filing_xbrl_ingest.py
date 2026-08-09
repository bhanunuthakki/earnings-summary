"""Accession-atomic ingestion of qualified filing-native XBRL output."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast
from urllib.parse import unquote, urlparse

from pydantic import BaseModel, ConfigDict, Field

from filings.inline_xbrl_processor import (
    ApprovedProcessorBundle,
    InlineXbrlProcessorRequest,
    InlineXbrlProcessorResult,
    ProcessorBundleManifest,
    ProcessorPackageMember,
    ProcessorRawFact,
    package_member_set_sha256,
    run_inline_xbrl_processor,
)
from provenance.evidence_ledger import EvidenceLedger, EvidenceLocator, EvidenceNode, ExtractionRun
from provenance.filing_xbrl_extraction_ledger import (
    FilingXbrlExtractionLedger,
    FilingXbrlExtractionLedgerReceipt,
)
from provenance.filing_xbrl_fact_adapter import (
    FilingXbrlExtractionIdentity,
    FilingXbrlFactAdapter,
    FilingXbrlNormalizationRejection,
    FilingXbrlNormalizedOutput,
    FilingXbrlSubjectIdentity,
    NormalizedFilingXbrlFact,
)
from provenance.issuer_registry import IssuerRegistry
from provenance.reporting_entity_registry import ReportingEntityRegistry
from provenance.sec_native_capture import load_captured_sec_filing_package
from provenance.source_coverage_refresh import CoverageRefreshRequest, refresh_source_coverage

_EXTRACTOR_NAME = "filing-native-xbrl"
_EXTRACTOR_VERSION = "v1"


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FilingXbrlIngestRequest(_Closed):
    inventory_key: str = Field(min_length=1, max_length=256)
    accession_number: str = Field(pattern=r"^\d{10}-\d{2}-\d{6}$")
    expected_cik: str = Field(pattern=r"^\d{10}$")
    runtime_root: Path
    bundle_python: Path
    sandbox_launcher: Path
    recorded_at: datetime
    offline_artifacts: tuple[ProcessorPackageMember, ...] = ()
    apply: bool = False


class FilingXbrlIngestResult(_Closed):
    mode: Literal["dry_run", "apply"]
    accession_number: str
    extraction_run_id: str
    processor_artifact_sha256: str
    input_member_count: int = Field(ge=1)
    raw_fact_count: int = Field(ge=0)
    normalized_count: int = Field(ge=0)
    rejected_count: int = Field(ge=0)
    published_count: int = Field(ge=0)
    duplicate_count: int = Field(ge=0)
    quarantined_count: int = Field(ge=0)
    exact_replay: bool
    publication_id: str | None = None


def ingest_sec_filing_xbrl(
    conn: sqlite3.Connection,
    request: FilingXbrlIngestRequest,
    *,
    approved_bundle: ApprovedProcessorBundle,
) -> FilingXbrlIngestResult:
    if not isinstance(cast(object, approved_bundle), ApprovedProcessorBundle):
        raise ValueError("filing-XBRL ingest requires an approved processor bundle")
    manifest = approved_bundle.manifest
    captured = load_captured_sec_filing_package(
        conn,
        inventory_key=request.inventory_key,
        accession_number=request.accession_number,
    )
    primary = captured[0]
    members = tuple(
        ProcessorPackageMember(
            member_ordinal=index,
            member_role=_captured_member_role(member.document_type, member.source_url),
            document_version_id=member.document_version_id,
            source_url=member.source_url,
            local_path=file_uri_path(member.storage_uri),
            blob_sha256=member.blob_sha256,
            byte_size=member.byte_size,
            media_type=member.media_type,
        )
        for index, member in enumerate(captured)
    )
    members = _append_offline_artifacts(
        conn,
        captured=members,
        additional=request.offline_artifacts,
        issuer_id=primary.issuer_id,
        accession_number=request.accession_number,
        knowledge_cutoff=request.recorded_at,
    )
    member_set_sha = package_member_set_sha256(members)
    processor_result = run_inline_xbrl_processor(
        InlineXbrlProcessorRequest(
            accession_number=request.accession_number,
            entrypoint_ordinal=0,
            members=members,
            expected_cik=request.expected_cik,
            package_member_set_sha256=member_set_sha,
        ),
        approved_bundle=approved_bundle,
        runtime_root=request.runtime_root,
        bundle_python=request.bundle_python,
        sandbox_launcher=request.sandbox_launcher,
    )
    run_id = _run_id(request, manifest, processor_result)
    effective_recorded_at = _original_recorded_at(
        conn,
        run_id=run_id,
        proposed=request.recorded_at,
    )
    effective_request = request.model_copy(update={"recorded_at": effective_recorded_at})
    _verify_package_evidence_clock(
        conn,
        members=members,
        knowledge_cutoff=effective_recorded_at,
    )
    canonical_cik = IssuerRegistry(conn).resolve_identifier(
        "sec_cik",
        request.expected_cik,
        knowledge_at=effective_recorded_at,
    )
    if canonical_cik.issuer_id != primary.issuer_id or canonical_cik.material_dissent:
        raise ValueError("filing-XBRL CIK conflicts with the canonical filing issuer")
    subject = ReportingEntityRegistry(conn).canonicalize_recorded_subject(
        primary.issuer_id,
        knowledge_at=effective_recorded_at,
    )
    if subject.reporting_entity_id is None or subject.material_dissent:
        raise ValueError("filing-XBRL publication requires one undisputed reporting entity")
    output, run_id, config_sha = _normalized_output(
        request=effective_request,
        manifest=manifest,
        processor=processor_result,
        primary_document_version_id=primary.document_version_id,
        reporting_entity_id=subject.reporting_entity_id,
        subject_binding_revision_id=subject.binding_revision_id,
        scope_security_id=subject.security_id,
        input_sha256=primary.blob_sha256,
    )
    if not request.apply:
        adapted = FilingXbrlFactAdapter().adapt(output)
        return FilingXbrlIngestResult(
            mode="dry_run",
            accession_number=request.accession_number,
            extraction_run_id=run_id,
            processor_artifact_sha256=processor_result.runtime_artifact_sha256,
            input_member_count=len(members),
            raw_fact_count=len(processor_result.facts),
            normalized_count=len(output.entries),
            rejected_count=len(output.rejections),
            published_count=adapted.published_count,
            duplicate_count=adapted.duplicate_count,
            quarantined_count=adapted.quarantined_count,
            exact_replay=False,
        )

    try:
        conn.execute("BEGIN IMMEDIATE")
        evidence = EvidenceLedger(conn)
        evidence.persist(
            ExtractionRun(
                extraction_run_id=run_id,
                idempotency_key=run_id,
                document_version_id=primary.document_version_id,
                input_sha256=primary.blob_sha256,
                extractor_name=_EXTRACTOR_NAME,
                extractor_config_sha256=config_sha,
                extractor_code_version=_EXTRACTOR_VERSION,
                output_sha256=output.canonical_payload_sha256,
                started_at=effective_recorded_at,
                completed_at=effective_recorded_at,
                outcome="succeeded",
            )
        )
        _persist_evidence_nodes(
            evidence,
            run_id,
            processor_result,
            effective_recorded_at,
        )
        processor_artifact_id = _persist_processor_artifact(
            conn, manifest, processor_result, effective_recorded_at
        )
        _persist_input_closure(
            conn,
            run_id=run_id,
            processor_artifact_id=processor_artifact_id,
            members=members,
            processor=processor_result,
            accession_number=effective_request.accession_number,
            expected_cik=effective_request.expected_cik,
            issuer_id=primary.issuer_id,
            recorded_at=effective_recorded_at,
        )
        _persist_raw_commitments(
            conn,
            run_id=run_id,
            facts=processor_result.facts,
            recorded_at=effective_recorded_at,
        )
        receipt = FilingXbrlExtractionLedger(conn).publish(output)
        refresh_source_coverage(
            conn,
            CoverageRefreshRequest(
                inventory_keys=(request.inventory_key,),
                recorded_at=effective_recorded_at,
                extractor_names=(_EXTRACTOR_NAME,),
                apply=True,
            ),
            caller_owns_transaction=True,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return _result_from_receipt(request, processor_result, members, output, receipt)


def _normalized_output(
    *,
    request: FilingXbrlIngestRequest,
    manifest: ProcessorBundleManifest,
    processor: InlineXbrlProcessorResult,
    primary_document_version_id: str,
    reporting_entity_id: str,
    subject_binding_revision_id: str,
    scope_security_id: str | None,
    input_sha256: str,
) -> tuple[FilingXbrlNormalizedOutput, str, str]:
    run_id = _run_id(request, manifest, processor)
    config_sha = _sha(
        _canonical(
            {
                "bundle_manifest_sha256": manifest.manifest_sha256,
                "input_set_sha256": processor.package_member_set_sha256,
                "runtime_artifact_sha256": processor.runtime_artifact_sha256,
            }
        ).encode()
    )
    entries: list[NormalizedFilingXbrlFact] = []
    rejections: list[FilingXbrlNormalizationRejection] = []
    for fact in processor.facts:
        node_id = _node_id(run_id, fact.input_ordinal)
        if fact.normalized_fact is not None:
            payload = cast(dict[str, object], dict(fact.normalized_fact))
            _validate_normalized_locator(fact, payload)
            payload.update(
                {
                    "ordinal": fact.input_ordinal,
                    "evidence_node_id": node_id,
                    "scope_security_id": scope_security_id,
                    "source_locator": fact.source_locator,
                    "source_locator_sha256": fact.source_locator_sha256,
                    "source_entry_sha256": fact.source_entry_sha256,
                    "knowledge_at": request.recorded_at,
                    "recorded_at": request.recorded_at,
                }
            )
            entries.append(NormalizedFilingXbrlFact.model_validate(payload))
        else:
            rejections.append(
                FilingXbrlNormalizationRejection(
                    ordinal=fact.input_ordinal,
                    evidence_node_id=node_id,
                    canonical_raw_fact_json=_canonical(fact.canonical_raw_fact),
                    raw_fact_sha256=fact.raw_fact_sha256,
                    source_entry_sha256=fact.source_entry_sha256,
                    source_locator_sha256=fact.source_locator_sha256,
                    reason_code=str(fact.rejection_reason_code),
                    detail=str(fact.rejection_detail),
                    knowledge_at=request.recorded_at,
                    recorded_at=request.recorded_at,
                )
            )
    extraction = FilingXbrlExtractionIdentity(
        document_version_id=primary_document_version_id,
        extraction_run_id=run_id,
        extractor_name=_EXTRACTOR_NAME,
        extractor_code_version=_EXTRACTOR_VERSION,
        extractor_config_sha256=config_sha,
        extraction_input_sha256=input_sha256,
        extraction_output_sha256="0" * 64,
        expected_evidence_node_count=len(processor.facts),
        knowledge_at=request.recorded_at,
        recorded_at=request.recorded_at,
    )
    output = FilingXbrlNormalizedOutput.with_computed_digest(
        extraction=extraction,
        subject=FilingXbrlSubjectIdentity(
            reporting_entity_id=reporting_entity_id,
            selected_subject_binding_revision_id=subject_binding_revision_id,
        ),
        entries=tuple(entries),
        rejections=tuple(rejections),
    )
    return output, run_id, config_sha


def _persist_evidence_nodes(
    ledger: EvidenceLedger,
    run_id: str,
    result: InlineXbrlProcessorResult,
    recorded_at: datetime,
) -> None:
    for fact in result.facts:
        ledger.persist(
            EvidenceNode(
                node_id=_node_id(run_id, fact.input_ordinal),
                evidence_key=f"{run_id}:fact:{fact.input_ordinal}",
                revision=1,
                extraction_run_id=run_id,
                node_kind="table_cell",
                text=fact.evidence_text,
                locator=EvidenceLocator.model_validate(fact.source_locator),
                recorded_at=recorded_at,
            )
        )


def _persist_processor_artifact(
    conn: sqlite3.Connection,
    manifest: ProcessorBundleManifest,
    result: InlineXbrlProcessorResult,
    recorded_at: datetime,
) -> str:
    identity = _sha(f"{manifest.manifest_sha256}|{result.runtime_artifact_sha256}".encode())
    artifact_id = f"filing-xbrl-processor:{identity}"
    persist_exact(
        conn,
        table="filing_xbrl_processor_artifacts",
        columns=(
            "processor_artifact_id",
            "idempotency_key",
            "bundle_name",
            "arelle_version",
            "edgar_version",
            "xule_version",
            "bridge_protocol_version",
            "artifact_sha256",
            "sandbox_launcher_sha256",
            "bundle_python_sha256",
            "canonical_manifest_json",
            "manifest_sha256",
            "recorded_at",
        ),
        values=(
            artifact_id,
            artifact_id,
            manifest.bundle_name,
            manifest.coordinates.arelle,
            manifest.coordinates.edgar,
            manifest.coordinates.xule,
            manifest.bridge_protocol_version,
            result.runtime_artifact_sha256,
            manifest.execution.sandbox_launcher_sha256,
            manifest.execution.bundle_python_sha256,
            manifest.canonical_json,
            manifest.manifest_sha256,
            recorded_at,
        ),
    )
    return artifact_id


def _persist_input_closure(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    processor_artifact_id: str,
    members: tuple[ProcessorPackageMember, ...],
    processor: InlineXbrlProcessorResult,
    accession_number: str,
    expected_cik: str,
    issuer_id: str,
    recorded_at: datetime,
) -> None:
    member_payloads: list[dict[str, object]] = []
    for member in members:
        payload: dict[str, object] = {
            "blob_sha256": member.blob_sha256,
            "byte_size": member.byte_size,
            "document_version_id": member.document_version_id,
            "media_type": member.media_type,
            "member_ordinal": member.member_ordinal,
            "member_role": member.member_role,
            "source_url": member.source_url,
        }
        canonical = _canonical(payload)
        digest = _sha(canonical.encode())
        identity = _sha(f"{run_id}|{member.member_ordinal}".encode())
        persist_exact(
            conn,
            table="filing_xbrl_extraction_input_members",
            columns=(
                "input_member_id",
                "idempotency_key",
                "extraction_run_id",
                "member_ordinal",
                "member_role",
                "document_version_id",
                "source_url",
                "blob_sha256",
                "byte_size",
                "media_type",
                "canonical_member_json",
                "member_sha256",
                "recorded_at",
            ),
            values=(
                f"filing-xbrl-input:{identity}",
                f"filing-xbrl-input:{identity}",
                run_id,
                member.member_ordinal,
                member.member_role,
                member.document_version_id,
                member.source_url,
                member.blob_sha256,
                member.byte_size,
                member.media_type,
                canonical,
                digest,
                recorded_at,
            ),
        )
        member_payloads.append(payload)
    canonical_set = _canonical(member_payloads)
    set_sha = _sha(canonical_set.encode())
    network_payload = [
        {"blob_sha256": item.blob_sha256, "source_url": item.source_url}
        for item in processor.network_artifacts
    ]
    canonical_network = _canonical(network_payload)
    footnote_payload = [
        {
            "canonical_footnote": footnote.canonical_footnote,
            "footnote_ordinal": footnote.footnote_ordinal,
            "footnote_sha256": footnote.footnote_sha256,
            "input_ordinal": fact.input_ordinal,
        }
        for fact in processor.facts
        for footnote in fact.footnotes
    ]
    canonical_footnotes = _canonical(footnote_payload)
    execution_evidence_json = _canonical(processor.execution_evidence.model_dump(mode="json"))
    seal_id = f"filing-xbrl-input-seal:{_sha(run_id.encode())}"
    persist_exact(
        conn,
        table="filing_xbrl_extraction_input_seals",
        columns=(
            "input_seal_id",
            "idempotency_key",
            "extraction_run_id",
            "processor_artifact_id",
            "accession_number",
            "expected_cik",
            "issuer_id",
            "member_count",
            "canonical_member_set_json",
            "member_set_sha256",
            "network_artifact_count",
            "canonical_network_artifact_set_json",
            "network_artifact_set_sha256",
            "raw_fact_count",
            "raw_fact_set_sha256",
            "footnote_count",
            "canonical_footnote_set_json",
            "footnote_set_sha256",
            "canonical_execution_evidence_json",
            "execution_evidence_sha256",
            "zero_fact_disposition",
            "recorded_at",
        ),
        values=(
            seal_id,
            seal_id,
            run_id,
            processor_artifact_id,
            accession_number,
            expected_cik,
            issuer_id,
            len(members),
            canonical_set,
            set_sha,
            processor.network_artifact_count,
            canonical_network,
            processor.network_artifact_set_sha256,
            len(processor.facts),
            processor.raw_fact_set_sha256,
            processor.footnote_count,
            canonical_footnotes,
            processor.footnote_set_sha256,
            execution_evidence_json,
            _sha(execution_evidence_json.encode()),
            processor.zero_fact_disposition,
            recorded_at,
        ),
    )


def _persist_raw_commitments(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    facts: tuple[ProcessorRawFact, ...],
    recorded_at: datetime,
) -> None:
    for fact in facts:
        identity = _sha(f"{run_id}|{fact.input_ordinal}".encode())
        persist_exact(
            conn,
            table="filing_xbrl_raw_fact_commitments",
            columns=(
                "raw_fact_commitment_id",
                "idempotency_key",
                "extraction_run_id",
                "input_ordinal",
                "evidence_node_id",
                "package_member_ordinal",
                "package_member_blob_sha256",
                "accession_number",
                "observed_cik",
                "source_entry_sha256",
                "source_locator_sha256",
                "canonical_raw_fact_json",
                "raw_fact_sha256",
                "normalization_outcome",
                "reason_code",
                "recorded_at",
            ),
            values=(
                f"filing-xbrl-raw:{identity}",
                f"filing-xbrl-raw:{identity}",
                run_id,
                fact.input_ordinal,
                _node_id(run_id, fact.input_ordinal),
                fact.package_member_ordinal,
                fact.package_member_blob_sha256,
                fact.accession_number,
                fact.observed_cik,
                fact.source_entry_sha256,
                fact.source_locator_sha256,
                _canonical(fact.canonical_raw_fact),
                fact.raw_fact_sha256,
                fact.normalization_outcome,
                fact.rejection_reason_code,
                recorded_at,
            ),
        )
        for footnote in fact.footnotes:
            footnote_identity = _sha(
                f"{run_id}|{fact.input_ordinal}|{footnote.footnote_ordinal}".encode()
            )
            persist_exact(
                conn,
                table="filing_xbrl_footnote_commitments",
                columns=(
                    "footnote_commitment_id",
                    "idempotency_key",
                    "extraction_run_id",
                    "input_ordinal",
                    "footnote_ordinal",
                    "canonical_footnote_json",
                    "footnote_sha256",
                    "recorded_at",
                ),
                values=(
                    f"filing-xbrl-footnote:{footnote_identity}",
                    f"filing-xbrl-footnote:{footnote_identity}",
                    run_id,
                    fact.input_ordinal,
                    footnote.footnote_ordinal,
                    _canonical(footnote.canonical_footnote),
                    footnote.footnote_sha256,
                    recorded_at,
                ),
            )


def _result_from_receipt(
    request: FilingXbrlIngestRequest,
    processor: InlineXbrlProcessorResult,
    members: tuple[ProcessorPackageMember, ...],
    output: FilingXbrlNormalizedOutput,
    receipt: FilingXbrlExtractionLedgerReceipt,
) -> FilingXbrlIngestResult:
    return FilingXbrlIngestResult(
        mode="apply",
        accession_number=request.accession_number,
        extraction_run_id=receipt.extraction_run_id,
        processor_artifact_sha256=processor.runtime_artifact_sha256,
        input_member_count=len(members),
        raw_fact_count=len(processor.facts),
        normalized_count=len(output.entries),
        rejected_count=len(output.rejections),
        published_count=receipt.published_count,
        duplicate_count=receipt.duplicate_count,
        quarantined_count=receipt.quarantined_count,
        exact_replay=receipt.exact_replay,
        publication_id=receipt.publication_receipt.publication_id,
    )


def _run_id(
    request: FilingXbrlIngestRequest,
    manifest: ProcessorBundleManifest,
    processor: InlineXbrlProcessorResult,
) -> str:
    seed = _canonical(
        {
            "accession_number": request.accession_number,
            "bundle_manifest_sha256": manifest.manifest_sha256,
            "input_set_sha256": processor.package_member_set_sha256,
            "runtime_artifact_sha256": processor.runtime_artifact_sha256,
        }
    )
    return f"filing-xbrl-run:{_sha(seed.encode())}"


def _original_recorded_at(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    proposed: datetime,
) -> datetime:
    row = conn.execute(
        "SELECT started_at,completed_at FROM evidence_extraction_runs WHERE extraction_run_id=?",
        (run_id,),
    ).fetchone()
    if row is None:
        return proposed
    started = datetime.fromisoformat(str(row[0]))
    completed = datetime.fromisoformat(str(row[1]))
    if started != completed:
        raise ValueError("filing-XBRL replay has a non-atomic original timestamp")
    _verify_existing_run_closure(conn, run_id=run_id, recorded_at=started)
    return started


def persist_exact(
    conn: sqlite3.Connection,
    *,
    table: str,
    columns: tuple[str, ...],
    values: tuple[object, ...],
) -> None:
    identity_index = columns.index("idempotency_key")
    recorded_index = columns.index("recorded_at")
    existing = conn.execute(
        f"SELECT {','.join(columns)} FROM {table} WHERE idempotency_key=?",  # nosec B608 -- table and columns are closed internal constants
        (values[identity_index],),
    ).fetchone()
    if existing is not None:
        expected = list(values)
        expected[recorded_index] = existing[recorded_index]
        if not all(
            _database_value_equal(actual, wanted)
            for actual, wanted in zip(existing, expected, strict=True)
        ):
            raise ValueError(f"immutable {table} replay conflicts with existing data")
        return
    conn.execute(
        f"INSERT INTO {table} ({','.join(columns)}) "  # nosec B608 -- table and columns are closed internal constants
        f"VALUES ({','.join('?' for _ in columns)})",
        values,
    )


def _database_value_equal(actual: object, expected: object) -> bool:
    if isinstance(expected, datetime):
        try:
            return datetime.fromisoformat(str(actual)) == expected
        except ValueError:
            return False
    return actual == expected


def _append_offline_artifacts(
    conn: sqlite3.Connection,
    *,
    captured: tuple[ProcessorPackageMember, ...],
    additional: tuple[ProcessorPackageMember, ...],
    issuer_id: str,
    accession_number: str,
    knowledge_cutoff: datetime,
) -> tuple[ProcessorPackageMember, ...]:
    result = list(captured)
    for member in additional:
        if member.member_role not in {
            "issuer_taxonomy",
            "standard_taxonomy",
            "network_artifact",
        }:
            raise ValueError("offline artifacts may contain only taxonomy/network roles")
        blob = conn.execute(
            "SELECT byte_size,media_type,storage_uri FROM evidence_content_blobs WHERE sha256=?",
            (member.blob_sha256,),
        ).fetchone()
        if blob is None:
            raise ValueError("offline artifact is absent from the evidence blob ledger")
        try:
            stored_path = file_uri_path(str(blob[2])).resolve(strict=True)
            supplied_path = member.local_path.resolve(strict=True)
        except (OSError, ValueError) as exc:
            raise ValueError("offline artifact has no verified local evidence location") from exc
        if (
            int(blob[0]) != member.byte_size
            or str(blob[1]) != member.media_type
            or stored_path != supplied_path
        ):
            raise ValueError("offline artifact conflicts with its evidence blob record")
        source = conn.execute(
            "SELECT retrieved_at FROM evidence_source_observations "
            "WHERE source_url=? AND blob_sha256=? "
            "AND julianday(retrieved_at)<=julianday(?) "
            "ORDER BY retrieved_at,observation_id LIMIT 1",
            (member.source_url, member.blob_sha256, knowledge_cutoff),
        ).fetchone()
        if source is None:
            raise ValueError("offline artifact lacks a source observation available at the cutoff")
        if member.member_role == "issuer_taxonomy":
            document = conn.execute(
                "SELECT document.issuer_id,document.accession_number,"
                "document.blob_sha256,observation.source_url "
                "FROM evidence_document_versions document "
                "JOIN evidence_source_observations observation "
                "ON observation.observation_id=document.observation_id "
                "WHERE document.document_version_id=?",
                (member.document_version_id,),
            ).fetchone()
            if document is None or (
                str(document[0]),
                str(document[1]),
                str(document[2]),
                str(document[3]),
            ) != (
                issuer_id,
                accession_number,
                member.blob_sha256,
                member.source_url,
            ):
                raise ValueError("issuer-taxonomy artifact is outside the captured filing identity")
        result.append(member.model_copy(update={"member_ordinal": len(result)}))
    return tuple(result)


def _verify_package_evidence_clock(
    conn: sqlite3.Connection,
    *,
    members: tuple[ProcessorPackageMember, ...],
    knowledge_cutoff: datetime,
) -> None:
    """Reject post-hoc bytes or document identities on an exact replay."""

    for member in members:
        blob = conn.execute(
            "SELECT recorded_at FROM evidence_content_blobs WHERE sha256=?",
            (member.blob_sha256,),
        ).fetchone()
        if blob is None or _utc_datetime(blob[0]) > _utc_datetime(knowledge_cutoff):
            raise ValueError("filing-XBRL package blob was unavailable at the effective clock")
        if member.document_version_id is None:
            source = conn.execute(
                "SELECT 1 FROM evidence_source_observations "
                "WHERE source_url=? AND blob_sha256=? "
                "AND julianday(retrieved_at)<=julianday(?) LIMIT 1",
                (member.source_url, member.blob_sha256, knowledge_cutoff),
            ).fetchone()
            if source is None:
                raise ValueError(
                    "offline artifact source evidence was unavailable at the effective clock"
                )
            continue
        document = conn.execute(
            "SELECT recorded_at FROM evidence_document_versions "
            "WHERE document_version_id=? AND blob_sha256=?",
            (member.document_version_id, member.blob_sha256),
        ).fetchone()
        if document is None or _utc_datetime(document[0]) > _utc_datetime(knowledge_cutoff):
            raise ValueError("filing-XBRL document identity was unavailable at the effective clock")


def _verify_existing_run_closure(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    recorded_at: datetime,
) -> None:
    seal = conn.execute(
        "SELECT member_count,raw_fact_count,footnote_count,recorded_at "
        "FROM filing_xbrl_extraction_input_seals WHERE extraction_run_id=?",
        (run_id,),
    ).fetchone()
    disposition_seal = conn.execute(
        "SELECT entry_count,recorded_at,knowledge_at "
        "FROM filing_xbrl_extraction_disposition_seals WHERE extraction_run_id=?",
        (run_id,),
    ).fetchone()
    if seal is None or disposition_seal is None:
        raise ValueError("filing-XBRL replay targets an incomplete historical run")
    expected_clock = _utc_datetime(recorded_at)
    clocks = (
        seal[3],
        disposition_seal[1],
        disposition_seal[2],
    )
    if any(_utc_datetime(value) != expected_clock for value in clocks):
        raise ValueError("filing-XBRL replay has non-atomic durable clocks")
    counts = conn.execute(
        "SELECT "
        "(SELECT COUNT(*) FROM filing_xbrl_extraction_input_members "
        " WHERE extraction_run_id=?),"
        "(SELECT COUNT(*) FROM evidence_nodes WHERE extraction_run_id=?),"
        "(SELECT COUNT(*) FROM filing_xbrl_raw_fact_commitments "
        " WHERE extraction_run_id=?),"
        "(SELECT COUNT(*) FROM filing_xbrl_footnote_commitments "
        " WHERE extraction_run_id=?),"
        "(SELECT COUNT(*) FROM filing_xbrl_extraction_dispositions "
        " WHERE extraction_run_id=?)",
        (run_id, run_id, run_id, run_id, run_id),
    ).fetchone()
    assert counts is not None
    expected_counts = (
        int(seal[0]),
        int(seal[1]),
        int(seal[1]),
        int(seal[2]),
        int(disposition_seal[0]),
    )
    if tuple(int(value) for value in counts) != expected_counts:
        raise ValueError("filing-XBRL replay targets an incomplete durable closure")
    clock_tables = (
        ("filing_xbrl_extraction_input_members", "recorded_at"),
        ("evidence_nodes", "recorded_at"),
        ("filing_xbrl_raw_fact_commitments", "recorded_at"),
        ("filing_xbrl_footnote_commitments", "recorded_at"),
        ("filing_xbrl_extraction_dispositions", "recorded_at"),
        ("filing_xbrl_extraction_dispositions", "knowledge_at"),
    )
    for table, column in clock_tables:
        mismatch = conn.execute(
            f"SELECT 1 FROM {table} WHERE extraction_run_id=? "  # nosec B608 -- closed internal table/column pairs
            f"AND julianday({column})<>julianday(?) LIMIT 1",
            (run_id, recorded_at),
        ).fetchone()
        if mismatch is not None:
            raise ValueError("filing-XBRL replay has non-atomic durable clocks")


def _utc_datetime(value: object) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _captured_member_role(
    document_type: str,
    source_url: str,
) -> Literal["primary_document", "filing_attachment", "issuer_taxonomy"]:
    if document_type == "filing":
        return "primary_document"
    name = Path(urlparse(source_url).path).name.lower()
    if name.endswith(".xsd") or any(
        name.endswith(suffix) for suffix in ("_cal.xml", "_def.xml", "_lab.xml", "_pre.xml")
    ):
        return "issuer_taxonomy"
    return "filing_attachment"


def _validate_normalized_locator(
    fact: ProcessorRawFact,
    payload: dict[str, object],
) -> None:
    locator = fact.source_locator
    exact_pairs = (
        ("concept_namespace", "xbrl_concept_namespace"),
        ("concept_name", "xbrl_concept_name"),
        ("source_context_id", "xbrl_context_id"),
    )
    for normalized_field, locator_field in exact_pairs:
        if payload.get(normalized_field) != locator.get(locator_field):
            raise ValueError(f"normalized {normalized_field} conflicts with its XBRL locator")
    if payload.get("value_kind") == "numeric":
        unit_id = payload.get("source_unit_id")
        if not isinstance(unit_id, str) or not unit_id or unit_id != locator.get("xbrl_unit_id"):
            raise ValueError("normalized numeric fact conflicts with its XBRL unit locator")


def file_uri_path(value: str) -> Path:
    parsed = urlparse(value)
    if parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("captured filing package member is not a local file URI")
    path = unquote(parsed.path)
    if len(path) >= 3 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    if "\x00" in path or path.startswith(("//", "\\\\", "/\\\\", "\\//")):
        raise ValueError("captured filing package member cannot use a UNC path")
    local = Path(path)
    if not local.is_absolute():
        raise ValueError("captured filing package member must use an absolute local path")
    return local


def _node_id(run_id: str, ordinal: int) -> str:
    return f"filing-xbrl-node:{_sha(f'{run_id}|{ordinal}'.encode())}"


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
