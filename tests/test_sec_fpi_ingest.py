"""Hermetic unit and contract tests for FPI Form 6-K and Form 20-F SEC ingestion."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

from models.facts import Currency, Unit
from pipeline.sec_fpi_ingest import (
    LocatedFpiExhibit,
    extract_fpi_financial_facts_html,
    extract_fpi_kpis_narrative,
    fetch_fpi_exhibit,
    parse_numeric,
    persist_fpi_facts,
    resolve_cik,
    strip_html,
)
from provenance.evidence_backfill import ensure_legacy_document_evidence
from table_extractors.period_axis import expected_period_ends


def test_resolve_cik() -> None:
    assert resolve_cik("WIX") == "0001576789"
    assert resolve_cik("NU") == "0001691493"
    assert resolve_cik("NVO") == "0000353278"
    assert resolve_cik("ASML") == "0000937966"
    assert resolve_cik("NONEXISTENT_TICKER_XYZ") is None


def test_parse_numeric() -> None:
    assert parse_numeric("$123.45") == Decimal("123.45")
    assert parse_numeric("(50.2)") == Decimal("-50.2")
    assert parse_numeric("-1,234.56") == Decimal("-1234.56")
    assert parse_numeric("14.5%") == Decimal("14.5")
    assert parse_numeric("") is None
    assert parse_numeric("abc") is None


def test_strip_html() -> None:
    html = "<p>Total Revenue was <b>$563.1 million</b> &nbsp; YoY.</p>"
    plain = strip_html(html)
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
      All monetary amounts are in US dollars. Total bookings in the second quarter of 2026 were $569.1 million, up 12% YoY.
      Creative Subscriptions revenue was $398.4 million.
      Creative Subscriptions bookings were $410.2 million.
      Free cash flow margin was 10.8% for the quarter.
    </p>
    """
    plain = strip_html(html)
    kpis = extract_fpi_kpis_narrative(html, plain, "WIX")

    kpi_dict = {name: (val, unit, currency) for name, val, unit, currency, _ in kpis}
    assert "bookings" in kpi_dict
    assert kpi_dict["bookings"][0] == Decimal("569100000")
    assert kpi_dict["bookings"][2] is Currency.USD
    assert "free_cash_flow_margin" in kpi_dict
    assert kpi_dict["free_cash_flow_margin"][0] == Decimal("10.8")
    assert "creative_subscriptions_revenue" in kpi_dict
    assert kpi_dict["creative_subscriptions_revenue"][0] == Decimal("398400000")
    assert "creative_subscriptions_bookings" in kpi_dict
    assert kpi_dict["creative_subscriptions_bookings"][0] == Decimal("410200000")


def test_extract_fpi_financial_facts_uses_declared_dkk_and_skips_unknown_currency() -> None:
    declared = (
        "<h3>Income statement (in DKK millions)</h3><table>"
        "<tr><td>Revenue</td><td>12</td></tr></table>"
    )
    facts = extract_fpi_financial_facts_html(declared, "NVO", datetime(2026, 6, 30), "Q2")
    assert facts["revenue"][:2] == (Decimal("12000000"), "DKK")
    unknown = "<table><tr><td>Revenue</td><td>$12</td></tr></table>"
    assert extract_fpi_financial_facts_html(unknown, "NVO", datetime(2026, 6, 30), "Q2") == {}


def test_extract_fpi_currency_is_local_to_the_matched_table() -> None:
    html = """
    <h3>Prior table (in USD millions)</h3><table><tr><td>Other</td><td>1</td></tr></table>
    <h3>Current table (in DKK millions)</h3><table><tr><td>Revenue</td><td>12</td></tr></table>
    """
    facts = extract_fpi_financial_facts_html(html, "NVO", datetime(2026, 6, 30), "Q2")
    assert facts["revenue"][:2] == (Decimal("12000000"), "DKK")


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
    repo_root = tmp_path / "repo"
    doc_dir = repo_root / "data" / "historical" / "sec"
    doc_dir.mkdir(parents=True, exist_ok=True)
    doc_content = b"<html><body>test content for evidence anchoring</body></html>"
    doc_file = doc_dir / "test_wix_doc.html"
    doc_file.write_bytes(doc_content)
    doc_sha = hashlib.sha256(doc_content).hexdigest()

    db_path = migrated_db(tmp_path / "test_fpi_persist.db")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            """
            INSERT INTO documents (ticker, source_type, doc_type, file_path, sha256, fetched_at, fetch_status, http_code, raw_bytes_size)
            VALUES ('WIX', 'sec_xbrl', 'sec_6k', 'data/historical/sec/test_wix_doc.html', ?, '2026-08-04', 'ok', 200, ?)
            """,
            (doc_sha, len(doc_content)),
        )
        assert cur.lastrowid is not None
        doc_id = int(cur.lastrowid)

        ensure_legacy_document_evidence(conn, repo_root=repo_root, document_id=doc_id)

        stored = conn.execute(
            "SELECT sha256, file_path FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
        assert stored is not None
        assert stored["sha256"] == doc_sha
        assert stored["file_path"] == "data/historical/sec/test_wix_doc.html"
        anchored = repo_root / stored["file_path"]
        assert anchored.is_file()
        assert anchored.read_bytes() == doc_content

        dt = datetime(2026, 6, 30, tzinfo=UTC)
        financial_facts = {
            "revenue": (Decimal("563058000"), "USD", "Table: Revenue = 563,058"),
            "operating_income": (Decimal("55120000"), "USD", "Table: Operating income = 55,120"),
        }
        kpis: list[tuple[str, Decimal, Unit, Currency | None, str]] = [
            (
                "bookings",
                Decimal("569100000"),
                Unit.ACTUAL,
                Currency.USD,
                "Total bookings: $569.1M",
            ),
            ("free_cash_flow_margin", Decimal("10.8"), Unit.PERCENT, None, "FCF margin: 10.8%"),
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
    finally:
        conn.close()


def test_extract_fpi_handles_paragraph_unit_headers_note_cols_and_standalone_vs_ytd() -> None:
    nu_like = """
    <html><body>
      <p>Unaudited Interim Condensed Consolidated Statements of Income</p>
      <p>For the three and nine-month periods ended September 30, 2026 and 2025</p>
      <p>(In thousands of U.S. Dollars, except earnings per share)</p>
      <table>
        <tr><td></td><td></td><td colspan="2">Three-month period ended</td><td colspan="2">Nine-month period ended</td></tr>
        <tr><td></td><td>Note</td><td>09/30/2026</td><td>09/30/2025</td><td>09/30/2026</td><td>09/30/2025</td></tr>
        <tr><td>Total revenue</td><td></td><td>5,820,364</td><td>4,172,716</td><td>16,301,539</td><td>11,088,875</td></tr>
        <tr><td>Gross profit</td><td></td><td>2,436,898</td><td>1,814,532</td><td>6,648,309</td><td>4,681,963</td></tr>
        <tr><td>Net income for the period</td><td></td><td>1,091,558</td><td>782,678</td><td>3,024,078</td><td>1,976,873</td></tr>
        <tr><td>Net income attributable to shareholders of the parent company</td><td></td><td>1,090,836</td><td>782,473</td><td>3,023,091</td><td>1,976,514</td></tr>
        <tr><td>Net income attributable to non-controlling interests</td><td></td><td>722</td><td>205</td><td>987</td><td>359</td></tr>
        <tr><td>Earnings per share \u2013 Diluted</td><td>9</td><td>0.2235</td><td>0.1595</td><td>0.6166</td><td>0.4031</td></tr>
      </table>
      <p>(In thousands of U.S. Dollars)</p>
      <p>For the nine-month period ended September 30, 2026 and 2025</p>
      <table>
        <tr><td>Note</td><td>09/30/2026</td><td>09/30/2025</td></tr>
        <tr><td>Cash flows generated from (used in) operating activities</td><td></td><td>(1,049,562)</td><td>2,669,433</td></tr>
      </table>
    </body></html>
    """
    facts = extract_fpi_financial_facts_html(nu_like, "NU", datetime(2026, 9, 30, tzinfo=UTC), "Q3")
    assert facts["revenue"][:2] == (Decimal("5820364000"), "USD")
    assert facts["gross_profit"][:2] == (Decimal("2436898000"), "USD")
    assert facts["net_income"][:2] == (Decimal("1090836000"), "USD")
    assert facts["eps_diluted"][:2] == (Decimal("0.2235"), "USD")
    assert "operating_cash_flow" not in facts  # 9M-only cash flow table must be skipped


def test_extract_fpi_handles_dkk_million_standalone_fcf_and_ignores_pct_cells() -> None:
    nvo_like = """
    <html><body>
      <table>
        <tr><td>FINANCIAL HIGHLIGHTS</td><td>Q3 2026</td><td>Growth in DKK</td><td>9M 2026</td></tr>
        <tr><td colspan="4">(Amounts are in DKK million, except for earnings per share)</td></tr>
        <tr><td>Net sales</td><td>79,195</td><td>6%</td><td>254,506</td></tr>
        <tr><td>Adjusted net sales 1</td><td>79,195</td><td>6%</td><td>227,746</td></tr>
        <tr><td>Gross profit</td><td>60,958</td><td>7%</td><td>205,571</td></tr>
        <tr><td>Operating profit (EBIT)</td><td>23,011</td><td>(3%)</td><td>109,690</td></tr>
        <tr><td>Net profit</td><td>16,740</td><td>(16%)</td><td>86,286</td></tr>
        <tr><td>Diluted earnings per share / ADR (in DKK)</td><td>3.79</td><td>(16%)</td><td>19.45</td></tr>
      </table>
      <table>
        <tr><td>DKK million</td><td>9M 2026</td><td>9M 2025</td></tr>
        <tr><td>Net cash flows from operating activities</td><td>120,993</td><td>112,770</td></tr>
      </table>
      <table>
        <tr><td>DKK million</td><td>Q3 2026</td><td>Q3 2025</td><td>9M 2026</td></tr>
        <tr><td>Net cash generated from operating activities</td><td>41,710</td><td>46,266</td><td>120,993</td></tr>
        <tr><td>Purchase of property, plant and equipment</td><td>(13,227)</td><td>(13,628)</td><td>(37,213)</td></tr>
        <tr><td>Free cash flow 1)</td><td>28,483</td><td>32,638</td><td>83,780</td></tr>
      </table>
      <table>
        <tr><td>DKK million</td><td>Q3 2026</td></tr>
        <tr><td>Gross margin</td><td>77.0%</td></tr>
      </table>
    </body></html>
    """
    facts = extract_fpi_financial_facts_html(
        nvo_like, "NVO", datetime(2026, 9, 30, tzinfo=UTC), "Q3"
    )
    assert facts["revenue"][:2] == (Decimal("79195000000"), "DKK")
    assert facts["gross_profit"][:2] == (Decimal("60958000000"), "DKK")
    assert facts["operating_income"][:2] == (Decimal("23011000000"), "DKK")
    assert facts["net_income"][:2] == (Decimal("16740000000"), "DKK")
    assert facts["eps_diluted"][:2] == (Decimal("3.79"), "DKK")
    assert facts["operating_cash_flow"][:2] == (Decimal("41710000000"), "DKK")
    assert facts["capital_expenditure"][:2] == (Decimal("-13227000000"), "DKK")
    assert facts["free_cash_flow"][:2] == (Decimal("28483000000"), "DKK")
    # Also verify Q4 nominal quarter resolves cleanly without KeyError
    q4_end, _ = expected_period_ends("Q4", 2025, 12, 31)
    assert q4_end.isoformat() == "2025-12-31"
