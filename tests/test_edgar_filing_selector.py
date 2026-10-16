"""Bounded EDGAR selector preserves foreign-filer amendment identities."""

from __future__ import annotations

import json
from io import BytesIO

import pytest
import requests

from filings import edgar_fetch
from filings.models import FilingForm


@pytest.mark.parametrize(
    ("base_form", "mapped_form"),
    [("6-K", FilingForm.FORM_6K), ("40-F", FilingForm.FORM_40F)],
)
def test_edgar_amendments_preserve_identity_and_share_base_form_limit(
    base_form: str, mapped_form: FilingForm, monkeypatch: pytest.MonkeyPatch
) -> None:
    accessions = [f"0000001001-26-{number:06d}" for number in range(1, 4)]
    documents = ["amended.htm", "original.htm", "older-amendment.htm"]
    forms = [f"{base_form}/A", base_form, f"{base_form}/A"]
    payload = {
        "filings": {
            "recent": {
                "form": forms,
                "accessionNumber": accessions,
                "filingDate": ["2026-08-03", "2026-08-02", "2026-08-01"],
                "reportDate": ["2026-06-30"] * 3,
                "primaryDocument": documents,
            }
        }
    }
    calls: list[str] = []

    def fake_get(_session: requests.Session, url: str, **_kwargs: object) -> requests.Response:
        calls.append(url)
        response = requests.Response()
        response.status_code = 200
        response.encoding = "utf-8"
        response.raw = BytesIO(json.dumps(payload).encode("utf-8"))
        return response

    monkeypatch.setattr(requests.Session, "get", fake_get)
    with requests.Session() as session:
        refs = edgar_fetch.list_filings(
            "acme",
            forms=frozenset({mapped_form}),
            cik="0000001001",
            user_agent="Synthetic selector test contact test@example.invalid",
            limit_per_form=2,
            session=session,
        )

    assert calls == ["https://data.sec.gov/submissions/CIK0000001001.json"]
    assert [(ref.form, ref.form_raw, ref.accession, ref.primary_document) for ref in refs] == [
        (mapped_form, forms[index], accessions[index], documents[index]) for index in range(2)
    ]
    assert all(ref.ticker == "ACME" and ref.cik == "0000001001" for ref in refs)
    assert [ref.url for ref in refs] == [
        f"https://www.sec.gov/Archives/edgar/data/1001/{accessions[index].replace('-', '')}/{documents[index]}"
        for index in range(2)
    ]
