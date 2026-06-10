"""Tests for the P3.5 source viewers: the transcript reader (line anchors),
the 10-K/10-Q section reader, the /source/<doc_id> dispatcher route, and the
Governance Restatements panel over the supersede chains.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from pipeline.restatements_panel import load_restatements, render_restatements_panel
from pipeline.source_viewers import (
    render_fallback_page,
    render_form10k_page,
    render_transcript_page,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import comments_server  # noqa: E402

_DOCS_DDL = """
CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    source_type TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    file_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    fetched_at TIMESTAMP NOT NULL,
    fetch_status TEXT NOT NULL,
    raw_bytes_size INTEGER NOT NULL DEFAULT 0,
    source_url TEXT,
    accession_number TEXT,
    filing_date TEXT
);
CREATE TABLE financial_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    period_end TIMESTAMP NOT NULL,
    fiscal_period_type TEXT NOT NULL,
    line_item TEXT NOT NULL,
    value TEXT NOT NULL,
    source_doc_id INTEGER NOT NULL,
    supersedes_id INTEGER
);
"""


def _seed_repo(repo: Path) -> Path:
    """A repo root with a documents DB, a transcript txt, and a form-10k JSON."""
    db = repo / "data" / "portfolio.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript(_DOCS_DDL)
    docs = [
        # 1: transcript with a speaker line + an HTML-ish line to escape
        (
            "NU",
            "transcript_audio",
            "earnings_call_transcript",
            "transcripts/processed/NU_Q1_2026.txt",
            "a",
            None,
            None,
            None,
        ),
        # 2: parsed 10-K JSON with accession identity
        (
            "NU",
            "fmp",
            "fmp_10k_json",
            "data/historical/fmp/NU_form_10k_2025.json",
            "b",
            None,
            "0001-01-000001",
            "2026-02-20",
        ),
        # 3: IR PDF with a source_url -> dispatcher 302s
        (
            "NU",
            "ir_doc",
            "ir_presentation",
            "ir_documents/NU/2026-03-31/deck.pdf",
            "c",
            "https://ir.example/deck.pdf",
            None,
            None,
        ),
        # 4: FMP endpoint dump, no source_url -> fallback page
        (
            "NU",
            "fmp",
            "fmp_income_statement",
            "data/historical/fmp/NU_income_statement_quarterly.json",
            "d",
            None,
            None,
            None,
        ),
    ]
    conn.executemany(
        "INSERT INTO documents (ticker, source_type, doc_type, file_path, sha256, "
        " fetched_at, fetch_status, source_url, accession_number, filing_date) "
        "VALUES (?, ?, ?, ?, ?, '2026-06-01 10:00:00', 'ok', ?, ?, ?)",
        [(t, st, dt, fp, sha, url, acc, filed) for t, st, dt, fp, sha, url, acc, filed in docs],
    )
    # Supersede chain: Q4 revenue restated 100 -> 110 (doc 2 supersedes doc 4),
    # plus a same-value re-report that must NOT list.
    conn.executemany(
        "INSERT INTO financial_facts "
        "(ticker, period_end, fiscal_period_type, line_item, value, source_doc_id, supersedes_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("NU", "2025-12-31 00:00:00", "Q4", "revenue", "100", 4, None),
            ("NU", "2025-12-31 00:00:00", "Q4", "revenue", "110", 2, 1),
            ("NU", "2025-09-30 00:00:00", "Q3", "revenue", "90", 4, None),
            ("NU", "2025-09-30 00:00:00", "Q3", "revenue", "90", 2, 3),
        ],
    )
    conn.commit()
    conn.close()

    tdir = repo / "transcripts" / "processed"
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "NU_Q1_2026.txt").write_text(
        "Operator: Good morning, welcome to the call.\n"
        "David Velez: NIM expanded to 17.8% this quarter.\n"
        "<script>alert('x')</script>\n",
        encoding="utf-8",
    )
    fdir = repo / "data" / "historical" / "fmp"
    fdir.mkdir(parents=True, exist_ok=True)
    (fdir / "NU_form_10k_2025.json").write_text(
        json.dumps(
            {
                "symbol": "NU",
                "period": "FY",
                "year": 2025,
                "Cover": [{"Document Type": ["10-K"]}],
                "Income Taxes": [{"Effective rate <note>": ["22% effective rate"]}],
            }
        ),
        encoding="utf-8",
    )
    return db


# ----------------------------------------------------------------------------
# renderers
# ----------------------------------------------------------------------------


def test_transcript_page_line_anchors_and_escaping(tmp_path: Path) -> None:
    db = _seed_repo(tmp_path)
    html = render_transcript_page(tmp_path, db, 1)
    assert html is not None
    assert '<li id="L2">' in html
    assert '<span class="ln-speaker">David Velez:</span>' in html
    # Raw HTML in the transcript is escaped, never executed.
    assert "<script>alert" not in html
    assert "&lt;script&gt;alert" in html
    assert "3 lines" in html


def test_transcript_page_rejects_wrong_type_and_missing_file(tmp_path: Path) -> None:
    db = _seed_repo(tmp_path)
    assert render_transcript_page(tmp_path, db, 2) is None  # a 10-K, not a transcript
    assert render_transcript_page(tmp_path, db, 99) is None  # unknown doc
    (tmp_path / "transcripts" / "processed" / "NU_Q1_2026.txt").unlink()
    assert render_transcript_page(tmp_path, db, 1) is None  # file gone


def test_form10k_page_sections_and_deep_link(tmp_path: Path) -> None:
    db = _seed_repo(tmp_path)
    html = render_form10k_page(tmp_path, db, 2)
    assert html is not None
    assert "<h2>Cover</h2>" in html  # default = first section
    assert "0001-01-000001" in html  # filing identity in the header
    deep = render_form10k_page(tmp_path, db, 2, section="Income Taxes")
    assert deep is not None
    assert "<h2>Income Taxes</h2>" in deep
    assert "22% effective rate" in deep
    assert "Effective rate &lt;note&gt;" in deep  # keys escaped
    assert render_form10k_page(tmp_path, db, 1) is None  # transcript, not a filing


def test_fallback_page_lists_metadata(tmp_path: Path) -> None:
    db = _seed_repo(tmp_path)
    html = render_fallback_page(db, 4)
    assert "fmp_income_statement" in html
    assert "NU_income_statement_quarterly.json" in html
    assert "Unknown document" in render_fallback_page(db, 404)


# ----------------------------------------------------------------------------
# /source/<doc_id> dispatcher route
# ----------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path) -> FlaskClient:
    _seed_repo(tmp_path)
    return comments_server.create_app(tmp_path).test_client()


def test_source_route_dispatches_by_doc_type(client: FlaskClient) -> None:
    t = client.get("/source/1")
    assert t.status_code == 200
    assert '<li id="L2">' in t.data.decode()

    k = client.get("/source/2?section=Income Taxes")
    assert k.status_code == 200
    assert "<h2>Income Taxes</h2>" in k.data.decode()

    r = client.get("/source/3")
    assert r.status_code == 302
    assert r.headers["Location"] == "https://ir.example/deck.pdf"

    f = client.get("/source/4")
    assert f.status_code == 200
    assert "No in-app viewer" in f.data.decode()


def test_restatements_panel_route(client: FlaskClient) -> None:
    resp = client.get("/api/panel/restatements")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Restatements" in body
    assert 'href="/source/2"' in body  # the new filing links into the viewer


# ----------------------------------------------------------------------------
# restatements panel
# ----------------------------------------------------------------------------


def test_load_restatements_counts_and_rows(tmp_path: Path) -> None:
    db = _seed_repo(tmp_path)
    ov = load_restatements(db)
    assert ov is not None
    assert ov.chains_total == 2  # value-changed + same-value re-report
    assert ov.value_changed == 1
    assert ov.tickers_affected == 1
    assert len(ov.rows) == 1
    row = ov.rows[0]
    assert (row.old_value, row.new_value) == (100.0, 110.0)
    assert row.new_accession == "0001-01-000001"


def test_restatements_panel_renders_was_now(tmp_path: Path) -> None:
    db = _seed_repo(tmp_path)
    html = render_restatements_panel(db)
    assert '"was X, now Y"' in html
    assert "+10.0%" in html
    assert 'href="/source/4"' in html  # the superseded filing too
    # Same-value re-reports are counted but not listed.
    assert "Chains total" in html


def test_restatements_panel_empty_and_legacy_states(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    conn = sqlite3.connect(db)
    conn.executescript(_DOCS_DDL)
    conn.commit()
    conn.close()
    assert "No supersede chains yet" in render_restatements_panel(db)

    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.execute("CREATE TABLE financial_facts (id INTEGER PRIMARY KEY, value TEXT)")
    conn.commit()
    conn.close()
    assert "alembic upgrade head" in render_restatements_panel(legacy)
