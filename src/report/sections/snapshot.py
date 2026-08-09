"""§1 Executive snapshot — thesis verdict + valuation card + tier-1 KPI strip.

Current price is read in priority order:
  1. data_rules.current_price_override in the holdings JSON (manual override)
  2. data/historical/fmp/{TICKER}_price_chart_*.json (most recent close)
  3. None (rendered as "—")
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Literal, cast

from report.models import (
    DecisionBadge,
    KpiSnapshotRow,
    PricedInCard,
    PricedInLever,
    SectionStatus,
    SnapshotSection,
    ValuationSnapshot,
)
from report.rules import load_rules
from report.sections._common import has_table, missing, open_repo_db
from report.sections.thesis import _split_stub_warning


def build(
    ticker: str,
    repo_root: Path,
    model_link: str | None,
    *,
    held: bool = False,
    conn: sqlite3.Connection | None = None,
) -> SnapshotSection:
    holdings = _read_holdings(ticker, repo_root)
    rules = load_rules(ticker, repo_root)
    company_name = _company_name(ticker, repo_root, conn=conn)
    mos_bar = _mos_bar(holdings)
    sheet_url = _linked_sheet_url(holdings)
    current_price = rules.current_price_override or _latest_price(ticker, repo_root)
    valuation = _valuation_snapshot(
        ticker,
        repo_root,
        current_price,
        model_link,
        mos_bar,
        sheet_url,
        held=held,
        conn=conn,
    )
    verdict, verdict_as_of = _verdict(ticker, repo_root, conn=conn)
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
        verdict_as_of=verdict_as_of,
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


def _company_name(
    ticker: str,
    repo_root: Path,
    *,
    conn: sqlite3.Connection | None = None,
) -> str | None:
    db_conn = open_repo_db(repo_root, conn)
    if db_conn is None:
        return None
    cursor = db_conn.cursor()
    cursor.execute(
        "SELECT name FROM tracked_companies WHERE ticker = ? LIMIT 1",
        (ticker.upper(),),
    )
    row = cursor.fetchone()
    if conn is None:
        db_conn.close()
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
    conn: sqlite3.Connection | None = None,
) -> ValuationSnapshot:
    db_conn = open_repo_db(repo_root, conn)
    if db_conn is None or not has_table(db_conn, "dcf_runs"):
        if db_conn is not None and conn is None:
            db_conn.close()
        return ValuationSnapshot(
            current_price=current_price,
            model_link=model_link,
            sheet_url=sheet_url,
            mos_bar=mos_bar,
        )

    cursor = db_conn.cursor()
    # The sync-outcome columns are migration 0091 — read them only when the
    # DB has them so briefs still build against an older repo data dir.
    cols = {str(r[1]) for r in cursor.execute("PRAGMA table_info(dcf_runs)")}
    has_sync_cols = "assumptions_sync_status" in cols and "assumptions_synced_at" in cols
    sync_select = ", assumptions_sync_status, assumptions_synced_at" if has_sync_cols else ""
    has_sanity_col = "sanity_flag" in cols
    sanity_select = ", sanity_flag" if has_sanity_col else ""
    # ORDER BY matters: the versioned dcf_runs schema (migration 0137) keeps
    # superseded history rows per ticker, so a bare LIMIT 1 reads an ARBITRARY
    # (in practice the oldest) run — the card would render a stale valuation and
    # miss the priced_in/scenario_prior blocks the newest refresh persisted.
    # Same current-run selection every other dcf_runs reader already uses
    # (build_per_metric below, synthesis lenses, standup signals).
    cursor.execute(
        f"""
        SELECT valuation_date, wacc, terminal_growth, npv, npv_per_share,
               shares_outstanding, breakdown_json,
               live_price, live_price_at, over_under_pct, mos_bar_used,
               assumption_snapshot_json{sync_select}{sanity_select}
        FROM dcf_runs
        WHERE ticker = ?
        ORDER BY valuation_date DESC, id DESC
        LIMIT 1
        """,
        (ticker.upper(),),
    )
    row = cursor.fetchone()
    if conn is None:
        db_conn.close()
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
    bull_fv, bear_fv = _scenario_range(row["assumption_snapshot_json"])
    sp_weights, sp_rationale, sp_set_by = _scenario_prior_card(row["assumption_snapshot_json"])
    exp_return, skew = _scenario_ev_skew(
        live_price, cons_npv_per_share, row["assumption_snapshot_json"]
    )
    sync_status = (
        str(row["assumptions_sync_status"])
        if has_sync_cols and row["assumptions_sync_status"] is not None
        else None
    )
    synced_at = _parse_iso_datetime(row["assumptions_synced_at"]) if has_sync_cols else None
    sanity_flag = (
        str(row["sanity_flag"]) if has_sanity_col and row["sanity_flag"] is not None else None
    )

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
        bull_npv_per_share=bull_fv,
        bear_npv_per_share=bear_fv,
        valuation_model_label=_model_label(row["assumption_snapshot_json"]),
        priced_in=_priced_in(row["assumption_snapshot_json"]),
        scenario_weights=sp_weights,
        scenario_rationale=sp_rationale,
        scenario_set_by=sp_set_by,
        scenario_expected_return=exp_return,
        scenario_skew=skew,
        assumptions_sync_status=sync_status,
        assumptions_synced_at=synced_at,
        sanity_flag=sanity_flag,
    )


def _scenario_range(snapshot_json: object) -> tuple[float | None, float | None]:
    """(bull, bear) per-share fair values from a dcf_runs assumption snapshot.

    The "scenarios" block is written by execution/refresh_dcf.py for the
    redesigned FCFF archetype; rows that predate it (or other archetypes) have
    none — both values None and the card keeps its single-point readout. An
    un-valuable scenario is persisted as null and stays None here.
    """
    if not isinstance(snapshot_json, str) or not snapshot_json:
        return None, None
    try:
        data: object = json.loads(snapshot_json)
    except ValueError:
        return None, None
    if not isinstance(data, dict):
        return None, None
    scenarios = cast("dict[str, object]", data).get("scenarios")
    if not isinstance(scenarios, dict):
        return None, None

    def fair_value(key: str) -> float | None:
        block = cast("dict[str, object]", scenarios).get(key)
        if not isinstance(block, dict):
            return None
        v = cast("dict[str, object]", block).get("fair_value_per_share_usd")
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    return fair_value("bull"), fair_value("bear")


def _priced_in(snapshot_json: object) -> PricedInCard | None:
    """The reverse-DCF "what's priced in" block from a dcf_runs assumption
    snapshot, or None.

    Written by execution/refresh_dcf.py (``dcf.reverse.PricedIn.to_snapshot_dict``)
    for redesigned FCFF names only; bespoke archetypes and rows predating the
    block carry none. Fully tolerant — a malformed/partial block returns None so
    the card degrades to its single-point readout rather than raising. Never
    recomputes: the card is a pure consumer of what refresh_dcf persisted.
    """
    if not isinstance(snapshot_json, str) or not snapshot_json:
        return None
    try:
        data: object = json.loads(snapshot_json)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    block = cast("dict[str, object]", data).get("priced_in")
    if not isinstance(block, dict):
        return None
    b = cast("dict[str, object]", block)

    def _lever(key: str) -> PricedInLever | None:
        raw = b.get(key)
        if not isinstance(raw, dict):
            return None
        lv = cast("dict[str, object]", raw)
        lever = lv.get("lever")
        label = lv.get("label")
        unit = lv.get("unit")
        base = lv.get("base_value")
        if (
            not isinstance(lever, str)
            or not isinstance(label, str)
            or unit not in ("pct", "turns")
            or not isinstance(base, (int, float))
            or isinstance(base, bool)
        ):
            return None
        implied = lv.get("implied_value")
        implied_f = (
            float(implied)
            if isinstance(implied, (int, float)) and not isinstance(implied, bool)
            else None
        )
        note = lv.get("note")
        return PricedInLever(
            lever=lever,
            label=label,
            unit=unit,
            base_value=float(base),
            implied_value=implied_f,
            note=note if isinstance(note, str) else "",
        )

    price = b.get("price")
    base_val = b.get("base_value_per_share_usd")
    growth = _lever("growth")
    terminal = _lever("terminal")
    if (
        not isinstance(price, (int, float))
        or isinstance(price, bool)
        or not isinstance(base_val, (int, float))
        or isinstance(base_val, bool)
        or growth is None
        or terminal is None
    ):
        return None
    return PricedInCard(
        price=float(price),
        base_value_per_share_usd=float(base_val),
        growth=growth,
        terminal=terminal,
    )


def _scenario_prior_card(
    snapshot_json: object,
) -> tuple[dict[str, float] | None, str | None, str | None]:
    """(weights, rationale, set_by) from the dcf_runs ``scenario_prior`` block, for
    the valuation card. Weights are the normalized Bull/Base/Bear simplex (via the
    single producer ``dcf.scenario_reward.parse_scenario_prior_weights``); rationale
    is the LLM's (None when empty); set_by is "llm"/"owner"/"global". All None when
    the run carries no scenario_prior block."""
    from dcf.scenario_reward import parse_scenario_prior_weights

    weights = parse_scenario_prior_weights(snapshot_json)
    if weights is None:
        return None, None, None
    rationale: str | None = None
    set_by: str | None = None
    if isinstance(snapshot_json, str) and snapshot_json:
        try:
            data: object = json.loads(snapshot_json)
        except ValueError:
            data = None
        if isinstance(data, dict):
            block = cast("dict[str, object]", data).get("scenario_prior")
            if isinstance(block, dict):
                b = cast("dict[str, object]", block)
                r = b.get("rationale")
                rationale = r.strip() if isinstance(r, str) and r.strip() else None
                sb = b.get("set_by")
                set_by = sb if isinstance(sb, str) and sb else None
    return weights, rationale, set_by


def _scenario_ev_skew(
    price: float | None, base_fv: float | None, snapshot_json: object
) -> tuple[float | None, float | None]:
    """The probability-weighted expected value E[V] + its skew vs the base point
    estimate, for the card (``dcf.scenario_reward``). Both None unless the run
    carries a real scenario range (tails) — a point estimate has no skew to show."""
    from dcf.scenario_reward import scenario_reward

    reward = scenario_reward(price=price, base_fv=base_fv, snapshot_json=snapshot_json)
    if reward is None or not reward.has_scenarios:
        return None, None
    return reward.expected_return, reward.skew


# Snapshot "model" tag (stamped by the bespoke builders) → card-facing label.
# The redesigned FCFF refresher stamps format="redesign" instead of a model tag.
_MODEL_LABELS = {
    "holdco_sotp": "SOTP / NAV",
    "holdco_sotp_brk": "SOTP / NAV",
    "bank_excess_return": "Excess return",
    "platform_dcf": "Platform DCF",
    "fintech_sotp": "Fintech SOTP",
}


def _model_label(snapshot_json: object) -> str | None:
    """Human label for the valuation archetype that produced a dcf_runs row,
    so the card says which model the number came from. An unrecognized model
    tag passes through raw (a new archetype labels itself); rows predating
    model tagging return None and the card stays generic."""
    if not isinstance(snapshot_json, str) or not snapshot_json:
        return None
    try:
        data: object = json.loads(snapshot_json)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    payload = cast("dict[str, object]", data)
    model = payload.get("model")
    if isinstance(model, str) and model:
        return _MODEL_LABELS.get(model, model)
    if payload.get("format") == "redesign":
        return "FCFF DCF"
    return None


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


def _verdict(
    ticker: str,
    repo_root: Path,
    *,
    conn: sqlite3.Connection | None = None,
) -> tuple[Literal["intact", "watch", "broken", "pending"], datetime | None]:
    """(verdict, as_of) — ``as_of`` is ``thesis_state.last_updated``, the
    timestamp ``persist_verdict`` writes from ``verdict.evaluated_at``. The
    chrome badge greys a verdict that predates the newest reported quarter
    instead of rendering a stale read as fresh green."""
    db_conn = open_repo_db(repo_root, conn)
    if db_conn is None or not has_table(db_conn, "thesis_state"):
        if db_conn is not None and conn is None:
            db_conn.close()
        return "pending", None
    cursor = db_conn.cursor()
    cursor.execute(
        "SELECT breach_status, last_updated FROM thesis_state WHERE ticker = ?",
        (ticker.upper(),),
    )
    row = cursor.fetchone()
    if conn is None:
        db_conn.close()
    if row is None or row["breach_status"] is None:
        return "pending", None
    as_of = _parse_iso_datetime(row["last_updated"])
    status = row["breach_status"].lower()
    if status == "ok":
        return "intact", as_of
    if status == "warn":
        return "watch", as_of
    if status == "breach":
        return "broken", as_of
    return "pending", as_of


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
        outcome = cast('Literal["correct", "wrong", "mixed", "unfalsifiable", "pending"]', raw)
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


def build_per_metric(
    ticker: str,
    repo_root: Path,
    *,
    conn: sqlite3.Connection | None = None,
) -> dict[str, dict[str, object]]:
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
    db_conn = open_repo_db(repo_root, conn)
    if db_conn is None:
        return {}
    try:
        if not has_table(db_conn, "dcf_runs"):
            return {}
        cursor = db_conn.cursor()
        cursor.execute(
            "SELECT id, valuation_date, created_at, live_price_at "
            "FROM dcf_runs WHERE ticker = ? "
            "ORDER BY valuation_date DESC, id DESC LIMIT 1",
            (ticker.upper(),),
        )
        row = cursor.fetchone()
    finally:
        if conn is None:
            db_conn.close()
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
