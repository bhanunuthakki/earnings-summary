"""Show a per-source operational summary of external data-source calls.

The read-side consumer for the `source_calls` provenance log (the follow-up the
`sources.registry` docstring promised). Surfaces, per (source, kind): call
volume, the cache-skip rate (deliberate non-calls — a cache hit), the error
rate (network / rate-limit / tier-restricted), and latency percentiles — so
external-fetch volume and cache effectiveness are measurable rather than buried
in a write-only table.

Usage:
    python execution/show_source_calls.py                 # all-time
    python execution/show_source_calls.py --since 2026-05-01
    python execution/show_source_calls.py --db-path /tmp/x.db
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sources.registry import SourceCallSummary, summarize_source_calls  # noqa: E402


def _fmt_pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def _fmt_ms(x: int | None) -> str:
    return "-" if x is None else f"{x}ms"


def _render(rows: list[SourceCallSummary]) -> str:
    if not rows:
        return (
            "No source-call rows yet. Adapters in src/sources/ log to source_calls "
            "on every fetch attempt; the table fills as the daily jobs run."
        )
    header = (
        f"{'source':<16} {'kind':<22} {'calls':>6} {'ok':>5} {'skip%':>6} "
        f"{'err%':>5} {'p50':>7} {'p95':>7} {'records':>8}"
    )
    lines = [header, "-" * len(header)]
    for r in rows:
        lines.append(
            f"{r.source_name:<16} {r.kind:<22} {r.total:>6} {r.ok:>5} "
            f"{_fmt_pct(r.cache_skip_rate):>6} {_fmt_pct(r.error_rate):>5} "
            f"{_fmt_ms(r.p50_latency_ms):>7} {_fmt_ms(r.p95_latency_ms):>7} "
            f"{r.total_records:>8}"
        )
    total_calls = sum(r.total for r in rows)
    total_skips = sum(r.skipped for r in rows)
    total_errs = sum(r.errors for r in rows)
    total_cost_saved = sum(r.cost_saved_usd for r in rows)
    lines.append("-" * len(header))
    lines.append(
        f"{'TOTAL':<16} {'':<22} {total_calls:>6} {'':>5} "
        f"{_fmt_pct(total_skips / total_calls if total_calls else 0.0):>6} "
        f"{_fmt_pct(total_errs / total_calls if total_calls else 0.0):>5}"
    )
    saved_line = f"cache avoided {total_skips} of {total_calls} network call(s)"
    if total_cost_saved > 0:
        saved_line += f" · ${total_cost_saved:,.2f} saved (SOURCE_COST_PER_CALL_USD)"
    lines.append(saved_line)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        default=None,
        help="Inclusive ISO lower bound on called_at, e.g. 2026-05-01.",
    )
    parser.add_argument("--db-path", default=None, help="Override the portfolio DB path.")
    args = parser.parse_args()

    db_path = Path(args.db_path) if args.db_path else None
    rows = summarize_source_calls(since=args.since, db_path=db_path)
    sys.stdout.write(_render(rows) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
