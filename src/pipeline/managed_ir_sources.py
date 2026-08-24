"""Capability-gated promotion of issuer documents staged by a managed run."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path, PurePath
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from db_paths import configured_db_path
from ir_uploads import CategorizationFailure, classify_ir_file
from models.ir_uploads import CategorizationResult
from pipeline.issuer_document_inventory import (
    IssuerDocumentInventoryReceipt,
    IssuerDocumentInventoryRequest,
    build_issuer_document_inventory,
)
from pipeline.source_policy import (
    ArtifactKind,
    CollectionSource,
    authorize_stored_collection_target,
    reported_quarter_is_in_window,
)
from provenance.immutable_artifact import (
    ImmutableArtifactConflictError,
    read_stable_artifact,
    require_canonical_text_artifact,
    require_no_reparse_points,
)
from provenance.secure_file_install import (
    SecureFileInstallError,
    SecureFileOwnershipToken,
    cleanup_owned_file,
    install_bytes_no_clobber,
)
from runtime.job_runtime import JobLock, current_lock_claim
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

_OPERATION = "managed_issuer_document_publication.v1"


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha(value: object) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _document_set(receipt: IssuerDocumentStagingReceipt) -> str:
    return _sha([item.model_dump(mode="json") for item in receipt.documents])


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _relative_path(root: Path, path: Path) -> str:
    """One canonical persisted path form, independent of host path separators."""
    return _canonical_relative_path(root, path)


def _canonical_relative_path(root: PurePath, path: PurePath) -> str:
    """Pure-path form makes the persistence contract directly testable on Windows."""
    return path.relative_to(root).as_posix()


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class IssuerDocumentStagingRequest(_Closed):
    schema_version: Literal["issuer_document_staging_request.v1"] = (
        "issuer_document_staging_request.v1"
    )
    attempt_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")
    inventory_request: IssuerDocumentInventoryRequest
    inventory_request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def bind(self) -> Self:
        if self.inventory_request_sha256 != self.inventory_request.request_sha256:
            raise ValueError("inventory_request_sha256 mismatch")
        return self


class StagedIssuerDocument(_Closed):
    schema_version: Literal["staged_issuer_document.v1"] = "staged_issuer_document.v1"
    source_url: str
    document_type: str = Field(
        pattern=r"^(ir_investor_update|ir_presentation|ir_supplement|sec_10q|ir_transcript|ir_press_release)$"
    )
    object_path: str = Field(pattern=r"^objects/[A-Za-z0-9._-]+$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int = Field(gt=0)
    fetched_at: datetime
    media_type: Literal[
        "application/pdf", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ]
    ticker: str
    period_end: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    classification_confidence: Literal["high", "medium", "low"]
    classification_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_path(self) -> Self:
        if self.fetched_at.tzinfo is None or self.fetched_at.utcoffset() != UTC.utcoffset(None):
            raise ValueError("fetched_at must be UTC")
        if (self.media_type == "application/pdf") != self.object_path.endswith(".pdf"):
            raise ValueError("media type/object extension mismatch")
        if self.media_type.endswith("sheet") and not self.object_path.endswith(".xlsx"):
            raise ValueError("media type/object extension mismatch")
        return self


class IssuerDocumentStagingReceipt(_Closed):
    schema_version: Literal["issuer_document_staging_receipt.v1"] = (
        "issuer_document_staging_receipt.v1"
    )
    request: IssuerDocumentStagingRequest
    documents: tuple[StagedIssuerDocument, ...] = Field(min_length=1)
    classifier_code_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verifier_code_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_mutations: Literal[False] = False
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def seal(self) -> Self:
        expected = tuple(
            (x.source_url, x.document_type)
            for x in self.request.inventory_request.expected_documents
        )
        actual = tuple((x.source_url, x.document_type) for x in self.documents)
        if actual != expected or len({x.object_path for x in self.documents}) != len(
            self.documents
        ):
            raise ValueError("staged documents must exactly cover request")
        if any(
            x.ticker != self.request.inventory_request.ticker
            or x.period_end != self.request.inventory_request.period_end.isoformat()
            for x in self.documents
        ):
            raise ValueError("staged classification mismatch")
        if self.receipt_sha256 != _sha(self.model_dump(mode="json", exclude={"receipt_sha256"})):
            raise ValueError("receipt_sha256 mismatch")
        return self

    @property
    def canonical_json(self) -> str:
        return _json(self.model_dump(mode="json"))


class PreparedIssuerDocumentPublication(_Closed):
    schema_version: Literal["prepared_issuer_document_publication.v1"] = (
        "prepared_issuer_document_publication.v1"
    )
    committed: Literal[True] = True
    staging_receipt: IssuerDocumentStagingReceipt
    receipt_path: str
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    inserted_document_ids: tuple[int, ...]
    reused_document_ids: tuple[int, ...]
    canonical_paths: tuple[str, ...]
    canonical_path_sha256: tuple[str, ...]
    # Only paths newly linked by this attempt. Replays retain this durable
    # disposition rather than treating every canonical path as newly created.
    created_paths: tuple[str, ...]
    created_path_sha256: tuple[str, ...]
    inventory_receipt_path: str
    inventory_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def seal(self) -> Self:
        receipt = self.staging_receipt
        expected_paths = tuple(
            f"ir_documents/{item.ticker}/{item.period_end}/{item.document_type}__{item.sha256[:8]}"
            f"{'.pdf' if item.media_type == 'application/pdf' else '.xlsx'}"
            for item in receipt.documents
        )
        document_set = _document_set(receipt)
        evidence_prefix = f"data/managed_ir_publications/{receipt.request.attempt_id}/"
        if (
            self.receipt_sha256 != receipt.receipt_sha256
            or self.document_set_sha256 != document_set
            or self.canonical_paths != expected_paths
            or self.canonical_path_sha256 != tuple(item.sha256 for item in receipt.documents)
            or len(self.canonical_paths) != len(self.canonical_path_sha256)
            or len(self.created_paths) != len(self.created_path_sha256)
            or len(set(self.created_paths)) != len(self.created_paths)
            or any(path not in self.canonical_paths for path in self.created_paths)
            or any(
                self.canonical_path_sha256[self.canonical_paths.index(path)] != digest
                for path, digest in zip(self.created_paths, self.created_path_sha256, strict=True)
            )
            or self.receipt_path != evidence_prefix + "staging_receipt.json"
            or self.inventory_receipt_path != evidence_prefix + "inventory_receipt.json"
            or set(self.inserted_document_ids).intersection(self.reused_document_ids)
        ):
            raise ValueError("publication binding mismatch")
        if self.result_sha256 != _sha(self.model_dump(mode="json", exclude={"result_sha256"})):
            raise ValueError("result_sha256 mismatch")
        return self


class PreparedIssuerDocumentPublisherError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        committed: bool = False,
        inserted: tuple[int, ...] = (),
        reused: tuple[int, ...] = (),
        created: tuple[str, ...] = (),
        removed_paths: tuple[str, ...] = (),
        remaining_paths: tuple[str, ...] = (),
        owned_artifacts: tuple[str, ...] = (),
        owned_identities: tuple[SecureFileOwnershipToken, ...] = (),
        inventory_state: str = "not_started",
        result_state: str = "not_started",
    ) -> None:
        self.code = code
        self.committed = committed
        self.inserted = inserted
        self.reused = reused
        self.created = created
        self.removed_paths = removed_paths
        self.remaining_paths = remaining_paths
        self.owned_artifacts = owned_artifacts
        self.owned_identities = owned_identities
        self.inventory_state = inventory_state
        self.result_state = result_state
        super().__init__(code)


def _cleanup_owned_artifacts(
    artifacts: list[SecureFileOwnershipToken],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Remove only still-owned names and report replacements as retained residue."""
    removed: list[str] = []
    remaining: list[str] = []
    for artifact in reversed(artifacts):
        result = cleanup_owned_file(artifact)
        if result.removed:
            removed.append(str(result.path))
        else:
            remaining.append(str(result.path))
    return tuple(removed), tuple(remaining)


class _CanonicalPublicationPath(_Closed):
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int = Field(gt=0)


class _PublicationIntentItem(_Closed):
    source_url: str
    canonical_path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int = Field(gt=0)
    fetched_at: datetime
    row_disposition: Literal["insert", "reuse"]
    file_disposition: Literal["create", "reuse"]
    existing_id: int | None = None

    @model_validator(mode="after")
    def bind_disposition(self) -> Self:
        if (self.row_disposition == "reuse") != (self.existing_id is not None):
            raise ValueError("reuse disposition must bind an existing row id")
        return self


class _PublicationIntent(_Closed):
    schema_version: Literal["managed_ir_publication_intent.v1"] = "managed_ir_publication_intent.v1"
    attempt_id: str
    staging_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    classifier_code_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verifier_code_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    configured_database: str
    binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    documents: tuple[_PublicationIntentItem, ...] = Field(min_length=1)
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def seal(self) -> Self:
        if len({item.source_url for item in self.documents}) != len(
            self.documents
        ) or self.payload_sha256 != _sha(self.model_dump(mode="json", exclude={"payload_sha256"})):
            raise ValueError("publication intent binding mismatch")
        return self


class _InventoryEvidence(_Closed):
    attempt_id: str
    publication_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    inventory_receipt_path: str
    inventory_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def seal(self) -> Self:
        if self.payload_sha256 != _sha(self.model_dump(mode="json", exclude={"payload_sha256"})):
            raise ValueError("inventory evidence binding mismatch")
        return self


class _CommittedPublicationRecord(_Closed):
    """The immutable episode truth written with the document transaction."""

    attempt_id: str
    staging_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    inserted_ids: tuple[int, ...]
    reused_ids: tuple[int, ...]
    canonical_paths: tuple[_CanonicalPublicationPath, ...]
    created_paths: tuple[str, ...]
    staging_receipt_path: str
    inventory_receipt_path: str
    publication_result_path: str
    intent_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    committed_at: datetime
    state: Literal["committed"]
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def seal(self) -> Self:
        if (
            set(self.inserted_ids).intersection(self.reused_ids)
            or len(set(self.created_paths)) != len(self.created_paths)
            or any(
                path not in {entry.path for entry in self.canonical_paths}
                for path in self.created_paths
            )
            or self.staging_receipt_path
            != f"data/managed_ir_publications/{self.attempt_id}/staging_receipt.json"
            or self.inventory_receipt_path
            != f"data/managed_ir_publications/{self.attempt_id}/inventory_receipt.json"
            or self.publication_result_path
            != f"data/managed_ir_publications/{self.attempt_id}/publication_result.json"
            or self.payload_sha256 != _sha(self.model_dump(mode="json", exclude={"payload_sha256"}))
        ):
            raise ValueError("publication record binding mismatch")
        return self


def _sealed_publication_record(value: Mapping[str, object]) -> _CommittedPublicationRecord:
    """Hash Pydantic's canonical JSON shape, including UTC normalization."""
    raw_paths = value["canonical_paths"]
    if not isinstance(raw_paths, (list, tuple)):
        raise PreparedIssuerDocumentPublisherError("publication_record_invalid")
    canonical_paths = cast("list[object] | tuple[object, ...]", raw_paths)
    normalized = {
        **value,
        "canonical_paths": tuple(
            _CanonicalPublicationPath.model_validate(item) for item in canonical_paths
        ),
        "committed_at": datetime.fromisoformat(str(value["committed_at"])),
    }
    provisional = _CommittedPublicationRecord.model_construct(
        None, **normalized, payload_sha256="0" * 64
    )
    return _CommittedPublicationRecord.model_validate(
        {
            **normalized,
            "payload_sha256": _sha(provisional.model_dump(mode="json", exclude={"payload_sha256"})),
        }
    )


@dataclass(frozen=True, slots=True)
class _Authority:
    operation: str
    pid: int
    attempt: str
    receipt_sha: str
    code_root: str
    state_root: str
    database: str
    ir_claim: tuple[int, str, str | None]
    db_claim: tuple[int, str, str | None]


def _code_root() -> Path:
    root = Path(__file__).resolve().parents[2]
    require_no_reparse_points(root)
    return root


def classifier_code_identity() -> str:
    root = _code_root()
    return _sha(
        {
            "fetch_preparer": _file_sha(root / "execution" / "fetch_ir_documents.py"),
            "classifier_and_registry": _file_sha(root / "src" / "ir_uploads.py"),
            "classifier_model": _file_sha(root / "src" / "models" / "ir_uploads.py"),
            "document_model": _file_sha(root / "src" / "models" / "documents.py"),
            "source_policy": _file_sha(root / "src" / "pipeline" / "source_policy.py"),
            "secure_file_install": _file_sha(
                root / "src" / "provenance" / "secure_file_install.py"
            ),
            "verifier_identity": _file_sha(root / "src" / "provenance" / "verifier_identity.py"),
            "inventory_capture": _file_sha(
                root / "execution" / "capture_issuer_document_inventory.py"
            ),
        }
    )


def verifier_code_identity() -> str:
    root = _code_root()
    return _sha(
        {
            "managed": _file_sha(Path(__file__)),
            "inventory": _file_sha(root / "src" / "pipeline" / "issuer_document_inventory.py"),
            "source_policy": _file_sha(root / "src" / "pipeline" / "source_policy.py"),
            "document_model": _file_sha(root / "src" / "models" / "documents.py"),
            "job_runtime": _file_sha(root / "src" / "runtime" / "job_runtime.py"),
            "sqlite_runtime": _file_sha(root / "src" / "sqlite_runtime.py"),
            "db_paths": _file_sha(root / "src" / "db_paths.py"),
            "immutable_artifact": _file_sha(root / "src" / "provenance" / "immutable_artifact.py"),
            "secure_file_install": _file_sha(
                root / "src" / "provenance" / "secure_file_install.py"
            ),
            "verifier_identity": _file_sha(root / "src" / "provenance" / "verifier_identity.py"),
            "inventory_capture": _file_sha(
                root / "execution" / "capture_issuer_document_inventory.py"
            ),
            "publication_schema": _file_sha(
                root / "alembic" / "versions" / "0021_managed_ir_publications.py"
            ),
        }
    )


def classification_evidence(outcome: CategorizationResult) -> str:
    dumped = outcome.model_dump()
    return _sha(
        {
            key: dumped[key]
            for key in ("ticker_evidence", "doc_type_evidence", "period_evidence", "confidence")
        }
    )


def _receipt_path(root: Path, attempt: str) -> Path:
    return root / ".tmp" / "managed_ir_staging" / attempt / "staging_receipt.json"


def _intent_path(root: Path, attempt: str) -> Path:
    return root / ".tmp" / "managed_ir_staging" / attempt / "publication_intent.json"


def _evidence_directory(root: Path, attempt: str) -> Path:
    return root / "data" / "managed_ir_publications" / attempt


def _inventory_path(root: Path, attempt: str) -> Path:
    return _evidence_directory(root, attempt) / "inventory_receipt.json"


def _result_path(root: Path, attempt: str) -> Path:
    return _evidence_directory(root, attempt) / "publication_result.json"


def _durable_receipt_path(root: Path, attempt: str) -> Path:
    return _evidence_directory(root, attempt) / "staging_receipt.json"


def _configured(root: Path, db_path: Path) -> Path:
    actual = db_path.resolve()
    if configured_db_path(root) != actual:
        raise PreparedIssuerDocumentPublisherError("configured_database_mismatch")
    require_no_reparse_points(actual)
    return actual


def _policy(request: IssuerDocumentStagingRequest, db_path: Path) -> None:
    inv = request.inventory_request
    auth = authorize_stored_collection_target(
        db_path,
        inv.ticker,
        requested=True,
        source=CollectionSource.IR,
        artifact_kind=ArtifactKind.IR_DOCUMENT,
    )
    if not auth.allowed:
        raise PreparedIssuerDocumentPublisherError("source_policy_denied")
    if auth.fiscal_year_end_month is None or not reported_quarter_is_in_window(
        fiscal_year=inv.fiscal_year,
        fiscal_quarter=inv.fiscal_quarter,
        fiscal_year_end_month=auth.fiscal_year_end_month,
        as_of=date.today(),
    ):
        raise PreparedIssuerDocumentPublisherError("reported_quarter_window_denied")


def _read_receipt(
    root: Path, request: IssuerDocumentStagingRequest
) -> IssuerDocumentStagingReceipt:
    try:
        snap, raw = read_stable_artifact(_receipt_path(root, request.attempt_id))
        receipt = IssuerDocumentStagingReceipt.model_validate_json(raw)
        require_canonical_text_artifact(snap, receipt.canonical_json)
    except (OSError, ValueError, ImmutableArtifactConflictError) as exc:
        raise PreparedIssuerDocumentPublisherError("staging_receipt_invalid") from exc
    if receipt.request != request:
        raise PreparedIssuerDocumentPublisherError("staging_request_mismatch")
    return receipt


def _read_completed_receipt(
    root: Path, request: IssuerDocumentStagingRequest, db_path: Path
) -> IssuerDocumentStagingReceipt:
    """Recover the receipt embedded in immutable completion evidence.

    Completed publication evidence is deliberately replayable after attempt
    staging is wiped, so only policy and code identities are rechecked here;
    object-byte validation was completed before the original commit.
    """
    try:
        snap, raw = read_stable_artifact(_result_path(root, request.attempt_id))
        result = PreparedIssuerDocumentPublication.model_validate_json(raw)
        require_canonical_text_artifact(snap, result.model_dump_json())
        receipt_snap, receipt_raw = read_stable_artifact(
            _durable_receipt_path(root, request.attempt_id)
        )
        durable_receipt = IssuerDocumentStagingReceipt.model_validate_json(receipt_raw)
        require_canonical_text_artifact(receipt_snap, durable_receipt.canonical_json)
    except (OSError, ValueError, ImmutableArtifactConflictError) as exc:
        raise PreparedIssuerDocumentPublisherError("publication_result_invalid") from exc
    receipt = result.staging_receipt
    if receipt != durable_receipt or receipt.request != request:
        raise PreparedIssuerDocumentPublisherError("staging_request_mismatch")
    if (
        receipt.classifier_code_sha256 != classifier_code_identity()
        or receipt.verifier_code_sha256 != verifier_code_identity()
    ):
        raise PreparedIssuerDocumentPublisherError("staging_code_identity_changed")
    _policy(request, db_path)
    return receipt


def validate_prepared_staging(
    request: IssuerDocumentStagingRequest, *, state_root: Path, db_path: Path
) -> IssuerDocumentStagingReceipt:
    """Stable-read, reclassify and re-authorize a receipt; never publishes."""
    root = state_root.resolve(strict=True)
    db_path = _configured(root, db_path)
    receipt = _read_receipt(root, request)
    if (
        receipt.classifier_code_sha256 != classifier_code_identity()
        or receipt.verifier_code_sha256 != verifier_code_identity()
    ):
        raise PreparedIssuerDocumentPublisherError("staging_code_identity_changed")
    _policy(request, db_path)
    staging = root / ".tmp" / "managed_ir_staging" / request.attempt_id
    require_no_reparse_points(staging)
    objects = staging / "objects"
    try:
        require_no_reparse_points(objects)
        entries = tuple(objects.iterdir())
    except (OSError, ImmutableArtifactConflictError) as exc:
        raise PreparedIssuerDocumentPublisherError("staged_objects_directory_invalid") from exc
    declared = {Path(item.object_path).name for item in receipt.documents}
    observed: set[str] = set()
    for entry in entries:
        try:
            metadata = entry.lstat()
        except OSError as exc:
            raise PreparedIssuerDocumentPublisherError("staged_objects_directory_invalid") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or int(metadata.st_nlink) != 1
        ):
            raise PreparedIssuerDocumentPublisherError("staged_objects_directory_invalid")
        observed.add(entry.name)
    if observed != declared:
        raise PreparedIssuerDocumentPublisherError("staged_objects_directory_invalid")
    for item in receipt.documents:
        source = staging / item.object_path
        try:
            snap, _raw = read_stable_artifact(source)
        except (OSError, ImmutableArtifactConflictError) as exc:
            raise PreparedIssuerDocumentPublisherError("staged_object_invalid") from exc
        if snap.file_sha256 != item.sha256 or snap.size_bytes != item.byte_size:
            raise PreparedIssuerDocumentPublisherError("staged_bytes_mismatch")
        if source.stat().st_nlink != 1:
            raise PreparedIssuerDocumentPublisherError("staged_object_linked")
        result = classify_ir_file(
            source,
            ticker_hint=request.inventory_request.ticker,
            source_url=item.source_url,
        )
        if isinstance(result, CategorizationFailure) or (
            result.ticker != item.ticker
            or result.doc_type.value != item.document_type
            or result.period_end.isoformat() != item.period_end
            or result.confidence.value != item.classification_confidence
            or classification_evidence(result) != item.classification_evidence_sha256
        ):
            raise PreparedIssuerDocumentPublisherError("staged_classification_mismatch")
    return receipt


def _claim(root: Path, write_set: str) -> tuple[int, str, str | None]:
    claim = current_lock_claim(root, write_set)
    if claim is None:
        raise PreparedIssuerDocumentPublisherError("managed_lock_claim_missing")
    return claim


def _issue(receipt: IssuerDocumentStagingReceipt, root: Path, db_path: Path) -> _Authority:
    return _Authority(
        _OPERATION,
        os.getpid(),
        receipt.request.attempt_id,
        receipt.receipt_sha256,
        str(_code_root()),
        str(root),
        str(db_path),
        _claim(root, "ir-discovery"),
        _claim(root, "portfolio-db"),
    )


def _assert_authority(
    authority: _Authority, receipt: IssuerDocumentStagingReceipt, root: Path, db_path: Path
) -> None:
    if (
        authority.operation != _OPERATION
        or authority.pid != os.getpid()
        or authority.attempt != receipt.request.attempt_id
        or authority.receipt_sha != receipt.receipt_sha256
        or authority.code_root != str(_code_root())
        or authority.state_root != str(root)
        or authority.database != str(db_path)
    ):
        raise PreparedIssuerDocumentPublisherError("managed_authority_required")
    if (
        _claim(root, "ir-discovery") != authority.ir_claim
        or _claim(root, "portfolio-db") != authority.db_claim
    ):
        raise PreparedIssuerDocumentPublisherError("managed_lock_claim_changed")


def _authority_binding(authority: _Authority) -> str:
    """Commit public binding commitments, never raw process lock credentials."""
    return _sha(
        {
            "code_root": authority.code_root,
            "state_root": authority.state_root,
            "database": authority.database,
            "operation": authority.operation,
        }
    )


def _target(root: Path, item: StagedIssuerDocument) -> Path:
    suffix = ".pdf" if item.media_type == "application/pdf" else ".xlsx"
    target = (
        root
        / "ir_documents"
        / item.ticker
        / item.period_end
        / f"{item.document_type}__{item.sha256[:8]}{suffix}"
    )
    try:
        require_no_reparse_points(target)
        target.resolve(strict=False).relative_to((root / "ir_documents").resolve())
    except (OSError, ValueError, ImmutableArtifactConflictError) as exc:
        raise PreparedIssuerDocumentPublisherError("unsafe_canonical_path") from exc
    return target


def _copy_no_replace(
    source: Path, target: Path, sha: str, size: int
) -> SecureFileOwnershipToken | None:
    try:
        snap, payload = read_stable_artifact(source)
    except (OSError, ImmutableArtifactConflictError) as exc:
        raise PreparedIssuerDocumentPublisherError("staged_object_invalid") from exc
    if snap.file_sha256 != sha or snap.size_bytes != size:
        raise PreparedIssuerDocumentPublisherError("staged_bytes_mismatch")
    try:
        installed = install_bytes_no_clobber(
            target.parent,
            target.name,
            payload,
            expected_sha256=sha,
            expected_size=size,
            read_only=False,
        )
    except SecureFileInstallError as exc:
        code = (
            "canonical_file_conflict"
            if exc.code == "existing_target_conflict"
            else "canonical_target_unsafe"
        )
        ownership = () if exc.ownership is None else (exc.ownership,)
        residues = tuple(str(path) for path in exc.residue_paths)
        raise PreparedIssuerDocumentPublisherError(
            code,
            created=tuple(str(item.path) for item in ownership),
            remaining_paths=residues,
            owned_artifacts=tuple((*[str(item.path) for item in ownership], *residues)),
            owned_identities=ownership,
        ) from exc
    if not installed.created:
        if installed.residue_paths:
            raise PreparedIssuerDocumentPublisherError(
                "canonical_temp_residue",
                remaining_paths=tuple(str(path) for path in installed.residue_paths),
                owned_artifacts=tuple(str(path) for path in installed.residue_paths),
            )
        return None
    if installed.ownership is None:
        raise PreparedIssuerDocumentPublisherError("canonical_target_ownership_missing")
    return installed.ownership


def _preflight_existing_targets(items: list[tuple[StagedIssuerDocument, Path, Path]]) -> None:
    """Reject an unsafe pre-existing canonical target before DB mutation."""
    for item, source, target in items:
        source_snapshot, _payload = read_stable_artifact(source)
        if (
            source_snapshot.file_sha256 != item.sha256
            or source_snapshot.size_bytes != item.byte_size
            or source.stat().st_nlink != 1
        ):
            raise PreparedIssuerDocumentPublisherError("staged_object_invalid")
        if not target.exists():
            continue
        target_snapshot, _target_payload = read_stable_artifact(target)
        if (
            target_snapshot.file_sha256 != item.sha256
            or target_snapshot.size_bytes != item.byte_size
            or target.stat().st_nlink != 1
        ):
            raise PreparedIssuerDocumentPublisherError("canonical_file_conflict")
        if os.path.samefile(source, target):
            raise PreparedIssuerDocumentPublisherError("canonical_staging_alias")


def _expected_row(item: StagedIssuerDocument, root: Path, target: Path) -> tuple[object, ...]:
    return (
        item.ticker,
        "ir_doc",
        item.document_type,
        item.period_end,
        _relative_path(root, target),
        item.sha256,
        item.fetched_at.isoformat(),
        "ok",
        item.byte_size,
        item.source_url,
    )


def _read_intent(
    receipt: IssuerDocumentStagingReceipt, root: Path, authority: _Authority
) -> _PublicationIntent | None:
    path = _intent_path(root, receipt.request.attempt_id)
    if not path.exists():
        return None
    try:
        snap, raw = read_stable_artifact(path)
        intent = _PublicationIntent.model_validate_json(raw)
        require_canonical_text_artifact(snap, intent.model_dump_json())
    except (OSError, ValueError, ImmutableArtifactConflictError) as exc:
        raise PreparedIssuerDocumentPublisherError("publication_intent_invalid") from exc
    if len(receipt.documents) != len(intent.documents):
        raise PreparedIssuerDocumentPublisherError("publication_intent_mismatch")
    try:
        expected = tuple(
            _PublicationIntentItem(
                source_url=item.source_url,
                canonical_path=_relative_path(root, _target(root, item)),
                sha256=item.sha256,
                byte_size=item.byte_size,
                fetched_at=item.fetched_at,
                row_disposition=intent_item.row_disposition,
                file_disposition=intent_item.file_disposition,
                existing_id=intent_item.existing_id,
            )
            for item, intent_item in zip(receipt.documents, intent.documents, strict=True)
        )
    except (OSError, ValueError, ImmutableArtifactConflictError) as exc:
        raise PreparedIssuerDocumentPublisherError("publication_intent_invalid") from exc
    if (
        intent.attempt_id != receipt.request.attempt_id
        or intent.staging_receipt_sha256 != receipt.receipt_sha256
        or intent.document_set_sha256 != _document_set(receipt)
        or intent.classifier_code_sha256 != receipt.classifier_code_sha256
        or intent.verifier_code_sha256 != receipt.verifier_code_sha256
        or intent.configured_database != str(Path(authority.database))
        or intent.binding_sha256 != _authority_binding(authority)
        or intent.documents != expected
    ):
        raise PreparedIssuerDocumentPublisherError("publication_intent_mismatch")
    return intent


def _make_intent(
    receipt: IssuerDocumentStagingReceipt,
    root: Path,
    authority: _Authority,
    items: list[tuple[StagedIssuerDocument, Path, Path]],
    conn: sqlite3.Connection,
) -> _PublicationIntent:
    """Plan all mutations while the writer snapshot excludes competing row changes."""
    plans: list[_PublicationIntentItem] = []
    for item, source, target in items:
        try:
            _preflight_existing_targets([(item, source, target)])
        except (OSError, ImmutableArtifactConflictError) as exc:
            raise PreparedIssuerDocumentPublisherError("canonical_target_unsafe") from exc
        rows = conn.execute(
            "SELECT id,ticker,source_type,doc_type,period_end,file_path,sha256,fetched_at,fetch_status,raw_bytes_size,source_url FROM documents WHERE source_url=?",
            (item.source_url,),
        ).fetchall()
        if len(rows) > 1:
            raise PreparedIssuerDocumentPublisherError("document_source_url_cardinality")
        target_exists = target.exists()
        if rows:
            row = rows[0]
            if tuple(row[1:]) != _expected_row(item, root, target) or not target_exists:
                raise PreparedIssuerDocumentPublisherError("document_row_conflict")
            row_disposition: Literal["insert", "reuse"] = "reuse"
            existing_id: int | None = int(row[0])
        else:
            row_disposition = "insert"
            existing_id = None
        plans.append(
            _PublicationIntentItem(
                source_url=item.source_url,
                canonical_path=_relative_path(root, target),
                sha256=item.sha256,
                byte_size=item.byte_size,
                fetched_at=item.fetched_at,
                row_disposition=row_disposition,
                file_disposition="reuse" if target_exists else "create",
                existing_id=existing_id,
            )
        )
    value = {
        "schema_version": "managed_ir_publication_intent.v1",
        "attempt_id": receipt.request.attempt_id,
        "staging_receipt_sha256": receipt.receipt_sha256,
        "document_set_sha256": _document_set(receipt),
        "classifier_code_sha256": receipt.classifier_code_sha256,
        "verifier_code_sha256": receipt.verifier_code_sha256,
        "configured_database": authority.database,
        "binding_sha256": _authority_binding(authority),
        "documents": [item.model_dump(mode="json") for item in plans],
    }
    return _PublicationIntent.model_validate({**value, "payload_sha256": _sha(value)})


def _seal_intent(intent: _PublicationIntent, root: Path) -> _PublicationIntent:
    try:
        _publish_managed_text(_intent_path(root, intent.attempt_id), intent.model_dump_json())
        snap, raw = read_stable_artifact(_intent_path(root, intent.attempt_id))
        persisted = _PublicationIntent.model_validate_json(raw)
        require_canonical_text_artifact(snap, persisted.model_dump_json())
    except (OSError, ImmutableArtifactConflictError) as exc:
        raise PreparedIssuerDocumentPublisherError("publication_intent_conflict") from exc
    except ValueError as exc:
        raise PreparedIssuerDocumentPublisherError("publication_intent_invalid") from exc
    if persisted != intent:
        raise PreparedIssuerDocumentPublisherError("publication_intent_conflict")
    return persisted


def _publish_managed_text(path: Path, text: str) -> None:
    """Publish canonical JSON only through the shared replacement-safe installer."""
    payload = (text + "\n").encode("utf-8")
    try:
        result = install_bytes_no_clobber(
            path.parent,
            path.name,
            payload,
            expected_sha256=hashlib.sha256(payload).hexdigest(),
            expected_size=len(payload),
        )
    except SecureFileInstallError as exc:
        ownership = () if exc.ownership is None else (exc.ownership,)
        remaining = tuple(
            dict.fromkeys(
                (
                    *[str(item.path) for item in ownership],
                    *[str(item) for item in exc.residue_paths],
                )
            )
        )
        raise PreparedIssuerDocumentPublisherError(
            "managed_artifact_publish_failed",
            created=tuple(str(item.path) for item in ownership),
            remaining_paths=remaining,
            owned_artifacts=remaining,
            owned_identities=ownership,
        ) from exc
    if result.residue_paths:
        raise PreparedIssuerDocumentPublisherError(
            "managed_artifact_residue_retained",
            remaining_paths=tuple(str(item) for item in result.residue_paths),
            owned_artifacts=tuple(str(item) for item in result.residue_paths),
        )


def _resume_intent(
    intent: _PublicationIntent,
    receipt: IssuerDocumentStagingReceipt,
    root: Path,
    items: list[tuple[StagedIssuerDocument, Path, Path]],
    conn: sqlite3.Connection,
) -> None:
    """Reject every post-intent state that could conceal an unrecorded commit."""
    for (item, source, target), planned in zip(items, intent.documents, strict=True):
        try:
            _preflight_existing_targets([(item, source, target)])
        except (OSError, ImmutableArtifactConflictError) as exc:
            raise PreparedIssuerDocumentPublisherError("canonical_target_unsafe") from exc
        rows = conn.execute(
            "SELECT id,ticker,source_type,doc_type,period_end,file_path,sha256,fetched_at,fetch_status,raw_bytes_size,source_url FROM documents WHERE source_url=?",
            (item.source_url,),
        ).fetchall()
        if len(rows) > 1:
            raise PreparedIssuerDocumentPublisherError("document_source_url_cardinality")
        exists = target.exists()
        if planned.row_disposition == "insert":
            if rows or exists:
                raise PreparedIssuerDocumentPublisherError("publication_outcome_ambiguous")
        else:
            if (
                len(rows) != 1
                or int(rows[0][0]) != planned.existing_id
                or tuple(rows[0][1:]) != _expected_row(item, root, target)
                or not exists
            ):
                raise PreparedIssuerDocumentPublisherError("publication_intent_reuse_drift")
        if planned.file_disposition == "reuse" and not exists:
            raise PreparedIssuerDocumentPublisherError("publication_intent_reuse_drift")


def _result(
    receipt: IssuerDocumentStagingReceipt,
    root: Path,
    inserted: list[int],
    reused: list[int],
    canonical: list[Path],
    created: list[Path],
    inventory_path: Path,
    inventory_sha: str,
) -> PreparedIssuerDocumentPublication:
    canonical_paths = tuple(_relative_path(root, p) for p in canonical)
    paths = tuple(_relative_path(root, p) for p in created)
    value = {
        "schema_version": "prepared_issuer_document_publication.v1",
        "committed": True,
        "staging_receipt": receipt.model_dump(mode="json"),
        "receipt_path": _relative_path(
            root, _durable_receipt_path(root, receipt.request.attempt_id)
        ),
        "receipt_sha256": receipt.receipt_sha256,
        "document_set_sha256": _document_set(receipt),
        "inserted_document_ids": tuple(inserted),
        "reused_document_ids": tuple(reused),
        "canonical_paths": canonical_paths,
        "canonical_path_sha256": tuple(_file_sha(p) for p in canonical),
        "created_paths": paths,
        "created_path_sha256": tuple(_file_sha(p) for p in created),
        "inventory_receipt_path": _relative_path(root, inventory_path),
        "inventory_receipt_sha256": inventory_sha,
    }
    return PreparedIssuerDocumentPublication.model_validate({**value, "result_sha256": _sha(value)})


def _canonical_rows(
    receipt: IssuerDocumentStagingReceipt,
    root: Path,
    db_path: Path,
    *,
    conn: sqlite3.Connection | None = None,
) -> tuple[tuple[int, ...], tuple[Path, ...]]:
    """Validate the exact logical/canonical set without publishing evidence."""
    owned_connection = conn is None
    if conn is None:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY, schema_preflight=True)
        conn.execute("BEGIN")
        conn.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone()
    ids: list[int] = []
    paths: list[Path] = []
    try:
        for item in receipt.documents:
            target = _target(root, item)
            stage = (
                root / ".tmp" / "managed_ir_staging" / receipt.request.attempt_id / item.object_path
            )
            try:
                snap, _payload = read_stable_artifact(target)
            except (OSError, ImmutableArtifactConflictError) as exc:
                raise PreparedIssuerDocumentPublisherError(
                    "canonical_file_missing_or_unsafe"
                ) from exc
            if (
                snap.file_sha256 != item.sha256
                or snap.size_bytes != item.byte_size
                or target.stat().st_nlink != 1
            ):
                raise PreparedIssuerDocumentPublisherError("canonical_file_drift")
            if stage.exists():
                try:
                    if os.path.samefile(stage, target):
                        raise PreparedIssuerDocumentPublisherError("canonical_staging_alias")
                except OSError as exc:
                    raise PreparedIssuerDocumentPublisherError(
                        "canonical_alias_check_failed"
                    ) from exc
            row = conn.execute(
                "SELECT id,ticker,source_type,doc_type,period_end,file_path,sha256,fetched_at,fetch_status,raw_bytes_size,source_url FROM documents WHERE source_url=?",
                (item.source_url,),
            ).fetchall()
            expected = (
                item.ticker,
                "ir_doc",
                item.document_type,
                item.period_end,
                _relative_path(root, target),
                item.sha256,
                item.fetched_at.isoformat(),
                "ok",
                item.byte_size,
                item.source_url,
            )
            if len(row) != 1 or tuple(row[0][1:]) != expected:
                raise PreparedIssuerDocumentPublisherError("canonical_row_drift")
            ids.append(int(row[0][0]))
            paths.append(target)
    finally:
        if owned_connection:
            if conn.in_transaction:
                conn.rollback()
            conn.close()
    return tuple(ids), tuple(paths)


def _published_inventory(
    receipt: IssuerDocumentStagingReceipt,
    root: Path,
    db_path: Path,
    authority: _Authority,
    durable: _CommittedPublicationRecord,
) -> tuple[Path, str, tuple[int, ...], tuple[Path, ...]]:
    """Seal/read back a physical committed-DB inventory under the held claims."""
    inventory_path = _inventory_path(root, receipt.request.attempt_id)
    _assert_authority(authority, receipt, root, db_path)
    evidence = _read_inventory_evidence(receipt, db_path, authority, durable)
    if inventory_path.exists() and evidence is not None:
        row_ids, canonical_paths = _canonical_rows(receipt, root, db_path)
        inventory_sha = _read_persisted_inventory(receipt, root, inventory_path)
        if inventory_sha != evidence.inventory_receipt_sha256:
            raise PreparedIssuerDocumentPublisherError("inventory_evidence_mismatch")
        return inventory_path, inventory_sha, row_ids, canonical_paths
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY, schema_preflight=True)
    try:
        conn.execute("BEGIN")
        conn.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone()
        row_ids, canonical_paths = _canonical_rows(receipt, root, db_path, conn=conn)
        inventory = build_issuer_document_inventory(
            conn,
            database_path=db_path,
            repo_root=root,
            request=receipt.request.inventory_request,
            transaction_open=True,
        )
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.close()
    if inventory_path.exists():
        # An evidence-less artifact is a crash gap, never a reusable cache.  It
        # must be the exact receipt from this one current committed snapshot.
        try:
            _snap, raw = read_stable_artifact(inventory_path)
            persisted = IssuerDocumentInventoryReceipt.model_validate_json(raw)
            require_canonical_text_artifact(_snap, persisted.canonical_json)
        except (OSError, ValueError, ImmutableArtifactConflictError) as exc:
            raise PreparedIssuerDocumentPublisherError("inventory_evidence_ambiguous") from exc
        if persisted != inventory:
            raise PreparedIssuerDocumentPublisherError("inventory_evidence_ambiguous")
    else:
        try:
            _publish_managed_text(inventory_path, inventory.canonical_json)
            _snap, raw = read_stable_artifact(inventory_path)
            persisted = IssuerDocumentInventoryReceipt.model_validate_json(raw)
            require_canonical_text_artifact(_snap, persisted.canonical_json)
        except (OSError, ValueError, ImmutableArtifactConflictError) as exc:
            raise PreparedIssuerDocumentPublisherError("inventory_receipt_invalid") from exc
        if persisted != inventory:
            raise PreparedIssuerDocumentPublisherError("inventory_receipt_drift")
    inventory_sha = _read_persisted_inventory(receipt, root, inventory_path)
    _assert_authority(authority, receipt, root, db_path)
    return inventory_path, inventory_sha, row_ids, canonical_paths


def _read_persisted_inventory(
    receipt: IssuerDocumentStagingReceipt, root: Path, inventory_path: Path
) -> str:
    """Validate an original sealed inventory without rebuilding it from later DB state."""
    try:
        snap, raw = read_stable_artifact(inventory_path)
        persisted = IssuerDocumentInventoryReceipt.model_validate_json(raw)
        require_canonical_text_artifact(snap, persisted.canonical_json)
    except (OSError, ValueError, ImmutableArtifactConflictError) as exc:
        raise PreparedIssuerDocumentPublisherError("inventory_receipt_invalid") from exc
    expected = {
        (
            item.source_url,
            item.document_type,
            _relative_path(root, _target(root, item)),
            item.sha256,
            item.byte_size,
        )
        for item in receipt.documents
    }
    actual = {
        (
            record.source_url,
            record.document_type,
            record.local_path,
            record.sha256,
            record.byte_size,
        )
        for record in persisted.records
    }
    if persisted.request != receipt.request.inventory_request or actual != expected:
        raise PreparedIssuerDocumentPublisherError("inventory_receipt_drift")
    return persisted.receipt_sha256


def _read_inventory_evidence(
    receipt: IssuerDocumentStagingReceipt,
    db_path: Path,
    authority: _Authority,
    durable: _CommittedPublicationRecord,
) -> _InventoryEvidence | None:
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY, schema_preflight=True)
    try:
        row = conn.execute(
            "SELECT attempt_id,publication_payload_sha256,inventory_receipt_path,inventory_receipt_sha256,binding_sha256,payload_sha256 FROM managed_ir_inventory_evidence WHERE attempt_id=?",
            (receipt.request.attempt_id,),
        ).fetchone()
    except sqlite3.Error as exc:
        raise PreparedIssuerDocumentPublisherError("inventory_evidence_schema_missing") from exc
    finally:
        conn.close()
    if row is None:
        return None
    try:
        evidence = _InventoryEvidence.model_validate(
            {
                "attempt_id": str(row[0]),
                "publication_payload_sha256": str(row[1]),
                "inventory_receipt_path": str(row[2]),
                "inventory_receipt_sha256": str(row[3]),
                "binding_sha256": str(row[4]),
                "payload_sha256": str(row[5]),
            }
        )
    except (TypeError, ValueError) as exc:
        raise PreparedIssuerDocumentPublisherError("inventory_evidence_invalid") from exc
    if (
        evidence.attempt_id != receipt.request.attempt_id
        or evidence.publication_payload_sha256 != durable.payload_sha256
        or evidence.inventory_receipt_path != durable.inventory_receipt_path
        or evidence.binding_sha256 != _authority_binding(authority)
    ):
        raise PreparedIssuerDocumentPublisherError("inventory_evidence_mismatch")
    return evidence


def _seal_inventory_evidence(
    receipt: IssuerDocumentStagingReceipt,
    db_path: Path,
    authority: _Authority,
    durable: _CommittedPublicationRecord,
    inventory_path: Path,
    inventory_sha: str,
) -> _InventoryEvidence:
    existing = _read_inventory_evidence(receipt, db_path, authority, durable)
    if existing is not None:
        if existing.inventory_receipt_sha256 != inventory_sha:
            raise PreparedIssuerDocumentPublisherError("inventory_evidence_mismatch")
        return existing
    unsigned = {
        "attempt_id": receipt.request.attempt_id,
        "publication_payload_sha256": durable.payload_sha256,
        "inventory_receipt_path": _relative_path(Path(authority.state_root), inventory_path),
        "inventory_receipt_sha256": inventory_sha,
        "binding_sha256": _authority_binding(authority),
    }
    evidence = _InventoryEvidence.model_validate({**unsigned, "payload_sha256": _sha(unsigned)})
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _assert_authority(authority, receipt, Path(authority.state_root), db_path)
        conn.execute(
            "INSERT INTO managed_ir_inventory_evidence (attempt_id,publication_payload_sha256,inventory_receipt_path,inventory_receipt_sha256,binding_sha256,payload_sha256) VALUES (?,?,?,?,?,?)",
            (
                evidence.attempt_id,
                evidence.publication_payload_sha256,
                evidence.inventory_receipt_path,
                evidence.inventory_receipt_sha256,
                evidence.binding_sha256,
                evidence.payload_sha256,
            ),
        )
        conn.commit()
    except sqlite3.Error as exc:
        if conn.in_transaction:
            conn.rollback()
        raise PreparedIssuerDocumentPublisherError("inventory_evidence_publish_failed") from exc
    finally:
        conn.close()
    persisted = _read_inventory_evidence(receipt, db_path, authority, durable)
    if persisted != evidence:
        raise PreparedIssuerDocumentPublisherError("inventory_evidence_mismatch")
    return evidence


def _finalize_committed(
    receipt: IssuerDocumentStagingReceipt,
    root: Path,
    db_path: Path,
    authority: _Authority,
    inserted: tuple[int, ...],
    reused: tuple[int, ...],
    created: tuple[Path, ...],
) -> PreparedIssuerDocumentPublication:
    """Finish only post-commit evidence; every caller maps failures to partial."""
    try:
        durable = _durable_recovery(receipt, db_path, authority)
        if durable is None:
            raise PreparedIssuerDocumentPublisherError("publication_record_missing")
        receipt_path = _durable_receipt_path(root, receipt.request.attempt_id)
        _publish_managed_text(receipt_path, receipt.canonical_json)
        receipt_snap, receipt_raw = read_stable_artifact(receipt_path)
        persisted_receipt = IssuerDocumentStagingReceipt.model_validate_json(receipt_raw)
        require_canonical_text_artifact(receipt_snap, persisted_receipt.canonical_json)
        if (
            persisted_receipt != receipt
            or _relative_path(root, receipt_path) != durable.staging_receipt_path
        ):
            raise PreparedIssuerDocumentPublisherError("staging_receipt_drift")
        inventory_path, inventory_sha, row_ids, canonical_paths = _published_inventory(
            receipt, root, db_path, authority, durable
        )
        _seal_inventory_evidence(
            receipt, db_path, authority, durable, inventory_path, inventory_sha
        )
    except Exception as exc:
        raise PreparedIssuerDocumentPublisherError(
            "publication_committed_partial",
            committed=True,
            inserted=inserted,
            reused=reused,
            created=tuple(map(str, created)),
            inventory_state="failed",
            result_state="not_started",
        ) from exc
    # A recovery has no original create disposition; bind all reconciled paths
    # rather than concealing durable state.
    try:
        result = _result(
            receipt,
            root,
            list(inserted),
            list(reused),
            list(canonical_paths),
            list(created),
            inventory_path,
            inventory_sha,
        )
        result_path = _result_path(root, receipt.request.attempt_id)
        _assert_authority(authority, receipt, root, db_path)
        _publish_managed_text(result_path, result.model_dump_json())
        snap, raw = read_stable_artifact(result_path)
        persisted = PreparedIssuerDocumentPublication.model_validate_json(raw)
        require_canonical_text_artifact(snap, persisted.model_dump_json())
        if persisted != result or tuple(sorted((*inserted, *reused))) != tuple(sorted(row_ids)):
            raise PreparedIssuerDocumentPublisherError("publication_result_drift")
        _assert_authority(authority, receipt, root, db_path)
    except Exception as exc:
        raise PreparedIssuerDocumentPublisherError(
            "publication_committed_partial",
            committed=True,
            inserted=inserted,
            reused=reused,
            created=tuple(map(str, created)),
            inventory_state="published",
            result_state="failed",
        ) from exc
    return persisted


def _replay_result(
    receipt: IssuerDocumentStagingReceipt, root: Path, db_path: Path, authority: _Authority
) -> PreparedIssuerDocumentPublication | None:
    path = _result_path(root, receipt.request.attempt_id)
    if not path.exists():
        return None
    try:
        snap, raw = read_stable_artifact(path)
        result = PreparedIssuerDocumentPublication.model_validate_json(raw)
        require_canonical_text_artifact(snap, result.model_dump_json())
    except (OSError, ValueError, ImmutableArtifactConflictError) as exc:
        raise PreparedIssuerDocumentPublisherError("publication_result_invalid") from exc
    durable = _durable_recovery(receipt, db_path, authority)
    if durable is None:
        raise PreparedIssuerDocumentPublisherError("publication_record_missing")
    if (
        result.inserted_document_ids != durable.inserted_ids
        or result.reused_document_ids != durable.reused_ids
        or result.created_paths != durable.created_paths
    ):
        raise PreparedIssuerDocumentPublisherError("publication_record_mismatch")
    ids, paths = _canonical_rows(receipt, root, db_path)
    if (
        result.staging_receipt != receipt
        or result.receipt_sha256 != receipt.receipt_sha256
        or tuple(sorted((*result.inserted_document_ids, *result.reused_document_ids)))
        != tuple(sorted(ids))
    ):
        raise PreparedIssuerDocumentPublisherError("publication_result_mismatch")
    for path_value, path_sha in zip(
        result.canonical_paths, result.canonical_path_sha256, strict=True
    ):
        target = root / path_value
        try:
            path_matches = target in paths and _file_sha(target) == path_sha
        except OSError as exc:
            raise PreparedIssuerDocumentPublisherError("publication_result_path_drift") from exc
        if not path_matches:
            raise PreparedIssuerDocumentPublisherError("publication_result_path_drift")
    inventory_path = _inventory_path(root, receipt.request.attempt_id)
    inventory_sha = _read_persisted_inventory(receipt, root, inventory_path)
    evidence = _read_inventory_evidence(receipt, db_path, authority, durable)
    if (
        _relative_path(root, inventory_path) != result.inventory_receipt_path
        or durable.inventory_receipt_path != result.inventory_receipt_path
        or durable.publication_result_path != _relative_path(root, path)
        or inventory_sha != result.inventory_receipt_sha256
        or evidence is None
        or evidence.inventory_receipt_path != result.inventory_receipt_path
        or evidence.inventory_receipt_sha256 != inventory_sha
    ):
        raise PreparedIssuerDocumentPublisherError("inventory_receipt_drift")
    return result


def _durable_recovery(
    receipt: IssuerDocumentStagingReceipt, db_path: Path, authority: _Authority
) -> _CommittedPublicationRecord | None:
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY, schema_preflight=True)
    try:
        row = conn.execute(
            "SELECT attempt_id,staging_receipt_sha256,document_set_sha256,inserted_ids_json,reused_ids_json,canonical_paths_json,created_paths_json,staging_receipt_path,inventory_receipt_path,publication_result_path,intent_sha256,binding_sha256,committed_at,state,payload_sha256 FROM managed_ir_publications WHERE attempt_id=?",
            (receipt.request.attempt_id,),
        ).fetchone()
    except sqlite3.Error as exc:
        raise PreparedIssuerDocumentPublisherError("publication_record_schema_missing") from exc
    finally:
        conn.close()
    if row is None:
        return None
    try:
        record = _CommittedPublicationRecord.model_validate(
            {
                "attempt_id": str(row[0]),
                "staging_receipt_sha256": str(row[1]),
                "document_set_sha256": str(row[2]),
                "inserted_ids": json.loads(str(row[3])),
                "reused_ids": json.loads(str(row[4])),
                "canonical_paths": json.loads(str(row[5])),
                "created_paths": json.loads(str(row[6])),
                "staging_receipt_path": str(row[7]),
                "inventory_receipt_path": str(row[8]),
                "publication_result_path": str(row[9]),
                "intent_sha256": str(row[10]),
                "binding_sha256": str(row[11]),
                "committed_at": str(row[12]),
                "state": str(row[13]),
                "payload_sha256": str(row[14]),
            }
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PreparedIssuerDocumentPublisherError("publication_record_invalid") from exc
    expected_paths = tuple(
        _CanonicalPublicationPath(
            path=_relative_path(
                Path(authority.state_root), _target(Path(authority.state_root), item)
            ),
            sha256=item.sha256,
            byte_size=item.byte_size,
        )
        for item in receipt.documents
    )
    if (
        record.attempt_id != receipt.request.attempt_id
        or record.staging_receipt_sha256 != receipt.receipt_sha256
        or record.document_set_sha256 != _document_set(receipt)
        or record.canonical_paths != expected_paths
        or record.binding_sha256 != _authority_binding(authority)
    ):
        raise PreparedIssuerDocumentPublisherError("publication_record_mismatch")
    return record


def _publish_prepared_issuer_documents_impl(
    request: IssuerDocumentStagingRequest,
    *,
    state_root: Path,
    db_path: Path,
    held_claims: bool,
) -> PreparedIssuerDocumentPublication:
    """Implementation reached only by the standalone or verified held-lock seams."""
    root = state_root.resolve(strict=True)
    db_path = _configured(root, db_path)
    lock = (
        nullcontext()
        if held_claims
        else JobLock(
            root,
            "managed-issuer-document-publication",
            ["ir-discovery", "portfolio-db"],
            wait_s=0,
        )
    )
    with lock:
        if _result_path(root, request.attempt_id).exists():
            receipt = _read_completed_receipt(root, request, db_path)
            authority = _issue(receipt, root, db_path)
            _assert_authority(authority, receipt, root, db_path)
            replay = _replay_result(receipt, root, db_path, authority)
            if replay is None:
                raise PreparedIssuerDocumentPublisherError("publication_result_missing")
            return replay
        receipt = validate_prepared_staging(request, state_root=root, db_path=db_path)
        authority = _issue(receipt, root, db_path)
        _assert_authority(authority, receipt, root, db_path)
        intent = _read_intent(receipt, root, authority)
        replay = _replay_result(receipt, root, db_path, authority)
        if replay is not None:
            return replay
        durable = _durable_recovery(receipt, db_path, authority)
        if durable is not None:
            if intent is None or durable.intent_sha256 != intent.payload_sha256:
                raise PreparedIssuerDocumentPublisherError("publication_intent_missing")
            try:
                return _finalize_committed(
                    receipt,
                    root,
                    db_path,
                    authority,
                    durable.inserted_ids,
                    durable.reused_ids,
                    tuple(root / path for path in durable.created_paths),
                )
            except PreparedIssuerDocumentPublisherError as exc:
                if exc.committed:
                    raise
                raise PreparedIssuerDocumentPublisherError(
                    "publication_committed_partial",
                    committed=True,
                    inserted=durable.inserted_ids,
                    reused=durable.reused_ids,
                    created=durable.created_paths,
                    inventory_state="failed",
                    result_state="not_started",
                ) from exc
            except Exception as exc:
                raise PreparedIssuerDocumentPublisherError(
                    "publication_committed_partial",
                    committed=True,
                    inserted=durable.inserted_ids,
                    reused=durable.reused_ids,
                    created=durable.created_paths,
                    inventory_state="failed",
                    result_state="not_started",
                ) from exc
        staged = root / ".tmp" / "managed_ir_staging" / request.attempt_id
        items = [
            (item, staged / item.object_path, _target(root, item)) for item in receipt.documents
        ]
        inserted: list[int] = []
        reused: list[int] = []
        created: list[SecureFileOwnershipToken] = []
        committed = False
        commit_attempted = False
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
        try:
            conn.execute("BEGIN IMMEDIATE")
            if intent is None:
                intent = _seal_intent(_make_intent(receipt, root, authority, items, conn), root)
            else:
                _resume_intent(intent, receipt, root, items, conn)
            for (item, source, target), planned in zip(items, intent.documents, strict=True):
                _assert_authority(authority, receipt, root, db_path)
                if planned.file_disposition == "create":
                    owned_target = _copy_no_replace(source, target, item.sha256, item.byte_size)
                    if owned_target is None:
                        raise PreparedIssuerDocumentPublisherError("publication_outcome_ambiguous")
                    created.append(owned_target)
                if planned.row_disposition == "insert":
                    cur = conn.execute(
                        "INSERT INTO documents (ticker,source_type,doc_type,period_end,file_path,sha256,fetched_at,fetch_status,raw_bytes_size,source_url) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        _expected_row(item, root, target),
                    )
                    if cur.lastrowid is None:
                        raise PreparedIssuerDocumentPublisherError("document_insert_missing_id")
                    inserted.append(int(cur.lastrowid))
                else:
                    if planned.existing_id is None:
                        raise PreparedIssuerDocumentPublisherError("publication_intent_reuse_drift")
                    reused.append(planned.existing_id)
            # This is deliberately logical-only. A physical DB-bound inventory
            # receipt belongs to the committed snapshot below, never this tx.
            _preflight_existing_targets(items)
            _assert_authority(authority, receipt, root, db_path)
            durable_value = {
                "attempt_id": receipt.request.attempt_id,
                "staging_receipt_sha256": receipt.receipt_sha256,
                "document_set_sha256": _document_set(receipt),
                "inserted_ids": tuple(inserted),
                "reused_ids": tuple(reused),
                "canonical_paths": tuple(
                    {
                        "path": _relative_path(root, target),
                        "sha256": item.sha256,
                        "byte_size": item.byte_size,
                    }
                    for item, _source, target in items
                ),
                "created_paths": tuple(_relative_path(root, artifact.path) for artifact in created),
                "staging_receipt_path": _relative_path(
                    root, _durable_receipt_path(root, receipt.request.attempt_id)
                ),
                "inventory_receipt_path": _relative_path(
                    root, _inventory_path(root, receipt.request.attempt_id)
                ),
                "publication_result_path": _relative_path(
                    root, _result_path(root, receipt.request.attempt_id)
                ),
                "intent_sha256": intent.payload_sha256,
                "binding_sha256": _authority_binding(authority),
                "committed_at": datetime.now(UTC).isoformat(),
                "state": "committed",
            }
            durable_record = _sealed_publication_record(durable_value)
            conn.execute(
                "INSERT INTO managed_ir_publications (attempt_id,staging_receipt_sha256,document_set_sha256,inserted_ids_json,reused_ids_json,canonical_paths_json,created_paths_json,staging_receipt_path,inventory_receipt_path,publication_result_path,intent_sha256,binding_sha256,committed_at,state,payload_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    durable_record.attempt_id,
                    durable_record.staging_receipt_sha256,
                    durable_record.document_set_sha256,
                    _json(durable_record.inserted_ids),
                    _json(durable_record.reused_ids),
                    _json(
                        [item.model_dump(mode="json") for item in durable_record.canonical_paths]
                    ),
                    _json(durable_record.created_paths),
                    durable_record.staging_receipt_path,
                    durable_record.inventory_receipt_path,
                    durable_record.publication_result_path,
                    durable_record.intent_sha256,
                    durable_record.binding_sha256,
                    durable_record.committed_at.isoformat(),
                    durable_record.state,
                    durable_record.payload_sha256,
                ),
            )
            _assert_authority(authority, receipt, root, db_path)
            commit_attempted = True
            conn.commit()
            committed = True
        except Exception as exc:
            if not committed and not commit_attempted:
                conn.rollback()
                owned_artifacts: list[SecureFileOwnershipToken] = list(created)
                if isinstance(exc, PreparedIssuerDocumentPublisherError):
                    for artifact in exc.owned_identities:
                        if artifact not in created:
                            created.append(artifact)
                        if artifact not in owned_artifacts:
                            owned_artifacts.append(artifact)
                removed, remaining = _cleanup_owned_artifacts(owned_artifacts)
                if isinstance(exc, PreparedIssuerDocumentPublisherError):
                    remaining = tuple(dict.fromkeys((*remaining, *exc.remaining_paths)))
                cleanup_failed = bool(remaining)
                code = (
                    "publication_cleanup_partial"
                    if cleanup_failed
                    else (
                        exc.code
                        if isinstance(exc, PreparedIssuerDocumentPublisherError)
                        else "publication_failed"
                    )
                )
                raise PreparedIssuerDocumentPublisherError(
                    code,
                    inserted=tuple(inserted),
                    reused=tuple(reused),
                    created=tuple(str(artifact.path) for artifact in created),
                    removed_paths=removed,
                    remaining_paths=remaining,
                    owned_artifacts=tuple(
                        dict.fromkeys(
                            (
                                *(str(artifact.path) for artifact in owned_artifacts),
                                *(
                                    exc.owned_artifacts
                                    if isinstance(exc, PreparedIssuerDocumentPublisherError)
                                    else ()
                                ),
                            )
                        )
                    ),
                    owned_identities=tuple(owned_artifacts),
                ) from exc
            if not committed:
                # SQLite can durably commit before a wrapper/driver raises while
                # acknowledging it. Never roll back or delete canonical files
                # after that point: reconcile through a fresh read-only handle.
                try:
                    durable = _durable_recovery(receipt, db_path, authority)
                    if durable is None:
                        raise PreparedIssuerDocumentPublisherError(
                            "publication_commit_outcome_unknown", committed=True
                        )
                    row_ids, canonical_paths = _canonical_rows(receipt, root, db_path)
                    if tuple(sorted(row_ids)) != tuple(
                        sorted((*durable.inserted_ids, *durable.reused_ids))
                    ) or tuple(_relative_path(root, path) for path in canonical_paths) != tuple(
                        entry.path for entry in durable.canonical_paths
                    ):
                        raise PreparedIssuerDocumentPublisherError(
                            "publication_commit_outcome_unknown", committed=True
                        )
                except Exception as reconciliation_error:
                    raise PreparedIssuerDocumentPublisherError(
                        "publication_commit_outcome_unknown", committed=True
                    ) from reconciliation_error
                committed = True
            else:
                raise PreparedIssuerDocumentPublisherError(
                    "publication_committed_partial",
                    committed=True,
                    inserted=tuple(inserted),
                    reused=tuple(reused),
                    created=tuple(str(artifact.path) for artifact in created),
                ) from exc
        finally:
            conn.close()
        try:
            return _finalize_committed(
                receipt,
                root,
                db_path,
                authority,
                tuple(inserted),
                tuple(reused),
                tuple(artifact.path for artifact in created),
            )
        except Exception as exc:
            if isinstance(exc, PreparedIssuerDocumentPublisherError) and exc.committed:
                raise
            raise PreparedIssuerDocumentPublisherError(
                "publication_committed_partial",
                committed=True,
                inserted=tuple(inserted),
                reused=tuple(reused),
                created=tuple(str(artifact.path) for artifact in created),
                inventory_state="partial",
                result_state="partial",
            ) from exc


def _publish_prepared_issuer_documents_held(
    request: IssuerDocumentStagingRequest, *, state_root: Path, db_path: Path
) -> PreparedIssuerDocumentPublication:
    """Private outer-admission seam; it borrows, never reacquires, exact claims."""
    root = state_root.resolve(strict=True)
    configured = _configured(root, db_path)
    # These claims are process-local ContextVar-backed live ownership proofs.
    # Merely creating similarly named lock files cannot enter this seam.
    _claim(root, "ir-discovery")
    _claim(root, "portfolio-db")
    return _publish_prepared_issuer_documents_impl(
        request, state_root=root, db_path=configured, held_claims=True
    )


def publish_prepared_issuer_documents(
    request: IssuerDocumentStagingRequest, *, state_root: Path, db_path: Path
) -> PreparedIssuerDocumentPublication:
    """Standalone publisher: acquire exact lanes then delegate to the held seam."""
    root = state_root.resolve(strict=True)
    configured = _configured(root, db_path)
    with JobLock(
        root, "managed-issuer-document-publication", ["ir-discovery", "portfolio-db"], wait_s=0
    ):
        return _publish_prepared_issuer_documents_held(request, state_root=root, db_path=configured)
