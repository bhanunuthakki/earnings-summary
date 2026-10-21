"""Exact, deliberately promoted identities for deterministic full-text extraction."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from provenance.ooxml_extraction import classify_office_format
from runtime.deterministic import as_utc, parse_instant

FULLTEXT_EXTRACTOR_IDENTITY_POLICY_VERSION = "fulltext-extractor-identity-policy@2"
FULLTEXT_EXTRACTOR_NAME = "fulltext-evidence-backfill"
PDF_TABLE_EXTRACTOR_NAME = "PyMuPDF.Page.find_tables"


@dataclass(frozen=True, slots=True)
class FulltextExtractorIdentity:
    """One exact extractor implementation/configuration approved for a format lane."""

    name: str
    code_version: str
    config_sha256: str
    idempotency_namespace: str
    evidence_namespace: str
    hierarchy: Literal["flat", "structured"]


BASE_FULLTEXT_EXTRACTOR = FulltextExtractorIdentity(
    name=FULLTEXT_EXTRACTOR_NAME,
    code_version="fulltext-evidence-backfill@1",
    config_sha256=hashlib.sha256(
        b"fulltext-evidence-backfill-config-v1:utf8-replace,html-readable,pdf-pages"
    ).hexdigest(),
    idempotency_namespace="fulltext",
    evidence_namespace="fulltext",
    hierarchy="flat",
)
# Deliberate PDF-only promotion of the reviewed requirements.lock runtime.
# Readers must never select authority from their ambient installed parser.
RETAINED_PDF_FULLTEXT_EXTRACTOR_6_19_0 = FulltextExtractorIdentity(
    name=FULLTEXT_EXTRACTOR_NAME,
    code_version="fulltext-evidence-backfill@6-pdf-public-access;pypdf=6.19.0",
    config_sha256="ea890987a338e86830c366dac7c9587344de831e487695cb6753f51cbb042f15",
    idempotency_namespace="fulltext-pdf-v6",
    evidence_namespace="fulltext-pdf-v6",
    hierarchy="flat",
)
RETAINED_PDF_FULLTEXT_EXTRACTORS = (RETAINED_PDF_FULLTEXT_EXTRACTOR_6_19_0,)
PDF_FULLTEXT_PYPDF_VERSION = "6.20.1"
PDF_FULLTEXT_EXTRACTOR = FulltextExtractorIdentity(
    name=FULLTEXT_EXTRACTOR_NAME,
    code_version=f"fulltext-evidence-backfill@6-pdf-public-access;pypdf={PDF_FULLTEXT_PYPDF_VERSION}",
    config_sha256=hashlib.sha256(
        f"fulltext-evidence-backfill-config-v6-pdf:pdf-pages,empty-opening-password,"
        f"verify-permissions,pypdf={PDF_FULLTEXT_PYPDF_VERSION}".encode()
    ).hexdigest(),
    # Run keys must distinguish parser identities; page evidence keeps its revision lineage.
    idempotency_namespace="fulltext-pdf-v6-pypdf-6.20.1",
    evidence_namespace="fulltext-pdf-v6",
    hierarchy="flat",
)
OFFICE_FULLTEXT_EXTRACTOR = FulltextExtractorIdentity(
    name=FULLTEXT_EXTRACTOR_NAME,
    code_version="fulltext-evidence-backfill@3-office-native-inventories",
    config_sha256=hashlib.sha256(
        b"fulltext-evidence-backfill-config-v3-office:"
        b"bounded-ooxml,presentation-order,slide-text,chart-series-cache,"
        b"native-chart-table-hierarchy,per-slide-object-inventories,"
        b"worksheet-row,formula-and-raw-value,named-table-hierarchy,"
        b"per-sheet-and-workbook-named-table-inventories,raw-part-sha256"
    ).hexdigest(),
    idempotency_namespace="fulltext-office-v3",
    evidence_namespace="structured-office-v3",
    hierarchy="structured",
)
STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR = FulltextExtractorIdentity(
    name=FULLTEXT_EXTRACTOR_NAME,
    code_version="fulltext-evidence-backfill@4-replica-aware-web-archive",
    config_sha256=hashlib.sha256(
        b"fulltext-evidence-backfill-config-v4:"
        b"html-source-dom-paths,exact-decoded-source-char-spans,table-row-cell-hierarchy,"
        b"row=ordered-cell-unit-separator,table=structural-label,"
        b"dom-locator-sha256,max-html-depth=256,max-html-nodes=100000,"
        b"max-html-tag-name=128,"
        b"zip-sorted-nfc-paths,max-members=2048,max-member-bytes=67108864,"
        b"max-total-bytes=268435456,max-ratio=1000,supported-inner="
        b"txt,text,md,csv,json,xml,xsd,xbrl,svg,xhtml,html,htm,strict-utf8,"
        b"binary-image-replica=same-issuer-accession-current-sealed-snapshot"
    ).hexdigest(),
    idempotency_namespace="fulltext-structured-web-archive-v4",
    evidence_namespace="structured-web-archive",
    hierarchy="structured",
)


def pdf_table_extractor_code_version(
    *,
    detector_version: str,
    pymupdf_version: str,
    mupdf_version: str,
) -> str:
    """Return the exact runtime identity committed by a PDF-table artifact."""

    values = (detector_version, pymupdf_version, mupdf_version)
    if any(not value or ";" in value or "=" in value for value in values):
        raise ValueError("invalid_pdf_table_detector_version_identity")
    return f"{detector_version};pymupdf={pymupdf_version};mupdf={mupdf_version}"


_HTML_SUFFIXES = frozenset({".html", ".htm", ".xhtml"})
_ARCHIVE_SUFFIXES = frozenset({".zip"})
_ARCHIVE_MEDIA_TYPES = frozenset({"application/zip", "application/x-zip-compressed"})


def resolve_fulltext_extractor_identity(
    source_ref: str,
    media_type: str | None,
) -> FulltextExtractorIdentity:
    """Resolve the exact promoted identity; version numbers never self-promote."""

    if classify_office_format(source_ref, media_type) is not None:
        return OFFICE_FULLTEXT_EXTRACTOR
    suffix = Path(urlparse(source_ref).path).suffix.lower()
    normalized_media_type = None if media_type is None else media_type.partition(";")[0].lower()
    if normalized_media_type == "application/pdf" or suffix == ".pdf":
        return PDF_FULLTEXT_EXTRACTOR
    if (
        suffix in _ARCHIVE_SUFFIXES
        or normalized_media_type in _ARCHIVE_MEDIA_TYPES
        or normalized_media_type in {"text/html", "application/xhtml+xml"}
        or suffix in _HTML_SUFFIXES
    ):
        return STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR
    return BASE_FULLTEXT_EXTRACTOR


def readable_fulltext_extractor_identities(
    identity: FulltextExtractorIdentity,
) -> tuple[FulltextExtractorIdentity, ...]:
    """Return exact approved reader identities, with the promoted writer first."""

    if identity == PDF_FULLTEXT_EXTRACTOR:
        return (identity, *RETAINED_PDF_FULLTEXT_EXTRACTORS)
    return (identity,)


def select_approved_fulltext_run(
    conn: sqlite3.Connection,
    document_version_id: str,
    identity: FulltextExtractorIdentity,
    *,
    observed_through: datetime | None = None,
) -> str | None:
    """Choose one whole successful run, preferring the promoted identity.

    Retained runs remain usable even when their nodes have successors in an
    incomplete promotion. PDF native coverage must satisfy any available
    preflight inventory; OCR-required pages are selected by their own governor.
    Without preflight, promotion must preserve every substantive retained page.
    """

    cutoff = None if observed_through is None else as_utc(observed_through)
    required_pages: set[int] | None = None
    if (
        identity == PDF_FULLTEXT_EXTRACTOR
        and conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='ocr_document_assessments'"
        ).fetchone()
        is not None
    ):
        assessments = conn.execute(
            "SELECT assessment.assessment_id, assessment.page_count, assessment.assessed_at "
            "FROM ocr_document_assessments assessment "
            "JOIN evidence_document_versions document "
            "ON document.document_version_id=assessment.document_version_id "
            "WHERE assessment.document_version_id=? "
            "AND assessment.input_sha256=document.blob_sha256 "
            "ORDER BY assessment.assessed_at DESC, assessment.assessment_id DESC",
            (document_version_id,),
        ).fetchall()
        assessment = next(
            (row for row in assessments if cutoff is None or parse_instant(row[2]) <= cutoff),
            None,
        )
        if assessment is not None:
            pages = conn.execute(
                "SELECT page_number, native_character_count, requires_ocr "
                "FROM ocr_preflight_pages WHERE assessment_id=? ORDER BY page_number",
                (str(assessment[0]),),
            ).fetchall()
            if [int(page[0]) for page in pages] != list(range(1, int(assessment[1]) + 1)):
                return None
            required_pages = {
                int(page[0]) for page in pages if int(page[1]) > 0 and not bool(page[2])
            }
    candidates: list[tuple[str, set[int]]] = []
    clock_columns = "run.completed_at, node.recorded_at" if cutoff is not None else "NULL, NULL"
    for approved in readable_fulltext_extractor_identities(identity):
        rows = conn.execute(
            "SELECT run.extraction_run_id, CASE WHEN node.node_kind='pdf_page' "  # nosec B608 -- closed internal column list; all values remain bound
            "THEN json_extract(node.locator_json, '$.page_number') END, "
            f"{clock_columns}, "
            "node.node_kind<>'document' AND length(trim(node.text))>0 "
            "FROM evidence_extraction_runs run "
            "JOIN evidence_document_versions document "
            "ON document.document_version_id=run.document_version_id "
            "JOIN evidence_nodes node ON node.extraction_run_id=run.extraction_run_id "
            "WHERE run.document_version_id=? AND run.input_sha256=document.blob_sha256 "
            "AND run.outcome='succeeded' AND run.extractor_name=? "
            "AND run.extractor_code_version=? AND run.extractor_config_sha256=?",
            (
                document_version_id,
                approved.name,
                approved.code_version,
                approved.config_sha256,
            ),
        ).fetchall()
        # Like the seal validator, compare parsed UTC instants at stored precision.
        # Reject the entire run, including when only its document node is late.
        ineligible_run_ids = {
            str(row[0])
            for row in rows
            if cutoff is not None
            and (parse_instant(row[2]) > cutoff or parse_instant(row[3]) > cutoff)
        }
        rows = [row for row in rows if row[4] and str(row[0]) not in ineligible_run_ids]
        run_ids = {str(row[0]) for row in rows}
        if len(run_ids) > 1:
            raise ValueError("approved_fulltext_extraction_run_ambiguous")
        if not run_ids:
            continue
        page_numbers = {int(row[1]) for row in rows if row[1] is not None}
        if required_pages is not None and not required_pages.issubset(page_numbers):
            continue
        if required_pages is not None:
            return next(iter(run_ids))
        candidates.append((next(iter(run_ids)), page_numbers))
    if not candidates:
        return None
    if required_pages is None and len(candidates) > 1:
        promoted, retained = candidates[:2]
        if not retained[1] or not retained[1].issubset(promoted[1]):
            return retained[0]
    return candidates[0][0]
