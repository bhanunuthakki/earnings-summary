"""Build one sealed, immutable lexical corpus from canonical evidence nodes.

The expected-document inventory is deliberately caller supplied.  The builder
does not mistake the documents already captured locally for the reporting
universe; absent and quarantined obligations become first-class corpus
memberships and make the resulting seal incomplete.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Sequence
from datetime import datetime
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from provenance.fulltext_extractor_identity import (
    BASE_FULLTEXT_EXTRACTOR,
    FULLTEXT_EXTRACTOR_IDENTITY_POLICY_VERSION,
    FULLTEXT_EXTRACTOR_NAME,
    OFFICE_FULLTEXT_EXTRACTOR,
    STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR,
    FulltextExtractorIdentity,
    resolve_fulltext_extractor_identity,
)
from provenance.image_ocr_extraction import (
    IMAGE_OCR_EXTRACTOR_CODE_VERSION,
    IMAGE_OCR_EXTRACTOR_NAME,
)
from provenance.source_inventory_seal import (
    InventoryManifestLink,
    SourceInventorySealStore,
)
from search.grounded import (
    CorpusDocumentMembership,
    CorpusManifest,
    CorpusManifestSeal,
    GroundedSearchStore,
    IndexRun,
    SearchChunk,
    membership_digest,
)

_BuilderMode = Literal["dry_run", "apply"]
_MembershipStatus = Literal["included", "missing", "quarantined"]
_BUILDER_VERSION = "grounded-corpus-builder@4-authoritative-extractor-selection"
NODE_SELECTION_POLICY_VERSION = "canonical-search-node-selection@2"


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExpectedDocument(_ClosedModel):
    """One reporting obligation in a closed external expected-document inventory."""

    expected_document_key: str = Field(min_length=1, max_length=256)
    document_version_id: str | None = Field(default=None, min_length=1, max_length=128)
    membership_status: _MembershipStatus
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_anchor_contract(self) -> Self:
        if self.membership_status == "included" and self.document_version_id is None:
            raise ValueError("included expected documents require document_version_id")
        if self.membership_status != "included" and self.document_version_id is not None:
            raise ValueError("missing or quarantined expected documents cannot claim an anchor")
        return self


class ExpectedDocumentInventory(_ClosedModel):
    """Strict JSON-file envelope for the complete expected reporting universe."""

    expected_documents: tuple[ExpectedDocument, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_unique_document_keys(self) -> Self:
        keys = [document.expected_document_key for document in self.expected_documents]
        if len(keys) != len(set(keys)):
            raise ValueError("expected_document_key values must be unique")
        return self


class ChunkerConfig(_ClosedModel):
    """Bounded, whitespace-aware slices that never cross an evidence node."""

    max_characters: int = Field(default=1_200, ge=1, le=100_000)
    max_tokens: int = Field(default=220, ge=1, le=20_000)


class CorpusBuildRequest(_ClosedModel):
    corpus_key: str = Field(min_length=1, max_length=256)
    revision: int = Field(gt=0)
    selector_code_version: str = Field(min_length=1, max_length=255)
    recorded_at: datetime
    expected_documents: tuple[ExpectedDocument, ...] = Field(min_length=1)
    source_inventory_snapshot_ids: tuple[str, ...] = ()
    chunker: ChunkerConfig = Field(default_factory=ChunkerConfig)
    required_extractor_names: tuple[str, ...] = Field(
        default=(
            "fulltext-evidence-backfill",
            "governed-pdf-ocr",
            "governed-image-ocr",
        ),
        min_length=1,
    )
    knowledge_cutoff: datetime | None = None
    persist_batch_size: int = Field(default=250, ge=1, le=5_000)
    apply: bool = False

    @model_validator(mode="after")
    def _validate_inventory(self) -> Self:
        ExpectedDocumentInventory(expected_documents=self.expected_documents)
        if any(not name.strip() for name in self.required_extractor_names):
            raise ValueError("required extractor names must be non-empty")
        if len(self.required_extractor_names) != len(set(self.required_extractor_names)):
            raise ValueError("required extractor names must be unique")
        if len(self.source_inventory_snapshot_ids) != len(set(self.source_inventory_snapshot_ids)):
            raise ValueError("source inventory snapshot IDs must be unique")
        return self


class CorpusBuildResult(_ClosedModel):
    mode: _BuilderMode
    manifest_id: str
    lexical_index_run_id: str
    completion_status: Literal["complete", "incomplete"]
    expected_document_count: int = Field(ge=0)
    included_document_count: int = Field(ge=0)
    chunks_planned: int = Field(ge=0)
    records_created: int = Field(ge=0)
    records_replayed: int = Field(ge=0)
    manifest_config_sha256: str
    chunker_config_sha256: str
    planned_chunks: tuple[SearchChunk, ...] = Field(default=(), exclude=True)


def load_expected_document_inventory(path_text: str) -> ExpectedDocumentInventory:
    """Parse one closed JSON inventory without inferring any reporting obligations."""

    with open(path_text, encoding="utf-8") as inventory_file:
        return ExpectedDocumentInventory.model_validate_json(inventory_file.read())


def load_coverage_expected_document_inventory(
    conn: sqlite3.Connection, inventory_keys: tuple[str, ...]
) -> tuple[ExpectedDocumentInventory, tuple[str, ...]]:
    """Derive corpus membership from complete, sealed current source inventories."""

    if not inventory_keys:
        raise ValueError("at least one source coverage inventory key is required")
    if len(inventory_keys) != len(set(inventory_keys)):
        raise ValueError("source coverage inventory keys must be unique")
    expected: list[ExpectedDocument] = []
    snapshot_ids: list[str] = []
    for inventory_key in sorted(inventory_keys):
        row = conn.execute(
            "SELECT snapshot_id FROM v_source_inventory_sealed_complete WHERE inventory_key = ?",
            (inventory_key,),
        ).fetchone()
        if row is None:
            raise ValueError(
                "source inventory is absent, unsealed, incomplete, or not current: " + inventory_key
            )
        snapshot_id = str(row[0])
        snapshot_ids.append(snapshot_id)
        rows = conn.execute(
            "SELECT expected.expected_document_key, assessment.document_version_id, "
            "assessment.coverage_status, assessment.reason_code "
            "FROM expected_documents AS expected "
            "LEFT JOIN v_source_coverage_current AS assessment "
            "ON assessment.expected_document_id = expected.expected_document_id "
            "WHERE expected.snapshot_id = ? "
            "ORDER BY expected.expected_document_key",
            (snapshot_id,),
        ).fetchall()
        for item in rows:
            if item[2] is None:
                raise ValueError(
                    "expected document has no current coverage assessment: " + str(item[0])
                )
            status = str(item[2])
            document_version_id = None if item[1] is None else str(item[1])
            if status in {"captured", "extracted", "indexed"}:
                if document_version_id is None:
                    raise ValueError(
                        "positive coverage status has no document version: " + str(item[0])
                    )
                membership_status: _MembershipStatus = "included"
            elif status == "quarantined":
                membership_status = "quarantined"
                document_version_id = None
            else:
                membership_status = "missing"
                document_version_id = None
            expected.append(
                ExpectedDocument(
                    expected_document_key=str(item[0]),
                    document_version_id=document_version_id,
                    membership_status=membership_status,
                    reason=f"coverage:{status}:{item[3]}",
                )
            )
    if not expected:
        raise ValueError("sealed source inventories contain no expected documents")
    return (ExpectedDocumentInventory(expected_documents=tuple(expected)), tuple(snapshot_ids))


def build_grounded_search_corpus(
    conn: sqlite3.Connection,
    request: CorpusBuildRequest,
    on_chunk_batch_complete: Callable[[int], None] | None = None,
) -> CorpusBuildResult:
    """Plan or incrementally stage and atomically publish a lexical corpus.

    Applying a large corpus deliberately does not hold one ``BEGIN IMMEDIATE``
    across extraction, chunking, and every insert.  The immutable manifest,
    memberships, and chunks are staged in bounded transactions.  A manifest is
    queryable only after a second deterministic pass proves that every expected
    chunk is present exactly and no extra chunk exists; the short publication
    transaction then inserts the seal and lexical run together.
    """

    store = GroundedSearchStore(conn)
    inventory_store = SourceInventorySealStore(conn)
    if not request.apply:
        store.require_fts5()
        plan = _plan(conn, request)
        return _result(plan, mode="dry_run", records_created=0, records_replayed=0)

    store.require_fts5()
    metadata = _metadata_plan(conn, request)
    records_created, records_replayed = _persist_record_batch(conn, store, (metadata.manifest,))
    for offset in range(0, len(metadata.memberships), request.persist_batch_size):
        created, replayed = _persist_record_batch(
            conn,
            store,
            metadata.memberships[offset : offset + request.persist_batch_size],
        )
        records_created += created
        records_replayed += replayed
    for offset in range(0, len(request.source_inventory_snapshot_ids), request.persist_batch_size):
        created, replayed = _persist_inventory_links(
            conn,
            inventory_store,
            manifest_id=metadata.manifest.manifest_id,
            snapshot_ids=request.source_inventory_snapshot_ids[
                offset : offset + request.persist_batch_size
            ],
            linked_at=request.recorded_at,
        )
        records_created += created
        records_replayed += replayed

    chunk_batch: list[SearchChunk] = []
    staged_chunks = 0
    for chunk in _iter_manifest_chunks(conn, metadata.memberships, request, metadata.chunker_sha):
        chunk_batch.append(chunk)
        if len(chunk_batch) == request.persist_batch_size:
            created, replayed = _persist_chunk_batch(conn, store, chunk_batch)
            records_created += created
            records_replayed += replayed
            staged_chunks += len(chunk_batch)
            if on_chunk_batch_complete is not None:
                on_chunk_batch_complete(staged_chunks)
            chunk_batch = []
    if chunk_batch:
        created, replayed = _persist_chunk_batch(conn, store, chunk_batch)
        records_created += created
        records_replayed += replayed
        staged_chunks += len(chunk_batch)
        if on_chunk_batch_complete is not None:
            on_chunk_batch_complete(staged_chunks)

    chunk_count = _verify_complete_chunk_stage(
        conn,
        metadata.manifest.manifest_id,
        metadata.memberships,
        request,
        metadata.chunker_sha,
    )
    chunk_config_sha = lexical_index_config_sha256(conn, manifest_id=metadata.manifest.manifest_id)
    lexical_index = _lexical_index(
        manifest_id=metadata.manifest.manifest_id,
        recorded_at=request.recorded_at,
        config_sha256=chunk_config_sha,
    )
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Rows are append-only.  Rechecking the count under the write lock is
        # sufficient to prove the previously verified digest cannot have
        # changed between verification and publication.
        stored_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM search_chunks WHERE manifest_id = ?",
                (metadata.manifest.manifest_id,),
            ).fetchone()[0]
        )
        if stored_count != chunk_count:
            raise RuntimeError("corpus chunk set changed before atomic publication")
        locked_config_sha = lexical_index_config_sha256(
            conn,
            manifest_id=metadata.manifest.manifest_id,
        )
        if locked_config_sha != lexical_index.config_sha256:
            raise RuntimeError("lexical projection changed before atomic corpus publication")
        for record in (metadata.seal, lexical_index):
            created = store.persist(record).created
            records_created += int(created)
            records_replayed += int(not created)
        if metadata.seal.completion_status == "complete":
            projection_created = _persist_lexical_projection_seal(
                conn,
                lexical_index=lexical_index,
                sealed_at=request.recorded_at,
            )
            records_created += int(projection_created)
            records_replayed += int(not projection_created)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return CorpusBuildResult(
        mode="apply",
        manifest_id=metadata.manifest.manifest_id,
        lexical_index_run_id=lexical_index.index_run_id,
        completion_status=metadata.seal.completion_status,
        expected_document_count=len(metadata.memberships),
        included_document_count=sum(
            membership.membership_status == "included" for membership in metadata.memberships
        ),
        chunks_planned=chunk_count,
        records_created=records_created,
        records_replayed=records_replayed,
        manifest_config_sha256=metadata.manifest_config_sha256,
        chunker_config_sha256=metadata.chunker_sha,
    )


class _CorpusPlan(_ClosedModel):
    manifest: CorpusManifest
    memberships: tuple[CorpusDocumentMembership, ...]
    chunks: tuple[SearchChunk, ...]
    seal: CorpusManifestSeal
    lexical_index: IndexRun
    manifest_config_sha256: str
    chunker_config_sha256: str


class _CorpusMetadata(_ClosedModel):
    manifest: CorpusManifest
    memberships: tuple[CorpusDocumentMembership, ...]
    seal: CorpusManifestSeal
    manifest_config_sha256: str
    chunker_sha: str


def _plan(conn: sqlite3.Connection, request: CorpusBuildRequest) -> _CorpusPlan:
    metadata = _metadata_plan(conn, request)
    chunks = tuple(
        _iter_manifest_chunks(
            conn,
            metadata.memberships,
            request,
            metadata.chunker_sha,
        )
    )
    chunk_digest = _chunk_digest_from_models(chunks)
    lexical_digest = _digest_rows(
        (chunk.chunk_id, chunk.text) for chunk in sorted(chunks, key=lambda item: item.chunk_id)
    )
    lexical_index = _lexical_index(
        manifest_id=metadata.manifest.manifest_id,
        recorded_at=request.recorded_at,
        config_sha256=_lexical_config_hash(
            metadata.manifest.manifest_id,
            len(chunks),
            chunk_digest,
            len(chunks),
            lexical_digest,
        ),
    )
    return _CorpusPlan(
        manifest=metadata.manifest,
        memberships=metadata.memberships,
        chunks=chunks,
        seal=metadata.seal,
        lexical_index=lexical_index,
        manifest_config_sha256=metadata.manifest_config_sha256,
        chunker_config_sha256=metadata.chunker_sha,
    )


def _metadata_plan(conn: sqlite3.Connection, request: CorpusBuildRequest) -> _CorpusMetadata:
    inventory = sorted(request.expected_documents, key=lambda item: item.expected_document_key)
    config_sha = _sha256_json(
        {
            "corpus_key": request.corpus_key,
            "revision": request.revision,
            "expected_documents": [document.model_dump(mode="json") for document in inventory],
            "required_extractor_names": sorted(request.required_extractor_names),
            "knowledge_cutoff": request.knowledge_cutoff,
            "source_inventory_snapshot_ids": sorted(request.source_inventory_snapshot_ids),
            "node_selection_policy": _node_selection_policy_config(),
        }
    )
    chunker_sha = _sha256_json(
        {
            "chunker": request.chunker.model_dump(mode="json"),
            "node_selection_policy": _node_selection_policy_config(),
        }
    )
    manifest_seed = _sha256_json(
        {
            "corpus_key": request.corpus_key,
            "revision": request.revision,
            "selection_config_sha256": config_sha,
            "selector_code_version": request.selector_code_version,
            "recorded_at": request.recorded_at,
        }
    )
    manifest_id = f"corpus-manifest:{manifest_seed}"
    manifest = CorpusManifest(
        manifest_id=manifest_id,
        idempotency_key=f"corpus-manifest:{manifest_seed}",
        corpus_key=request.corpus_key,
        revision=request.revision,
        selection_config_sha256=config_sha,
        selector_code_version=request.selector_code_version,
        knowledge_cutoff=request.knowledge_cutoff,
        supersedes_manifest_id=_prior_manifest_id(conn, request),
        recorded_at=request.recorded_at,
    )
    memberships = tuple(_membership(conn, manifest_id, expected, request) for expected in inventory)
    completion_status: Literal["complete", "incomplete"] = (
        "complete"
        if all(item.membership_status == "included" for item in memberships)
        else "incomplete"
    )
    seal = CorpusManifestSeal(
        manifest_id=manifest_id,
        expected_document_count=len(memberships),
        membership_digest_sha256=membership_digest(memberships),
        completion_status=completion_status,
        sealed_at=request.recorded_at,
    )
    return _CorpusMetadata(
        manifest=manifest,
        memberships=memberships,
        seal=seal,
        manifest_config_sha256=config_sha,
        chunker_sha=chunker_sha,
    )


def _membership(
    conn: sqlite3.Connection,
    manifest_id: str,
    expected: ExpectedDocument,
    request: CorpusBuildRequest,
) -> CorpusDocumentMembership:
    status = expected.membership_status
    reason = expected.reason
    if (
        status == "included"
        and expected.document_version_id is not None
        and not _document_extraction_complete(
            conn, expected.document_version_id, request.required_extractor_names
        )
    ):
        nonsemantic_assessment_id = _human_nonsemantic_assessment(
            conn,
            expected.document_version_id,
        )
        if nonsemantic_assessment_id is None:
            status = "quarantined"
            reason = "extraction:approved_substantive_coverage_incomplete"
        else:
            reason = f"semantic:not_required:{nonsemantic_assessment_id}"
    return CorpusDocumentMembership(
        membership_id=f"corpus-membership:{_sha256_text(manifest_id + chr(0) + expected.expected_document_key)}",
        manifest_id=manifest_id,
        expected_document_key=expected.expected_document_key,
        document_version_id=expected.document_version_id,
        membership_status=status,
        reason=reason,
        recorded_at=request.recorded_at,
    )


def _document_extraction_complete(
    conn: sqlite3.Connection,
    document_version_id: str,
    extractor_names: tuple[str, ...],
) -> bool:
    row = conn.execute(
        "SELECT observation.source_url, blob.media_type "
        "FROM evidence_document_versions AS document "
        "JOIN evidence_content_blobs AS blob ON blob.sha256 = document.blob_sha256 "
        "JOIN evidence_source_observations AS observation "
        "ON observation.observation_id = document.observation_id "
        "WHERE document.document_version_id = ?",
        (document_version_id,),
    ).fetchone()
    if row is None:
        raise ValueError("document version does not exist: " + document_version_id)
    source_ref = str(row[0])
    media_type = str(row[1])
    fulltext_identity = resolve_fulltext_extractor_identity(source_ref, media_type)
    accepted_image_node_ids = _accepted_current_image_ocr_node_ids(
        conn,
        document_version_id,
        media_type=media_type,
    )
    if media_type.lower() != "application/pdf":
        return _has_approved_substantive_extraction(
            conn,
            document_version_id,
            extractor_names,
            fulltext_identity=fulltext_identity,
            accepted_image_node_ids=accepted_image_node_ids,
        )
    if (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ocr_document_assessments'"
        ).fetchone()
        is None
    ):
        return False
    assessment = conn.execute(
        "SELECT assessment_id, outcome, page_count "
        "FROM ocr_document_assessments WHERE document_version_id = ? "
        "ORDER BY assessed_at DESC, assessment_id DESC LIMIT 1",
        (document_version_id,),
    ).fetchone()
    if assessment is None or str(assessment[1]) not in {
        "native_sufficient",
        "ocr_required",
    }:
        return False
    page_count = int(assessment[2])
    preflight = {
        int(item[0]): bool(item[1])
        for item in conn.execute(
            "SELECT page_number, requires_ocr FROM ocr_preflight_pages WHERE assessment_id = ?",
            (str(assessment[0]),),
        ).fetchall()
    }
    if set(preflight) != set(range(1, page_count + 1)):
        return False
    native_pages: set[int] = set()
    for item in conn.execute(
        "SELECT node.locator_json FROM evidence_nodes AS node "
        "JOIN evidence_extraction_runs AS run "
        "ON run.extraction_run_id = node.extraction_run_id "
        "WHERE run.document_version_id = ? AND run.outcome = 'succeeded' "
        "AND node.node_kind = 'pdf_page' AND run.extractor_name = ? "
        "AND run.extractor_code_version = ? AND run.extractor_config_sha256 = ?",
        (
            document_version_id,
            fulltext_identity.name,
            fulltext_identity.code_version,
            fulltext_identity.config_sha256,
        ),
    ).fetchall():
        if item[0] is None:
            continue
        locator = json.loads(str(item[0]))
        locator_object = cast(dict[str, object], locator) if isinstance(locator, dict) else {}
        page_number = locator_object.get("page_number")
        if isinstance(page_number, int):
            native_pages.add(page_number)
    accepted_ocr_pages = {
        int(item[0])
        for item in conn.execute(
            "SELECT result.page_number FROM ocr_page_results AS result "
            "JOIN ocr_extraction_governance AS governance "
            "ON governance.extraction_run_id = result.extraction_run_id "
            "JOIN evidence_extraction_runs AS run "
            "ON run.extraction_run_id = result.extraction_run_id "
            "WHERE governance.assessment_id = ? AND result.outcome = 'accepted' "
            "AND run.outcome = 'succeeded'",
            (str(assessment[0]),),
        ).fetchall()
    }
    return all(
        page_number in (accepted_ocr_pages if requires_ocr else native_pages)
        for page_number, requires_ocr in preflight.items()
    )


def _has_approved_substantive_extraction(
    conn: sqlite3.Connection,
    document_version_id: str,
    extractor_names: tuple[str, ...],
    *,
    fulltext_identity: FulltextExtractorIdentity,
    accepted_image_node_ids: frozenset[str],
) -> bool:
    placeholders = ", ".join("?" for _ in extractor_names)
    rows = conn.execute(
        "SELECT DISTINCT node.node_id, run.extractor_name, run.extractor_code_version, "  # nosec B608 -- trusted internal SQL shape; values remain bound
        "run.extractor_config_sha256 FROM v_evidence_current AS node "
        "JOIN evidence_extraction_runs AS run "
        "ON run.extraction_run_id = node.extraction_run_id "
        "WHERE run.document_version_id = ? AND run.outcome = 'succeeded' "
        "AND node.node_kind <> 'document' AND length(trim(node.text)) > 0 "
        f"AND run.extractor_name IN ({placeholders})",
        (document_version_id, *extractor_names),
    ).fetchall()
    for row in rows:
        node_id = str(row[0])
        extractor_name = str(row[1])
        if extractor_name == IMAGE_OCR_EXTRACTOR_NAME:
            if node_id in accepted_image_node_ids:
                return True
            continue
        if extractor_name == "governed-pdf-ocr":
            continue
        if extractor_name != FULLTEXT_EXTRACTOR_NAME:
            return True
        if (
            str(row[2]) == fulltext_identity.code_version
            and str(row[3]) == fulltext_identity.config_sha256
        ):
            return True
    return False


def _accepted_current_image_ocr_node_ids(
    conn: sqlite3.Connection,
    document_version_id: str,
    *,
    media_type: str,
) -> frozenset[str]:
    if media_type.lower() not in {"image/jpeg", "image/png"}:
        return frozenset()
    required_tables = (
        "image_ocr_assessments",
        "image_ocr_extraction_governance",
        "image_ocr_results",
    )
    existing = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN (?, ?, ?)",
            required_tables,
        ).fetchall()
    }
    if existing != set(required_tables):
        return frozenset()
    assessment = conn.execute(
        "SELECT assessment_id, outcome FROM image_ocr_assessments "
        "WHERE document_version_id = ? "
        "ORDER BY assessed_at DESC, assessment_id DESC LIMIT 1",
        (document_version_id,),
    ).fetchone()
    if assessment is None or str(assessment[1]) != "ocr_required":
        return frozenset()
    rows = conn.execute(
        "SELECT result.node_id FROM image_ocr_results AS result "
        "JOIN image_ocr_extraction_governance AS governance "
        "ON governance.extraction_run_id = result.extraction_run_id "
        "JOIN image_ocr_assessments AS current_assessment "
        "ON current_assessment.assessment_id = governance.assessment_id "
        "JOIN evidence_extraction_runs AS run "
        "ON run.extraction_run_id = result.extraction_run_id "
        "JOIN v_evidence_current AS node ON node.node_id = result.node_id "
        "WHERE current_assessment.assessment_id = ? "
        "AND result.outcome = 'accepted' AND result.node_id IS NOT NULL "
        "AND run.outcome = 'succeeded' "
        "AND run.document_version_id = current_assessment.document_version_id "
        "AND run.input_sha256 = current_assessment.input_sha256 "
        "AND run.extractor_name = ? AND run.extractor_code_version = ? "
        "AND run.extractor_config_sha256 = governance.extractor_config_sha256",
        (
            str(assessment[0]),
            IMAGE_OCR_EXTRACTOR_NAME,
            IMAGE_OCR_EXTRACTOR_CODE_VERSION,
        ),
    ).fetchall()
    node_ids = frozenset(str(row[0]) for row in rows)
    if len(node_ids) > 1:
        raise ValueError("current image OCR assessment has multiple accepted governed results")
    return node_ids


def _human_nonsemantic_assessment(
    conn: sqlite3.Connection,
    document_version_id: str,
) -> str | None:
    if (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'document_semantic_disposition_revisions'"
        ).fetchone()
        is None
    ):
        return None
    row = conn.execute(
        "SELECT assessment_id FROM v_document_semantic_dispositions_current "
        "WHERE document_version_id = ? AND semantic_status = 'not_required' "
        "AND decision_kind = 'human' AND reviewer_identity IS NOT NULL",
        (document_version_id,),
    ).fetchone()
    return None if row is None else str(row[0])


def _prior_manifest_id(conn: sqlite3.Connection, request: CorpusBuildRequest) -> str | None:
    if request.revision == 1:
        return None
    row = conn.execute(
        "SELECT manifest_id FROM search_corpus_manifests WHERE corpus_key = ? AND revision = ?",
        (request.corpus_key, request.revision - 1),
    ).fetchone()
    if row is None:
        raise ValueError("corpus revision requires its exact prior manifest")
    return str(row[0])


def _iter_manifest_chunks(
    conn: sqlite3.Connection,
    memberships: Sequence[CorpusDocumentMembership],
    request: CorpusBuildRequest,
    chunker_sha: str,
) -> Iterator[SearchChunk]:
    """Yield each document version once, regardless of obligation aliases."""

    included = sorted(
        {
            membership.document_version_id: membership
            for membership in memberships
            if membership.membership_status == "included"
            and membership.document_version_id is not None
        }.values(),
        key=lambda membership: (
            str(membership.document_version_id),
            membership.expected_document_key,
        ),
    )
    for membership in included:
        yield from _iter_chunks_for_document(conn, membership, request, chunker_sha)


def _iter_chunks_for_document(
    conn: sqlite3.Connection,
    membership: CorpusDocumentMembership,
    request: CorpusBuildRequest,
    chunker_sha: str,
) -> Iterator[SearchChunk]:
    if membership.document_version_id is None:
        raise ValueError("included membership unexpectedly has no document version")
    if membership.reason.startswith("semantic:not_required:"):
        return
    rows = _selected_substantive_nodes(
        conn,
        membership.document_version_id,
        request.required_extractor_names,
    )
    found = False
    for node_id, text, completed_at in rows:
        found = True
        for ordinal, (char_start, char_end) in enumerate(_offsets(text, request.chunker), start=1):
            chunk_text = text[char_start:char_end]
            chunk_key = "chunk-key:" + _sha256_json(
                {
                    "manifest_id": membership.manifest_id,
                    "evidence_node_id": node_id,
                    "ordinal": ordinal,
                    "char_start": char_start,
                    "char_end": char_end,
                    "content_sha256": _sha256_text(chunk_text),
                }
            )
            chunk_seed = _sha256_text(chunk_key)
            yield SearchChunk(
                chunk_id=f"search-chunk:{chunk_seed}",
                idempotency_key=f"search-chunk:{chunk_seed}",
                manifest_id=membership.manifest_id,
                evidence_node_id=node_id,
                chunk_key=chunk_key,
                chunk_revision=1,
                text=chunk_text,
                char_start=char_start,
                char_end=char_end,
                chunker_config_sha256=chunker_sha,
                chunker_code_version=_BUILDER_VERSION,
                available_at=completed_at,
                recorded_at=request.recorded_at,
            )
    if not found:
        raise ValueError(
            "included document version has no current substantive evidence nodes "
            "from the required succeeded extractors: " + membership.document_version_id
        )


def _selected_substantive_nodes(
    conn: sqlite3.Connection,
    document_version_id: str,
    extractor_names: tuple[str, ...],
) -> tuple[tuple[str, str, datetime], ...]:
    """Return the one canonical text projection approved for corpus use.

    PDF preflight is a page-level selection decision.  A page that required
    OCR must contribute only its accepted governed OCR node; a page that had
    sufficient native text must contribute only its deterministic native node.
    Treating both namespaces as current evidence would duplicate and sometimes
    contradict the same page in retrieval.
    """

    media_row = conn.execute(
        "SELECT observation.source_url, lower(blob.media_type) "
        "FROM evidence_document_versions AS document "
        "JOIN evidence_content_blobs AS blob ON blob.sha256 = document.blob_sha256 "
        "JOIN evidence_source_observations AS observation "
        "ON observation.observation_id = document.observation_id "
        "WHERE document.document_version_id = ?",
        (document_version_id,),
    ).fetchone()
    if media_row is None:
        raise ValueError("document version does not exist: " + document_version_id)
    fulltext_identity = resolve_fulltext_extractor_identity(str(media_row[0]), str(media_row[1]))
    placeholders = ", ".join("?" for _ in extractor_names)
    raw_rows = conn.execute(
        "SELECT node.node_id, node.text, run.completed_at "  # nosec B608 -- trusted internal SQL shape; values remain bound
        ", node.node_kind, node.locator_json, run.extractor_name "
        ", node.parent_node_id, run.extractor_code_version "
        ", run.extractor_config_sha256, run.extraction_run_id "
        "FROM v_evidence_current AS node "
        "JOIN evidence_extraction_runs AS run ON run.extraction_run_id = node.extraction_run_id "
        "WHERE run.document_version_id = ? AND run.outcome = 'succeeded' "
        "AND node.node_kind <> 'document' "
        f"AND run.extractor_name IN ({placeholders}) "
        "ORDER BY node.evidence_key, node.revision, node.node_id",
        (document_version_id, *extractor_names),
    ).fetchall()
    if str(media_row[1]) != "application/pdf":
        accepted_image_node_ids = _accepted_current_image_ocr_node_ids(
            conn,
            document_version_id,
            media_type=str(media_row[1]),
        )
        return _canonical_non_pdf_nodes(
            _authoritative_non_pdf_rows(
                raw_rows,
                fulltext_identity,
                accepted_image_node_ids=accepted_image_node_ids,
            )
        )

    assessment = conn.execute(
        "SELECT assessment_id, page_count FROM ocr_document_assessments "
        "WHERE document_version_id = ? "
        "ORDER BY assessed_at DESC, assessment_id DESC LIMIT 1",
        (document_version_id,),
    ).fetchone()
    if assessment is None:
        raise ValueError("PDF corpus selection requires a current OCR preflight assessment")
    assessment_id = str(assessment[0])
    page_count = int(assessment[1])
    page_policy = {
        int(row[0]): bool(row[1])
        for row in conn.execute(
            "SELECT page_number, requires_ocr FROM ocr_preflight_pages "
            "WHERE assessment_id = ? ORDER BY page_number",
            (assessment_id,),
        ).fetchall()
    }
    if set(page_policy) != set(range(1, page_count + 1)):
        raise ValueError("PDF corpus selection requires complete preflight page coverage")
    accepted_ocr_nodes = {
        (int(row[0]), str(row[1]))
        for row in conn.execute(
            "SELECT result.page_number, result.node_id "
            "FROM ocr_page_results AS result "
            "JOIN ocr_extraction_governance AS governance "
            "ON governance.extraction_run_id = result.extraction_run_id "
            "JOIN evidence_extraction_runs AS run "
            "ON run.extraction_run_id = result.extraction_run_id "
            "WHERE governance.assessment_id = ? AND result.outcome = 'accepted' "
            "AND result.node_id IS NOT NULL AND run.outcome = 'succeeded'",
            (assessment_id,),
        ).fetchall()
    }
    by_page: dict[int, list[tuple[str, str, datetime]]] = {
        page_number: [] for page_number in page_policy
    }
    for row in raw_rows:
        if str(row[3]) != "pdf_page" or row[4] is None:
            continue
        locator = json.loads(str(row[4]))
        locator_object = cast(dict[str, object], locator) if isinstance(locator, dict) else {}
        page_value = locator_object.get("page_number")
        if not isinstance(page_value, int) or page_value not in page_policy:
            continue
        node_id = str(row[0])
        extractor_name = str(row[5])
        if extractor_name == FULLTEXT_EXTRACTOR_NAME and (
            str(row[7]) != fulltext_identity.code_version
            or str(row[8]) != fulltext_identity.config_sha256
        ):
            continue
        requires_ocr = page_policy[page_value]
        if requires_ocr:
            if (page_value, node_id) not in accepted_ocr_nodes:
                continue
        elif extractor_name == "governed-pdf-ocr":
            continue
        by_page[page_value].append((node_id, str(row[1]), _datetime(row[2])))
    selected: list[tuple[str, str, datetime]] = []
    for page_number in sorted(by_page):
        candidates = by_page[page_number]
        if len(candidates) != 1:
            lane = "accepted OCR" if page_policy[page_number] else "native"
            raise ValueError(
                f"PDF page {page_number} requires exactly one {lane} evidence node; "
                f"found {len(candidates)}"
            )
        selected.append(candidates[0])
    return tuple(selected)


def _authoritative_non_pdf_rows(
    raw_rows: Sequence[sqlite3.Row | tuple[object, ...]],
    fulltext_identity: FulltextExtractorIdentity,
    *,
    accepted_image_node_ids: frozenset[str],
) -> tuple[sqlite3.Row | tuple[object, ...], ...]:
    """Keep only the exact deliberately promoted full-text identity.

    Evidence revisions supersede matching evidence keys, but a materially
    redesigned extractor can use a new key namespace.  Without this additional
    authority decision, an older whole-document assertion and its newer
    structured replacement are both current and duplicate the same disclosure.
    """

    selected = tuple(
        row
        for row in raw_rows
        if (
            (
                str(row[5]) == FULLTEXT_EXTRACTOR_NAME
                and str(row[7]) == fulltext_identity.code_version
                and str(row[8]) == fulltext_identity.config_sha256
            )
            or (str(row[5]) == IMAGE_OCR_EXTRACTOR_NAME and str(row[0]) in accepted_image_node_ids)
            or str(row[5])
            not in {
                FULLTEXT_EXTRACTOR_NAME,
                IMAGE_OCR_EXTRACTOR_NAME,
                "governed-pdf-ocr",
            }
        )
    )
    matching_run_ids = {str(row[9]) for row in selected if str(row[5]) == FULLTEXT_EXTRACTOR_NAME}
    if len(matching_run_ids) > 1:
        raise ValueError(
            "non-PDF corpus selection requires exactly one succeeded run for "
            "the promoted full-text extractor identity"
        )
    return selected


def _canonical_non_pdf_nodes(
    raw_rows: Sequence[sqlite3.Row | tuple[object, ...]],
) -> tuple[tuple[str, str, datetime], ...]:
    """Select one searchable level from structured evidence hierarchies.

    Current v3 HTML rows contain only exact opening-tag spans, while their
    cells contain the substantive values.  Those rows and their aggregate
    table parents are structural provenance, not search text.  A later
    structured extractor may emit a source-grounded contextual row; when it
    does, that row replaces all of its direct cells.  Earlier extractors retain
    their historical all-node behavior.
    """

    structured = [row for row in raw_rows if _uses_canonical_hierarchy_policy(str(row[7]))]
    contextual_row_ids = {
        str(row[0])
        for row in structured
        if str(row[3]) == "table_row" and not _is_raw_structure_tag(str(row[1]))
    }
    selected: list[tuple[str, str, datetime]] = []
    for row in raw_rows:
        node_id = str(row[0])
        text = str(row[1])
        if not _uses_canonical_hierarchy_policy(str(row[7])):
            selected.append((node_id, text, _datetime(row[2])))
            continue
        node_kind = str(row[3])
        parent_node_id = None if row[6] is None else str(row[6])
        if node_kind == "table":
            continue
        if node_kind == "table_row":
            if node_id in contextual_row_ids:
                selected.append((node_id, text, _datetime(row[2])))
            continue
        if node_kind == "table_cell" and parent_node_id in contextual_row_ids:
            continue
        selected.append((node_id, text, _datetime(row[2])))
    return tuple(selected)


def _uses_canonical_hierarchy_policy(extractor_code_version: str) -> bool:
    return extractor_code_version == STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR.code_version


def _is_raw_structure_tag(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith("<") and stripped.endswith(">")


def _node_selection_policy_config() -> dict[str, object]:
    return {
        "version": NODE_SELECTION_POLICY_VERSION,
        "scope": "deliberately promoted exact extractor identities",
        "extractor_authority_policy": FULLTEXT_EXTRACTOR_IDENTITY_POLICY_VERSION,
        "fulltext_identities": tuple(
            {
                "name": identity.name,
                "code_version": identity.code_version,
                "config_sha256": identity.config_sha256,
                "hierarchy": identity.hierarchy,
            }
            for identity in (
                BASE_FULLTEXT_EXTRACTOR,
                OFFICE_FULLTEXT_EXTRACTOR,
                STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR,
            )
        ),
        "include": (
            "non_hierarchical_nodes",
            "contextual_table_row",
            "table_cell_when_parent_row_is_structural",
        ),
        "exclude": (
            "table",
            "raw_markup_table_row",
            "table_cell_when_parent_row_is_contextual",
        ),
        "unknown_generation_behavior": "exclude",
    }


def _offsets(text: str, config: ChunkerConfig) -> tuple[tuple[int, int], ...]:
    """Return contiguous offsets bounded by characters and whitespace tokens.

    A very long individual token is retained whole instead of silently cutting
    its characters.  This makes the exact source slice reconstructible and
    keeps every chunk wholly within a single evidence node.
    """

    if not text:
        raise ValueError("evidence node text must not be empty")
    offsets: list[tuple[int, int]] = []
    start = 0
    text_length = len(text)
    while start < text_length:
        maximum_end = min(text_length, start + config.max_characters)
        end = _token_bounded_end(text, start, maximum_end, config.max_tokens)
        if end < text_length and not text[end].isspace():
            boundary = text.rfind(" ", start, end + 1)
            if boundary > start:
                end = boundary + 1
            else:
                next_boundary = _next_whitespace(text, end)
                end = text_length if next_boundary is None else next_boundary + 1
        if end <= start:
            raise RuntimeError("chunker failed to advance its source offset")
        offsets.append((start, end))
        start = end
    return tuple(offsets)


def _token_bounded_end(text: str, start: int, maximum_end: int, maximum_tokens: int) -> int:
    token_count = 0
    in_token = False
    end = start
    while end < maximum_end:
        is_token_character = not text[end].isspace()
        if is_token_character and not in_token:
            token_count += 1
            if token_count > maximum_tokens:
                break
        in_token = is_token_character
        end += 1
    return end


def _next_whitespace(text: str, start: int) -> int | None:
    for index in range(start, len(text)):
        if text[index].isspace():
            return index
    return None


def _persist_record_batch(
    conn: sqlite3.Connection,
    store: GroundedSearchStore,
    records: Sequence[CorpusManifest | CorpusDocumentMembership],
) -> tuple[int, int]:
    created = 0
    replayed = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        for record in records:
            was_created = _persist_unsealed_replay_safe(conn, store, record)
            created += int(was_created)
            replayed += int(not was_created)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return created, replayed


def _persist_inventory_links(
    conn: sqlite3.Connection,
    store: SourceInventorySealStore,
    *,
    manifest_id: str,
    snapshot_ids: Sequence[str],
    linked_at: datetime,
) -> tuple[int, int]:
    created = 0
    replayed = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        for snapshot_id in snapshot_ids:
            result = store.persist(
                InventoryManifestLink(
                    manifest_id=manifest_id,
                    snapshot_id=snapshot_id,
                    linked_at=linked_at,
                )
            )
            created += int(result.created)
            replayed += int(not result.created)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return created, replayed


def _persist_chunk_batch(
    conn: sqlite3.Connection,
    store: GroundedSearchStore,
    chunks: Sequence[SearchChunk],
) -> tuple[int, int]:
    """Persist at most the caller's configured batch in one write transaction."""

    created = 0
    replayed = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        for chunk in chunks:
            was_created = _persist_unsealed_replay_safe(conn, store, chunk)
            created += int(was_created)
            replayed += int(not was_created)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return created, replayed


def _verify_complete_chunk_stage(
    conn: sqlite3.Connection,
    manifest_id: str,
    memberships: Sequence[CorpusDocumentMembership],
    request: CorpusBuildRequest,
    chunker_sha: str,
) -> int:
    """Prove all deterministic chunks exist exactly and no extra row exists."""

    expected_count = 0
    for expected in _iter_manifest_chunks(conn, memberships, request, chunker_sha):
        expected_count += 1
        _require_exact_chunk(conn, expected)
        row = conn.execute(
            "SELECT COUNT(*), MIN(text), MAX(text) FROM search_lexical_chunks WHERE chunk_id = ?",
            (expected.chunk_id,),
        ).fetchone()
        if (
            row is None
            or int(row[0]) != 1
            or str(row[1]) != expected.text
            or str(row[2]) != expected.text
        ):
            raise RuntimeError(f"FTS5 row missing or conflicting for {expected.chunk_id}")
    stored_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM search_chunks WHERE manifest_id = ?",
            (manifest_id,),
        ).fetchone()[0]
    )
    if stored_count != expected_count:
        raise RuntimeError(
            "staged corpus has missing or unexpected chunks: "
            f"expected {expected_count}, found {stored_count}"
        )
    return expected_count


def _require_exact_chunk(conn: sqlite3.Connection, chunk: SearchChunk) -> None:
    columns = (
        "chunk_id",
        "idempotency_key",
        "manifest_id",
        "evidence_node_id",
        "chunk_key",
        "chunk_revision",
        "text",
        "content_sha256",
        "char_start",
        "char_end",
        "chunker_config_sha256",
        "chunker_code_version",
        "available_at",
        "recorded_at",
    )
    expected: tuple[object, ...] = (
        chunk.chunk_id,
        chunk.idempotency_key,
        chunk.manifest_id,
        chunk.evidence_node_id,
        chunk.chunk_key,
        chunk.chunk_revision,
        chunk.text,
        chunk.content_sha256,
        chunk.char_start,
        chunk.char_end,
        chunk.chunker_config_sha256,
        chunk.chunker_code_version,
        chunk.available_at,
        chunk.recorded_at,
    )
    row = conn.execute(
        f"SELECT {', '.join(columns)} FROM search_chunks WHERE chunk_id = ?",  # nosec B608 -- trusted internal SQL shape; values remain bound
        (chunk.chunk_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"deterministic search chunk is absent: {chunk.chunk_id}")
    if not _same_stored_values(tuple(row), expected):
        raise ValueError(f"immutable search chunk {chunk.chunk_id!r} conflicts with plan")


_CHUNK_DIGEST_COLUMNS = (
    "chunk_id",
    "idempotency_key",
    "manifest_id",
    "evidence_node_id",
    "chunk_key",
    "chunk_revision",
    "text",
    "content_sha256",
    "char_start",
    "char_end",
    "chunker_config_sha256",
    "chunker_code_version",
    "available_at",
    "recorded_at",
)


def _digest_rows(rows: Iterable[Sequence[object]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(
            json.dumps(
                [_canonical_digest_value(value) for value in row],
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _canonical_digest_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None).isoformat()
    return value


def _chunk_digest_from_models(chunks: Sequence[SearchChunk]) -> str:
    by_id = sorted(chunks, key=lambda chunk: chunk.chunk_id)
    return _digest_rows(
        (
            chunk.chunk_id,
            chunk.idempotency_key,
            chunk.manifest_id,
            chunk.evidence_node_id,
            chunk.chunk_key,
            chunk.chunk_revision,
            chunk.text,
            chunk.content_sha256,
            chunk.char_start,
            chunk.char_end,
            chunk.chunker_config_sha256,
            chunk.chunker_code_version,
            chunk.available_at,
            chunk.recorded_at,
        )
        for chunk in by_id
    )


def _chunk_count_and_digest(
    conn: sqlite3.Connection,
    manifest_id: str,
) -> tuple[int, str]:
    cursor = conn.execute(
        f"SELECT {', '.join(_CHUNK_DIGEST_COLUMNS)} FROM search_chunks "  # nosec B608 -- trusted internal SQL shape; values remain bound
        "WHERE manifest_id = ? ORDER BY chunk_id",
        (manifest_id,),
    )
    count = 0

    def counted_rows() -> Iterator[Sequence[object]]:
        nonlocal count
        while True:
            batch = cursor.fetchmany(500)
            if not batch:
                return
            for row in batch:
                count += 1
                yield row

    digest = _digest_rows(counted_rows())
    return count, digest


def _lexical_projection_count_and_digest(
    conn: sqlite3.Connection,
    manifest_id: str,
) -> tuple[int, str]:
    cursor = conn.execute(
        "SELECT lexical.chunk_id, lexical.text "
        "FROM search_lexical_chunks AS lexical "
        "JOIN search_chunks AS chunk ON chunk.chunk_id = lexical.chunk_id "
        "WHERE chunk.manifest_id = ? ORDER BY lexical.chunk_id, lexical.rowid",
        (manifest_id,),
    )
    count = 0

    def counted_rows() -> Iterator[Sequence[object]]:
        nonlocal count
        while True:
            batch = cursor.fetchmany(500)
            if not batch:
                return
            for row in batch:
                count += 1
                yield row

    digest = _digest_rows(counted_rows())
    return count, digest


def _lexical_config_hash(
    manifest_id: str,
    chunk_count: int,
    chunk_digest: str,
    lexical_count: int,
    lexical_digest: str,
) -> str:
    return _sha256_json(
        {
            "manifest_id": manifest_id,
            "index_kind": "lexical",
            "fts": "sqlite-fts5",
            "chunk_count": chunk_count,
            "chunk_digest_sha256": chunk_digest,
            "lexical_row_count": lexical_count,
            "lexical_projection_digest_sha256": lexical_digest,
        }
    )


def lexical_index_config_sha256(conn: sqlite3.Connection, *, manifest_id: str) -> str:
    """Recompute the exact source-chunk and live FTS projection commitment."""

    chunk_count, chunk_digest = _chunk_count_and_digest(conn, manifest_id)
    lexical_count, lexical_digest = _lexical_projection_count_and_digest(conn, manifest_id)
    if lexical_count != chunk_count:
        raise ValueError("lexical projection row count does not match the manifest chunk set")
    return _lexical_config_hash(
        manifest_id,
        chunk_count,
        chunk_digest,
        lexical_count,
        lexical_digest,
    )


def _lexical_index(
    *,
    manifest_id: str,
    recorded_at: datetime,
    config_sha256: str,
) -> IndexRun:
    index_seed = _sha256_text(manifest_id + chr(0) + "lexical")
    index_run_id = f"lexical-index:{index_seed}"
    return IndexRun(
        index_run_id=index_run_id,
        idempotency_key=f"lexical-index:{index_seed}",
        index_key=f"lexical:{manifest_id}",
        revision=1,
        manifest_id=manifest_id,
        index_kind="lexical",
        config_sha256=config_sha256,
        code_version=_BUILDER_VERSION,
        outcome="succeeded",
        started_at=recorded_at,
        completed_at=recorded_at,
    )


def _persist_lexical_projection_seal(
    conn: sqlite3.Connection,
    *,
    lexical_index: IndexRun,
    sealed_at: datetime,
) -> bool:
    """Publish the exact FTS representation only on schemas that support seals.

    The conditional preserves explicit historical-schema unit fixtures.  A
    current production writer is independently required to match Alembic head,
    so current publication always traverses this path.
    """

    if (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'search_projection_seals'"
        ).fetchone()
        is None
    ):
        return False
    # Lazy import avoids a module cycle: the lineage verifier reuses this
    # module's lexical config recomputation.
    from provenance.search_index_lineage import (
        SearchProjectionSeal,
        lexical_projection_commitment,
        manifest_chunk_commitment,
        persist_projection_seal,
    )

    chunk_count, chunk_digest = manifest_chunk_commitment(
        conn,
        manifest_id=lexical_index.manifest_id,
    )
    lexical_count, lexical_digest = lexical_projection_commitment(
        conn,
        manifest_id=lexical_index.manifest_id,
    )
    if lexical_count != chunk_count:
        raise RuntimeError("lexical projection cannot be sealed with missing or extra rows")
    seal_seed = _sha256_text(lexical_index.index_run_id + chr(0) + lexical_index.config_sha256)
    return persist_projection_seal(
        conn,
        SearchProjectionSeal(
            projection_seal_id=f"search-projection-seal:{seal_seed}",
            idempotency_key=f"search-projection-seal:{seal_seed}",
            index_run_id=lexical_index.index_run_id,
            manifest_id=lexical_index.manifest_id,
            index_kind="lexical",
            chunk_count=chunk_count,
            chunk_set_sha256=chunk_digest,
            projection_records_sha256=lexical_digest,
            artifact_set_sha256=None,
            provider=None,
            model=None,
            dimensions=None,
            config_sha256=lexical_index.config_sha256,
            storage_uri="sqlite-fts5://search_lexical_chunks",
            sealed_at=sealed_at,
        ),
    )


def _persist_unsealed_replay_safe(
    conn: sqlite3.Connection,
    store: GroundedSearchStore,
    record: CorpusManifest | CorpusDocumentMembership | SearchChunk,
) -> bool:
    """Replay records blocked by the sealed-manifest INSERT guards without bypassing conflicts.

    SQLite executes the migration's ``BEFORE INSERT`` guard before evaluating
    ``ON CONFLICT DO NOTHING``.  Once a manifest is sealed, an exact replay of
    its already-persisted memberships/chunks must therefore be recognized by a
    read before calling the standard typed append boundary.
    """

    if isinstance(record, CorpusDocumentMembership):
        columns = (
            "membership_id",
            "manifest_id",
            "expected_document_key",
            "document_version_id",
            "membership_status",
            "reason",
            "recorded_at",
        )
        values: tuple[object, ...] = (
            record.membership_id,
            record.manifest_id,
            record.expected_document_key,
            record.document_version_id,
            record.membership_status,
            record.reason,
            record.recorded_at,
        )
        table, identity_column, identity_value = (
            "search_corpus_document_memberships",
            "membership_id",
            record.membership_id,
        )
    elif isinstance(record, SearchChunk):
        columns = (
            "chunk_id",
            "idempotency_key",
            "manifest_id",
            "evidence_node_id",
            "chunk_key",
            "chunk_revision",
            "text",
            "content_sha256",
            "char_start",
            "char_end",
            "chunker_config_sha256",
            "chunker_code_version",
            "available_at",
            "recorded_at",
        )
        values = (
            record.chunk_id,
            record.idempotency_key,
            record.manifest_id,
            record.evidence_node_id,
            record.chunk_key,
            record.chunk_revision,
            record.text,
            record.content_sha256,
            record.char_start,
            record.char_end,
            record.chunker_config_sha256,
            record.chunker_code_version,
            record.available_at,
            record.recorded_at,
        )
        table, identity_column, identity_value = "search_chunks", "chunk_id", record.chunk_id
    else:
        return store.persist(record).created
    existing = conn.execute(
        f"SELECT {', '.join(columns)} FROM {table} WHERE {identity_column} = ?",  # nosec B608 -- trusted internal SQL shape; values remain bound
        (identity_value,),
    ).fetchone()
    if existing is None:
        return store.persist(record).created
    if not _same_stored_values(tuple(existing), values):
        raise ValueError(
            f"immutable {table} identity {identity_value!r} conflicts with existing data"
        )
    return False


def _same_stored_values(existing: tuple[object, ...], expected: tuple[object, ...]) -> bool:
    if len(existing) != len(expected):
        return False
    for stored, supplied in zip(existing, expected, strict=True):
        if isinstance(supplied, datetime):
            try:
                normalized = datetime.fromisoformat(str(stored)).replace(tzinfo=None)
            except ValueError:
                return False
            if normalized != supplied.replace(tzinfo=None):
                return False
        elif stored != supplied:
            return False
    return True


def _result(
    plan: _CorpusPlan,
    *,
    mode: _BuilderMode,
    records_created: int,
    records_replayed: int,
) -> CorpusBuildResult:
    return CorpusBuildResult(
        mode=mode,
        manifest_id=plan.manifest.manifest_id,
        lexical_index_run_id=plan.lexical_index.index_run_id,
        completion_status=plan.seal.completion_status,
        expected_document_count=len(plan.memberships),
        included_document_count=sum(
            membership.membership_status == "included" for membership in plan.memberships
        ),
        chunks_planned=len(plan.chunks),
        records_created=records_created,
        records_replayed=records_replayed,
        manifest_config_sha256=plan.manifest_config_sha256,
        chunker_config_sha256=plan.chunker_config_sha256,
        planned_chunks=plan.chunks,
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, default=_json_default, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    ).hexdigest()


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def _datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("evidence extraction clock is not ISO-8601") from error
    raise ValueError("evidence extraction clock has an unsupported type")
