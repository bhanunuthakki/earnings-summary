"""Hermetic unit and contract tests for FPI Form 6-K and Form 20-F SEC ingestion."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

from models.facts import Unit
from pipeline.sec_fpi_ingest import (
    LocatedFpiExhibit,
    _parse_numeric,
    _strip_html,
    extract_fpi_financial_facts_html,
    extract_fpi_kpis_narrative,
    fetch_fpi_exhibit,
    persist_fpi_facts,
    resolve_cik,
)


def test_resolve_cik() -> None:
    assert resolve_cik("WIX") == "0001576789"
    assert resolve_cik("NU") == "0001691493"
    assert resolve_cik("NVO") == "0000353278"
    assert resolve_cik("ASML") == "0000937966"
    assert resolve_cik("NONEXISTENT_TICKER_XYZ") is None


def test_parse_numeric() -> None:
    assert _parse_numeric("$123.45") == Decimal("123.45")
    assert _parse_numeric("(50.2)") == Decimal("-50.2")
    assert _parse_numeric("-1,234.56") == Decimal("-1234.56")
    assert _parse_numeric("14.5%") == Decimal("14.5")
    assert _parse_numeric("") is None
    assert _parse_numeric("abc") is None


def test_strip_html() -> None:
    html = "<p>Total Revenue was <b>$563.1 million</b> &nbsp; YoY.</p>"
    plain = _strip_html(html)
    assert plain == "Total Revenue was $563.1 million YoY."


def test_extract_fpi_financial_facts_html() -> None:
    html = """
    <html>
      <body>
        <h3>Condensed Consolidated Statements of Operations (in thousands of US dollars)</h3>
        <table>
          <tr><td>Revenues</td><td>563,058</td><td>489,901</td></tr>
          <tr><td>Gross profit</td><td>384,100</td><td>331,200</td></tr>
          <tr><td>Operating income</td><td>55,120</td><td>32,400</td></tr>
          <tr><td>Net income</td><td>39,800</td><td>25,100</td></tr>
        </table>
        <h3>Condensed Consolidated Statements of Cash Flows (in thousands of US dollars)</h3>
        <table>
          <tr><td>Net cash provided by operating activities</td><td>55,562</td><td>45,100</td></tr>
          <tr><td>Purchase of property and equipment</td><td>2,920</td><td>3,100</td></tr>
        </table>
      </body>
    </html>
    """
    dt = datetime(2026, 6, 30, tzinfo=UTC)
    facts = extract_fpi_financial_facts_html(html, "WIX", dt, "Q2")

    assert "revenue" in facts
    assert facts["revenue"][0] == Decimal("563058000")  # scaled by 1,000
    assert "gross_profit" in facts
    assert facts["gross_profit"][0] == Decimal("384100000")
    assert "operating_income" in facts
    assert facts["operating_income"][0] == Decimal("55120000")
    assert "net_income" in facts
    assert facts["net_income"][0] == Decimal("39800000")
    assert "operating_cash_flow" in facts
    assert facts["operating_cash_flow"][0] == Decimal("55562000")
    assert "capital_expenditure" in facts
    assert facts["capital_expenditure"][0] == Decimal("-2920000")  # negative capex convention
    assert "free_cash_flow" in facts
    assert facts["free_cash_flow"][0] == Decimal("52642000")  # OCF - Capex


def test_extract_fpi_kpis_narrative() -> None:
    html = """
    <p>
      Total bookings in the second quarter of 2026 were $569.1 million, up 12% YoY.
      Creative Subscriptions revenue was $398.4 million.
      Creative Subscriptions bookings were $410.2 million.
      Free cash flow margin was 10.8% for the quarter.
    </p>
    """
    plain = _strip_html(html)
    kpis = extract_fpi_kpis_narrative(html, plain, "WIX")

    kpi_dict = {name: (val, unit) for name, val, unit, _ in kpis}
    assert "bookings" in kpi_dict
    assert kpi_dict["bookings"][0] == Decimal("569100000")
    assert "free_cash_flow_margin" in kpi_dict
    assert kpi_dict["free_cash_flow_margin"][0] == Decimal("10.8")
    assert "creative_subscriptions_revenue" in kpi_dict
    assert kpi_dict["creative_subscriptions_revenue"][0] == Decimal("398400000")
    assert "creative_subscriptions_bookings" in kpi_dict
    assert kpi_dict["creative_subscriptions_bookings"][0] == Decimal("410200000")


def test_fetch_fpi_exhibit_image_only_guard() -> None:
    located = LocatedFpiExhibit(
        ticker="ASML",
        cik="0000937966",
        form_type="6-K",
        accession="0001628280-26-025147",
        filing_date="2026-04-16",
        exhibit_filename="financialstatements.htm",
        exhibit_url="https://www.sec.gov/Archives/edgar/data/937966/000162828026025147/financialstatements.htm",
    )
    # Simulate a slide-deck exhibit with many <img> tags and sparse text (<800 chars)
    mock_html = "<html><body>" + '<img src="slide1.jpg" />' * 20 + "<p>Notes</p></body></html>"
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = mock_html

    mock_sess = MagicMock()
    mock_sess.get.return_value = mock_resp

    fetched = fetch_fpi_exhibit(located, session=mock_sess)
    assert fetched is not None
    assert fetched.is_image_only is True


def test_persist_fpi_facts_idempotency(migrated_db: Callable[..., Path], tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    db_path = migrated_db(tmp_path / "test_fpi_persist.db")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row


    # Setup test document
    import hashlib
    doc_content = b"<html><body>test content for evidence anchoring</body></html>"
    doc_file = project_root / "data" / "historical" / "sec" / "test_wix_doc.html"
    doc_file.parent.mkdir(parents=True, exist_ok=True)
    doc_file.write_bytes(doc_content)
    doc_sha = hashlib.sha256(doc_content).hexdigest()

    cur = conn.execute(
        """
        INSERT INTO documents (ticker, source_type, doc_type, file_path, sha256, fetched_at, fetch_status, http_code, raw_bytes_size)
        VALUES ('WIX', 'sec_xbrl', 'sec_6k', 'data/historical/sec/test_wix_doc.html', ?, '2026-08-04', 'ok', 200, ?)
        """,
        (doc_sha, len(doc_content)),
    )
    doc_id = int(cur.lastrowid)

    from provenance.evidence_backfill import ensure_legacy_document_evidence
    ensure_legacy_document_evidence(conn, repo_root=project_root, document_id=doc_id)



    dt = datetime(2026, 6, 30, tzinfo=UTC)
    financial_facts = {
        "revenue": (Decimal("563058000"), "USD", "Table: Revenue = 563,058"),
        "operating_income": (Decimal("55120000"), "USD", "Table: Operating income = 55,120"),
    }
    kpis = [
        ("bookings", Decimal("569100000"), Unit.ACTUAL, "Total bookings: $569.1M"),
        ("free_cash_flow_margin", Decimal("10.8"), Unit.PERCENT, "FCF margin: 10.8%"),
    ]

    # First insertion
    ff_n1, kpi_n1 = persist_fpi_facts(
        conn,
        ticker="WIX",
        period_end=dt,
        fiscal_period_type="Q2",
        doc_id=doc_id,
        financial_facts=financial_facts,
        kpis=kpis,
        force=False,
    )
    conn.commit()
    assert ff_n1 == 2
    assert kpi_n1 == 2

    # Second insertion with force=True (clean transactional update)
    ff_n2, kpi_n2 = persist_fpi_facts(
        conn,
        ticker="WIX",
        period_end=dt,
        fiscal_period_type="Q2",
        doc_id=doc_id,
        financial_facts=financial_facts,
        kpis=kpis,
        force=True,
    )
    conn.commit()
    assert ff_n2 == 2
    assert kpi_n2 == 2

    # Verify no fact duplication occurred
    count_ff = conn.execute(
        "SELECT COUNT(*) FROM financial_facts WHERE ticker = 'WIX' AND period_end = ? AND fiscal_period_type = 'Q2'",
        (dt,),
    ).fetchone()[0]
    assert count_ff == 2

    count_kpi = conn.execute(
        "SELECT COUNT(*) FROM kpi_facts WHERE ticker = 'WIX' AND period_end = ? AND fiscal_period_type = 'Q2'",
        (dt,),
    ).fetchone()[0]
    assert count_kpi == 2

    conn.close()
