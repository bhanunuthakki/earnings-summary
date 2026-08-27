"""Fail-closed inventory of every file in one SEC accession package.

The archive ``index.json`` is the authoritative directory listing, but its
``type`` field is an icon category (for example ``text.gif``), not the EDGAR
document type.  The accession's official filing-index manifest supplies the
document type, sequence, filename, and description.  This module validates and
joins both authority responses so exhibits are never inferred from filenames.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Literal, cast
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field

_ACCESSION = re.compile(r"^\d{10}-\d{2}-\d{6}$")
_SAFE_FILENAME = re.compile(r"^[^/\\\x00-\x1f]+$")
_EXHIBIT_TYPE = re.compile(r"^EX-\d{1,3}(?:\.\d+)*(?:[A-Z])?$")
_FINANCIAL_EXHIBIT_TYPE = re.compile(r"^EX-(?:101(?:\.[A-Z0-9]+)?|104)$")
_RENDERED_FINANCIAL_REPORT = re.compile(r"^R\d+\.htm$", re.IGNORECASE)
_ARCHIVE_BASE = "https://www.sec.gov/Archives/edgar/data"
_FINANCIAL_FILENAMES = frozenset({"FilingSummary.xml", "MetaLinks.json"})
_FINANCIAL_SUFFIXES = (
    ".xsd",
    "_cal.xml",
    "_def.xml",
    "_lab.xml",
    "_pre.xml",
    "_htm.xml",
    "-xbrl.zip",
)


class SecFilingPackageContractError(ValueError):
    """The authority package cannot support a complete, auditable inventory."""


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


AttachmentRole = Literal[
    "primary_document",
    "exhibit",
    "financial_report",
    "supporting_attachment",
]
SourceInventoryPresence = Literal["matched", "index_only", "manifest_only"]


class SecFilingPackageAttachment(_Closed):
    attachment_id: str = Field(min_length=64, max_length=64)
    parent_accession_number: str
    filename: str
    declared_type: str | None
    sequence: int | None = Field(default=None, gt=0)
    description: str | None
    index_media_icon: str | None
    byte_size: int | None = Field(default=None, ge=0)
    last_modified_at: datetime | None
    source_url: str
    role: AttachmentRole
    inventory_presence: SourceInventoryPresence


class ParsedSecFilingPackage(_Closed):
    cik: str
    accession_number: str
    form_type: str
    primary_document: str
    index_url: str
    filing_manifest_url: str
    attachments: tuple[SecFilingPackageAttachment, ...] = Field(min_length=1)

    @property
    def exhibits(self) -> tuple[SecFilingPackageAttachment, ...]:
        return tuple(item for item in self.attachments if item.role == "exhibit")


class _ArchiveItem(_Closed):
    name: str
    type: str
    size: str
    last_modified: str = Field(alias="last-modified")


class _ManifestDocument(_Closed):
    declared_type: str | None
    sequence: int | None = Field(default=None, gt=0)
    filename: str
    description: str | None
    byte_size: int | None = Field(default=None, ge=0)


def filing_package_index_url(cik: str, accession_number: str) -> str:
    """Return the exact authority URL for an accession directory listing."""

    normalized_cik, accession = _identities(cik, accession_number)
    return f"{_ARCHIVE_BASE}/{int(normalized_cik)}/{accession.replace('-', '')}/index.json"


def filing_package_manifest_url(cik: str, accession_number: str) -> str:
    """Return the exact authority URL for the accession attachment manifest."""

    normalized_cik, accession = _identities(cik, accession_number)
    return (
        f"{_ARCHIVE_BASE}/{int(normalized_cik)}/{accession.replace('-', '')}/{accession}-index.html"
    )


def parse_sec_filing_package_inventory(
    *,
    cik: str,
    accession_number: str,
    form_type: str,
    primary_document: str,
    index_body: bytes,
    filing_manifest_body: bytes,
) -> ParsedSecFilingPackage:
    """Validate and join one accession's directory and SGML document manifest."""

    normalized_cik, accession = _identities(cik, accession_number)
    normalized_form = form_type.strip().upper()
    if not normalized_form:
        raise SecFilingPackageContractError("form_type must not be empty")
    primary = _filename(primary_document, label="primary document")
    index_root = _json_object(index_body)
    directory_raw = index_root.get("directory")
    if not isinstance(directory_raw, dict):
        raise SecFilingPackageContractError("archive index has no directory object")
    directory = cast(Mapping[str, object], directory_raw)
    directory_name = str(directory.get("name") or "").strip()
    expected_directory = f"/Archives/edgar/data/{int(normalized_cik)}/{accession.replace('-', '')}"
    if directory_name != expected_directory:
        raise SecFilingPackageContractError(
            "archive directory identity does not match requested accession"
        )
    parent_dir = str(directory.get("parent-dir") or "").rstrip("/")
    expected_parent = f"/Archives/edgar/data/{int(normalized_cik)}"
    if parent_dir != expected_parent:
        raise SecFilingPackageContractError(
            "archive parent directory identity does not match requested CIK"
        )
    item_values = directory.get("item")
    if not isinstance(item_values, list):
        raise SecFilingPackageContractError("archive directory item must be an array")
    archive_items = _archive_items(cast(list[object], item_values))
    submission_documents = _manifest_documents(
        filing_manifest_body,
        cik=normalized_cik,
        accession_number=accession,
    )
    archive_by_name = {item.name: item for item in archive_items}
    primary_manifest = submission_documents.get(primary)
    if primary_manifest is None:
        raise SecFilingPackageContractError(
            "primary document is not present in filing-index manifest"
        )
    if (
        primary_manifest.declared_type is None
        or primary_manifest.declared_type.upper() != normalized_form
    ):
        raise SecFilingPackageContractError(
            "primary document type conflicts with submissions inventory form"
        )

    base = f"{_ARCHIVE_BASE}/{int(normalized_cik)}/{accession.replace('-', '')}"
    ordered_names = [item.name for item in archive_items]
    ordered_names.extend(name for name in submission_documents if name not in archive_by_name)
    attachments: list[SecFilingPackageAttachment] = []
    for name in ordered_names:
        item = archive_by_name.get(name)
        manifest = submission_documents.get(name)
        declared_type = (
            None
            if manifest is None or manifest.declared_type is None
            else manifest.declared_type.upper()
        )
        index_byte_size = None if item is None else _byte_size(item.size, item.name)
        byte_size = (
            index_byte_size
            if index_byte_size is not None
            else None
            if manifest is None
            else manifest.byte_size
        )
        if (
            item is not None
            and manifest is not None
            and manifest.byte_size is not None
            and index_byte_size is not None
            and manifest.byte_size != index_byte_size
        ):
            raise SecFilingPackageContractError(
                f"{name} size conflicts between archive index and manifest"
            )
        modified: datetime | None = None
        if item is not None:
            try:
                modified = datetime.strptime(item.last_modified, "%Y-%m-%d %H:%M:%S")
            except ValueError as exc:
                raise SecFilingPackageContractError(f"{name} has invalid last-modified") from exc
        presence: SourceInventoryPresence = (
            "matched"
            if item is not None and manifest is not None
            else "index_only"
            if item is not None
            else "manifest_only"
        )
        role = _role(
            filename=name,
            declared_type=declared_type,
            primary_document=primary,
        )
        identity = json.dumps(
            {
                "accession_number": accession,
                "byte_size": byte_size,
                "declared_type": declared_type,
                "description": None if manifest is None else manifest.description,
                "filename": name,
                "index_media_icon": None if item is None else item.type,
                "inventory_presence": presence,
                "last_modified_at": None if modified is None else modified.isoformat(),
                "role": role,
                "sequence": None if manifest is None else manifest.sequence,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        attachments.append(
            SecFilingPackageAttachment(
                attachment_id=hashlib.sha256(identity.encode()).hexdigest(),
                parent_accession_number=accession,
                filename=name,
                declared_type=declared_type,
                sequence=None if manifest is None else manifest.sequence,
                description=None if manifest is None else manifest.description,
                index_media_icon=None if item is None else item.type,
                byte_size=byte_size,
                last_modified_at=modified,
                source_url=f"{base}/{name}",
                role=role,
                inventory_presence=presence,
            )
        )
    return ParsedSecFilingPackage(
        cik=normalized_cik,
        accession_number=accession,
        form_type=normalized_form,
        primary_document=primary,
        index_url=filing_package_index_url(normalized_cik, accession),
        filing_manifest_url=filing_package_manifest_url(normalized_cik, accession),
        attachments=tuple(attachments),
    )


def _archive_items(values: list[object]) -> tuple[_ArchiveItem, ...]:
    by_name: dict[str, _ArchiveItem] = {}
    ordered_names: list[str] = []
    for index, raw in enumerate(values):
        if not isinstance(raw, dict):
            raise SecFilingPackageContractError(f"archive item {index} must be an object")
        try:
            item = _ArchiveItem.model_validate(raw)
        except ValueError as exc:
            raise SecFilingPackageContractError(
                f"archive item {index} violates the authority contract"
            ) from exc
        name = _filename(item.name, label="attachment filename")
        normalized = item.model_copy(update={"name": name})
        prior = by_name.get(name)
        if prior is None:
            by_name[name] = normalized
            ordered_names.append(name)
        elif prior != normalized:
            raise SecFilingPackageContractError(
                f"duplicate attachment {name!r} has conflicting metadata"
            )
    if not ordered_names:
        raise SecFilingPackageContractError("archive index contains no attachments")
    return tuple(by_name[name] for name in ordered_names)


def _manifest_documents(
    body: bytes,
    *,
    cik: str,
    accession_number: str,
) -> dict[str, _ManifestDocument]:
    soup = BeautifulSoup(body, "html.parser")
    result: dict[str, _ManifestDocument] = {}
    tables = soup.select("table.tableFile")
    if not tables:
        raise SecFilingPackageContractError("filing-index manifest contains no attachment tables")
    expected_header = ("Seq", "Description", "Document", "Type", "Size")
    for table_index, table in enumerate(tables):
        rows = table.find_all("tr", recursive=False)
        if not rows:
            raise SecFilingPackageContractError(
                f"filing-index table {table_index} contains no rows"
            )
        header = tuple(
            cell.get_text(" ", strip=True) for cell in rows[0].find_all("th", recursive=False)
        )
        if header != expected_header:
            raise SecFilingPackageContractError(
                f"filing-index table {table_index} has an unexpected header"
            )
        for row_index, row in enumerate(rows[1:], start=1):
            cells = row.find_all("td", recursive=False)
            if len(cells) != 5:
                raise SecFilingPackageContractError(
                    f"filing-index table {table_index} row {row_index} must contain five cells"
                )
            anchors = cells[2].find_all("a")
            if len(anchors) != 1:
                raise SecFilingPackageContractError(
                    f"filing-index table {table_index} row {row_index} "
                    "must contain one document link"
                )
            href_value = anchors[0].get("href")
            if not isinstance(href_value, str):
                raise SecFilingPackageContractError(
                    f"filing-index table {table_index} row {row_index} has no document URL"
                )
            filename = _manifest_filename(
                href_value,
                displayed_filename=anchors[0].get_text(" ", strip=True),
                cik=cik,
                accession_number=accession_number,
            )
            sequence_text = cells[0].get_text(" ", strip=True)
            try:
                sequence = int(sequence_text) if sequence_text else None
            except ValueError as exc:
                raise SecFilingPackageContractError(
                    f"manifest document {filename!r} has invalid sequence"
                ) from exc
            description = cells[1].get_text(" ", strip=True) or None
            declared_type = cells[3].get_text(" ", strip=True).upper() or None
            size_text = cells[4].get_text(" ", strip=True)
            byte_size = _byte_size(size_text, filename)
            document = _ManifestDocument(
                declared_type=declared_type,
                sequence=sequence,
                filename=filename,
                description=description,
                byte_size=byte_size,
            )
            prior = result.get(filename)
            if prior is None:
                result[filename] = document
            elif prior != document:
                raise SecFilingPackageContractError(
                    f"duplicate manifest filename {filename!r} has conflicting metadata"
                )
    if not result:
        raise SecFilingPackageContractError("filing-index manifest contains no documents")
    sequences = [item.sequence for item in result.values() if item.sequence is not None]
    if len(sequences) != len(set(sequences)):
        raise SecFilingPackageContractError(
            "filing-index manifest contains duplicate document sequences"
        )
    return result


def _manifest_filename(
    href: str,
    *,
    displayed_filename: str,
    cik: str,
    accession_number: str,
) -> str:
    parsed = urlparse(href)
    if parsed.path == "/ix":
        document_values = parse_qs(parsed.query, strict_parsing=True).get("doc")
        if document_values is None or len(document_values) != 1:
            raise SecFilingPackageContractError(
                "inline-XBRL manifest link has no unique document path"
            )
        document_path = document_values[0]
    else:
        if parsed.query or parsed.fragment:
            raise SecFilingPackageContractError(
                "manifest document URL has unexpected query or fragment"
            )
        document_path = parsed.path
    prefix = f"/Archives/edgar/data/{int(cik)}/{accession_number.replace('-', '')}/"
    if not document_path.startswith(prefix):
        raise SecFilingPackageContractError(
            "manifest document URL is outside the requested accession"
        )
    linked_filename = _filename(
        document_path[len(prefix) :],
        label="manifest attachment filename",
    )
    legacy_prefix = f"{accession_number}-"
    if linked_filename.startswith(legacy_prefix):
        displayed = _filename(
            displayed_filename,
            label="displayed manifest attachment filename",
        )
        if linked_filename == f"{legacy_prefix}{displayed}":
            return displayed
    return linked_filename


def _role(
    *,
    filename: str,
    declared_type: str | None,
    primary_document: str,
) -> AttachmentRole:
    if filename == primary_document:
        return "primary_document"
    if (
        declared_type is not None and _FINANCIAL_EXHIBIT_TYPE.fullmatch(declared_type)
    ) or _is_financial_filename(filename):
        return "financial_report"
    if declared_type is not None and _EXHIBIT_TYPE.fullmatch(declared_type):
        return "exhibit"
    return "supporting_attachment"


def _is_financial_filename(filename: str) -> bool:
    if filename in _FINANCIAL_FILENAMES:
        return True
    lowered = filename.lower()
    return any(lowered.endswith(suffix.lower()) for suffix in _FINANCIAL_SUFFIXES) or bool(
        _RENDERED_FINANCIAL_REPORT.fullmatch(filename)
    )


def _byte_size(value: str, filename: str) -> int | None:
    normalized = value.strip()
    if not normalized:
        return None
    try:
        size = int(normalized)
    except ValueError as exc:
        raise SecFilingPackageContractError(f"{filename} has invalid size") from exc
    if size < 0:
        raise SecFilingPackageContractError(f"{filename} has invalid size")
    return size


def _filename(value: str, *, label: str) -> str:
    normalized = value.strip()
    if not normalized or normalized in {".", ".."} or not _SAFE_FILENAME.fullmatch(normalized):
        raise SecFilingPackageContractError(f"invalid {label}")
    return normalized


def _json_object(body: bytes) -> Mapping[str, object]:
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SecFilingPackageContractError("archive index response is not valid JSON") from exc
    if not isinstance(value, dict):
        raise SecFilingPackageContractError("archive index response must be a JSON object")
    return cast(Mapping[str, object], value)


def _identities(cik: str, accession_number: str) -> tuple[str, str]:
    normalized_cik = cik.strip()
    if not normalized_cik.isdigit() or len(normalized_cik) > 10:
        raise SecFilingPackageContractError("CIK must contain at most ten digits")
    accession = accession_number.strip()
    if not _ACCESSION.fullmatch(accession):
        raise SecFilingPackageContractError("invalid accession number")
    return normalized_cik.zfill(10), accession
