"""Tests for src/insider_transactions.py — schema, parsing, scoring."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from insider_transactions import (
    InsiderTransaction,
    _form4_code_to_type,
    conviction_signals,
    fetch_transactions,
    parse_form4_xml,
    upsert,
)


def _schema(conn: sqlite3.Connection) -> None:
    """Mirror migration 0041 (the insider_transactions table only)."""
    conn.executescript(
        """
        CREATE TABLE insider_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker VARCHAR(16) NOT NULL,
            insider_entity_id INTEGER,
            insider_name VARCHAR(255) NOT NULL,
            insider_title VARCHAR(255), insider_relation VARCHAR(64),
            transaction_date DATETIME NOT NULL,
            filing_date DATETIME NOT NULL,
            filing_doc_id INTEGER,
            transaction_type VARCHAR(32) NOT NULL,
            shares NUMERIC(24,6) NOT NULL,
            price_per_share NUMERIC(24,6),
            transaction_value NUMERIC(24,6),
            shares_owned_after NUMERIC(24,6),
            ownership_pct FLOAT,
            is_10b5_1 BOOLEAN NOT NULL DEFAULT 0,
            acquired_or_disposed VARCHAR(1),
            ownership_form VARCHAR(1),
            form_filed VARCHAR(8) NOT NULL DEFAULT '4',
            regulator VARCHAR(16) NOT NULL DEFAULT 'SEC',
            source_url VARCHAR(1024),
            notes TEXT,
            ingested_at DATETIME NOT NULL,
            UNIQUE(ticker, insider_name, transaction_date, transaction_type, shares, form_filed)
        );
        """
    )
    conn.commit()


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    p = tmp_path / "ix.db"
    conn = sqlite3.connect(str(p))
    try:
        _schema(conn)
    finally:
        conn.close()
    return p


# ---------------------------------------------------------------------------
# Code-to-type mapping
# ---------------------------------------------------------------------------


def test_form4_code_p_is_open_market_buy() -> None:
    assert _form4_code_to_type("P", "A") == "open_market_buy"


def test_form4_code_s_is_open_market_sell() -> None:
    assert _form4_code_to_type("S", "D") == "open_market_sell"


def test_form4_code_a_is_stock_grant() -> None:
    assert _form4_code_to_type("A", "A") == "stock_grant"


def test_form4_code_m_is_exercise() -> None:
    assert _form4_code_to_type("M", "A") == "option_exercise"


def test_form4_code_f_is_rsu_vesting() -> None:
    assert _form4_code_to_type("F", "D") == "rsu_vesting"


def test_form4_code_i_acquired_is_buy() -> None:
    assert _form4_code_to_type("I", "A") == "open_market_buy"
    assert _form4_code_to_type("I", "D") == "open_market_sell"


def test_form4_code_unknown_is_other() -> None:
    assert _form4_code_to_type("Z", "A") == "other"


# ---------------------------------------------------------------------------
# XML parsing — synthetic Form 4
# ---------------------------------------------------------------------------


def _synthetic_form4_xml(*, code: str = "P", shares: float = 1000.0, price: float = 150.0) -> str:
    return f"""<?xml version="1.0"?>
<ownershipDocument>
  <schemaVersion>X0202</schemaVersion>
  <documentType>4</documentType>
  <periodOfReport>2026-04-15</periodOfReport>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerName>PICHAI SUNDAR</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>0</isDirector>
      <isOfficer>1</isOfficer>
      <isTenPercentOwner>0</isTenPercentOwner>
      <officerTitle>Chief Executive Officer</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Class A Common Stock</value></securityTitle>
      <transactionDate><value>2026-04-12</value></transactionDate>
      <transactionCoding>
        <transactionFormType>4</transactionFormType>
        <transactionCode>{code}</transactionCode>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>{shares}</value></transactionShares>
        <transactionPricePerShare><value>{price}</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>{"A" if code == "P" else "D"}</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>50000</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
      <ownershipNature>
        <directOrIndirectOwnership><value>D</value></directOrIndirectOwnership>
      </ownershipNature>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""


def test_parse_form4_extracts_purchase() -> None:
    xml = _synthetic_form4_xml(code="P", shares=1000, price=150)
    txs = parse_form4_xml(
        xml, ticker="GOOG", form_filed="4", filing_date=datetime(2026, 4, 15, tzinfo=UTC)
    )
    assert len(txs) == 1
    tx = txs[0]
    assert tx.ticker == "GOOG"
    assert tx.insider_name == "PICHAI SUNDAR"
    assert tx.insider_title == "Chief Executive Officer"
    assert tx.insider_relation == "officer"
    assert tx.transaction_type == "open_market_buy"
    assert tx.shares == 1000.0
    assert tx.price_per_share == 150.0
    assert tx.transaction_value == 150000.0
    assert tx.acquired_or_disposed == "A"
    assert tx.ownership_form == "D"
    assert tx.shares_owned_after == 50000.0
    assert tx.is_10b5_1 is False


def test_parse_form4_detects_10b5_1_footnote() -> None:
    # Inject a footnote mentioning 10b5-1
    xml = _synthetic_form4_xml(code="S", shares=500, price=150)
    xml = xml.replace(
        "</nonDerivativeTable>",
        "</nonDerivativeTable><footnotes><footnote id='F1'>Sale pursuant to 10b5-1 plan</footnote></footnotes>",
    )
    txs = parse_form4_xml(
        xml, ticker="GOOG", form_filed="4", filing_date=datetime(2026, 4, 15, tzinfo=UTC)
    )
    assert len(txs) == 1
    assert txs[0].is_10b5_1 is True


def test_parse_form4_handles_malformed_xml() -> None:
    txs = parse_form4_xml(
        "<not-valid-xml",
        ticker="X",
        form_filed="4",
        filing_date=datetime(2026, 4, 15, tzinfo=UTC),
    )
    assert txs == []


# ---------------------------------------------------------------------------
# Upsert idempotency
# ---------------------------------------------------------------------------


def test_upsert_idempotent_on_natural_key(db: Path) -> None:
    tx = InsiderTransaction(
        ticker="GOOG",
        insider_name="PICHAI SUNDAR",
        transaction_date=datetime(2026, 4, 12, tzinfo=UTC),
        filing_date=datetime(2026, 4, 15, tzinfo=UTC),
        transaction_type="open_market_buy",
        shares=1000,
        price_per_share=150,
        transaction_value=150000,
    )
    n1 = upsert([tx], db_path=db)
    n2 = upsert([tx], db_path=db)
    assert n1 == 1
    assert n2 == 0  # idempotent


def test_upsert_distinct_share_counts_are_separate_rows(db: Path) -> None:
    base = dict(
        ticker="GOOG",
        insider_name="PICHAI SUNDAR",
        transaction_date=datetime(2026, 4, 12, tzinfo=UTC),
        filing_date=datetime(2026, 4, 15, tzinfo=UTC),
        transaction_type="open_market_buy",
    )
    tx1 = InsiderTransaction(**base, shares=1000)  # type: ignore[arg-type]
    tx2 = InsiderTransaction(**base, shares=2000)  # type: ignore[arg-type]
    upsert([tx1, tx2], db_path=db)
    rows = fetch_transactions(ticker="GOOG", db_path=db)
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# Conviction signals
# ---------------------------------------------------------------------------


def test_conviction_signal_ranks_ceo_buy_highest(db: Path) -> None:
    # CEO open-market buy ($300k)
    upsert(
        [
            InsiderTransaction(
                ticker="GOOG",
                insider_name="CEO Person",
                insider_title="Chief Executive Officer",
                insider_relation="officer",
                transaction_date=datetime(2026, 5, 1, tzinfo=UTC),
                filing_date=datetime(2026, 5, 3, tzinfo=UTC),
                transaction_type="open_market_buy",
                shares=2000,
                price_per_share=150,
                transaction_value=300000,
            ),
            # Director purchase (smaller)
            InsiderTransaction(
                ticker="GOOG",
                insider_name="Director X",
                insider_title="Director",
                insider_relation="director",
                transaction_date=datetime(2026, 5, 2, tzinfo=UTC),
                filing_date=datetime(2026, 5, 4, tzinfo=UTC),
                transaction_type="open_market_buy",
                shares=50,
                price_per_share=150,
                transaction_value=7500,
            ),
            # CFO scheduled 10b5-1 sell (should be filtered out)
            InsiderTransaction(
                ticker="GOOG",
                insider_name="CFO Person",
                insider_title="Chief Financial Officer",
                insider_relation="officer",
                transaction_date=datetime(2026, 5, 5, tzinfo=UTC),
                filing_date=datetime(2026, 5, 5, tzinfo=UTC),
                transaction_type="open_market_sell",
                shares=5000,
                price_per_share=150,
                transaction_value=750000,
                is_10b5_1=True,
            ),
        ],
        db_path=db,
    )
    signals = conviction_signals(ticker="GOOG", window_days=30, db_path=db)
    # 10b5-1 sell filtered out → 2 signals
    assert len(signals) == 2
    # CEO buy should rank above Director buy
    assert signals[0].insider_name == "CEO Person"
    assert signals[0].signal_strength > signals[1].signal_strength


def test_conviction_signal_detects_cluster_event(db: Path) -> None:
    # 3 insiders buy on the same day → cluster bonus
    same_day = datetime(2026, 5, 1, tzinfo=UTC)
    upsert(
        [
            InsiderTransaction(
                ticker="META",
                insider_name=f"Officer {i}",
                insider_title="Director",
                transaction_date=same_day,
                filing_date=same_day,
                transaction_type="open_market_buy",
                shares=1000,
                price_per_share=500,
                transaction_value=500000,
            )
            for i in range(3)
        ],
        db_path=db,
    )
    signals = conviction_signals(ticker="META", window_days=30, db_path=db)
    assert len(signals) == 3
    # All three should mention "cluster (3 insiders same day)"
    assert all("cluster (3 insiders same day)" in s.rationale for s in signals)
