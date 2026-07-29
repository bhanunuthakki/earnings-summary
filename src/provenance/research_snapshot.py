"""Obligation-complete Document Processing and Research Snapshot admission."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ProcessingLane = Literal[
    "html_native_hierarchy",
    "pdf_text",
    "pdf_ocr",
    "pdf_table",
    "image_ocr",
    "pptx_slides",
    "pptx_charts",
    "pptx_tables",
    "xlsx_workbook",
    "xlsx_sheets",
    "xlsx_tables",
    "transcript_turns",
    "transcript_speakers",
    "filing_xbrl",
]
TerminalStatus = Literal[
    "succeeded",
    "not_applicable",
    "source_unavailable",
    "quarantined",
    "failed",
]
_LANES: tuple[ProcessingLane, ...] = (
    "html_native_hierarchy",
    "pdf_text",
    "pdf_ocr",
    "pdf_table",
    "image_ocr",
    "pptx_slides",
    "pptx_charts",
    "pptx_tables",
    "xlsx_workbook",
    "xlsx_sheets",
    "xlsx_tables",
    "transcript_turns",
    "transcript_speakers",
    "filing_xbrl",
)


def canonical_json(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _digest(value: object) -> str:
    return _digest_text(canonical_json(value))


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _db_time(value: datetime) -> str:
    return _utc(value).replace(tzinfo=None).isoformat(sep=" ")


def _parse_time(value: object) -> datetime:
    return _utc(datetime.fromisoformat(str(value)))


def _validate_sha(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("must be a lowercase SHA-256 digest")
    return value


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DocumentProcessingScope(_Frozen):
    issuer_ids: tuple[str, ...] = ()
    document_version_ids: tuple[str, ...] = ()

    @field_validator("issuer_ids", "document_version_ids")
    @classmethod
    def _sorted_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item for item in value):
            raise ValueError("scope identifiers must be non-empty")
        return tuple(sorted(set(value)))

    @model_validator(mode="after")
    def _bounded(self) -> Self:
        if not self.issuer_ids and not self.document_version_ids:
            raise ValueError("scope requires issuer_ids or document_version_ids")
        return self


class DocumentProcessingPolicy(_Frozen):
    policy_name: str = Field(min_length=1, max_length=128)
    policy_version: str = Field(min_length=1, max_length=64)
    include_optional_source_obligations: bool = True

    @property
    def config_sha256(self) -> str:
        return _digest(self)


class DocumentProcessingObligation(_Frozen):
    processing_obligation_revision_id: str
    processing_obligation_key: str
    source_obligation_revision_id: str
    document_version_id: str
    processing_lane: ProcessingLane
    applicability: Literal["applicable", "not_applicable"]
    commitment_sha256: str
    knowledge_at: datetime
    recorded_at: datetime


class ProcessingEvidenceReference(_Frozen):
    evidence_table: str = Field(min_length=1, max_length=128)
    evidence_id: str = Field(min_length=1, max_length=128)
    evidence_commitment_sha256: str
    knowledge_at: datetime
    recorded_at: datetime

    _sha = field_validator("evidence_commitment_sha256")(_validate_sha)

    @model_validator(mode="after")
    def _clocks(self) -> Self:
        if _utc(self.recorded_at) < _utc(self.knowledge_at):
            raise ValueError("evidence recorded_at must not precede knowledge_at")
        return self


class DocumentProcessingDisposition(_Frozen):
    processing_disposition_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    processing_obligation_revision_id: str = Field(min_length=1, max_length=128)
    terminal_status: TerminalStatus
    reason_code: str = Field(min_length=1, max_length=128)
    reason_details: dict[str, object]
    evidence: tuple[ProcessingEvidenceReference, ...] = ()
    knowledge_at: datetime
    recorded_at: datetime

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if _utc(self.recorded_at) < _utc(self.knowledge_at):
            raise ValueError("disposition recorded_at must not precede knowledge_at")
        if self.terminal_status == "succeeded" and not self.evidence:
            raise ValueError("succeeded disposition requires committed evidence")
        if self.terminal_status == "not_applicable" and self.evidence:
            raise ValueError("not_applicable disposition cannot cite processing output")
        keys = {(item.evidence_table, item.evidence_id) for item in self.evidence}
        if len(keys) != len(self.evidence):
            raise ValueError("disposition evidence references must be unique")
        return self


class DocumentProcessingSnapshotReceipt(_Frozen):
    processing_snapshot_id: str
    member_count: int = Field(ge=0)
    member_set_sha256: str
    cutoff_at: datetime

    _sha = field_validator("member_set_sha256")(_validate_sha)


class CorpusProjectionBundle(_Frozen):
    corpus_manifest_id: str = Field(min_length=1, max_length=128)
    lexical_index_run_id: str = Field(min_length=1, max_length=128)
    vector_index_run_id: str | None = None
    embedding_promotion_id: str | None = None

    @model_validator(mode="after")
    def _projection_shape(self) -> Self:
        semantic = (self.vector_index_run_id, self.embedding_promotion_id)
        if (semantic[0] is None) != (semantic[1] is None):
            raise ValueError("semantic corpus projection requires vector seal and promotion")
        return self


class ResearchUniverse(_Frozen):
    """The exact legal/reporting subject and evidence boundary for one answer."""

    issuer_id: str = Field(min_length=1, max_length=128)
    reporting_entity_ids: tuple[str, ...] = Field(min_length=1)
    document_version_ids: tuple[str, ...] = Field(min_length=1)
    source_obligation_revision_ids: tuple[str, ...] = Field(min_length=1)

    @field_validator(
        "reporting_entity_ids",
        "document_version_ids",
        "source_obligation_revision_ids",
    )
    @classmethod
    def _sorted_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item for item in value):
            raise ValueError("research universe identifiers must be non-empty")
        if tuple(sorted(set(value))) != value:
            raise ValueError("research universe identifiers must be sorted and unique")
        return value


class ResearchSnapshotRequest(_Frozen):
    research_snapshot_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    research_universe: ResearchUniverse
    processing_snapshot_ids: tuple[str, ...]
    corpus_bundles: tuple[CorpusProjectionBundle, ...]
    source_fact_publication_ids: tuple[str, ...]
    ontology_snapshot_id: str = Field(min_length=1, max_length=128)
    canonical_fact_resolution_snapshot_id: str = Field(min_length=1, max_length=128)
    canonical_fact_projection_run_id: str = Field(min_length=1, max_length=128)
    cutoff_at: datetime
    recorded_at: datetime

    @model_validator(mode="after")
    def _request_shape(self) -> Self:
        if not self.processing_snapshot_ids:
            raise ValueError("Research Snapshot requires a processing snapshot")
        if not self.corpus_bundles:
            raise ValueError("Research Snapshot requires a corpus projection bundle")
        if len(set(self.processing_snapshot_ids)) != len(self.processing_snapshot_ids):
            raise ValueError("processing snapshot ids must be unique")
        if list(self.processing_snapshot_ids) != sorted(self.processing_snapshot_ids):
            raise ValueError("processing snapshot ids must be sorted")
        if len(set(self.source_fact_publication_ids)) != len(self.source_fact_publication_ids):
            raise ValueError("publication ids must be unique")
        if list(self.source_fact_publication_ids) != sorted(self.source_fact_publication_ids):
            raise ValueError("publication ids must be sorted")
        manifest_ids = [item.corpus_manifest_id for item in self.corpus_bundles]
        if len(set(manifest_ids)) != len(manifest_ids):
            raise ValueError("corpus manifest ids must be unique")
        if manifest_ids != sorted(manifest_ids):
            raise ValueError("corpus bundles must be sorted by manifest id")
        if _utc(self.recorded_at) < _utc(self.cutoff_at):
            raise ValueError("Research Snapshot recorded_at must follow cutoff_at")
        return self


class VerifiedResearchReference(_Frozen):
    requested_lane: str
    reference_table: str
    reference_id: str
    commitment_sha256: str
    knowledge_at: datetime
    recorded_at: datetime
    attributes: dict[str, object] = {}

    _sha = field_validator("commitment_sha256")(_validate_sha)

    @model_validator(mode="after")
    def _clocks(self) -> Self:
        if _utc(self.recorded_at) < _utc(self.knowledge_at):
            raise ValueError("research reference recorded_at must not precede knowledge_at")
        return self


class _ResearchReferenceVerifier(Protocol):
    def verify(
        self,
        conn: sqlite3.Connection,
        *,
        requested_lane: str,
        reference_id: str,
        cutoff_at: datetime,
        request: ResearchSnapshotRequest,
    ) -> VerifiedResearchReference: ...


class ResearchSnapshotAdmission(_Frozen):
    research_snapshot_id: str
    admitted: Literal[True] = True
    cutoff_at: datetime
    member_count: int = Field(gt=0)
    member_set_sha256: str
    requested_lanes: tuple[str, ...]

    _sha = field_validator("member_set_sha256")(_validate_sha)


@dataclass(frozen=True)
class _DocumentState:
    document_version_id: str
    issuer_id: str
    document_type: str
    form_type: str
    media_type: str
    blob_sha256: str
    document_recorded_at: datetime
    evidence_knowledge_at: datetime
    evidence_recorded_at: datetime


@contextmanager
def _savepoint(conn: sqlite3.Connection, name: str) -> Generator[None, None, None]:
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
        conn.execute(f"RELEASE SAVEPOINT {name}")
        raise
    conn.execute(f"RELEASE SAVEPOINT {name}")


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
    }


def _require_columns(conn: sqlite3.Connection, table: str, columns: set[str]) -> None:
    if table not in _tables(conn):
        raise RuntimeError(f"required evidence table {table!r} is unavailable")
    present = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    missing = sorted(columns - present)
    if missing:
        raise RuntimeError(
            f"required evidence table {table!r} lacks clocks/commitments: " + ", ".join(missing)
        )


def _insert_exact(
    conn: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
    values: tuple[object, ...],
    *,
    idempotency_key: str,
) -> bool:
    serialized = tuple(
        _db_time(value) if isinstance(value, datetime) else value for value in values
    )
    existing = conn.execute(
        f"SELECT {','.join(columns)} FROM {table} WHERE idempotency_key=?",  # nosec B608 -- trusted internal SQL shape; values remain bound
        (idempotency_key,),
    ).fetchone()
    if existing is not None:
        if tuple(existing) != serialized:
            raise ValueError(f"idempotency conflict for {table}")
        return False
    conn.execute(
        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",  # nosec B608 -- trusted internal SQL shape; values remain bound
        serialized,
    )
    return True


def _document_family(document_type: str, form_type: str) -> str:
    value = f"{document_type} {form_type}".lower()
    if any(token in value for token in ("presentation", "slides", "deck")):
        return "issuer_presentations"
    if any(token in value for token in ("earnings", "transcript", "press_release")):
        return "issuer_earnings_materials"
    if any(token in value for token in ("10-k", "10-q", "20-f", "40-f", "periodic")):
        return "operating_company_periodic"
    if any(token in value for token in ("annual_report", "financial_statement")):
        return "issuer_financial_statements"
    return "continuous_disclosure"


def _applicable_lanes(state: _DocumentState) -> set[ProcessingLane]:
    media = state.media_type.partition(";")[0].lower()
    descriptor = f"{state.document_type} {state.form_type}".lower()
    lanes: set[ProcessingLane] = set()
    if media in {"text/html", "application/xhtml+xml"}:
        lanes.add("html_native_hierarchy")
    if media == "application/pdf":
        lanes.update(("pdf_text", "pdf_ocr", "pdf_table"))
    if media in {"image/jpeg", "image/png", "image/tiff"}:
        lanes.add("image_ocr")
    if "presentationml" in media or media.endswith("powerpoint"):
        lanes.update(("pptx_slides", "pptx_charts", "pptx_tables"))
    if "spreadsheetml" in media or media.endswith("excel"):
        lanes.update(("xlsx_workbook", "xlsx_sheets", "xlsx_tables"))
    if "transcript" in descriptor:
        lanes.update(("transcript_turns", "transcript_speakers"))
    if any(token in descriptor for token in ("10-k", "10-q", "20-f", "40-f", "xbrl")):
        lanes.add("filing_xbrl")
    return lanes


def _scope_filter(scope: DocumentProcessingScope) -> tuple[str, list[object]]:
    clauses: list[str] = []
    parameters: list[object] = []
    if scope.issuer_ids:
        clauses.append(f"document.issuer_id IN ({','.join('?' for _ in scope.issuer_ids)})")
        parameters.extend(scope.issuer_ids)
    if scope.document_version_ids:
        clauses.append(
            f"document.document_version_id IN ({','.join('?' for _ in scope.document_version_ids)})"
        )
        parameters.extend(scope.document_version_ids)
    return "(" + " OR ".join(clauses) + ")", parameters


def _document_states(
    conn: sqlite3.Connection,
    scope: DocumentProcessingScope,
    cutoff_at: datetime,
) -> tuple[_DocumentState, ...]:
    for table, columns in (
        (
            "evidence_document_versions",
            {
                "document_version_id",
                "document_key",
                "issuer_id",
                "document_type",
                "form_type",
                "observation_id",
                "blob_sha256",
                "recorded_at",
                "version_sequence",
            },
        ),
        (
            "evidence_source_observations",
            {"observation_id", "retrieved_at", "blob_sha256"},
        ),
        ("evidence_content_blobs", {"sha256", "media_type", "recorded_at"}),
    ):
        _require_columns(conn, table, columns)
    scope_sql, parameters = _scope_filter(scope)
    cutoff = _db_time(cutoff_at)
    rows = conn.execute(
        """
        SELECT document.document_version_id,document.issuer_id,
               document.document_type,document.form_type,blob.media_type,
               document.blob_sha256,document.recorded_at,
               observation.retrieved_at,blob.recorded_at
        FROM evidence_document_versions document
        JOIN evidence_source_observations observation
          ON observation.observation_id=document.observation_id
         AND observation.blob_sha256=document.blob_sha256
        JOIN evidence_content_blobs blob ON blob.sha256=document.blob_sha256
        WHERE """  # nosec B608 -- trusted internal SQL shape; values remain bound
        + scope_sql
        + """
          AND datetime(observation.retrieved_at)<=datetime(?)
          AND datetime(document.recorded_at)<=datetime(?)
          AND datetime(blob.recorded_at)<=datetime(?)
        ORDER BY document.document_version_id
        """,
        (*parameters, cutoff, cutoff, cutoff),
    ).fetchall()
    return tuple(
        _DocumentState(
            document_version_id=str(row[0]),
            issuer_id=str(row[1]),
            document_type=str(row[2]),
            form_type=str(row[3]),
            media_type=str(row[4]),
            blob_sha256=str(row[5]),
            document_recorded_at=_parse_time(row[6]),
            evidence_knowledge_at=_parse_time(row[7]),
            evidence_recorded_at=_parse_time(row[8]),
        )
        for row in rows
    )


def _source_obligations(
    conn: sqlite3.Connection,
    *,
    issuer_id: str,
    document_family: str,
    cutoff_at: datetime,
    include_optional: bool,
) -> tuple[sqlite3.Row, ...]:
    _require_columns(
        conn,
        "source_obligation_revisions",
        {
            "obligation_revision_id",
            "obligation_key",
            "revision",
            "issuer_id",
            "document_family",
            "obligation_state",
            "active_from",
            "active_to",
            "effective_at",
            "knowledge_at",
            "recorded_at",
        },
    )
    cutoff = _db_time(cutoff_at)
    states = ("required", "optional") if include_optional else ("required",)
    rows = conn.execute(
        """
        SELECT * FROM source_obligation_revisions obligation
        WHERE obligation.issuer_id=?
          AND obligation.document_family=?
          AND obligation.obligation_state IN """  # nosec B608 -- trusted internal SQL shape; values remain bound
        + f"({','.join('?' for _ in states)})"
        + """
          AND datetime(obligation.active_from)<=datetime(?)
          AND (obligation.active_to IS NULL
               OR datetime(obligation.active_to)>datetime(?))
          AND datetime(obligation.knowledge_at)<=datetime(?)
          AND datetime(obligation.recorded_at)<=datetime(?)
          AND NOT EXISTS (
              SELECT 1 FROM source_obligation_revisions newer
              WHERE newer.obligation_key=obligation.obligation_key
                AND newer.revision>obligation.revision
                AND datetime(newer.knowledge_at)<=datetime(?)
                AND datetime(newer.recorded_at)<=datetime(?))
        ORDER BY obligation.obligation_key,obligation.revision
        """,  # nosec B608 -- trusted internal SQL shape; values remain bound
        (
            issuer_id,
            document_family,
            *states,
            cutoff,
            cutoff,
            cutoff,
            cutoff,
            cutoff,
            cutoff,
        ),
    ).fetchall()
    if not rows:
        raise ValueError(f"document lacks an applicable source obligation for {document_family}")
    return tuple(rows)


def _derive_obligations(
    conn: sqlite3.Connection,
    scope: DocumentProcessingScope,
    cutoff: datetime,
    policy: DocumentProcessingPolicy,
    *,
    persist: bool,
) -> tuple[DocumentProcessingObligation, ...]:
    conn.row_factory = sqlite3.Row
    states = _document_states(conn, scope, cutoff)
    obligations: list[DocumentProcessingObligation] = []
    with _savepoint(conn, "derive_document_processing_obligations"):
        for document in states:
            sources = _source_obligations(
                conn,
                issuer_id=document.issuer_id,
                document_family=_document_family(document.document_type, document.form_type),
                cutoff_at=cutoff,
                include_optional=policy.include_optional_source_obligations,
            )
            applicable = _applicable_lanes(document)
            for source in sources:
                source_knowledge = _parse_time(source["knowledge_at"])
                source_recorded = _parse_time(source["recorded_at"])
                knowledge_at = max(source_knowledge, document.evidence_knowledge_at)
                recorded_at = max(
                    source_recorded,
                    document.document_recorded_at,
                    document.evidence_recorded_at,
                    knowledge_at,
                )
                for lane in _LANES:
                    key = f"{source['obligation_key']}:{document.document_version_id}:{lane}"
                    applicability = "applicable" if lane in applicable else "not_applicable"
                    source_state = {
                        "blob_sha256": document.blob_sha256,
                        "document_recorded_at": _utc(document.document_recorded_at).isoformat(),
                        "document_type": document.document_type,
                        "document_version_id": document.document_version_id,
                        "evidence_knowledge_at": _utc(document.evidence_knowledge_at).isoformat(),
                        "evidence_recorded_at": _utc(document.evidence_recorded_at).isoformat(),
                        "form_type": document.form_type,
                        "issuer_id": document.issuer_id,
                        "media_type": document.media_type,
                        "source_obligation_key": str(source["obligation_key"]),
                        "source_obligation_revision_id": str(source["obligation_revision_id"]),
                        "source_obligation_revision": int(source["revision"]),
                    }
                    source_json = canonical_json(source_state)
                    commitment = {
                        "applicability": applicability,
                        "document_version_id": document.document_version_id,
                        "policy_config_sha256": policy.config_sha256,
                        "processing_lane": lane,
                        "processing_obligation_key": key,
                        "source_obligation_revision_id": str(source["obligation_revision_id"]),
                        "source_state_sha256": _digest_text(source_json),
                    }
                    commitment_json = canonical_json(commitment)
                    commitment_sha256 = _digest_text(commitment_json)
                    replay = conn.execute(
                        """
                        SELECT * FROM document_processing_obligation_revisions
                        WHERE processing_obligation_key=?
                          AND commitment_sha256=?
                          AND datetime(knowledge_at)<=datetime(?)
                          AND datetime(recorded_at)<=datetime(?)
                        ORDER BY revision DESC
                        LIMIT 1
                        """,
                        (
                            key,
                            commitment_sha256,
                            _db_time(cutoff),
                            _db_time(cutoff),
                        ),
                    ).fetchone()
                    if replay is not None:
                        obligations.append(
                            DocumentProcessingObligation(
                                processing_obligation_revision_id=str(
                                    replay["processing_obligation_revision_id"]
                                ),
                                processing_obligation_key=key,
                                source_obligation_revision_id=str(
                                    replay["source_obligation_revision_id"]
                                ),
                                document_version_id=document.document_version_id,
                                processing_lane=lane,
                                applicability=cast(
                                    Literal["applicable", "not_applicable"],
                                    replay["applicability"],
                                ),
                                commitment_sha256=str(replay["commitment_sha256"]),
                                knowledge_at=_parse_time(replay["knowledge_at"]),
                                recorded_at=_parse_time(replay["recorded_at"]),
                            )
                        )
                        continue
                    if not persist:
                        raise ValueError("Document Processing Obligation preparation is incomplete")

                    prior = conn.execute(
                        """
                        SELECT revision,processing_obligation_revision_id
                        FROM document_processing_obligation_revisions
                        WHERE processing_obligation_key=?
                        ORDER BY revision DESC
                        LIMIT 1
                        """,
                        (key,),
                    ).fetchone()
                    revision = 1 if prior is None else int(prior["revision"]) + 1
                    supersedes = (
                        None if prior is None else str(prior["processing_obligation_revision_id"])
                    )
                    obligation_id = "processing-obligation:" + _digest(
                        {
                            "commitment_sha256": commitment_sha256,
                            "key": key,
                            "revision": revision,
                        }
                    )
                    columns = (
                        "processing_obligation_revision_id",
                        "idempotency_key",
                        "processing_obligation_key",
                        "revision",
                        "source_obligation_revision_id",
                        "document_version_id",
                        "processing_lane",
                        "applicability",
                        "policy_name",
                        "policy_version",
                        "policy_config_sha256",
                        "source_state_json",
                        "source_state_sha256",
                        "commitment_json",
                        "commitment_sha256",
                        "effective_at",
                        "knowledge_at",
                        "recorded_at",
                        "supersedes_processing_obligation_revision_id",
                    )
                    values = (
                        obligation_id,
                        obligation_id,
                        key,
                        revision,
                        str(source["obligation_revision_id"]),
                        document.document_version_id,
                        lane,
                        applicability,
                        policy.policy_name,
                        policy.policy_version,
                        policy.config_sha256,
                        source_json,
                        _digest_text(source_json),
                        commitment_json,
                        commitment_sha256,
                        _parse_time(source["effective_at"]),
                        knowledge_at,
                        recorded_at,
                        supersedes,
                    )
                    _insert_exact(
                        conn,
                        "document_processing_obligation_revisions",
                        columns,
                        values,
                        idempotency_key=obligation_id,
                    )
                    obligations.append(
                        DocumentProcessingObligation(
                            processing_obligation_revision_id=obligation_id,
                            processing_obligation_key=key,
                            source_obligation_revision_id=str(source["obligation_revision_id"]),
                            document_version_id=document.document_version_id,
                            processing_lane=lane,
                            applicability=applicability,
                            commitment_sha256=commitment_sha256,
                            knowledge_at=knowledge_at,
                            recorded_at=recorded_at,
                        )
                    )
    return tuple(
        sorted(
            obligations,
            key=lambda item: (
                item.document_version_id,
                item.source_obligation_revision_id,
                item.processing_lane,
            ),
        )
    )


def derive_obligations(
    conn: sqlite3.Connection,
    scope: DocumentProcessingScope,
    cutoff: datetime,
    policy: DocumentProcessingPolicy,
) -> tuple[DocumentProcessingObligation, ...]:
    """Prepare and persist every lane from as-known source/document evidence."""

    return _derive_obligations(conn, scope, cutoff, policy, persist=True)


def _verify_processing_evidence(
    conn: sqlite3.Connection,
    obligation: sqlite3.Row,
    reference: ProcessingEvidenceReference,
    cutoff_at: datetime,
) -> None:
    if obligation["processing_lane"] != "filing_xbrl":
        if reference.evidence_table != "document_processing_evidence_seals":
            raise ValueError("processing evidence source lacks an approved exact verifier")
        from provenance.document_processing_evidence import (
            verify_document_processing_evidence,
        )

        verified = verify_document_processing_evidence(
            conn,
            reference.evidence_id,
            document_version_id=str(obligation["document_version_id"]),
            processing_lane=str(obligation["processing_lane"]),
            cutoff_at=cutoff_at,
        )
        if verified.member_set_sha256 != reference.evidence_commitment_sha256:
            raise ValueError("processing evidence commitment is missing or mismatched")
        if _utc(verified.knowledge_at) != _utc(reference.knowledge_at) or _utc(
            verified.recorded_at
        ) != _utc(reference.recorded_at):
            raise ValueError("processing evidence clocks do not match the live seal")
        return
    if reference.evidence_table != "filing_xbrl_extraction_disposition_seals":
        raise ValueError("processing evidence source lacks an approved exact verifier")
    _require_columns(
        conn,
        reference.evidence_table,
        {
            "disposition_seal_id",
            "extraction_run_id",
            "publication_id",
            "normalized_output_schema_name",
            "normalized_output_schema_version",
            "extraction_output_sha256",
            "entry_count",
            "published_count",
            "duplicate_count",
            "quarantined_count",
            "canonical_disposition_set_json",
            "disposition_set_sha256",
            "completeness_policy_sha256",
            "knowledge_at",
            "recorded_at",
        },
    )
    _require_columns(
        conn,
        "evidence_extraction_runs",
        {
            "extraction_run_id",
            "document_version_id",
            "output_sha256",
            "outcome",
        },
    )
    _require_columns(
        conn,
        "filing_xbrl_extraction_dispositions",
        {
            "extraction_run_id",
            "input_ordinal",
            "disposition",
            "canonical_disposition_json",
            "disposition_sha256",
        },
    )
    cutoff = _db_time(cutoff_at)
    seal_row = conn.execute(
        "SELECT * FROM filing_xbrl_extraction_disposition_seals "
        "WHERE disposition_seal_id=? "
        "AND datetime(knowledge_at)<=datetime(?) "
        "AND datetime(recorded_at)<=datetime(?)",
        (
            reference.evidence_id,
            cutoff,
            cutoff,
        ),
    ).fetchone()
    if seal_row is None:
        raise ValueError("processing evidence commitment is missing or mismatched")
    from provenance.filing_xbrl_extraction_ledger import (
        FilingXbrlExtractionDispositionRecord,
        FilingXbrlExtractionDispositionSeal,
    )

    seal = FilingXbrlExtractionDispositionSeal.model_validate(dict(seal_row))
    run = conn.execute(
        "SELECT document_version_id,output_sha256,outcome "
        "FROM evidence_extraction_runs WHERE extraction_run_id=?",
        (seal.extraction_run_id,),
    ).fetchone()
    if (
        run is None
        or str(run[0]) != str(obligation["document_version_id"])
        or str(run[1]) != seal.extraction_output_sha256
        or str(run[2]) != "succeeded"
    ):
        raise ValueError("filing-XBRL evidence extraction identity mismatch")
    member_rows = conn.execute(
        "SELECT * "
        "FROM filing_xbrl_extraction_dispositions "
        "WHERE extraction_run_id=? ORDER BY input_ordinal",
        (seal.extraction_run_id,),
    ).fetchall()
    members = tuple(
        FilingXbrlExtractionDispositionRecord.model_validate(dict(member)) for member in member_rows
    )
    canonical_members: list[object] = []
    counts = {"published": 0, "duplicate": 0, "quarantined": 0}
    for ordinal, member in enumerate(members):
        if member.input_ordinal != ordinal:
            raise ValueError("filing-XBRL evidence members are non-contiguous")
        counts[member.disposition] += 1
        canonical_members.append(json.loads(member.canonical_disposition_json))
    member_set_json = canonical_json(canonical_members)
    if (
        seal.entry_count != len(members)
        or seal.published_count != counts["published"]
        or seal.duplicate_count != counts["duplicate"]
        or seal.quarantined_count != counts["quarantined"]
        or seal.canonical_disposition_set_json != member_set_json
        or seal.disposition_set_sha256 != _digest_text(member_set_json)
    ):
        raise ValueError("filing-XBRL evidence final seal mismatch")
    from provenance.source_fact_publication import verify_source_fact_publication

    verify_source_fact_publication(
        conn,
        publication_id=seal.publication_id,
        cutoff=cutoff_at,
    )
    commitment = seal.disposition_set_sha256
    if commitment != reference.evidence_commitment_sha256:
        raise ValueError("processing evidence commitment is missing or mismatched")
    if _utc(seal.knowledge_at) != _utc(reference.knowledge_at) or _utc(seal.recorded_at) != _utc(
        reference.recorded_at
    ):
        raise ValueError("processing evidence clocks do not match the live seal")


def record_disposition(
    conn: sqlite3.Connection,
    disposition: DocumentProcessingDisposition,
) -> None:
    """Record one terminal result; callers cannot change obligation membership."""

    conn.row_factory = sqlite3.Row
    obligation = conn.execute(
        "SELECT * FROM document_processing_obligation_revisions "
        "WHERE processing_obligation_revision_id=?",
        (disposition.processing_obligation_revision_id,),
    ).fetchone()
    if obligation is None:
        raise ValueError("unknown Document Processing Obligation")
    if disposition.terminal_status == "succeeded":
        if obligation["applicability"] != "applicable":
            raise ValueError("only applicable obligations may succeed")
        for reference in disposition.evidence:
            _verify_processing_evidence(conn, obligation, reference, disposition.knowledge_at)
    elif disposition.terminal_status == "not_applicable":
        if obligation["applicability"] != "not_applicable":
            raise ValueError("not_applicable requires a non-applicable obligation")
    reason_json = canonical_json(disposition.reason_details)
    commitment = {
        "evidence": [reference.model_dump(mode="json") for reference in disposition.evidence],
        "processing_obligation_revision_id": disposition.processing_obligation_revision_id,
        "reason_code": disposition.reason_code,
        "reason_details_sha256": _digest_text(reason_json),
        "terminal_status": disposition.terminal_status,
    }
    commitment_json = canonical_json(commitment)
    with _savepoint(conn, "record_document_processing_disposition"):
        _insert_exact(
            conn,
            "document_processing_disposition_headers",
            (
                "processing_disposition_id",
                "idempotency_key",
                "processing_obligation_revision_id",
                "terminal_status",
                "reason_code",
                "reason_details_json",
                "reason_details_sha256",
                "commitment_json",
                "commitment_sha256",
                "knowledge_at",
                "recorded_at",
            ),
            (
                disposition.processing_disposition_id,
                disposition.idempotency_key,
                disposition.processing_obligation_revision_id,
                disposition.terminal_status,
                disposition.reason_code,
                reason_json,
                _digest_text(reason_json),
                commitment_json,
                _digest_text(commitment_json),
                disposition.knowledge_at,
                disposition.recorded_at,
            ),
            idempotency_key=disposition.idempotency_key,
        )
        existing = conn.execute(
            "SELECT COUNT(*) FROM document_processing_disposition_members "
            "WHERE processing_disposition_id=?",
            (disposition.processing_disposition_id,),
        ).fetchone()
        if existing is None or int(existing[0]) == 0:
            for ordinal, reference in enumerate(disposition.evidence):
                member = {
                    "evidence_commitment_sha256": (reference.evidence_commitment_sha256),
                    "evidence_id": reference.evidence_id,
                    "evidence_knowledge_at": _utc(reference.knowledge_at).isoformat(),
                    "evidence_recorded_at": _utc(reference.recorded_at).isoformat(),
                    "evidence_table": reference.evidence_table,
                }
                member_json = canonical_json(member)
                conn.execute(
                    "INSERT INTO document_processing_disposition_members "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        disposition.processing_disposition_id,
                        ordinal,
                        reference.evidence_table,
                        reference.evidence_id,
                        reference.evidence_commitment_sha256,
                        _db_time(reference.knowledge_at),
                        _db_time(reference.recorded_at),
                        member_json,
                        _digest_text(member_json),
                    ),
                )


def _member_set(
    conn: sqlite3.Connection,
    table: str,
    key_column: str,
    key: str,
) -> tuple[list[sqlite3.Row], str, str]:
    rows = conn.execute(
        f"SELECT * FROM {table} WHERE {key_column}=? ORDER BY member_ordinal",  # nosec B608 -- trusted internal SQL shape; values remain bound
        (key,),
    ).fetchall()
    payload = canonical_json([json.loads(str(row["canonical_member_json"])) for row in rows])
    return rows, payload, _digest_text(payload)


def seal_disposition(
    conn: sqlite3.Connection,
    processing_disposition_id: str,
    *,
    sealed_at: datetime,
) -> None:
    conn.row_factory = sqlite3.Row
    header = conn.execute(
        "SELECT header.*,obligation.document_version_id,"
        "obligation.processing_lane,obligation.applicability "
        "FROM document_processing_disposition_headers header "
        "JOIN document_processing_obligation_revisions obligation "
        "ON obligation.processing_obligation_revision_id="
        "header.processing_obligation_revision_id "
        "WHERE header.processing_disposition_id=?",
        (processing_disposition_id,),
    ).fetchone()
    if header is None:
        raise ValueError("unknown Document Processing Disposition")
    rows, payload, digest = _member_set(
        conn,
        "document_processing_disposition_members",
        "processing_disposition_id",
        processing_disposition_id,
    )
    if header["terminal_status"] == "succeeded" and not rows:
        raise ValueError("succeeded disposition is missing evidence")
    if header["terminal_status"] == "not_applicable" and rows:
        raise ValueError("not_applicable disposition has unexpected evidence")
    for row in rows:
        reference = ProcessingEvidenceReference(
            evidence_table=str(row["evidence_table"]),
            evidence_id=str(row["evidence_id"]),
            evidence_commitment_sha256=str(row["evidence_commitment_sha256"]),
            knowledge_at=_parse_time(row["evidence_knowledge_at"]),
            recorded_at=_parse_time(row["evidence_recorded_at"]),
        )
        _verify_processing_evidence(conn, header, reference, sealed_at)
    existing = conn.execute(
        "SELECT member_count,canonical_member_set_json,member_set_sha256,sealed_at "
        "FROM document_processing_disposition_seals "
        "WHERE processing_disposition_id=?",
        (processing_disposition_id,),
    ).fetchone()
    expected = (len(rows), payload, digest, _db_time(sealed_at))
    if existing is not None:
        if tuple(existing) != expected:
            raise ValueError("Document Processing Disposition seal conflict")
        return
    conn.execute(
        "INSERT INTO document_processing_disposition_seals VALUES (?,?,?,?,?)",
        (processing_disposition_id, *expected),
    )


def _verify_disposition(
    conn: sqlite3.Connection,
    processing_disposition_id: str,
    cutoff_at: datetime,
) -> sqlite3.Row:
    conn.row_factory = sqlite3.Row
    header = conn.execute(
        "SELECT header.*,obligation.document_version_id,"
        "obligation.processing_lane,obligation.applicability "
        "FROM document_processing_disposition_headers header "
        "JOIN document_processing_obligation_revisions obligation "
        "ON obligation.processing_obligation_revision_id="
        "header.processing_obligation_revision_id "
        "WHERE header.processing_disposition_id=? "
        "AND datetime(header.knowledge_at)<=datetime(?) "
        "AND datetime(header.recorded_at)<=datetime(?)",
        (
            processing_disposition_id,
            _db_time(cutoff_at),
            _db_time(cutoff_at),
        ),
    ).fetchone()
    if header is None:
        raise ValueError("disposition is absent at cutoff")
    rows, payload, digest = _member_set(
        conn,
        "document_processing_disposition_members",
        "processing_disposition_id",
        processing_disposition_id,
    )
    seal = conn.execute(
        "SELECT member_count,canonical_member_set_json,member_set_sha256,sealed_at "
        "FROM document_processing_disposition_seals "
        "WHERE processing_disposition_id=? "
        "AND datetime(sealed_at)<=datetime(?)",
        (processing_disposition_id, _db_time(cutoff_at)),
    ).fetchone()
    if seal is None or tuple(seal[:3]) != (len(rows), payload, digest):
        raise ValueError("Document Processing Disposition seal mismatch")
    if header["terminal_status"] == "succeeded" and not rows:
        raise ValueError("succeeded disposition lacks evidence")
    if header["terminal_status"] == "not_applicable" and rows:
        raise ValueError("not_applicable disposition has unexpected evidence")
    if (header["terminal_status"] == "succeeded" and header["applicability"] != "applicable") or (
        header["terminal_status"] == "not_applicable"
        and header["applicability"] != "not_applicable"
    ):
        raise ValueError("disposition status does not match obligation applicability")
    evidence_payload: list[dict[str, object]] = []
    for row in rows:
        reference = ProcessingEvidenceReference(
            evidence_table=str(row["evidence_table"]),
            evidence_id=str(row["evidence_id"]),
            evidence_commitment_sha256=str(row["evidence_commitment_sha256"]),
            knowledge_at=_parse_time(row["evidence_knowledge_at"]),
            recorded_at=_parse_time(row["evidence_recorded_at"]),
        )
        member = {
            "evidence_commitment_sha256": reference.evidence_commitment_sha256,
            "evidence_id": reference.evidence_id,
            "evidence_knowledge_at": _utc(reference.knowledge_at).isoformat(),
            "evidence_recorded_at": _utc(reference.recorded_at).isoformat(),
            "evidence_table": reference.evidence_table,
        }
        member_json = canonical_json(member)
        if str(row["canonical_member_json"]) != member_json or str(
            row["member_sha256"]
        ) != _digest_text(member_json):
            raise ValueError("processing evidence member commitment mismatch")
        evidence_payload.append(reference.model_dump(mode="json"))
        _verify_processing_evidence(conn, header, reference, cutoff_at)
    reason_json = canonical_json(json.loads(str(header["reason_details_json"])))
    commitment_json = canonical_json(
        {
            "evidence": evidence_payload,
            "processing_obligation_revision_id": str(header["processing_obligation_revision_id"]),
            "reason_code": str(header["reason_code"]),
            "reason_details_sha256": _digest_text(reason_json),
            "terminal_status": str(header["terminal_status"]),
        }
    )
    if (
        str(header["reason_details_json"]) != reason_json
        or str(header["reason_details_sha256"]) != _digest_text(reason_json)
        or str(header["commitment_json"]) != commitment_json
        or str(header["commitment_sha256"]) != _digest_text(commitment_json)
    ):
        raise ValueError("Document Processing Disposition commitment mismatch")
    return header


def seal_processing_snapshot(
    conn: sqlite3.Connection,
    *,
    processing_snapshot_id: str,
    idempotency_key: str,
    scope: DocumentProcessingScope,
    cutoff_at: datetime,
    policy: DocumentProcessingPolicy,
    recorded_at: datetime,
) -> DocumentProcessingSnapshotReceipt:
    obligations = _derive_obligations(conn, scope, cutoff_at, policy, persist=False)
    scope_json = canonical_json(scope)
    policy_json = canonical_json(policy)
    members: list[dict[str, object]] = []
    rows: list[tuple[object, ...]] = []
    for obligation in obligations:
        matches = conn.execute(
            "SELECT header.processing_disposition_id "
            "FROM document_processing_disposition_headers header "
            "JOIN document_processing_disposition_seals seal "
            "ON seal.processing_disposition_id=header.processing_disposition_id "
            "WHERE header.processing_obligation_revision_id=? "
            "AND datetime(header.knowledge_at)<=datetime(?) "
            "AND datetime(header.recorded_at)<=datetime(?) "
            "AND datetime(seal.sealed_at)<=datetime(?)",
            (
                obligation.processing_obligation_revision_id,
                _db_time(cutoff_at),
                _db_time(cutoff_at),
                _db_time(cutoff_at),
            ),
        ).fetchall()
        if len(matches) != 1:
            raise ValueError("every processing lane requires exactly one terminal seal")
        disposition_id = str(matches[0][0])
        header = _verify_disposition(conn, disposition_id, cutoff_at)
        if header["terminal_status"] not in {"succeeded", "not_applicable"}:
            raise ValueError("only succeeded or valid not_applicable admits a document")
        member: dict[str, object] = {
            "document_version_id": obligation.document_version_id,
            "processing_disposition_id": disposition_id,
            "processing_lane": obligation.processing_lane,
            "processing_obligation_revision_id": (obligation.processing_obligation_revision_id),
            "terminal_status": str(header["terminal_status"]),
        }
        member_json = canonical_json(member)
        members.append(member)
        rows.append(
            (
                processing_snapshot_id,
                len(rows),
                obligation.processing_obligation_revision_id,
                disposition_id,
                obligation.processing_lane,
                obligation.document_version_id,
                member_json,
                _digest_text(member_json),
            )
        )
    payload = canonical_json(members)
    digest = _digest_text(payload)
    with _savepoint(conn, "seal_document_processing_snapshot"):
        _insert_exact(
            conn,
            "document_processing_snapshot_headers",
            (
                "processing_snapshot_id",
                "idempotency_key",
                "scope_json",
                "scope_sha256",
                "policy_json",
                "policy_sha256",
                "cutoff_at",
                "recorded_at",
            ),
            (
                processing_snapshot_id,
                idempotency_key,
                scope_json,
                _digest_text(scope_json),
                policy_json,
                _digest_text(policy_json),
                cutoff_at,
                recorded_at,
            ),
            idempotency_key=idempotency_key,
        )
        existing_count = conn.execute(
            "SELECT COUNT(*) FROM document_processing_snapshot_members "
            "WHERE processing_snapshot_id=?",
            (processing_snapshot_id,),
        ).fetchone()
        if existing_count is None or int(existing_count[0]) == 0:
            conn.executemany(
                "INSERT INTO document_processing_snapshot_members VALUES (?,?,?,?,?,?,?,?)",
                rows,
            )
        seal = conn.execute(
            "SELECT member_count,canonical_member_set_json,member_set_sha256,"
            "sealed_at FROM document_processing_snapshot_seals "
            "WHERE processing_snapshot_id=?",
            (processing_snapshot_id,),
        ).fetchone()
        expected = (len(rows), payload, digest, _db_time(recorded_at))
        if seal is None:
            conn.execute(
                "INSERT INTO document_processing_snapshot_seals VALUES (?,?,?,?,?)",
                (processing_snapshot_id, *expected),
            )
        elif tuple(seal) != expected:
            raise ValueError("Document Processing Snapshot seal conflict")
    verify_processing_snapshot(conn, processing_snapshot_id)
    return DocumentProcessingSnapshotReceipt(
        processing_snapshot_id=processing_snapshot_id,
        member_count=len(rows),
        member_set_sha256=digest,
        cutoff_at=cutoff_at,
    )


def verify_processing_snapshot(
    conn: sqlite3.Connection,
    processing_snapshot_id: str,
) -> DocumentProcessingSnapshotReceipt:
    conn.row_factory = sqlite3.Row
    header = conn.execute(
        "SELECT * FROM document_processing_snapshot_headers WHERE processing_snapshot_id=?",
        (processing_snapshot_id,),
    ).fetchone()
    seal = conn.execute(
        "SELECT * FROM document_processing_snapshot_seals WHERE processing_snapshot_id=?",
        (processing_snapshot_id,),
    ).fetchone()
    if header is None or seal is None:
        raise ValueError("Document Processing Snapshot is not fully sealed")
    scope_payload = json.loads(str(header["scope_json"]))
    policy_payload = json.loads(str(header["policy_json"]))
    scope_json = canonical_json(scope_payload)
    policy_json = canonical_json(policy_payload)
    if (
        str(header["scope_json"]) != scope_json
        or str(header["scope_sha256"]) != _digest_text(scope_json)
        or str(header["policy_json"]) != policy_json
        or str(header["policy_sha256"]) != _digest_text(policy_json)
    ):
        raise ValueError("Document Processing Snapshot header commitment mismatch")
    scope = DocumentProcessingScope.model_validate(scope_payload)
    policy = DocumentProcessingPolicy.model_validate(policy_payload)
    cutoff = _parse_time(header["cutoff_at"])
    if _parse_time(header["recorded_at"]) < cutoff or _parse_time(seal["sealed_at"]) < _parse_time(
        header["recorded_at"]
    ):
        raise ValueError("Document Processing Snapshot seal clocks are invalid")
    obligations = _derive_obligations(conn, scope, cutoff, policy, persist=False)
    rows, payload, digest = _member_set(
        conn,
        "document_processing_snapshot_members",
        "processing_snapshot_id",
        processing_snapshot_id,
    )
    expected_ids = [obligation.processing_obligation_revision_id for obligation in obligations]
    actual_ids = [str(row["processing_obligation_revision_id"]) for row in rows]
    if expected_ids != actual_ids:
        raise ValueError(
            "Document Processing Snapshot has omitted, extra, or reordered obligations"
        )
    for row, obligation in zip(rows, obligations, strict=True):
        disposition = _verify_disposition(conn, str(row["processing_disposition_id"]), cutoff)
        if (
            str(disposition["processing_obligation_revision_id"])
            != obligation.processing_obligation_revision_id
            or str(row["document_version_id"]) != obligation.document_version_id
            or str(row["processing_lane"]) != obligation.processing_lane
        ):
            raise ValueError("Document Processing Snapshot coordinate mismatch")
        if disposition["terminal_status"] not in {"succeeded", "not_applicable"}:
            raise ValueError("Document Processing Snapshot contains a non-admitting lane")
        member = {
            "document_version_id": obligation.document_version_id,
            "processing_disposition_id": str(row["processing_disposition_id"]),
            "processing_lane": obligation.processing_lane,
            "processing_obligation_revision_id": (obligation.processing_obligation_revision_id),
            "terminal_status": str(disposition["terminal_status"]),
        }
        member_json = canonical_json(member)
        if str(row["canonical_member_json"]) != member_json or str(
            row["member_sha256"]
        ) != _digest_text(member_json):
            raise ValueError("Document Processing Snapshot member commitment mismatch")
    if (
        int(seal["member_count"]) != len(rows)
        or str(seal["canonical_member_set_json"]) != payload
        or str(seal["member_set_sha256"]) != digest
    ):
        raise ValueError("Document Processing Snapshot commitment mismatch")
    return DocumentProcessingSnapshotReceipt(
        processing_snapshot_id=processing_snapshot_id,
        member_count=len(rows),
        member_set_sha256=digest,
        cutoff_at=cutoff,
    )


def _research_lanes(request: ResearchSnapshotRequest) -> list[tuple[str, str]]:
    lanes: list[tuple[str, str]] = [("research_universe", request.research_snapshot_id)]
    lanes.extend((f"processing:{item}", item) for item in request.processing_snapshot_ids)
    for bundle in request.corpus_bundles:
        coordinate = bundle.corpus_manifest_id
        lanes.append((f"corpus:{coordinate}", bundle.corpus_manifest_id))
        lanes.append((f"lexical_projection:{coordinate}", bundle.lexical_index_run_id))
        if bundle.vector_index_run_id is not None:
            assert bundle.embedding_promotion_id is not None
            lanes.append((f"vector_projection:{coordinate}", bundle.vector_index_run_id))
            lanes.append(
                (
                    f"embedding_promotion:{coordinate}",
                    bundle.embedding_promotion_id,
                )
            )
    lanes.extend(
        (f"source_fact_publication:{item}", item) for item in request.source_fact_publication_ids
    )
    lanes.extend(
        (
            ("ontology_snapshot", request.ontology_snapshot_id),
            (
                "canonical_fact_resolution_snapshot",
                request.canonical_fact_resolution_snapshot_id,
            ),
            (
                "canonical_fact_projection:" + request.canonical_fact_resolution_snapshot_id,
                request.canonical_fact_projection_run_id,
            ),
        )
    )
    return lanes


def _required_source_fact_publications(
    conn: sqlite3.Connection,
    resolution_snapshot_id: str,
) -> tuple[str, ...]:
    _require_columns(
        conn,
        "canonical_fact_resolution_snapshot_members",
        {"resolution_snapshot_id", "candidate_universe_id"},
    )
    _require_columns(
        conn,
        "canonical_fact_candidate_dispositions",
        {"candidate_universe_id", "source_publication_id"},
    )
    rows = conn.execute(
        "SELECT DISTINCT candidate.source_publication_id "
        "FROM canonical_fact_resolution_snapshot_members member "
        "JOIN canonical_fact_candidate_dispositions candidate "
        "ON candidate.candidate_universe_id=member.candidate_universe_id "
        "WHERE member.resolution_snapshot_id=? "
        "AND candidate.source_publication_id IS NOT NULL "
        "ORDER BY candidate.source_publication_id",
        (resolution_snapshot_id,),
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


class _DefaultResearchReferenceVerifier:
    def verify(
        self,
        conn: sqlite3.Connection,
        *,
        requested_lane: str,
        reference_id: str,
        cutoff_at: datetime,
        request: ResearchSnapshotRequest,
    ) -> VerifiedResearchReference:
        if requested_lane == "research_universe":
            canonical = canonical_json(_universe_payload(request.research_universe))
            return VerifiedResearchReference(
                requested_lane=requested_lane,
                reference_table="research_snapshot_universe_commitments",
                reference_id=reference_id,
                commitment_sha256=_digest_text(canonical),
                knowledge_at=request.cutoff_at,
                recorded_at=request.recorded_at,
                attributes={
                    "issuer_id": request.research_universe.issuer_id,
                    "reporting_entity_ids": list(request.research_universe.reporting_entity_ids),
                },
            )
        if requested_lane.startswith("processing:"):
            receipt = verify_processing_snapshot(conn, reference_id)
            row = conn.execute(
                "SELECT recorded_at FROM document_processing_snapshot_headers "
                "WHERE processing_snapshot_id=?",
                (reference_id,),
            ).fetchone()
            assert row is not None
            return VerifiedResearchReference(
                requested_lane=requested_lane,
                reference_table="document_processing_snapshot_seals",
                reference_id=reference_id,
                commitment_sha256=receipt.member_set_sha256,
                knowledge_at=receipt.cutoff_at,
                recorded_at=_parse_time(row[0]),
            )
        if requested_lane.startswith("corpus:"):
            return self._corpus(conn, requested_lane, reference_id, cutoff_at)
        if requested_lane.startswith(("lexical_projection:", "vector_projection:")):
            return self._search_projection(conn, requested_lane, reference_id, cutoff_at)
        if requested_lane.startswith("source_fact_publication:"):
            from provenance.source_fact_publication import (
                verify_source_fact_publication,
            )

            verified = verify_source_fact_publication(
                conn, publication_id=reference_id, cutoff=cutoff_at
            )
            return VerifiedResearchReference(
                requested_lane=requested_lane,
                reference_table="source_fact_publication_seals",
                reference_id=verified.publication_seal_id,
                commitment_sha256=verified.member_set_sha256,
                knowledge_at=verified.cutoff,
                recorded_at=max(verified.recorded_at, verified.sealed_at),
            )
        if requested_lane == "ontology_snapshot":
            from provenance.metric_ontology import MetricOntology

            MetricOntology(conn).verify_snapshot(reference_id)
            row = conn.execute(
                "SELECT header.cutoff_at,header.recorded_at,seal.member_set_sha256 "
                "FROM ontology_snapshot_headers header "
                "JOIN ontology_snapshot_seals seal "
                "ON seal.ontology_snapshot_id=header.ontology_snapshot_id "
                "WHERE header.ontology_snapshot_id=? "
                "AND datetime(header.cutoff_at)<=datetime(?) "
                "AND datetime(header.recorded_at)<=datetime(?)",
                (reference_id, _db_time(cutoff_at), _db_time(cutoff_at)),
            ).fetchone()
            if row is None:
                raise ValueError("Ontology Snapshot is absent at cutoff")
            return VerifiedResearchReference(
                requested_lane=requested_lane,
                reference_table="ontology_snapshot_seals",
                reference_id=reference_id,
                commitment_sha256=str(row[2]),
                knowledge_at=_parse_time(row[0]),
                recorded_at=_parse_time(row[1]),
            )
        if requested_lane == "canonical_fact_resolution_snapshot":
            from provenance.canonical_fact_resolution import (
                CanonicalFactResolutionEngine,
            )

            verified = CanonicalFactResolutionEngine(conn).verify_snapshot(reference_id, cutoff_at)
            if _utc(verified.cutoff_at) > _utc(cutoff_at) or _utc(verified.recorded_at) > _utc(
                cutoff_at
            ):
                raise ValueError("Canonical Fact Resolution Snapshot is absent at cutoff")
            return VerifiedResearchReference(
                requested_lane=requested_lane,
                reference_table="canonical_fact_resolution_snapshot_scope_seals",
                reference_id=reference_id,
                commitment_sha256=verified.snapshot_commitment_sha256,
                knowledge_at=verified.cutoff_at,
                recorded_at=verified.recorded_at,
                attributes={
                    "issuer_id": verified.scope.issuer_id,
                    "reporting_entity_ids": list(verified.scope.reporting_entity_ids),
                    "scope_sha256": verified.scope_sha256,
                },
            )
        if requested_lane.startswith("canonical_fact_projection:"):
            from search.canonical_fact_projection import (
                verify_canonical_projection_generation,
            )

            verified = verify_canonical_projection_generation(
                conn,
                reference_id,
                resolution_snapshot_id=(request.canonical_fact_resolution_snapshot_id),
                ontology_snapshot_id=request.ontology_snapshot_id,
                cutoff_at=cutoff_at,
            )
            return VerifiedResearchReference(
                requested_lane=requested_lane,
                reference_table="canonical_fact_projection_seals",
                reference_id=verified.projection_seal_id,
                commitment_sha256=verified.projection_seal_sha256,
                knowledge_at=verified.knowledge_at,
                recorded_at=verified.recorded_at,
                attributes={
                    "ontology_snapshot_id": verified.ontology_snapshot_id,
                    "resolution_snapshot_id": verified.resolution_snapshot_id,
                    "resolution_scope_sha256": verified.resolution_scope_sha256,
                    "resolution_snapshot_commitment_sha256": (
                        verified.resolution_snapshot_commitment_sha256
                    ),
                },
            )
        if requested_lane.startswith("embedding_promotion:"):
            return self._embedding_promotion(conn, requested_lane, reference_id, cutoff_at)
        raise ValueError(f"unsupported research lane {requested_lane!r}")

    @staticmethod
    def _corpus(
        conn: sqlite3.Connection,
        requested_lane: str,
        manifest_id: str,
        cutoff_at: datetime,
    ) -> VerifiedResearchReference:
        row = conn.execute(
            "SELECT manifest.knowledge_cutoff,manifest.recorded_at,"
            "seal.expected_document_count,seal.membership_digest_sha256,"
            "seal.completion_status,seal.sealed_at "
            "FROM search_corpus_manifests manifest "
            "JOIN search_corpus_manifest_seals seal "
            "ON seal.manifest_id=manifest.manifest_id "
            "WHERE manifest.manifest_id=? "
            "AND manifest.knowledge_cutoff IS NOT NULL "
            "AND datetime(manifest.knowledge_cutoff)<=datetime(?) "
            "AND datetime(manifest.recorded_at)<=datetime(?) "
            "AND datetime(seal.sealed_at)<=datetime(?)",
            (
                manifest_id,
                _db_time(cutoff_at),
                _db_time(cutoff_at),
                _db_time(cutoff_at),
            ),
        ).fetchone()
        if row is None or str(row[4]) != "complete":
            raise ValueError("complete corpus seal is absent at cutoff")
        members = conn.execute(
            "SELECT membership_id,expected_document_key,document_version_id,"
            "membership_status,reason FROM search_corpus_document_memberships "
            "WHERE manifest_id=? ORDER BY expected_document_key,membership_id",
            (manifest_id,),
        ).fetchall()
        membership_payload = json.dumps(
            [list(member) for member in members],
            separators=(",", ":"),
            ensure_ascii=True,
        )
        if int(row[2]) != len(members) or str(row[3]) != _digest_text(membership_payload):
            raise ValueError("corpus seal commitment mismatch")
        return VerifiedResearchReference(
            requested_lane=requested_lane,
            reference_table="search_corpus_manifest_seals",
            reference_id=manifest_id,
            commitment_sha256=str(row[3]),
            knowledge_at=_parse_time(row[0]),
            recorded_at=_parse_time(row[5]),
            attributes={"manifest_id": manifest_id},
        )

    @staticmethod
    def _search_projection(
        conn: sqlite3.Connection,
        requested_lane: str,
        index_run_id: str,
        cutoff_at: datetime,
    ) -> VerifiedResearchReference:
        from provenance.search_index_lineage import (
            load_projection_seal,
            verify_ledger_projection_seal,
        )

        seal = load_projection_seal(conn, index_run_id=index_run_id)
        expected_kind = "lexical" if requested_lane.startswith("lexical_projection:") else "vector"
        if (
            seal is None
            or seal.index_kind != expected_kind
            or _utc(seal.sealed_at) > _utc(cutoff_at)
        ):
            raise ValueError(f"{expected_kind} projection seal is absent at cutoff")
        verify_ledger_projection_seal(conn, seal)
        manifest = conn.execute(
            "SELECT knowledge_cutoff,recorded_at FROM search_corpus_manifests "
            "WHERE manifest_id=? AND knowledge_cutoff IS NOT NULL "
            "AND datetime(knowledge_cutoff)<=datetime(?) "
            "AND datetime(recorded_at)<=datetime(?)",
            (
                seal.manifest_id,
                _db_time(cutoff_at),
                _db_time(cutoff_at),
            ),
        ).fetchone()
        if manifest is None:
            raise ValueError("projection corpus clocks are absent at cutoff")
        return VerifiedResearchReference(
            requested_lane=requested_lane,
            reference_table="search_projection_seals",
            reference_id=seal.projection_seal_id,
            commitment_sha256=_digest(seal),
            knowledge_at=_parse_time(manifest[0]),
            recorded_at=max(_parse_time(manifest[1]), seal.sealed_at),
            attributes={
                "dimensions": seal.dimensions,
                "manifest_id": seal.manifest_id,
                "model": seal.model,
                "provider": seal.provider,
            },
        )

    @staticmethod
    def _embedding_promotion(
        conn: sqlite3.Connection,
        requested_lane: str,
        promotion_id: str,
        cutoff_at: datetime,
    ) -> VerifiedResearchReference:
        _require_columns(
            conn,
            "search_embedding_model_promotions",
            {
                "promotion_id",
                "provider",
                "model",
                "dimensions",
                "approved_at",
                "knowledge_at",
                "recorded_at",
            },
        )
        row = conn.execute(
            "SELECT * FROM search_embedding_model_promotions "
            "WHERE promotion_id=? "
            "AND datetime(knowledge_at)<=datetime(?) "
            "AND datetime(recorded_at)<=datetime(?)",
            (promotion_id, _db_time(cutoff_at), _db_time(cutoff_at)),
        ).fetchone()
        if row is None:
            raise ValueError("embedding promotion is absent at cutoff")
        columns = [
            str(item[1])
            for item in conn.execute("PRAGMA table_info(search_embedding_model_promotions)")
        ]
        payload = {column: row[index] for index, column in enumerate(columns)}
        knowledge_at = _parse_time(payload["knowledge_at"])
        recorded_at = _parse_time(payload["recorded_at"])
        return VerifiedResearchReference(
            requested_lane=requested_lane,
            reference_table="search_embedding_model_promotions",
            reference_id=promotion_id,
            commitment_sha256=_digest(payload),
            knowledge_at=knowledge_at,
            recorded_at=recorded_at,
            attributes={
                "dimensions": int(payload["dimensions"]),
                "model": str(payload["model"]),
                "provider": str(payload["provider"]),
            },
        )


def _assert_disjoint(
    named_sets: tuple[tuple[str, frozenset[str]], ...],
    *,
    label: str,
) -> frozenset[str]:
    union: set[str] = set()
    for name, identifiers in named_sets:
        overlap = union.intersection(identifiers)
        if overlap:
            raise ValueError(
                f"{label} sets must not overlap; {name} repeats " + ", ".join(sorted(overlap))
            )
        union.update(identifiers)
    return frozenset(union)


def _processing_document_sets(
    conn: sqlite3.Connection,
    processing_snapshot_ids: tuple[str, ...],
) -> tuple[tuple[str, frozenset[str]], ...]:
    return tuple(
        (
            snapshot_id,
            frozenset(
                str(row[0])
                for row in conn.execute(
                    "SELECT DISTINCT document_version_id "
                    "FROM document_processing_snapshot_members "
                    "WHERE processing_snapshot_id=? ORDER BY document_version_id",
                    (snapshot_id,),
                )
            ),
        )
        for snapshot_id in processing_snapshot_ids
    )


def _corpus_document_sets(
    conn: sqlite3.Connection,
    bundles: tuple[CorpusProjectionBundle, ...],
) -> tuple[tuple[str, frozenset[str]], ...]:
    return tuple(
        (
            bundle.corpus_manifest_id,
            frozenset(
                str(row[0])
                for row in conn.execute(
                    "SELECT document_version_id "
                    "FROM search_corpus_document_memberships "
                    "WHERE manifest_id=? AND membership_status='included' "
                    "ORDER BY document_version_id",
                    (bundle.corpus_manifest_id,),
                )
                if row[0] is not None
            ),
        )
        for bundle in bundles
    )


def _corpus_obligation_bindings(
    conn: sqlite3.Connection,
    bundles: tuple[CorpusProjectionBundle, ...],
) -> tuple[sqlite3.Row, ...]:
    all_rows: list[sqlite3.Row] = []
    expected_documents: set[str] = set()
    for bundle in bundles:
        manifest_id = bundle.corpus_manifest_id
        memberships = conn.execute(
            "SELECT membership_id FROM search_corpus_document_memberships "
            "WHERE manifest_id=? ORDER BY membership_id",
            (manifest_id,),
        ).fetchall()
        for membership in memberships:
            rows = conn.execute(
                "SELECT expected.expected_document_id,"
                "binding.source_obligation_revision_id,binding.issuer_id,"
                "binding.reporting_entity_id,binding.document_family,"
                "membership.document_version_id,membership.membership_status "
                "FROM search_corpus_document_memberships AS membership "
                "JOIN search_manifest_source_inventories AS inventory "
                "ON inventory.manifest_id=membership.manifest_id "
                "JOIN expected_documents AS expected "
                "ON expected.snapshot_id=inventory.snapshot_id "
                "AND expected.expected_document_key=membership.expected_document_key "
                "JOIN expected_document_obligation_bindings AS binding "
                "ON binding.expected_document_id=expected.expected_document_id "
                "WHERE membership.membership_id=? "
                "ORDER BY expected.expected_document_id",
                (str(membership[0]),),
            ).fetchall()
            if len(rows) != 1:
                raise ValueError(
                    "every corpus membership must resolve to exactly one "
                    "expected-document source-obligation binding"
                )
            expected_document_id = str(rows[0][0])
            if expected_document_id in expected_documents:
                raise ValueError(
                    "corpus manifests must not overlap expected documents: " + expected_document_id
                )
            expected_documents.add(expected_document_id)
            all_rows.append(rows[0])
    return tuple(all_rows)


def _validate_document_obligation_subject_pairs(
    document_subjects: dict[str, tuple[str, str]],
    binding_rows: tuple[sqlite3.Row | tuple[object, ...], ...],
) -> None:
    for row in binding_rows:
        if str(row[6]) != "included":
            continue
        document_version_id = None if row[5] is None else str(row[5])
        subject = (
            None if document_version_id is None else document_subjects.get(document_version_id)
        )
        if subject != (str(row[2]), str(row[3])):
            raise ValueError(
                "each included document must match its exact source-obligation "
                "issuer and reporting entity"
            )


def _verify_research_universe(
    conn: sqlite3.Connection,
    request: ResearchSnapshotRequest,
    *,
    verify_fact_subjects: bool,
) -> None:
    required = {
        "document_processing_snapshot_members",
        "expected_document_obligation_bindings",
        "reporting_entities",
        "research_snapshot_universe_commitments",
        "search_corpus_document_memberships",
        "search_manifest_source_inventories",
        "v_evidence_document_versions_canonical",
    }
    missing = sorted(required - _tables(conn))
    if missing:
        raise RuntimeError("research universe closure schema is unavailable: " + ", ".join(missing))
    universe = request.research_universe
    issuer = conn.execute(
        "SELECT issuer_id FROM issuer_entities WHERE issuer_id=?",
        (universe.issuer_id,),
    ).fetchone()
    if issuer is None:
        raise ValueError("research universe issuer does not exist")
    entity_rows = conn.execute(
        "SELECT reporting_entity_id,issuer_id FROM reporting_entities "
        "WHERE reporting_entity_id IN (SELECT value FROM json_each(?)) "
        "ORDER BY reporting_entity_id",
        (canonical_json(list(universe.reporting_entity_ids)),),
    ).fetchall()
    if tuple(str(row[0]) for row in entity_rows) != universe.reporting_entity_ids or any(
        str(row[1]) != universe.issuer_id for row in entity_rows
    ):
        raise ValueError("research universe reporting entities must belong to its issuer")

    processing = _assert_disjoint(
        _processing_document_sets(conn, request.processing_snapshot_ids),
        label="processing snapshot document",
    )
    corpus = _assert_disjoint(
        _corpus_document_sets(conn, request.corpus_bundles),
        label="corpus manifest document",
    )
    requested_documents = frozenset(universe.document_version_ids)
    if processing != corpus or processing != requested_documents:
        raise ValueError(
            "processing snapshots, corpus manifests, and research universe "
            "must contain the exact same document set"
        )

    document_rows = conn.execute(
        "SELECT document_version_id,issuer_id,reporting_entity_id "
        "FROM v_evidence_document_versions_canonical "
        "WHERE document_version_id IN (SELECT value FROM json_each(?)) "
        "ORDER BY document_version_id",
        (canonical_json(list(universe.document_version_ids)),),
    ).fetchall()
    if tuple(str(row[0]) for row in document_rows) != universe.document_version_ids:
        raise ValueError("research universe document versions are absent or duplicated")
    document_entities: set[str] = set()
    document_subjects: dict[str, tuple[str, str]] = {}
    for row in document_rows:
        if str(row[1]) != universe.issuer_id or row[2] is None:
            raise ValueError(
                "every research document must have the exact issuer and reporting entity"
            )
        document_entities.add(str(row[2]))
        document_subjects[str(row[0])] = (str(row[1]), str(row[2]))
    if document_entities != set(universe.reporting_entity_ids):
        raise ValueError("document reporting-entity set must equal the research universe")

    binding_rows = _corpus_obligation_bindings(conn, request.corpus_bundles)
    obligation_ids = tuple(sorted({str(row[1]) for row in binding_rows}))
    if obligation_ids != universe.source_obligation_revision_ids:
        raise ValueError("corpus source obligations must exactly match the research universe")
    obligation_entities: set[str] = set()
    for row in binding_rows:
        if str(row[2]) != universe.issuer_id or row[3] is None:
            raise ValueError(
                "every expected-document obligation must have the exact issuer and reporting entity"
            )
        obligation_entities.add(str(row[3]))
    _validate_document_obligation_subject_pairs(document_subjects, binding_rows)
    if obligation_entities != set(universe.reporting_entity_ids):
        raise ValueError("source-obligation reporting-entity set must equal the research universe")

    if not verify_fact_subjects or not request.source_fact_publication_ids:
        return
    fact_rows = conn.execute(
        "SELECT DISTINCT cell.reporting_entity_id,entity.issuer_id "
        "FROM canonical_fact_resolution_snapshot_members AS member "
        "JOIN canonical_metric_cells AS cell "
        "ON cell.canonical_metric_cell_id=member.canonical_metric_cell_id "
        "JOIN reporting_entities AS entity "
        "ON entity.reporting_entity_id=cell.reporting_entity_id "
        "WHERE member.resolution_snapshot_id=? "
        "ORDER BY cell.reporting_entity_id",
        (request.canonical_fact_resolution_snapshot_id,),
    ).fetchall()
    fact_entities = {str(row[0]) for row in fact_rows}
    if (
        not fact_rows
        or fact_entities != set(universe.reporting_entity_ids)
        or any(str(row[1]) != universe.issuer_id for row in fact_rows)
    ):
        raise ValueError("canonical fact reporting-entity set must equal the research universe")


def _universe_payload(universe: ResearchUniverse) -> dict[str, object]:
    return {
        "document_version_ids": list(universe.document_version_ids),
        "issuer_id": universe.issuer_id,
        "reporting_entity_ids": list(universe.reporting_entity_ids),
        "source_obligation_revision_ids": list(universe.source_obligation_revision_ids),
    }


def _persist_research_universe(
    conn: sqlite3.Connection,
    request: ResearchSnapshotRequest,
) -> None:
    payload = _universe_payload(request.research_universe)
    canonical = canonical_json(payload)
    row = (
        request.research_snapshot_id,
        request.research_universe.issuer_id,
        canonical_json(list(request.research_universe.reporting_entity_ids)),
        canonical_json(list(request.research_universe.document_version_ids)),
        canonical_json(list(request.research_universe.source_obligation_revision_ids)),
        canonical,
        _digest_text(canonical),
        _db_time(request.cutoff_at),
        _db_time(request.recorded_at),
    )
    existing = conn.execute(
        "SELECT research_snapshot_id,issuer_id,reporting_entity_ids_json,"
        "document_version_ids_json,source_obligation_revision_ids_json,"
        "canonical_universe_json,universe_sha256,cutoff_at,recorded_at "
        "FROM research_snapshot_universe_commitments "
        "WHERE research_snapshot_id=?",
        (request.research_snapshot_id,),
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO research_snapshot_universe_commitments VALUES (?,?,?,?,?,?,?,?,?)",
            row,
        )
    elif tuple(existing) != row:
        raise ValueError("Research Snapshot universe commitment conflict")


def _verify_stored_research_universe(
    conn: sqlite3.Connection,
    request: ResearchSnapshotRequest,
) -> None:
    row = conn.execute(
        "SELECT issuer_id,reporting_entity_ids_json,document_version_ids_json,"
        "source_obligation_revision_ids_json,canonical_universe_json,"
        "universe_sha256,cutoff_at,recorded_at "
        "FROM research_snapshot_universe_commitments "
        "WHERE research_snapshot_id=?",
        (request.research_snapshot_id,),
    ).fetchone()
    payload = _universe_payload(request.research_universe)
    canonical = canonical_json(payload)
    expected = (
        request.research_universe.issuer_id,
        canonical_json(list(request.research_universe.reporting_entity_ids)),
        canonical_json(list(request.research_universe.document_version_ids)),
        canonical_json(list(request.research_universe.source_obligation_revision_ids)),
        canonical,
        _digest_text(canonical),
        _db_time(request.cutoff_at),
        _db_time(request.recorded_at),
    )
    if row is None or tuple(row) != expected:
        raise ValueError("Research Snapshot universe commitment mismatch")


def _verify_research_references(
    conn: sqlite3.Connection,
    request: ResearchSnapshotRequest,
    verifier: _ResearchReferenceVerifier,
) -> tuple[VerifiedResearchReference, ...]:
    _verify_research_universe(
        conn,
        request,
        verify_fact_subjects=isinstance(verifier, _DefaultResearchReferenceVerifier),
    )
    verified_resolution = None
    if isinstance(verifier, _DefaultResearchReferenceVerifier):
        from provenance.canonical_fact_resolution import CanonicalFactResolutionEngine

        verified_resolution = CanonicalFactResolutionEngine(conn).verify_snapshot(
            request.canonical_fact_resolution_snapshot_id, request.cutoff_at
        )
        if (
            verified_resolution.scope.issuer_id != request.research_universe.issuer_id
            or verified_resolution.scope.reporting_entity_ids
            != request.research_universe.reporting_entity_ids
        ):
            raise ValueError(
                "canonical resolution snapshot scope must exactly match the research universe"
            )
    required_publications = _required_source_fact_publications(
        conn, request.canonical_fact_resolution_snapshot_id
    )
    if request.source_fact_publication_ids != required_publications:
        raise ValueError(
            "Source Fact Publications must exactly match the requested 0244 candidate universes"
        )
    references = tuple(
        (_DefaultResearchReferenceVerifier() if lane == "research_universe" else verifier).verify(
            conn,
            requested_lane=lane,
            reference_id=reference_id,
            cutoff_at=request.cutoff_at,
            request=request,
        )
        for lane, reference_id in _research_lanes(request)
    )
    expected_lanes = [lane for lane, _ in _research_lanes(request)]
    actual_lanes = [item.requested_lane for item in references]
    if actual_lanes != expected_lanes:
        raise ValueError("research verifier changed or reordered requested lanes")
    for reference in references:
        if reference.requested_lane == "research_universe":
            if _utc(reference.knowledge_at) != _utc(request.cutoff_at) or _utc(
                reference.recorded_at
            ) != _utc(request.recorded_at):
                raise ValueError(
                    "research universe reference must use the exact cutoff and "
                    "sealed commitment clock"
                )
            continue
        if _utc(reference.knowledge_at) > _utc(request.cutoff_at) or _utc(
            reference.recorded_at
        ) > _utc(request.cutoff_at):
            raise ValueError("research reference postdates the requested cutoff")
    if len({item.requested_lane for item in references}) != len(references):
        raise ValueError("every requested research lane must have exactly one member")
    by_lane = {item.requested_lane: item for item in references}
    projection_lane = "canonical_fact_projection:" + request.canonical_fact_resolution_snapshot_id
    projection_attributes = by_lane[projection_lane].attributes
    if (
        projection_attributes.get("resolution_snapshot_id")
        != request.canonical_fact_resolution_snapshot_id
        or projection_attributes.get("ontology_snapshot_id") != request.ontology_snapshot_id
        or (
            verified_resolution is not None
            and (
                projection_attributes.get("resolution_scope_sha256")
                != verified_resolution.scope_sha256
                or projection_attributes.get("resolution_snapshot_commitment_sha256")
                != verified_resolution.snapshot_commitment_sha256
            )
        )
    ):
        raise ValueError(
            "canonical fact projection is not bound to the requested ontology "
            "and resolution snapshots"
        )
    for bundle in request.corpus_bundles:
        coordinate = bundle.corpus_manifest_id
        corpus = by_lane[f"corpus:{coordinate}"]
        lexical = by_lane[f"lexical_projection:{coordinate}"]
        if (
            corpus.attributes.get("manifest_id") != coordinate
            or lexical.attributes.get("manifest_id") != coordinate
        ):
            raise ValueError("lexical projection does not match its exact corpus manifest")
        if bundle.vector_index_run_id is None:
            continue
        vector = by_lane[f"vector_projection:{coordinate}"]
        promotion = by_lane[f"embedding_promotion:{coordinate}"]
        if vector.attributes.get("manifest_id") != coordinate:
            raise ValueError("vector projection does not match its exact corpus manifest")
        if any(
            vector.attributes.get(field) != promotion.attributes.get(field)
            for field in ("provider", "model", "dimensions")
        ):
            raise ValueError("semantic vector seal does not match the exact embedding promotion")
    return references


def _build_research_snapshot_with_verifier(
    conn: sqlite3.Connection,
    request: ResearchSnapshotRequest,
    *,
    verifier: _ResearchReferenceVerifier,
) -> ResearchSnapshotAdmission:
    references = _verify_research_references(conn, request, verifier)
    request_json = canonical_json(request)
    members: list[dict[str, object]] = []
    rows: list[tuple[object, ...]] = []
    for ordinal, reference in enumerate(references):
        member: dict[str, object] = {
            "reference_commitment_sha256": reference.commitment_sha256,
            "reference_id": reference.reference_id,
            "reference_knowledge_at": _utc(reference.knowledge_at).isoformat(),
            "reference_recorded_at": _utc(reference.recorded_at).isoformat(),
            "reference_table": reference.reference_table,
            "requested_lane": reference.requested_lane,
        }
        member_json = canonical_json(member)
        members.append(member)
        rows.append(
            (
                request.research_snapshot_id,
                ordinal,
                reference.requested_lane,
                reference.reference_table,
                reference.reference_id,
                reference.commitment_sha256,
                _db_time(reference.knowledge_at),
                _db_time(reference.recorded_at),
                member_json,
                _digest_text(member_json),
            )
        )
    payload = canonical_json(members)
    digest = _digest_text(payload)
    with _savepoint(conn, "build_research_snapshot"):
        _insert_exact(
            conn,
            "research_snapshot_headers",
            (
                "research_snapshot_id",
                "idempotency_key",
                "request_json",
                "request_sha256",
                "cutoff_at",
                "recorded_at",
            ),
            (
                request.research_snapshot_id,
                request.idempotency_key,
                request_json,
                _digest_text(request_json),
                request.cutoff_at,
                request.recorded_at,
            ),
            idempotency_key=request.idempotency_key,
        )
        _persist_research_universe(conn, request)
        existing_count = conn.execute(
            "SELECT COUNT(*) FROM research_snapshot_members WHERE research_snapshot_id=?",
            (request.research_snapshot_id,),
        ).fetchone()
        if existing_count is None or int(existing_count[0]) == 0:
            conn.executemany(
                "INSERT INTO research_snapshot_members VALUES (?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
        seal = conn.execute(
            "SELECT member_count,canonical_member_set_json,member_set_sha256,"
            "sealed_at FROM research_snapshot_seals WHERE research_snapshot_id=?",
            (request.research_snapshot_id,),
        ).fetchone()
        expected = (
            len(rows),
            payload,
            digest,
            _db_time(request.recorded_at),
        )
        if seal is None:
            conn.execute(
                "INSERT INTO research_snapshot_seals VALUES (?,?,?,?,?)",
                (request.research_snapshot_id, *expected),
            )
        elif tuple(seal) != expected:
            raise ValueError("Research Snapshot seal conflict")
    return _verify_research_snapshot_with_verifier(
        conn, request.research_snapshot_id, verifier=verifier
    )


def _verify_research_snapshot_with_verifier(
    conn: sqlite3.Connection,
    research_snapshot_id: str,
    *,
    verifier: _ResearchReferenceVerifier,
) -> ResearchSnapshotAdmission:
    conn.row_factory = sqlite3.Row
    header = conn.execute(
        "SELECT * FROM research_snapshot_headers WHERE research_snapshot_id=?",
        (research_snapshot_id,),
    ).fetchone()
    seal = conn.execute(
        "SELECT * FROM research_snapshot_seals WHERE research_snapshot_id=?",
        (research_snapshot_id,),
    ).fetchone()
    if header is None or seal is None:
        raise ValueError("Research Snapshot is not fully sealed")
    request_payload = json.loads(str(header["request_json"]))
    request_json = canonical_json(request_payload)
    if str(header["request_json"]) != request_json or str(header["request_sha256"]) != _digest_text(
        request_json
    ):
        raise ValueError("Research Snapshot header commitment mismatch")
    request = ResearchSnapshotRequest.model_validate(request_payload)
    _verify_stored_research_universe(conn, request)
    if (
        _parse_time(header["cutoff_at"]) != _utc(request.cutoff_at)
        or _parse_time(header["recorded_at"]) != _utc(request.recorded_at)
        or _parse_time(seal["sealed_at"]) < _parse_time(header["recorded_at"])
    ):
        raise ValueError("Research Snapshot seal clocks are invalid")
    references = _verify_research_references(conn, request, verifier)
    rows, payload, digest = _member_set(
        conn, "research_snapshot_members", "research_snapshot_id", research_snapshot_id
    )
    expected_lanes = [item.requested_lane for item in references]
    actual_lanes = [str(row["requested_lane"]) for row in rows]
    if expected_lanes != actual_lanes:
        raise ValueError("Research Snapshot has omitted, extra, or reordered lanes")
    for row, reference in zip(rows, references, strict=True):
        expected = (
            reference.reference_table,
            reference.reference_id,
            reference.commitment_sha256,
            _utc(reference.knowledge_at),
            _utc(reference.recorded_at),
        )
        actual = (
            str(row["reference_table"]),
            str(row["reference_id"]),
            str(row["reference_commitment_sha256"]),
            _parse_time(row["reference_knowledge_at"]),
            _parse_time(row["reference_recorded_at"]),
        )
        if actual != expected:
            raise ValueError("Research Snapshot reference commitment mismatch")
        member = {
            "reference_commitment_sha256": reference.commitment_sha256,
            "reference_id": reference.reference_id,
            "reference_knowledge_at": _utc(reference.knowledge_at).isoformat(),
            "reference_recorded_at": _utc(reference.recorded_at).isoformat(),
            "reference_table": reference.reference_table,
            "requested_lane": reference.requested_lane,
        }
        member_json = canonical_json(member)
        if str(row["canonical_member_json"]) != member_json or str(
            row["member_sha256"]
        ) != _digest_text(member_json):
            raise ValueError("Research Snapshot member commitment mismatch")
    if (
        int(seal["member_count"]) != len(rows)
        or str(seal["canonical_member_set_json"]) != payload
        or str(seal["member_set_sha256"]) != digest
    ):
        raise ValueError("Research Snapshot commitment mismatch")
    return ResearchSnapshotAdmission(
        research_snapshot_id=research_snapshot_id,
        cutoff_at=request.cutoff_at,
        member_count=len(rows),
        member_set_sha256=digest,
        requested_lanes=tuple(expected_lanes),
    )


def build_research_snapshot(
    conn: sqlite3.Connection,
    request: ResearchSnapshotRequest,
) -> ResearchSnapshotAdmission:
    return _build_research_snapshot_with_verifier(
        conn, request, verifier=_DefaultResearchReferenceVerifier()
    )


def verify_research_snapshot(
    conn: sqlite3.Connection,
    research_snapshot_id: str,
) -> ResearchSnapshotAdmission:
    return _verify_research_snapshot_with_verifier(
        conn,
        research_snapshot_id,
        verifier=_DefaultResearchReferenceVerifier(),
    )


def admit(
    conn: sqlite3.Connection,
    research_snapshot_id: str,
) -> ResearchSnapshotAdmission:
    return verify_research_snapshot(conn, research_snapshot_id)
