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

import sys

try:  # direct script invocation
    from _lib import add_database_argument, command_parser, require_db_path
except ImportError:  # pragma: no cover - package import fallback
    from execution._lib import add_database_argument, command_parser, require_db_path

from sources.registry import SourceCallSummary, summarize_source_calls


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
    parser = command_parser(__doc__)
    parser.add_argument(
        "--since",
        default=None,
        help="Inclusive ISO lower bound on called_at, e.g. 2026-05-01.",
    )
    add_database_argument(
        parser,
        flag="--db-path",
        help_text="Override the portfolio DB path.",
        metavar=None,
    )
    args = parser.parse_args()

    try:
        db_path = require_db_path(args.db_path)
    except (RuntimeError, FileNotFoundError) as exc:
        sys.stderr.write(f"refusing to run: {exc}\n")
        return 2
    rows = summarize_source_calls(since=args.since, db_path=db_path)
    sys.stdout.write(_render(rows) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
