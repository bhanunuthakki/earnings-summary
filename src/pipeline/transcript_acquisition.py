"""DB-bound transcript authorization and immutable artifact handoff.

This is the only module that projects stored company identity into the pure
BHA-29 transcript authorization contract.  Provider callers receive a typed
receipt before external work; persistence callers consume only immutable
staged bytes after revalidating both the receipt and stored identity.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models.documents import DocType, SourceType
from pipeline.source_policy import (
    ArtifactKind,
    AuthorizationReason,
    CollectionSource,
    StoredIdentityStatus,
    authorize_collection_target_in_connection,
    decision_for,
    reported_quarter_is_in_window,
)
from provenance.source_regime import SourceRegime, contract_for, receipt_identity
from transcripts.acquisition_semantics import (
    TRANSCRIPT_ACQUISITION_POLICY_VERSION,
    ExistingArtifactBehavior,
    StoredTargetStatus,
    TranscriptAcquisitionAuthorization,
    TranscriptAcquisitionEntrypoint,
    TranscriptAcquisitionRequest,
    TranscriptAuthorizationStatus,
    TranscriptProvider,
    TranscriptReportingStatus,
    TranscriptStoredTarget,
    authorize_transcript_acquisition_request,
    validate_transcript_acquisition_authorization,
)
from transcripts.immutable_staging import (
    StagedTranscriptArtifact,
    read_staged_transcript,
    stage_transcript_artifact,
)

_STRICT_FROZEN = ConfigDict(
    extra="forbid",
    frozen=True,
    strict=True,
    revalidate_instances="always",
)
COMBINED_SOURCE_REGIME_IDENTITY = receipt_identity(contract_for(SourceRegime.COMBINED))
MAX_TRANSCRIPT_BYTES = 32 * 1024 * 1024


class TranscriptAcquisitionDeniedError(PermissionError):
    """The canonical transcript contract denied work before side effects."""


class AuthorizedTranscriptArtifact(BaseModel):
    """Exact authorized provenance bound to one immutable staged snapshot."""

    model_config = _STRICT_FROZEN

    schema_version: Literal["authorized-transcript-artifact@1"] = "authorized-transcript-artifact@1"
    authorization: TranscriptAcquisitionAuthorization
    document_id: int | None = Field(default=None, ge=1)
    source_url: str | None
    source_path: Path
    staged: StagedTranscriptArtifact

    @model_validator(mode="after")
    def _exact_binding(self) -> Self:
        authorization = validate_transcript_acquisition_authorization(self.authorization)
        if authorization.status is not TranscriptAuthorizationStatus.AUTHORIZED:
            raise ValueError("artifact requires an authorized acquisition receipt")
        if self.source_path != self.staged.source_path:
            raise ValueError("artifact source path does not match staged provenance")
        return self

    @property
    def sha256(self) -> str:
        return self.staged.sha256

    @property
    def size_bytes(self) -> int:
        return self.staged.size_bytes


def _stored_target_for_request(
    conn: sqlite3.Connection,
    request: TranscriptAcquisitionRequest,
) -> TranscriptStoredTarget:
    stored = authorize_collection_target_in_connection(
        conn,
        request.canonical_ticker,
        requested=request.owner_requested,
        source=CollectionSource.TRANSCRIPT,
        artifact_kind=(
            ArtifactKind.WEBCAST
            if request.source_type is SourceType.TRANSCRIPT_AUDIO
            else ArtifactKind.TEXT_TRANSCRIPT
        ),
    )
    coverage_role = stored.target.coverage_role if stored.target is not None else None
    identity_authorized = stored.status is StoredIdentityStatus.AUTHORIZED or (
        stored.status is StoredIdentityStatus.POLICY_DENIED
        and stored.decision is not None
        and stored.decision.reason is AuthorizationReason.WEBCAST_EXCLUDED
        and coverage_role is not None
        and decision_for(
            coverage_role,
            CollectionSource.TRANSCRIPT,
            ArtifactKind.TEXT_TRANSCRIPT,
            requested=request.owner_requested,
        ).allowed
    )
    if identity_authorized:
        status = StoredTargetStatus.AUTHORIZED
        if stored.fiscal_year_end_month is None:
            reporting = TranscriptReportingStatus.FISCAL_CALENDAR_UNAVAILABLE
        elif reported_quarter_is_in_window(
            fiscal_year=request.fiscal_year,
            fiscal_quarter=request.fiscal_quarter,
            fiscal_year_end_month=stored.fiscal_year_end_month,
            as_of=request.as_of,
        ):
            reporting = TranscriptReportingStatus.ELIGIBLE
        else:
            reporting = TranscriptReportingStatus.OUT_OF_WINDOW
    else:
        status = StoredTargetStatus(stored.status.value)
        reporting = TranscriptReportingStatus.NOT_EVALUATED
    return TranscriptStoredTarget(
        canonical_ticker=request.canonical_ticker,
        fiscal_year=request.fiscal_year,
        fiscal_quarter=request.fiscal_quarter,
        as_of=request.as_of,
        owner_requested=request.owner_requested,
        coverage_role=coverage_role,
        fiscal_year_end_month=stored.fiscal_year_end_month,
        source_policy_version=request.source_policy_version,
        source_regime_identity=request.source_regime_identity,
        status=status,
        reporting_status=reporting,
    )


def authorize_transcript_request(
    conn: sqlite3.Connection,
    request: TranscriptAcquisitionRequest,
) -> TranscriptAcquisitionAuthorization:
    """Project stored identity into the pure canonical contract without writes."""

    receipt = authorize_transcript_acquisition_request(
        request,
        _stored_target_for_request(conn, request),
    )
    return validate_transcript_acquisition_authorization(receipt)


def require_authorized_transcript_request(
    conn: sqlite3.Connection,
    request: TranscriptAcquisitionRequest,
) -> TranscriptAcquisitionAuthorization:
    receipt = authorize_transcript_request(conn, request)
    if receipt.status is not TranscriptAuthorizationStatus.AUTHORIZED:
        raise TranscriptAcquisitionDeniedError(
            f"transcript acquisition denied: {receipt.reason.value} ({receipt.idempotency_key})"
        )
    return receipt


def _stage_authorized_file(
    authorization: TranscriptAcquisitionAuthorization,
    *,
    source_path: Path,
    private_root: Path,
    expected_sha256: str,
    expected_size_bytes: int,
    source_url: str | None,
    document_id: int | None,
) -> AuthorizedTranscriptArtifact:
    validated = validate_transcript_acquisition_authorization(authorization)
    if validated.status is not TranscriptAuthorizationStatus.AUTHORIZED:
        raise TranscriptAcquisitionDeniedError("cannot stage a denied transcript acquisition")
    staged = stage_transcript_artifact(
        source_path,
        private_root,
        expected_sha256=expected_sha256,
        expected_size_bytes=expected_size_bytes,
        max_bytes=MAX_TRANSCRIPT_BYTES,
    )
    return AuthorizedTranscriptArtifact(
        authorization=validated,
        document_id=document_id,
        source_url=source_url,
        source_path=staged.source_path,
        staged=staged,
    )


def stage_authorized_payload(
    authorization: TranscriptAcquisitionAuthorization,
    *,
    payload: bytes,
    private_root: Path,
    source_url: str | None,
) -> AuthorizedTranscriptArtifact:
    """Stage acquired response bytes only after the exact provider is authorized."""

    validated = validate_transcript_acquisition_authorization(authorization)
    if validated.status is not TranscriptAuthorizationStatus.AUTHORIZED:
        raise TranscriptAcquisitionDeniedError("cannot stage a denied transcript acquisition")
    if len(payload) > MAX_TRANSCRIPT_BYTES:
        raise ValueError("transcript payload exceeds the bounded staging limit")
    private_root.mkdir(parents=True, exist_ok=True)
    descriptor, raw_name = tempfile.mkstemp(
        prefix="authorized-source-",
        suffix=".transcript",
        dir=private_root.parent,
    )
    source_path = Path(raw_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        return _stage_authorized_file(
            validated,
            source_path=source_path,
            private_root=private_root,
            expected_sha256=hashlib.sha256(payload).hexdigest(),
            expected_size_bytes=len(payload),
            source_url=source_url,
            document_id=None,
        )
    finally:
        source_path.unlink(missing_ok=True)


def read_authorized_transcript(
    conn: sqlite3.Connection,
    artifact: AuthorizedTranscriptArtifact,
    *,
    project_root: Path,
) -> bytes:
    """Revalidate identity and consume only the immutable staged snapshot."""

    del project_root  # reserved for persisted-path containment validation below
    validated = AuthorizedTranscriptArtifact.model_validate(artifact, strict=True)
    current = authorize_transcript_request(conn, validated.authorization.request)
    if current != validated.authorization:
        raise TranscriptAcquisitionDeniedError(
            "transcript authorization no longer matches stored policy"
        )
    staged = validated.staged
    return read_staged_transcript(
        staged,
        trusted_staging_root=staged.staging_root,
        trusted_staging_root_device=staged.staging_root_device,
        trusted_staging_root_inode=staged.staging_root_inode,
        expected_source_path=staged.source_path,
        expected_source_device=staged.source_device,
        expected_source_inode=staged.source_inode,
        expected_sha256=staged.sha256,
        expected_size_bytes=staged.size_bytes,
    )


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _durable_artifact_json(artifact: AuthorizedTranscriptArtifact) -> str:
    return _canonical_json(artifact.model_dump(mode="json"))


def _receipt_id(artifact_json: str) -> str:
    return hashlib.sha256(artifact_json.encode("utf-8")).hexdigest()


def persist_authorized_transcript_artifact(
    conn: sqlite3.Connection,
    artifact: AuthorizedTranscriptArtifact,
    *,
    project_root: Path,
) -> str:
    """Append or exactly replay one durable artifact receipt."""

    validated = AuthorizedTranscriptArtifact.model_validate(artifact, strict=True)
    payload = read_authorized_transcript(conn, validated, project_root=project_root)
    if hashlib.sha256(payload).hexdigest() != validated.sha256:
        raise TranscriptAcquisitionDeniedError("staged transcript digest changed before receipt")
    authorization_json = _canonical_json(validated.authorization.model_dump(mode="json"))
    artifact_json = _durable_artifact_json(validated)
    receipt_id = _receipt_id(artifact_json)
    request = validated.authorization.request
    values = (
        validated.authorization.idempotency_key,
        validated.document_id,
        validated.sha256,
        validated.size_bytes,
        validated.source_url,
        request.provider.value,
        request.source_type.value,
        request.document_type.value,
        request.source_regime_identity.regime.value,
        request.source_regime_identity.contract_sha256,
        authorization_json,
        artifact_json,
    )
    existing = conn.execute(
        "SELECT idempotency_key,document_id,artifact_sha256,artifact_size_bytes,"
        "source_url,provider,source_type,document_type,source_regime,"
        "source_regime_contract_sha256,authorization_json,artifact_json "
        "FROM transcript_acquisition_receipts WHERE receipt_id=?",
        (receipt_id,),
    ).fetchone()
    if existing is not None:
        if tuple(existing) != values:
            raise TranscriptAcquisitionDeniedError("immutable transcript receipt collision")
        return receipt_id
    conn.execute(
        "INSERT INTO transcript_acquisition_receipts "
        "(receipt_id,idempotency_key,document_id,artifact_sha256,artifact_size_bytes,"
        "source_url,provider,source_type,document_type,source_regime,"
        "source_regime_contract_sha256,authorization_json,artifact_json,recorded_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            receipt_id,
            *values,
            datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        ),
    )
    return receipt_id


def load_authorized_transcript_replay(
    conn: sqlite3.Connection,
    *,
    request: TranscriptAcquisitionRequest,
    project_root: Path,
) -> AuthorizedTranscriptArtifact | None:
    """Load and revalidate the latest exact durable receipt for one target."""

    current = authorize_transcript_request(conn, request)
    try:
        row = conn.execute(
            "SELECT receipt_id,document_id,artifact_sha256,artifact_size_bytes,source_url,"
            "provider,source_type,document_type,source_regime,source_regime_contract_sha256,"
            "authorization_json,artifact_json FROM transcript_acquisition_receipts "
            "WHERE idempotency_key=? ORDER BY recorded_at DESC,receipt_id DESC LIMIT 1",
            (current.idempotency_key,),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        raise TranscriptAcquisitionDeniedError(
            "transcript acquisition receipt store is unavailable"
        ) from exc
    if row is None:
        return None
    try:
        artifact = AuthorizedTranscriptArtifact.model_validate_json(str(row["artifact_json"]))
    except (ValueError, TypeError) as exc:
        raise TranscriptAcquisitionDeniedError("stored transcript receipt is invalid") from exc
    artifact_json = _durable_artifact_json(artifact)
    durable_values = (
        artifact.document_id,
        artifact.sha256,
        artifact.size_bytes,
        artifact.source_url,
        artifact.authorization.request.provider.value,
        artifact.authorization.request.source_type.value,
        artifact.authorization.request.document_type.value,
        artifact.authorization.request.source_regime_identity.regime.value,
        artifact.authorization.request.source_regime_identity.contract_sha256,
        _canonical_json(artifact.authorization.model_dump(mode="json")),
        artifact_json,
    )
    if (
        str(row["receipt_id"]) != _receipt_id(artifact_json)
        or tuple(row)[1:] != durable_values
        or artifact.authorization.idempotency_key != current.idempotency_key
    ):
        raise TranscriptAcquisitionDeniedError(
            "stored transcript receipt does not exactly match target"
        )
    read_authorized_transcript(conn, artifact, project_root=project_root)
    return artifact


def require_persisted_authorized_transcript_artifact(
    conn: sqlite3.Connection,
    artifact: AuthorizedTranscriptArtifact,
    *,
    project_root: Path,
) -> AuthorizedTranscriptArtifact:
    """Require exact durable provenance before canonical transcript writes."""

    validated = AuthorizedTranscriptArtifact.model_validate(artifact, strict=True)
    persisted = load_authorized_transcript_replay(
        conn,
        request=validated.authorization.request,
        project_root=project_root,
    )
    if persisted != validated:
        raise TranscriptAcquisitionDeniedError(
            "authorized transcript artifact lacks its exact durable receipt"
        )
    return validated


def _document_request(
    *,
    entrypoint: TranscriptAcquisitionEntrypoint,
    ticker: str,
    year: int,
    quarter: int,
    as_of: date,
) -> TranscriptAcquisitionRequest:
    return TranscriptAcquisitionRequest(
        entrypoint=entrypoint,
        canonical_ticker=ticker,
        fiscal_year=year,
        fiscal_quarter=quarter,
        as_of=as_of,
        source_type=SourceType.IR_DOC,
        document_type=DocType.EARNINGS_CALL_TRANSCRIPT,
        provider=TranscriptProvider.ISSUER_IR,
        owner_requested=False,
        existing_artifact=False,
        existing_artifact_behavior=ExistingArtifactBehavior.REFRESH,
        source_policy_version=TRANSCRIPT_ACQUISITION_POLICY_VERSION,
        source_regime_identity=COMBINED_SOURCE_REGIME_IDENTITY,
    )


def stage_pending_issuer_transcripts(
    conn: sqlite3.Connection,
    *,
    tickers: Sequence[str],
    project_root: Path,
    private_root: Path,
    entrypoint: TranscriptAcquisitionEntrypoint,
    as_of: date,
) -> dict[int, AuthorizedTranscriptArtifact]:
    """Authorize then stage every pending persisted issuer-IR transcript."""

    normalized = tuple(dict.fromkeys(ticker.strip().upper() for ticker in tickers))
    if not normalized:
        return {}
    placeholders = ",".join("?" for _ in normalized)
    rows = conn.execute(
        "SELECT id,ticker,file_path,sha256,raw_bytes_size,source_url "
        "FROM documents WHERE source_type=? AND doc_type=? "
        f"AND ticker IN ({placeholders}) "  # nosec B608 -- placeholders derive from tuple length only
        "AND id NOT IN (SELECT document_id FROM transcripts) ORDER BY id",
        (SourceType.IR_DOC.value, DocType.IR_TRANSCRIPT.value, *normalized),
    ).fetchall()
    planned: list[tuple[sqlite3.Row, TranscriptAcquisitionAuthorization, Path, int, int]] = []
    for row in rows:
        name = Path(str(row["file_path"])).name
        parts = name.rsplit("_", 2)
        if len(parts) != 3 or not parts[1].upper().startswith("Q"):
            raise TranscriptAcquisitionDeniedError(
                f"document {row['id']} lacks canonical period identity"
            )
        try:
            quarter = int(parts[1][1:])
            year = int(Path(parts[2]).stem)
        except ValueError as exc:
            raise TranscriptAcquisitionDeniedError(
                f"document {row['id']} lacks canonical period identity"
            ) from exc
        request = _document_request(
            entrypoint=entrypoint,
            ticker=str(row["ticker"]).upper(),
            year=year,
            quarter=quarter,
            as_of=as_of,
        )
        authorization = require_authorized_transcript_request(conn, request)
        relative = Path(str(row["file_path"]))
        source_path = (project_root / relative).resolve(strict=True)
        try:
            source_path.relative_to(project_root.resolve(strict=True))
        except ValueError as exc:
            raise TranscriptAcquisitionDeniedError(
                "persisted transcript path escapes project root"
            ) from exc
        expected_size = int(row["raw_bytes_size"])
        planned.append((row, authorization, source_path, expected_size, int(row["id"])))

    if planned:
        private_root.mkdir(parents=True, exist_ok=True)
    artifacts: dict[int, AuthorizedTranscriptArtifact] = {}
    for row, authorization, source_path, expected_size, document_id in planned:
        artifacts[document_id] = _stage_authorized_file(
            authorization,
            source_path=source_path,
            private_root=private_root,
            expected_sha256=str(row["sha256"]),
            expected_size_bytes=expected_size,
            source_url=str(row["source_url"]) if row["source_url"] is not None else None,
            document_id=document_id,
        )
    return artifacts


__all__ = [
    "COMBINED_SOURCE_REGIME_IDENTITY",
    "AuthorizedTranscriptArtifact",
    "TranscriptAcquisitionDeniedError",
    "authorize_transcript_request",
    "load_authorized_transcript_replay",
    "persist_authorized_transcript_artifact",
    "read_authorized_transcript",
    "require_authorized_transcript_request",
    "require_persisted_authorized_transcript_artifact",
    "stage_authorized_payload",
    "stage_pending_issuer_transcripts",
]
