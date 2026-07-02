"""Owner-decision persist/link adapters + the tracker retro net.

The persist_fn/link_fn seams #709/#718 left uninjected, plus
detect_unannounced_fills — the fills→decisions inverse of
reconcile_decision_actions (which matches decisions→fills)."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from integrations.portfolio_tracker_client import LivePortfolio, LiveTransaction
from research.decision_capture import capture_decision
from research.decision_feed import (
    detect_unannounced_fills,
    link_decision_to_note,
    persist_owner_decision,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0129_commitment_scan_log"
HEAD = "0130_owner_decision_extension"

_PRE_DDL = """
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    recommendation_kind VARCHAR(32) NOT NULL,
    recommendation_value FLOAT,
    conviction VARCHAR(16),
    source_artifact_id INTEGER,
    source_memo_id INTEGER,
    source_dismissal_id INTEGER,
    rationale_excerpt TEXT,
    user_notes TEXT,
    made_at DATETIME NOT NULL,
    outcome_label VARCHAR(16),
    created_at DATETIME NOT NULL,
    CONSTRAINT ck_decisions_source_present CHECK (
        source_artifact_id IS NOT NULL OR source_memo_id IS NOT NULL
        OR recommendation_kind = 'avoid')
);
CREATE TABLE tenants (id TEXT PRIMARY KEY);
INSERT INTO tenants (id) VALUES ('bhanu');
CREATE TABLE analyst_notes (
    id INTEGER NOT NULL,
    user_id TEXT DEFAULT 'bhanu' NOT NULL,
    ticker TEXT,
    kind TEXT NOT NULL,
    status TEXT DEFAULT 'open' NOT NULL,
    body TEXT NOT NULL,
    source TEXT NOT NULL,
    source_ref TEXT,
    context_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    decision_id INTEGER,
    PRIMARY KEY (id),
    CONSTRAINT ck_analyst_notes_kind CHECK (kind IN
        ('question','decision','watch','assumption','observation','musing'))
);
CREATE VIRTUAL TABLE analyst_notes_fts USING fts5(
    body, content='analyst_notes', content_rowid='id');
"""


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "feed.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(_PRE_DDL)
        conn.commit()
    finally:
        conn.close()
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, HEAD)
    return db


def _txn(ticker: str, day: str, kind: str, amount: float) -> LiveTransaction:
    return LiveTransaction(
        date=day,
        ticker=ticker,
        name=ticker,
        type=kind,
        subtype=None,
        quantity=1.0,
        amount=amount,
        account_name="Roth IRA" if ticker == "WIX" else "Brokerage",
    )


def _portfolio(*txns: LiveTransaction, available: bool = True) -> LivePortfolio:
    return LivePortfolio(available=available, api_url="http://test", transactions=list(txns))


def _row(db: Path, decision_id: int) -> sqlite3.Row:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
    finally:
        conn.close()


def test_persist_owner_decision_writes_0130_shape(db_path: Path) -> None:
    did = persist_owner_decision(
        ticker="nvo",
        direction="buy",
        conviction="high",
        falsifier="GLP-1 share loss 2Q",
        size_usd=31000,
        account="taxable",
        db_path=db_path,
    )
    row = _row(db_path, did)
    assert row["decided_by"] == "owner"
    assert row["recommendation_kind"] == "initiate"
    assert row["ticker"] == "NVO"
    assert (row["conviction"], row["falsifier"]) == ("high", "GLP-1 share loss 2Q")
    with pytest.raises(ValueError):
        persist_owner_decision(ticker="NVO", direction="yolo", db_path=db_path)


def test_capture_decision_composes_with_the_adapters(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO analyst_notes (kind, body, source, created_at, updated_at) "
            "VALUES ('musing','adding to NU','capture','2026-07-02','2026-07-02')"
        )
        conn.commit()
        note_id = int(conn.execute("SELECT max(id) FROM analyst_notes").fetchone()[0])
    finally:
        conn.close()

    did = capture_decision(
        ticker="NU",
        direction="add",
        size_pct=3.0,
        conviction="high",
        falsifier="NPL >5% 2Q",
        note_id=note_id,
        db_path=db_path,
        persist_fn=persist_owner_decision,
        link_fn=link_decision_to_note,
    )
    assert did is not None
    conn = sqlite3.connect(str(db_path))
    try:
        linked = conn.execute(
            "SELECT decision_id FROM analyst_notes WHERE id=?", (note_id,)
        ).fetchone()[0]
        assert linked == did
    finally:
        conn.close()


def test_retro_net_stubs_unannounced_and_skips_announced(db_path: Path) -> None:
    now = datetime(2026, 7, 2, 12, 0, 0)
    # Announced: an owner decision on NVO the same day a buy fill lands
    persist_owner_decision(
        ticker="NVO", direction="buy", made_at="2026-07-01T00:00:00", db_path=db_path
    )
    portfolio = _portfolio(
        _txn("NVO", "2026-07-01", "buy", 31000.0),  # announced → matched
        _txn("WIX", "2026-06-30", "sell", 9000.0),  # unannounced → stub (roth)
        _txn("NU", "2026-06-29", "buy", 400.0),  # below threshold
        _txn("MU", "2026-05-01", "buy", 50000.0),  # outside lookback
    )
    tally = detect_unannounced_fills(
        db_path=db_path, portfolio=portfolio, lookback_days=7, min_usd=1000.0, now=now
    )
    assert tally["matched"] == 1
    assert tally["stubs_created"] == 1
    assert tally["below_threshold"] == 1
    assert tally["fills_seen"] == 3  # MU is outside the lookback window

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        stub = conn.execute(
            "SELECT * FROM decisions WHERE user_notes LIKE '%retro-net:WIX%'"
        ).fetchone()
        assert stub["recommendation_kind"] == "sell"
        assert stub["account"] == "roth"
        assert stub["conviction"] is None  # annotation pending — coach asks
        assert stub["size_usd"] == 9000.0
    finally:
        conn.close()

    # Idempotent: the marker key blocks a duplicate stub on rerun
    again = detect_unannounced_fills(
        db_path=db_path, portfolio=portfolio, lookback_days=7, min_usd=1000.0, now=now
    )
    assert again["stubs_created"] == 0
    assert again["skipped_existing"] == 1


def test_retro_net_degrades_offline(db_path: Path) -> None:
    tally = detect_unannounced_fills(db_path=db_path, portfolio=_portfolio(available=False))
    assert tally["tracker_unavailable"] == 1
    assert tally["stubs_created"] == 0
