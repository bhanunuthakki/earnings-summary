"""Exhaustive, fail-closed SEC submissions inventory contracts."""

from __future__ import annotations

import json

import pytest

from filings.sec_submissions_inventory import (
    HistoricalComponent,
    SecInventoryContractError,
    parse_sec_submissions_inventory,
)


def _columns(
    accessions: list[str],
    *,
    forms: list[str] | None = None,
    primary_documents: list[str] | None = None,
) -> dict[str, list[str]]:
    size = len(accessions)
    return {
        "accessionNumber": accessions,
        "filingDate": ["2025-02-01"] * size,
        "reportDate": ["2024-12-31"] * size,
        "acceptanceDateTime": ["20250201120000"] * size,
        "act": ["34"] * size,
        "form": forms or ["10-K"] * size,
        "fileNumber": ["001-00001"] * size,
        "filmNumber": ["25000001"] * size,
        "items": [""] * size,
        "size": ["100"] * size,
        "isXBRL": ["1"] * size,
        "isInlineXBRL": ["1"] * size,
        "primaryDocument": primary_documents or ["annual.htm"] * size,
        "primaryDocDescription": ["Annual report"] * size,
    }


def _root(
    recent: dict[str, list[str]],
    files: list[dict[str, object]],
) -> bytes:
    return json.dumps(
        {
            "cik": "1001",
            "name": "Acme Corp",
            "tickers": ["OLD", "ACME"],
            "filings": {"recent": recent, "files": files},
        }
    ).encode()


def test_root_and_all_advertised_history_pages_are_unioned_without_truncation() -> None:
    root = _root(
        _columns(["0000001001-25-000001"]),
        [
            {"name": "CIK0000001001-submissions-001.json", "filingCount": 1},
            {"name": "CIK0000001001-submissions-002.json", "filingCount": 1},
        ],
    )
    result = parse_sec_submissions_inventory(
        cik="1001",
        ticker="ACME",
        primary_body=root,
        historical=(
            HistoricalComponent(
                name="CIK0000001001-submissions-001.json",
                body=json.dumps(_columns(["0000001001-24-000001"])).encode(),
            ),
            HistoricalComponent(
                name="CIK0000001001-submissions-002.json",
                body=json.dumps(_columns(["0000001001-23-000001"])).encode(),
            ),
        ),
    )
    assert result.issuer_id == "sec-cik:0000001001"
    assert result.complete
    assert [item.accession_number for item in result.filings] == [
        "0000001001-23-000001",
        "0000001001-24-000001",
        "0000001001-25-000001",
    ]
    assert result.required_component_names == (
        "CIK0000001001.json",
        "CIK0000001001-submissions-001.json",
        "CIK0000001001-submissions-002.json",
    )


def test_missing_historical_component_is_explicitly_partial() -> None:
    root = _root(
        _columns(["0000001001-25-000001"]),
        [{"name": "CIK0000001001-submissions-001.json", "filingCount": 2}],
    )
    result = parse_sec_submissions_inventory(
        cik="0000001001",
        ticker="ACME",
        primary_body=root,
        historical=(
            HistoricalComponent(
                name="CIK0000001001-submissions-001.json",
                failure_reason="http_503",
            ),
        ),
    )
    assert not result.complete
    assert result.issues[0].code == "component_fetch_failed"
    assert result.issues[0].component_name.endswith("-001.json")


def test_parallel_column_mismatch_fails_closed() -> None:
    malformed = _columns(["0000001001-25-000001"])
    malformed["form"] = []
    with pytest.raises(SecInventoryContractError, match="parallel columns"):
        parse_sec_submissions_inventory(
            cik="1001",
            ticker="ACME",
            primary_body=_root(malformed, []),
            historical=(),
        )


def test_duplicate_accession_conflict_is_material_and_not_silently_selected() -> None:
    root = _root(_columns(["0000001001-25-000001"]), [])
    conflicting = _columns(
        ["0000001001-25-000001"],
        forms=["10-K/A"],
        primary_documents=["amended.htm"],
    )
    result = parse_sec_submissions_inventory(
        cik="1001",
        ticker="ACME",
        primary_body=root,
        historical=(
            HistoricalComponent(
                name="unexpected-but-captured.json",
                body=json.dumps(conflicting).encode(),
                required=False,
            ),
        ),
    )
    assert not result.complete
    assert result.filings == ()
    assert result.issues[0].code == "accession_conflict"


def test_missing_primary_document_is_preserved_as_authority_unavailable() -> None:
    columns = _columns(["0000001001-25-000001"], primary_documents=[""])
    result = parse_sec_submissions_inventory(
        cik="1001",
        ticker="ACME",
        primary_body=_root(columns, []),
        historical=(),
    )

    assert result.complete
    assert result.filings[0].primary_document is None
    assert result.filings[0].primary_document_url is None
