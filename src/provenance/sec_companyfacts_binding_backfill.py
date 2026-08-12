"""Bounded SEC CompanyFacts capture for legacy accession evidence bindings.

This backfill is intentionally narrower than the normal SEC XBRL ingestion
path: it creates aggregate snapshot ``documents`` rows and immutable evidence bindings, but
never financial facts or the mutable latest-companyfacts compatibility cache.
Dry runs are offline and read-only.  Apply runs fetch fresh official SEC bytes.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

import requests
from pydantic import BaseModel, ConfigDict, Field, field_validator

from log_redact import redact
from pipeline.sec_xbrl import (
    FetchedCompanyFacts,
    enumerate_companyfacts_accessions,
    fetch_companyfacts,
    upsert_companyfacts_snapshot_document,
)
from provenance.issuer_registry import IssuerRegistry, UnresolvedIssuerIdentityError
from provenance.sec_companyfacts_capture import (
    SecCompanyFactsCaptureRequest,
    capture_sec_companyfacts,
    parse_companyfacts_body,
    supported_companyfacts_accessions,
)

_Mode = Literal["dry_run", "apply"]
_MINIMUM_SEC_INTERVAL_SECONDS = 0.15
_REQUIRED_TABLES = frozenset(
    {
        "documents",
        "evidence_blob_location_observations",
        "evidence_content_blobs",
        "evidence_document_observation_links",
        "evidence_document_versions",
        "evidence_extraction_runs",
        "evidence_nodes",
        "evidence_source_observations",
        "issuer_entities",
        "issuer_identifier_assertions",
        "issuer_identifier_resolution_outcomes",
        "legacy_document_evidence_binding_revisions",
        "legacy_issuer_binding_revisions",
    }
)
_REQUIRED_VIEWS = frozenset(
    {
        "v_evidence_blob_locations_current",
        "v_legacy_document_evidence_bindings_current",
    }
)


class CompanyFactsFetcher(Protocol):
    def __call__(self, cik: str, *, timeout: int = 30) -> FetchedCompanyFacts: ...


class CompanyFactsBackfillError(RuntimeError):
    """The backfill cannot safely continue."""


class CompanyFactsBackfillHardStopError(CompanyFactsBackfillError):
    """SEC rejected authorization and the run must stop without retrying."""


class CompanyFactsBindingBackfillRequest(BaseModel):
    """Validated controls for one offline plan or bounded network batch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    blob_root: Path
    checkpoint_root: Path
    apply: bool = False
    tickers: tuple[str, ...] = ()
    batch_size: int = Field(default=10, ge=1, le=500)
    task_id: str = Field(
        default="sec-companyfacts-binding-backfill",
        pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$",
    )
    request_timeout_seconds: int = Field(default=30, ge=1, le=300)
    minimum_request_interval_seconds: float = Field(
        default=_MINIMUM_SEC_INTERVAL_SECONDS,
        ge=_MINIMUM_SEC_INTERVAL_SECONDS,
        le=60.0,
    )

    @field_validator("tickers")
    @classmethod
    def _normalized_tickers(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.strip().upper() for value in values)
        if any(not value or len(value) > 32 for value in normalized):
            raise ValueError("tickers must contain one to 32 non-whitespace characters")
        if len(normalized) != len(set(normalized)):
            raise ValueError("tickers must be unique")
        return tuple(sorted(normalized))


class CompanyFactsBindingTarget(BaseModel):
    """One exact current ticker, issuer, and SEC registrant identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    issuer_id: str
    normalized_cik: str = Field(pattern=r"^\d{10}$")

    @property
    def target_key(self) -> str:
        return f"{self.ticker}|{self.issuer_id}|{self.normalized_cik}"


class CompanyFactsBindingItemResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    issuer_id: str | None = None
    normalized_cik: str | None = None
    outcome: Literal["planned", "captured", "identity_blocked"]
    reason: str | None = None
    supported_accessions: int = Field(ge=0)
    documents_created: int = Field(ge=0)
    bindings_created: int = Field(ge=0)
    bindings_unchanged: int = Field(ge=0)
    records_created: int = Field(ge=0)
    records_replayed: int = Field(ge=0)
    blob_sha256: str | None = None


class CompanyFactsBindingBackfillSummary(BaseModel):
    """One JSON-safe summary emitted to stdout by the execution wrapper."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    mode: _Mode
    dry_run: bool
    run_at: datetime
    batch_size: int
    candidates_total: int = Field(ge=0)
    identity_blocked: int = Field(ge=0)
    already_completed: int = Field(ge=0)
    considered: int = Field(ge=0)
    fetched: int = Field(ge=0)
    supported_accessions: int = Field(ge=0)
    documents_created: int = Field(ge=0)
    bindings_created: int = Field(ge=0)
    bindings_unchanged: int = Field(ge=0)
    records_created: int = Field(ge=0)
    records_replayed: int = Field(ge=0)
    has_more: bool
    items: tuple[CompanyFactsBindingItemResult, ...]


class CompanyFactsBindingCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    completed_target_keys: tuple[str, ...]
    updated_at: datetime


def emit_structured_event(event: str, **fields: object) -> None:
    """Write one sorted JSON object to stderr, never stdout."""

    sys.stderr.write(json.dumps({"event": event, **fields}, default=str, sort_keys=True) + "\n")


def backfill_sec_companyfacts_bindings(
    conn: sqlite3.Connection,
    request: CompanyFactsBindingBackfillRequest,
    *,
    fetcher: CompanyFactsFetcher = fetch_companyfacts,
    sleeper: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> CompanyFactsBindingBackfillSummary:
    """Plan or capture one bounded batch without ever writing financial facts."""

    _require_schema(conn)
    run_at = _utc(now())
    targets, blocked_items = _resolve_targets(
        conn,
        request.tickers,
        knowledge_at=run_at,
    )
    checkpoint_path = request.checkpoint_root / request.task_id / "state.json"
    checkpoint = (
        _read_checkpoint(checkpoint_path, request.task_id)
        if request.apply
        else CompanyFactsBindingCheckpoint(
            task_id=request.task_id,
            completed_target_keys=(),
            updated_at=run_at,
        )
    )
    completed = set(checkpoint.completed_target_keys)
    pending = [target for target in targets if target.target_key not in completed]
    batch = pending[: request.batch_size]

    if not request.apply:
        items = (*blocked_items, *(_planned_item(target) for target in batch))
        summary = _summary(
            request,
            run_at=run_at,
            targets=targets,
            blocked_items=blocked_items,
            completed=completed,
            has_more=len(pending) > len(batch),
            items=items,
        )
        emit_structured_event(
            "sec_companyfacts_binding_backfill_dry_run",
            task_id=request.task_id,
            candidates_total=len(targets) + len(blocked_items),
            identity_blocked=len(blocked_items),
            considered=summary.considered,
            has_more=summary.has_more,
        )
        return summary

    results: list[CompanyFactsBindingItemResult] = []
    for request_index, target in enumerate(batch):
        if request_index:
            sleeper(request.minimum_request_interval_seconds)
        fetched = _fetch_fresh(
            fetcher,
            target,
            timeout=request.request_timeout_seconds,
        )
        result = _capture_target(conn, request, target, fetched)
        results.append(result)
        completed.add(target.target_key)
        checkpoint = CompanyFactsBindingCheckpoint(
            task_id=request.task_id,
            completed_target_keys=tuple(sorted(completed)),
            updated_at=_utc(now()),
        )
        _write_checkpoint(checkpoint_path, checkpoint)
        emit_structured_event(
            "sec_companyfacts_binding_target_captured",
            task_id=request.task_id,
            ticker=target.ticker,
            normalized_cik=target.normalized_cik,
            supported_accessions=result.supported_accessions,
            bindings_created=result.bindings_created,
            bindings_unchanged=result.bindings_unchanged,
        )

    remaining = [target for target in targets if target.target_key not in completed]
    summary = _summary(
        request,
        run_at=run_at,
        targets=targets,
        blocked_items=blocked_items,
        completed=completed,
        has_more=bool(remaining),
        items=(*blocked_items, *results),
    )
    emit_structured_event(
        "sec_companyfacts_binding_backfill_completed",
        task_id=request.task_id,
        considered=summary.considered,
        fetched=summary.fetched,
        has_more=summary.has_more,
    )
    return summary


def _capture_target(
    conn: sqlite3.Connection,
    request: CompanyFactsBindingBackfillRequest,
    target: CompanyFactsBindingTarget,
    fetched: FetchedCompanyFacts,
) -> CompanyFactsBindingItemResult:
    payload = parse_companyfacts_body(
        fetched.raw_body,
        expected_cik=target.normalized_cik,
    )
    legacy_payload = payload.model_dump(mode="json", by_alias=True)
    accessions = enumerate_companyfacts_accessions(legacy_payload)
    supported_accessions = set(supported_companyfacts_accessions(payload))
    digest = hashlib.sha256(fetched.raw_body).hexdigest()
    before_documents = int(conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
    try:
        conn.execute("BEGIN IMMEDIATE")
        snapshot_document_id = upsert_companyfacts_snapshot_document(
            conn,
            ticker=target.ticker,
            digest=digest,
            normalized_cik=target.normalized_cik,
            raw_body=fetched.raw_body,
            snapshot_root=request.blob_root,
            fetched_at=fetched.retrieved_at,
        )
        if not supported_accessions.issubset(accessions):
            raise CompanyFactsBackfillError("validated accession inventory is inconsistent")
        captured = capture_sec_companyfacts(
            conn,
            SecCompanyFactsCaptureRequest(
                ticker=target.ticker,
                normalized_cik=target.normalized_cik,
                issuer_id=target.issuer_id,
                source_url=fetched.source_url,
                raw_body=fetched.raw_body,
                payload=payload,
                snapshot_document_id=snapshot_document_id,
                blob_root=request.blob_root,
                observed_at=fetched.observed_at,
                retrieved_at=fetched.retrieved_at,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    after_documents = int(conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
    return CompanyFactsBindingItemResult(
        ticker=target.ticker,
        issuer_id=target.issuer_id,
        normalized_cik=target.normalized_cik,
        outcome="captured",
        supported_accessions=len(supported_accessions),
        documents_created=after_documents - before_documents,
        bindings_created=captured.bindings_created,
        bindings_unchanged=captured.bindings_unchanged,
        records_created=captured.records_created,
        records_replayed=captured.records_replayed,
        blob_sha256=captured.blob_sha256,
    )


def _fetch_fresh(
    fetcher: CompanyFactsFetcher,
    target: CompanyFactsBindingTarget,
    *,
    timeout: int,
) -> FetchedCompanyFacts:
    try:
        fetched = fetcher(target.normalized_cik, timeout=timeout)
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        if status_code in {401, 403}:
            emit_structured_event(
                "sec_companyfacts_authorization_hard_stop",
                ticker=target.ticker,
                normalized_cik=target.normalized_cik,
                http_status=status_code,
            )
            raise CompanyFactsBackfillHardStopError(
                "SEC CompanyFacts authorization hard stop; verify the declared User-Agent"
            ) from None
        raise CompanyFactsBackfillError(f"SEC CompanyFacts request failed: {redact(exc)}") from None
    expected_url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{target.normalized_cik}.json"
    if fetched.source_url != expected_url:
        raise CompanyFactsBackfillError(
            "CompanyFacts fetcher returned a non-canonical SEC source URL"
        )
    return fetched


def _resolve_targets(
    conn: sqlite3.Connection,
    requested_tickers: tuple[str, ...],
    *,
    knowledge_at: datetime,
) -> tuple[
    tuple[CompanyFactsBindingTarget, ...],
    tuple[CompanyFactsBindingItemResult, ...],
]:
    tickers = (
        requested_tickers
        if requested_tickers
        else tuple(
            str(row[0]).strip().upper()
            for row in conn.execute(
                "SELECT DISTINCT UPPER(ticker) FROM documents "
                "WHERE source_type = 'sec_xbrl' ORDER BY UPPER(ticker)"
            )
            if str(row[0]).strip()
        )
    )
    registry = IssuerRegistry(conn)
    targets: list[CompanyFactsBindingTarget] = []
    blocked: list[CompanyFactsBindingItemResult] = []
    for ticker in tickers:
        try:
            ticker_issuer = registry.canonicalize_recorded_issuer(
                f"legacy-ticker:{ticker}",
                knowledge_at=knowledge_at,
            )
        except UnresolvedIssuerIdentityError as exc:
            blocked.append(
                _blocked_item(
                    ticker,
                    f"no exact current canonical issuer: {type(exc).__name__}",
                )
            )
            continue
        if ticker_issuer.material_dissent:
            blocked.append(
                _blocked_item(
                    ticker,
                    "canonical issuer has material dissent",
                    issuer_id=ticker_issuer.issuer_id,
                )
            )
            continue
        cik_rows = conn.execute(
            """
            SELECT assertion.normalized_value
            FROM issuer_identifier_resolution_outcomes AS resolution
            JOIN issuer_identifier_assertions AS assertion
              ON assertion.assertion_id = resolution.selected_assertion_id
            WHERE resolution.outcome = 'selected'
              AND assertion.identifier_type = 'sec_cik'
              AND assertion.issuer_id = ?
              AND resolution.knowledge_at <= ?
              AND assertion.knowledge_at <= ?
              AND NOT EXISTS (
                  SELECT 1
                  FROM issuer_identifier_resolution_outcomes AS newer
                  WHERE newer.resolution_key = resolution.resolution_key
                    AND newer.knowledge_at <= ?
                    AND newer.revision > resolution.revision
              )
            ORDER BY assertion.normalized_value
            """,
            (
                ticker_issuer.issuer_id,
                knowledge_at,
                knowledge_at,
                knowledge_at,
            ),
        ).fetchall()
        normalized_ciks = tuple(sorted({str(row[0]) for row in cik_rows}))
        if len(normalized_ciks) != 1:
            blocked.append(
                _blocked_item(
                    ticker,
                    f"requires exactly one current SEC CIK; found {len(normalized_ciks)}",
                    issuer_id=ticker_issuer.issuer_id,
                )
            )
            continue
        normalized_cik = normalized_ciks[0]
        try:
            cik_issuer = registry.resolve_identifier(
                "sec_cik",
                normalized_cik,
                knowledge_at=knowledge_at,
            )
        except UnresolvedIssuerIdentityError as exc:
            blocked.append(
                _blocked_item(
                    ticker,
                    f"current SEC CIK is unresolved: {type(exc).__name__}",
                    issuer_id=ticker_issuer.issuer_id,
                    normalized_cik=normalized_cik,
                )
            )
            continue
        if cik_issuer.issuer_id != ticker_issuer.issuer_id or cik_issuer.material_dissent:
            blocked.append(
                _blocked_item(
                    ticker,
                    "ticker and current SEC CIK do not resolve exactly",
                    issuer_id=ticker_issuer.issuer_id,
                    normalized_cik=normalized_cik,
                )
            )
            continue
        targets.append(
            CompanyFactsBindingTarget(
                ticker=ticker,
                issuer_id=ticker_issuer.issuer_id,
                normalized_cik=normalized_cik,
            )
        )
    return tuple(targets), tuple(blocked)


def _planned_item(
    target: CompanyFactsBindingTarget,
) -> CompanyFactsBindingItemResult:
    return CompanyFactsBindingItemResult(
        ticker=target.ticker,
        issuer_id=target.issuer_id,
        normalized_cik=target.normalized_cik,
        outcome="planned",
        supported_accessions=0,
        documents_created=0,
        bindings_created=0,
        bindings_unchanged=0,
        records_created=0,
        records_replayed=0,
    )


def _blocked_item(
    ticker: str,
    reason: str,
    *,
    issuer_id: str | None = None,
    normalized_cik: str | None = None,
) -> CompanyFactsBindingItemResult:
    return CompanyFactsBindingItemResult(
        ticker=ticker,
        issuer_id=issuer_id,
        normalized_cik=normalized_cik,
        outcome="identity_blocked",
        reason=reason,
        supported_accessions=0,
        documents_created=0,
        bindings_created=0,
        bindings_unchanged=0,
        records_created=0,
        records_replayed=0,
    )


def _summary(
    request: CompanyFactsBindingBackfillRequest,
    *,
    run_at: datetime,
    targets: tuple[CompanyFactsBindingTarget, ...],
    blocked_items: tuple[CompanyFactsBindingItemResult, ...],
    completed: set[str],
    has_more: bool,
    items: tuple[CompanyFactsBindingItemResult, ...],
) -> CompanyFactsBindingBackfillSummary:
    return CompanyFactsBindingBackfillSummary(
        task_id=request.task_id,
        mode="apply" if request.apply else "dry_run",
        dry_run=not request.apply,
        run_at=run_at,
        batch_size=request.batch_size,
        candidates_total=len(targets) + len(blocked_items),
        identity_blocked=len(blocked_items),
        already_completed=sum(target.target_key in completed for target in targets),
        considered=sum(item.outcome != "identity_blocked" for item in items),
        fetched=sum(item.outcome == "captured" for item in items),
        supported_accessions=sum(item.supported_accessions for item in items),
        documents_created=sum(item.documents_created for item in items),
        bindings_created=sum(item.bindings_created for item in items),
        bindings_unchanged=sum(item.bindings_unchanged for item in items),
        records_created=sum(item.records_created for item in items),
        records_replayed=sum(item.records_replayed for item in items),
        has_more=has_more,
        items=items,
    )


def _require_schema(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT type, name FROM sqlite_master WHERE type IN ('table', 'view')"
    ).fetchall()
    tables = {str(row[1]) for row in rows if str(row[0]) == "table"}
    views = {str(row[1]) for row in rows if str(row[0]) == "view"}
    missing_tables = sorted(_REQUIRED_TABLES - tables)
    missing_views = sorted(_REQUIRED_VIEWS - views)
    if missing_tables or missing_views:
        raise CompanyFactsBackfillError(
            "SEC CompanyFacts binding migrations are unavailable "
            f"(missing_tables={missing_tables}, missing_views={missing_views})"
        )
    document_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(documents)")}
    required_document_columns = {
        "accession_number",
        "filing_date",
        "source_quality_tier",
    }
    missing_columns = sorted(required_document_columns - document_columns)
    if missing_columns:
        raise CompanyFactsBackfillError(
            f"documents table is missing SEC accession columns {missing_columns}"
        )


def _read_checkpoint(
    path: Path,
    task_id: str,
) -> CompanyFactsBindingCheckpoint:
    if not path.exists():
        return CompanyFactsBindingCheckpoint(
            task_id=task_id,
            completed_target_keys=(),
            updated_at=datetime.now(UTC),
        )
    try:
        checkpoint = CompanyFactsBindingCheckpoint.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise CompanyFactsBackfillError("SEC CompanyFacts binding checkpoint is invalid") from exc
    if checkpoint.task_id != task_id:
        raise CompanyFactsBackfillError("SEC CompanyFacts binding checkpoint task identity changed")
    return checkpoint


def _write_checkpoint(
    path: Path,
    checkpoint: CompanyFactsBindingCheckpoint,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(checkpoint.model_dump_json())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        temporary = Path(temporary_name)
        if temporary.exists():
            temporary.unlink()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
