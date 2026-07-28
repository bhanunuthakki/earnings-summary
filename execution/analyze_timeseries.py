"""Time-series intelligence report for one ticker.

Runs every primitive in src/timeseries/ over the canonical financial line
items + per-ticker registered KPIs, and emits a JSON report combining the
results. Mirrors the run_lens.py CLI shape (PROJECT_ROOT discovery,
sys.path insert, --ticker / --repo-root args).

Usage:
    python execution/analyze_timeseries.py --ticker GOOG
    python execution/analyze_timeseries.py --ticker META --out report.json
    python execution/analyze_timeseries.py --ticker NU --pretty

The output JSON has the shape:
    {
      "ticker": "GOOG",
      "generated_at": "2026-05-25T12:00:00Z",
      "series": {
        "<line_item or kpi_name>": {
          "kind": "financial" | "kpi",
          "n_observations": int,
          "period_start": "YYYY-MM-DD",
          "period_end": "YYYY-MM-DD",
          "trend": { ... detect_trend output ... },
          "inflection": { ... detect_inflection output ... },
          "zscore_anomalies": [ ... ],
          "seasonal_decomposition": { ... },
          "yoy_acceleration": { ... }
        },
        ...
      },
      "correlations": { ... correlation_matrix output across KPIs ... }
    }
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import sqlite3
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402
from timeseries import (  # noqa: E402
    Observation,
    compute_and_persist_signals,
    correlation_matrix,
    detect_inflection,
    detect_trend,
    load_financial_series,
    load_kpi_series,
    rolling_zscore_anomalies,
    seasonal_decompose,
    yoy_acceleration,
)

log = logging.getLogger("analyze_timeseries")


# Canonical financial line items always profiled (mirrors llm_client._STATS_LINE_ITEMS
# but kept independent so the CLI can grow its own set without coupling).
_FINANCIAL_LINE_ITEMS: tuple[str, ...] = (
    "revenue",
    "operating_income",
    "free_cash_flow",
    "net_income",
    "operating_cash_flow",
    "capital_expenditure",
    "gross_profit",
)


def _registered_kpi_names(ticker: str, db_path: Path, limit: int = 10) -> list[str]:
    """Pull up to `limit` KPI names registered for this ticker. Returns []
    when the DB is missing or the table is empty."""
    if not db_path.exists():
        return []
    try:
        conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
        try:
            rows = conn.execute(
                "SELECT name FROM kpi_definitions WHERE ticker = ? LIMIT ?",
                (ticker.upper(), int(limit)),
            ).fetchall()
            return [str(r[0]) for r in rows if r[0]]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.warning({"event": "kpi_definitions_lookup_failed", "error": str(exc)})
        return []


def _profile_series(series: list[Observation], kind: str) -> dict[str, object]:
    """Run every primitive against one series; assemble a single dict."""
    if not series:
        return cast("dict[str, object]", {"kind": kind, "n_observations": 0})
    profile: dict[str, object] = {
        "kind": kind,
        "n_observations": len(series),
        "period_start": series[0].period_end.date().isoformat(),
        "period_end": series[-1].period_end.date().isoformat(),
        "trend": detect_trend(series),
        "inflection": detect_inflection(series),
        "zscore_anomalies": rolling_zscore_anomalies(series, window=8, threshold=2.5),
        "yoy_acceleration": yoy_acceleration(series),
    }
    # Seasonal decomposition can be heavy and verbose; only emit summary stats
    # (period, method, strength) to keep the report compact. Full arrays are
    # available by re-running the function directly.
    decomp = seasonal_decompose(series, period=4)
    if "insufficient_data" in decomp:
        profile["seasonal_decomposition"] = decomp
    else:
        profile["seasonal_decomposition"] = cast(
            "dict[str, object]",
            {
                "n": decomp.get("n"),
                "period": decomp.get("period"),
                "method": decomp.get("method"),
                "seasonal_strength": decomp.get("seasonal_strength"),
            },
        )
    return profile


def build_report(
    ticker: str,
    repo_root: Path,
    *,
    as_of_date: date | None = None,
) -> dict[str, object]:
    """Assemble the full time-series intelligence report for one ticker.

    `as_of_date`: when set, the loaders exclude rows from documents fetched
    after that date — the resulting profile is what was knowable on that
    date. Useful for replaying a historical brief deterministically.
    """
    db_path = repo_root / "data" / "portfolio.db"

    series_block: dict[str, object] = {}
    loaded_kpi_names: list[str] = []

    # Financial line items
    for li in _FINANCIAL_LINE_ITEMS:
        s = load_financial_series(
            ticker=ticker,
            line_item=li,
            repo_root=repo_root,
            as_of_date=as_of_date,
        )
        if s:
            series_block[li] = _profile_series(s, kind="financial")

    # Registered KPIs
    for kpi_name in _registered_kpi_names(ticker, db_path):
        s = load_kpi_series(
            ticker=ticker,
            kpi_name=kpi_name,
            repo_root=repo_root,
            as_of_date=as_of_date,
        )
        if s:
            series_block[kpi_name] = _profile_series(s, kind="kpi")
            loaded_kpi_names.append(kpi_name)

    # Cross-KPI correlation across the loaded KPI names. Only meaningful when
    # we have >= 2 KPIs that share enough quarterly overlap.
    correlations: dict[str, object]
    if len(loaded_kpi_names) >= 2:
        correlations = correlation_matrix(
            ticker=ticker, kpi_names=loaded_kpi_names, repo_root=repo_root
        )
    else:
        correlations = cast(
            "dict[str, object]",
            {"insufficient_data": True, "reason": "fewer_than_two_kpis_with_data"},
        )

    report: dict[str, object] = {
        "ticker": ticker.upper(),
        "generated_at": datetime.now(UTC).isoformat(),
        "n_series_profiled": len(series_block),
        "series": series_block,
        "correlations": correlations,
    }
    if as_of_date is not None:
        report["as_of_date"] = as_of_date.isoformat()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", required=True, help="Ticker symbol (e.g. GOOG, META).")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root containing data/portfolio.db. Defaults to the worktree root.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write JSON to this path instead of stdout.",
    )
    parser.add_argument("--pretty", action="store_true", help="Pretty-print the JSON (indent=2).")
    parser.add_argument(
        "--no-persist",
        action="store_true",
        help="Skip writing signals into the timeseries_signals table "
        "(default: persist, so ad-hoc CLI runs benefit downstream consumers).",
    )
    parser.add_argument(
        "--as-of-date",
        type=lambda s: date.fromisoformat(s),
        default=None,
        help=(
            "Reproduce the report as it would have looked on this date. "
            "Excludes rows from documents fetched after the date. "
            "Format: YYYY-MM-DD."
        ),
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    ticker = args.ticker.upper()
    repo_root = args.repo_root.resolve()
    report = build_report(ticker, repo_root, as_of_date=args.as_of_date)

    # Signal persistence is unconditional unless the caller opts out OR
    # --as-of-date is set (replaying a historical view shouldn't overwrite
    # the live signals table with stale results).
    if not args.no_persist and args.as_of_date is None:
        db_path = repo_root / "data" / "portfolio.db"
        if db_path.exists():
            try:
                conn = connect_sqlite(
                    str(db_path), role=SQLiteConnectionRole.WRITER, schema_preflight=True
                )
                try:
                    n = compute_and_persist_signals(ticker=ticker, db=conn, repo_root=repo_root)
                    report["signals_persisted"] = n
                finally:
                    conn.close()
            except sqlite3.OperationalError as exc:
                # Most commonly: table missing because migration hasn't been
                # applied. Surface in the report payload — the JSON still
                # renders for inspection.
                report["signals_persisted_error"] = str(exc)

    indent = 2 if args.pretty else None
    rendered = json.dumps(report, indent=indent, default=str, ensure_ascii=False)

    if args.out:
        args.out.write_text(rendered + "\n", encoding="utf-8")
        # Print path only — actual content already on disk
        sys.stdout.write(f"Wrote {args.out} ({len(rendered)} chars)\n")
    else:
        # Force UTF-8 on Windows to handle any unicode in series names
        with contextlib.suppress(AttributeError, OSError):
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stdout.write(rendered + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
