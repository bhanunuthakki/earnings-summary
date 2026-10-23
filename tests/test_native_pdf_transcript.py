"""Hermetic native untimed PDF transcript partitions and legacy lane guards."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from provenance import document_processing_evidence as processing
from provenance import native_pdf_transcript as transcript
from provenance.document_processing_evidence import (
    DocumentProcessingEvidenceError,
    publish_document_processing_evidence,
    verify_document_processing_evidence,
)
from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    EvidenceLocator,
    EvidenceNode,
    ExtractionRun,
    SourceObservation,
)
from provenance.fulltext_extractor_identity import (
    BASE_FULLTEXT_EXTRACTOR,
    PDF_FULLTEXT_EXTRACTOR,
    RETAINED_PDF_FULLTEXT_EXTRACTOR_6_19_0,
    RETAINED_PDF_FULLTEXT_EXTRACTOR_6_21_1,
    FulltextExtractorIdentity,
)
from provenance.native_pdf_transcript import (
    NativePdfTranscriptError,
    NativePdfTranscriptRequest,
    PdfTranscriptPageIdentity,
    pdf_transcript_source_labels,
    prepare_native_pdf_transcript_review,
    publish_native_pdf_transcript,
    verify_native_pdf_transcript,
)
from provenance.ocr_extraction import normalize_extracted_text
from sqlite_runtime import register_sqlite_integrity_functions
from tests.test_source_fact_repository import seed_foundation

T0 = datetime(2031, 1, 1, tzinfo=UTC)
T1, T2, T3 = (T0 + timedelta(hours=i) for i in (1, 2, 3))
TEXTS = (
    "Synthetic fixture opening without attribution.\n"
    " Operator : Welcome.\nAlice  Example: Prepared remarks.\n"
    "Ordinary speech: this colon is not a speaker label.\n"
    "Disclaimer unaffiliated source text.\n",
    "Unattributed page continuation.\nAlice  Example: More remarks.\nBob Example: Question.\n",
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def native_source(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[sqlite3.Connection]:
    identity = getattr(request, "param", PDF_FULLTEXT_EXTRACTOR)
    assert isinstance(identity, FulltextExtractorIdentity)
    path = tmp_path / "synthetic-native-transcript.db"
    migrated_db(path)
    conn = sqlite3.connect(path)
    register_sqlite_integrity_functions(conn)
    conn.execute("PRAGMA foreign_keys=ON")
    seed_foundation(conn)
    ledger = EvidenceLedger(conn)
    digest = _sha("synthetic PDF bytes")
    ledger.persist(
        ContentBlob(
            sha256=digest,
            byte_size=19,
            media_type="application/pdf",
            storage_uri="file:///synthetic-transcript.pdf",
            recorded_at=T0,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id="call-source",
            idempotency_key="call-source",
            source_kind="earnings_call",
            source_url="https://issuer.example/synthetic-transcript.pdf",
            blob_sha256=digest,
            source_published_at=None,
            filing_at=None,
            accepted_at=None,
            observed_at=T0,
            retrieved_at=T0,
            retrieval_config_sha256="a" * 64,
            collector_code_version="synthetic-test@1",
        )
    )
    ledger.persist(
        DocumentVersion(
            document_version_id="native-call",
            document_key="native-call",
            version_sequence=1,
            observation_id="call-source",
            blob_sha256=digest,
            issuer_id="issuer-1",
            ticker="TEST",
            document_type="earnings_transcript",
            form_type="IR",
            language="en",
            recorded_at=T0,
        )
    )
    nodes = (
        EvidenceNode(
            node_id="source-root",
            evidence_key="source-root",
            revision=1,
            extraction_run_id="pdf-run",
            node_kind="document",
            text="Synthetic native PDF extraction.",
            recorded_at=T0,
            locator=EvidenceLocator(source_ref="https://issuer.example/synthetic-transcript.pdf"),
        ),
        *(
            EvidenceNode(
                node_id=f"source-page-{number}",
                evidence_key=f"source-page-{number}",
                revision=1,
                extraction_run_id="pdf-run",
                parent_node_id="source-root",
                node_kind="pdf_page",
                text=text,
                locator=EvidenceLocator(
                    source_ref="https://issuer.example/synthetic-transcript.pdf", page_number=number
                ),
                recorded_at=T0,
            )
            for number, text in enumerate(TEXTS, 1)
        ),
    )
    output = json.dumps(
        [node.model_dump(mode="json", exclude_none=True) for node in nodes],
        sort_keys=True,
        separators=(",", ":"),
    )
    ledger.persist(
        ExtractionRun(
            extraction_run_id="pdf-run",
            idempotency_key="pdf-run",
            document_version_id="native-call",
            input_sha256=digest,
            extractor_name=identity.name,
            extractor_code_version=identity.code_version,
            extractor_config_sha256=identity.config_sha256,
            output_sha256=_sha(output),
            started_at=T0,
            completed_at=T0,
            outcome="succeeded",
        )
    )
    for node in nodes:
        ledger.persist(node)
    conn.execute(
        "INSERT INTO ocr_document_assessments VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "pdf-assessment",
            "pdf-assessment",
            "native-call",
            digest,
            "pypdf-native-text-preflight",
            "d" * 64,
            "pypdf-native-text-preflight@2-public-access",
            "e" * 64,
            len(TEXTS),
            "native_sufficient",
            None,
            T0,
        ),
    )
    for number, text in enumerate(TEXTS, 1):
        normalized = normalize_extracted_text(text)
        conn.execute(
            "INSERT INTO ocr_preflight_pages VALUES (?,?,?,?,?)",
            ("pdf-assessment", number, len(normalized), _sha(normalized), 0),
        )
    # Historical seals retain their exact identity after the reader policy changes.
    with monkeypatch.context() as historical_policy:
        historical_policy.setattr(processing, "PDF_FULLTEXT_EXTRACTOR", identity)
        publish_document_processing_evidence(
            conn,
            document_version_id="native-call",
            processing_lane="pdf_text",
            cutoff_at=T1,
            recorded_at=T1,
        )
    conn.commit()
    yield conn
    conn.close()


def _request(conn: sqlite3.Connection) -> NativePdfTranscriptRequest:
    seal = conn.execute(
        "SELECT evidence_seal_id FROM document_processing_evidence_headers "
        "WHERE processing_lane='pdf_text'"
    ).fetchone()[0]
    return prepare_native_pdf_transcript_review(
        conn,
        document_version_id="native-call",
        fulltext_extraction_run_id="pdf-run",
        pdf_text_evidence_seal_id=seal,
        pdf_text_cutoff_at=T1,
        speaker_labels=("Operator", "Alice  Example", "Bob Example"),
        unknown_line_prefixes=("Disclaimer",),
        reviewer="synthetic-reviewer",
        rationale="Synthetic source has explicit names and no audio timestamps.",
        recorded_at=T2,
    )


@pytest.mark.parametrize(
    "native_source",
    [
        RETAINED_PDF_FULLTEXT_EXTRACTOR_6_19_0,
        RETAINED_PDF_FULLTEXT_EXTRACTOR_6_21_1,
        PDF_FULLTEXT_EXTRACTOR,
    ],
    indirect=True,
    ids=["pypdf-6.19.0", "pypdf-6.21.1", "pypdf-6.21.2"],
)
def test_complete_exact_partition_keeps_unknown_and_both_lanes(native_source: sqlite3.Connection):
    conn = native_source
    request = _request(conn)
    before = conn.total_changes
    plan = publish_native_pdf_transcript(conn, request)
    assert conn.total_changes == before and plan.mode == "dry_run"
    receipt = publish_native_pdf_transcript(conn, request, apply=True)
    assert receipt.records_created > 0 and receipt.source_page_count == 2
    replay = publish_native_pdf_transcript(conn, request, apply=True)
    assert replay.records_created == 0
    assert (
        verify_native_pdf_transcript(
            conn, receipt.extraction_run_id, observed_through=T3
        ).span_count
        == receipt.span_count
    )
    page_parts: dict[int, list[str]] = {1: [], 2: []}
    locators: list[tuple[str, EvidenceLocator]] = []
    for text, locator_raw in conn.execute(
        "SELECT text,locator_json FROM evidence_nodes "
        "WHERE extraction_run_id=? AND node_kind='transcript_turn' "
        "ORDER BY rowid",
        (receipt.extraction_run_id,),
    ):
        locator = EvidenceLocator.model_validate_json(locator_raw)
        assert locator.page_number is not None
        page_parts[locator.page_number].append(text)
        assert TEXTS[locator.page_number - 1][locator.char_start : locator.char_end] == text
        assert locator.transcript_time_code_start is locator.transcript_time_code_end is None
        locators.append((text, locator))
    assert ["".join(page_parts[i]) for i in (1, 2)] == list(TEXTS)
    assert [loc.transcript_turn_sequence for _, loc in locators] == list(range(receipt.span_count))
    assert next(loc for text, loc in locators if "Ordinary speech:" in text).transcript_speaker == (
        "Alice  Example"
    )
    assert (
        next(loc for text, loc in locators if text.startswith("Disclaimer")).transcript_speaker
        is None
    )
    assert (
        next(loc for text, loc in locators if text.startswith("Unattributed")).transcript_speaker
        is None
    )
    assert receipt.unknown_span_count == 3
    for lane in ("transcript_turns", "transcript_speakers"):
        proof = publish_document_processing_evidence(
            conn,
            document_version_id="native-call",
            processing_lane=lane,
            cutoff_at=T3,
            recorded_at=T3,
        )
        assert proof.member_count == receipt.span_count
        assert (
            verify_document_processing_evidence(
                conn,
                proof.evidence_seal_id,
                document_version_id="native-call",
                processing_lane=lane,
                cutoff_at=T3,
            ).member_count
            == receipt.span_count
        )


@pytest.mark.parametrize("native_source", [RETAINED_PDF_FULLTEXT_EXTRACTOR_6_19_0], indirect=True)
@pytest.mark.parametrize("complete_promotion", [False, True])
def test_no_ocr_selection_keeps_one_run_and_transcript_replays_its_pin(
    native_source: sqlite3.Connection, complete_promotion: bool
) -> None:
    conn = native_source
    request = _request(conn)
    receipt = publish_native_pdf_transcript(conn, request, apply=True)
    promoted = tuple(
        EvidenceNode(
            node_id="promoted:" + node_id,
            evidence_key=node_id,
            revision=2,
            supersedes_node_id=node_id,
            extraction_run_id="promoted-pdf-run",
            parent_node_id="promoted:" + parent_id if parent_id else None,
            node_kind=kind,
            text=text,
            locator=EvidenceLocator.model_validate_json(locator),
            recorded_at=T0,
        )
        for node_id, parent_id, kind, text, locator in conn.execute(
            "SELECT node_id,parent_node_id,node_kind,text,locator_json FROM evidence_nodes "
            "WHERE extraction_run_id='pdf-run' ORDER BY rowid"
        )
        if complete_promotion or node_id != "source-page-2"
    )
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ExtractionRun(
            extraction_run_id="promoted-pdf-run",
            idempotency_key="promoted-pdf-run",
            document_version_id="native-call",
            input_sha256=request.input_sha256,
            extractor_name=PDF_FULLTEXT_EXTRACTOR.name,
            extractor_code_version=PDF_FULLTEXT_EXTRACTOR.code_version,
            extractor_config_sha256=PDF_FULLTEXT_EXTRACTOR.config_sha256,
            output_sha256=_sha(
                json.dumps(
                    [node.model_dump(mode="json", exclude_none=True) for node in promoted],
                    sort_keys=True,
                    separators=(",", ":"),
                )
            ),
            started_at=T0,
            completed_at=T0,
            outcome="succeeded",
        )
    )
    for node in promoted:
        ledger.persist(node)
    proof = publish_document_processing_evidence(
        conn,
        document_version_id="native-call",
        processing_lane="pdf_ocr",
        cutoff_at=T3,
        recorded_at=T3,
    )
    assert proof.extraction_run_id == ("promoted-pdf-run" if complete_promotion else "pdf-run")
    members = {
        row[0]
        for row in conn.execute(
            "SELECT native_id FROM document_processing_evidence_members WHERE evidence_seal_id=?",
            (proof.evidence_seal_id,),
        )
    }
    assert members == (
        {node.node_id for node in promoted[1:]}
        if complete_promotion
        else {page.evidence_node_id for page in request.pages}
    )
    assert (
        verify_document_processing_evidence(
            conn,
            proof.evidence_seal_id,
            document_version_id="native-call",
            processing_lane="pdf_ocr",
            cutoff_at=T3,
        ).extraction_run_id
        == proof.extraction_run_id
    )
    # The complete promoted run is already visible at the review's original cutoff.
    # Replay must still use the retained run and seal recorded in the review.
    assert (
        verify_native_pdf_transcript(
            conn, receipt.extraction_run_id, observed_through=T3
        ).review_sha256
        == receipt.review_sha256
    )


@pytest.mark.parametrize(
    ("native_source", "reason"),
    [
        (BASE_FULLTEXT_EXTRACTOR, "native_pdf_transcript_source_extractor_not_approved"),
        (
            replace(PDF_FULLTEXT_EXTRACTOR, code_version="unapproved@1"),
            "pdf_extractor_identity_not_approved",
        ),
        (
            replace(PDF_FULLTEXT_EXTRACTOR, config_sha256="f" * 64),
            "pdf_extractor_identity_not_approved",
        ),
        (replace(PDF_FULLTEXT_EXTRACTOR, name="unapproved"), "native_extraction_run_missing"),
    ],
    indirect=["native_source"],
    ids=["legacy-base", "unapproved-code", "unapproved-config", "unapproved-name"],
)
def test_transcript_replay_rejects_unapproved_source_identity(
    native_source: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    conn = native_source
    name, code, config = conn.execute(
        "SELECT extractor_name,extractor_code_version,extractor_config_sha256 "
        "FROM evidence_extraction_runs WHERE extraction_run_id='pdf-run'"
    ).fetchone()
    identity = replace(PDF_FULLTEXT_EXTRACTOR, name=name, code_version=code, config_sha256=config)
    with monkeypatch.context() as historical_policy:
        historical_policy.setattr(processing, "PDF_FULLTEXT_EXTRACTOR", identity)
        historical_policy.setattr(transcript, "PDF_FULLTEXT_EXTRACTOR", identity)
        receipt = publish_native_pdf_transcript(conn, _request(conn), apply=True)
    before = conn.total_changes
    with pytest.raises((NativePdfTranscriptError, DocumentProcessingEvidenceError), match=reason):
        verify_native_pdf_transcript(conn, receipt.extraction_run_id, observed_through=T3)
    assert conn.total_changes == before


@pytest.mark.parametrize("change", ["invented_speaker", "missing_page", "page_hash", "wrong_run"])
def test_review_mismatch_fails_before_writes(native_source: sqlite3.Connection, change: str):
    conn = native_source
    request = _request(conn)
    if change == "invented_speaker":
        request = request.model_copy(
            update={"speaker_labels": (*request.speaker_labels, "Invented")}
        )
    elif change == "missing_page":
        request = request.model_copy(update={"pages": request.pages[:1]})
    elif change == "page_hash":
        page = request.pages[0].model_copy(update={"text_sha256": "0" * 64})
        request = request.model_copy(update={"pages": (page, request.pages[1])})
    else:
        request = request.model_copy(update={"fulltext_extraction_run_id": "other-run"})
    before = conn.total_changes
    with pytest.raises((NativePdfTranscriptError, DocumentProcessingEvidenceError)):
        publish_native_pdf_transcript(conn, request, apply=True)
    assert conn.total_changes == before


@pytest.mark.parametrize(
    "tamper", ["source_text", "span_text", "speaker", "offset", "delete", "review", "forged_output"]
)
def test_native_replay_rejects_source_and_partition_tamper(
    native_source: sqlite3.Connection, tamper: str
):
    conn = native_source
    receipt = publish_native_pdf_transcript(conn, _request(conn), apply=True)
    proofs = [
        publish_document_processing_evidence(
            conn,
            document_version_id="native-call",
            processing_lane=lane,
            cutoff_at=T3,
            recorded_at=T3,
        )
        for lane in ("transcript_turns", "transcript_speakers")
    ]
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='evidence_nodes'"
    ).fetchall():
        conn.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
    span = conn.execute(
        "SELECT node_id,locator_json FROM evidence_nodes "
        "WHERE extraction_run_id=? AND node_kind='transcript_turn' "
        "AND json_extract(locator_json,'$.transcript_speaker') IS NOT NULL "
        "ORDER BY rowid LIMIT 1",
        (receipt.extraction_run_id,),
    ).fetchone()
    if tamper == "source_text":
        conn.execute(
            "UPDATE evidence_nodes SET text=text||' changed' WHERE node_id='source-page-1'"
        )
    elif tamper in {"span_text", "forged_output"}:
        conn.execute("UPDATE evidence_nodes SET text=text||' changed' WHERE node_id=?", (span[0],))
    elif tamper in {"speaker", "offset"}:
        locator = json.loads(span[1])
        locator["transcript_speaker" if tamper == "speaker" else "char_start"] = (
            "Invented Speaker" if tamper == "speaker" else 4
        )
        loc = EvidenceLocator.model_validate(locator)
        conn.execute(
            "UPDATE evidence_nodes SET locator_json=?,locator_sha256=? WHERE node_id=?",
            (loc.model_dump_json(exclude_none=True), loc.canonical_sha256, span[0]),
        )
    elif tamper == "delete":
        conn.execute("DELETE FROM evidence_nodes WHERE node_id=?", (span[0],))
    else:
        conn.execute(
            "UPDATE evidence_nodes SET text='{}' WHERE extraction_run_id=? "
            "AND node_kind='document'",
            (receipt.extraction_run_id,),
        )
    if tamper == "forged_output":
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='evidence_extraction_runs'"
        ).fetchall():
            conn.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
        forged_nodes = []
        fields = tuple(key for key in EvidenceNode.model_fields if key != "locator")
        for row in conn.execute(
            "SELECT " + ",".join(fields) + ",locator_json FROM evidence_nodes "
            "WHERE extraction_run_id=? ORDER BY rowid",
            (receipt.extraction_run_id,),
        ):
            values = dict(zip(fields, row[:-1], strict=True))
            values["locator"] = json.loads(row[-1])
            forged_nodes.append(
                EvidenceNode.model_validate(values).model_dump(mode="json", exclude_none=True)
            )
        conn.execute(
            "UPDATE evidence_extraction_runs SET output_sha256=? WHERE extraction_run_id=?",
            (
                _sha(json.dumps(forged_nodes, sort_keys=True, separators=(",", ":"))),
                receipt.extraction_run_id,
            ),
        )
    for proof, lane in zip(proofs, ("transcript_turns", "transcript_speakers"), strict=True):
        with pytest.raises((DocumentProcessingEvidenceError, ValueError)):
            verify_document_processing_evidence(
                conn,
                proof.evidence_seal_id,
                document_version_id="native-call",
                processing_lane=lane,
                cutoff_at=T3,
            )


def test_raw_label_candidates_preserve_spacing_and_source_text():
    assert pdf_transcript_source_labels(" Operator : hi\nAlice  Example: text\n") == (
        "Operator",
        "Alice  Example",
    )
    with pytest.raises(ValueError, match="source order"):
        NativePdfTranscriptRequest(
            document_version_id="x",
            input_sha256="a" * 64,
            fulltext_extraction_run_id="r",
            pdf_text_evidence_seal_id="s",
            pdf_text_cutoff_at=T1,
            pages=(
                PdfTranscriptPageIdentity(
                    page_number=2,
                    evidence_node_id="p",
                    text_sha256="a" * 64,
                    locator_sha256="a" * 64,
                ),
            ),
            speaker_labels=("Operator",),
            reviewer="test",
            rationale="synthetic",
            recorded_at=T2,
        )
