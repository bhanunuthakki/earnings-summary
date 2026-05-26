"""Grade pending decisions against subsequent realized price moves.

For each decision where outcome_at IS NULL and made_at is older than the
threshold (default 30 days), read the FMP price chart at made_at and at the
latest available bar, compute the % change, and write an outcome.

Grading heuristic (price-only, no fundamentals):
  ADD / INITIATE:  +5%   = correct, -5%  = wrong, in between = mixed
  TRIM / SELL:     -5%   = correct, +5%  = wrong, in between = mixed
  HOLD / AVOID:    ±5% (no big move) = correct, big move either way = mixed
                   (HOLD is wrong if the stock falls >15% — the system told
                   the user not to trim and they got hurt)

This is a coarse first pass — a richer grader (DCF rebuild, fundamental
delta) is a future-work iteration. The point here is to close the loop:
get an outcome on the record, however rough, so the LLM has SOMETHING to
calibrate against.

Usage:
    python execution/grade_decisions.py
    python execution/grade_decisions.py --since-days 60
    python execution/grade_decisions.py --threshold-pct 0.07
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def _sync_db_path(repo_root: Path) -> None:
    import db

    db.PROJECT_ROOT = str(repo_root)
    db.DATA_DIR = str(repo_root / "data")
    db.DB_PATH = str(repo_root / "data" / "portfolio.db")
    db.FMP_DIR = str(repo_root / "data" / "historical" / "fmp")


from decision_extractor import OutcomeLabel, pending_for_grading, record_outcome  # noqa: E402

log = logging.getLogger("grade_decisions")


def _load_price_series(repo_root: Path, ticker: str) -> list[tuple[str, float]] | None:
    """Read FMP dividend-adjusted price chart for ticker. Returns sorted
    (date, adjClose) list newest-first, or None when no file exists."""
    candidates = [
        repo_root / "data" / "historical" / "fmp" / f"{ticker}_price_chart_10y_div_adj.json",
        # Some tickers (GOOG) are stored under their primary symbol (GOOGL)
        repo_root / "data" / "historical" / "fmp" / f"{_fmp_alias(ticker)}_price_chart_10y_div_adj.json",
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, list):
            continue
        out: list[tuple[str, float]] = []
        for rec in cast("list[object]", payload):
            if not isinstance(rec, dict):
                continue
            r = cast("dict[str, object]", rec)
            d = r.get("date")
            p = r.get("adjClose")
            if isinstance(d, str) and isinstance(p, (int, float)) and p > 0:
                out.append((d[:10], float(p)))
        if out:
            out.sort(key=lambda t: t[0], reverse=True)
            return out
    return None


# Ticker → FMP filename prefix for known sister tickers (matches db._FMP_ALIASES).
_ALIASES = {"GOOG": "GOOGL"}


def _fmp_alias(ticker: str) -> str:
    return _ALIASES.get(ticker.upper(), ticker.upper())


def _price_on_or_before(series: list[tuple[str, float]], iso_date: str) -> float | None:
    """First adjClose at or before the iso_date (YYYY-MM-DD). Series is
    newest-first, so we walk forward until we hit it."""
    for d, p in series:
        if d <= iso_date:
            return p
    return None


def _verdict(
    *, kind: str, pct_change: float, threshold_pct: float
) -> tuple[str, str]:
    """Map (recommendation_kind, realized_pct_change) → (outcome_label, notes).
    Threshold is the symmetric move that flips correct vs wrong."""
    big_move = abs(pct_change) > threshold_pct
    direction = "up" if pct_change >= 0 else "down"

    if kind == "add" or kind == "initiate":
        if pct_change >= threshold_pct:
            return ("correct", f"price moved +{pct_change * 100:.1f}% — ADD captured upside")
        if pct_change <= -threshold_pct:
            return ("wrong", f"price moved {pct_change * 100:.1f}% — ADD into drawdown")
        return ("mixed", f"price moved {pct_change * 100:.1f}% — flat band, neither vindicated nor refuted")

    if kind == "trim" or kind == "sell":
        if pct_change <= -threshold_pct:
            return ("correct", f"price moved {pct_change * 100:.1f}% — TRIM/SELL avoided drawdown")
        if pct_change >= threshold_pct:
            return ("wrong", f"price moved +{pct_change * 100:.1f}% — TRIM/SELL gave up upside")
        return ("mixed", f"price moved {pct_change * 100:.1f}% — flat band, neither vindicated nor refuted")

    if kind == "hold" or kind == "avoid":
        if not big_move:
            return ("correct", f"price moved {pct_change * 100:.1f}% — HOLD/AVOID was right to not act")
        # Holding through a big drawdown is wrong; holding through a big rally is mixed
        # (could have added more)
        if pct_change <= -threshold_pct * 3:  # 15% drawdown threshold for HOLD-wrong
            return ("wrong", f"price moved {pct_change * 100:.1f}% — HOLD through significant drawdown")
        return ("mixed", f"price moved {direction} {abs(pct_change * 100):.1f}% — HOLD missed an action signal")

    return ("unfalsifiable", f"unknown kind={kind}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since-days",
        type=int,
        default=30,
        help="Grade decisions made at least this many days ago.",
    )
    parser.add_argument(
        "--threshold-pct",
        type=float,
        default=0.05,
        help="Symmetric price-move threshold (fraction) that separates correct/wrong/mixed.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root containing data/portfolio.db.",
    )
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    _sync_db_path(args.repo_root.resolve())

    db_path = args.repo_root / "data" / "portfolio.db"
    pending = pending_for_grading(
        older_than_days=args.since_days, limit=args.limit, db_path=db_path
    )
    log.info({"event": "pending_decisions", "n": len(pending)})

    tally = {"graded": 0, "correct": 0, "wrong": 0, "mixed": 0, "unfalsifiable": 0, "skipped_no_price": 0}
    today_iso = datetime.now(UTC).date().isoformat()

    for dec in pending:
        series = _load_price_series(args.repo_root, dec.ticker)
        if series is None:
            tally["skipped_no_price"] += 1
            continue
        made_iso = dec.made_at.date().isoformat()
        # Reference price on/before made_at
        ref_price = _price_on_or_before(series, made_iso)
        # Outcome price = today (or latest available)
        cur_price = _price_on_or_before(series, today_iso)
        if ref_price is None or cur_price is None or ref_price <= 0:
            tally["skipped_no_price"] += 1
            continue

        pct_change = (cur_price - ref_price) / ref_price
        label, notes = _verdict(
            kind=dec.recommendation_kind,
            pct_change=pct_change,
            threshold_pct=args.threshold_pct,
        )
        # Don't grade if the price observation is from the same calendar day as
        # the recommendation — there's no realized move to measure.
        if made_iso == today_iso:
            continue

        ok = record_outcome(
            decision_id=dec.id,
            outcome_label=cast("OutcomeLabel", label),
            outcome_pct=pct_change,
            outcome_notes=notes,
            outcome_at=datetime.now(UTC),
            db_path=db_path,
        )
        if ok:
            tally["graded"] += 1
            tally[label] = tally.get(label, 0) + 1

    # Show summary line even when zero — the user runs this to confirm the
    # pipeline works against fresh artifacts.
    print(
        "Decision grading complete · "
        f"pending={len(pending)} · graded={tally['graded']} · "
        f"correct={tally['correct']} · wrong={tally['wrong']} · "
        f"mixed={tally['mixed']} · skipped_no_price={tally['skipped_no_price']}"
    )
    if len(pending) == 0:
        # Most likely cause: artifacts younger than --since-days. Show the
        # threshold so the operator can re-run with a longer window.
        cutoff = (datetime.now(UTC) - timedelta(days=args.since_days)).date().isoformat()
        print(f"  (no decisions made on or before {cutoff} are awaiting grading)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
