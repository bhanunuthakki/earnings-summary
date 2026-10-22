"""Complete SEC submissions retain exact original-document byte locators."""

from __future__ import annotations

import hashlib

import pytest

from filings.sec_submission_text import SecSubmissionTextError, parse_sec_submission_text

ACCESSION = "0001140361-26-000001"


def _document(sequence: int, filename: str, body: bytes, kind: str = "8-K") -> bytes:
    return (
        (
            f"<DOCUMENT>\n<TYPE>{kind}\n<SEQUENCE>{sequence}\n<FILENAME>{filename}\n"
            "<DESCRIPTION>Original document\n<TEXT>\n"
        ).encode()
        + body
        + b"</TEXT>\n</DOCUMENT>\n"
    )


def _submission(*documents: bytes) -> bytes:
    return (
        (
            f"<SEC-DOCUMENT>{ACCESSION}.txt\n<SEC-HEADER>header\n"
            "<ACCEPTANCE-DATETIME>20260914163039\n"
            f"ACCESSION NUMBER: {ACCESSION}\nCONFORMED SUBMISSION TYPE: 8-K\n"
            "FILER:\n COMPANY DATA:\n  CENTRAL INDEX KEY: 0000001001\n"
            "</SEC-HEADER>\n"
        ).encode()
        + b"".join(documents)
        + b"</SEC-DOCUMENT>\n"
    )


def _parse(raw: bytes):
    return parse_sec_submission_text(raw, cik="1001", accession_number=ACCESSION, form_type="8-K")


def test_preserves_exact_original_bytes_and_parent_locators() -> None:
    primary = b"<html>\r\n<p>Revenue &amp; cash</p>\r\n</html>\n"
    exhibit = b"<html>Original agreement</html>\n"
    raw = _submission(
        _document(1, "primary.htm", primary), _document(2, "ex10.htm", exhibit, "EX-10.1")
    )
    parsed = _parse(raw)
    assert parsed.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert parsed.cik == "0000001001"  # Agent accession prefix is not issuer CIK.
    assert parsed.acceptance_datetime_raw == "20260914163039"
    assert parsed.acceptance_clock_basis == "sec_asserted_eastern_wall_clock"
    assert len(parsed.documents) == 2
    for document, body in zip(parsed.documents, (primary, exhibit), strict=True):
        assert raw[document.text_start : document.text_end] == body
        assert document.text_sha256 == hashlib.sha256(body).hexdigest()
        assert (
            document.document_start
            < document.text_start
            < document.text_end
            < document.document_end
        )
    assert parsed.documents[1].declared_type == "EX-10.1"


@pytest.mark.parametrize(
    "before,after",
    [
        (b"ACCESSION NUMBER: " + ACCESSION.encode(), b"ACCESSION NUMBER: 0001140361-26-000002"),
        (b"CENTRAL INDEX KEY: 0000001001", b"CENTRAL INDEX KEY: 0000002002"),
        (b"CONFORMED SUBMISSION TYPE: 8-K", b"CONFORMED SUBMISSION TYPE: 10-Q"),
        (b"<ACCEPTANCE-DATETIME>20260914163039", b"<ACCEPTANCE-DATETIME>20261399163039"),
        (b"</SEC-DOCUMENT>", b""),
        (b"</DOCUMENT>", b""),
        (b"</TEXT>", b""),
        (b"<FILENAME>primary.htm", b"<FILENAME>../primary.htm"),
        (b"<FILENAME>primary.htm", b"<FILENAME>primary.htm?extra"),
    ],
)
def test_rejects_wrong_identity_unsafe_names_and_truncation(before: bytes, after: bytes) -> None:
    raw = _submission(_document(1, "primary.htm", b"<html>value</html>\n"))
    with pytest.raises(SecSubmissionTextError):
        _parse(raw.replace(before, after, 1))


@pytest.mark.parametrize("duplicate", ["filename", "sequence"])
def test_rejects_ambiguous_document_identity(duplicate: str) -> None:
    raw = _submission(
        _document(1, "primary.htm", b"first\n"),
        _document(
            1 if duplicate == "sequence" else 2,
            "PRIMARY.HTM" if duplicate == "filename" else "other.htm",
            b"second\n",
            "EX-99.1",
        ),
    )
    with pytest.raises(SecSubmissionTextError):
        _parse(raw)


def test_rejects_trailing_non_document_content_and_empty_package() -> None:
    with pytest.raises(SecSubmissionTextError):
        _parse(_submission())
    raw = _submission(_document(1, "primary.htm", b"body\n"))
    with pytest.raises(SecSubmissionTextError):
        _parse(raw + b"unindexed bytes")


def test_does_not_accept_subject_cik_as_primary_filer_identity() -> None:
    raw = _submission(_document(1, "primary.htm", b"body\n"))
    raw = raw.replace(
        b"CENTRAL INDEX KEY: 0000001001",
        b"CENTRAL INDEX KEY: 0000002002\nSUBJECT COMPANY:\n CENTRAL INDEX KEY: 0000001001",
    )
    with pytest.raises(SecSubmissionTextError):
        _parse(raw)
