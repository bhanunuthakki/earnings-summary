"""execution/compute_macro_sensitivities.py — regress ticker returns on macros.

For each ticker × series_id pair, downloads the most recent N years of
ticker prices (from the existing FMP price chart JSON, since that's already
on disk) plus the macro_series rows, downsamples both to weekly closes, and
fits an OLS regression of ticker log-returns on macro log-returns. The
resulting (beta, r^2, n_obs) tuple is upserted into macro_sensitivities.

The math lives in src/macro_store.compute_sensitivities — this script is
just wiring (load prices, load series, persist).

CLI:
    python execution/compute_macro_sensitivities.py --ticker AMZN
    python execution/compute_macro_sensitivities.py --portfolio
    python execution/compute_macro_sensitivities.py --all
    python execution/compute_macro_sensitivities.py --ticker AMZN --lookback-days 504

Default lookback is 252 trading days (~1 year). Pass `--lookback-days 756`
to also write a 3-year row (the unique constraint keys on lookback so the
two rows coexist).

Exit code 0 if at least one (ticker, series) pair was persisted; 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Any, cast

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from macro_series import REGISTRY  # noqa: E402
from macro_store import (  # noqa: E402
    compute_sensitivities,
    fetch_series,
    upsert_sensitivity,
)

log = logging.getLogger("compute_macro_sensitivities")

DEFAULT_LOOKBACK = 252


def _sync_db_path(repo_root: Path) -> None:
    import db

    db.PROJECT_ROOT = str(repo_root)
    db.DATA_DIR = str(repo_root / "data")
    db.DB_PATH = str(repo_root / "data" / "portfolio.db")
    db.FMP_DIR = str(repo_root / "data" / "historical" / "fmp")


def _load_ticker_prices(ticker: str, repo_root: Path) -> list[tuple[date, float]]:
    """Read the most recent FMP price-chart JSON for the ticker. Returns
    ascending date-sorted (date, adj_close) tuples."""
    fmp_dir = repo_root / "data" / "historical" / "fmp"
    if not fmp_dir.exists():
        return []
    upper = ticker.upper()
    candidates = list(fmp_dir.glob(f"{upper}_*price_chart*.json"))
    candidates.extend(fmp_dir.glob(f"{upper}L_*price_chart*.json"))  # GOOG ↔ GOOGL
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        rows: list[dict[str, object]] = []
        if isinstance(data, list):
            raw_list = cast("list[object]", data)
            rows = [cast("dict[str, object]", r) for r in raw_list if isinstance(r, dict)]
        elif isinstance(data, dict):
            inner = cast("dict[str, object]", data).get("historical")
            if isinstance(inner, list):
                inner_list = cast("list[object]", inner)
                rows = [cast("dict[str, object]", r) for r in inner_list if isinstance(r, dict)]
        out: list[tuple[date, float]] = []
        for r in rows:
            d_raw = r.get("date")
            v_raw = (
                r.get("adjClose") if "adjClose" in r else r.get("close") if "close" in r else None
            )
            if not isinstance(d_raw, str) or v_raw is None:
                continue
            try:
                d = date.fromisoformat(d_raw[:10])
                v = float(cast("Any", v_raw))
            except (ValueError, TypeError):
                continue
            if v <= 0:
                continue
            out.append((d, v))
        if out:
            out.sort(key=lambda t: t[0])
            return out
    return []


def _load_macro_series(series_id: str, lookback_days: int) -> list[tuple[date, float]]:
    pts = fetch_series(series_id=series_id, lookback_days=lookback_days * 2)
    pts_sorted = sorted(pts, key=lambda p: p.rate_date)
    return [(p.rate_date, p.value) for p in pts_sorted]


def _select_tickers(args: argparse.Namespace) -> list[str]:
    if args.ticker:
        return [args.ticker.upper()]
    import db

    companies = db.get_tracked_companies()
    if args.portfolio:
        return [str(c["ticker"]) for c in companies if c["list_type"] == "portfolio"]
    if args.all:
        return [str(c["ticker"]) for c in companies if c["list_type"] in db.ACTIVE_LIST_TYPES]
    return []


def _run_one_ticker(
    ticker: str, *, lookback_days: int, repo_root: Path, dry_run: bool
) -> dict[str, tuple[float, float, int]]:
    prices = _load_ticker_prices(ticker, repo_root)
    if not prices:
        log.warning(
            {"event": "macro_sens_no_prices", "ticker": ticker, "lookback_days": lookback_days}
        )
        return {}
    series_data: dict[str, list[tuple[date, float]]] = {}
    for sid in REGISTRY.keys():
        rows = _load_macro_series(sid, lookback_days)
        if rows:
            series_data[sid] = rows
    if not series_data:
        log.warning(
            {"event": "macro_sens_no_series", "ticker": ticker, "lookback_days": lookback_days}
        )
        return {}
    results = compute_sensitivities(
        ticker_prices=prices,
        series_lookups=series_data,
        lookback_days=lookback_days,
    )
    if not results or dry_run:
        return results
    for sid, (beta, r_sq, _n) in results.items():
        upsert_sensitivity(
            ticker=ticker,
            series_id=sid,
            beta=beta,
            r_squared=r_sq,
            lookback_window_days=lookback_days,
        )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ticker", help="Single ticker (e.g. AMZN).")
    group.add_argument("--portfolio", action="store_true", help="All portfolio holdings.")
    group.add_argument("--all", action="store_true", help="All active-universe tickers.")
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=DEFAULT_LOOKBACK,
        help=f"Trading days of weekly-returns history. Default {DEFAULT_LOOKBACK}.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Compute but don't persist.")
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    repo_root = args.repo_root.resolve()
    _sync_db_path(repo_root)

    tickers = _select_tickers(args)
    if not tickers:
        print("No tickers selected.", file=sys.stderr)
        return 2

    summary: dict[str, dict[str, list[float]]] = {}
    total_pairs = 0
    for t in tickers:
        results = _run_one_ticker(
            t, lookback_days=args.lookback_days, repo_root=repo_root, dry_run=args.dry_run
        )
        if results:
            total_pairs += len(results)
            summary[t] = {
                sid: [round(beta, 4), round(r_sq, 4), float(n)]
                for sid, (beta, r_sq, n) in results.items()
            }
        else:
            summary[t] = {}
    print(
        json.dumps(
            {
                "lookback_days": args.lookback_days,
                "tickers_processed": len(tickers),
                "pairs_persisted": total_pairs,
                "dry_run": args.dry_run,
                "per_ticker": summary,
            },
            indent=2,
        )
    )
    return 0 if total_pairs > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
