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


def _legacy_alias_manifest(*documents: tuple[str, str, str]) -> bytes:
    accession = "0000001001-25-000001"
    rows = "".join(
        (
            "<tr>"
            f"<td>{sequence}</td><td>{description}</td>"
            '<td><a href="/Archives/edgar/data/1001/'
            f'000000100125000001/{accession}-{filename}">{filename}</a></td>'
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


def _paper_manifest(*, scan_href: str, scan_filename: str = "scanned.pdf") -> bytes:
    return (
        '<html><body><table class="tableFile">'
        "<tr><th>Seq</th><th>Description</th><th>Document</th>"
        "<th>Type</th><th>Size</th></tr>"
        '<tr><td>1</td><td>AUTO-GENERATED PAPER DOCUMENT</td><td><a href="'
        "/Archives/edgar/data/1001/000000100125000001/primary.paper"
        '">primary.paper</a></td><td>6-K</td><td>150</td></tr>'
        '<tr><td></td><td>Scanned paper document</td><td><a href="'
        f'{scan_href}">{scan_filename}</a></td><td></td><td>676505</td></tr>'
        "</table></body></html>"
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


def test_legacy_accession_prefixed_link_uses_displayed_archive_filename() -> None:
    result = parse_sec_filing_package_inventory(
        cik="1001",
        accession_number="0000001001-25-000001",
        form_type="10-Q",
        primary_document="0001.txt",
        index_body=_index(
            _item("0001.txt", size="130146"),
            _item("0002.txt", size="9813"),
        ),
        filing_manifest_body=_legacy_alias_manifest(
            ("0001.txt", "10-Q", "Quarterly report"),
            ("0002.txt", "EX-10.25", "Material agreement"),
        ),
    )

    assert [item.filename for item in result.attachments] == ["0001.txt", "0002.txt"]
    assert result.attachments[0].role == "primary_document"
    assert result.attachments[1].role == "exhibit"
    assert result.attachments[0].source_url.endswith("/0001.txt")


def test_noncanonical_prefixed_link_does_not_alias_displayed_filename() -> None:
    manifest = _legacy_alias_manifest(("0001.txt", "10-Q", "Quarterly report")).replace(
        b"0000001001-25-000001-0001.txt",
        b"unrelated-prefix-0001.txt",
    )

    with pytest.raises(SecFilingPackageContractError, match="primary document"):
        parse_sec_filing_package_inventory(
            cik="1001",
            accession_number="0000001001-25-000001",
            form_type="10-Q",
            primary_document="0001.txt",
            index_body=_index(_item("0001.txt")),
            filing_manifest_body=manifest,
        )


def test_sec_vprr_paper_scan_preserves_manifest_only_authority_url() -> None:
    result = parse_sec_filing_package_inventory(
        cik="1001",
        accession_number="0000001001-25-000001",
        form_type="6-K",
        primary_document="primary.paper",
        index_body=_index(_item("primary.paper", size="150")),
        filing_manifest_body=_paper_manifest(scan_href="/Archives/edgar/vprr/0201/02013406.pdf"),
    )

    scan = next(item for item in result.attachments if item.filename == "scanned.pdf")
    assert scan.inventory_presence == "manifest_only"
    assert scan.role == "supporting_attachment"
    assert scan.source_url == "https://www.sec.gov/Archives/edgar/vprr/0201/02013406.pdf"


@pytest.mark.parametrize(
    ("scan_href", "scan_filename", "match"),
    [
        ("/Archives/edgar/vprr/0201/not-numeric.pdf", "scanned.pdf", "outside"),
        ("/Archives/edgar/vprr/0201/02013406.pdf", "renamed.pdf", "outside"),
        (
            "https://example.com/Archives/edgar/vprr/0201/02013406.pdf",
            "scanned.pdf",
            "outside SEC authority",
        ),
    ],
)
def test_noncanonical_vprr_reference_fails_closed(
    scan_href: str,
    scan_filename: str,
    match: str,
) -> None:
    with pytest.raises(SecFilingPackageContractError, match=match):
        parse_sec_filing_package_inventory(
            cik="1001",
            accession_number="0000001001-25-000001",
            form_type="6-K",
            primary_document="primary.paper",
            index_body=_index(_item("primary.paper", size="150")),
            filing_manifest_body=_paper_manifest(
                scan_href=scan_href,
                scan_filename=scan_filename,
            ),
        )


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


def test_filing_manifest_only_document_preserves_authority_disagreement() -> None:
    result = parse_sec_filing_package_inventory(
        cik="1001",
        accession_number="0000001001-25-000001",
        form_type="8-K",
        primary_document="acme-8k.htm",
        index_body=_index(_item("acme-8k.htm")),
        filing_manifest_body=_manifest(
            ("acme-8k.htm", "8-K", "Current report"),
            ("missing.htm", "EX-99.1", "Manifest-only exhibit"),
        ),
    )

    assert [item.inventory_presence for item in result.attachments] == [
        "matched",
        "manifest_only",
    ]
    manifest_only = result.attachments[1]
    assert manifest_only.filename == "missing.htm"
    assert manifest_only.index_media_icon is None
    assert manifest_only.last_modified_at is None
    assert manifest_only.role == "exhibit"


def test_manifest_only_primary_preserves_authority_disagreement() -> None:
    result = parse_sec_filing_package_inventory(
        cik="1001",
        accession_number="0000001001-25-000001",
        form_type="8-K",
        primary_document="acme-8k.htm",
        index_body=_index(_item("supporting.css")),
        filing_manifest_body=_manifest(
            ("acme-8k.htm", "8-K", "Current report"),
        ),
    )

    primary = next(item for item in result.attachments if item.role == "primary_document")
    assert primary.inventory_presence == "manifest_only"
    assert primary.index_media_icon is None
    assert primary.last_modified_at is None
