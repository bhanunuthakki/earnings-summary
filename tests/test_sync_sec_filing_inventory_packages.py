"""Bounded, resumable SEC accession-package synchronization behavior."""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

import pytest
import requests

from execution import sync_sec_filing_inventory as sync
from filings.edgar_fetch import HardStopError, TransientError
from filings.sec_submissions_inventory import (
    HistoricalComponent,
    SecFilingInventoryEntry,
    SecInventoryContractError,
)
from runtime.job_runtime import JobAlreadyRunningError


class _BusyLock:
    write_sets: ClassVar[list[str]] = []

    def __init__(self, repo_root: Path, job_name: str, write_sets: list[str]) -> None:
        assert repo_root.exists()
        assert job_name
        type(self).write_sets = write_sets

    def __enter__(self) -> _BusyLock:
        raise JobAlreadyRunningError("already owned")

    def __exit__(self, *args: object) -> None:
        return None


def test_apply_returns_retryable_exit_before_opening_locked_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sync, "JobLock", _BusyLock)
    db_path = tmp_path / "absent.db"
    blob_root = tmp_path / "blobs"
    checkpoint_root = tmp_path / "checkpoint"

    exit_code = sync.main(
        [
            "--db",
            str(db_path),
            "--ticker",
            "ACME",
            "--cik",
            "1001",
            "--revision",
            "1",
            "--blob-root",
            str(blob_root),
            "--package-checkpoint-root",
            str(checkpoint_root),
            "--apply",
        ]
    )

    assert exit_code == 75
    assert any(item.startswith("sqlite:") for item in _BusyLock.write_sets)
    assert any(item.startswith("evidence-blobs:") for item in _BusyLock.write_sets)
    assert any(item.startswith("sec-package-checkpoint:") for item in _BusyLock.write_sets)
    assert any(item.startswith("source-inventory:") for item in _BusyLock.write_sets)
    assert not db_path.exists()
    assert not blob_root.exists()
    assert not checkpoint_root.exists()


def test_sec_fetch_observes_the_declared_request_spacing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, object]] = []

    class _Response:
        status_code = 200
        content = b"{}"

    class _Session:
        def get(
            self,
            url: str,
            *,
            headers: dict[str, str],
            timeout: tuple[int, int],
        ) -> _Response:
            events.append(("get", (url, headers, timeout)))
            return _Response()

    monkeypatch.setattr(
        sync.time,
        "sleep",
        lambda seconds: events.append(("sleep", seconds)),
    )

    assert (
        sync._fetch(
            _Session(),
            "https://data.sec.gov/submissions/CIK0000001001.json",
            "researcher researcher@example.test",
        )
        == b"{}"
    )
    assert events[0] == ("sleep", sync._SEC_REQUEST_DELAY_SECONDS)
    assert events[1][0] == "get"


def test_sec_contract_failure_preserves_every_raw_component(
    tmp_path: Path,
) -> None:
    root_body = b'{"root":true}'
    historical_body = b'{"filings":{"primaryDocument":[""]}}'
    manifest_path = sync._dump_inventory_contract_failure(
        tmp_path,
        ticker="BKNG",
        cik="0001075531",
        root_body=root_body,
        historical=(
            HistoricalComponent(
                name="CIK0001075531-submissions-001.json",
                body=historical_body,
            ),
        ),
        error=SecInventoryContractError("empty primaryDocument"),
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["ticker"] == "BKNG"
    assert manifest["cik"] == "0001075531"
    assert manifest["error_type"] == "SecInventoryContractError"
    assert manifest["components"][0]["name"] == "CIK0001075531.json"
    assert manifest["components"][1]["name"] == "CIK0001075531-submissions-001.json"
    assert (manifest_path.parent / manifest["components"][0]["file"]).read_bytes() == root_body
    assert (
        manifest_path.parent / manifest["components"][1]["file"]
    ).read_bytes() == historical_body


def _filing(
    accession: str,
    primary: str,
    *,
    form_type: str = "8-K",
) -> SecFilingInventoryEntry:
    return SecFilingInventoryEntry(
        issuer_id="sec-cik:0000001001",
        ticker="ACME",
        accession_number=accession,
        form_type=form_type,
        filing_date="2025-02-01",
        report_date="2024-12-31",
        accepted_at="20250201120000",
        primary_document=primary,
        primary_document_url=(
            f"https://www.sec.gov/Archives/edgar/data/1001/{accession.replace('-', '')}/{primary}"
        ),
        source_component_name="CIK0000001001.json",
    )


def test_package_scope_separates_company_reports_external_filings_and_unknowns() -> None:
    filings = (
        _filing("0000001001-25-000001", "report.htm", form_type="8-K"),
        _filing("0000001001-25-000002", "ownership.xml", form_type="4"),
        _filing("0000001001-25-000003", "effect.txt", form_type="EFFECT"),
        _filing("0000001001-25-000004", "unknown.htm", form_type="NEW-FORM"),
    )

    scope = sync.partition_filing_package_scope(filings)

    assert [item.form_type for item in scope.issuer_reports] == ["8-K"]
    assert [item.form_type for item in scope.external_or_administrative] == [
        "4",
        "EFFECT",
    ]
    assert [item.form_type for item in scope.unclassified] == ["NEW-FORM"]


def _bodies(
    filing: SecFilingInventoryEntry,
) -> tuple[str, bytes, str, bytes]:
    accession = filing.accession_number
    directory = accession.replace("-", "")
    index_url = sync.filing_package_index_url("1001", accession)
    manifest_url = sync.filing_package_manifest_url("1001", accession)
    index_body = json.dumps(
        {
            "directory": {
                "name": f"/Archives/edgar/data/1001/{directory}",
                "parent-dir": "/Archives/edgar/data/1001",
                "item": [
                    {
                        "name": filing.primary_document,
                        "type": "text.gif",
                        "size": "100",
                        "last-modified": "2025-02-01 12:00:00",
                    },
                    {
                        "name": "release.htm",
                        "type": "text.gif",
                        "size": "200",
                        "last-modified": "2025-02-01 12:00:00",
                    },
                ],
            }
        }
    ).encode()
    manifest_body = (
        '<html><body><table class="tableFile">'
        "<tr><th>Seq</th><th>Description</th><th>Document</th>"
        "<th>Type</th><th>Size</th></tr>"
        '<tr><td>1</td><td>Current report</td><td><a href="'
        f'/Archives/edgar/data/1001/{directory}/{filing.primary_document}">'
        f"{filing.primary_document}</a></td><td>8-K</td><td>100</td></tr>"
        '<tr><td>2</td><td>Earnings release</td><td><a href="'
        f'/Archives/edgar/data/1001/{directory}/release.htm">release.htm</a>'
        "</td><td>EX-99.1</td><td>200</td></tr>"
        "</table></body></html>"
    ).encode()
    return index_url, index_body, manifest_url, manifest_body


def test_package_collection_resumes_content_addressed_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _filing("0000001001-25-000001", "first.htm")
    second = _filing("0000001001-25-000002", "second.htm")
    first_index_url, first_index, first_submission_url, first_submission = _bodies(first)
    first_responses = {
        first_index_url: first_index,
        first_submission_url: first_submission,
    }
    first_calls: list[str] = []

    def fetch_first(_session: requests.Session, url: str, _user_agent: str) -> bytes:
        first_calls.append(url)
        return first_responses[url]

    monkeypatch.setattr(sync, "_fetch", fetch_first)

    initial = sync.collect_filing_packages(
        session=requests.Session(),
        user_agent="research@example.com",
        cik="0000001001",
        filings=(first, second),
        checkpoint_root=tmp_path,
        package_limit=1,
        capture_response=None,
    )

    assert [item.accession_number for item in initial.packages] == [first.accession_number]
    assert initial.deferred_accession_count == 1
    second_index_url, second_index, second_submission_url, second_submission = _bodies(second)
    second_responses = {
        second_index_url: second_index,
        second_submission_url: second_submission,
    }
    second_calls: list[str] = []

    def fetch_second(_session: requests.Session, url: str, _user_agent: str) -> bytes:
        second_calls.append(url)
        return second_responses[url]

    monkeypatch.setattr(sync, "_fetch", fetch_second)

    resumed = sync.collect_filing_packages(
        session=requests.Session(),
        user_agent="research@example.com",
        cik="0000001001",
        filings=(first, second),
        checkpoint_root=tmp_path,
        package_limit=1,
        capture_response=None,
    )

    assert [item.accession_number for item in resumed.packages] == [
        first.accession_number,
        second.accession_number,
    ]
    assert resumed.deferred_accession_count == 0
    assert first_calls == [first_index_url, first_submission_url]
    assert second_calls == [second_index_url, second_submission_url]


def test_transient_package_failure_is_explicit_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filing = _filing("0000001001-25-000001", "first.htm")
    index_url, _index, submission_url, submission = _bodies(filing)

    def fetch(_session: requests.Session, url: str, _user_agent: str) -> bytes:
        if url == index_url:
            raise TransientError("deferred")
        assert url == submission_url
        return submission

    monkeypatch.setattr(sync, "_fetch", fetch)

    result = sync.collect_filing_packages(
        session=requests.Session(),
        user_agent="research@example.com",
        cik="0000001001",
        filings=(filing,),
        checkpoint_root=tmp_path,
        package_limit=1,
        capture_response=None,
    )

    assert result.packages == ()
    assert [
        item.failure_reason for item in result.components if item.failure_reason is not None
    ] == ["transient_deferred"]
    assert not (tmp_path / "0000001001" / "state.json").exists()


def test_package_failure_summary_identifies_accession_without_exposing_url() -> None:
    failures = (
        sync._PackageComponent(
            accession_number="0000001001-25-000001",
            component_kind="validation",
            source_url="https://www.sec.gov/Archives/private-looking-path",
            failure_reason="package_contract_invalid",
        ),
    )

    summary = sync.summarize_package_failures(failures)

    assert [item.model_dump() for item in summary] == [
        {
            "accession_number": "0000001001-25-000001",
            "component_kind": "validation",
            "reason_code": "package_contract_invalid",
        }
    ]


def test_package_auth_failure_is_a_hard_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filing = _filing("0000001001-25-000001", "first.htm")
    index_url, _index, submission_url, submission = _bodies(filing)

    def fetch(_session: requests.Session, url: str, _user_agent: str) -> bytes:
        if url == index_url:
            raise HardStopError("auth")
        assert url == submission_url
        return submission

    monkeypatch.setattr(sync, "_fetch", fetch)

    with pytest.raises(HardStopError):
        sync.collect_filing_packages(
            session=requests.Session(),
            user_agent="research@example.com",
            cik="0000001001",
            filings=(filing,),
            checkpoint_root=tmp_path,
            package_limit=1,
            capture_response=None,
        )


def test_authority_bytes_are_captured_before_package_contract_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filing = _filing("0000001001-25-000001", "missing-primary.htm")
    index_url, index, submission_url, submission = _bodies(filing)
    malformed_index = index.replace(b"missing-primary.htm", b"other-primary.htm")
    responses = {
        index_url: malformed_index,
        submission_url: submission,
    }
    captured: list[str] = []

    def fetch(_session: requests.Session, url: str, _user_agent: str) -> bytes:
        return responses[url]

    def capture(_body: bytes, url: str, _media_type: str) -> str:
        captured.append(url)
        return f"observation:{len(captured)}"

    monkeypatch.setattr(sync, "_fetch", fetch)
    result = sync.collect_filing_packages(
        session=requests.Session(),
        user_agent="research@example.com",
        cik="0000001001",
        filings=(filing,),
        checkpoint_root=tmp_path,
        package_limit=1,
        capture_response=capture,
    )

    assert captured == [index_url, submission_url]
    assert result.packages == ()
    assert result.components[-1].failure_reason == "package_contract_invalid"


def test_expected_documents_keep_accession_parentage_for_every_package_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filing = _filing("0000001001-25-000001", "first.htm")
    index_url, index, submission_url, submission = _bodies(filing)
    responses = {index_url: index, submission_url: submission}

    def fetch(_session: requests.Session, url: str, _user_agent: str) -> bytes:
        return responses[url]

    monkeypatch.setattr(sync, "_fetch", fetch)
    packages = sync.collect_filing_packages(
        session=requests.Session(),
        user_agent="research@example.com",
        cik="0000001001",
        filings=(filing,),
        checkpoint_root=tmp_path,
        package_limit=1,
        capture_response=None,
    ).packages

    documents = sync.build_expected_documents(
        issuer_id="sec-cik:0000001001",
        filings=(filing,),
        packages=packages,
    )

    assert [document.document_type for document in documents] == [
        "filing",
        "sec_exhibit",
    ]
    child = documents[1]
    assert child.accession_number == filing.accession_number
    assert child.source_url is not None and child.source_url.endswith("/release.htm")
    assert child.absence is not None
    assert dict(child.absence.reason_details)["parent_expected_document_key"] == (
        f"sec-cik:0000001001:{filing.accession_number}"
    )
