"""Index original SEC submission documents without normalizing their bytes.

This is a container index, not financial extraction or a directory-completeness
receipt. Every locator is an exact byte interval in the retained parent blob.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_ACCESSION = re.compile(r"^\d{10}-\d{2}-\d{6}$")
_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$")
_DOCUMENT = re.compile(rb"^<(/?)DOCUMENT>[ \t]*\r?$", re.MULTILINE | re.IGNORECASE)
_TEXT_OPEN = re.compile(rb"^<TEXT>[ \t]*\r?\n", re.MULTILINE | re.IGNORECASE)
_TEXT_CLOSE = re.compile(rb"^</TEXT>[ \t]*\r?$", re.MULTILINE | re.IGNORECASE)
_MAX_BYTES = 100 * 1024 * 1024
_MAX_DOCUMENTS = 2048


class SecSubmissionTextError(ValueError):
    """The retained container cannot prove an unambiguous original index."""


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SubmissionDocumentSpan(_Closed):
    sequence: int = Field(gt=0)
    filename: str
    declared_type: str
    description: str | None
    document_start: int = Field(ge=0)
    document_end: int = Field(gt=0)
    text_start: int = Field(ge=0)
    text_end: int = Field(gt=0)
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ParsedSecSubmissionText(_Closed):
    schema_version: Literal["sec-submission-text-index/v1"] = "sec-submission-text-index/v1"
    cik: str
    accession_number: str
    form_type: str
    raw_sha256: str
    byte_size: int = Field(gt=0)
    acceptance_datetime_raw: str
    acceptance_clock_basis: Literal["sec_asserted_eastern_wall_clock"] = (
        "sec_asserted_eastern_wall_clock"
    )
    documents: tuple[SubmissionDocumentSpan, ...]


def _line(header: bytes, label: bytes, *, tag: bool = False) -> str:
    prefix = rb"<" + re.escape(label) + rb">" if tag else re.escape(label) + rb":"
    values = re.findall(rb"^[ \t]*" + prefix + rb"[ \t]*([^\r\n]+)", header, re.MULTILINE)
    if len(values) != 1:
        raise SecSubmissionTextError("submission header field missing or ambiguous")
    try:
        return values[0].decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise SecSubmissionTextError("unsupported submission header encoding") from exc


def _span(
    raw: bytes, start: int, opened_end: int, closed_start: int, end: int
) -> SubmissionDocumentSpan:
    block = raw[opened_end:closed_start]
    opens, closes = tuple(_TEXT_OPEN.finditer(block)), tuple(_TEXT_CLOSE.finditer(block))
    if len(opens) != 1 or len(closes) != 1 or opens[0].end() >= closes[0].start():
        raise SecSubmissionTextError("document text envelope missing or ambiguous")
    if block[closes[0].end() :].strip():
        raise SecSubmissionTextError("unindexed content after document text")
    header = block[: opens[0].start()]
    filename = _line(header, b"FILENAME", tag=True)
    if not _FILENAME.fullmatch(filename):
        raise SecSubmissionTextError("unsafe submission document filename")
    try:
        sequence = int(_line(header, b"SEQUENCE", tag=True))
    except ValueError as exc:
        raise SecSubmissionTextError("invalid document sequence") from exc
    if sequence <= 0:
        raise SecSubmissionTextError("document sequence must be positive")
    description = _line(header, b"DESCRIPTION", tag=True) if b"<DESCRIPTION>" in header else None
    text_start = opened_end + opens[0].end()
    text_end = opened_end + closes[0].start()
    return SubmissionDocumentSpan(
        sequence=sequence,
        filename=filename,
        declared_type=_line(header, b"TYPE", tag=True),
        description=description,
        document_start=start,
        document_end=end,
        text_start=text_start,
        text_end=text_end,
        text_sha256=hashlib.sha256(raw[text_start:text_end]).hexdigest(),
    )


def parse_sec_submission_text(
    raw_body: bytes, *, cik: str, accession_number: str, form_type: str
) -> ParsedSecSubmissionText:
    """Replay the native header and all original SGML document byte spans.

    The filing agent's accession prefix need not equal the issuer's CIK. The
    first native filer CIK, accession field and submission form must match.
    """
    if not cik.isdecimal() or len(cik) > 10 or not _ACCESSION.fullmatch(accession_number):
        raise SecSubmissionTextError("invalid requested submission identity")
    if not raw_body or len(raw_body) > _MAX_BYTES:
        raise SecSubmissionTextError("submission byte bound exceeded or empty")
    if not raw_body.startswith(b"<SEC-DOCUMENT>" + accession_number.encode() + b".txt"):
        raise SecSubmissionTextError("submission outer identity differs")
    header_start = raw_body.find(b"<SEC-HEADER>")
    header_end = raw_body.find(b"</SEC-HEADER>")
    closing = re.search(rb"</SEC-DOCUMENT>[ \t\r\n]*\Z", raw_body)
    if header_start < 0 or header_end <= header_start or closing is None:
        raise SecSubmissionTextError("submission envelope is incomplete")
    header = raw_body[header_start:header_end]
    filer = re.search(rb"^[ \t]*CENTRAL INDEX KEY:[ \t]*([0-9]+)", header, re.MULTILINE)
    if (
        filer is None
        or filer[1].decode().zfill(10) != cik.zfill(10)
        or _line(header, b"ACCESSION NUMBER") != accession_number
        or _line(header, b"CONFORMED SUBMISSION TYPE") != form_type
    ):
        raise SecSubmissionTextError("submission filer, accession or form differs")
    acceptance = _line(header, b"ACCEPTANCE-DATETIME", tag=True)
    try:
        datetime.strptime(acceptance, "%Y%m%d%H%M%S")
    except ValueError as exc:
        raise SecSubmissionTextError("invalid native acceptance clock") from exc
    if len(acceptance) != 14:
        raise SecSubmissionTextError("invalid native acceptance clock precision")
    cursor = header_end + len(b"</SEC-HEADER>")
    opening: re.Match[bytes] | None = None
    documents: list[SubmissionDocumentSpan] = []
    for token in _DOCUMENT.finditer(raw_body, cursor, closing.start()):
        if not token[1]:
            if opening is not None or raw_body[cursor : token.start()].strip():
                raise SecSubmissionTextError("nested document or unindexed content")
            opening = token
        else:
            if opening is None:
                raise SecSubmissionTextError("unmatched document close")
            documents.append(
                _span(raw_body, opening.start(), opening.end(), token.start(), token.end())
            )
            if len(documents) > _MAX_DOCUMENTS:
                raise SecSubmissionTextError("submission document bound exceeded")
            cursor, opening = token.end(), None
    if opening is not None or raw_body[cursor : closing.start()].strip() or not documents:
        raise SecSubmissionTextError("submission document population is incomplete")
    if len({d.filename.casefold() for d in documents}) != len(documents) or len(
        {d.sequence for d in documents}
    ) != len(documents):
        raise SecSubmissionTextError("duplicate document filename or sequence")
    return ParsedSecSubmissionText(
        cik=cik.zfill(10),
        accession_number=accession_number,
        form_type=form_type,
        raw_sha256=hashlib.sha256(raw_body).hexdigest(),
        byte_size=len(raw_body),
        acceptance_datetime_raw=acceptance,
        documents=tuple(documents),
    )
