"""Fail-closed contracts for complete SEC accession-package inventories."""

from __future__ import annotations

import json

import pytest

from filings.sec_filing_package_inventory import (
    SecFilingPackageContractError,
    parse_sec_filing_package_inventory,
)


def _index(
    *items: dict[str, str],
    name: str = "/Archives/edgar/data/1001/000000100125000001",
) -> bytes:
    return json.dumps(
        {
            "directory": {
                "name": name,
                "parent-dir": "/Archives/edgar/data/1001",
                "item": list(items),
            }
        }
    ).encode()


def _item(
    name: str,
    *,
    size: str = "100",
    modified: str = "2025-02-01 12:00:00",
) -> dict[str, str]:
    return {
        "name": name,
        "type": "text.gif",
        "size": size,
        "last-modified": modified,
    }


def _manifest(*documents: tuple[str, str, str]) -> bytes:
    rows = "".join(
        (
            "<tr>"
            f"<td>{sequence}</td><td>{description}</td>"
            '<td><a href="/Archives/edgar/data/1001/'
            f'000000100125000001/{filename}">{filename}</a></td>'
            f"<td>{declared_type}</td><td></td>"
            "</tr>"
        )
        for sequence, (filename, declared_type, description) in enumerate(documents, start=1)
    )
    return (
        '<html><body><table class="tableFile">'
        "<tr><th>Seq</th><th>Description</th><th>Document</th>"
        "<th>Type</th><th>Size</th></tr>"
        f"{rows}</table></body></html>"
    ).encode()


def test_package_retains_primary_exhibits_and_financial_report_attachments() -> None:
    result = parse_sec_filing_package_inventory(
        cik="1001",
        accession_number="0000001001-25-000001",
        form_type="8-K",
        primary_document="acme-8k.htm",
        index_body=_index(
            _item("acme-8k.htm"),
            _item("earnings-release.htm", size="900"),
            _item("investor-deck.pdf", size="700"),
            _item("acme-20241231_htm.xml", size="500"),
            _item("logo.jpg", size="50"),
        ),
        filing_manifest_body=_manifest(
            ("acme-8k.htm", "8-K", "Current report"),
            ("earnings-release.htm", "EX-99.1", "Earnings release"),
            ("investor-deck.pdf", "EX-99.2", "Investor presentation"),
            ("acme-20241231_htm.xml", "XML", "Inline XBRL instance"),
            ("logo.jpg", "GRAPHIC", "Company logo"),
        ),
    )

    assert [item.role for item in result.attachments] == [
        "primary_document",
        "exhibit",
        "exhibit",
        "financial_report",
        "supporting_attachment",
    ]
    assert [item.declared_type for item in result.exhibits] == ["EX-99.1", "EX-99.2"]
    assert result.attachments[1].parent_accession_number == "0000001001-25-000001"
    assert result.attachments[1].source_url.endswith(
        "/1001/000000100125000001/earnings-release.htm"
    )


def test_identical_duplicate_attachment_is_exact_deduped() -> None:
    duplicate = _item("earnings-release.htm")
    result = parse_sec_filing_package_inventory(
        cik="1001",
        accession_number="0000001001-25-000001",
        form_type="8-K",
        primary_document="acme-8k.htm",
        index_body=_index(
            _item("acme-8k.htm"),
            duplicate,
            duplicate.copy(),
        ),
        filing_manifest_body=_manifest(
            ("acme-8k.htm", "8-K", "Current report"),
            ("earnings-release.htm", "EX-99.1", "Earnings release"),
        ),
    )

    assert [item.filename for item in result.attachments] == [
        "acme-8k.htm",
        "earnings-release.htm",
    ]


def test_manifest_allows_authority_generated_complete_submission_row_without_type() -> None:
    result = parse_sec_filing_package_inventory(
        cik="1001",
        accession_number="0000001001-25-000001",
        form_type="8-K",
        primary_document="acme-8k.htm",
        index_body=_index(
            _item("acme-8k.htm"),
            _item("0000001001-25-000001.txt", size=""),
        ),
        filing_manifest_body=_manifest(
            ("acme-8k.htm", "8-K", "Current report"),
            (
                "0000001001-25-000001.txt",
                "",
                "Complete submission text file",
            ),
        ),
    )

    assert result.attachments[1].declared_type is None
    assert result.attachments[1].role == "supporting_attachment"


def test_duplicate_filename_with_conflicting_authority_metadata_fails_closed() -> None:
    with pytest.raises(SecFilingPackageContractError, match="conflicting metadata"):
        parse_sec_filing_package_inventory(
            cik="1001",
            accession_number="0000001001-25-000001",
            form_type="8-K",
            primary_document="acme-8k.htm",
            index_body=_index(
                _item("acme-8k.htm"),
                _item("earnings-release.htm", size="100"),
                _item("earnings-release.htm", size="101"),
            ),
            filing_manifest_body=_manifest(
                ("acme-8k.htm", "8-K", "Current report"),
                ("earnings-release.htm", "EX-99.1", "Earnings release"),
            ),
        )


def test_missing_primary_document_fails_closed() -> None:
    with pytest.raises(SecFilingPackageContractError, match="primary document"):
        parse_sec_filing_package_inventory(
            cik="1001",
            accession_number="0000001001-25-000001",
            form_type="10-K",
            primary_document="annual.htm",
            index_body=_index(_item("exhibit.htm")),
            filing_manifest_body=_manifest(
                ("exhibit.htm", "EX-10.1", "Material agreement"),
            ),
        )


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("name", "../escape.htm", "invalid attachment filename"),
        ("size", "-1", "invalid size"),
        ("last-modified", "", "last-modified"),
    ],
)
def test_invalid_attachment_contract_fails_closed(field: str, value: str, match: str) -> None:
    attachment = _item("annual.htm")
    attachment[field] = value
    with pytest.raises(SecFilingPackageContractError, match=match):
        parse_sec_filing_package_inventory(
            cik="1001",
            accession_number="0000001001-25-000001",
            form_type="10-K",
            primary_document="annual.htm",
            index_body=_index(attachment),
            filing_manifest_body=_manifest(("annual.htm", "10-K", "Annual report")),
        )


def test_directory_identity_must_match_requested_accession() -> None:
    with pytest.raises(SecFilingPackageContractError, match="directory identity"):
        parse_sec_filing_package_inventory(
            cik="1001",
            accession_number="0000001001-25-000001",
            form_type="10-K",
            primary_document="annual.htm",
            index_body=_index(
                _item("annual.htm"),
                name="/Archives/edgar/data/1001/000000100124000099",
            ),
            filing_manifest_body=_manifest(("annual.htm", "10-K", "Annual report")),
        )


def test_filing_manifest_document_missing_from_directory_fails_closed() -> None:
    with pytest.raises(SecFilingPackageContractError, match="not present in index"):
        parse_sec_filing_package_inventory(
            cik="1001",
            accession_number="0000001001-25-000001",
            form_type="8-K",
            primary_document="acme-8k.htm",
            index_body=_index(_item("acme-8k.htm")),
            filing_manifest_body=_manifest(
                ("acme-8k.htm", "8-K", "Current report"),
                ("missing.htm", "EX-99.1", "Missing exhibit"),
            ),
        )
