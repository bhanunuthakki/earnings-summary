"""Tests for src/position_lifecycle.py + the holding-page timeline panel
(fund-grade S5, PR 2).

The reconciler is state-based — these tests drive it through portfolio-list
changes and injected tracker snapshots (the tracker client itself is never
called):

  * open on a new portfolio name — tracker OFFLINE (entry_date = today,
    price NULL, honest degradation) and ONLINE (buy-transaction date+price,
    avg-cost fallback)
  * close on portfolio exit — sell-transaction enrichment vs offline today
  * idempotency (second run over the same world is a no-op)
  * --backfill semantics (source='backfill', entry_date NULL without a txn)
  * entry snapshots: conviction (decisions fallback), thesis excerpt
    (holdings JSON), entry_conditions (decisions.decision_conditions, #468)
  * enrichment pass fills a missing entry_price once the tracker returns
  * update_exit_fields validation (enum gate, LookupError, clear-with-empty)
  * panel render: timeline shapes, grading form only on ungraded closed rows
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from integrations.portfolio_tracker_client import (
    LivePortfolio,
    LivePosition,
    LiveTransaction,
)
from pipeline.position_lifecycle_panel import render_position_lifecycle_section
from position_lifecycle import (
    PositionEntry,
    list_entries,
    sync_position_lifecycle,
    update_exit_fields,
)

_SCHEMA = """
CREATE TABLE position_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'bhanu',
    ticker TEXT NOT NULL,
    entry_date TEXT,
    entry_price FLOAT,
    entry_conviction TEXT,
    entry_thesis_excerpt TEXT,
    entry_conditions TEXT,
    exit_date TEXT,
    exit_price FLOAT,
    exit_reason TEXT,
    lessons TEXT,
    outcome_vs_thesis TEXT,
    source TEXT NOT NULL DEFAULT 'reconciler',
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    CHECK (outcome_vs_thesis IS NULL OR outcome_vs_thesis IN
           ('played_out','broke','mixed','unrelated')),
    CHECK (source IN ('reconciler','backfill','manual'))
);
CREATE UNIQUE INDEX uq_position_entries_open ON position_entries(user_id, ticker)
    WHERE exit_date IS NULL;
CREATE TABLE tracked_companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    list_type TEXT NOT NULL,
    archived_at TEXT
);
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    recommendation_kind TEXT NOT NULL,
    recommendation_value FLOAT,
    conviction TEXT,
    source_artifact_id INTEGER,
    source_lens TEXT,
    made_at DATETIME NOT NULL,
    outcome_at DATETIME,
    decision_conditions TEXT,
    created_at DATETIME NOT NULL
);
CREATE TABLE position_sizing_intent (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    intent_kind TEXT NOT NULL,
    intent_value TEXT,
    narrative TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_NOW = datetime.now(UTC).replace(tzinfo=None).isoformat()
_TODAY = datetime.now(UTC).replace(tzinfo=None).date().isoformat()

_CONDITION = {
    "metric": "NPL 90d+",
    "metric_source": "kpi",
    "op": "gt",
    "threshold": 7.0,
    "unit": "percent",
    "for_periods": 2,
    "note": "credit deterioration",
}


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A repo-shaped tmp dir: data/portfolio.db + micro_thesis/holdings/."""
    (tmp_path / "data").mkdir()
    db = tmp_path / "data" / "portfolio.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    holdings = tmp_path / "micro_thesis" / "holdings"
    holdings.mkdir(parents=True)
    (holdings / "NU.json").write_text(
        json.dumps({"ticker": "NU", "thesis": "LatAm digital bank with a deposit moat."}),
        encoding="utf-8",
    )
    return tmp_path


def _db(repo: Path) -> Path:
    return repo / "data" / "portfolio.db"


def _set_portfolio(repo: Path, tickers: list[str]) -> None:
    conn = sqlite3.connect(str(_db(repo)))
    conn.execute("DELETE FROM tracked_companies")
    for t in tickers:
        conn.execute(
            "INSERT INTO tracked_companies (ticker, list_type) VALUES (?, 'portfolio')", (t,)
        )
    # An archived portfolio row and a watchlist row must never count.
    conn.execute(
        "INSERT INTO tracked_companies (ticker, list_type, archived_at) "
        "VALUES ('DEAD', 'portfolio', ?)",
        (_NOW,),
    )
    conn.execute("INSERT INTO tracked_companies (ticker, list_type) VALUES ('WATCH', 'watchlist')")
    conn.commit()
    conn.close()


def _offline() -> LivePortfolio:
    return LivePortfolio(available=False, api_url="http://x", error="down")


def _online(
    *,
    positions: list[LivePosition] | None = None,
    transactions: list[LiveTransaction] | None = None,
) -> LivePortfolio:
    return LivePortfolio(
        available=True,
        api_url="http://x",
        positions=positions or [],
        transactions=transactions or [],
    )


def _txn(ticker: str, type_: str, date: str, quantity: float, amount: float) -> LiveTransaction:
    return LiveTransaction(
        date=date,
        ticker=ticker,
        name=None,
        type=type_,
        subtype=None,
        quantity=quantity,
        amount=amount,
        account_name="brokerage",
    )


# ---------------------------------------------------------------------------
# Reconciler — open / close / idempotency
# ---------------------------------------------------------------------------


def test_open_on_new_portfolio_name_tracker_offline(repo: Path) -> None:
    _set_portfolio(repo, ["NU"])
    tally = sync_position_lifecycle(db_path=_db(repo), portfolio=_offline())
    assert tally == {
        "opened": 1,
        "closed": 0,
        "unchanged": 0,
        "tracker_available": False,
        "db_unavailable": 0,
    }
    [entry] = list_entries(db_path=_db(repo), ticker="NU")
    assert entry.is_open
    assert entry.entry_date == _TODAY  # live transition: today is honest
    assert entry.entry_price is None  # tracker offline — no invented price
    assert entry.source == "reconciler"
    assert entry.entry_thesis_excerpt is not None
    assert "deposit moat" in entry.entry_thesis_excerpt


def test_open_uses_buy_transaction_then_avg_cost(repo: Path) -> None:
    _set_portfolio(repo, ["NU", "MELI"])
    tracker = _online(
        positions=[
            LivePosition(
                ticker="MELI",
                name=None,
                quantity=10.0,
                market_value=None,
                cost_basis=18_000.0,  # avg 1800
                unrealized_pnl=None,
                percent_of_portfolio=None,
            )
        ],
        transactions=[
            _txn("NU", "buy", "2026-06-01", 100.0, -1180.0),  # $11.80/sh
            _txn("NU", "buy", "2026-06-08", 50.0, -640.0),  # later buy — earliest wins
        ],
    )
    sync_position_lifecycle(db_path=_db(repo), portfolio=tracker)

    [nu] = list_entries(db_path=_db(repo), ticker="NU")
    assert nu.entry_date == "2026-06-01"
    assert nu.entry_price == 11.8

    [meli] = list_entries(db_path=_db(repo), ticker="MELI")
    assert meli.entry_date == _TODAY  # no txn visible — detection date
    assert meli.entry_price == 1800.0  # avg-cost fallback


def test_close_on_portfolio_exit_with_sell_enrichment(repo: Path) -> None:
    _set_portfolio(repo, ["NU"])
    sync_position_lifecycle(db_path=_db(repo), portfolio=_offline())

    _set_portfolio(repo, [])  # NU leaves the portfolio
    tracker = _online(transactions=[_txn("NU", "sell", "2026-06-10", 150.0, 1920.0)])
    tally = sync_position_lifecycle(db_path=_db(repo), portfolio=tracker)
    assert tally["closed"] == 1

    [entry] = list_entries(db_path=_db(repo), ticker="NU")
    assert not entry.is_open
    assert entry.exit_date == "2026-06-10"
    assert entry.exit_price == 12.8  # 1920 / 150
    assert entry.exit_reason is None  # grading is the analyst's, not the bot's


def test_close_tracker_offline_stamps_today(repo: Path) -> None:
    _set_portfolio(repo, ["NU"])
    sync_position_lifecycle(db_path=_db(repo), portfolio=_offline())
    _set_portfolio(repo, [])
    sync_position_lifecycle(db_path=_db(repo), portfolio=_offline())
    [entry] = list_entries(db_path=_db(repo), ticker="NU")
    assert entry.exit_date == _TODAY
    assert entry.exit_price is None


def test_sync_is_idempotent_and_reopen_creates_second_row(repo: Path) -> None:
    _set_portfolio(repo, ["NU"])
    sync_position_lifecycle(db_path=_db(repo), portfolio=_offline())
    again = sync_position_lifecycle(db_path=_db(repo), portfolio=_offline())
    assert again["opened"] == 0
    assert again["unchanged"] == 1

    _set_portfolio(repo, [])
    sync_position_lifecycle(db_path=_db(repo), portfolio=_offline())
    _set_portfolio(repo, ["NU"])  # re-entry opens a SECOND lifecycle row
    sync_position_lifecycle(db_path=_db(repo), portfolio=_offline())
    entries = list_entries(db_path=_db(repo), ticker="NU")
    assert len(entries) == 2
    assert sum(1 for e in entries if e.is_open) == 1
    assert entries[0].is_open  # open stint leads the timeline


def test_backfill_marks_unknown_entry_date(repo: Path) -> None:
    _set_portfolio(repo, ["NU"])
    tally = sync_position_lifecycle(
        db_path=_db(repo), portfolio=_offline(), assume_preexisting=True
    )
    assert tally["opened"] == 1
    [entry] = list_entries(db_path=_db(repo), ticker="NU")
    assert entry.entry_date is None  # held before the ledger existed — honest
    assert entry.source == "backfill"


# ---------------------------------------------------------------------------
# Entry snapshots — conviction, conditions
# ---------------------------------------------------------------------------


def test_entry_snapshots_conviction_and_conditions(repo: Path) -> None:
    conn = sqlite3.connect(str(_db(repo)))
    conn.execute(
        "INSERT INTO decisions (ticker, recommendation_kind, conviction, source_artifact_id, "
        "made_at, decision_conditions, created_at) VALUES ('NU', 'trim', 'high', 1, ?, ?, ?)",
        (_NOW, json.dumps([_CONDITION]), _NOW),
    )
    conn.commit()
    conn.close()

    _set_portfolio(repo, ["NU"])
    sync_position_lifecycle(db_path=_db(repo), portfolio=_offline())
    [entry] = list_entries(db_path=_db(repo), ticker="NU")
    assert entry.entry_conviction == "high"  # decisions fallback
    assert entry.entry_conditions is not None
    snapshot = json.loads(entry.entry_conditions)
    assert snapshot[0]["metric"] == "NPL 90d+"


def test_enrichment_fills_missing_entry_price_later(repo: Path) -> None:
    _set_portfolio(repo, ["NU"])
    sync_position_lifecycle(db_path=_db(repo), portfolio=_offline())
    tracker = _online(
        positions=[
            LivePosition(
                ticker="NU",
                name=None,
                quantity=100.0,
                market_value=None,
                cost_basis=1180.0,
                unrealized_pnl=None,
                percent_of_portfolio=None,
            )
        ]
    )
    sync_position_lifecycle(db_path=_db(repo), portfolio=tracker)
    [entry] = list_entries(db_path=_db(repo), ticker="NU")
    assert entry.entry_price == 11.8


# ---------------------------------------------------------------------------
# Degradation
# ---------------------------------------------------------------------------


def test_missing_db_and_missing_tables_degrade(tmp_path: Path) -> None:
    tally = sync_position_lifecycle(db_path=tmp_path / "nope.db", portfolio=_offline())
    assert tally["db_unavailable"] == 1

    # position_entries present but tracked_companies absent: must NOT
    # mass-close on missing membership data.
    db = tmp_path / "partial.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA.split("CREATE TABLE tracked_companies")[0])
    conn.commit()
    conn.close()
    tally = sync_position_lifecycle(db_path=db, portfolio=_offline())
    assert tally["db_unavailable"] == 1


# ---------------------------------------------------------------------------
# update_exit_fields
# ---------------------------------------------------------------------------


def _one_closed_entry(repo: Path) -> PositionEntry:
    _set_portfolio(repo, ["NU"])
    sync_position_lifecycle(db_path=_db(repo), portfolio=_offline())
    _set_portfolio(repo, [])
    sync_position_lifecycle(db_path=_db(repo), portfolio=_offline())
    [entry] = list_entries(db_path=_db(repo), ticker="NU")
    return entry


def test_update_exit_fields_roundtrip_and_validation(repo: Path) -> None:
    entry = _one_closed_entry(repo)
    assert update_exit_fields(
        db_path=_db(repo),
        entry_id=entry.id,
        exit_reason="Thesis broke on funding costs",
        lessons="Watch deposit beta earlier",
        outcome_vs_thesis="broke",
    )
    [after] = list_entries(db_path=_db(repo), ticker="NU")
    assert after.exit_reason == "Thesis broke on funding costs"
    assert after.outcome_vs_thesis == "broke"

    # Empty string clears; None leaves untouched.
    assert update_exit_fields(db_path=_db(repo), entry_id=entry.id, exit_reason="")
    [after] = list_entries(db_path=_db(repo), ticker="NU")
    assert after.exit_reason is None
    assert after.lessons == "Watch deposit beta earlier"

    with pytest.raises(ValueError, match="outcome_vs_thesis"):
        update_exit_fields(db_path=_db(repo), entry_id=entry.id, outcome_vs_thesis="vibes")
    with pytest.raises(LookupError):
        update_exit_fields(db_path=_db(repo), entry_id=99999, lessons="x")


# ---------------------------------------------------------------------------
# Panel render
# ---------------------------------------------------------------------------


def test_panel_renders_timeline_and_grade_form(repo: Path) -> None:
    conn = sqlite3.connect(str(_db(repo)))
    conn.execute(
        "INSERT INTO decisions (ticker, recommendation_kind, conviction, source_artifact_id, "
        "made_at, decision_conditions, created_at) VALUES ('NU', 'trim', 'high', 1, ?, ?, ?)",
        (_NOW, json.dumps([_CONDITION]), _NOW),
    )
    conn.commit()
    conn.close()
    entry = _one_closed_entry(repo)  # closed, ungraded
    _set_portfolio(repo, ["NU"])  # and a fresh open stint
    sync_position_lifecycle(db_path=_db(repo), portfolio=_offline())

    html = render_position_lifecycle_section(_db(repo), "nu", user_id="bhanu")
    assert 'data-plc-ticker="NU"' in html
    assert "Position lifecycle" in html
    assert "OPEN" in html  # the open stint's pill
    assert "NPL 90d+ &gt; 7.0 percent" in html  # entry-conditions snapshot
    assert f'data-plc-grade="{entry.id}"' in html  # grading form on the ungraded close
    assert "outcome vs thesis" in html

    # Grading completed → the form disappears, the outcome pill shows.
    update_exit_fields(
        db_path=_db(repo),
        entry_id=entry.id,
        exit_reason="took profits",
        lessons="size up earlier",
        outcome_vs_thesis="played_out",
    )
    html = render_position_lifecycle_section(_db(repo), "NU", user_id="bhanu")
    assert f'data-plc-grade="{entry.id}"' not in html
    assert "thesis played out" in html
    assert "size up earlier" in html


def test_panel_empty_state(repo: Path) -> None:
    html = render_position_lifecycle_section(_db(repo), "NU", user_id="bhanu")
    assert "No lifecycle rows yet" in html
    assert "data-plc-root" in html  # the refresh contract holds even when empty
