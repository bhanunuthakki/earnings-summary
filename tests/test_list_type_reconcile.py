"""Unit tests for list_type_reconcile — the holdings → list_type reconciler.

The rule (user directive 2026-06-15): operating companies held > $threshold are
``portfolio``; portfolio names not held demote to ``evaluation``; ETFs/funds/cash
are never promoted; pinned names stay in portfolio regardless of holdings.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from list_type_reconcile import (  # noqa: E402
    apply_reclassification,
    compute_reclassification,
    load_pins,
)

USER = "bhanu"


def _es_db() -> sqlite3.Connection:
    """A minimal tracked_companies table (only the columns the reconciler reads)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE tracked_companies (
            user_id TEXT, ticker TEXT, name TEXT, list_type TEXT,
            ir_url TEXT, archived_at TIMESTAMP,
            processing_tier TEXT, brief_dirty BOOLEAN DEFAULT 0
        )
        """
    )
    return conn


def _track(
    conn: sqlite3.Connection, ticker: str, list_type: str, *, archived: bool = False
) -> None:
    conn.execute(
        "INSERT INTO tracked_companies(user_id, ticker, name, list_type, ir_url, archived_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            USER,
            ticker,
            f"{ticker} Inc",
            list_type,
            f"https://ir.{ticker}.com",
            "2020-01-01" if archived else None,
        ),
    )
    conn.commit()


def _tracker_db() -> sqlite3.Connection:
    """A minimal portfolio-tracker schema: securities + holdings_snapshots."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE securities (security_id INTEGER PRIMARY KEY, ticker TEXT, "
        "type TEXT, is_cash_equivalent INTEGER DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE holdings_snapshots (snapshot_date TEXT, account_id INTEGER, "
        "security_id INTEGER, quantity REAL, institution_value REAL)"
    )
    return conn


def _hold(
    conn: sqlite3.Connection,
    sec_id: int,
    ticker: str,
    sec_type: str,
    value: float,
    *,
    cash_eq: int = 0,
    account: int = 1,
    snapshot: str = "2026-06-14",
    qty: float = 10.0,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO securities(security_id, ticker, type, is_cash_equivalent) "
        "VALUES (?, ?, ?, ?)",
        (sec_id, ticker, sec_type, cash_eq),
    )
    conn.execute(
        "INSERT INTO holdings_snapshots(snapshot_date, account_id, security_id, quantity, institution_value) "
        "VALUES (?, ?, ?, ?, ?)",
        (snapshot, account, sec_id, qty, value),
    )
    conn.commit()


# ---------------------------------------------------------------------------


def test_promotes_held_equity_demotes_unheld_portfolio() -> None:
    es, tk = _es_db(), _tracker_db()
    # Held equities over threshold:
    _hold(tk, 1, "BKNG", "equity", 6237.0)
    _hold(tk, 2, "UBER", "equity", 14935.0)
    _hold(tk, 3, "MELI", "cs", 88258.0)
    # Tracked state:
    _track(es, "BKNG", "evaluation")  # → promote
    _track(es, "UBER", "evaluation")  # → promote
    _track(es, "MELI", "portfolio")  # held → unchanged
    _track(es, "AMZN", "portfolio")  # not held → demote
    _track(es, "GOOG", "portfolio")  # not held → demote

    plan = compute_reclassification(es_conn=es, tracker_conn=tk, user_id=USER)

    assert [p[0] for p in plan.promotions] == ["BKNG", "UBER"]
    assert all(p[1] == "evaluation" for p in plan.promotions)
    assert [d[0] for d in plan.demotions] == ["AMZN", "GOOG"]
    assert plan.unchanged_portfolio == ["MELI"]
    assert plan.has_changes


def test_etfs_funds_cash_never_promoted() -> None:
    es, tk = _es_db(), _tracker_db()
    # Large positions, but none are operating companies → never promoted.
    _hold(tk, 1, "FLKR", "et", 49626.0)  # Franklin FTSE South Korea ETF
    _hold(tk, 2, "VTI", "et", 136754.0)  # index ETF
    _hold(tk, 3, "SPY", "etf", 19034.0)  # index ETF
    _hold(tk, 4, "FDRXX", "oef", 5000.0, cash_eq=1)  # money-market fund
    _hold(tk, 5, "CUR:USD", "cash", 5000.0, cash_eq=1)
    _track(es, "FLKR", "evaluation")  # even if tracked, an ETF must not promote
    _track(es, "VTI", "watchlist")

    plan = compute_reclassification(es_conn=es, tracker_conn=tk, user_id=USER)

    assert plan.promotions == []
    assert plan.untracked_held == []  # ETFs aren't "held operating companies"
    assert not plan.has_changes


def test_threshold_excludes_tiny_positions() -> None:
    es, tk = _es_db(), _tracker_db()
    _hold(tk, 1, "GOOGL", "equity", 14.91)  # sub-$100 fractional
    _hold(tk, 2, "BKNG", "equity", 150.0)  # over $100
    _track(es, "GOOGL", "evaluation")
    _track(es, "BKNG", "evaluation")

    plan = compute_reclassification(es_conn=es, tracker_conn=tk, min_value=100.0, user_id=USER)
    assert [p[0] for p in plan.promotions] == ["BKNG"]

    # A lower threshold pulls GOOGL in too.
    plan2 = compute_reclassification(es_conn=es, tracker_conn=tk, min_value=10.0, user_id=USER)
    assert [p[0] for p in plan2.promotions] == ["BKNG", "GOOGL"]


def test_pinned_name_kept_in_portfolio() -> None:
    es, tk = _es_db(), _tracker_db()
    _hold(tk, 1, "MELI", "cs", 88000.0)  # a real holding so the safety latch passes
    _track(es, "MELI", "portfolio")  # held → unchanged
    _track(es, "META", "portfolio")  # not held, but pinned
    plan = compute_reclassification(es_conn=es, tracker_conn=tk, pins={"META"}, user_id=USER)
    assert plan.demotions == []
    assert [p[0] for p in plan.pinned_kept] == ["META"]
    assert not plan.has_changes

    # Without the pin it demotes.
    plan2 = compute_reclassification(es_conn=es, tracker_conn=tk, pins=set(), user_id=USER)
    assert [d[0] for d in plan2.demotions] == ["META"]


def test_untracked_held_equity_reported_not_promoted() -> None:
    es, tk = _es_db(), _tracker_db()
    _hold(tk, 1, "FLKRCO", "cs", 49626.0)  # an operating company we don't track
    plan = compute_reclassification(es_conn=es, tracker_conn=tk, user_id=USER)
    assert [u[0] for u in plan.untracked_held] == ["FLKRCO"]
    assert plan.promotions == []  # never auto-added


def test_archived_rows_ignored() -> None:
    es, tk = _es_db(), _tracker_db()
    _hold(tk, 1, "BKNG", "equity", 6237.0)
    _track(es, "BKNG", "evaluation", archived=True)  # archived → not active
    plan = compute_reclassification(es_conn=es, tracker_conn=tk, user_id=USER)
    # Archived BKNG isn't "tracked" for promotion → reported as untracked-held.
    assert plan.promotions == []
    assert [u[0] for u in plan.untracked_held] == ["BKNG"]


def test_zero_holdings_is_safe_noop_never_mass_demotes() -> None:
    # The catastrophic case: tracker DB present but EMPTY (outage / failed sync).
    # A naive reconcile would see every portfolio name as "not held" and demote
    # the whole book. The safety latch must refuse to act.
    es, tk = _es_db(), _tracker_db()  # tk has zero holdings_snapshots rows
    _track(es, "MELI", "portfolio")
    _track(es, "NU", "portfolio")
    _track(es, "BKNG", "evaluation")

    plan = compute_reclassification(es_conn=es, tracker_conn=tk, user_id=USER)
    assert plan.holdings_unavailable is True
    assert plan.demotions == []  # NOT [MELI, NU] — the whole point
    assert plan.promotions == []
    assert not plan.has_changes


def test_latest_snapshot_wins() -> None:
    es, tk = _es_db(), _tracker_db()
    # Old snapshot under threshold, newest over → newest wins.
    _hold(tk, 1, "BKNG", "equity", 50.0, snapshot="2026-01-01")
    _hold(tk, 1, "BKNG", "equity", 6237.0, snapshot="2026-06-14")
    _track(es, "BKNG", "evaluation")
    plan = compute_reclassification(es_conn=es, tracker_conn=tk, user_id=USER)
    assert [p[0] for p in plan.promotions] == ["BKNG"]


def test_apply_writes_only_list_type_and_is_idempotent() -> None:
    es, tk = _es_db(), _tracker_db()
    _hold(tk, 1, "BKNG", "equity", 6237.0)
    _track(es, "BKNG", "evaluation")
    _track(es, "AMZN", "portfolio")

    plan = compute_reclassification(es_conn=es, tracker_conn=tk, user_id=USER)
    tally = apply_reclassification(es_conn=es, plan=plan, user_id=USER)
    assert tally == {"promoted": 1, "demoted": 1}

    rows = {
        r["ticker"]: (r["list_type"], r["ir_url"])
        for r in es.execute("SELECT ticker, list_type, ir_url FROM tracked_companies")
    }
    assert rows["BKNG"][0] == "portfolio"
    assert rows["AMZN"][0] == "evaluation"
    # Untouched columns preserved.
    assert rows["BKNG"][1] == "https://ir.BKNG.com"

    # Second pass: world now matches DB → no changes.
    plan2 = compute_reclassification(es_conn=es, tracker_conn=tk, user_id=USER)
    assert not plan2.has_changes


def test_load_pins_roundtrip(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "portfolio_pins.json").write_text(
        json.dumps(
            {
                "pinned_portfolio": [
                    {"ticker": "META", "reason": "conviction, no live position"},
                    {"ticker": "abc"},  # missing reason → empty string
                    {"reason": "no ticker"},  # skipped
                ]
            }
        ),
        encoding="utf-8",
    )
    pins = load_pins(tmp_path)
    assert pins == {"META": "conviction, no live position", "ABC": ""}


def test_load_pins_absent_or_malformed(tmp_path: Path) -> None:
    assert load_pins(tmp_path) == {}  # absent
    data = tmp_path / "data"
    data.mkdir()
    (data / "portfolio_pins.json").write_text("not json{", encoding="utf-8")
    assert load_pins(tmp_path) == {}  # malformed
