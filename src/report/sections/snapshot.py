"""§1 Executive snapshot — thesis verdict + valuation card + tier-1 KPI strip.

Current price is read in priority order:
  1. data_rules.current_price_override in the holdings JSON (manual override)
  2. data/historical/fmp/{TICKER}_price_chart_*.json (most recent close)
  3. None (rendered as "—")
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from report.models import (
    KpiSnapshotRow,
    SectionStatus,
    SnapshotSection,
    ValuationSnapshot,
)
from report.rules import load_rules
from report.sections._common import has_table, missing, open_repo_db
from report.sections.thesis import _split_stub_warning


def build(ticker: str, repo_root: Path, model_link: str | None) -> SnapshotSection:
    holdings = _read_holdings(ticker, repo_root)
    rules = load_rules(ticker, repo_root)
    company_name = _company_name(ticker, repo_root)
    current_price = rules.current_price_override or _latest_price(ticker, repo_root)
    valuation = _valuation_snapshot(ticker, repo_root, current_price, model_link)
    verdict = _verdict(ticker, repo_root)
    tier_1_strip = _tier_1_strip(holdings)

    if holdings is None and valuation.consolidated_npv_per_share is None:
        return SnapshotSection(
            status=SectionStatus.MISSING_DATA,
            missing=missing(
                stage="INGEST(holdings) + COMPUTE(dcf)",
                fix_command=(
                    f"create micro_thesis/holdings/{ticker.upper()}.json "
                    f"and run: python execution/run_dcf.py --ticker {ticker.upper()} ..."
                ),
            ),
            ticker=ticker.upper(),
            company_name=company_name,
            valuation=valuation,
        )

    thesis_clean, _ = _split_stub_warning(holdings or {})
    return SnapshotSection(
        status=SectionStatus.OK if valuation.consolidated_npv_per_share else SectionStatus.PARTIAL,
        ticker=ticker.upper(),
        company_name=company_name,
        thesis_one_liner=thesis_clean,
        verdict=verdict,
        valuation=valuation,
        tier_1_kpi_row=tier_1_strip,
    )


def _read_holdings(ticker: str, repo_root: Path) -> dict[str, object] | None:
    path = repo_root / "micro_thesis" / "holdings" / f"{ticker.upper()}.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _company_name(ticker: str, repo_root: Path) -> str | None:
    conn = open_repo_db(repo_root)
    if conn is None:
        return None
    cursor = conn.cursor()
    cursor.execute(
        "SELECT name FROM tracked_companies WHERE ticker = ? LIMIT 1",
        (ticker.upper(),),
    )
    row = cursor.fetchone()
    conn.close()
    return row["name"] if row else None


def _latest_price(ticker: str, repo_root: Path) -> float | None:
    """Most recent close price from the FMP price-chart JSON, or None."""
    fmp_dir = repo_root / "data" / "historical" / "fmp"
    if not fmp_dir.exists():
        return None
    upper = ticker.upper()
    candidates = [p for p in fmp_dir.glob(f"{upper}_*price_chart*.json")]
    candidates.extend(fmp_dir.glob(f"{upper}L_*price_chart*.json"))  # GOOG ↔ GOOGL alias
    for path in candidates:
        price = _read_latest_close(path)
        if price is not None:
            return price
    return None


def _read_latest_close(path: Path) -> float | None:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    records = data if isinstance(data, list) else data.get("historical") if isinstance(data, dict) else None
    if not isinstance(records, list) or not records:
        return None
    sorted_records = sorted(
        (r for r in records if isinstance(r, dict) and isinstance(r.get("date"), str)),
        key=lambda r: r["date"],
        reverse=True,
    )
    for r in sorted_records:
        for key in ("adjClose", "close", "price"):
            v = r.get(key)
            if isinstance(v, (int, float)):
                return float(v)
    return None


def _valuation_snapshot(
    ticker: str, repo_root: Path, current_price: float | None, model_link: str | None
) -> ValuationSnapshot:
    conn = open_repo_db(repo_root)
    if conn is None or not has_table(conn, "dcf_runs"):
        if conn is not None:
            conn.close()
        return ValuationSnapshot(current_price=current_price, model_link=model_link)

    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT valuation_date, wacc, terminal_growth, npv, npv_per_share,
               shares_outstanding, breakdown_json
        FROM dcf_runs
        WHERE ticker = ?
        LIMIT 1
        """,
        (ticker.upper(),),
    )
    row = cursor.fetchone()
    conn.close()
    if row is None:
        return ValuationSnapshot(current_price=current_price, model_link=model_link)

    cons_npv_per_share = (
        float(row["npv_per_share"]) if row["npv_per_share"] is not None else None
    )
    sum_of_segments = _sum_of_segments_npv_per_share(
        row["breakdown_json"], row["shares_outstanding"]
    )
    upside = None
    if cons_npv_per_share is not None and current_price not in (None, 0):
        assert current_price is not None
        upside = cons_npv_per_share / current_price - 1.0
    return ValuationSnapshot(
        consolidated_npv_per_share=cons_npv_per_share,
        sum_of_segments_npv_per_share=sum_of_segments,
        current_price=current_price,
        implied_upside_pct=upside,
        wacc=row["wacc"],
        terminal_growth=row["terminal_growth"],
        valuation_date=row["valuation_date"],
        model_link=model_link,
    )


def _sum_of_segments_npv_per_share(
    breakdown_json: str | None, shares_outstanding: object
) -> float | None:
    """Sum the NPVs of components flagged `segment` (excludes overhead) and per-share scale."""
    if breakdown_json is None:
        return None
    try:
        components = json.loads(breakdown_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(components, list):
        return None
    seg_npv_total = 0.0
    found_segment = False
    for c in components:
        if not isinstance(c, dict):
            continue
        if c.get("component_type") != "segment":
            continue
        npv = c.get("npv")
        if isinstance(npv, (int, float)):
            seg_npv_total += float(npv)
            found_segment = True
    if not found_segment:
        return None
    try:
        shares = float(shares_outstanding) if shares_outstanding is not None else None
    except (TypeError, ValueError):
        shares = None
    if not shares or shares <= 0:
        return None
    return seg_npv_total / shares


def _verdict(ticker: str, repo_root: Path) -> Literal["intact", "watch", "broken", "pending"]:
    conn = open_repo_db(repo_root)
    if conn is None or not has_table(conn, "thesis_state"):
        if conn is not None:
            conn.close()
        return "pending"
    cursor = conn.cursor()
    cursor.execute("SELECT breach_status FROM thesis_state WHERE ticker = ?", (ticker.upper(),))
    row = cursor.fetchone()
    conn.close()
    if row is None or row["breach_status"] is None:
        return "pending"
    status = row["breach_status"].lower()
    if status == "ok":
        return "intact"
    if status == "warn":
        return "watch"
    if status == "breach":
        return "broken"
    return "pending"


def _tier_1_strip(holdings: dict[str, object] | None) -> list[KpiSnapshotRow]:
    if holdings is None:
        return []
    rows: list[KpiSnapshotRow] = []
    kpis = holdings.get("tier_1_kpis") or []
    if not isinstance(kpis, list):
        return []
    for k in kpis:
        if not isinstance(k, dict):
            continue
        rows.append(
            KpiSnapshotRow(
                name=str(k.get("name", "")),
                threshold=str(k.get("break")) if k.get("break") else None,
                status="unknown",  # populated by §2 builder when KPI facts exist
            )
        )
    return rows
