"""Show prompt-calibration scores grouped by (purpose, prompt_version).

Reads ``data/portfolio.db.prompt_calibration_scores`` (populated by the
graders in ``execution/grade_bear_cases.py`` + ``execution/grade_decisions.py``
via ``src/llm/calibration.py::record_score``). Aggregates with
``summarize_by_prompt_version`` and prints a per-(purpose, version) table
so the analyst can answer "is the v3 bear_case prompt actually better
than v2?" without spelunking the per-call ledger.

Default window: last 30 days. Pass ``--window-days 0`` for all time.

Usage:
    python execution/show_prompt_calibration.py
    python execution/show_prompt_calibration.py --window-days 7
    python execution/show_prompt_calibration.py --purpose bear_case
    python execution/show_prompt_calibration.py --ticker META --json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from llm.calibration import VersionSummary, summarize_by_prompt_version  # noqa: E402

EMPTY_HINT = (
    "No calibration scores recorded yet. Run "
    "`python execution/grade_bear_cases.py` or "
    "`python execution/grade_decisions.py` to populate."
)


def _load_summaries(
    *,
    db_path: Path,
    window_days: int,
    purpose: str | None,
    ticker: str | None,
) -> list[VersionSummary]:
    since: datetime | None = None
    if window_days > 0:
        # Stored timestamps are naive UTC (record_score uses utcnow().isoformat()),
        # so compare with a naive cutoff to keep the SQL string-comparison correct.
        since = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=window_days)
    return summarize_by_prompt_version(
        db_path=db_path,
        purpose=purpose,
        ticker=ticker,
        since=since,
    )


def _fmt_score(v: float) -> str:
    return f"{v:.3f}"


def _fmt_ts(ts: str | None) -> str:
    if not ts:
        return "-"
    return ts[:19].replace("T", " ")


def _render_human(summaries: list[VersionSummary], window_label: str) -> str:
    if not summaries:
        return EMPTY_HINT
    out: list[str] = []
    out.append(f"=== PROMPT CALIBRATION - {window_label} ===")
    out.append("")
    header = (
        f"  {'purpose':<22s} {'version':<10s} {'n':>5s} "
        f"{'avg':>7s} {'p25':>7s} {'p50':>7s} {'p75':>7s} {'last_scored_at':<19s}"
    )
    out.append(header)
    out.append("  " + "-" * (len(header) - 2))
    for s in summaries:
        out.append(
            f"  {s.purpose:<22s} {s.prompt_version:<10s} {s.score_count:>5d} "
            f"{_fmt_score(s.avg_score):>7s} "
            f"{_fmt_score(s.p25):>7s} {_fmt_score(s.p50):>7s} {_fmt_score(s.p75):>7s} "
            f"{_fmt_ts(s.last_scored_at):<19s}"
        )
    return "\n".join(out)


def _to_dict(s: VersionSummary) -> dict[str, object]:
    return {
        "purpose": s.purpose,
        "prompt_version": s.prompt_version,
        "n_runs": s.score_count,
        "avg_score": s.avg_score,
        "p25": s.p25,
        "p50": s.p50,
        "p75": s.p75,
        "min_score": s.min_score,
        "max_score": s.max_score,
        "last_scored_at": s.last_scored_at,
    }


def _window_label(window_days: int) -> str:
    if window_days <= 0:
        return "all time"
    return f"last {window_days} day(s)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--window-days",
        type=int,
        default=30,
        help="Days to look back (0 = all time). Default: 30.",
    )
    parser.add_argument("--purpose", default=None, help="Filter to one purpose (e.g. bear_case)")
    parser.add_argument("--ticker", default=None, help="Filter to one ticker (uppercased)")
    parser.add_argument(
        "--db",
        default=str(PROJECT_ROOT / "data" / "portfolio.db"),
        help="Path to portfolio.db (default: this project's DB)",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of formatted text")
    args = parser.parse_args(argv)

    ticker = args.ticker.upper() if args.ticker else None
    summaries = _load_summaries(
        db_path=Path(args.db),
        window_days=args.window_days,
        purpose=args.purpose,
        ticker=ticker,
    )

    if args.json:
        print(
            json.dumps(
                {
                    "window_days": args.window_days,
                    "purpose": args.purpose,
                    "ticker": ticker,
                    "summaries": [_to_dict(s) for s in summaries],
                },
                indent=2,
                default=str,
            )
        )
    else:
        print(_render_human(summaries, _window_label(args.window_days)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
