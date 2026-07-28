"""Fetch, preserve, and reconcile one exhaustive SEC submissions inventory."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

import requests
from pydantic import BaseModel, ConfigDict, Field, model_validator

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from filings.edgar_fetch import (  # noqa: E402
    HardStopError,
    SourceContractError,
    TransientError,
)
from filings.sec_filing_package_inventory import (  # noqa: E402
    ParsedSecFilingPackage,
    SecFilingPackageContractError,
    filing_package_index_url,
    filing_package_manifest_url,
    parse_sec_filing_package_inventory,
)
from filings.sec_submissions_inventory import (  # noqa: E402
    HistoricalComponent,
    SecFilingInventoryEntry,
    SecInventoryContractError,
    advertised_historical_components,
    historical_component_url,
    parse_sec_submissions_inventory,
)
from provenance.evidence_ledger import (  # noqa: E402
    ContentBlob,
    EvidenceLedger,
    SourceObservation,
)
from provenance.evidence_links import (  # noqa: E402
    BlobLocationObservation,
    EvidenceLinkLedger,
)
from provenance.inventory_identity import (  # noqa: E402
    InventoryIdentityError,
    issuer_registry_available,
    resolve_sec_inventory_subject,
)
from provenance.source_coverage_reconcile import (  # noqa: E402
    ExpectedDocumentImport,
    ExplicitAbsence,
    InventoryComponentImport,
    SourceCoverageImport,
    reconcile_source_coverage,
)
from runtime.job_runtime import JobAlreadyRunningError, JobLock  # noqa: E402
from sec_identity import sec_user_agent  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

_TIMEOUT = (10, 60)
_COLLECTOR = "sync-sec-filing-inventory@2"
_DEFAULT_PACKAGE_LIMIT = 250
_SEC_REQUEST_DELAY_SECONDS = 0.25
_PACKAGE_SCOPE_POLICY_VERSION = "company-report-package-scope@1"
_ISSUER_REPORT_FORMS = frozenset(
    {
        "10-K",
        "10-K/A",
        "10-Q",
        "10-Q/A",
        "11-K",
        "11-K/A",
        "20-F",
        "20-F/A",
        "40-F",
        "40-F/A",
        "424B1",
        "424B2",
        "424B3",
        "424B4",
        "424B5",
        "6-K",
        "6-K/A",
        "8-A12B",
        "8-A12G",
        "8-K",
        "8-K/A",
        "ARS",
        "CERT",
        "CORRESP",
        "DEF 14A",
        "DEFA14A",
        "DRS",
        "DRS/A",
        "DRSLTR",
        "F-1",
        "F-1/A",
        "F-3",
        "F-3/A",
        "F-4",
        "F-4/A",
        "PRE 14A",
        "S-1",
        "S-1/A",
        "S-1MEF",
        "S-3",
        "S-3/A",
        "S-3MEF",
        "S-4",
        "S-4/A",
        "S-8",
        "SD",
        "SD/A",
    }
)
_EXTERNAL_OR_ADMINISTRATIVE_FORMS = frozenset(
    {
        "3",
        "3/A",
        "4",
        "4/A",
        "5",
        "5/A",
        "144",
        "144/A",
        "EFFECT",
        "SC 13D",
        "SC 13D/A",
        "SC 13G",
        "SC 13G/A",
        "SCHEDULE 13D",
        "SCHEDULE 13D/A",
        "SCHEDULE 13G",
        "SCHEDULE 13G/A",
        "SEC STAFF LETTER",
        "UPLOAD",
    }
)


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _CheckpointEntry(_ClosedModel):
    accession_number: str
    index_sha256: str = Field(min_length=64, max_length=64)
    manifest_sha256: str = Field(min_length=64, max_length=64)


class _PackageCheckpoint(_ClosedModel):
    cik: str
    entries: tuple[_CheckpointEntry, ...] = ()

    @model_validator(mode="after")
    def _validate_entries(self) -> Self:
        accessions = [item.accession_number for item in self.entries]
        if len(accessions) != len(set(accessions)):
            raise ValueError("package checkpoint accessions must be unique")
        return self


class _PackageComponent(_ClosedModel):
    accession_number: str
    component_kind: Literal["package_index", "filing_manifest", "validation"]
    source_url: str
    body: bytes | None = None
    failure_reason: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def _validate_outcome(self) -> Self:
        if (self.body is None) == (self.failure_reason is None):
            raise ValueError("package component requires exactly one body or failure_reason")
        return self


class PackageFailureSummary(_ClosedModel):
    accession_number: str
    component_kind: Literal["package_index", "filing_manifest", "validation"]
    reason_code: str


def summarize_package_failures(
    failures: tuple[_PackageComponent, ...],
    *,
    sample_limit: int = 20,
) -> tuple[PackageFailureSummary, ...]:
    """Return bounded diagnostics without emitting request URLs or response bodies."""

    if sample_limit < 0:
        raise ValueError("package failure sample limit must be non-negative")
    return tuple(
        PackageFailureSummary(
            accession_number=component.accession_number,
            component_kind=component.component_kind,
            reason_code=component.failure_reason or "unknown_failure",
        )
        for component in failures[:sample_limit]
    )


class _PackageCollection(_ClosedModel):
    packages: tuple[ParsedSecFilingPackage, ...]
    components: tuple[_PackageComponent, ...]
    deferred_accession_count: int = Field(ge=0)


class FilingPackageScope(_ClosedModel):
    issuer_reports: tuple[SecFilingInventoryEntry, ...]
    external_or_administrative: tuple[SecFilingInventoryEntry, ...]
    unclassified: tuple[SecFilingInventoryEntry, ...]


def partition_filing_package_scope(
    filings: tuple[SecFilingInventoryEntry, ...],
) -> FilingPackageScope:
    """Classify SEC feed entries without treating third-party filings as issuer reports."""

    issuer_reports: list[SecFilingInventoryEntry] = []
    external: list[SecFilingInventoryEntry] = []
    unclassified: list[SecFilingInventoryEntry] = []
    for filing in filings:
        normalized_form = filing.form_type.strip().upper()
        if normalized_form in _ISSUER_REPORT_FORMS:
            issuer_reports.append(filing)
        elif normalized_form in _EXTERNAL_OR_ADMINISTRATIVE_FORMS:
            external.append(filing)
        else:
            unclassified.append(filing)
    return FilingPackageScope(
        issuer_reports=tuple(issuer_reports),
        external_or_administrative=tuple(external),
        unclassified=tuple(unclassified),
    )


class SyncResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: str
    ticker: str
    issuer_id: str
    filing_count: int = Field(ge=0)
    issuer_report_filing_count: int = Field(default=0, ge=0)
    external_or_administrative_filing_count: int = Field(default=0, ge=0)
    unclassified_form_types: tuple[str, ...] = ()
    attachment_count: int = Field(default=0, ge=0)
    component_count: int = Field(ge=0)
    deferred_accession_count: int = Field(default=0, ge=0)
    package_failure_count: int = Field(default=0, ge=0)
    package_failure_samples: tuple[PackageFailureSummary, ...] = ()
    complete: bool
    issue_codes: tuple[str, ...]
    snapshot_id: str | None = None
    records_created: int = Field(default=0, ge=0)


def _event(event: str, **fields: object) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")


def _fetch(session: requests.Session, url: str, user_agent: str) -> bytes:
    time.sleep(_SEC_REQUEST_DELAY_SECONDS)
    try:
        response = session.get(
            url,
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=_TIMEOUT,
        )
    except requests.Timeout as exc:
        raise TransientError(f"timeout fetching SEC submissions component: {url}") from exc
    except requests.RequestException as exc:
        raise TransientError(f"network failure fetching SEC submissions component: {url}") from exc
    if response.status_code in {401, 403}:
        raise HardStopError(f"SEC returned {response.status_code}; verify the declared User-Agent")
    if response.status_code == 429 or response.status_code >= 500:
        raise TransientError(f"SEC returned transient status {response.status_code} for {url}")
    if response.status_code != 200:
        raise SourceContractError(f"SEC returned contract status {response.status_code} for {url}")
    return response.content


def _capture_component(
    conn: sqlite3.Connection,
    *,
    body: bytes,
    url: str,
    blob_root: Path,
    config_sha: str,
    recorded_at: datetime,
    media_type: str = "application/json",
    source_kind: str = "sec_submissions",
) -> str:
    digest = hashlib.sha256(body).hexdigest()
    path = blob_root / digest[:2] / digest
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = path.read_bytes()
        if hashlib.sha256(existing).hexdigest() != digest:
            raise RuntimeError("existing SEC evidence blob fails hash verification")
    else:
        path.write_bytes(body)
    ledger = EvidenceLedger(conn)
    blob = conn.execute(
        "SELECT sha256, byte_size FROM evidence_content_blobs WHERE sha256 = ?",
        (digest,),
    ).fetchone()
    if blob is None:
        ledger.persist(
            ContentBlob(
                sha256=digest,
                byte_size=len(body),
                media_type=media_type,
                storage_uri=path.resolve().as_uri(),
                recorded_at=recorded_at,
            )
        )
    elif int(blob[1]) != len(body):
        raise ValueError("existing SEC evidence blob metadata conflicts")
    observation_seed = hashlib.sha256(f"{url}\0{digest}\0{config_sha}".encode()).hexdigest()
    observation_id = f"source-observation:{observation_seed}"
    observation = conn.execute(
        "SELECT observation_id FROM evidence_source_observations WHERE observation_id = ?",
        (observation_id,),
    ).fetchone()
    if observation is None:
        ledger.persist(
            SourceObservation(
                observation_id=observation_id,
                idempotency_key=observation_id,
                source_kind=source_kind,
                source_url=url,
                blob_sha256=digest,
                source_published_at=None,
                filing_at=None,
                accepted_at=None,
                observed_at=recorded_at,
                retrieved_at=recorded_at,
                retrieval_config_sha256=config_sha,
                collector_code_version=_COLLECTOR,
            )
        )
    location_id = (
        "blob-location:"
        + hashlib.sha256(f"{digest}\0{path.resolve().as_uri()}".encode()).hexdigest()
    )
    if (
        conn.execute(
            "SELECT 1 FROM evidence_blob_location_observations WHERE location_observation_id = ?",
            (location_id,),
        ).fetchone()
        is None
    ):
        EvidenceLinkLedger(conn).persist_location(
            BlobLocationObservation(
                location_observation_id=location_id,
                idempotency_key=location_id,
                blob_sha256=digest,
                storage_uri=path.resolve().as_uri(),
                location_kind="local",
                availability_state="present",
                location_sequence=1,
                verified_at=recorded_at,
                verified_byte_size=len(body),
                verified_sha256=digest,
                recorded_at=recorded_at,
            )
        )
    return observation_id


def collect_filing_packages(
    *,
    session: requests.Session,
    user_agent: str,
    cik: str,
    filings: tuple[SecFilingInventoryEntry, ...],
    checkpoint_root: Path,
    package_limit: int,
    capture_response: Callable[[bytes, str, str], str] | None,
) -> _PackageCollection:
    """Fetch one bounded resumable package batch and parse every available response."""

    if package_limit <= 0:
        raise ValueError("package_limit must be positive")
    run_root = checkpoint_root / cik
    state_path = run_root / "state.json"
    checkpoint = _load_package_checkpoint(
        state_path,
        cik=cik,
    )
    cached_by_accession = {entry.accession_number: entry for entry in checkpoint.entries}
    packages: list[ParsedSecFilingPackage] = []
    components: list[_PackageComponent] = []
    attempts = 0
    deferred = 0
    for filing in filings:
        accession = filing.accession_number
        index_url = filing_package_index_url(cik, accession)
        manifest_url = filing_package_manifest_url(cik, accession)
        cached = cached_by_accession.get(accession)
        if cached is None and attempts >= package_limit:
            deferred += 1
            components.extend(
                (
                    _PackageComponent(
                        accession_number=accession,
                        component_kind="package_index",
                        source_url=index_url,
                        failure_reason="deferred_by_package_limit",
                    ),
                    _PackageComponent(
                        accession_number=accession,
                        component_kind="filing_manifest",
                        source_url=manifest_url,
                        failure_reason="deferred_by_package_limit",
                    ),
                )
            )
            continue
        if cached is None:
            attempts += 1
            index_body, index_failure = _fetch_package_component(session, index_url, user_agent)
            manifest_body, manifest_failure = _fetch_package_component(
                session, manifest_url, user_agent
            )
            if index_body is None or manifest_body is None:
                components.extend(
                    (
                        _PackageComponent(
                            accession_number=accession,
                            component_kind="package_index",
                            source_url=index_url,
                            body=index_body,
                            failure_reason=index_failure,
                        ),
                        _PackageComponent(
                            accession_number=accession,
                            component_kind="filing_manifest",
                            source_url=manifest_url,
                            body=manifest_body,
                            failure_reason=manifest_failure,
                        ),
                    )
                )
                if index_body is not None and capture_response is not None:
                    capture_response(index_body, index_url, "application/json")
                if manifest_body is not None and capture_response is not None:
                    capture_response(manifest_body, manifest_url, "text/html")
                continue
            cached = _store_package_checkpoint_entry(
                run_root=run_root,
                state_path=state_path,
                checkpoint=checkpoint,
                accession_number=accession,
                index_body=index_body,
                manifest_body=manifest_body,
            )
            cached_by_accession[accession] = cached
            checkpoint = checkpoint.model_copy(
                update={
                    "entries": tuple(
                        cached_by_accession[key] for key in sorted(cached_by_accession)
                    )
                }
            )
        else:
            index_body = _read_checkpoint_body(run_root, cached.index_sha256)
            manifest_body = _read_checkpoint_body(run_root, cached.manifest_sha256)

        components.extend(
            (
                _PackageComponent(
                    accession_number=accession,
                    component_kind="package_index",
                    source_url=index_url,
                    body=index_body,
                ),
                _PackageComponent(
                    accession_number=accession,
                    component_kind="filing_manifest",
                    source_url=manifest_url,
                    body=manifest_body,
                ),
            )
        )
        if capture_response is not None:
            capture_response(index_body, index_url, "application/json")
            capture_response(manifest_body, manifest_url, "text/html")
        try:
            packages.append(
                parse_sec_filing_package_inventory(
                    cik=cik,
                    accession_number=accession,
                    form_type=filing.form_type,
                    primary_document=filing.primary_document,
                    index_body=index_body,
                    filing_manifest_body=manifest_body,
                )
            )
        except SecFilingPackageContractError:
            components.append(
                _PackageComponent(
                    accession_number=accession,
                    component_kind="validation",
                    source_url=index_url + "#package-validation",
                    failure_reason="package_contract_invalid",
                )
            )
    return _PackageCollection(
        packages=tuple(packages),
        components=tuple(components),
        deferred_accession_count=deferred,
    )


def _fetch_package_component(
    session: requests.Session, url: str, user_agent: str
) -> tuple[bytes | None, str | None]:
    try:
        return _fetch(session, url, user_agent), None
    except TransientError:
        return None, "transient_deferred"
    except SourceContractError:
        return None, "source_contract_failure"


def _load_package_checkpoint(
    path: Path,
    *,
    cik: str,
) -> _PackageCheckpoint:
    if not path.exists():
        return _PackageCheckpoint(cik=cik)
    checkpoint = _PackageCheckpoint.model_validate_json(path.read_text(encoding="utf-8"))
    if checkpoint.cik != cik:
        raise ValueError("SEC package checkpoint identity conflicts with this run")
    return checkpoint


def _store_package_checkpoint_entry(
    *,
    run_root: Path,
    state_path: Path,
    checkpoint: _PackageCheckpoint,
    accession_number: str,
    index_body: bytes,
    manifest_body: bytes,
) -> _CheckpointEntry:
    index_sha = _store_checkpoint_body(run_root, index_body)
    manifest_sha = _store_checkpoint_body(run_root, manifest_body)
    entry = _CheckpointEntry(
        accession_number=accession_number,
        index_sha256=index_sha,
        manifest_sha256=manifest_sha,
    )
    by_accession = {item.accession_number: item for item in checkpoint.entries}
    prior = by_accession.get(accession_number)
    if prior is not None and prior != entry:
        raise ValueError("SEC package checkpoint entry conflicts with prior bytes")
    by_accession[accession_number] = entry
    next_checkpoint = checkpoint.model_copy(
        update={"entries": tuple(by_accession[key] for key in sorted(by_accession))}
    )
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_path.with_suffix(".json.tmp")
    temporary.write_text(next_checkpoint.model_dump_json(), encoding="utf-8")
    temporary.replace(state_path)
    return entry


def _store_checkpoint_body(run_root: Path, body: bytes) -> str:
    digest = hashlib.sha256(body).hexdigest()
    path = run_root / "responses" / digest
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise RuntimeError("SEC package checkpoint body fails hash verification")
    else:
        path.write_bytes(body)
    return digest


def _read_checkpoint_body(run_root: Path, digest: str) -> bytes:
    path = run_root / "responses" / digest
    body = path.read_bytes()
    if hashlib.sha256(body).hexdigest() != digest:
        raise RuntimeError("SEC package checkpoint body fails hash verification")
    return body


def _dump_inventory_contract_failure(
    root: Path,
    *,
    ticker: str,
    cik: str,
    root_body: bytes,
    historical: tuple[HistoricalComponent, ...],
    error: SecInventoryContractError,
) -> Path:
    """Preserve every raw SEC component before halting on schema drift."""

    components: list[tuple[str, bytes | None, str | None]] = [(f"CIK{cik}.json", root_body, None)]
    components.extend((item.name, item.body, item.failure_reason) for item in historical)
    component_digests = tuple(
        (
            name,
            None if body is None else hashlib.sha256(body).hexdigest(),
            failure_reason,
        )
        for name, body, failure_reason in components
    )
    run_id = hashlib.sha256(
        json.dumps(
            {
                "cik": cik,
                "components": component_digests,
                "error_type": type(error).__name__,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    run_root = root / cik / run_id
    run_root.mkdir(parents=True, exist_ok=True)
    manifest_components: list[dict[str, object]] = []
    for name, body, failure_reason in components:
        digest = None if body is None else hashlib.sha256(body).hexdigest()
        filename = None if digest is None else f"{digest}.raw"
        if body is not None and filename is not None:
            path = run_root / filename
            if path.exists():
                if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    raise RuntimeError("SEC contract-failure dump fails hash verification")
            else:
                path.write_bytes(body)
        manifest_components.append(
            {
                "name": name,
                "sha256": digest,
                "file": filename,
                "failure_reason": failure_reason,
            }
        )
    manifest = {
        "ticker": ticker,
        "cik": cik,
        "error_type": type(error).__name__,
        "components": manifest_components,
    }
    manifest_path = run_root / "manifest.json"
    temporary = run_root / "manifest.json.tmp"
    temporary.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(manifest_path)
    return manifest_path


def build_expected_documents(
    *,
    issuer_id: str,
    filings: tuple[SecFilingInventoryEntry, ...],
    packages: tuple[ParsedSecFilingPackage, ...],
) -> tuple[ExpectedDocumentImport, ...]:
    """Build the accession root and every separately addressable package child."""

    expected_documents: list[ExpectedDocumentImport] = [
        ExpectedDocumentImport(
            expected_document_key=f"{issuer_id}:{filing.accession_number}",
            source_kind="sec_filing",
            document_type="filing",
            form_type=filing.form_type,
            accession_number=filing.accession_number,
            source_url=filing.primary_document_url,
            primary_document=filing.primary_document,
            filing_at=datetime.fromisoformat(filing.filing_date),
            expectation_basis="authoritative",
            absence=ExplicitAbsence(
                coverage_status="available",
                reason_code="sec_authority_inventory",
                reason_details=(("component", filing.source_component_name),),
            ),
        )
        for filing in filings
    ]
    filing_by_accession = {filing.accession_number: filing for filing in filings}
    for package in packages:
        filing = filing_by_accession[package.accession_number]
        parent_key = f"{issuer_id}:{package.accession_number}"
        for attachment in package.attachments:
            if attachment.role == "primary_document":
                continue
            document_type = {
                "exhibit": "sec_exhibit",
                "financial_report": "sec_financial_report",
                "supporting_attachment": "sec_supporting_attachment",
            }[attachment.role]
            expected_documents.append(
                ExpectedDocumentImport(
                    expected_document_key=(f"{parent_key}:attachment:{attachment.attachment_id}"),
                    source_kind="sec_filing",
                    document_type=document_type,
                    form_type=filing.form_type,
                    accession_number=package.accession_number,
                    source_url=attachment.source_url,
                    primary_document=attachment.filename,
                    filing_at=datetime.fromisoformat(filing.filing_date),
                    expectation_basis="authoritative",
                    absence=ExplicitAbsence(
                        coverage_status="available",
                        reason_code="sec_authority_package_inventory",
                        reason_details=tuple(
                            sorted(
                                (
                                    ("attachment_id", attachment.attachment_id),
                                    (
                                        "declared_type",
                                        attachment.declared_type or "index_only",
                                    ),
                                    ("parent_expected_document_key", parent_key),
                                    ("role", attachment.role),
                                )
                            )
                        ),
                    ),
                )
            )
    return tuple(expected_documents)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--cik", required=True)
    parser.add_argument("--revision", type=int, required=True)
    parser.add_argument(
        "--blob-root",
        type=Path,
        default=PROJECT_ROOT / "data" / "evidence" / "blobs",
    )
    parser.add_argument(
        "--package-checkpoint-root",
        type=Path,
        default=PROJECT_ROOT / ".tmp" / "sec_filing_package_inventory",
    )
    parser.add_argument(
        "--contract-failure-root",
        type=Path,
        default=PROJECT_ROOT / ".tmp" / "sec_inventory_contract_failures",
    )
    parser.add_argument(
        "--package-limit",
        type=int,
        default=_DEFAULT_PACKAGE_LIMIT,
        help=(
            "Maximum uncached accession packages fetched this run; rerun dry mode "
            "to resume the content-addressed checkpoint."
        ),
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    if args.apply:
        try:
            with JobLock(
                PROJECT_ROOT,
                "sync-sec-filing-inventory",
                [
                    f"sqlite:{args.db.resolve()}",
                    f"evidence-blobs:{args.blob_root.resolve()}",
                    (f"sec-package-checkpoint:{args.package_checkpoint_root.resolve()}"),
                    f"source-inventory:sec-cik:{str(args.cik).strip().zfill(10)}",
                ],
            ):
                return _run(args)
        except JobAlreadyRunningError:
            _event(
                "sec_filing_inventory_locked",
                ticker=str(args.ticker).strip().upper(),
            )
            return 75
    return _run(args)


def _run(args: argparse.Namespace) -> int:
    ticker = str(args.ticker).strip().upper()
    cik = str(args.cik).strip().zfill(10)
    root_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    config_sha = hashlib.sha256(
        json.dumps(
            {
                "collector": _COLLECTOR,
                "timeout": _TIMEOUT,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    inventory_config_sha = hashlib.sha256(
        json.dumps(
            {
                "collector": _COLLECTOR,
                "package_limit": int(args.package_limit),
                "package_scope_policy_version": _PACKAGE_SCOPE_POLICY_VERSION,
                "retrieval_config_sha256": config_sha,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    started = datetime.now(UTC)
    canonical_issuer_id: str | None = None
    identity_conn = connect_sqlite(
        args.db,
        role=SQLiteConnectionRole.READ_ONLY,
        schema_preflight=False,
    )
    try:
        if issuer_registry_available(identity_conn):
            try:
                subject = resolve_sec_inventory_subject(
                    identity_conn,
                    ticker=ticker,
                    cik=cik,
                    knowledge_at=started,
                )
            except InventoryIdentityError as exc:
                _event(
                    "sec_filing_inventory_identity_hard_stop",
                    ticker=ticker,
                    error_type=type(exc).__name__,
                )
                return 2
            canonical_issuer_id = subject.issuer_id
            root_url = subject.source_url
    finally:
        identity_conn.close()
    session = requests.Session()
    user_agent = sec_user_agent()
    root_body = _fetch(session, root_url, user_agent)
    conn: sqlite3.Connection | None = None
    root_observation: str | None = None
    observation_by_name: dict[str, str] = {}
    captured_at = datetime.now(UTC)
    if args.apply:
        conn = connect_sqlite(args.db, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
        conn.execute("BEGIN IMMEDIATE")
        root_observation = _capture_component(
            conn,
            body=root_body,
            url=root_url,
            blob_root=args.blob_root,
            config_sha=config_sha,
            recorded_at=captured_at,
        )
        conn.commit()
    historical: list[HistoricalComponent] = []
    for name, _count in advertised_historical_components(root_body):
        url = historical_component_url(name)
        try:
            body = _fetch(session, url, user_agent)
        except TransientError as exc:
            historical.append(
                HistoricalComponent(
                    name=name,
                    failure_reason=type(exc).__name__.lower(),
                )
            )
            continue
        if conn is not None:
            conn.execute("BEGIN IMMEDIATE")
            observation_by_name[name] = _capture_component(
                conn,
                body=body,
                url=historical_component_url(name),
                blob_root=args.blob_root,
                config_sha=config_sha,
                recorded_at=captured_at,
            )
            conn.commit()
        historical.append(HistoricalComponent(name=name, body=body))
    try:
        parsed = parse_sec_submissions_inventory(
            cik=cik,
            ticker=ticker,
            primary_body=root_body,
            historical=tuple(historical),
        )
    except SecInventoryContractError as exc:
        failure_manifest = _dump_inventory_contract_failure(
            args.contract_failure_root,
            ticker=ticker,
            cik=cik,
            root_body=root_body,
            historical=tuple(historical),
            error=exc,
        )
        _event(
            "sec_filing_inventory_contract_hard_stop",
            ticker=ticker,
            cik=cik,
            error_type=type(exc).__name__,
            failure_manifest=str(failure_manifest),
        )
        return 3
    issuer_id = canonical_issuer_id or parsed.issuer_id
    filing_scope = partition_filing_package_scope(parsed.filings)
    package_observation_by_url: dict[str, str] = {}

    def capture_package_response(body: bytes, url: str, media_type: str) -> str:
        if conn is None:
            raise RuntimeError("package response capture requires an apply connection")
        try:
            conn.execute("BEGIN IMMEDIATE")
            observation_id = _capture_component(
                conn,
                body=body,
                url=url,
                blob_root=args.blob_root,
                config_sha=config_sha,
                recorded_at=captured_at,
                media_type=media_type,
                source_kind="sec_filing_package",
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        package_observation_by_url[url] = observation_id
        return observation_id

    package_collection = collect_filing_packages(
        session=session,
        user_agent=user_agent,
        cik=cik,
        filings=filing_scope.issuer_reports,
        checkpoint_root=args.package_checkpoint_root,
        package_limit=int(args.package_limit),
        capture_response=capture_package_response if conn is not None else None,
    )
    package_failures = tuple(
        component
        for component in package_collection.components
        if component.failure_reason is not None
    )
    package_issue_codes = tuple(
        dict.fromkeys(f"package_{component.failure_reason}" for component in package_failures)
    )
    package_failure_samples = summarize_package_failures(package_failures)
    unclassified_form_types = tuple(sorted({item.form_type for item in filing_scope.unclassified}))
    complete = parsed.complete and not package_failures and not filing_scope.unclassified
    attachment_count = sum(len(package.attachments) for package in package_collection.packages)
    if not args.apply:
        sys.stdout.write(
            SyncResult(
                mode="dry_run",
                ticker=ticker,
                issuer_id=issuer_id,
                filing_count=len(parsed.filings),
                issuer_report_filing_count=len(filing_scope.issuer_reports),
                external_or_administrative_filing_count=len(
                    filing_scope.external_or_administrative
                ),
                unclassified_form_types=unclassified_form_types,
                attachment_count=attachment_count,
                component_count=(
                    len(parsed.required_component_names) + len(package_collection.components)
                ),
                deferred_accession_count=package_collection.deferred_accession_count,
                package_failure_count=len(package_failures),
                package_failure_samples=package_failure_samples,
                complete=complete,
                issue_codes=(
                    *(issue.code for issue in parsed.issues),
                    *package_issue_codes,
                    *(("unclassified_filing_form",) if filing_scope.unclassified else ()),
                ),
            ).model_dump_json()
            + "\n"
        )
        return 0

    if conn is None or root_observation is None:
        raise RuntimeError("apply mode did not initialize SEC evidence persistence")
    now = datetime.now(UTC)
    try:
        components: list[InventoryComponentImport] = [
            InventoryComponentImport(
                component_key="primary",
                component_kind="primary",
                source_url=root_url,
                source_observation_id=root_observation,
                outcome="succeeded",
                ordinal=0,
            )
        ]
        for ordinal, component in enumerate(historical, start=1):
            components.append(
                InventoryComponentImport(
                    component_key=component.name,
                    component_kind="historical_page",
                    source_url=historical_component_url(component.name),
                    source_observation_id=observation_by_name.get(component.name),
                    outcome="succeeded" if component.body is not None else "failed",
                    failure_reason=component.failure_reason,
                    ordinal=ordinal,
                )
            )
        for package_component in package_collection.components:
            component_prefix = {
                "package_index": "package-index",
                "filing_manifest": "filing-manifest",
                "validation": "package-validation",
            }[package_component.component_kind]
            components.append(
                InventoryComponentImport(
                    component_key=(f"{component_prefix}:{package_component.accession_number}"),
                    component_kind="other",
                    source_url=package_component.source_url,
                    source_observation_id=(
                        package_observation_by_url.get(package_component.source_url)
                        if package_component.body is not None
                        else None
                    ),
                    outcome=("succeeded" if package_component.body is not None else "failed"),
                    failure_reason=package_component.failure_reason,
                    ordinal=len(components),
                )
            )
        if parsed.issues:
            components.append(
                InventoryComponentImport(
                    component_key="contract-validation",
                    component_kind="other",
                    source_url=root_url + "#contract-validation",
                    outcome="failed",
                    failure_reason=parsed.issues[0].code,
                    ordinal=len(components),
                )
            )
        expected_documents = build_expected_documents(
            issuer_id=issuer_id,
            filings=filing_scope.issuer_reports,
            packages=package_collection.packages,
        )
        request = SourceCoverageImport(
            inventory_key=f"{issuer_id}:sec-submissions",
            revision=int(args.revision),
            issuer_id=issuer_id,
            ticker=ticker,
            source_kind="sec_submissions",
            source_url=root_url,
            source_observation_id=root_observation,
            outcome="succeeded" if complete else "partial",
            authoritative=True,
            retrieval_config_sha256=inventory_config_sha,
            collector_code_version=_COLLECTOR,
            started_at=started,
            completed_at=now,
            recorded_at=now,
            reconciled_at=now,
            components=tuple(components),
            expected_documents=expected_documents,
            apply=True,
        )
        coverage = reconcile_source_coverage(conn, request)
    finally:
        conn.close()
    result = SyncResult(
        mode="apply",
        ticker=ticker,
        issuer_id=issuer_id,
        filing_count=len(parsed.filings),
        issuer_report_filing_count=len(filing_scope.issuer_reports),
        external_or_administrative_filing_count=len(filing_scope.external_or_administrative),
        unclassified_form_types=unclassified_form_types,
        attachment_count=attachment_count,
        component_count=len(components),
        deferred_accession_count=package_collection.deferred_accession_count,
        package_failure_count=len(package_failures),
        package_failure_samples=package_failure_samples,
        complete=complete,
        issue_codes=(
            *(issue.code for issue in parsed.issues),
            *package_issue_codes,
            *(("unclassified_filing_form",) if filing_scope.unclassified else ()),
        ),
        snapshot_id=coverage.snapshot_id,
        records_created=coverage.records_created,
    )
    sys.stdout.write(result.model_dump_json() + "\n")
    _event(
        "sec_filing_inventory_synced",
        ticker=ticker,
        snapshot_id=coverage.snapshot_id,
        complete=complete,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
