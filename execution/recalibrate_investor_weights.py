"""execution/recalibrate_investor_weights.py — tune 13F source weights against
realized forward returns (the Discovery rule's quarterly recalibration).

Three steps over the ``investor_calibration`` ledger (alembic 0100):

  1. **snapshot** — record every CURRENT ``investor_13f`` new/add buy
     (``discovery_signals``) at its surfacing price (the close on/after the 13F
     filing date), once. The miner full-replaces the signal class each quarter,
     so this append-only ledger is what preserves the buy for later scoring.
  2. **measure** — for ledger rows whose ``horizon_days`` have elapsed, fill the
     exit price (the close one horizon out) + the realized forward return + a
     win/loss verdict. A name with no price on file → ``no_price`` (excluded,
     not scored as a loss).
  3. **recalibrate** — per fund, compute the hit-rate over its measured
     win/loss buys; if it has at least ``MIN_OUTCOMES``, nudge its
     ``base_weight`` toward the hit-rate (gently, EWMA-damped + bounded), and
     stamp ``last_calibrated_at``. Funds with too little history are left
     untouched.

Largely a NO-OP until ≥2 quarters of buys clear the horizon — disclosed, by
construction (the snapshot must accumulate before the measure can fire). Price
data is the FMP price-chart cache via ``allocation.price_history``; the loader
is injectable so the math is unit-tested without files.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from allocation.price_history import load_daily_closes  # noqa: E402
from discovery.sources import list_sources, set_source_weight  # noqa: E402
from discovery.store import signals_by_ticker  # noqa: E402
from identity import DEFAULT_USER_ID  # noqa: E402
from user_state._db import now_iso, open_conn  # noqa: E402

#: Forward-return measurement window — ~2 quarters, long enough that a 13F
#: surfacing (already 45 days lagged) has time to play out.
HORIZON_DAYS = 180
#: A fund needs at least this many measured win/loss buys before its weight is
#: tuned — below it the hit-rate is noise.
MIN_OUTCOMES = 8
#: EWMA dampening on the weight nudge (0 = never move, 1 = jump to target).
_ALPHA = 0.3
#: Bounds on a recalibrated weight (the seed tier weights top out ~1.0).
_WEIGHT_FLOOR = 0.1
_WEIGHT_CEIL = 1.2

PriceLoader = Callable[[str], "list[tuple[date, float]]"]


def price_on_or_after(closes: list[tuple[date, float]], target: date) -> float | None:
    """The first close on/after ``target`` from an ascending (date, close)
    series, or None when the series doesn't reach that far."""
    for d, v in closes:
        if d >= target:
            return v
    return None


def _parse_date(raw: str) -> date | None:
    try:
        return date.fromisoformat(raw[:10])
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# step 1: snapshot current new buys
# ---------------------------------------------------------------------------


def snapshot_new_buys(db_path: Path, *, loader: PriceLoader, user_id: str = DEFAULT_USER_ID) -> int:
    """Record each current investor_13f new/add buy at its surfacing price,
    once (idempotent on the ledger's UNIQUE identity). Returns rows inserted."""
    by_ticker = signals_by_ticker("investor_13f", user_id=user_id, db_path=db_path)
    now = now_iso()
    inserted = 0
    conn = open_conn(db_path)
    try:
        for ticker, rows in by_ticker.items():
            closes = loader(ticker)
            for r in rows:
                obs = _parse_date(r.observed_at)
                entry = price_on_or_after(closes, obs) if obs is not None else None
                action = r.meta.get("action")
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO investor_calibration
                      (user_id, source_key, ticker, observed_at, action, entry_price,
                       entry_recorded_at, horizon_days)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        r.source_key,
                        ticker,
                        r.observed_at,
                        action if isinstance(action, str) else None,
                        entry,
                        now,
                        HORIZON_DAYS,
                    ),
                )
                inserted += cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return inserted


# ---------------------------------------------------------------------------
# step 2: measure rows whose horizon has elapsed
# ---------------------------------------------------------------------------


def measure_due(
    db_path: Path,
    *,
    loader: PriceLoader,
    as_of: date | None = None,
    user_id: str = DEFAULT_USER_ID,
) -> int:
    """Fill the exit price + realized return + verdict for every unmeasured
    ledger row past its horizon. Returns rows measured."""
    as_of = as_of or datetime.now(UTC).date()
    now = now_iso()
    measured = 0
    conn = open_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT id, ticker, observed_at, entry_price, horizon_days "
            "FROM investor_calibration WHERE outcome IS NULL AND user_id = ?",
            (user_id,),
        ).fetchall()
        for row in rows:
            obs = _parse_date(str(row["observed_at"]))
            if obs is None:
                continue
            horizon = obs + timedelta(days=int(row["horizon_days"]))
            if horizon > as_of:
                continue  # not yet due
            entry = row["entry_price"]
            exit_price = price_on_or_after(loader(str(row["ticker"])), horizon)
            if entry is None or exit_price is None or float(entry) <= 0:
                outcome, fwd = "no_price", None
            else:
                fwd = float(exit_price) / float(entry) - 1.0
                outcome = "win" if fwd > 0 else "loss" if fwd < 0 else "flat"
            conn.execute(
                "UPDATE investor_calibration SET measured_at = ?, exit_price = ?, "
                "forward_return = ?, outcome = ? WHERE id = ?",
                (now, exit_price, fwd, outcome, int(row["id"])),
            )
            measured += 1
        conn.commit()
    finally:
        conn.close()
    return measured


# ---------------------------------------------------------------------------
# step 3: recalibrate weights from the measured hit-rates
# ---------------------------------------------------------------------------


def _target_multiplier(hit_rate: float) -> float:
    """Hit-rate → a weight multiplier centered on 1.0 at a coin-flip 0.5."""
    return 0.6 + 0.8 * hit_rate


def recalibrate(
    db_path: Path,
    *,
    user_id: str = DEFAULT_USER_ID,
) -> list[dict[str, object]]:
    """Nudge each fund's base_weight toward its measured hit-rate (EWMA-damped,
    bounded). Funds below MIN_OUTCOMES are left unchanged. Returns the per-fund
    changes for the run summary."""
    conn = open_conn(db_path)
    try:
        counts = conn.execute(
            "SELECT source_key, "
            "SUM(outcome = 'win') AS wins, SUM(outcome = 'loss') AS losses "
            "FROM investor_calibration WHERE user_id = ? GROUP BY source_key",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    weights = {
        s.source_key: s.base_weight
        for s in list_sources(signal_class="investor_13f", db_path=db_path)
    }
    changes: list[dict[str, object]] = []
    for row in counts:
        key = str(row["source_key"])
        wins, losses = int(row["wins"] or 0), int(row["losses"] or 0)
        decided = wins + losses
        if decided < MIN_OUTCOMES or key not in weights:
            continue
        hit_rate = wins / decided
        old = weights[key]
        target = old * _target_multiplier(hit_rate)
        new = old + _ALPHA * (target - old)
        new = round(min(max(new, _WEIGHT_FLOOR), _WEIGHT_CEIL), 3)
        if new != round(old, 3):
            set_source_weight(key, new, db_path=db_path)
            changes.append(
                {"source_key": key, "hit_rate": round(hit_rate, 3), "from": old, "to": new}
            )
    return changes


# ---------------------------------------------------------------------------
# orchestration + CLI
# ---------------------------------------------------------------------------


def run(
    repo_root: Path,
    *,
    user_id: str = DEFAULT_USER_ID,
    loader: PriceLoader | None = None,
    as_of: date | None = None,
) -> dict[str, object]:
    """snapshot → measure → recalibrate. Degrades to a no-op summary when the
    DB / ledger table is absent (pre-0100 DB)."""
    db_path = repo_root / "data" / "portfolio.db"

    def _default_load(ticker: str) -> list[tuple[date, float]]:
        return load_daily_closes(ticker, repo_root)

    load: PriceLoader = loader or _default_load
    summary: dict[str, object] = {"snapshotted": 0, "measured": 0, "recalibrated": []}
    try:
        summary["snapshotted"] = snapshot_new_buys(db_path, loader=load, user_id=user_id)
        summary["measured"] = measure_due(db_path, loader=load, as_of=as_of, user_id=user_id)
        summary["recalibrated"] = recalibrate(db_path, user_id=user_id)
    except (sqlite3.Error, FileNotFoundError, RuntimeError):
        pass  # missing DB / pre-0100 ledger → partial-or-no-op (best-effort cron step)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else "")
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--user-id", default=DEFAULT_USER_ID)
    args = parser.parse_args(argv)
    summary = run(args.repo_root.resolve(), user_id=args.user_id)
    print(json.dumps({"event": "recalibrate_investor_weights_done", **summary}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
