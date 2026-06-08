"""§1 Executive snapshot — thesis verdict + valuation card + tier-1 KPI strip.

Current price is read in priority order:
  1. data_rules.current_price_override in the holdings JSON (manual override)
  2. data/historical/fmp/{TICKER}_price_chart_*.json (most recent close)
  3. None (rendered as "—")
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Literal, cast

from report.models import (
    DecisionBadge,
    KpiSnapshotRow,
    SectionStatus,
    SnapshotSection,
    ValuationSnapshot,
)
from report.rules import load_rules
from report.sections._common import has_table, missing, open_repo_db
from report.sections.thesis import _split_stub_warning


def build(
    ticker: str, repo_root: Path, model_link: str | None, *, held: bool = False
) -> SnapshotSection:
    holdings = _read_holdings(ticker, repo_root)
    rules = load_rules(ticker, repo_root)
    company_name = _company_name(ticker, repo_root)
    mos_bar = _mos_bar(holdings)
    sheet_url = _linked_sheet_url(holdings)
    current_price = rules.current_price_override or _latest_price(ticker, repo_root)
    valuation = _valuation_snapshot(
        ticker, repo_root, current_price, model_link, mos_bar, sheet_url, held=held
    )
    verdict = _verdict(ticker, repo_root)
    tier_1_strip = _tier_1_strip(holdings)
    recent_decisions = _recent_decisions(ticker, repo_root)

    if holdings is None and valuation.consolidated_npv_per_share is None:
        return SnapshotSection(
            status=SectionStatus.MISSING_DATA,
            missing=missing(
                stage="INGEST(holdings) + COMPUTE(dcf)",
                fix_command=(
                    f"create micro_thesis/holdings/{ticker.upper()}.json "
                    f"and run: python execution/refresh_dcf.py --ticker {ticker.upper()}"
                ),
            ),
            ticker=ticker.upper(),
            company_name=company_name,
            valuation=valuation,
            recent_decisions=recent_decisions,
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
        recent_decisions=recent_decisions,
    )


def _read_holdings(ticker: str, repo_root: Path) -> dict[str, object] | None:
    path = repo_root / "micro_thesis" / "holdings" / f"{ticker.upper()}.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _linked_sheet_url(holdings: dict[str, object] | None) -> str | None:
    """The live Google Sheet URL for a ticker's DCF, from ``dcf_defaults.gsheet_id``.

    Mirrors ``comments_server._linked_gsheet`` so the brief's static link and the
    server's ``/dcf/<T>`` redirect resolve a Sheet the same way. ``None`` when no
    Sheet is linked (the card then falls back to the ``.xlsx`` link)."""
    if not holdings:
        return None
    dd = holdings.get("dcf_defaults")
    if not isinstance(dd, dict):
        return None
    gid = cast("dict[str, object]", dd).get("gsheet_id")
    if isinstance(gid, str) and gid:
        return f"https://docs.google.com/spreadsheets/d/{gid}/edit"
    return None


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
    records = (
        data
        if isinstance(data, list)
        else data.get("historical")
        if isinstance(data, dict)
        else None
    )
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
    ticker: str,
    repo_root: Path,
    current_price: float | None,
    model_link: str | None,
    mos_bar: float | None,
    sheet_url: str | None = None,
    held: bool = False,
) -> ValuationSnapshot:
    conn = open_repo_db(repo_root)
    if conn is None or not has_table(conn, "dcf_runs"):
        if conn is not None:
            conn.close()
        return ValuationSnapshot(
            current_price=current_price,
            model_link=model_link,
            sheet_url=sheet_url,
            mos_bar=mos_bar,
        )

    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT valuation_date, wacc, terminal_growth, npv, npv_per_share,
               shares_outstanding, breakdown_json,
               live_price, live_price_at, over_under_pct, mos_bar_used
        FROM dcf_runs
        WHERE ticker = ?
        LIMIT 1
        """,
        (ticker.upper(),),
    )
    row = cursor.fetchone()
    conn.close()
    if row is None:
        return ValuationSnapshot(
            current_price=current_price,
            model_link=model_link,
            sheet_url=sheet_url,
            mos_bar=mos_bar,
        )

    cons_npv_per_share = float(row["npv_per_share"]) if row["npv_per_share"] is not None else None
    sum_of_segments = _sum_of_segments_npv_per_share(
        row["breakdown_json"], row["shares_outstanding"]
    )

    # Prefer live_price persisted by refresh_dcf (the system-authoritative
    # snapshot at valuation time); fall back to the older _latest_price path
    # so snapshots still render for tickers whose DCF predates the audit cols.
    live_price = float(row["live_price"]) if row["live_price"] is not None else current_price
    over_under = float(row["over_under_pct"]) if row["over_under_pct"] is not None else None
    mos_bar_used = float(row["mos_bar_used"]) if row["mos_bar_used"] is not None else mos_bar
    live_price_at = _parse_iso_datetime(row["live_price_at"])
    upside = -over_under if over_under is not None else None
    trigger = _trigger_status(over_under, mos_bar_used, held=held)

    return ValuationSnapshot(
        consolidated_npv_per_share=cons_npv_per_share,
        sum_of_segments_npv_per_share=sum_of_segments,
        current_price=live_price,
        implied_upside_pct=upside,
        wacc=row["wacc"],
        terminal_growth=row["terminal_growth"],
        valuation_date=row["valuation_date"],
        model_link=model_link,
        sheet_url=sheet_url,
        over_under_pct=over_under,
        mos_bar=mos_bar_used,
        trigger_status=trigger,
        live_price_at=live_price_at,
    )


def _mos_bar(holdings: dict[str, object] | None) -> float | None:
    """Read mos_bar from holdings JSON v2; return None for pre-v2 stubs."""
    if holdings is None:
        return None
    raw = holdings.get("mos_bar")
    return float(raw) if isinstance(raw, (int, float)) else None


def _trigger_status(
    over_under: float | None, mos_bar: float | None, *, held: bool = False
) -> Literal["sell", "trim", "hold", "add", "initiate_candidate", "unknown"]:
    """Map over_under_pct to the trim/sell ladder, holding-aware.

    >20% over → sell; >10% over → trim; below the MoS bar → ADD when the name is
    already held (accumulate the discount) else INITIATE_CANDIDATE (open a new
    position); otherwise hold. 'initiate_candidate' is reserved for unowned names
    — a held position trading at a discount is an add, not an initiation. Returns
    'unknown' when we have no over/under reading yet.
    """
    if over_under is None:
        return "unknown"
    if over_under > 0.20:
        return "sell"
    if over_under > 0.10:
        return "trim"
    if mos_bar is not None and over_under < -mos_bar:
        return "add" if held else "initiate_candidate"
    return "hold"


def _parse_iso_datetime(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


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


_VALID_OUTCOME_LABELS: frozenset[str] = frozenset(
    {"correct", "wrong", "mixed", "unfalsifiable", "pending"}
)
_RATIONALE_MAX_CHARS = 80


def _recent_decisions(ticker: str, repo_root: Path) -> list[DecisionBadge]:
    """Return the 3 most-recent decisions for `ticker` as DecisionBadge rows.

    Reads via decision_extractor.history so we inherit its db-absent / table-
    absent tolerance (both return []). Empty list means the renderer omits
    the sidebar entirely.
    """
    try:
        from decision_extractor import history as decision_history
    except ImportError:
        return []
    db_path = repo_root / "data" / "portfolio.db"
    rows = decision_history(ticker=ticker.upper(), limit=3, db_path=db_path)
    badges: list[DecisionBadge] = []
    for r in rows:
        raw = r.outcome_label if r.outcome_label in _VALID_OUTCOME_LABELS else "pending"
        outcome = cast(
            'Literal["correct", "wrong", "mixed", "unfalsifiable", "pending"]', raw
        )
        badges.append(
            DecisionBadge(
                date_short=r.made_at.strftime("%Y-%m"),
                recommendation_kind=r.recommendation_kind,
                outcome_label=outcome,
                rationale_short=_rationale_short(r.rationale_excerpt),
            )
        )
    return badges


def _rationale_short(text: str | None) -> str:
    if not text:
        return ""
    cleaned = text.strip()
    if len(cleaned) <= _RATIONALE_MAX_CHARS:
        return cleaned
    return cleaned[:_RATIONALE_MAX_CHARS].rstrip() + "..."


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


def build_per_metric(ticker: str, repo_root: Path) -> dict[str, dict[str, object]]:
    """Per-metric provenance for the §1 snapshot.

    Today the snapshot's only auditable fact-like value is the DCF row
    that drives the valuation card. We surface it as a single entry so
    the audit drawer can trace `consolidated_npv_per_share` /
    `current_price` back to the dcf_runs row that produced them. The
    shape mirrors the financial_facts provenance shape (source, fact_id,
    fetched_at) so a downstream reader doesn't branch on metric type:

        {"valuation": {"source": "dcf_run",
                        "fact_id": 12,
                        "fetched_at": "2025-12-31",
                        "valuation_date": "2025-12-31"}}

    Returns {} when no DCF row exists for the ticker.
    """
    conn = open_repo_db(repo_root)
    if conn is None:
        return {}
    try:
        if not has_table(conn, "dcf_runs"):
            return {}
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, valuation_date, created_at, live_price_at "
            "FROM dcf_runs WHERE ticker = ? "
            "ORDER BY valuation_date DESC, id DESC LIMIT 1",
            (ticker.upper(),),
        )
        row = cursor.fetchone()
    finally:
        conn.close()
    if row is None:
        return {}
    fetched_at = row["live_price_at"] or row["created_at"] or row["valuation_date"]
    return {
        "valuation": {
            "source": "dcf_run",
            "fact_id": int(row["id"]),
            "fetched_at": str(fetched_at) if fetched_at is not None else None,
            "valuation_date": str(row["valuation_date"]),
        }
    }
