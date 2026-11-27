"""DB-bound transcript authorization and immutable artifact handoff.

This is the only module that projects stored company identity into the pure
BHA-29 transcript authorization contract.  Provider callers receive a typed
receipt before external work; persistence callers consume only immutable
staged bytes after revalidating both the receipt and stored identity.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import tempfile
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models.documents import DocType, SourceType
from pipeline.data_coverage_dispositions import fiscal_quarter_period_end
from pipeline.reported_period_inventory import reported_period_window_from_inventory
from pipeline.source_policy import (
    ArtifactKind,
    AuthorizationReason,
    CollectionSource,
    ReportedPeriodStatus,
    ReportedPeriodWindow,
    StoredIdentityStatus,
    authorize_collection_target_in_connection,
    canonical_https_url,
    current_utc_date,
    decision_for,
    fiscal_quarter_end,
    ir_url_is_authorized,
    issuer_policy,
)
from provenance.selection import selected_transcripts_relation
from provenance.source_regime import SourceRegime, contract_for, receipt_identity
from runtime.deterministic import canonical_json_ascii as _canonical_json
from transcripts.acquisition_semantics import (
    TRANSCRIPT_ACQUISITION_POLICY_VERSION,
    ExistingArtifactBehavior,
    StoredTargetStatus,
    TranscriptAcquisitionAuthorization,
    TranscriptAcquisitionEntrypoint,
    TranscriptAcquisitionRequest,
    TranscriptAuthorizationReason,
    TranscriptAuthorizationStatus,
    TranscriptProvider,
    TranscriptReportingStatus,
    TranscriptStoredTarget,
    authorize_transcript_acquisition_request,
    is_canonical_issuer_transcript_path,
    parse_canonical_transcript_filename,
    transcript_authorization_idempotency_key,
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
_UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")


class TranscriptAcquisitionDeniedError(PermissionError):
    """The canonical transcript contract denied work before side effects."""


def _require_current_source_request(request: TranscriptAcquisitionRequest) -> None:
    """Keep legacy receipts readable without allowing new calendar-based work."""

    if request.schema_version != "transcript-acquisition-request@2":
        raise TranscriptAcquisitionDeniedError(
            "current transcript acquisition requires a source-bound request@2"
        )


def _is_retained_reindex_source_url(source_url: str) -> bool:
    """Return True for a safe local reindex provenance marker on a persisted IR document."""
    return (
        source_url.startswith("reindex_subdir:")
        and len(source_url) > len("reindex_subdir:")
        and ".." not in source_url
        and "\x00" not in source_url
    )


class AuthorizedTranscriptArtifact(BaseModel):
    """Exact authorized provenance bound to one immutable staged snapshot."""

    model_config = _STRICT_FROZEN

    schema_version: Literal["authorized-transcript-artifact@1"] = "authorized-transcript-artifact@1"
    authorization: TranscriptAcquisitionAuthorization
    document_id: int | None = Field(default=None, ge=1)
    source_url: str | None
    canonical_document_path: Path
    source_path: Path
    staged: StagedTranscriptArtifact

    @model_validator(mode="after")
    def _exact_binding(self) -> Self:
        authorization = validate_transcript_acquisition_authorization(self.authorization)
        if authorization.status is not TranscriptAuthorizationStatus.AUTHORIZED:
            raise ValueError("artifact requires an authorized acquisition receipt")
        if self.source_path != self.staged.source_path:
            raise ValueError("artifact source path does not match staged provenance")
        relative = self.canonical_document_path
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ValueError("canonical document path must be repository-relative")
        request = authorization.request
        expected_stem = (
            f"{request.canonical_ticker}_Q{request.fiscal_quarter}_{request.fiscal_year}"
        )
        expected_names = (
            f"{expected_stem}.txt",
            f"{expected_stem}__{self.staged.sha256[:8]}.txt",
        )
        if self.document_id is None:
            if relative.parts[:2] != ("transcripts", "raw"):
                raise ValueError(
                    "new acquisition must target the canonical raw transcript directory"
                )
            if relative.name not in expected_names:
                raise ValueError("canonical document path does not match authorized target")
        else:
            is_canonical_issuer_path = is_canonical_issuer_transcript_path(
                relative, request.canonical_ticker
            )
            if relative.name not in expected_names and not is_canonical_issuer_path:
                raise ValueError("canonical document path does not match authorized target")
        if self.source_url is not None and not (
            self.document_id is not None and _is_retained_reindex_source_url(self.source_url)
        ):
            parsed = urlsplit(self.source_url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.fragment
            ):
                raise ValueError("transcript source URL must be a public HTTPS URL")
        return self

    @property
    def sha256(self) -> str:
        return self.staged.sha256

    @property
    def size_bytes(self) -> int:
        return self.staged.size_bytes


def _month_end_mmdd(fiscal_year_end_month: int) -> str:
    next_month = (
        date(2002, 1, 1)
        if fiscal_year_end_month == 12
        else date(2001, fiscal_year_end_month + 1, 1)
    )
    last_day = (next_month - timedelta(days=1)).day
    return f"{fiscal_year_end_month:02d}-{last_day:02d}"


def _is_52_53_week_fye(fye: str, fiscal_year_end_month: int) -> bool:
    """Return True for a 52/53-week close (days 22..<month_end) in `fiscal_year_end_month`."""
    if not (
        len(fye) == 5
        and fye[2] == "-"
        and fye[:2].isdigit()
        and fye[3:].isdigit()
        and int(fye[:2]) == fiscal_year_end_month
    ):
        return False
    day = int(fye[3:])
    month_end_day = int(_month_end_mmdd(fiscal_year_end_month)[3:])
    return 22 <= day < month_end_day


_REPORTED_WITNESS_DOC_TYPES = (
    "10-Q",
    "10-K",
    "20-F",
    "40-F",
    "6-K",
    "ir_press_release",
    "ir_presentation",
    "ir_transcript",
)


def _reported_period_witness_dates(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    as_of: date,
) -> set[date]:
    """Collect reported `period_end` dates (<= `as_of`) from canonical facts and reported docs."""
    from provenance.financial_fact_resolution import canonical_fact_relation

    witnesses: set[date] = set()
    ticker_upper = ticker.upper()
    as_of_iso = as_of.isoformat()

    try:
        fact_relation = canonical_fact_relation(conn, "financial_facts").sql
        rows = conn.execute(
            f"SELECT DISTINCT date(period_end) FROM {fact_relation} "  # nosec B608 -- closed internal relation from canonical_fact_relation
            "WHERE UPPER(ticker)=? AND period_end IS NOT NULL AND date(period_end)<=date(?)",
            (ticker_upper, as_of_iso),
        ).fetchall()
        for row in rows:
            raw_date = str(row[0] or "").strip()
            if len(raw_date) >= 10:
                try:
                    witnesses.add(date.fromisoformat(raw_date[:10]))
                except ValueError:
                    continue
    except (sqlite3.Error, ValueError):
        pass

    doc_cols: set[str]
    try:
        doc_cols = {
            str(col[1]).lower() for col in conn.execute("PRAGMA table_info(documents)").fetchall()
        }
    except sqlite3.Error:
        doc_cols = set()
    if {"ticker", "doc_type", "period_end"} <= doc_cols:
        placeholders = ",".join("?" for _ in _REPORTED_WITNESS_DOC_TYPES)
        status_clause = " AND fetch_status='ok'" if "fetch_status" in doc_cols else ""
        try:
            doc_rows = conn.execute(
                "SELECT DISTINCT date(period_end) FROM documents "
                f"WHERE UPPER(ticker)=? AND period_end IS NOT NULL AND date(period_end)<=date(?) "  # nosec B608 -- placeholder count from constant tuple
                f"AND doc_type IN ({placeholders}){status_clause}",
                (ticker_upper, as_of_iso, *_REPORTED_WITNESS_DOC_TYPES),
            ).fetchall()
            for row in doc_rows:
                raw_date = str(row[0] or "").strip()
                if len(raw_date) >= 10:
                    try:
                        witnesses.add(date.fromisoformat(raw_date[:10]))
                    except ValueError:
                        continue
        except sqlite3.Error:
            pass

    return witnesses


def _derive_fiscal_year_end(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    fiscal_year_end_month: int | None,
) -> str | None:
    if fiscal_year_end_month is None or not 1 <= fiscal_year_end_month <= 12:
        return None
    fye: str | None = None
    try:
        row = conn.execute(
            "SELECT fiscal_year_end FROM tracked_companies WHERE UPPER(ticker)=? LIMIT 1",
            (ticker.upper(),),
        ).fetchone()
        raw = str(row[0]).strip() if row is not None and row[0] is not None else ""
        if (
            len(raw) == 5
            and raw[2] == "-"
            and raw[:2].isdigit()
            and raw[3:].isdigit()
            and int(raw[:2]) == fiscal_year_end_month
        ):
            fye = raw
    except sqlite3.Error:
        fye = None
    if fye is not None:
        return fye
    return _month_end_mmdd(fiscal_year_end_month)


def _calendar_reported_period_window(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    fiscal_year_end_month: int | None,
    as_of: date,
    max_quarters: int = 8,
    include_all_transcript_docs: bool = False,
) -> ReportedPeriodWindow | None:
    fye = _derive_fiscal_year_end(
        conn,
        ticker=ticker,
        fiscal_year_end_month=fiscal_year_end_month,
    )
    if fye is None or fiscal_year_end_month is None:
        return None
    if _is_52_53_week_fye(fye, fiscal_year_end_month):
        witnesses = _reported_period_witness_dates(conn, ticker=ticker, as_of=as_of)
        if not witnesses:
            return None
        canonical_fye = _month_end_mmdd(fiscal_year_end_month)
        witnessed_ends: set[date] = set()
        for fy in range(as_of.year - 4, as_of.year + 2):
            for fq in (1, 2, 3, 4):
                try:
                    q_end = fiscal_quarter_end(
                        fiscal_year=fy,
                        fiscal_quarter=fq,
                        fiscal_year_end=canonical_fye,
                    )
                except ValueError:
                    continue
                if any(abs((w - q_end).days) <= 7 for w in witnesses):
                    witnessed_ends.add(q_end)
        ordered_witnessed = tuple(sorted(witnessed_ends, reverse=True)[:max_quarters])
        if not ordered_witnessed:
            return None
        proof_sha256 = hashlib.sha256(
            _canonical_json(
                {
                    "fiscal_year_end": canonical_fye,
                    "period_ends": [d.isoformat() for d in ordered_witnessed],
                    "reason": "fiscal_calendar_52_53_week_witness",
                    "ticker": ticker.upper(),
                    "witness_dates": sorted(w.isoformat() for w in witnesses),
                }
            ).encode("utf-8")
        ).hexdigest()
        return ReportedPeriodWindow(
            status=ReportedPeriodStatus.COMPLETE,
            period_ends=ordered_witnessed,
            snapshot_ids=(),
            fiscal_year_end=canonical_fye,
            proof_sha256=proof_sha256,
            reason="fiscal_calendar_52_53_week_witness",
        )
    candidates: set[date] = set()
    for fy in range(as_of.year - 4, as_of.year + 2):
        for fq in (1, 2, 3, 4):
            try:
                q_end = fiscal_quarter_end(
                    fiscal_year=fy,
                    fiscal_quarter=fq,
                    fiscal_year_end=fye,
                )
            except ValueError:
                continue
            if q_end <= as_of:
                candidates.add(q_end)
    period_ends = sorted(candidates, reverse=True)[:max_quarters]

    doc_cols: set[str]
    try:
        doc_cols = {
            str(col[1]).lower() for col in conn.execute("PRAGMA table_info(documents)").fetchall()
        }
    except sqlite3.Error:
        doc_cols = set()
    if doc_cols:
        has_period_end = "period_end" in doc_cols
        sql = (
            "SELECT file_path, period_end FROM documents "
            if has_period_end
            else "SELECT file_path, NULL FROM documents "
        ) + "WHERE UPPER(ticker)=? AND source_type=? AND doc_type=?"
        for row in conn.execute(
            sql,
            (ticker.upper(), SourceType.IR_DOC.value, DocType.IR_TRANSCRIPT.value),
        ).fetchall():
            period = _resolve_document_period(
                conn,
                ticker=ticker,
                file_path=str(row[0] or ""),
                period_end_raw=row[1],
            )
            if period is None:
                continue
            try:
                doc_end = fiscal_quarter_end(
                    fiscal_year=period[0],
                    fiscal_quarter=period[1],
                    fiscal_year_end=fye,
                )
            except ValueError:
                continue
            if doc_end <= as_of and doc_end not in period_ends:
                period_ends.append(doc_end)

    if include_all_transcript_docs:
        try:
            transcripts_rel = selected_transcripts_relation(conn).sql
            transcript_rows = conn.execute(
                f"SELECT period_end FROM {transcripts_rel} "  # nosec B608 -- repository-owned selection relation
                "WHERE UPPER(ticker)=? AND period_end IS NOT NULL",
                (ticker.upper(),),
            ).fetchall()
        except sqlite3.Error:
            transcript_rows = []
        for row in transcript_rows:
            raw = str(row[0] or "").strip()
            if len(raw) < 10:
                continue
            try:
                tr_end = date.fromisoformat(raw[:10])
            except ValueError:
                continue
            if tr_end <= as_of and tr_end not in period_ends:
                period_ends.append(tr_end)

    ordered = tuple(sorted(set(period_ends), reverse=True))
    if not ordered:
        return None
    proof_sha256 = hashlib.sha256(
        _canonical_json(
            {
                "fiscal_year_end": fye,
                "period_ends": [d.isoformat() for d in ordered],
                "reason": "fiscal_calendar_window",
                "ticker": ticker.upper(),
            }
        ).encode("utf-8")
    ).hexdigest()
    return ReportedPeriodWindow(
        status=ReportedPeriodStatus.COMPLETE,
        period_ends=ordered,
        snapshot_ids=(),
        fiscal_year_end=fye,
        proof_sha256=proof_sha256,
        reason="fiscal_calendar_window",
    )


def _stored_target_for_request(
    conn: sqlite3.Connection,
    request: TranscriptAcquisitionRequest,
) -> TranscriptStoredTarget:
    _require_current_source_request(request)
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
    reported_window: ReportedPeriodWindow | None = None
    fiscal_year_end: str | None = None
    if identity_authorized:
        status = StoredTargetStatus.AUTHORIZED
        try:
            reported_window = reported_period_window_from_inventory(
                conn, ticker=request.canonical_ticker, as_of=request.as_of
            )
        except sqlite3.Error:
            reported_window = ReportedPeriodWindow(
                status=ReportedPeriodStatus.UNKNOWN, reason="source_inventory_unavailable"
            )
        if reported_window.status is not ReportedPeriodStatus.COMPLETE:
            calendar_window = _calendar_reported_period_window(
                conn,
                ticker=request.canonical_ticker,
                fiscal_year_end_month=stored.fiscal_year_end_month,
                as_of=request.as_of,
            )
            if calendar_window is not None:
                reported_window = calendar_window
        if reported_window.status is ReportedPeriodStatus.COMPLETE:
            fiscal_year_end = reported_window.fiscal_year_end
            if (
                fiscal_year_end is None
                or stored.fiscal_year_end_month is None
                or int(fiscal_year_end[:2]) != stored.fiscal_year_end_month
            ):
                reported_window = ReportedPeriodWindow(
                    status=ReportedPeriodStatus.AMBIGUOUS,
                    reason="fiscal_profile_mismatch",
                    snapshot_ids=reported_window.snapshot_ids,
                )
                fiscal_year_end = None
        if fiscal_year_end is None:
            reporting = TranscriptReportingStatus.PENDING
        else:
            target_end = fiscal_quarter_end(
                fiscal_year=request.fiscal_year,
                fiscal_quarter=request.fiscal_quarter,
                fiscal_year_end=fiscal_year_end,
            )
            if request.owner_requested and not reported_window.includes(target_end):
                expanded_window = _calendar_reported_period_window(
                    conn,
                    ticker=request.canonical_ticker,
                    fiscal_year_end_month=stored.fiscal_year_end_month,
                    as_of=request.as_of,
                    max_quarters=12,
                    include_all_transcript_docs=True,
                )
                if (
                    expanded_window is not None
                    and expanded_window.fiscal_year_end == fiscal_year_end
                    and expanded_window.includes(target_end)
                ):
                    reported_window = expanded_window
            reporting = (
                TranscriptReportingStatus.ELIGIBLE
                if reported_window.includes(target_end)
                else TranscriptReportingStatus.OUT_OF_WINDOW
            )
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
        fiscal_year_end=fiscal_year_end,
        reported_window=reported_window,
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

    _require_current_source_request(request)
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
    canonical_document_path: Path,
    document_id: int | None,
) -> AuthorizedTranscriptArtifact:
    validated = validate_transcript_acquisition_authorization(authorization)
    _require_current_source_request(validated.request)
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
        canonical_document_path=canonical_document_path,
        source_path=staged.source_path,
        staged=staged,
    )


def stage_authorized_payload(
    authorization: TranscriptAcquisitionAuthorization,
    *,
    payload: bytes,
    private_root: Path,
    source_url: str | None,
    canonical_document_path: Path,
) -> AuthorizedTranscriptArtifact:
    """Stage acquired response bytes only after the exact provider is authorized."""

    validated = validate_transcript_acquisition_authorization(authorization)
    _require_current_source_request(validated.request)
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
            canonical_document_path=canonical_document_path,
            document_id=None,
        )
    finally:
        source_path.unlink(missing_ok=True)


def read_authorized_transcript(
    conn: sqlite3.Connection,
    artifact: AuthorizedTranscriptArtifact,
    *,
    project_root: Path,
    trusted_staging_root: Path,
) -> bytes:
    """Require current authorization before an active read or canonical write."""

    validated = AuthorizedTranscriptArtifact.model_validate(artifact, strict=True)
    current = authorize_transcript_request(conn, validated.authorization.request)
    if current != validated.authorization:
        raise TranscriptAcquisitionDeniedError(
            "transcript authorization no longer matches stored policy"
        )
    return _read_transcript_snapshot(
        conn,
        validated,
        project_root=project_root,
        trusted_staging_root=trusted_staging_root,
    )


def _read_transcript_snapshot(
    conn: sqlite3.Connection,
    artifact: AuthorizedTranscriptArtifact,
    *,
    project_root: Path,
    trusted_staging_root: Path,
) -> bytes:
    """Read exact bytes from a validated immutable receipt without current policy."""

    validated = AuthorizedTranscriptArtifact.model_validate(artifact, strict=True)
    root = project_root.resolve(strict=True)
    trusted_root = trusted_staging_root.resolve(strict=True)
    trusted_root_stat = trusted_root.stat()
    canonical_path = (root / validated.canonical_document_path).resolve()
    try:
        canonical_path.relative_to(root)
    except ValueError as exc:
        raise TranscriptAcquisitionDeniedError(
            "canonical transcript path escapes the trusted project root"
        ) from exc
    staged = validated.staged
    expected_source_path = staged.source_path
    expected_source_device = staged.source_device
    expected_source_inode = staged.source_inode
    expected_sha256 = staged.sha256
    expected_size_bytes = staged.size_bytes
    if validated.document_id is not None:
        row = conn.execute(
            "SELECT ticker,source_type,doc_type,file_path,sha256,raw_bytes_size,source_url "
            "FROM documents WHERE id=?",
            (validated.document_id,),
        ).fetchone()
        request = validated.authorization.request
        if row is None or (
            str(row["ticker"]).upper() != request.canonical_ticker
            or str(row["source_type"]) != SourceType.IR_DOC.value
            or str(row["doc_type"]) != DocType.IR_TRANSCRIPT.value
            or Path(str(row["file_path"])) != validated.canonical_document_path
            or str(row["sha256"]) != validated.sha256
            or int(row["raw_bytes_size"]) != validated.size_bytes
            or (str(row["source_url"]) if row["source_url"] is not None else None)
            != validated.source_url
        ):
            raise TranscriptAcquisitionDeniedError(
                "authorized transcript artifact does not match its canonical document row"
            )
        expected_source_path = canonical_path.resolve(strict=True)
        source_stat = expected_source_path.stat()
        expected_source_device = int(source_stat.st_dev)
        expected_source_inode = int(source_stat.st_ino)
        expected_sha256 = str(row["sha256"])
        expected_size_bytes = int(row["raw_bytes_size"])
    else:
        source_parent = staged.source_path.resolve().parent
        if source_parent != trusted_root.parent:
            raise TranscriptAcquisitionDeniedError(
                "acquired transcript source is outside the trusted staging namespace"
            )
    return read_staged_transcript(
        staged,
        trusted_staging_root=trusted_root,
        trusted_staging_root_device=int(trusted_root_stat.st_dev),
        trusted_staging_root_inode=int(trusted_root_stat.st_ino),
        expected_source_path=expected_source_path,
        expected_source_device=expected_source_device,
        expected_source_inode=expected_source_inode,
        expected_sha256=expected_sha256,
        expected_size_bytes=expected_size_bytes,
    )


def _durable_artifact_json(artifact: AuthorizedTranscriptArtifact) -> str:
    payload = artifact.model_dump(mode="json")
    payload["canonical_document_path"] = artifact.canonical_document_path.as_posix()
    return _canonical_json(payload)


def _receipt_id(artifact_json: str) -> str:
    return hashlib.sha256(artifact_json.encode("utf-8")).hexdigest()


def transcript_acquisition_receipt_id(artifact: AuthorizedTranscriptArtifact) -> str:
    """Return the immutable receipt identity committed by an authorized artifact."""

    validated = AuthorizedTranscriptArtifact.model_validate(artifact, strict=True)
    return _receipt_id(_durable_artifact_json(validated))


def project_root_for_database(database_path: str | os.PathLike[str]) -> Path:
    """Derive the trusted repository root from the canonical database location."""

    path = Path(database_path).resolve()
    return path.parent.parent if path.parent.name.lower() == "data" else path.parent


def _host_matches_authority(candidate_host: str, authority_url: str | None) -> bool:
    if not authority_url:
        return False
    authority = canonical_https_url(authority_url)
    if authority is None:
        return False
    return candidate_host.removeprefix("www.") == authority[0].removeprefix("www.")


def issuer_transcript_source_url_is_authorized(
    ticker: str,
    source_url: str,
    *,
    project_root: Path,
    fiscal_year: int | None = None,
    fiscal_quarter: int | None = None,
) -> bool:
    """Bind issuer transcript URLs to reviewed policy or configured issuer authority."""

    candidate = canonical_https_url(source_url)
    if candidate is None:
        return False
    from ir_pipeline.transcript import reviewed_issuer_transcript_url_is_authorized

    if (
        fiscal_year is not None
        and fiscal_quarter is not None
        and reviewed_issuer_transcript_url_is_authorized(
            ticker,
            fiscal_year,
            fiscal_quarter,
            source_url,
        )
    ):
        return True
    try:
        policy = issuer_policy(ticker)
    except ValueError:
        policy = None
    if policy is not None:
        return ir_url_is_authorized(policy.ir, source_url)

    from ir_pipeline.config import get_config
    from ir_pipeline.manifest import load_manifest

    try:
        config = get_config(ticker, project_root)
    except (OSError, TypeError, ValueError):
        return False
    if (
        config is not None
        and config.results_center_url
        and _host_matches_authority(candidate[0], config.results_center_url)
    ):
        return True
    if config is None:
        return False
    return any(
        entry.ticker.upper() == ticker.upper()
        and entry.doc_type == "transcript"
        and entry.url == source_url
        for entry in load_manifest(project_root, ticker)
    )


def _receipt_paths_are_safe(
    artifact: AuthorizedTranscriptArtifact,
    *,
    project_root: Path,
) -> bool:
    trusted_root = (project_root / ".tmp" / "transcript-acquisition").resolve()
    staged = artifact.staged
    try:
        source = staged.source_path.resolve()
        staging_root = staged.staging_root.resolve()
        staged_path = staged.staged_path.resolve()
        canonical = (project_root / artifact.canonical_document_path).resolve()
        canonical.relative_to(project_root)
        if staging_root != trusted_root:
            return False
        staged_path.relative_to(trusted_root)
        if staged_path != trusted_root / f"{artifact.sha256}.transcript":
            return False
        if artifact.document_id is None:
            source.relative_to(trusted_root.parent)
        else:
            source.relative_to(project_root)
    except (OSError, ValueError):
        return False
    return True


def register_transcript_receipt_sqlite_functions(
    conn: sqlite3.Connection,
    *,
    project_root: Path,
) -> None:
    """Register deterministic validation used by the receipt INSERT trigger."""

    project_root = project_root.resolve(strict=True)

    def validate(
        receipt_id: object,
        idempotency_key: object,
        document_id: object,
        canonical_ticker: object,
        fiscal_year: object,
        fiscal_quarter: object,
        canonical_document_path: object,
        artifact_sha256: object,
        artifact_size_bytes: object,
        source_url: object,
        provider: object,
        source_type: object,
        document_type: object,
        source_regime: object,
        source_regime_contract_sha256: object,
        authorization_json: object,
        artifact_json: object,
        recorded_at: object,
    ) -> int:
        try:
            if not isinstance(authorization_json, str) or not isinstance(artifact_json, str):
                return 0
            authorization = TranscriptAcquisitionAuthorization.model_validate_json(
                authorization_json
            )
            authorization = validate_transcript_acquisition_authorization(authorization)
            artifact = AuthorizedTranscriptArtifact.model_validate_json(artifact_json)
            if artifact.authorization != authorization:
                return 0
            request = authorization.request
            scalar_values = (
                receipt_id,
                idempotency_key,
                document_id,
                canonical_ticker,
                fiscal_year,
                fiscal_quarter,
                canonical_document_path,
                artifact_sha256,
                artifact_size_bytes,
                source_url,
                provider,
                source_type,
                document_type,
                source_regime,
                source_regime_contract_sha256,
            )
            expected_values = (
                _receipt_id(_durable_artifact_json(artifact)),
                transcript_authorization_idempotency_key(request),
                artifact.document_id,
                request.canonical_ticker,
                request.fiscal_year,
                request.fiscal_quarter,
                artifact.canonical_document_path.as_posix(),
                artifact.sha256,
                artifact.size_bytes,
                artifact.source_url,
                request.provider.value,
                request.source_type.value,
                request.document_type.value,
                request.source_regime_identity.regime.value,
                request.source_regime_identity.contract_sha256,
            )
            if scalar_values != expected_values:
                return 0
            if authorization_json != _canonical_json(authorization.model_dump(mode="json")):
                return 0
            if artifact_json != _durable_artifact_json(artifact):
                return 0
            if not isinstance(recorded_at, str) or _UTC_TIMESTAMP.fullmatch(recorded_at) is None:
                return 0
            parsed_recorded_at = datetime.strptime(recorded_at, "%Y-%m-%dT%H:%M:%S.%fZ")
            if parsed_recorded_at.tzinfo is not None:
                return 0
            if not _receipt_paths_are_safe(artifact, project_root=project_root):
                return 0
            if (
                request.provider is TranscriptProvider.ISSUER_IR
                and artifact.document_id is None
                and (
                    artifact.source_url is None
                    or not issuer_transcript_source_url_is_authorized(
                        request.canonical_ticker,
                        artifact.source_url,
                        project_root=project_root,
                        fiscal_year=request.fiscal_year,
                        fiscal_quarter=request.fiscal_quarter,
                    )
                )
            ):
                return 0
        except (OSError, TypeError, ValueError):
            return 0
        return 1

    conn.create_function(
        "transcript_receipt_valid",
        18,
        validate,
        deterministic=True,
    )


def persist_authorized_transcript_artifact(
    conn: sqlite3.Connection,
    artifact: AuthorizedTranscriptArtifact,
    *,
    project_root: Path,
    trusted_staging_root: Path,
) -> str:
    """Append or exactly replay one durable artifact receipt."""

    validated = AuthorizedTranscriptArtifact.model_validate(artifact, strict=True)
    _require_current_source_request(validated.authorization.request)
    payload = read_authorized_transcript(
        conn,
        validated,
        project_root=project_root,
        trusted_staging_root=trusted_staging_root,
    )
    if hashlib.sha256(payload).hexdigest() != validated.sha256:
        raise TranscriptAcquisitionDeniedError("staged transcript digest changed before receipt")
    authorization_json = _canonical_json(validated.authorization.model_dump(mode="json"))
    artifact_json = _durable_artifact_json(validated)
    receipt_id = transcript_acquisition_receipt_id(validated)
    request = validated.authorization.request
    values = (
        validated.authorization.idempotency_key,
        validated.document_id,
        request.canonical_ticker,
        request.fiscal_year,
        request.fiscal_quarter,
        validated.canonical_document_path.as_posix(),
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
        "SELECT idempotency_key,document_id,canonical_ticker,fiscal_year,fiscal_quarter,"
        "canonical_document_path,artifact_sha256,artifact_size_bytes,"
        "source_url,provider,source_type,document_type,source_regime,"
        "source_regime_contract_sha256,authorization_json,artifact_json "
        "FROM transcript_acquisition_receipts WHERE receipt_id=?",
        (receipt_id,),
    ).fetchone()
    if existing is not None:
        if tuple(existing) != values:
            raise TranscriptAcquisitionDeniedError("immutable transcript receipt collision")
        return receipt_id
    register_transcript_receipt_sqlite_functions(conn, project_root=project_root)
    conn.execute(
        "INSERT INTO transcript_acquisition_receipts "
        "(receipt_id,idempotency_key,document_id,canonical_ticker,fiscal_year,fiscal_quarter,"
        "canonical_document_path,artifact_sha256,artifact_size_bytes,"
        "source_url,provider,source_type,document_type,source_regime,"
        "source_regime_contract_sha256,authorization_json,artifact_json,recorded_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
    trusted_staging_root: Path,
) -> AuthorizedTranscriptArtifact | None:
    """Load and revalidate the latest exact durable receipt for one target."""

    current = authorize_transcript_request(conn, request)
    try:
        rows = conn.execute(
            "SELECT receipt_id,document_id,canonical_ticker,fiscal_year,fiscal_quarter,"
            "canonical_document_path,artifact_sha256,artifact_size_bytes,source_url,"
            "provider,source_type,document_type,source_regime,source_regime_contract_sha256,"
            "authorization_json,artifact_json FROM transcript_acquisition_receipts "
            "WHERE idempotency_key=? ORDER BY recorded_at DESC,receipt_id DESC",
            (current.idempotency_key,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        raise TranscriptAcquisitionDeniedError(
            "transcript acquisition receipt store is unavailable"
        ) from exc
    for row in rows:
        artifact = _validate_transcript_receipt_row(row)
        if artifact.authorization.idempotency_key != current.idempotency_key:
            raise TranscriptAcquisitionDeniedError(
                "stored transcript receipt does not exactly match target"
            )
        # Entrypoint and owner intent are authorization boundaries even though
        # they are deliberately excluded from the target-level idempotency key.
        # Never replay a receipt across those origins; let the current origin
        # persist its own exact receipt instead.
        stored_request = artifact.authorization.request
        if (
            stored_request.entrypoint is not request.entrypoint
            or stored_request.owner_requested is not request.owner_requested
        ):
            continue
        read_authorized_transcript(
            conn,
            artifact,
            project_root=project_root,
            trusted_staging_root=trusted_staging_root,
        )
        return artifact
    return None


def _validate_transcript_receipt_row(
    row: sqlite3.Row,
) -> AuthorizedTranscriptArtifact:
    try:
        artifact = AuthorizedTranscriptArtifact.model_validate_json(str(row["artifact_json"]))
    except (IndexError, KeyError, ValueError, TypeError) as exc:
        raise TranscriptAcquisitionDeniedError("stored transcript receipt is invalid") from exc
    artifact_json = _durable_artifact_json(artifact)
    durable_values = (
        artifact.document_id,
        artifact.authorization.request.canonical_ticker,
        artifact.authorization.request.fiscal_year,
        artifact.authorization.request.fiscal_quarter,
        artifact.canonical_document_path.as_posix(),
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
        str(row["receipt_id"]) != transcript_acquisition_receipt_id(artifact)
        or tuple(row)[1:] != durable_values
    ):
        raise TranscriptAcquisitionDeniedError(
            "stored transcript receipt does not exactly match target"
        )
    return artifact


def load_authorized_transcript_receipt(
    conn: sqlite3.Connection,
    *,
    receipt_id: str,
    project_root: Path,
    trusted_staging_root: Path,
) -> AuthorizedTranscriptArtifact:
    """Reconstruct one exact durable receipt and its unchanged artifact bytes."""

    try:
        row = conn.execute(
            "SELECT receipt_id,document_id,canonical_ticker,fiscal_year,fiscal_quarter,"
            "canonical_document_path,artifact_sha256,artifact_size_bytes,source_url,"
            "provider,source_type,document_type,source_regime,source_regime_contract_sha256,"
            "authorization_json,artifact_json FROM transcript_acquisition_receipts "
            "WHERE receipt_id=?",
            (receipt_id,),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        raise TranscriptAcquisitionDeniedError(
            "transcript acquisition receipt store is unavailable"
        ) from exc
    if row is None:
        raise TranscriptAcquisitionDeniedError("transcript acquisition receipt is unavailable")
    artifact = _validate_transcript_receipt_row(row)
    _read_transcript_snapshot(
        conn,
        artifact,
        project_root=project_root,
        trusted_staging_root=trusted_staging_root,
    )
    return artifact


def require_persisted_authorized_transcript_artifact(
    conn: sqlite3.Connection,
    artifact: AuthorizedTranscriptArtifact,
    *,
    project_root: Path,
    trusted_staging_root: Path,
) -> AuthorizedTranscriptArtifact:
    """Require exact durable provenance before canonical transcript writes."""

    validated = AuthorizedTranscriptArtifact.model_validate(artifact, strict=True)
    persisted = load_authorized_transcript_replay(
        conn,
        request=validated.authorization.request,
        project_root=project_root,
        trusted_staging_root=trusted_staging_root,
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
        schema_version="transcript-acquisition-request@2",
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


_ISO_DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _resolve_document_period(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    file_path: str,
    period_end_raw: object | None,
) -> tuple[int, int] | None:
    """Resolve ``(fiscal_year, fiscal_quarter)`` from legacy filename, ``period_end``, or ISO directory."""
    path_obj = Path(file_path)
    stem = path_obj.stem
    if "__" in stem:
        prefix, suffix = stem.rsplit("__", 1)
        if re.fullmatch(r"[0-9a-f]{8}", suffix):
            stem = prefix
    parts = stem.rsplit("_", 2)
    if len(parts) == 3 and parts[1].upper().startswith("Q"):
        try:
            quarter = int(parts[1][1:])
            year = int(parts[2])
        except ValueError:
            pass
        else:
            if 1 <= quarter <= 4:
                return year, quarter

    pe_date: date | None = None
    if period_end_raw is not None:
        raw_str = str(period_end_raw).strip()
        if len(raw_str) >= 10:
            try:
                pe_date = date.fromisoformat(raw_str[:10])
            except ValueError:
                pe_date = None
    if pe_date is None:
        for part in reversed(path_obj.parts[:-1]):
            if _ISO_DATE_DIR_RE.fullmatch(part):
                try:
                    pe_date = date.fromisoformat(part)
                    break
                except ValueError:
                    continue
    if pe_date is None:
        return None

    fye_row = conn.execute(
        "SELECT fiscal_year_end FROM tracked_companies WHERE UPPER(ticker)=? LIMIT 1",
        (ticker.upper(),),
    ).fetchone()
    fye = str(fye_row[0]).strip() if fye_row and fye_row[0] else "12-31"
    if len(fye) == 5 and fye[2] == "-" and fye[:2].isdigit():
        fye_month = int(fye[:2])
        if 1 <= fye_month <= 12 and _is_52_53_week_fye(fye, fye_month):
            fye = _month_end_mmdd(fye_month)
    best_period: tuple[int, int] | None = None
    best_delta = 8
    for fy in (pe_date.year - 1, pe_date.year, pe_date.year + 1):
        for fq in (1, 2, 3, 4):
            try:
                q_end = fiscal_quarter_end(fiscal_year=fy, fiscal_quarter=fq, fiscal_year_end=fye)
            except ValueError:
                continue
            delta = abs((pe_date - q_end).days)
            if delta <= 7 and delta < best_delta:
                best_delta = delta
                best_period = (fy, fq)
    return best_period


def stage_pending_issuer_transcripts(
    conn: sqlite3.Connection,
    *,
    tickers: Sequence[str],
    project_root: Path,
    private_root: Path,
    entrypoint: TranscriptAcquisitionEntrypoint,
    as_of: date,
    pending_callback: Callable[[tuple[int, ...]], None] | None = None,
) -> dict[int, AuthorizedTranscriptArtifact]:
    """Authorize then stage every pending persisted issuer-IR transcript."""

    normalized = tuple(dict.fromkeys(ticker.strip().upper() for ticker in tickers))
    if not normalized:
        return {}
    placeholders = ",".join("?" for _ in normalized)
    doc_cols = {str(r[1]).lower() for r in conn.execute("PRAGMA table_info(documents)").fetchall()}
    has_period_end = "period_end" in doc_cols
    rows = conn.execute(
        (
            "SELECT id,ticker,file_path,sha256,raw_bytes_size,source_url,period_end "
            "FROM documents WHERE source_type=? AND doc_type=? "
            if has_period_end
            else "SELECT id,ticker,file_path,sha256,raw_bytes_size,source_url "
            "FROM documents WHERE source_type=? AND doc_type=? "
        )
        + f"AND ticker IN ({placeholders}) "  # nosec B608 -- placeholders derive from tuple length only
        + "AND id NOT IN (SELECT document_id FROM transcripts) ORDER BY id",
        (SourceType.IR_DOC.value, DocType.IR_TRANSCRIPT.value, *normalized),
    ).fetchall()
    planned: list[tuple[sqlite3.Row, TranscriptAcquisitionAuthorization, Path, Path, int, int]] = []
    pending_ids: list[int] = []
    for row in rows:
        if str(row["source_url"] or "").startswith("reindex_subdir:"):
            continue
        ticker_upper = str(row["ticker"]).upper()
        period = _resolve_document_period(
            conn,
            ticker=ticker_upper,
            file_path=str(row["file_path"]),
            period_end_raw=row["period_end"] if has_period_end else None,
        )
        if period is None:
            raise TranscriptAcquisitionDeniedError(
                f"document {row['id']} lacks canonical period identity"
            )
        year, quarter = period
        request = _document_request(
            entrypoint=entrypoint,
            ticker=ticker_upper,
            year=year,
            quarter=quarter,
            as_of=as_of,
        )
        authorization = authorize_transcript_request(conn, request)
        if authorization.reason is TranscriptAuthorizationReason.REPORTED_QUARTER_WINDOW_PENDING:
            pending_ids.append(int(row["id"]))
            continue
        if authorization.status is not TranscriptAuthorizationStatus.AUTHORIZED:
            raise TranscriptAcquisitionDeniedError(
                f"transcript acquisition denied: {authorization.reason.value} "
                f"({authorization.idempotency_key})"
            )
        relative = Path(str(row["file_path"]))
        source_path = (project_root / relative).resolve(strict=True)
        try:
            source_path.relative_to(project_root.resolve(strict=True))
        except ValueError as exc:
            raise TranscriptAcquisitionDeniedError(
                "persisted transcript path escapes project root"
            ) from exc
        expected_size = int(row["raw_bytes_size"])
        planned.append((row, authorization, source_path, relative, expected_size, int(row["id"])))

    if pending_ids and pending_callback is not None:
        pending_callback(tuple(pending_ids))

    if planned:
        private_root.mkdir(parents=True, exist_ok=True)
    artifacts: dict[int, AuthorizedTranscriptArtifact] = {}
    for row, authorization, source_path, relative, expected_size, document_id in planned:
        artifacts[document_id] = _stage_authorized_file(
            authorization,
            source_path=source_path,
            private_root=private_root,
            expected_sha256=str(row["sha256"]),
            expected_size_bytes=expected_size,
            source_url=str(row["source_url"]) if row["source_url"] is not None else None,
            canonical_document_path=relative,
            document_id=document_id,
        )
    return artifacts


class RetainedTranscriptAdmissionItem(BaseModel):
    """Per-transcript outcome from retained transcript receipt admission."""

    model_config = _STRICT_FROZEN

    transcript_id: int
    document_id: int
    ticker: str
    period_end: str
    fiscal_period_type: str
    file_path: str
    status: str
    provider: str | None = None
    receipt_id: str | None = None


class RetainedTranscriptAdmissionReport(BaseModel):
    """Summary of dry-run or applied retained transcript receipt admission."""

    model_config = _STRICT_FROZEN

    apply: bool
    as_of: date
    scanned: int
    already_bound: int
    eligible: int
    admitted: int
    skipped_counts: dict[str, int]
    items: tuple[RetainedTranscriptAdmissionItem, ...]


def _resolve_transcript_fiscal_coordinate(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    fiscal_period_type: str,
    period_end_raw: object,
) -> tuple[int, int] | None:
    """Resolve exact ``(fiscal_year, fiscal_quarter)`` matching ``fiscal_quarter_period_end``."""
    quarter_text = fiscal_period_type.strip().upper()
    if len(quarter_text) != 2 or not quarter_text.startswith("Q") or not quarter_text[1].isdigit():
        return None
    fiscal_quarter = int(quarter_text[1])
    if not 1 <= fiscal_quarter <= 4:
        return None
    raw_pe = str(period_end_raw or "").strip()
    if len(raw_pe) < 10:
        return None
    try:
        pe_date = date.fromisoformat(raw_pe[:10])
    except ValueError:
        return None
    row = conn.execute(
        "SELECT fiscal_year_end FROM tracked_companies "
        "WHERE UPPER(ticker)=? AND archived_at IS NULL LIMIT 1",
        (ticker.upper(),),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    fye_str = str(row[0]).strip()
    if len(fye_str) != 5 or fye_str[2] != "-" or not fye_str[:2].isdigit():
        return None
    fye_month = int(fye_str[:2])
    if not 1 <= fye_month <= 12:
        return None
    for fy in (pe_date.year - 1, pe_date.year, pe_date.year + 1):
        try:
            expected = fiscal_quarter_period_end(fy, fiscal_quarter, fye_month)
        except ValueError:
            continue
        if expected == pe_date:
            return (fy, fiscal_quarter)
    return None


def _safe_retained_file_path(
    project_root: Path, relative_raw: str
) -> tuple[Path, PurePosixPath] | None:
    """Validate and resolve a repo-relative file path without symlink escape."""
    rel_posix = PurePosixPath(relative_raw.replace("\\", "/"))
    if rel_posix.is_absolute() or ".." in rel_posix.parts or not rel_posix.parts:
        return None
    root = project_root.resolve(strict=True)
    lexical = root.joinpath(*rel_posix.parts)
    current = lexical
    while True:
        try:
            attributes = getattr(current.lstat(), "st_file_attributes", 0)
        except OSError:
            return None
        if current.is_symlink() or (isinstance(attributes, int) and bool(attributes & 0x400)):
            return None
        if current == root:
            break
        if root not in current.parents:
            return None
        current = current.parent
    try:
        resolved = lexical.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    if not resolved.is_file():
        return None
    return (resolved, rel_posix)


def admit_retained_transcripts(
    conn: sqlite3.Connection,
    *,
    project_root: Path,
    private_root: Path,
    tickers: Sequence[str] | None = None,
    transcript_ids: Sequence[int] | None = None,
    as_of: date | None = None,
    apply: bool = False,
) -> RetainedTranscriptAdmissionReport:
    """Verify retained on-disk transcript bytes and persist durable acquisition receipts.

    Fails closed when on-disk files are missing, symlink-escaped, size-mismatched,
    hash-mismatched, or outside tracked portfolio/requested authorization policy.
    """
    from pipeline.commitment_scan_receipts import current_transcript_scan_binding

    effective_as_of = as_of or current_utc_date()
    resolved_root = project_root.resolve(strict=True)
    relation = selected_transcripts_relation(conn).sql
    sql = (
        "SELECT t.id AS transcript_id, t.document_id, UPPER(t.ticker) AS ticker, "
        "t.fiscal_period_type, t.period_end, d.source_type, d.doc_type, d.file_path, "
        "d.sha256, d.raw_bytes_size, d.source_url "
        f"FROM {relation} AS t "  # nosec B608 -- repository-owned selection relation
        "JOIN documents AS d ON d.id = t.document_id "
        "WHERE t.is_current = 1 "
        "AND EXISTS (SELECT 1 FROM transcript_segments AS s WHERE s.transcript_id = t.id)"
    )
    params: list[object] = []
    if tickers:
        normalized_tickers = tuple(dict.fromkeys(t.strip().upper() for t in tickers if t.strip()))
        if normalized_tickers:
            placeholders = ",".join("?" for _ in normalized_tickers)
            sql += f" AND UPPER(t.ticker) IN ({placeholders})"  # nosec B608
            params.extend(normalized_tickers)
    if transcript_ids:
        unique_ids = tuple(dict.fromkeys(int(tid) for tid in transcript_ids))
        placeholders = ",".join("?" for _ in unique_ids)
        sql += f" AND t.id IN ({placeholders})"  # nosec B608
        params.extend(unique_ids)
    sql += " ORDER BY UPPER(t.ticker), t.period_end DESC, t.id"
    rows = conn.execute(sql, params).fetchall()

    items: list[RetainedTranscriptAdmissionItem] = []
    already_bound = 0
    eligible = 0
    admitted = 0
    skipped_counts: dict[str, int] = {}

    def _record_skip(
        row: sqlite3.Row,
        status: str,
        *,
        provider: str | None = None,
    ) -> None:
        skipped_counts[status] = skipped_counts.get(status, 0) + 1
        items.append(
            RetainedTranscriptAdmissionItem(
                transcript_id=int(row["transcript_id"]),
                document_id=int(row["document_id"]),
                ticker=str(row["ticker"]),
                period_end=str(row["period_end"] or ""),
                fiscal_period_type=str(row["fiscal_period_type"] or ""),
                file_path=str(row["file_path"] or ""),
                status=status,
                provider=provider,
            )
        )

    for row in rows:
        transcript_id = int(row["transcript_id"])
        document_id = int(row["document_id"])
        ticker = str(row["ticker"]).upper()
        period_end_str = str(row["period_end"] or "")
        fiscal_period_type = str(row["fiscal_period_type"] or "")
        file_path_raw = str(row["file_path"] or "")
        expected_sha256 = str(row["sha256"] or "")
        expected_size = int(row["raw_bytes_size"] or 0)
        doc_source_type = str(row["source_type"] or "")
        doc_type = str(row["doc_type"] or "")
        doc_source_url = None if row["source_url"] is None else str(row["source_url"])

        existing_binding = current_transcript_scan_binding(conn, transcript_id)
        if existing_binding is not None:
            already_bound += 1
            items.append(
                RetainedTranscriptAdmissionItem(
                    transcript_id=transcript_id,
                    document_id=document_id,
                    ticker=ticker,
                    period_end=period_end_str,
                    fiscal_period_type=fiscal_period_type,
                    file_path=file_path_raw,
                    status="already_bound",
                    receipt_id=existing_binding.transcript_acquisition_receipt_id,
                )
            )
            continue

        resolved_pair = _safe_retained_file_path(resolved_root, file_path_raw)
        if resolved_pair is None:
            _record_skip(row, "artifact_missing_or_unsafe")
            continue
        disk_path, rel_posix = resolved_pair
        disk_bytes = disk_path.read_bytes()
        if len(disk_bytes) != expected_size:
            _record_skip(row, "artifact_size_mismatch")
            continue
        if hashlib.sha256(disk_bytes).hexdigest() != expected_sha256:
            _record_skip(row, "artifact_hash_mismatch")
            continue

        coord = _resolve_transcript_fiscal_coordinate(
            conn,
            ticker=ticker,
            fiscal_period_type=fiscal_period_type,
            period_end_raw=row["period_end"],
        )
        if coord is None:
            _record_skip(row, "period_coordinate_unresolved")
            continue
        fiscal_year, fiscal_quarter = coord
        expected_txt_name = f"{ticker}_Q{fiscal_quarter}_{fiscal_year}.txt"

        if doc_source_type == SourceType.IR_DOC.value and doc_type == DocType.IR_TRANSCRIPT.value:
            is_issuer = is_canonical_issuer_transcript_path(rel_posix, ticker)
            is_named = rel_posix.name == expected_txt_name
            if not (is_issuer or is_named):
                _record_skip(row, "noncanonical_issuer_path")
                continue
            provider = TranscriptProvider.ISSUER_IR
            request = TranscriptAcquisitionRequest(
                schema_version="transcript-acquisition-request@2",
                entrypoint=TranscriptAcquisitionEntrypoint.INGEST_TRANSCRIPTS,
                canonical_ticker=ticker,
                fiscal_year=fiscal_year,
                fiscal_quarter=fiscal_quarter,
                as_of=effective_as_of,
                source_type=SourceType.IR_DOC,
                document_type=DocType.EARNINGS_CALL_TRANSCRIPT,
                provider=provider,
                owner_requested=True,
                existing_artifact=False,
                existing_artifact_behavior=ExistingArtifactBehavior.REFRESH,
                source_policy_version=TRANSCRIPT_ACQUISITION_POLICY_VERSION,
                source_regime_identity=COMBINED_SOURCE_REGIME_IDENTITY,
            )
            authorization = authorize_transcript_request(conn, request)
            if authorization.status is not TranscriptAuthorizationStatus.AUTHORIZED:
                _record_skip(row, f"denied:{authorization.reason.value}", provider=provider.value)
                continue
            if not apply:
                eligible += 1
                items.append(
                    RetainedTranscriptAdmissionItem(
                        transcript_id=transcript_id,
                        document_id=document_id,
                        ticker=ticker,
                        period_end=period_end_str,
                        fiscal_period_type=fiscal_period_type,
                        file_path=file_path_raw,
                        status="eligible",
                        provider=provider.value,
                    )
                )
                continue
            savepoint = f"admit_retained_{transcript_id}"
            conn.execute(f"SAVEPOINT {savepoint}")  # nosec B608 -- integer transcript_id
            try:
                private_root.mkdir(parents=True, exist_ok=True)
                artifact = _stage_authorized_file(
                    authorization,
                    source_path=disk_path,
                    private_root=private_root,
                    expected_sha256=expected_sha256,
                    expected_size_bytes=expected_size,
                    source_url=doc_source_url,
                    canonical_document_path=Path(rel_posix.as_posix()),
                    document_id=document_id,
                )
                receipt_id = persist_authorized_transcript_artifact(
                    conn,
                    artifact,
                    project_root=resolved_root,
                    trusted_staging_root=private_root,
                )
                if current_transcript_scan_binding(conn, transcript_id) is None:
                    raise TranscriptAcquisitionDeniedError(
                        f"transcript {transcript_id} failed post-admission scan binding verification"
                    )
            except (OSError, ValueError, sqlite3.Error, TranscriptAcquisitionDeniedError) as exc:
                conn.execute(f"ROLLBACK TO {savepoint}")  # nosec B608
                conn.execute(f"RELEASE {savepoint}")  # nosec B608
                _record_skip(row, f"error:{type(exc).__name__}", provider=provider.value)
                continue
            conn.execute(f"RELEASE {savepoint}")  # nosec B608
            eligible += 1
            admitted += 1
            items.append(
                RetainedTranscriptAdmissionItem(
                    transcript_id=transcript_id,
                    document_id=document_id,
                    ticker=ticker,
                    period_end=period_end_str,
                    fiscal_period_type=fiscal_period_type,
                    file_path=file_path_raw,
                    status="admitted",
                    provider=provider.value,
                    receipt_id=receipt_id,
                )
            )
            continue

        if doc_source_type == SourceType.TRANSCRIPT_AUDIO.value and doc_source_url is None:
            parsed_name = parse_canonical_transcript_filename(rel_posix)
            allowed_parents = {
                PurePosixPath("transcripts/raw"),
                PurePosixPath("transcripts/processed"),
                PurePosixPath("transcripts/raw/.evidence") / expected_sha256,
            }
            if (
                parsed_name != (ticker, fiscal_year, fiscal_quarter)
                or rel_posix.parent not in allowed_parents
            ):
                _record_skip(row, "noncanonical_aggregator_path")
                continue
            provider = TranscriptProvider.ROIC
            request = TranscriptAcquisitionRequest(
                schema_version="transcript-acquisition-request@2",
                entrypoint=TranscriptAcquisitionEntrypoint.INGEST_TRANSCRIPTS,
                canonical_ticker=ticker,
                fiscal_year=fiscal_year,
                fiscal_quarter=fiscal_quarter,
                as_of=effective_as_of,
                source_type=SourceType.IR_DOC,
                document_type=DocType.EARNINGS_CALL_TRANSCRIPT,
                provider=provider,
                owner_requested=True,
                existing_artifact=False,
                existing_artifact_behavior=ExistingArtifactBehavior.REFRESH,
                source_policy_version=TRANSCRIPT_ACQUISITION_POLICY_VERSION,
                source_regime_identity=COMBINED_SOURCE_REGIME_IDENTITY,
            )
            authorization = authorize_transcript_request(conn, request)
            if authorization.status is not TranscriptAuthorizationStatus.AUTHORIZED:
                _record_skip(row, f"denied:{authorization.reason.value}", provider=provider.value)
                continue
            if not apply:
                eligible += 1
                items.append(
                    RetainedTranscriptAdmissionItem(
                        transcript_id=transcript_id,
                        document_id=document_id,
                        ticker=ticker,
                        period_end=period_end_str,
                        fiscal_period_type=fiscal_period_type,
                        file_path=file_path_raw,
                        status="eligible",
                        provider=provider.value,
                    )
                )
                continue
            savepoint = f"admit_retained_{transcript_id}"
            conn.execute(f"SAVEPOINT {savepoint}")  # nosec B608 -- integer transcript_id
            try:
                private_root.mkdir(parents=True, exist_ok=True)
                artifact = stage_authorized_payload(
                    authorization,
                    payload=disk_bytes,
                    private_root=private_root,
                    source_url=None,
                    canonical_document_path=Path("transcripts/raw") / expected_txt_name,
                )
                receipt_id = persist_authorized_transcript_artifact(
                    conn,
                    artifact,
                    project_root=resolved_root,
                    trusted_staging_root=private_root,
                )
                if current_transcript_scan_binding(conn, transcript_id) is None:
                    raise TranscriptAcquisitionDeniedError(
                        f"transcript {transcript_id} failed post-admission scan binding verification"
                    )
            except (OSError, ValueError, sqlite3.Error, TranscriptAcquisitionDeniedError) as exc:
                conn.execute(f"ROLLBACK TO {savepoint}")  # nosec B608
                conn.execute(f"RELEASE {savepoint}")  # nosec B608
                _record_skip(row, f"error:{type(exc).__name__}", provider=provider.value)
                continue
            conn.execute(f"RELEASE {savepoint}")  # nosec B608
            eligible += 1
            admitted += 1
            items.append(
                RetainedTranscriptAdmissionItem(
                    transcript_id=transcript_id,
                    document_id=document_id,
                    ticker=ticker,
                    period_end=period_end_str,
                    fiscal_period_type=fiscal_period_type,
                    file_path=file_path_raw,
                    status="admitted",
                    provider=provider.value,
                    receipt_id=receipt_id,
                )
            )
            continue

        _record_skip(row, "unsupported_source_metadata")

    if apply and admitted > 0:
        conn.commit()
    return RetainedTranscriptAdmissionReport(
        apply=apply,
        as_of=effective_as_of,
        scanned=len(rows),
        already_bound=already_bound,
        eligible=eligible,
        admitted=admitted,
        skipped_counts=dict(sorted(skipped_counts.items())),
        items=tuple(items),
    )


__all__ = [
    "COMBINED_SOURCE_REGIME_IDENTITY",
    "AuthorizedTranscriptArtifact",
    "RetainedTranscriptAdmissionItem",
    "RetainedTranscriptAdmissionReport",
    "TranscriptAcquisitionDeniedError",
    "admit_retained_transcripts",
    "authorize_transcript_request",
    "issuer_transcript_source_url_is_authorized",
    "load_authorized_transcript_receipt",
    "load_authorized_transcript_replay",
    "persist_authorized_transcript_artifact",
    "project_root_for_database",
    "read_authorized_transcript",
    "register_transcript_receipt_sqlite_functions",
    "require_authorized_transcript_request",
    "require_persisted_authorized_transcript_artifact",
    "stage_authorized_payload",
    "stage_pending_issuer_transcripts",
    "transcript_acquisition_receipt_id",
]
