"""Per-ticker x per-quarter segment coverage matrix (docs/design/
segment_quarterly_framework.md §5.2, Phase 2).

Usage:
    python execution/audit_segment_quarterly_coverage.py
    python execution/audit_segment_quarterly_coverage.py --tickers MELI,AMZN
    python execution/audit_segment_quarterly_coverage.py --since-year 2022

For each (ticker, fiscal_year, period) triple enumerated from that ticker's
cached ``{T}_financial_reports_dates.json`` (the ground-truth roster of FMP
filings that should exist, per §5.2 step 1) -- Q1/Q2/Q3 taken verbatim, plus a
synthesized ``Q4`` whenever ``FY`` is present (a 10-K's own fiscal-year-end
anchor; Q4 never has its own 10-Q) -- this script:

1. Checks whether a ``segment_periods`` row already covers that cell ->
   ``reported`` (period_basis discrete/cumulative) or ``derived``
   (period_basis derived).
2. Else surfaces an existing ``segment_quarterly_coverage`` row written by
   the extractor/deriver itself (``not_computable``/``tolerance_breach``/
   ``not_disclosed``/``not_applicable``) as-is.
3. Else writes a fresh ``source_missing`` row with a reason code
   (``fpi_route_unproven`` / ``filing_regime_unresolved`` /
   ``fiscal_year_end_unresolvable`` / ``legacy_endpoint_annual_only`` /
   ``legacy_cache_stale`` / ``no_10q_json_fetched`` /
   ``no_10k_fy_anchor_or_quarterly_inputs``).

Cross-referenced from ``audit_segment_coverage.py``'s docstring per the
design doc's own "extension takes the form of a new sibling script" note --
this is NOT a new mode bolted onto that DCF-build-coverage script; the two
are mechanically unrelated (subprocess-per-ticker vs. table-upsert).

Output: prints the ticker x (year, period) grid as JSON to stdout, or (per
the repo's >2,000-line / >100KB output rule) writes it to
``.tmp/segment_quarterly_coverage_audit.json`` and prints only the path.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db  # noqa: E402
from models.companies import FilingRegime  # noqa: E402
from pipeline.segment_quarterly_coverage import record_coverage  # noqa: E402
from pipeline.source_routing import segment_pipeline_for_regime  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402
from table_extractors.period_axis import NominalQuarter, expected_period_ends  # noqa: E402

AUDIT_METHOD_VERSION = "audit_segment_quarterly_coverage_v1"
_EXPECTED_FILED_PERIODS = ("Q1", "Q2", "Q3", "FY")
_LEGACY_STALE_DAYS = 120


def main() -> int:
    args = _parse_args()
    repo_root = args.repo_root.resolve()
    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        print(f"[error] no DB at {db_path}", file=sys.stderr)
        return 1

    conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
    conn.row_factory = sqlite3.Row

    tickers = _resolve_tickers(conn, args)
    grid: dict[str, dict[str, dict[str, object]]] = {}
    for ticker in tickers:
        cells = _audit_ticker(conn, repo_root, ticker, args.since_year)
        if cells:
            grid[ticker] = cells
    conn.commit()
    conn.close()

    payload_str = json.dumps(grid, indent=2, sort_keys=True)
    if len(payload_str) > 100_000 or payload_str.count("\n") > 2000:
        out_path = repo_root / ".tmp" / "segment_quarterly_coverage_audit.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(payload_str, encoding="utf-8")
        print(json.dumps({"tickers": len(grid), "written_to": str(out_path)}))
    else:
        print(payload_str)
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--tickers", default=None, help="Comma-separated ticker list")
    p.add_argument("--since-year", type=int, default=None, help="Only fiscal years >= this")
    p.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    return p.parse_args()


def _resolve_tickers(conn: sqlite3.Connection, args: argparse.Namespace) -> list[str]:
    if args.tickers:
        return [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    cur = conn.cursor()
    cur.execute(
        f"SELECT DISTINCT ticker FROM tracked_companies "
        f"WHERE list_type IN {db.ACTIVE_LIST_TYPES_SQL} ORDER BY ticker"
    )
    return [r[0] for r in cur.fetchall()]


def _financial_reports_dates(repo_root: Path, ticker: str) -> list[dict[str, object]]:
    path = repo_root / "data" / "historical" / "fmp" / f"{ticker}_financial_reports_dates.json"
    if not path.exists():
        return []
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    items = cast("list[object]", payload)
    return [cast("dict[str, object]", row) for row in items if isinstance(row, dict)]


def _parse_fye(raw: object) -> tuple[int, int] | None:
    if not isinstance(raw, str) or len(raw) != 5 or raw[2] != "-":
        return None
    try:
        return int(raw[:2]), int(raw[3:])
    except ValueError:
        return None


def _expected_period_end(
    fiscal_year: int, period: str, fye_month_day: tuple[int, int] | None
) -> date:
    if fye_month_day is None:
        # Best-effort fallback when FYE truly can't be resolved -- the
        # accompanying reason_code (fiscal_year_end_unresolvable) already
        # flags this cell's period_end as approximate.
        return date(fiscal_year, 12, 31)
    fye_month, fye_day = fye_month_day
    if period in ("FY", "Q4"):
        import calendar

        day = min(fye_day, calendar.monthrange(fiscal_year, fye_month)[1])
        return date(fiscal_year, fye_month, day)
    # ``period`` is validated to be "Q1"/"Q2"/"Q3" by the caller's own
    # branch above (FY/Q4 handled separately) -- a validated external-data
    # boundary, same convention extract_segment_quarterly.py's filename
    # regex cast uses.
    current_end, _prior = expected_period_ends(
        cast("NominalQuarter", period), fiscal_year, fye_month, fye_day
    )
    return current_end


def _legacy_cache_status(repo_root: Path, ticker: str) -> str | None:
    """Finding 1 + §7 risk #5: has the legacy FMP quarterly segment cache
    gone annual-only for this ticker, or gone stale itself? Checked once per
    ticker (not per-cell) -- the design doc's own framing is a per-ticker
    staleness signal."""
    for suffix in ("product_segments_quarterly", "geo_segments_quarterly"):
        path = repo_root / "data" / "historical" / "fmp" / f"{ticker}_{suffix}.json"
        if not path.exists():
            continue
        try:
            payload: object = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, list) or not payload:
            continue
        items = cast("list[object]", payload)
        rows: list[dict[str, object]] = [
            cast("dict[str, object]", r) for r in items if isinstance(r, dict)
        ]
        recent_fy_years = sorted(
            {str(r.get("fiscalYear")) for r in rows if r.get("period") == "FY"}, reverse=True
        )[:2]
        if not recent_fy_years:
            continue
        has_quarterly_for_recent = any(
            r.get("period") in ("Q1", "Q2", "Q3", "Q4")
            and str(r.get("fiscalYear")) in recent_fy_years
            for r in rows
        )
        if not has_quarterly_for_recent:
            age_days = (time.time() - path.stat().st_mtime) / 86400
            return (
                "legacy_cache_stale"
                if age_days > _LEGACY_STALE_DAYS
                else "legacy_endpoint_annual_only"
            )
    return None


def _period_status(
    conn: sqlite3.Connection, ticker: str, fiscal_period_type: str, fiscal_year: int
) -> str | None:
    row = conn.execute(
        """
        SELECT period_basis FROM segment_periods
        WHERE ticker = ? AND fiscal_period_type = ? AND strftime('%Y', period_end) = ?
        ORDER BY id DESC LIMIT 1
        """,
        (ticker, fiscal_period_type, str(fiscal_year)),
    ).fetchone()
    if row is None:
        return None
    return "derived" if row[0] == "derived" else "reported"


def _existing_gap_row(
    conn: sqlite3.Connection, ticker: str, fiscal_period_type: str, fiscal_year: int
) -> tuple[str, str | None] | None:
    row = conn.execute(
        """
        SELECT status, reason_code FROM segment_quarterly_coverage
        WHERE ticker = ? AND fiscal_period_type = ? AND strftime('%Y', period_end) = ?
          AND dim_type IS NULL AND dim_name IS NULL
        ORDER BY checked_at DESC LIMIT 1
        """,
        (ticker, fiscal_period_type, str(fiscal_year)),
    ).fetchone()
    if row is None:
        return None
    status = str(row[0])
    if status not in ("not_disclosed", "not_applicable", "not_computable", "tolerance_breach"):
        return None
    return (status, cast("str | None", row[1]))


def _cell_status(
    conn: sqlite3.Connection,
    repo_root: Path,
    ticker: str,
    fiscal_year: int,
    period: str,
    pipeline: str,
    fye_month_day: tuple[int, int] | None,
    legacy_reason: str | None,
) -> tuple[str, str | None]:
    reported = _period_status(conn, ticker, period, fiscal_year)
    if reported is not None:
        return (reported, None)

    gap = _existing_gap_row(conn, ticker, period, fiscal_year)
    if gap is not None:
        return gap

    if pipeline in ("fpi_6k", "mjds_ir_pdf"):
        # Genuinely a 20-F/40-F filer -- Phase-3-spike-gated (§1.1/§7 risk #1).
        reason = "fpi_route_unproven"
    elif pipeline == "unsupported":
        # filing_regime is still NULL for this ticker -- the §1.3 self-heal
        # hasn't run yet, not a Phase-3 FPI/MJDS gap. Real-data smoke testing
        # (AGX, filing_regime NULL despite being a plain 10-K domestic filer)
        # surfaced this as a distinct case from fpi_route_unproven -- do not
        # conflate "we don't know your regime yet" with "your regime is one
        # this framework doesn't support yet".
        reason = "filing_regime_unresolved"
    elif fye_month_day is None:
        reason = "fiscal_year_end_unresolvable"
    elif period in ("Q1", "Q2", "Q3") and legacy_reason is not None:
        reason = legacy_reason
    elif period == "Q4":
        reason = "no_10k_fy_anchor_or_quarterly_inputs"
    else:
        reason = "no_10q_json_fetched"

    period_end = _expected_period_end(fiscal_year, period, fye_month_day)
    record_coverage(
        conn,
        ticker=ticker,
        period_end=datetime(period_end.year, period_end.month, period_end.day),
        fiscal_period_type=period,
        dim_type=None,
        dim_name=None,
        status="source_missing",
        reason_code=reason,
        source_doc_id=None,
        method_version=AUDIT_METHOD_VERSION,
    )
    return ("source_missing", reason)


def _audit_ticker(
    conn: sqlite3.Connection, repo_root: Path, ticker: str, since_year: int | None
) -> dict[str, dict[str, object]]:
    row = conn.execute(
        "SELECT filing_regime, fiscal_year_end FROM tracked_companies WHERE ticker = ?", (ticker,)
    ).fetchone()
    if row is None:
        return {}
    filing_regime_raw = row[0] if not hasattr(row, "keys") else row["filing_regime"]
    fye_raw = row[1] if not hasattr(row, "keys") else row["fiscal_year_end"]
    filing_regime = FilingRegime(filing_regime_raw) if filing_regime_raw else None
    pipeline = segment_pipeline_for_regime(filing_regime)

    dates = _financial_reports_dates(repo_root, ticker)
    if not dates:
        return {}

    fye_month_day = _parse_fye(fye_raw)
    legacy_reason = _legacy_cache_status(repo_root, ticker)

    fiscal_years = sorted(
        {
            int(cast("int", d["fiscalYear"]))
            for d in dates
            if d.get("period") in _EXPECTED_FILED_PERIODS and isinstance(d.get("fiscalYear"), int)
        }
    )

    out: dict[str, dict[str, object]] = {}
    for fy in fiscal_years:
        if since_year is not None and fy < since_year:
            continue
        periods_this_year = {d.get("period") for d in dates if d.get("fiscalYear") == fy}
        expected = [p for p in ("Q1", "Q2", "Q3") if p in periods_this_year]
        if "FY" in periods_this_year:
            expected.extend(("FY", "Q4"))
        for period in expected:
            status, reason = _cell_status(
                conn, repo_root, ticker, fy, period, pipeline, fye_month_day, legacy_reason
            )
            out[f"{fy}_{period}"] = {"status": status, "reason_code": reason}
    return out


if __name__ == "__main__":
    raise SystemExit(main())
