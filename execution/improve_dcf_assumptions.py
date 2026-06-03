"""Improve a ticker's DCF forecast assumptions with Opus, then recompute.

The seeded model holds every driver flat at its TTM ratio — a poor starting
point. For each ticker this asks Opus (cached on disk) to NORMALIZE the per-year
Forecast drivers (growth fade, margin normalization, opex/SBC operating leverage,
capex/D&A convergence, working capital) against valuation best practice + the
thesis, writes them into `dcf/<TICKER>.xlsx`, and recomputes the valuation into
`dcf_runs` via `refresh_dcf.refresh_one`.

The Opus calls (the slow part) run in parallel; the apply + recompute (which
writes the shared DB) runs serially.

Usage:
  python execution/improve_dcf_assumptions.py --ticker AMZN
  python execution/improve_dcf_assumptions.py --all-named [--workers 4] [--force]
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import refresh_dcf  # noqa: E402

from dcf import llm_assumptions  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    repo_root = args.repo_root.resolve()
    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        print(json.dumps({"event": "no_db", "path": str(db_path)}))
        return 2
    tickers = _resolve_tickers(repo_root, args)
    if not tickers:
        print(json.dumps({"event": "no_tickers"}))
        return 0

    # Phase 1 — Opus generation in parallel (each call is independent + slow).
    generated: dict[str, llm_assumptions.LlmAssumptions] = {}
    errors: dict[str, str] = {}

    def _gen(ticker: str) -> tuple[str, llm_assumptions.LlmAssumptions | None, str | None]:
        wb = repo_root / "dcf" / f"{ticker}.xlsx"
        if not wb.exists():
            return ticker, None, f"no workbook at {wb}"
        try:
            return (
                ticker,
                llm_assumptions.generate_assumptions(ticker, wb, repo_root, force=args.force),
                None,
            )
        except Exception as e:  # one ticker's failure must not abort the batch
            return ticker, None, f"{type(e).__name__}: {e}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for ticker, assumptions, err in pool.map(_gen, tickers):
            if assumptions is not None:
                generated[ticker] = assumptions
            else:
                errors[ticker] = err or "unknown error"

    # Phase 2 — apply + recompute serially (the recompute writes the shared DB).
    results: list[dict[str, object]] = []
    for ticker in tickers:
        if ticker in errors:
            results.append({"ticker": ticker, "status": "llm_failed", "reason": errors[ticker]})
            continue
        assumptions = generated[ticker]
        wb = repo_root / "dcf" / f"{ticker}.xlsx"
        applied = llm_assumptions.apply_to_workbook(wb, assumptions)
        refresh = refresh_dcf.refresh_one(
            ticker, repo_root, db_path, valuation_year=args.valuation_year
        )
        results.append(
            {
                "ticker": ticker,
                "status": refresh.get("status"),
                "drivers_applied": applied,
                "fair_value_per_share": refresh.get("fair_value_per_share"),
                "over_under_pct": refresh.get("over_under_pct"),
                "narrative": assumptions.narrative,
            }
        )

    print(json.dumps(results, indent=2, default=str))
    return 0


def _resolve_tickers(repo_root: Path, args: argparse.Namespace) -> list[str]:
    if args.ticker:
        return [str(args.ticker).upper()]
    out: list[str] = []
    holdings_dir = repo_root / "micro_thesis" / "holdings"
    for path in sorted(holdings_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        # --all-named: holdings with a WACC (they produce a valuation) and a workbook.
        if (
            isinstance(data, dict)
            and isinstance(data.get("wacc"), (int, float))
            and (repo_root / "dcf" / f"{path.stem.upper()}.xlsx").exists()
        ):
            out.append(path.stem.upper())
    return out


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--ticker", help="Single ticker to improve")
    g.add_argument(
        "--all-named",
        action="store_true",
        help="All holdings with a WACC + an existing workbook",
    )
    p.add_argument("--workers", type=int, default=3, help="Parallel Opus calls (default 3)")
    p.add_argument(
        "--force", action="store_true", help="Re-call Opus even if a cached result exists"
    )
    p.add_argument("--valuation-year", type=int, default=date.today().year)
    p.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    return p.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
