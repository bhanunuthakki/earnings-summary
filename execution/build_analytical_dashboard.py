"""Build the analytical portfolio dashboard HTML (static export).

Reads cross-ticker data from the unified data model and writes a single
self-contained HTML file showing the trigger ladder, insider activity,
predictions outcomes, decisions ledger, and LLM spend/budget panels.

This CLI is now a thin static-export shim: the data layer lives in
``pipeline.analytical_dashboard`` and the render layer in
``pipeline.analytical_dashboard_html`` -- the SAME modules the live command
center (``execution/comments_server.py`` GET /analytical and
GET /api/overview) consumes, so the static file and the live page never
diverge. (ASCII-only: this docstring is argparse's --help description and the
Windows console is cp1252.)

Output: output/dashboard/<date>_portfolio_dashboard.html

Usage:
    python execution/build_analytical_dashboard.py
    python execution/build_analytical_dashboard.py --insider-window-days 30
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pipeline.analytical_dashboard import build_analytical_dashboard  # noqa: E402
from pipeline.analytical_dashboard_html import render_html  # noqa: E402
from pipeline.tier_runner import tier_coverage_summary  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root", type=Path, default=PROJECT_ROOT, help="Repo root with data/portfolio.db."
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output HTML path. Default: output/dashboard/<date>_portfolio_dashboard.html.",
    )
    parser.add_argument("--insider-window-days", type=int, default=90)
    parser.add_argument("--insider-top-n", type=int, default=25)
    args = parser.parse_args()

    db_path = args.repo_root / "data" / "portfolio.db"
    dash = build_analytical_dashboard(
        db_path,
        insider_window_days=args.insider_window_days,
        insider_top_n=args.insider_top_n,
    )
    coverage = tier_coverage_summary(args.repo_root)
    html = render_html(dash, generated_at=datetime.now(UTC), tier_coverage=coverage)

    if args.out is None:
        out_dir = args.repo_root / "output" / "dashboard"
        out_dir.mkdir(parents=True, exist_ok=True)
        args.out = out_dir / f"{datetime.now(UTC).strftime('%Y-%m-%d')}_portfolio_dashboard.html"

    args.out.write_text(html, encoding="utf-8")
    print(f"Wrote {args.out}")
    print(
        f"  trigger_ladder={len(dash.trigger_ladder)} insider_events={len(dash.insider_events)} "
        f"prediction_outcomes={len(dash.prediction_outcomes)} "
        f"decisions={len(dash.decisions.recent)} "
        f"llm_budgets={len(dash.llm_budgets.rows)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
