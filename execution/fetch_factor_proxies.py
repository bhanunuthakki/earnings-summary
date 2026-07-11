"""execution/fetch_factor_proxies.py — refresh the ETF style-proxy close series.

Pulls ~2 years of dividend-adjusted daily closes for the style-factor proxy
ETFs (SPY / VTV / VUG / IWM / MTUM) PLUS any held ETF the FMP price-chart
cache doesn't cover (``factor_proxies.HELD_ETF_TICKERS`` — FLKR, VTI, VOO) from
yfinance and persists them to ``data/factor_proxies/<TICKER>.json``
(``src/factor_proxies.py`` owns the store). The Risk panel's style-factor /
correlation / Monte-Carlo sections only ever read the files, so this is the
ONLY network leg — wired as morning-pipeline stage 0g, also runnable by hand.
A failed fetch leaves the last-good file untouched (the panel then renders a
stale-but-dated series).

CLI:
    python execution/fetch_factor_proxies.py
    python execution/fetch_factor_proxies.py --period 5y
    python execution/fetch_factor_proxies.py --tickers SPY VTV
    python execution/fetch_factor_proxies.py --tickers FLKR   # one held ETF only

Exit code 0 if at least one proxy series was persisted; 1 otherwise (mirrors
``compute_macro_sensitivities``'s convention so cron treats a total outage as
a failure but a partial refresh as success).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from factor_proxies import (  # noqa: E402
    DEFAULT_PERIOD,
    HELD_ETF_TICKERS,
    PROXY_TICKERS,
    refresh_factor_proxies,
)

_DEFAULT_TICKERS: tuple[str, ...] = (*PROXY_TICKERS, *HELD_ETF_TICKERS)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="repo root whose data/factor_proxies/ receives the series",
    )
    parser.add_argument(
        "--period",
        default=DEFAULT_PERIOD,
        help=f"yfinance history period (default {DEFAULT_PERIOD})",
    )
    parser.add_argument(
        "--tickers",
        nargs="*",
        default=list(_DEFAULT_TICKERS),
        help="proxy/held ETFs to refresh (default: style proxies + held ETFs)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    counts = refresh_factor_proxies(args.repo_root, args.tickers, period=args.period)
    print(json.dumps({"persisted": counts}, sort_keys=True))
    return 0 if any(n > 0 for n in counts.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
