"""Read-only data accessors for the P3 workspace panels.

The audit memo flagged six "captured but not surfaced" subsystems whose
data sits in portfolio.db but never reaches the workspace HTML:

  P3-17  decision history          -> decisions (alembic 0046)
  P3-18  macro sensitivities       -> macro_sensitivities (alembic 0045)
  P3-19a customer concentrations   -> customer_concentrations (alembic 0040)
  P3-19b lease commitment ladder   -> lease_commitments (alembic 0047)
  P3-20  strategic targets         -> strategic_targets (alembic 0053)
  P3-21  Say-Do verdict history    -> management_commitments (alembic 0017)

This module is the read side: pure Pydantic-shaped accessors that return
typed rows. The renderer (workspace_html.py) consumes them in a follow-on
PR — keeping the data + render concerns separate so the accessors can
be unit-tested without spinning up the full report pipeline.

All accessors are best-effort: missing table -> []. Synthetic test
environments without the matching migration applied keep working.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MacroSensitivityRow:
    """One row of macro_sensitivities for a ticker."""

    series_id: str
    beta: float
    r_squared: float | None
    lookback_window_days: int
    computed_at: datetime


@dataclass(frozen=True)
class StrategicTargetRow:
    """One row of strategic_targets — long-term mgmt commitment from an investor deck."""

    target_kind: str
    target_value: float | None
    target_unit: str
    target_period: str
    target_currency: str | None
    narrative_excerpt: str
    confidence: float


@dataclass(frozen=True)
class CustomerConcentrationRow:
    """One named customer's share of a ticker's revenue for a period."""

    fiscal_period: str
    fiscal_period_type: str
    customer_label: str
    pct_of_revenue: float
    revenue_amount: float | None
    revenue_currency: str | None


@dataclass(frozen=True)
class LeaseLadderRow:
    """One year of a lease maturity ladder."""

    fiscal_year: int
    as_of_date: date
    lease_type: str
    ladder_year: str
    ladder_calendar_year: int | None
    amount: float
    currency: str
    unit: str


@dataclass(frozen=True)
class DecisionRow:
    """One decision_audit row."""

    recommendation_kind: str
    recommendation_value: float | None
    conviction: str | None
    made_at: datetime
    outcome_pct: float | None
    outcome_at: datetime | None
    rationale_excerpt: str | None


@dataclass(frozen=True)
class DecisionHistorySummary:
    """Aggregated view: counts by (kind, conviction) + win-rate."""

    total: int
    by_kind: dict[str, int]
    by_conviction: dict[str, int]
    win_rate_overall: float | None
    rows: list[DecisionRow]


@dataclass(frozen=True)
class SayDoVerdictRow:
    """One management commitment + outcome — the P3-21 grading-overlay row.

    Field names mirror the management_commitments columns (alembic 0017)
    rather than inventing a new vocabulary: `period_made` is when management
    said it, `period_target` is the period they were guiding for, `outcome`
    is the post-match verdict.
    """

    period_made: datetime
    period_target: datetime
    kpi_name: str
    comparator: str
    target_value: float
    unit: str
    narrative: str
    realized_value: float | None
    outcome: str | None
    evaluated_at: datetime | None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open(db_path: Path | str) -> sqlite3.Connection | None:
    p = Path(db_path)
    if not p.exists():
        return None
    try:
        conn = sqlite3.connect(str(p), timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as exc:
        log.warning({"event": "p3_open_failed", "error": str(exc)})
        return None


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def _parse_dt(raw: object) -> datetime | None:
    if isinstance(raw, datetime):
        return raw
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _parse_date(raw: object) -> date | None:
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    dt = _parse_dt(raw)
    return dt.date() if dt is not None else None


# ---------------------------------------------------------------------------
# Accessors
# ---------------------------------------------------------------------------


def load_macro_sensitivities(
    ticker: str, *, db_path: Path | str
) -> list[MacroSensitivityRow]:
    """All macro_sensitivities rows for `ticker`, ordered by |beta| desc."""
    conn = _open(db_path)
    if conn is None or not _table_exists(conn, "macro_sensitivities"):
        return []
    try:
        rows = conn.execute(
            """
            SELECT series_id, beta, r_squared, lookback_window_days, computed_at
            FROM macro_sensitivities
            WHERE ticker = ?
            ORDER BY ABS(beta) DESC
            """,
            (ticker.upper(),),
        ).fetchall()
    finally:
        conn.close()
    out: list[MacroSensitivityRow] = []
    for r in rows:
        computed = _parse_dt(r["computed_at"])
        if computed is None:
            continue
        out.append(
            MacroSensitivityRow(
                series_id=str(r["series_id"]),
                beta=float(r["beta"]),
                r_squared=(float(r["r_squared"]) if r["r_squared"] is not None else None),
                lookback_window_days=int(r["lookback_window_days"]),
                computed_at=computed,
            )
        )
    return out


def load_strategic_targets(
    ticker: str, *, db_path: Path | str
) -> list[StrategicTargetRow]:
    """All strategic_targets rows for `ticker`, newest extraction first."""
    conn = _open(db_path)
    if conn is None or not _table_exists(conn, "strategic_targets"):
        return []
    try:
        rows = conn.execute(
            """
            SELECT target_kind, target_value, target_unit, target_period,
                   target_currency, narrative_excerpt, confidence, extracted_at
            FROM strategic_targets
            WHERE ticker = ?
            ORDER BY extracted_at DESC, id DESC
            """,
            (ticker.upper(),),
        ).fetchall()
    finally:
        conn.close()
    return [
        StrategicTargetRow(
            target_kind=str(r["target_kind"]),
            target_value=(float(r["target_value"]) if r["target_value"] is not None else None),
            target_unit=str(r["target_unit"]),
            target_period=str(r["target_period"]),
            target_currency=r["target_currency"],
            narrative_excerpt=str(r["narrative_excerpt"]),
            confidence=float(r["confidence"]),
        )
        for r in rows
    ]


def load_customer_concentrations(
    ticker: str, *, db_path: Path | str
) -> list[CustomerConcentrationRow]:
    """All customer_concentrations rows for `ticker`, newest period first.

    Only returns rows where pct_of_revenue >= 0.05 (5%) — the audit's
    threshold for "material concentration risk". Adjust at call site if
    you want the full table.
    """
    conn = _open(db_path)
    if conn is None or not _table_exists(conn, "customer_concentrations"):
        return []
    try:
        rows = conn.execute(
            """
            SELECT fiscal_period, fiscal_period_type, customer_label,
                   pct_of_revenue, revenue_amount, revenue_currency
            FROM customer_concentrations
            WHERE ticker = ?
              AND pct_of_revenue >= 0.05
            ORDER BY fiscal_period DESC, pct_of_revenue DESC
            """,
            (ticker.upper(),),
        ).fetchall()
    finally:
        conn.close()
    return [
        CustomerConcentrationRow(
            fiscal_period=str(r["fiscal_period"]),
            fiscal_period_type=str(r["fiscal_period_type"]),
            customer_label=str(r["customer_label"]),
            pct_of_revenue=float(r["pct_of_revenue"]),
            revenue_amount=(float(r["revenue_amount"]) if r["revenue_amount"] is not None else None),
            revenue_currency=r["revenue_currency"],
        )
        for r in rows
    ]


def load_lease_ladder(
    ticker: str, *, db_path: Path | str, lease_type: str = "operating"
) -> list[LeaseLadderRow]:
    """The most recent lease maturity ladder for `ticker`.

    Selects the max fiscal_year present in the table, then returns every
    ladder_year row for that vintage so the renderer can sort them
    Y1..Y5..Thereafter without juggling vintages.
    """
    conn = _open(db_path)
    if conn is None or not _table_exists(conn, "lease_commitments"):
        return []
    try:
        latest_year_row = conn.execute(
            """
            SELECT MAX(fiscal_year) AS fy FROM lease_commitments
            WHERE ticker = ? AND lease_type = ?
            """,
            (ticker.upper(), lease_type),
        ).fetchone()
        if latest_year_row is None or latest_year_row["fy"] is None:
            return []
        latest_fy = int(latest_year_row["fy"])
        rows = conn.execute(
            """
            SELECT fiscal_year, as_of_date, lease_type, ladder_year,
                   ladder_calendar_year, amount, currency, unit
            FROM lease_commitments
            WHERE ticker = ? AND lease_type = ? AND fiscal_year = ?
            ORDER BY
              CASE ladder_year
                WHEN 'Y1' THEN 1 WHEN 'Y2' THEN 2 WHEN 'Y3' THEN 3
                WHEN 'Y4' THEN 4 WHEN 'Y5' THEN 5
                WHEN 'Thereafter' THEN 6
                WHEN 'TotalPayments' THEN 7
                WHEN 'ImputedInterest' THEN 8
                WHEN 'LeaseLiability' THEN 9
                ELSE 10
              END
            """,
            (ticker.upper(), lease_type, latest_fy),
        ).fetchall()
    finally:
        conn.close()
    out: list[LeaseLadderRow] = []
    for r in rows:
        as_of = _parse_date(r["as_of_date"])
        if as_of is None:
            continue
        out.append(
            LeaseLadderRow(
                fiscal_year=int(r["fiscal_year"]),
                as_of_date=as_of,
                lease_type=str(r["lease_type"]),
                ladder_year=str(r["ladder_year"]),
                ladder_calendar_year=(
                    int(r["ladder_calendar_year"])
                    if r["ladder_calendar_year"] is not None
                    else None
                ),
                amount=float(r["amount"]),
                currency=str(r["currency"]),
                unit=str(r["unit"]),
            )
        )
    return out


def load_decision_history(
    ticker: str, *, db_path: Path | str, limit: int = 50
) -> DecisionHistorySummary:
    """Aggregate decision audit rows for `ticker`.

    Returns counts by recommendation_kind + conviction, an overall win-rate
    (fraction of decisions where outcome_pct > 0 OR outcome_status=='correct'),
    and the last `limit` raw rows for the time-series chart.
    """
    conn = _open(db_path)
    if conn is None or not _table_exists(conn, "decisions"):
        return DecisionHistorySummary(
            total=0,
            by_kind={},
            by_conviction={},
            win_rate_overall=None,
            rows=[],
        )
    try:
        rows = conn.execute(
            f"""
            SELECT recommendation_kind, recommendation_value, conviction,
                   made_at, outcome_pct, outcome_at, rationale_excerpt
            FROM decisions
            WHERE ticker = ?
            ORDER BY made_at DESC
            LIMIT {int(limit)}
            """,
            (ticker.upper(),),
        ).fetchall()
    finally:
        conn.close()
    by_kind: dict[str, int] = {}
    by_conviction: dict[str, int] = {}
    decisions: list[DecisionRow] = []
    wins = 0
    counted = 0
    for r in rows:
        made = _parse_dt(r["made_at"])
        if made is None:
            continue
        kind = str(r["recommendation_kind"])
        by_kind[kind] = by_kind.get(kind, 0) + 1
        if r["conviction"] is not None:
            by_conviction[str(r["conviction"])] = (
                by_conviction.get(str(r["conviction"]), 0) + 1
            )
        outcome_pct = (
            float(r["outcome_pct"]) if r["outcome_pct"] is not None else None
        )
        if outcome_pct is not None:
            counted += 1
            # ADD wins are positive return; TRIM/SELL wins are NEGATIVE return.
            if kind.upper() in ("TRIM", "SELL"):
                if outcome_pct < 0:
                    wins += 1
            else:
                if outcome_pct > 0:
                    wins += 1
        decisions.append(
            DecisionRow(
                recommendation_kind=kind,
                recommendation_value=(
                    float(r["recommendation_value"])
                    if r["recommendation_value"] is not None
                    else None
                ),
                conviction=r["conviction"],
                made_at=made,
                outcome_pct=outcome_pct,
                outcome_at=_parse_dt(r["outcome_at"]),
                rationale_excerpt=r["rationale_excerpt"],
            )
        )
    win_rate = (wins / counted) if counted else None
    return DecisionHistorySummary(
        total=len(decisions),
        by_kind=by_kind,
        by_conviction=by_conviction,
        win_rate_overall=win_rate,
        rows=decisions,
    )


@dataclass(frozen=True)
class PeerCompRow:
    """One peer's TTM metrics for the evaluation-snapshot peer-comp table."""

    peer_ticker: str
    peer_name: str | None
    market_cap_usd: float | None
    revenue_ttm_usd: float | None
    net_margin_ttm: float | None
    roic_ttm: float | None


def load_peer_comp(
    ticker: str, *, repo_root: Path, max_peers: int = 6
) -> list[PeerCompRow]:
    """Load FMP peer companies + their TTM metrics for the eval snapshot.

    Reads the cached `data/historical/fmp/{TICKER}_peers.json` for the peer
    list, then pulls each peer's `{PEER}_profile.json` + `{PEER}_key_metrics_ttm.json`
    for headline metrics. Best-effort — missing files → empty list. The
    audit's gap was that the evaluation snapshot showed only the target
    ticker's 3y baseline; this gives the screen "premium to peers" context.
    """
    import json as _json

    fmp_dir = Path(repo_root) / "data" / "historical" / "fmp"
    peers_path = fmp_dir / f"{ticker.upper()}_peers.json"
    if not peers_path.exists():
        return []
    try:
        peers_raw = _json.loads(peers_path.read_text(encoding="utf-8"))
    except (OSError, _json.JSONDecodeError):
        return []
    if not isinstance(peers_raw, list) or not peers_raw:
        return []

    peer_tickers: list[str] = []
    # FMP peer payload shapes have shifted across versions; tolerate both.
    if isinstance(peers_raw[0], dict):
        first = peers_raw[0]
        if "peersList" in first:
            raw = first.get("peersList")
            if isinstance(raw, list):
                peer_tickers = [str(p).upper() for p in raw][:max_peers]
        elif "symbol" in first:
            peer_tickers = [str(p["symbol"]).upper() for p in peers_raw][:max_peers]
    elif isinstance(peers_raw[0], str):
        peer_tickers = [str(p).upper() for p in peers_raw][:max_peers]

    out: list[PeerCompRow] = []
    for peer in peer_tickers:
        profile_path = fmp_dir / f"{peer}_profile.json"
        ttm_path = fmp_dir / f"{peer}_key_metrics_ttm.json"
        peer_name: str | None = None
        market_cap: float | None = None
        if profile_path.exists():
            try:
                profile_raw = _json.loads(profile_path.read_text(encoding="utf-8"))
                if isinstance(profile_raw, list) and profile_raw:
                    rec = profile_raw[0]
                    if isinstance(rec, dict):
                        peer_name = (
                            str(rec.get("companyName"))
                            if rec.get("companyName") is not None
                            else None
                        )
                        mc = rec.get("mktCap")
                        if isinstance(mc, (int, float)):
                            market_cap = float(mc)
            except (OSError, _json.JSONDecodeError):
                pass
        revenue: float | None = None
        net_margin: float | None = None
        roic: float | None = None
        if ttm_path.exists():
            try:
                ttm_raw = _json.loads(ttm_path.read_text(encoding="utf-8"))
                if isinstance(ttm_raw, list) and ttm_raw:
                    rec = ttm_raw[0]
                    if isinstance(rec, dict):
                        for revkey in ("revenueTTM", "revenuePerShareTTM"):
                            v = rec.get(revkey)
                            if isinstance(v, (int, float)) and revkey == "revenueTTM":
                                revenue = float(v)
                                break
                        nm = rec.get("netIncomePerRevenueTTM")
                        if isinstance(nm, (int, float)):
                            net_margin = float(nm)
                        rc = rec.get("roicTTM")
                        if isinstance(rc, (int, float)):
                            roic = float(rc)
            except (OSError, _json.JSONDecodeError):
                pass
        out.append(
            PeerCompRow(
                peer_ticker=peer,
                peer_name=peer_name,
                market_cap_usd=market_cap,
                revenue_ttm_usd=revenue,
                net_margin_ttm=net_margin,
                roic_ttm=roic,
            )
        )
    return out


def load_saydo_verdicts(
    ticker: str, *, db_path: Path | str, limit: int = 30
) -> list[SayDoVerdictRow]:
    """All management_commitments rows for `ticker` with their outcomes.

    The renderer can group by period_made to produce the per-quarter
    say-vs-do grading overlay that P3-21 calls for.
    """
    conn = _open(db_path)
    if conn is None or not _table_exists(conn, "management_commitments"):
        return []
    try:
        rows = conn.execute(
            f"""
            SELECT period_made, period_target, kpi_name, comparator,
                   target_value, unit, narrative,
                   realized_value, outcome, evaluated_at
            FROM management_commitments
            WHERE ticker = ?
            ORDER BY period_made DESC
            LIMIT {int(limit)}
            """,
            (ticker.upper(),),
        ).fetchall()
    finally:
        conn.close()
    out: list[SayDoVerdictRow] = []
    for r in rows:
        period_made = _parse_dt(r["period_made"])
        period_target = _parse_dt(r["period_target"])
        if period_made is None or period_target is None:
            continue
        out.append(
            SayDoVerdictRow(
                period_made=period_made,
                period_target=period_target,
                kpi_name=str(r["kpi_name"]),
                comparator=str(r["comparator"]),
                target_value=float(r["target_value"]),
                unit=str(r["unit"]),
                narrative=str(r["narrative"]),
                realized_value=(
                    float(r["realized_value"])
                    if r["realized_value"] is not None
                    else None
                ),
                outcome=r["outcome"],
                evaluated_at=_parse_dt(r["evaluated_at"]),
            )
        )
    return out
