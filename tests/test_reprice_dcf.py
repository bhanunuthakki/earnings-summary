"""L6 — DCF price-leg re-price (src/dcf/reprice.py).

``refresh_dcf`` recomputes both legs of ``over_under_pct = (live - fair) / fair``
but is opt-in with no cron, so the price leg freezes at the last rebuild while
the market moves. ``reprice_runs`` re-divides the *persisted* fair value by a
*fresh* live price and rewrites only ``live_price`` / ``live_price_at`` /
``over_under_pct`` — never re-running the DCF.

What's pinned here:
  * the price leg is rewritten and over/under re-derived through the canonical
    ``persist.derive_over_under`` (identical to a full refresh's price leg);
  * the fair-value leg (``npv_per_share`` / ``valuation_date`` / snapshot) is
    never touched;
  * the rewrite lands against the real migration-0076 CHECK table (all three
    columns move together, so the row stays self-consistent — an inconsistent
    write would be rejected by the DB);
  * no live price → the stale row is left intact (degrade to last-known, not
    blanked); a non-positive fair value (the #291 case) is skipped;
  * the downstream consumers (trim/sell ladder reads ``over_under_pct``; the
    cockpit / next-dollar "ret" factor recompute ``live_price / npv − 1``) now
    read the re-priced values.

The price source is injected — no network — via a fake ``read_live_price``.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from dcf import persist  # noqa: E402
from dcf.reprice import PriceReader, reprice_runs  # noqa: E402
from sources.price import LivePrice  # noqa: E402

# The production table post-0076: the named CHECK is the migration's _RATIO_CHECK
# verbatim, so the UPDATE under test is validated against the same predicate prod
# enforces (a price-leg rewrite that desynced the columns would raise here).
_DCF_RUNS_SCHEMA = """
CREATE TABLE dcf_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT UNIQUE,
    valuation_date TEXT, horizon_years INTEGER,
    wacc REAL, terminal_growth REAL,
    npv REAL, npv_per_share REAL, shares_outstanding REAL,
    currency TEXT, notes TEXT, run_id TEXT,
    live_price REAL, live_price_at TEXT, over_under_pct REAL,
    mos_bar_used REAL, assumption_snapshot_json TEXT,
    revenue_growths_json TEXT, fcf_margin REAL,
    created_at TEXT,
    CONSTRAINT ck_dcf_runs_over_under_ratio CHECK (
        over_under_pct IS NULL
        OR live_price IS NULL OR npv_per_share IS NULL
        OR live_price <= 0 OR npv_per_share <= 0
        OR ABS(over_under_pct - (1.0 * live_price / npv_per_share - 1.0)) <= 0.005
    )
);
"""

_FRESH_AT = datetime(2026, 6, 13, 13, 30, tzinfo=UTC)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_DCF_RUNS_SCHEMA)
    return conn


def _seed(
    conn: sqlite3.Connection,
    ticker: str,
    *,
    npv_per_share: float,
    live_price: float | None,
    valuation_date: date = date(2026, 5, 1),
) -> None:
    """Seed one row through the real persist chokepoint (so over_under starts on
    the canonical convention, exactly as a prior refresh would have left it)."""
    persist.upsert(
        conn,
        persist.DcfRunRow(
            ticker=ticker,
            valuation_date=valuation_date,
            horizon_years=5,
            wacc=0.10,
            npv=1000.0,
            npv_per_share=npv_per_share,
            shares_outstanding=5.0e7,
            currency="USD",
            live_price=live_price,
            live_price_at=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
            mos_bar_used=None,
            assumption_snapshot_json='{"format": "redesign"}',
        ),
    )


def _fixed_reader(prices: dict[str, float]) -> PriceReader:
    """A read_live_price stand-in: ticker -> price, or None when absent."""

    def reader(_repo_root: Path, ticker: str) -> LivePrice | None:
        px = prices.get(ticker.upper())
        if px is None:
            return None
        return LivePrice(price=px, fetched_at=_FRESH_AT, source_name="fake")

    return reader


def _read(conn: sqlite3.Connection, ticker: str) -> sqlite3.Row:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT valuation_date, npv_per_share, live_price, live_price_at, over_under_pct,"
        " assumption_snapshot_json FROM dcf_runs WHERE ticker = ?",
        (ticker,),
    ).fetchone()
    assert row is not None
    return row


# --------------------------------------------------------------------------- #
# Re-price rewrites the price leg, leaves the fair-value leg untouched.
# --------------------------------------------------------------------------- #
def test_reprice_rewrites_price_leg_and_rederives_over_under(tmp_path: Path) -> None:
    conn = _conn()
    # Seeded deeply under fair value: stored over_under = (10 - 20)/20 = -0.5.
    _seed(conn, "ZZT", npv_per_share=20.0, live_price=10.0)
    assert _read(conn, "ZZT")["over_under_pct"] == pytest.approx(-0.5)

    # The market re-rates the name to $24 — now ABOVE fair value.
    results = reprice_runs(conn, tmp_path, price_reader=_fixed_reader({"ZZT": 24.0}))

    assert [r.status for r in results] == ["repriced"]
    assert results[0].over_under_pct == pytest.approx(0.2)

    row = _read(conn, "ZZT")
    # Price leg rewritten + over/under re-derived to (24 - 20)/20 = +0.2 (the
    # trim band: a sign-and-band flip the stale value hid).
    assert row["live_price"] == pytest.approx(24.0)
    assert row["live_price_at"] == _FRESH_AT.isoformat()
    assert row["over_under_pct"] == pytest.approx(0.2)
    # ...identical to what a full refresh_dcf would persist for the price leg.
    assert row["over_under_pct"] == pytest.approx(persist.derive_over_under(24.0, 20.0))
    # Fair-value leg untouched.
    assert row["npv_per_share"] == pytest.approx(20.0)
    assert row["valuation_date"] == "2026-05-01"
    assert row["assumption_snapshot_json"] == '{"format": "redesign"}'


def test_reprice_keeps_row_consistent_with_0076_check(tmp_path: Path) -> None:
    """The UPDATE lands against the live CHECK; the post-write row satisfies the
    ratio predicate (a desynced price-leg rewrite would raise IntegrityError)."""
    conn = _conn()
    _seed(conn, "ZZT", npv_per_share=50.0, live_price=50.0)
    reprice_runs(conn, tmp_path, price_reader=_fixed_reader({"ZZT": 61.0}))
    row = _read(conn, "ZZT")
    assert row["over_under_pct"] == pytest.approx(row["live_price"] / row["npv_per_share"] - 1.0)


# --------------------------------------------------------------------------- #
# Degrade paths: no price (leave stale), non-positive fair value (skip).
# --------------------------------------------------------------------------- #
def test_no_live_price_leaves_row_untouched(tmp_path: Path) -> None:
    conn = _conn()
    _seed(conn, "NOPX", npv_per_share=20.0, live_price=10.0)
    before = dict(_read(conn, "NOPX"))

    results = reprice_runs(conn, tmp_path, price_reader=_fixed_reader({}))  # reader misses

    assert [r.status for r in results] == ["no_price"]
    after = dict(_read(conn, "NOPX"))
    # Last-known price leg preserved rather than blanked.
    assert after["live_price"] == before["live_price"]
    assert after["over_under_pct"] == before["over_under_pct"]
    assert after["live_price_at"] == before["live_price_at"]


def test_non_positive_fair_value_is_skipped(tmp_path: Path) -> None:
    conn = _conn()
    # The #291 case: a non-positive fair value -> over/under is undefined (NULL).
    _seed(conn, "NEGFV", npv_per_share=-3.0, live_price=10.0)
    assert _read(conn, "NEGFV")["over_under_pct"] is None

    results = reprice_runs(conn, tmp_path, price_reader=_fixed_reader({"NEGFV": 24.0}))

    assert [r.status for r in results] == ["no_fair_value"]
    row = _read(conn, "NEGFV")
    assert row["over_under_pct"] is None
    assert row["live_price"] == pytest.approx(10.0)  # untouched


# --------------------------------------------------------------------------- #
# Scoping + multi-ticker.
# --------------------------------------------------------------------------- #
def test_tickers_scope_limits_the_sweep(tmp_path: Path) -> None:
    conn = _conn()
    _seed(conn, "AAA", npv_per_share=20.0, live_price=10.0)
    _seed(conn, "BBB", npv_per_share=20.0, live_price=10.0)

    results = reprice_runs(
        conn, tmp_path, price_reader=_fixed_reader({"AAA": 24.0, "BBB": 24.0}), tickers=["aaa"]
    )

    assert [r.ticker for r in results] == ["AAA"]
    assert _read(conn, "AAA")["over_under_pct"] == pytest.approx(0.2)  # re-priced
    assert _read(conn, "BBB")["over_under_pct"] == pytest.approx(-0.5)  # left alone


def test_empty_or_missing_table_degrades(tmp_path: Path) -> None:
    # No dcf_runs rows -> empty result, no error.
    conn = _conn()
    assert reprice_runs(conn, tmp_path, price_reader=_fixed_reader({"X": 1.0})) == []
    conn.close()
    # No dcf_runs table at all -> degrade to [] rather than raise.
    bare = sqlite3.connect(":memory:")
    assert reprice_runs(bare, tmp_path, price_reader=_fixed_reader({"X": 1.0})) == []
    bare.close()


# --------------------------------------------------------------------------- #
# Downstream consumers read the re-priced value.
# --------------------------------------------------------------------------- #
def test_downstream_consumers_read_repriced_value(tmp_path: Path) -> None:
    """The trim/sell ladder reads the stored ``over_under_pct``; the cockpit /
    next-dollar "ret" factor recompute ``live_price / npv − 1`` from the row's
    own fields. Both now reflect the fresh price (the headline L6 contract)."""
    from pipeline.research_cockpit import latest_dcf_runs

    conn = _conn()
    _seed(conn, "ZZT", npv_per_share=20.0, live_price=10.0)
    reprice_runs(conn, tmp_path, price_reader=_fixed_reader({"ZZT": 24.0}))

    # 1) Ladder consumer: the stored column the analytical-dashboard SQL reads.
    stored = _read(conn, "ZZT")["over_under_pct"]
    assert stored == pytest.approx(0.2)  # > 0.10 -> 'trim' band (was deeply 'add')

    # 2) Cockpit / allocation consumer: the gap recomputed from the row's own
    #    live_price + npv now uses the fresh $24, not the frozen $10.
    gap, fv, px, _vd = latest_dcf_runs(conn)["ZZT"]
    assert px == pytest.approx(24.0)
    assert fv == pytest.approx(20.0)
    assert gap == pytest.approx((24.0 / 20.0 - 1.0) * 100.0)  # +20%


# --------------------------------------------------------------------------- #
# Migration 0137 (is_latest) — reprice must not re-scan superseded history.
# --------------------------------------------------------------------------- #
# A minimal versioned schema: real dcf_runs (migration 0137+) drops the
# UNIQUE(ticker) index (multiple rows per ticker survive as history) and adds
# is_latest / superseded_at / superseded_by_id. This mirrors just enough of
# that shape to exercise reprice's is_latest filter.
_VERSIONED_SCHEMA = """
CREATE TABLE dcf_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT,
    valuation_date TEXT, horizon_years INTEGER,
    wacc REAL, terminal_growth REAL,
    npv REAL, npv_per_share REAL, shares_outstanding REAL,
    currency TEXT, notes TEXT, run_id TEXT,
    live_price REAL, live_price_at TEXT, over_under_pct REAL,
    mos_bar_used REAL, assumption_snapshot_json TEXT,
    revenue_growths_json TEXT, fcf_margin REAL,
    created_at TEXT,
    is_latest INTEGER NOT NULL DEFAULT 1,
    superseded_at TEXT, superseded_by_id INTEGER,
    CONSTRAINT ck_dcf_runs_over_under_ratio CHECK (
        over_under_pct IS NULL
        OR live_price IS NULL OR npv_per_share IS NULL
        OR live_price <= 0 OR npv_per_share <= 0
        OR ABS(over_under_pct - (1.0 * live_price / npv_per_share - 1.0)) <= 0.005
    )
);
"""


def _versioned_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_VERSIONED_SCHEMA)
    return conn


def _insert_run(
    conn: sqlite3.Connection,
    ticker: str,
    *,
    npv_per_share: float,
    live_price: float | None,
    is_latest: bool,
) -> int:
    cur = conn.execute(
        "INSERT INTO dcf_runs (ticker, valuation_date, npv_per_share, live_price, "
        "over_under_pct, is_latest) VALUES (?, ?, ?, ?, ?, ?)",
        (
            ticker,
            "2026-05-01",
            npv_per_share,
            live_price,
            persist.derive_over_under(live_price, npv_per_share),
            1 if is_latest else 0,
        ),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def test_reprice_only_touches_is_latest_rows_on_versioned_schema(tmp_path: Path) -> None:
    """A superseded (is_latest=0) row must be left completely untouched — both
    to avoid re-pricing stale history and, per the reprice.py docstring, to
    keep the sweep's cost bounded to the current book rather than growing with
    every accumulated superseded version."""
    conn = _versioned_conn()
    _insert_run(conn, "ZZT", npv_per_share=20.0, live_price=10.0, is_latest=False)
    current_id = _insert_run(conn, "ZZT", npv_per_share=25.0, live_price=10.0, is_latest=True)

    results = reprice_runs(conn, tmp_path, price_reader=_fixed_reader({"ZZT": 30.0}))

    assert [r.ticker for r in results] == ["ZZT"]  # one result, not two
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, live_price, over_under_pct FROM dcf_runs ORDER BY id"
    ).fetchall()
    superseded, current = rows
    assert superseded["live_price"] == pytest.approx(10.0)  # untouched
    assert current["id"] == current_id
    assert current["live_price"] == pytest.approx(30.0)  # repriced
    assert current["over_under_pct"] == pytest.approx((30.0 - 25.0) / 25.0)


def test_reprice_falls_back_to_max_id_on_pre_0137_schema(tmp_path: Path) -> None:
    """No is_latest column at all (pre-migration-0137 DB) -> the MAX(id)-per-
    ticker fallback still picks the single legacy row (UNIQUE(ticker) means
    there is only ever one)."""
    conn = _conn()  # the module-level pre-0137 fixture (no is_latest column)
    _seed(conn, "ZZT", npv_per_share=20.0, live_price=10.0)

    results = reprice_runs(conn, tmp_path, price_reader=_fixed_reader({"ZZT": 24.0}))

    assert [r.status for r in results] == ["repriced"]


# --------------------------------------------------------------------------- #
# Per-ticker timeout bounding — a hung price fetch must not hang the sweep.
# --------------------------------------------------------------------------- #


def test_hung_ticker_does_not_block_the_others(tmp_path: Path) -> None:
    """yfinance exposes no request-level timeout, so a stalled socket could
    hang forever; reprice_runs must bound each ticker's fetch and move on. A
    ticker that blows its budget degrades to "no_price" (stale row untouched)
    without preventing the other tickers from being repriced, and the whole
    call must return in well under "hang forever"."""
    conn = _conn()
    _seed(conn, "AAA", npv_per_share=20.0, live_price=10.0)
    _seed(conn, "HUNG", npv_per_share=20.0, live_price=10.0)
    _seed(conn, "BBB", npv_per_share=20.0, live_price=10.0)

    def reader(_repo_root: Path, ticker: str) -> LivePrice | None:
        if ticker.upper() == "HUNG":
            time.sleep(5.0)  # longer than the timeout budget below
            return LivePrice(price=999.0, fetched_at=_FRESH_AT, source_name="fake")
        return LivePrice(price=24.0, fetched_at=_FRESH_AT, source_name="fake")

    t0 = time.monotonic()
    results = reprice_runs(
        conn, tmp_path, price_reader=reader, price_fetch_workers=3, per_ticker_timeout_s=0.2
    )
    elapsed = time.monotonic() - t0

    by_ticker = {r.ticker: r for r in results}
    assert by_ticker["AAA"].status == "repriced"
    assert by_ticker["BBB"].status == "repriced"
    assert by_ticker["HUNG"].status == "no_price"  # degraded, not the fake 999.0 price
    assert _read(conn, "HUNG")["live_price"] == pytest.approx(10.0)  # stale row untouched
    assert elapsed < 4.0  # bounded by the 0.2s budget, not the 5s sleep


def test_progress_events_cover_start_each_ticker_and_done(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = _conn()
    _seed(conn, "AAA", npv_per_share=20.0, live_price=10.0)
    _seed(conn, "BBB", npv_per_share=20.0, live_price=10.0)

    reprice_runs(conn, tmp_path, price_reader=_fixed_reader({"AAA": 24.0, "BBB": 24.0}))

    err = capsys.readouterr().err
    assert '"event": "reprice_start"' in err
    assert '"event": "reprice_done"' in err
    assert err.count('"event": "reprice_ticker_done"') == 2
