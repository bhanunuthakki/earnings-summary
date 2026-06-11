"""§1 (eval flavor) — 3y quick-categorization data table for new-name screening.

Reads the `metrics` and `ratios` views for 3 fiscal years + TTM, plus a 4th
prior FY to anchor the 3y CAGR baseline. Pulls company metadata (name from
the DB, sector/market-cap/current-price from the FMP `profile.json` cache
when present). No LLM in this section.

Rendered in §1 position only when `flavor == ReportFlavor.EVALUATION`.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import cast

from report.models import (
    EvaluationSnapshotSection,
    QuickCategorizationRow,
    SectionStatus,
)
from report.sections._common import has_table, missing, open_repo_db


def build(ticker: str, repo_root: Path) -> EvaluationSnapshotSection:
    ticker = ticker.upper()
    conn = open_repo_db(repo_root)
    if conn is None:
        return _missing(
            ticker,
            stage="PERSIST(init_db)",
            fix_command="alembic upgrade head",
            detail="No portfolio.db at data/portfolio.db.",
        )

    if not has_table(conn, "metrics") or not has_table(conn, "ratios"):
        conn.close()
        return _missing(
            ticker,
            stage="PERSIST(metrics_views)",
            fix_command="alembic upgrade head",
            detail="The metrics + ratios views are missing. Migration 0012 creates them.",
        )

    # 4 FY rows so the 3y CAGR baseline (LFY-3) is available, plus TTM.
    annual_metrics = _load_annual(conn, ticker, table="metrics", n=4)
    annual_ratios = _load_annual(conn, ticker, table="ratios", n=4)
    ttm_metrics = _load_ttm(conn, ticker, table="metrics")
    ttm_ratios = _load_ttm(conn, ticker, table="ratios")
    company_name = _load_company_name(conn, ticker)
    conn.close()

    if not annual_metrics:
        return _missing(
            ticker,
            stage="INGEST(financial_facts)",
            fix_command=f"python execution/onboard_ticker.py --ticker {ticker}",
            detail="No annual rows in the metrics view for this ticker.",
        )

    sector, market_cap, current_price = _load_profile_fields(repo_root, ticker)

    fiscal_years = [_safe_int(r["fiscal_year"]) for r in annual_metrics[-3:]]
    rows = _build_rows(annual_metrics, annual_ratios, ttm_metrics, ttm_ratios)

    return EvaluationSnapshotSection(
        status=SectionStatus.OK,
        ticker=ticker,
        company_name=company_name,
        sector=sector,
        market_cap=market_cap,
        current_price=current_price,
        rows=rows,
        fiscal_years=fiscal_years,
    )


# ---------------------------------------------------------------------------
# DB loaders
# ---------------------------------------------------------------------------


def _load_annual(
    conn: sqlite3.Connection, ticker: str, table: str, n: int
) -> list[dict[str, object]]:
    """Return up to N most-recent FY rows from `table`, oldest first."""
    cursor = conn.cursor()
    cursor.execute(
        f"""
        SELECT * FROM {table}
        WHERE ticker = ? AND fiscal_period_type = 'FY'
        ORDER BY period_end DESC LIMIT ?
        """,
        (ticker, n),
    )
    rows = [dict(r) for r in cursor.fetchall()]
    rows.reverse()  # oldest → newest
    return rows


def _load_ttm(conn: sqlite3.Connection, ticker: str, table: str) -> dict[str, object] | None:
    cursor = conn.cursor()
    cursor.execute(
        f"""
        SELECT * FROM {table}
        WHERE ticker = ? AND fiscal_period_type = 'TTM'
        ORDER BY period_end DESC LIMIT 1
        """,
        (ticker,),
    )
    row = cursor.fetchone()
    return dict(row) if row is not None else None


def _load_company_name(conn: sqlite3.Connection, ticker: str) -> str | None:
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM tracked_companies WHERE ticker = ?", (ticker,))
    row = cursor.fetchone()
    if row is None:
        return None
    return str(row["name"]) if row["name"] is not None else None


def _load_profile_fields(
    repo_root: Path, ticker: str
) -> tuple[str | None, float | None, float | None]:
    """Read sector / market_cap / current_price from FMP profile.json cache.

    All three are optional. If the file or any field is missing, returns None
    for that field — the section still renders fine.
    """
    path = repo_root / "data" / "historical" / "fmp" / f"{ticker}_profile.json"
    if not path.exists():
        return (None, None, None)
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return (None, None, None)
    # FMP profile endpoint returns a list of one object. Cast at the JSON
    # boundary — pyright cannot narrow `json.loads` output past `object`.
    rec: dict[str, object] | None = None
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        rec = cast("dict[str, object]", payload[0])
    elif isinstance(payload, dict):
        rec = cast("dict[str, object]", payload)
    if rec is None:
        return (None, None, None)
    sector = rec.get("sector")
    market_cap = rec.get("marketCap") or rec.get("mktCap")
    current_price = rec.get("price")
    return (
        sector if isinstance(sector, str) else None,
        float(market_cap) if isinstance(market_cap, (int, float)) else None,
        float(current_price) if isinstance(current_price, (int, float)) else None,
    )


# ---------------------------------------------------------------------------
# Row assembly
# ---------------------------------------------------------------------------


_USD_M = 1_000_000.0
# The ratios view stores FRACTIONS (operating_income / revenue → 0.062); the
# display contract is percent POINTS (6.2 + unit "%"). _scale divides, so the
# fraction→points conversion is a divisor of 0.01. Without this every ratio row
# rendered 100x too small ("0.1%" for a 6.2% margin).
_FRACTION_TO_PCT = 0.01


def _build_rows(
    annual_metrics: list[dict[str, object]],
    annual_ratios: list[dict[str, object]],
    ttm_metrics: dict[str, object] | None,
    ttm_ratios: dict[str, object] | None,
) -> list[QuickCategorizationRow]:
    """Map (metrics, ratios, ttm_*) into 5 display rows.

    Caller has already trimmed annual_* to at most 4 rows (LFY-3..LFY). We use
    LFY-3 as the CAGR baseline for absolute series and drop it from display.
    """
    by_period_m: dict[int, dict[str, object]] = {
        _safe_int(r["fiscal_year"]): r for r in annual_metrics
    }
    by_period_r: dict[int, dict[str, object]] = {
        _safe_int(r["fiscal_year"]): r for r in annual_ratios
    }
    years = sorted(by_period_m.keys())
    if not years:
        return []
    display_years = years[-3:]
    cagr_baseline_year = years[-4] if len(years) >= 4 else None

    return [
        _abs_row(
            "Revenue",
            "USD M",
            0,
            by_period_m,
            ttm_metrics,
            "revenue",
            display_years,
            cagr_baseline_year,
            scale=_USD_M,
        ),
        _abs_row(
            "EPS diluted",
            "USD",
            2,
            by_period_m,
            ttm_metrics,
            "eps_diluted",
            display_years,
            cagr_baseline_year,
            scale=1.0,
        ),
        _ratio_row(
            "Operating margin",
            "%",
            1,
            by_period_r,
            ttm_ratios,
            "operating_margin",
            display_years,
        ),
        _ratio_row(
            "FCF margin",
            "%",
            1,
            by_period_r,
            ttm_ratios,
            "fcf_margin",
            display_years,
        ),
        _ratio_row(
            "ROE",
            "%",
            1,
            by_period_r,
            ttm_ratios,
            "roe",
            display_years,
        ),
    ]


def _abs_row(
    metric: str,
    unit: str,
    digits: int,
    by_period: dict[int, dict[str, object]],
    ttm: dict[str, object] | None,
    col: str,
    display_years: list[int],
    cagr_baseline_year: int | None,
    scale: float,
) -> QuickCategorizationRow:
    """Build a row for an absolute-value series (Revenue, EPS). CAGR included."""
    lfy_minus_2 = _annual_scaled(by_period, _at(display_years, 0), col, scale)
    lfy_minus_1 = _annual_scaled(by_period, _at(display_years, 1), col, scale)
    lfy = _annual_scaled(by_period, _at(display_years, 2), col, scale)
    baseline = _annual_scaled(by_period, cagr_baseline_year, col, scale)
    return QuickCategorizationRow(
        metric=metric,
        unit=unit,
        digits=digits,
        lfy_minus_2=lfy_minus_2,
        lfy_minus_1=lfy_minus_1,
        lfy=lfy,
        ttm=_scale(_dict_get(ttm, col), scale),
        cagr_3y=_cagr_3y(baseline, lfy),
    )


def _ratio_row(
    metric: str,
    unit: str,
    digits: int,
    by_period: dict[int, dict[str, object]],
    ttm: dict[str, object] | None,
    col: str,
    display_years: list[int],
) -> QuickCategorizationRow:
    """Build a row for a ratio series (margins, ROE). CAGR intentionally None."""
    return QuickCategorizationRow(
        metric=metric,
        unit=unit,
        digits=digits,
        lfy_minus_2=_annual_scaled(by_period, _at(display_years, 0), col, _FRACTION_TO_PCT),
        lfy_minus_1=_annual_scaled(by_period, _at(display_years, 1), col, _FRACTION_TO_PCT),
        lfy=_annual_scaled(by_period, _at(display_years, 2), col, _FRACTION_TO_PCT),
        ttm=_scale(_dict_get(ttm, col), _FRACTION_TO_PCT),
        cagr_3y=None,
    )


def _annual_scaled(
    by_period: dict[int, dict[str, object]],
    year: int | None,
    col: str,
    scale: float,
) -> float | None:
    if year is None or year not in by_period:
        return None
    return _scale(_to_float(by_period[year].get(col)), scale)


def _at(xs: list[int], i: int) -> int | None:
    return xs[i] if 0 <= i < len(xs) else None


def _safe_int(v: object) -> int:
    """Convert a SQLite value to int. fiscal_year is always int in the schema."""
    if isinstance(v, (int, float)):
        return int(v)
    raise TypeError(f"expected numeric fiscal_year, got {type(v).__name__}: {v!r}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _missing(ticker: str, stage: str, fix_command: str, detail: str) -> EvaluationSnapshotSection:
    return EvaluationSnapshotSection(
        status=SectionStatus.MISSING_DATA,
        missing=missing(stage=stage, fix_command=fix_command, detail=detail),
        ticker=ticker,
    )


def _to_float(v: object) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v))
    except (TypeError, ValueError):
        return None


def _scale(v: float | None, divisor: float) -> float | None:
    return None if v is None else v / divisor


def _dict_get(d: dict[str, object] | None, key: str) -> float | None:
    if d is None:
        return None
    return _to_float(d.get(key))


def _cagr_3y(baseline: float | None, end: float | None) -> float | None:
    """LFY-3 → LFY CAGR over 3 years. Requires positive endpoints."""
    if baseline is None or end is None or baseline <= 0 or end <= 0:
        return None
    return (end / baseline) ** (1.0 / 3.0) - 1.0
