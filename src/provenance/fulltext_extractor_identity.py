"""Exact, deliberately promoted identities for deterministic full-text extraction."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from provenance.ooxml_extraction import classify_office_format

FULLTEXT_EXTRACTOR_IDENTITY_POLICY_VERSION = "fulltext-extractor-identity-policy@1"
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
    if (
        suffix in _ARCHIVE_SUFFIXES
        or normalized_media_type in _ARCHIVE_MEDIA_TYPES
        or normalized_media_type in {"text/html", "application/xhtml+xml"}
        or suffix in _HTML_SUFFIXES
    ):
        return STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR
    return BASE_FULLTEXT_EXTRACTOR
