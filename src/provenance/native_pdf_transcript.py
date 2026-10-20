"""Reviewed untimed PDF transcript turns over exact native page partitions.

Source labels identify speakers only where they occur explicitly. Every page
starts with unknown attribution. No audio timing or cross-page attribution is
inferred. The existing native PDF text seal owns source-page completeness.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from provenance.evidence_ledger import EvidenceLedger, EvidenceLocator, EvidenceNode, ExtractionRun
from provenance.fulltext_extractor_identity import PDF_FULLTEXT_EXTRACTOR
from runtime.deterministic import as_utc
from runtime.deterministic import sha256_text as _sha

EXTRACTOR_NAME = "reviewed-native-pdf-transcript"
EXTRACTOR_VERSION = "native-pdf-transcript@1"
EXTRACTOR_CONFIG_SHA256 = hashlib.sha256(
    b"native-pdf-transcript@1:promoted-pdf-text-seal,exact-all-page-partition,"
    b"reviewed-raw-line-labels,reviewed-colon-boundaries,explicit-unknown-headings,"
    b"unknown-page-openings,no-cross-page-carry,timecodes-unavailable"
).hexdigest()


class NativePdfTranscriptError(ValueError):
    """A source, review, partition, or immutable replay does not match."""


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PdfTranscriptPageIdentity(_Closed):
    page_number: int = Field(gt=0)
    evidence_node_id: str = Field(min_length=1, max_length=128)
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    locator_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class NativePdfTranscriptRequest(_Closed):
    schema_version: Literal["native-pdf-transcript-review/v1"] = "native-pdf-transcript-review/v1"
    document_version_id: str = Field(min_length=1, max_length=128)
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fulltext_extraction_run_id: str = Field(min_length=1, max_length=128)
    pdf_text_evidence_seal_id: str = Field(min_length=1, max_length=128)
    pdf_text_cutoff_at: AwareDatetime
    pages: tuple[PdfTranscriptPageIdentity, ...] = Field(min_length=1, max_length=500)
    speaker_labels: tuple[str, ...] = Field(min_length=1, max_length=100)
    unknown_line_prefixes: tuple[str, ...] = Field(default=(), max_length=100)
    reviewer: str = Field(min_length=1, max_length=256)
    rationale: str = Field(min_length=1, max_length=4096)
    timecode_status: Literal["unavailable_in_source_pdf"] = "unavailable_in_source_pdf"
    attribution_policy: Literal["explicit_raw_label_or_unknown_per_page/v1"] = (
        "explicit_raw_label_or_unknown_per_page/v1"
    )
    recorded_at: AwareDatetime

    @model_validator(mode="after")
    def _review(self) -> Self:
        if tuple(page.page_number for page in self.pages) != tuple(range(1, len(self.pages) + 1)):
            raise ValueError("transcript review must contain every page in source order")
        if len({page.evidence_node_id for page in self.pages}) != len(self.pages):
            raise ValueError("transcript page identities must be unique")
        for values in (self.speaker_labels, self.unknown_line_prefixes):
            if len(set(values)) != len(values) or any(
                not value.strip(" \t\r\n\u200b")
                or len(value) > 512
                or "\n" in value
                or "\r" in value
                or ":" in value
                for value in values
            ):
                raise ValueError("review labels must be distinct exact raw single-line prefixes")
        if set(self.speaker_labels) & set(self.unknown_line_prefixes):
            raise ValueError("speaker and unknown labels must not overlap")
        if self.recorded_at < self.pdf_text_cutoff_at:
            raise ValueError("transcript recording must follow its source evidence cutoff")
        return self


class NativePdfTranscriptReceipt(_Closed):
    extraction_run_id: str
    document_version_id: str
    review_sha256: str
    source_page_count: int
    span_count: int
    unknown_span_count: int
    timecode_status: Literal["unavailable_in_source_pdf"] = "unavailable_in_source_pdf"
    mode: Literal["dry_run", "apply", "verified"]
    records_created: int = 0


def _rows(conn: sqlite3.Connection, sql: str, args: tuple[object, ...]) -> list[dict[str, object]]:
    cursor = conn.execute(sql, args)
    columns = tuple(item[0] for item in cursor.description or ())
    return [dict(zip(columns, tuple(row), strict=True)) for row in cursor.fetchall()]


def _one(conn: sqlite3.Connection, sql: str, args: tuple[object, ...]) -> dict[str, object]:
    rows = _rows(conn, sql, args)
    if len(rows) != 1:
        raise NativePdfTranscriptError("native_pdf_transcript_source_missing_or_ambiguous")
    return rows[0]


def _nodes(conn: sqlite3.Connection, run_id: str) -> tuple[EvidenceNode, ...]:
    result: list[EvidenceNode] = []
    for row in _rows(
        conn, "SELECT * FROM evidence_nodes WHERE extraction_run_id=? ORDER BY rowid", (run_id,)
    ):
        fields = {key: row[key] for key in EvidenceNode.model_fields if key != "locator"}
        fields["locator"] = json.loads(str(row["locator_json"]))
        result.append(EvidenceNode.model_validate(fields))
    return tuple(result)


def _output_sha(nodes: tuple[EvidenceNode, ...]) -> str:
    return _sha(
        json.dumps(
            [node.model_dump(mode="json", exclude_none=True) for node in nodes],
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def pdf_transcript_source_labels(text: str) -> tuple[str, ...]:
    """Return exact line prefixes before colons for explicit source review."""
    result: list[str] = []
    for line in text.splitlines(keepends=True):
        prefix, separator, _body = line.partition(":")
        label = prefix.strip(" \t\r\n\u200b")
        if separator and label and len(label) <= 512:
            result.append(label)
    return tuple(dict.fromkeys(result))


def prepare_native_pdf_transcript_review(
    conn: sqlite3.Connection,
    *,
    document_version_id: str,
    fulltext_extraction_run_id: str,
    pdf_text_evidence_seal_id: str,
    pdf_text_cutoff_at: datetime,
    speaker_labels: tuple[str, ...],
    reviewer: str,
    rationale: str,
    recorded_at: datetime,
    unknown_line_prefixes: tuple[str, ...] = (),
) -> NativePdfTranscriptRequest:
    """Bind a source-label review to every page in the native PDF text inventory."""
    run = _one(
        conn,
        "SELECT input_sha256 FROM evidence_extraction_runs WHERE extraction_run_id=?",
        (fulltext_extraction_run_id,),
    )
    pages = sorted(
        (node for node in _nodes(conn, fulltext_extraction_run_id) if node.node_kind == "pdf_page"),
        key=lambda node: (node.locator.page_number or 0) if node.locator else 0,
    )
    request = NativePdfTranscriptRequest(
        document_version_id=document_version_id,
        input_sha256=str(run["input_sha256"]),
        fulltext_extraction_run_id=fulltext_extraction_run_id,
        pdf_text_evidence_seal_id=pdf_text_evidence_seal_id,
        pdf_text_cutoff_at=pdf_text_cutoff_at,
        pages=tuple(
            PdfTranscriptPageIdentity(
                page_number=node.locator.page_number or 0,
                evidence_node_id=node.node_id,
                text_sha256=_sha(node.text),
                locator_sha256=node.locator.canonical_sha256,
            )
            for node in pages
            if node.locator is not None
        ),
        speaker_labels=speaker_labels,
        unknown_line_prefixes=unknown_line_prefixes,
        reviewer=reviewer,
        rationale=rationale,
        recorded_at=recorded_at,
    )
    publish_native_pdf_transcript(conn, request)
    return request


def _source_pages(
    conn: sqlite3.Connection, request: NativePdfTranscriptRequest, observed_through: datetime
) -> tuple[EvidenceNode, ...]:
    # Local import avoids a module cycle when the processing adapter verifies
    # this profile. The nested lane is pdf_text, never another transcript lane.
    from provenance.document_processing_evidence import verify_document_processing_evidence

    document = _one(
        conn,
        "SELECT document.blob_sha256,document.document_type,blob.media_type "
        "FROM evidence_document_versions document JOIN evidence_content_blobs blob "
        "ON blob.sha256=document.blob_sha256 WHERE document.document_version_id=?",
        (request.document_version_id,),
    )
    if (
        document["blob_sha256"] != request.input_sha256
        or document["media_type"] != "application/pdf"
        or "transcript" not in str(document["document_type"]).lower()
    ):
        raise NativePdfTranscriptError("native_pdf_transcript_document_identity_mismatch")
    proof = verify_document_processing_evidence(
        conn,
        request.pdf_text_evidence_seal_id,
        document_version_id=request.document_version_id,
        processing_lane="pdf_text",
        cutoff_at=request.pdf_text_cutoff_at,
        observed_through=observed_through,
    )
    if proof.extraction_run_id != request.fulltext_extraction_run_id:
        raise NativePdfTranscriptError("native_pdf_transcript_source_run_mismatch")
    run = _one(
        conn,
        "SELECT * FROM evidence_extraction_runs WHERE extraction_run_id=?",
        (request.fulltext_extraction_run_id,),
    )
    if (
        run["extractor_name"],
        run["extractor_code_version"],
        run["extractor_config_sha256"],
        run["input_sha256"],
        run["document_version_id"],
        run["outcome"],
    ) != (
        PDF_FULLTEXT_EXTRACTOR.name,
        PDF_FULLTEXT_EXTRACTOR.code_version,
        PDF_FULLTEXT_EXTRACTOR.config_sha256,
        request.input_sha256,
        request.document_version_id,
        "succeeded",
    ):
        raise NativePdfTranscriptError("native_pdf_transcript_source_extractor_not_approved")
    all_nodes = _nodes(conn, request.fulltext_extraction_run_id)
    if _output_sha(all_nodes) != run["output_sha256"]:
        raise NativePdfTranscriptError("native_pdf_transcript_source_output_changed")
    pages = tuple(
        sorted(
            (node for node in all_nodes if node.node_kind == "pdf_page"),
            key=lambda node: (node.locator.page_number or 0) if node.locator else 0,
        )
    )
    identities = tuple(
        PdfTranscriptPageIdentity(
            page_number=node.locator.page_number or 0,
            evidence_node_id=node.node_id,
            text_sha256=_sha(node.text),
            locator_sha256=node.locator.canonical_sha256,
        )
        for node in pages
        if node.locator is not None
    )
    if identities != request.pages or len(pages) != proof.member_count:
        raise NativePdfTranscriptError("native_pdf_transcript_page_inventory_changed")
    if proof.sealed_at > request.recorded_at or as_utc(observed_through) < request.recorded_at:
        raise NativePdfTranscriptError("native_pdf_transcript_source_clock_mismatch")
    return pages


def _partition(
    pages: tuple[EvidenceNode, ...], request: NativePdfTranscriptRequest, run_id: str
) -> tuple[EvidenceNode, ...]:
    root_id = "pdf-transcript-root:" + _sha(run_id)
    nodes = [
        EvidenceNode(
            node_id=root_id,
            evidence_key=root_id,
            revision=1,
            extraction_run_id=run_id,
            node_kind="document",
            text=request.model_dump_json(),
            recorded_at=request.recorded_at,
            locator=EvidenceLocator(
                source_ref=pages[0].locator.source_ref if pages[0].locator else None
            ),
        )
    ]
    seen_speakers: set[str] = set()
    seen_unknown: set[str] = set()
    sequence = 0
    for page in pages:
        assert page.locator is not None
        boundaries: dict[int, str | None] = {0: None}
        offset = 0
        for line in page.text.splitlines(keepends=True):
            prefix, separator, _body = line.partition(":")
            label = prefix.strip(" \t\r\n\u200b")
            trim = len(prefix) - len(prefix.lstrip(" \t\r\n\u200b"))
            if separator and label in request.speaker_labels:
                start = offset + trim
                boundaries[start] = label
                seen_speakers.add(label)
            stripped = line.lstrip(" \t\r\n\u200b")
            for heading in request.unknown_line_prefixes:
                if stripped.startswith(heading) and (
                    len(stripped) == len(heading) or stripped[len(heading)] in " \t\r\n\u200b:"
                ):
                    boundaries[offset + len(line) - len(stripped)] = None
                    seen_unknown.add(heading)
            offset += len(line)
        starts = sorted(boundaries)
        for start, end in zip(starts, (*starts[1:], len(page.text)), strict=True):
            if start == end:
                continue
            node_id = "pdf-transcript-span:" + _sha(f"{run_id}\0{sequence}")
            nodes.append(
                EvidenceNode(
                    node_id=node_id,
                    evidence_key=node_id,
                    revision=1,
                    extraction_run_id=run_id,
                    parent_node_id=root_id,
                    node_kind="transcript_turn",
                    text=page.text[start:end],
                    locator=EvidenceLocator(
                        source_ref=page.locator.source_ref,
                        page_number=page.locator.page_number,
                        char_start=start,
                        char_end=end,
                        transcript_turn_sequence=sequence,
                        transcript_speaker=boundaries[start],
                    ),
                    recorded_at=request.recorded_at,
                )
            )
            sequence += 1
    if seen_speakers != set(request.speaker_labels) or seen_unknown != set(
        request.unknown_line_prefixes
    ):
        raise NativePdfTranscriptError("native_pdf_transcript_review_label_not_observed")
    return tuple(nodes)


def publish_native_pdf_transcript(
    conn: sqlite3.Connection, request: NativePdfTranscriptRequest, *, apply: bool = False
) -> NativePdfTranscriptReceipt:
    """Admit reviewed exact native spans; caller owns transaction and commit."""
    pages = _source_pages(conn, request, request.recorded_at)
    review_sha = _sha(request.model_dump_json())
    run_id = "native-pdf-transcript:" + review_sha
    nodes = _partition(pages, request, run_id)
    run = ExtractionRun(
        extraction_run_id=run_id,
        idempotency_key=run_id,
        document_version_id=request.document_version_id,
        input_sha256=request.input_sha256,
        extractor_name=EXTRACTOR_NAME,
        extractor_config_sha256=EXTRACTOR_CONFIG_SHA256,
        extractor_code_version=EXTRACTOR_VERSION,
        output_sha256=_output_sha(nodes),
        started_at=request.recorded_at,
        completed_at=request.recorded_at,
        outcome="succeeded",
    )
    created = 0
    if apply:
        conn.execute("SAVEPOINT native_pdf_transcript")
        try:
            ledger = EvidenceLedger(conn)
            created += int(ledger.persist(run).created)
            for node in nodes:
                created += int(ledger.persist(node).created)
            verify_native_pdf_transcript(conn, run_id, observed_through=request.recorded_at)
            conn.execute("RELEASE native_pdf_transcript")
        except BaseException:
            conn.execute("ROLLBACK TO native_pdf_transcript")
            conn.execute("RELEASE native_pdf_transcript")
            raise
    return NativePdfTranscriptReceipt(
        extraction_run_id=run_id,
        document_version_id=request.document_version_id,
        review_sha256=review_sha,
        source_page_count=len(pages),
        span_count=len(nodes) - 1,
        unknown_span_count=sum(
            node.locator is not None and node.locator.transcript_speaker is None
            for node in nodes[1:]
        ),
        mode="apply" if apply else "dry_run",
        records_created=created,
    )


def verify_native_pdf_transcript(
    conn: sqlite3.Connection, extraction_run_id: str, *, observed_through: datetime
) -> NativePdfTranscriptReceipt:
    """Replay the source seal and complete page partitions without caller callbacks."""
    run = _one(
        conn,
        "SELECT * FROM evidence_extraction_runs WHERE extraction_run_id=?",
        (extraction_run_id,),
    )
    nodes = _nodes(conn, extraction_run_id)
    roots = [node for node in nodes if node.node_kind == "document"]
    if len(roots) != 1:
        raise NativePdfTranscriptError("native_pdf_transcript_review_root_missing_or_ambiguous")
    request = NativePdfTranscriptRequest.model_validate_json(roots[0].text)
    pages = _source_pages(conn, request, observed_through)
    expected = _partition(pages, request, extraction_run_id)
    review_sha = _sha(request.model_dump_json())
    if (
        extraction_run_id != "native-pdf-transcript:" + review_sha
        or run["document_version_id"] != request.document_version_id
        or run["input_sha256"] != request.input_sha256
        or run["extractor_name"] != EXTRACTOR_NAME
        or run["extractor_code_version"] != EXTRACTOR_VERSION
        or run["extractor_config_sha256"] != EXTRACTOR_CONFIG_SHA256
        or run["outcome"] != "succeeded"
        or as_utc(datetime.fromisoformat(str(run["started_at"]))) != request.recorded_at
        or as_utc(datetime.fromisoformat(str(run["completed_at"]))) != request.recorded_at
        or nodes != expected
        or _output_sha(nodes) != run["output_sha256"]
    ):
        raise NativePdfTranscriptError("native_pdf_transcript_partition_or_run_changed")
    return NativePdfTranscriptReceipt(
        extraction_run_id=extraction_run_id,
        document_version_id=request.document_version_id,
        review_sha256=review_sha,
        source_page_count=len(pages),
        span_count=len(nodes) - 1,
        unknown_span_count=sum(
            node.locator is not None and node.locator.transcript_speaker is None
            for node in nodes[1:]
        ),
        mode="verified",
    )
