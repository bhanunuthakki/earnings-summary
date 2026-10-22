"""Import exact original SEC submission byte spans through native evidence owners.

This adapter performs no HTTP request and makes no package-completeness claim.
The caller supplies the real retained parent capture and an isolated idle writer.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Literal, Self, cast
from urllib.parse import urlsplit
from urllib.request import url2pathname

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from filings.sec_submission_text import (
    ParsedSecSubmissionText,
    SubmissionDocumentSpan,
    parse_sec_submission_text,
)
from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    SourceObservation,
)
from provenance.evidence_links import (
    BlobLocationObservation,
    DocumentObservationLink,
    EvidenceLinkLedger,
)
from provenance.immutable_artifact import publish_bytes_no_clobber, read_stable_artifact
from provenance.sec_native_capture import ExpectedSecDocument, load_expected_sec_documents
from provenance.source_coverage import CoverageAssessment, SourceCoverageLedger
from runtime.deterministic import as_utc, canonical_json_ascii, content_sha256_ascii

_COLLECTOR = "sec-submission-member-import@1"
_MAX_BYTES = 100 * 1024 * 1024


class SecSubmissionImportError(ValueError):
    """The retained source, native expectation or import receipt failed replay."""


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RetainedSubmissionImportRequest(_Closed):
    parent_observation: SourceObservation
    parent_blob: ContentBlob
    cik: str = Field(pattern=r"^\d{1,10}$")
    expected_documents: tuple[ExpectedSecDocument, ...] = Field(min_length=1, max_length=64)
    content_roots: tuple[Path, ...] = Field(min_length=1)
    blob_root: Path
    receipt_root: Path
    imported_at: AwareDatetime
    apply: bool = False

    @model_validator(mode="after")
    def exact_request(self) -> Self:
        ids = tuple(item.expected_document_id for item in self.expected_documents)
        if ids != tuple(sorted(set(ids))):
            raise ValueError("submission import selections must be sorted and unique")
        if self.parent_observation.blob_sha256 != self.parent_blob.sha256:
            raise ValueError("submission parent capture and blob differ")
        clocks = (
            self.parent_observation.observed_at,
            self.parent_observation.retrieved_at,
            self.parent_blob.recorded_at,
        )
        if any(value.tzinfo is None or value.utcoffset() is None for value in clocks):
            raise ValueError("submission capture clocks require explicit zones")
        if any(as_utc(value) > self.imported_at for value in clocks):
            raise ValueError("submission import cannot precede retained capture")
        for root in (*self.content_roots, self.blob_root, self.receipt_root):
            if not root.is_absolute() or root == Path(root.anchor) or ".." in root.parts:
                raise ValueError("submission import requires bounded absolute content roots")
        for output in (self.blob_root, self.receipt_root):
            if not any(output == root or root in output.parents for root in self.content_roots):
                raise ValueError("submission import output escapes approved roots")
        if (
            len(
                {
                    (item.accession_number, item.form_type, item.issuer_id)
                    for item in self.expected_documents
                }
            )
            != 1
        ):
            raise ValueError("submission selection crosses accession, form or issuer")
        return self


class ImportedSubmissionMember(_Closed):
    expected_document_id: str
    expected_document_key: str
    source_url: str
    span: SubmissionDocumentSpan
    child_observation_id: str
    document_version_id: str
    retrieval_config_sha256: str


class SecSubmissionImportReceipt(_Closed):
    schema_version: Literal["sec-submission-member-import/v1"] = "sec-submission-member-import/v1"
    collector: Literal["sec-submission-member-import@1"] = _COLLECTOR
    parent_observation_id: str
    parent_source_url: str
    parent_sha256: str
    parent_observed_at: AwareDatetime
    parent_retrieved_at: AwareDatetime
    imported_at: AwareDatetime
    cik: str
    accession_number: str
    form_type: str
    acceptance_datetime_raw: str
    acceptance_clock_basis: Literal["sec_asserted_eastern_wall_clock"]
    index_sha256: str
    members: tuple[ImportedSubmissionMember, ...]
    package_completeness: Literal["not_assessed"] = "not_assessed"

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json_ascii(self.model_dump(mode="json")).encode()

    @property
    def receipt_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()


class SecSubmissionImportResult(_Closed):
    applied: bool
    receipt: SecSubmissionImportReceipt
    receipt_path: Path
    records_created: int


def _source_path(uri: str, roots: tuple[Path, ...]) -> tuple[Path, Path]:
    parsed = urlsplit(uri)
    path = Path(url2pathname(parsed.path))
    if (
        parsed.scheme != "file"
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or not path.is_absolute()
        or path.as_uri() != uri
    ):
        raise SecSubmissionImportError("submission source storage is not an exact local file URI")
    root = next((root for root in roots if path == root or root in path.parents), None)
    if root is None:
        raise SecSubmissionImportError("submission source is outside approved content roots")
    return path, root


def _read_parent(request: RetainedSubmissionImportRequest) -> tuple[bytes, ParsedSecSubmissionText]:
    path, root = _source_path(request.parent_blob.storage_uri, request.content_roots)
    _snapshot, raw = read_stable_artifact(path, allowed_root=root, max_bytes=_MAX_BYTES)
    if (
        path.stat().st_nlink != 1
        or len(raw) != request.parent_blob.byte_size
        or hashlib.sha256(raw).hexdigest() != request.parent_blob.sha256
    ):
        raise SecSubmissionImportError("submission parent bytes or unique file identity differ")
    selected = request.expected_documents[0]
    prefix = f"https://www.sec.gov/Archives/edgar/data/{int(request.cik)}/{selected.accession_number.replace('-', '')}/"
    if request.parent_observation.source_url != prefix + selected.accession_number + ".txt":
        raise SecSubmissionImportError("submission parent URL differs from its native identity")
    index = parse_sec_submission_text(
        raw,
        cik=request.cik,
        accession_number=selected.accession_number,
        form_type=selected.form_type,
    )
    return raw, index


def _pending_identities(
    conn: sqlite3.Connection, request: RetainedSubmissionImportRequest
) -> dict[str, ExpectedSecDocument]:
    pending, truncated = load_expected_sec_documents(
        conn,
        inventory_keys=tuple(sorted({item.inventory_key for item in request.expected_documents})),
        accession_numbers=(request.expected_documents[0].accession_number,),
        limit=2048,
    )
    if truncated:
        raise SecSubmissionImportError("submission expected population exceeds the bounded loader")
    return {item.expected_document_id: item for item in pending}


def _verify_expected(
    conn: sqlite3.Connection,
    expected: ExpectedSecDocument,
    pending: dict[str, ExpectedSecDocument],
    request: RetainedSubmissionImportRequest,
    replay: bool,
) -> None:
    candidate = pending.get(expected.expected_document_id)
    if candidate is None:
        if not replay:
            raise SecSubmissionImportError(
                "selected submission original is not pending native capture"
            )
        cursor = conn.execute(
            "SELECT expected.*,inventory.inventory_key,inventory.source_url AS inventory_url "
            "FROM expected_documents expected JOIN source_inventory_snapshots inventory "
            "ON inventory.snapshot_id=expected.snapshot_id JOIN source_inventory_snapshot_seals seal "
            "ON seal.snapshot_id=inventory.snapshot_id WHERE expected.expected_document_id=? "
            "AND expected.expectation_basis='authoritative' AND inventory.authoritative=1 "
            "AND seal.completion_status='complete'",
            (expected.expected_document_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise SecSubmissionImportError(
                "retained submission expectation is not a sealed native record"
            )
        values = dict(zip((column[0] for column in cursor.description), row, strict=True))
        values["current_coverage_status"] = None
        candidate = ExpectedSecDocument.model_validate(
            {key: values[key] for key in ExpectedSecDocument.model_fields}
        )
    if candidate.model_dump(exclude={"current_coverage_status"}) != expected.model_dump(
        exclude={"current_coverage_status"}
    ):
        raise SecSubmissionImportError("submission expected identity was changed")
    inventory = conn.execute(
        "SELECT source_url FROM source_inventory_snapshots WHERE snapshot_id=?",
        (expected.snapshot_id,),
    ).fetchone()
    if (
        inventory is None
        or str(inventory[0]) != f"https://data.sec.gov/submissions/CIK{request.cik.zfill(10)}.json"
    ):
        raise SecSubmissionImportError("submission inventory CIK differs from native header")


def _version(
    conn: sqlite3.Connection,
    expected: ExpectedSecDocument,
    observation: SourceObservation,
    digest: str,
    recorded_at: datetime,
) -> DocumentVersion:
    cursor = conn.execute(
        "SELECT * FROM evidence_document_versions WHERE document_key=? ORDER BY version_sequence DESC",
        (expected.expected_document_key,),
    )
    rows = [
        dict(zip((column[0] for column in cursor.description), row, strict=True))
        for row in cursor.fetchall()
    ]
    for row in rows:
        if row["blob_sha256"] == digest:
            version = DocumentVersion.model_validate(row)
            if any(
                getattr(version, key) != getattr(expected, key)
                for key in (
                    "issuer_id",
                    "ticker",
                    "document_type",
                    "form_type",
                    "accession_number",
                    "period_start",
                    "period_end",
                )
            ):
                raise SecSubmissionImportError(
                    "submission bytes conflict with native document identity"
                )
            return version
    if rows:
        raise SecSubmissionImportError(
            "submission member differs from an already retained document version"
        )
    digest_id = hashlib.sha256(
        (expected.expected_document_key + "\0" + digest).encode()
    ).hexdigest()
    return DocumentVersion(
        document_version_id="sec-native-document:" + digest_id,
        document_key=expected.expected_document_key,
        version_sequence=1,
        observation_id=observation.observation_id,
        blob_sha256=digest,
        issuer_id=expected.issuer_id,
        ticker=expected.ticker,
        document_type=expected.document_type,
        form_type=expected.form_type,
        accession_number=expected.accession_number,
        period_start=expected.period_start,
        period_end=expected.period_end,
        as_of_at=expected.filing_at,
        language="und",
        recorded_at=recorded_at,
    )


def _location(conn: sqlite3.Connection, blob: ContentBlob, at: datetime) -> BlobLocationObservation:
    identity = content_sha256_ascii([blob.sha256, blob.storage_uri, at.isoformat()])
    cursor = conn.execute(
        "SELECT * FROM evidence_blob_location_observations WHERE idempotency_key=?",
        ("submission-location:" + identity,),
    )
    retained = cursor.fetchone()
    if retained is not None:
        return BlobLocationObservation.model_validate(
            dict(zip((column[0] for column in cursor.description or ()), retained, strict=True))
        )
    current = conn.execute(
        "SELECT location_observation_id,location_sequence FROM evidence_blob_location_observations "
        "WHERE blob_sha256=? AND storage_uri=? ORDER BY location_sequence DESC LIMIT 1",
        (blob.sha256, blob.storage_uri),
    ).fetchone()
    return BlobLocationObservation(
        location_observation_id="submission-location:" + identity,
        idempotency_key="submission-location:" + identity,
        blob_sha256=blob.sha256,
        storage_uri=blob.storage_uri,
        location_kind="local",
        availability_state="present",
        location_sequence=1 if current is None else int(current[1]) + 1,
        supersedes_location_observation_id=None if current is None else str(current[0]),
        verified_at=at,
        recorded_at=at,
        verified_byte_size=blob.byte_size,
        verified_sha256=blob.sha256,
    )


def import_sec_submission_members(
    conn: sqlite3.Connection, request: RetainedSubmissionImportRequest
) -> SecSubmissionImportResult:
    """Plan or atomically admit selected original members; never fetch or trim bytes."""
    if conn.in_transaction:
        raise SecSubmissionImportError("submission import requires an idle connection")
    request = RetainedSubmissionImportRequest.model_validate(request.model_dump())
    raw, index = _read_parent(request)
    identity = content_sha256_ascii(
        {
            "parent": request.parent_observation.observation_id,
            "sha256": index.raw_sha256,
            "expected": [
                item.model_dump(mode="json", exclude={"current_coverage_status"})
                for item in request.expected_documents
            ],
        }
    )
    receipt_path = request.receipt_root / (identity + ".json")
    by_name = {span.filename: span for span in index.documents}
    index_sha = content_sha256_ascii(index.model_dump(mode="json"))
    conn.execute("BEGIN IMMEDIATE" if request.apply else "BEGIN")
    try:
        pending = _pending_identities(conn, request)
        prepared: list[
            tuple[
                ExpectedSecDocument,
                ImportedSubmissionMember,
                SourceObservation,
                DocumentVersion,
                ContentBlob,
            ]
        ] = []
        for expected in request.expected_documents:
            _verify_expected(conn, expected, pending, request, receipt_path.exists())
            span = by_name.get(expected.primary_document)
            prefix = f"https://www.sec.gov/Archives/edgar/data/{int(request.cik)}/{index.accession_number.replace('-', '')}/"
            if (
                span is None
                or expected.source_url != prefix + span.filename
                or expected.document_type == "sec_financial_report"
            ):
                raise SecSubmissionImportError(
                    "selected source is not an exact original submission member"
                )
            if expected.document_type == "filing" and span.declared_type != index.form_type:
                raise SecSubmissionImportError("primary original type differs from submission form")
            body = raw[span.text_start : span.text_end]
            config_sha = content_sha256_ascii(
                {
                    "collector": _COLLECTOR,
                    "expected_document_id": expected.expected_document_id,
                    "parent_observation": request.parent_observation.observation_id,
                    "parent_url": request.parent_observation.source_url,
                    "parent_sha256": index.raw_sha256,
                    "index_sha256": index_sha,
                    "span": span.model_dump(mode="json"),
                }
            )
            observation = SourceObservation(
                observation_id="submission-member:" + config_sha,
                idempotency_key="submission-member:" + config_sha,
                source_kind="sec_filing",
                source_url=expected.source_url,
                blob_sha256=span.text_sha256,
                source_published_at=None,
                filing_at=expected.filing_at,
                accepted_at=None,
                observed_at=request.parent_observation.observed_at,
                retrieved_at=request.parent_observation.retrieved_at,
                retrieval_config_sha256=config_sha,
                collector_code_version=_COLLECTOR,
            )
            version = _version(conn, expected, observation, span.text_sha256, request.imported_at)
            blob = ContentBlob(
                sha256=span.text_sha256,
                byte_size=len(body),
                media_type="text/html"
                if span.filename.lower().endswith((".htm", ".html"))
                else "application/octet-stream",
                storage_uri=(request.blob_root / span.text_sha256[:2] / span.text_sha256).as_uri(),
                recorded_at=request.imported_at,
            )
            member = ImportedSubmissionMember(
                expected_document_id=expected.expected_document_id,
                expected_document_key=expected.expected_document_key,
                source_url=expected.source_url,
                span=span,
                child_observation_id=observation.observation_id,
                document_version_id=version.document_version_id,
                retrieval_config_sha256=config_sha,
            )
            prepared.append((expected, member, observation, version, blob))
        receipt = SecSubmissionImportReceipt(
            parent_observation_id=request.parent_observation.observation_id,
            parent_source_url=request.parent_observation.source_url,
            parent_sha256=index.raw_sha256,
            parent_observed_at=request.parent_observation.observed_at,
            parent_retrieved_at=request.parent_observation.retrieved_at,
            imported_at=request.imported_at,
            cik=index.cik,
            accession_number=index.accession_number,
            form_type=index.form_type,
            acceptance_datetime_raw=index.acceptance_datetime_raw,
            acceptance_clock_basis=index.acceptance_clock_basis,
            index_sha256=index_sha,
            members=tuple(item[1] for item in prepared),
        )
        if receipt_path.exists():
            _snapshot, receipt_bytes = read_stable_artifact(
                receipt_path, allowed_root=request.receipt_root, max_bytes=2_000_000
            )
            if receipt_bytes != receipt.canonical_bytes:
                raise SecSubmissionImportError("immutable submission import receipt differs")
        created = 0
        if request.apply:
            ledger, links = EvidenceLedger(conn), EvidenceLinkLedger(conn)
            created += ledger.persist(request.parent_blob).created
            created += ledger.persist(request.parent_observation).created
            created += links.persist_location(
                _location(conn, request.parent_blob, request.imported_at)
            ).created
            for expected, member, observation, version, blob in prepared:
                body = raw[member.span.text_start : member.span.text_end]
                publish_bytes_no_clobber(Path(url2pathname(urlsplit(blob.storage_uri).path)), body)
                cursor = conn.execute(
                    "SELECT * FROM evidence_content_blobs WHERE sha256=?", (blob.sha256,)
                )
                row = cursor.fetchone()
                canonical_blob = (
                    blob
                    if row is None
                    else ContentBlob.model_validate(
                        dict(
                            zip(
                                (column[0] for column in cursor.description or ()), row, strict=True
                            )
                        )
                    )
                )
                if canonical_blob.byte_size != blob.byte_size:
                    raise SecSubmissionImportError("retained member blob size differs")
                created += ledger.persist(canonical_blob).created
                created += links.persist_location(
                    _location(conn, blob, request.imported_at)
                ).created
                created += ledger.persist(observation).created
                created += ledger.persist(version).created
                link_id = "submission-link:" + observation.retrieval_config_sha256
                created += links.persist_link(
                    DocumentObservationLink(
                        link_id=link_id,
                        document_version_id=version.document_version_id,
                        observation_id=observation.observation_id,
                        link_kind="primary"
                        if version.observation_id == observation.observation_id
                        else "retrieval",
                        linked_at=request.imported_at,
                    )
                ).created
                created += _coverage(conn, request, expected, member, receipt, receipt_path)
            publish_bytes_no_clobber(receipt_path, receipt.canonical_bytes)
            conn.commit()
        else:
            conn.rollback()
        return SecSubmissionImportResult(
            applied=request.apply,
            receipt=receipt,
            receipt_path=receipt_path,
            records_created=created,
        )
    except BaseException:
        conn.rollback()
        raise


def _coverage(
    conn: sqlite3.Connection,
    request: RetainedSubmissionImportRequest,
    expected: ExpectedSecDocument,
    member: ImportedSubmissionMember,
    receipt: SecSubmissionImportReceipt,
    path: Path,
) -> int:
    identity = "submission-coverage:" + member.retrieval_config_sha256
    retained = conn.execute(
        "SELECT expected_document_id,document_version_id,policy_config_sha256,reason_details_json "
        "FROM source_coverage_assessments WHERE idempotency_key=?",
        (identity,),
    ).fetchone()
    if retained is not None:
        if (
            tuple(retained[:3])
            != (
                expected.expected_document_id,
                member.document_version_id,
                member.retrieval_config_sha256,
            )
            or json.loads(str(retained[3])).get("import_receipt_sha256") != receipt.receipt_sha256
        ):
            raise SecSubmissionImportError("submission capture replay conflicts with its receipt")
        return 0
    previous = conn.execute(
        "SELECT assessment_id,revision,reason_details_json FROM v_source_coverage_current WHERE expected_document_id=?",
        (expected.expected_document_id,),
    ).fetchone()
    subject_keys = {
        "subject_review_sha256",
        "subject_source_sha256",
        "subject_reviewer",
        "subject_reviewed_at",
        "subject_heading_selector",
        "subject_heading_text",
        "subject_period_end_text",
        "subject_rationale",
    }
    subject_keys |= {"imported_detail_" + key for key in tuple(subject_keys)}
    prior_raw: object = {} if previous is None else json.loads(str(previous[2]))
    if not isinstance(prior_raw, dict):
        raise SecSubmissionImportError("prior subject review details are invalid")
    prior_details = cast(dict[str, object], prior_raw)
    if any(not isinstance(value, str) for value in prior_details.values()):
        raise SecSubmissionImportError("prior subject review details are invalid")
    preserved_subject = tuple(
        sorted((key, str(value)) for key, value in prior_details.items() if key in subject_keys)
    )
    return int(
        SourceCoverageLedger(conn)
        .persist(
            CoverageAssessment(
                assessment_id=identity,
                idempotency_key=identity,
                expected_document_id=expected.expected_document_id,
                revision=1 if previous is None else int(previous[1]) + 1,
                supersedes_assessment_id=None if previous is None else str(previous[0]),
                coverage_status="captured",
                document_version_id=member.document_version_id,
                reason_code="original_submission_member_imported",
                reason_details=(
                    ("collector", _COLLECTOR),
                    ("parent_observation_id", receipt.parent_observation_id),
                    ("parent_sha256", receipt.parent_sha256),
                    ("index_sha256", receipt.index_sha256),
                    ("member_sha256", member.span.text_sha256),
                    ("text_start", str(member.span.text_start)),
                    ("text_end", str(member.span.text_end)),
                    ("import_receipt_sha256", receipt.receipt_sha256),
                    ("import_receipt_uri", path.as_uri()),
                    *preserved_subject,
                ),
                decision_kind="deterministic",
                policy_name="retained-sec-original-submission-import",
                policy_version="1",
                policy_config_sha256=member.retrieval_config_sha256,
                effective_at=request.parent_observation.observed_at,
                knowledge_at=request.imported_at,
                recorded_at=request.imported_at,
                material_dissent=False,
            )
        )
        .created
    )


def verify_sec_submission_import(
    conn: sqlite3.Connection,
    request: RetainedSubmissionImportRequest,
    receipt: SecSubmissionImportReceipt,
) -> SecSubmissionImportReceipt:
    """Replay containment, immutable receipt and native rows without any writes."""
    result = import_sec_submission_members(conn, request.model_copy(update={"apply": False}))
    if result.receipt != receipt or not result.receipt_path.exists():
        raise SecSubmissionImportError("submission import receipt is missing or differs")
    for table, identity, expected in (
        ("evidence_source_observations", "observation_id", request.parent_observation),
        ("evidence_content_blobs", "sha256", request.parent_blob),
    ):
        cursor = conn.execute(
            f"SELECT * FROM {table} WHERE {identity}=?",  # nosec B608 -- closed literal table/column set
            (getattr(expected, identity),),
        )
        row = cursor.fetchone()
        if (
            row is None
            or type(expected).model_validate(
                dict(zip((column[0] for column in cursor.description or ()), row, strict=True))
            )
            != expected
        ):
            raise SecSubmissionImportError(
                "submission parent native admission is missing or changed"
            )
    for member in receipt.members:
        cursor = conn.execute(
            "SELECT * FROM evidence_source_observations WHERE observation_id=?",
            (member.child_observation_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise SecSubmissionImportError("submission child observation admission is missing")
        observation = SourceObservation.model_validate(
            dict(zip((column[0] for column in cursor.description or ()), row, strict=True))
        )
        if (
            observation.source_kind != "sec_filing"
            or observation.source_url != member.source_url
            or observation.blob_sha256 != member.span.text_sha256
            or observation.observed_at != receipt.parent_observed_at
            or observation.retrieved_at != receipt.parent_retrieved_at
            or observation.retrieval_config_sha256 != member.retrieval_config_sha256
            or observation.collector_code_version != _COLLECTOR
        ):
            raise SecSubmissionImportError(
                "submission child observation does not retain parent lineage"
            )
        path = request.blob_root / member.span.text_sha256[:2] / member.span.text_sha256
        _snapshot, raw = read_stable_artifact(
            path, allowed_root=request.blob_root, max_bytes=_MAX_BYTES
        )
        if hashlib.sha256(raw).hexdigest() != member.span.text_sha256:
            raise SecSubmissionImportError("submission child bytes differ from the original span")
        linkage = conn.execute(
            "SELECT 1 FROM evidence_document_observation_links "
            "WHERE document_version_id=? AND observation_id=?",
            (member.document_version_id, member.child_observation_id),
        ).fetchone()
        coverage = conn.execute(
            "SELECT reason_details_json,document_version_id,policy_config_sha256 "
            "FROM source_coverage_assessments WHERE idempotency_key=?",
            ("submission-coverage:" + member.retrieval_config_sha256,),
        ).fetchone()
        if (
            linkage is None
            or coverage is None
            or coverage[1] != member.document_version_id
            or coverage[2] != member.retrieval_config_sha256
        ):
            raise SecSubmissionImportError(
                "submission child document or capture admission is missing"
            )
        details = json.loads(str(coverage[0]))
        if (
            not isinstance(details, dict)
            or details.get("import_receipt_sha256") != receipt.receipt_sha256
        ):
            raise SecSubmissionImportError("submission capture does not commit its import receipt")
    return receipt
