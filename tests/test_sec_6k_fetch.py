"""Tests for pipeline.sec_6k_fetch -- the Phase-3 FPI 6-K exhibit locator +
fetcher (docs/design/segment_quarterly_framework.md §1.1).

Covers: CIK resolution via the existing CIK_MAP (no new resolver), exhibit
location against a mocked EDGAR submissions/index response (date-window +
filename-hint matching), image-only exhibit detection (the ASML spike
finding -- slide-deck JPGs with near-zero extractable text), and document
registration (sha256 idempotence, real file written under data/historical/sec/).
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pipeline.sec_6k_fetch import (  # noqa: E402
    FetchedExhibit,
    LocatedExhibit,
    fetch_6k_exhibit_text,
    locate_6k_exhibit,
    register_6k_document,
    resolve_cik,
)


def test_resolve_cik_uses_existing_cik_map() -> None:
    """Reuses pipeline.sec_xbrl.CIK_MAP -- no new resolver invented."""
    assert resolve_cik("NU") is not None
    assert resolve_cik("NVO") is not None
    assert resolve_cik("ASML") is not None
    assert resolve_cik("NOT-A-REAL-TICKER-XYZ") is None


_SUBMISSIONS_PAYLOAD = {
    "filings": {
        "recent": {
            "form": ["6-K", "6-K", "20-F", "6-K"],
            "filingDate": ["2026-05-14", "2026-01-15", "2026-02-25", "2025-11-01"],
            "accessionNumber": [
                "0001292814-26-003053",
                "0001292814-26-000100",
                "0001292814-26-000200",
                "0001292814-25-009000",
            ],
        }
    }
}

_INDEX_PAYLOAD = {
    "directory": {
        "item": [
            {"name": "0001292814-26-003053-index.html"},
            {"name": "image_003.jpg"},
            {"name": "nufs1q26_6k.htm"},
        ]
    }
}


def _make_session(json_by_url: dict[str, object]) -> MagicMock:
    session = MagicMock()

    def fake_get(url: str, headers: object = None, timeout: object = None) -> MagicMock:
        resp = MagicMock()
        for key, payload in json_by_url.items():
            if key in url:
                resp.status_code = 200
                resp.json.return_value = payload
                return resp
        resp.status_code = 404
        return resp

    session.get.side_effect = fake_get
    return session


def test_locate_6k_exhibit_matches_filename_hint_in_window() -> None:
    """NU 1Q26: quarter-end 2026-03-31, filed 2026-05-14 (44 days later, well
    inside the [10, 100]-day window) -- the accession's index.json exhibit
    'nufs1q26_6k.htm' matches NU's registered filename hint."""
    session = _make_session(
        {
            "submissions": _SUBMISSIONS_PAYLOAD,
            "index.json": _INDEX_PAYLOAD,
        }
    )
    result = locate_6k_exhibit("NU", quarter="Q1", year=2026, session=session)
    assert result is not None
    assert result.exhibit_filename == "nufs1q26_6k.htm"
    assert result.accession == "0001292814-26-003053"


def test_locate_6k_exhibit_returns_none_for_untested_ticker() -> None:
    """A ticker with no registered exhibit-filename hint (e.g. ASML, or any
    20-F name the spike didn't validate) never triggers a network call --
    checked before any session.get()."""
    session = MagicMock()
    result = locate_6k_exhibit("ASML", quarter="Q1", year=2026, session=session)
    assert result is None
    session.get.assert_not_called()


def test_locate_6k_exhibit_returns_none_outside_filing_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 6-K filed only 3 days after quarter-end (implausibly fast) or a
    20-F filed inside the window (wrong form) must not match."""
    payload = {
        "filings": {
            "recent": {
                "form": ["6-K"],
                "filingDate": ["2026-04-03"],  # only 3 days after 2026-03-31
                "accessionNumber": ["0001292814-26-000001"],
            }
        }
    }
    session = _make_session({"submissions": payload, "index.json": _INDEX_PAYLOAD})
    result = locate_6k_exhibit("NU", quarter="Q1", year=2026, session=session)
    assert result is None


def test_fetch_6k_exhibit_text_detects_image_only_slide_deck() -> None:
    """The ASML spike finding: a 6-K exhibit that's <img> slide references
    with almost no real text must be flagged is_image_only=True, not passed
    to the LLM as if it were narrative text."""
    located = LocatedExhibit(
        ticker="ASML",
        cik="0000937966",
        accession="0001628280-26-025147",
        filing_date="2026-04-15",
        exhibit_filename="financialstatementsusgaa.htm",
        exhibit_url="https://example.invalid/financialstatementsusgaa.htm",
    )
    slide_html = (
        "<html><body>"
        + "".join(f'<img src="slide{i}.jpg" title="slide{i}">' for i in range(12))
        + "<p>Forward Looking Statements ASML Financial statements US GAAP Q1 2026</p></body></html>"
    )
    session = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.text = slide_html
    session.get.return_value = resp

    fetched = fetch_6k_exhibit_text(located, session=session)
    assert fetched is not None
    assert fetched.is_image_only is True


def test_fetch_6k_exhibit_text_accepts_real_narrative_table() -> None:
    """NU-shaped exhibit: a real HTML text table clears the image-only guard."""
    located = LocatedExhibit(
        ticker="NU",
        cik="0001691493",
        accession="0001292814-26-003053",
        filing_date="2026-05-14",
        exhibit_filename="nufs1q26_6k.htm",
        exhibit_url="https://example.invalid/nufs1q26_6k.htm",
    )
    narrative_html = (
        "<html><body><p>34. SEGMENT INFORMATION</p>"
        + "<p>Information about geographical area. The table below shows the "
        "revenue and non-current assets per geographical area: Brazil 3,586,566 "
        "Mexico 289,026 Other countries 76,747 Total 3,952,339</p>" * 20 + "</body></html>"
    )
    session = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.text = narrative_html
    session.get.return_value = resp

    fetched = fetch_6k_exhibit_text(located, session=session)
    assert fetched is not None
    assert fetched.is_image_only is False
    assert "SEGMENT INFORMATION" in fetched.plain_text


_DOC_SCHEMA = """
CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    source_type TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    period_start DATETIME,
    period_end DATETIME,
    file_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    fetched_at TIMESTAMP NOT NULL,
    fetch_status TEXT NOT NULL,
    http_code INTEGER,
    raw_bytes_size INTEGER NOT NULL,
    source_url TEXT,
    parent_document_id INTEGER,
    accession_number TEXT,
    filing_date TEXT
);
"""


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(_DOC_SCHEMA)
    c.commit()
    return c


def test_register_6k_document_writes_file_and_row(conn: sqlite3.Connection, tmp_path: Path) -> None:
    located = LocatedExhibit(
        ticker="NU",
        cik="0001691493",
        accession="0001292814-26-003053",
        filing_date="2026-05-14",
        exhibit_filename="nufs1q26_6k.htm",
        exhibit_url="https://example.invalid/nufs1q26_6k.htm",
    )
    fetched = FetchedExhibit(
        located=located,
        raw_html="<html>segment text</html>",
        plain_text="segment text",
        is_image_only=False,
    )
    doc_id = register_6k_document(
        conn, ticker="NU", fetched=fetched, repo_root=tmp_path, period_end=datetime(2026, 3, 31)
    )
    assert doc_id > 0
    row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    assert row["doc_type"] == "sec_6k"
    assert row["source_type"] == "sec_xbrl"
    assert row["accession_number"] == "0001292814-26-003053"
    out_file = tmp_path / "data" / "historical" / "sec" / "NU_6k_2026-05-14.html"
    assert out_file.exists()
    assert out_file.read_text(encoding="utf-8") == "<html>segment text</html>"


def test_register_6k_document_idempotent_on_sha256(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    located = LocatedExhibit(
        ticker="NU",
        cik="0001691493",
        accession="0001292814-26-003053",
        filing_date="2026-05-14",
        exhibit_filename="nufs1q26_6k.htm",
        exhibit_url="https://example.invalid/nufs1q26_6k.htm",
    )
    fetched = FetchedExhibit(
        located=located, raw_html="<html>same</html>", plain_text="same", is_image_only=False
    )
    first = register_6k_document(
        conn, ticker="NU", fetched=fetched, repo_root=tmp_path, period_end=datetime(2026, 3, 31)
    )
    second = register_6k_document(
        conn, ticker="NU", fetched=fetched, repo_root=tmp_path, period_end=datetime(2026, 3, 31)
    )
    assert first == second
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
