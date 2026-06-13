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
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import cast

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


def load_macro_sensitivities(ticker: str, *, db_path: Path | str) -> list[MacroSensitivityRow]:
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


def load_strategic_targets(ticker: str, *, db_path: Path | str) -> list[StrategicTargetRow]:
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
            revenue_amount=(
                float(r["revenue_amount"]) if r["revenue_amount"] is not None else None
            ),
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
            by_conviction[str(r["conviction"])] = by_conviction.get(str(r["conviction"]), 0) + 1
        outcome_pct = float(r["outcome_pct"]) if r["outcome_pct"] is not None else None
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
    """One peer's TTM metrics for the evaluation-snapshot peer-comp table.

    ``match_reasons`` carries why this peer was selected (P4.2 scored
    selection) so the renderer can show the basis instead of presenting an
    unexplained list — e.g. ``("named rival", "same industry", "similar
    scale")``.
    """

    peer_ticker: str
    peer_name: str | None
    market_cap_usd: float | None
    revenue_ttm_usd: float | None
    net_margin_ttm: float | None
    roic_ttm: float | None
    match_reasons: tuple[str, ...] = ()


def _read_json(path: Path) -> object | None:
    import json as _json

    try:
        return _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, _json.JSONDecodeError):
        return None


def _profile_fields(
    fmp_dir: Path, ticker: str
) -> tuple[str | None, str | None, str | None, float | None]:
    """(companyName, sector, industry, marketCap) from a cached FMP profile.

    Tolerates both the stable-API key (``marketCap``) and the legacy v3 one
    (``mktCap``).
    """
    raw = _read_json(fmp_dir / f"{ticker}_profile.json")
    if not isinstance(raw, list) or not raw or not isinstance(raw[0], dict):
        return None, None, None, None
    rec = cast("dict[str, object]", raw[0])
    name = str(rec["companyName"]) if rec.get("companyName") is not None else None
    sector = str(rec["sector"]) if rec.get("sector") else None
    industry = str(rec["industry"]) if rec.get("industry") else None
    market_cap: float | None = None
    for key in ("marketCap", "mktCap"):
        mc = rec.get(key)
        if isinstance(mc, (int, float)):
            market_cap = float(mc)
            break
    return name, sector, industry, market_cap


def _fmp_peer_pool(fmp_dir: Path, ticker: str) -> list[tuple[str, str | None, float | None]]:
    """The raw FMP peer candidates as (symbol, name, market_cap).

    Payload shapes shifted across FMP versions; tolerate the three known
    ones. The dict shape carries companyName + mktCap per peer — kept as
    fallbacks so candidates OUTSIDE the cached-profile universe (the common
    case for foreign rivals like ITUB) can still be name-matched and
    scale-scored.
    """
    peers_raw = _read_json(fmp_dir / f"{ticker}_peers.json")
    if not isinstance(peers_raw, list) or not peers_raw:
        return []
    pool = cast("list[object]", peers_raw)
    if isinstance(pool[0], dict):
        first = cast("dict[str, object]", pool[0])
        if "peersList" in first:
            raw = first.get("peersList")
            if isinstance(raw, list):
                return [(str(p).upper(), None, None) for p in cast("list[object]", raw)]
        if "symbol" in first:
            out: list[tuple[str, str | None, float | None]] = []
            for entry in pool:
                if not isinstance(entry, dict):
                    continue
                rec = cast("dict[str, object]", entry)
                if rec.get("symbol") is None:
                    continue
                name = str(rec["companyName"]) if rec.get("companyName") is not None else None
                cap_raw = rec.get("mktCap", rec.get("marketCap"))
                cap = float(cap_raw) if isinstance(cap_raw, (int, float)) else None
                out.append((str(rec["symbol"]).upper(), name, cap))
            return out
        return []
    return [(str(p).upper(), None, None) for p in pool]


def _watchlist_names(ticker: str, repo_root: Path) -> list[str]:
    """The owner's competitive_watchlist (prose rival names) for `ticker`,
    from the thesis JSON when one exists. Best-effort."""
    raw = _read_json(Path(repo_root) / "micro_thesis" / "holdings" / f"{ticker}.json")
    if not isinstance(raw, dict):
        return []
    wl = cast("dict[str, object]", raw).get("competitive_watchlist")
    if not isinstance(wl, list):
        return []
    return [str(n) for n in cast("list[object]", wl) if isinstance(n, str) and n.strip()]


def _peer_curation(ticker: str, repo_root: Path) -> tuple[list[str], dict[str, object] | None]:
    """The owner's `curate_peers` artifacts from the thesis JSON (S5):

    - ``peer_exclude`` — rivals to drop from the shown set (ticker or name),
      the one thing the +3 ``competitive_watchlist`` pin list can't express.
    - ``peers_section_override`` — the re-evaluable hide-unless-quality
      condition modelling "remove this section UNLESS you show better peers".

    Best-effort: ``([], None)`` on any miss."""
    raw = _read_json(Path(repo_root) / "micro_thesis" / "holdings" / f"{ticker}.json")
    if not isinstance(raw, dict):
        return [], None
    payload = cast("dict[str, object]", raw)
    excl_raw = payload.get("peer_exclude")
    exclude = (
        [str(n) for n in cast("list[object]", excl_raw) if isinstance(n, str) and n.strip()]
        if isinstance(excl_raw, list)
        else []
    )
    ov_raw = payload.get("peers_section_override")
    override = cast("dict[str, object]", ov_raw) if isinstance(ov_raw, dict) else None
    return exclude, override


# A bare exchange-ticker-shaped token: leading letter, ≤7 chars total, no
# spaces. Distinguishes a pinned ticker ("HOOD") from a prose rival name
# ("Itau Unibanco") so only genuine tickers get injected into the FMP pool.
_TICKER_RX = re.compile(r"[A-Z][A-Z0-9.\-]{0,6}")


def _looks_like_ticker(token: str) -> bool:
    return bool(_TICKER_RX.fullmatch(token))


def _has_peer_metrics(row: PeerCompRow) -> bool:
    """True when a peer carries at least one computed TTM multiple — a real
    comp, not a wall-of-em-dashes row."""
    return any(
        v is not None
        for v in (row.market_cap_usd, row.revenue_ttm_usd, row.net_margin_ttm, row.roic_ttm)
    )


def evaluate_peers_override(
    override: dict[str, object] | None, rows: list[PeerCompRow]
) -> tuple[bool, dict[str, object]]:
    """Re-evaluate a persisted peers-section override against freshly scored
    rows (S5 — the owner's "remove this section UNLESS you show better peers /
    computed multiples", modelled as an actionable, re-checkable condition
    rather than verbatim-logged text).

    A peer counts as *quality* when it satisfies the override's requirements:
    ``require_named`` (the owner vouched for it via the watchlist) and/or
    ``require_metrics`` (it carries at least one computed TTM multiple). The
    section is hidden only while fewer than ``min_quality_peers`` qualify; the
    moment enough credible rivals are pinned the condition flips and the panel
    returns — no manual un-hide, the directive's "act on the condition".

    Returns ``(hide, detail)``. ``hide`` is always False for a missing /
    malformed override or any ``action`` other than ``"hide"``.
    """
    if not isinstance(override, dict) or override.get("action") != "hide":
        return False, {"evaluated": False}
    require_named = bool(override.get("require_named", True))
    require_metrics = bool(override.get("require_metrics", True))
    raw_min = override.get("min_quality_peers", 2)
    min_quality = max(1, int(raw_min) if isinstance(raw_min, (int, float)) else 2)

    quality = sum(
        1
        for r in rows
        if (not require_named or "named rival" in r.match_reasons)
        and (not require_metrics or _has_peer_metrics(r))
    )
    hide = quality < min_quality
    return hide, {
        "evaluated": True,
        "quality_peers": quality,
        "min_quality_peers": min_quality,
        "require_named": require_named,
        "require_metrics": require_metrics,
        "satisfied": not hide,
    }


def _normalize_name(name: str) -> str:
    """Accent-stripped, lowercased, alnum-only — for rival-name matching."""
    import unicodedata

    decomposed = unicodedata.normalize("NFKD", name)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    return "".join(ch for ch in ascii_only.lower() if ch.isalnum() or ch == " ").strip()


def _is_named_rival(company_name: str | None, watchlist: list[str]) -> bool:
    if not company_name or not watchlist:
        return False
    norm_company = _normalize_name(company_name)
    for rival in watchlist:
        norm_rival = _normalize_name(rival)
        if norm_rival and (norm_rival in norm_company or norm_company in norm_rival):
            return True
    return False


def load_peer_comp(ticker: str, *, repo_root: Path, max_peers: int = 6) -> list[PeerCompRow]:
    """Scored comparable selection for the eval snapshot (P4.2; tightened PR7).

    The raw FMP peer list is a sector/market-cap screen whose alphabetical
    head is frequently wrong (the owner flagged NU → Barclays, NOW → Applied
    Materials). The pool usually *contains* the right names, so instead of
    taking the first ``max_peers`` we score every candidate:

      +3  named in the owner's competitive_watchlist (thesis JSON)
      +2  same FMP industry as the target
      +2  a TRACKED company (portfolio/evaluation) — relevance signal, and
          its metrics resolve from our own cached data
      +1  same FMP sector
      +1  market cap within 0.2x – 5x of the target

    and keep the top ``max_peers`` that score >= 1. PR7 additionally drops
    candidates that pass the screen but carry NO resolvable metrics at all
    (cap, revenue, margin, ROIC all absent) — those rows rendered as a wall
    of em-dashes (the owner's "shit peers" UBER table: six semicap/SaaS names
    with every cell blank). A named rival survives even metric-less (it's
    informative by itself). Empty result → the renderer hides the panel.

    S5 (steerable peers) layers the owner's `curate_peers` directives on top of
    the screen:
      - a pinned bare TICKER absent from the FMP pool is INJECTED so an
        explicit pin always shows (the screen routinely omits a deliberately
        chosen rival); name pins keep working via the +3 watchlist match;
      - ``peer_exclude`` drops a rival from the shown set by ticker or name;
      - ``peers_section_override`` is re-evaluated here so the "remove unless
        better peers" condition hides — and later un-hides — the whole panel
        on its own as the pinned set crosses the quality bar.
    """
    fmp_dir = Path(repo_root) / "data" / "historical" / "fmp"
    ticker = ticker.upper()
    pool = _fmp_peer_pool(fmp_dir, ticker)

    _, target_sector, target_industry, target_cap = _profile_fields(fmp_dir, ticker)
    watchlist = _watchlist_names(ticker, repo_root)
    peer_exclude, override = _peer_curation(ticker, repo_root)
    tracked = _tracked_tickers(repo_root)

    # Inject pinned tickers the FMP screen omitted (resolve cached metrics if we
    # have them). Only ticker-shaped, already-uppercase watchlist entries are
    # treated as injectable pins — a prose name ("Itau Unibanco") still scores
    # purely via the name match below, never as a fabricated pool ticker.
    pool_tickers = {p[0] for p in pool}
    pinned_tickers: set[str] = set()
    for entry in watchlist:
        tok = entry.strip()
        if tok and tok == tok.upper() and tok != ticker and _looks_like_ticker(tok):
            pinned_tickers.add(tok)
            if tok not in pool_tickers:
                pool.append((tok, None, None))
                pool_tickers.add(tok)

    if not pool:
        return []

    exclude_tickers = {e.strip().upper() for e in peer_exclude}
    exclude_names = {_normalize_name(e) for e in peer_exclude if e.strip()}

    scored: list[tuple[float, int, str, str | None, float | None, tuple[str, ...], bool]] = []
    for idx, (peer, pool_name, pool_cap) in enumerate(pool):
        if peer == ticker:
            continue
        prof_name, peer_sector, peer_industry, prof_cap = _profile_fields(fmp_dir, peer)
        # Foreign/untracked rivals usually have no cached profile — fall back
        # to the identity the peers payload itself carries.
        peer_name = prof_name or pool_name
        peer_cap = prof_cap if prof_cap is not None else pool_cap
        # Owner exclusion drops a rival from the shown set by ticker or name,
        # however well it would otherwise score.
        if peer in exclude_tickers or (peer_name and _normalize_name(peer_name) in exclude_names):
            continue
        score = 0.0
        reasons: list[str] = []
        named = _is_named_rival(peer_name, watchlist) or peer in pinned_tickers
        if named:
            score += 3
            reasons.append("named rival")
        if target_industry and peer_industry == target_industry:
            score += 2
            reasons.append("same industry")
        elif target_sector and peer_sector == target_sector:
            score += 1
            reasons.append("same sector")
        if peer in tracked:
            score += 2
            reasons.append("tracked")
        if target_cap and peer_cap and 0.2 <= peer_cap / target_cap <= 5.0:
            score += 1
            reasons.append("similar scale")
        if score < 1:
            continue
        # Tiebreak: original FMP order (idx) keeps the sort deterministic.
        scored.append((score, idx, peer, peer_name, peer_cap, tuple(reasons), named))

    scored.sort(key=lambda t: (-t[0], t[1]))

    out: list[PeerCompRow] = []
    for _score, _idx, peer, peer_name, peer_cap, row_reasons, named in scored:
        if len(out) >= max_peers:
            break
        row = PeerCompRow(
            peer_ticker=peer,
            peer_name=peer_name,
            market_cap_usd=peer_cap,
            revenue_ttm_usd=_peer_revenue_ttm(fmp_dir, peer),
            net_margin_ttm=_peer_net_margin_ttm(fmp_dir, peer),
            roic_ttm=_peer_roic_ttm(fmp_dir, peer),
            match_reasons=row_reasons,
        )
        has_metrics = any(
            v is not None
            for v in (row.market_cap_usd, row.revenue_ttm_usd, row.net_margin_ttm, row.roic_ttm)
        )
        if not has_metrics and not named:
            continue  # an all-dash row says nothing — hide-don't-stub
        out.append(row)

    # The owner's "remove this section unless better peers" condition is
    # re-evaluated every build: hide the whole panel while too few credible
    # comps qualify, return it the moment enough are pinned (S5).
    hide, _detail = evaluate_peers_override(override, out)
    if hide:
        return []
    return out


def _tracked_tickers(repo_root: Path) -> set[str]:
    """Active tracked symbols (portfolio + evaluation) — best-effort, {} on
    any miss. A tracked peer is both a relevance signal and one whose data
    we already cache locally."""
    db = Path(repo_root) / "data" / "portfolio.db"
    if not db.exists():
        return set()
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return set()
    try:
        rows = conn.execute(
            "SELECT ticker FROM tracked_companies "
            "WHERE archived_at IS NULL AND list_type IN ('portfolio', 'evaluation')"
        ).fetchall()
        return {str(r[0]).upper() for r in rows}
    except sqlite3.Error:
        return set()
    finally:
        conn.close()


def _first_record(path: Path) -> dict[str, object] | None:
    """First record of a list-of-dicts FMP JSON; None on any other shape."""
    raw = _read_json(path)
    if isinstance(raw, list) and raw and isinstance(raw[0], dict):
        return cast("dict[str, object]", raw[0])
    return None


def _peer_revenue_ttm(fmp_dir: Path, peer: str) -> float | None:
    """TTM revenue: sum of the 4 newest quarterly income-statement rows
    (stable shape); falls back to the legacy v3 ``revenueTTM`` key."""
    inc = _read_json(fmp_dir / f"{peer}_income_statement_quarterly.json")
    if isinstance(inc, list) and inc:
        vals: list[float] = []
        for entry in cast("list[object]", inc)[:4]:
            if not isinstance(entry, dict):
                continue
            rev = cast("dict[str, object]", entry).get("revenue")
            if isinstance(rev, (int, float)):
                vals.append(float(rev))
        if vals:
            return sum(vals)
    ttm = _first_record(fmp_dir / f"{peer}_key_metrics_ttm.json")
    if ttm is not None:
        v = ttm.get("revenueTTM")
        if isinstance(v, (int, float)):
            return float(v)
    return None


def _peer_net_margin_ttm(fmp_dir: Path, peer: str) -> float | None:
    """Net margin TTM: stable ``financial_ratios_ttm.netProfitMarginTTM``,
    legacy v3 ``key_metrics_ttm.netIncomePerRevenueTTM`` as fallback."""
    ratios = _first_record(fmp_dir / f"{peer}_financial_ratios_ttm.json")
    if ratios is not None:
        v = ratios.get("netProfitMarginTTM")
        if isinstance(v, (int, float)):
            return float(v)
    ttm = _first_record(fmp_dir / f"{peer}_key_metrics_ttm.json")
    if ttm is not None:
        v = ttm.get("netIncomePerRevenueTTM")
        if isinstance(v, (int, float)):
            return float(v)
    return None


def _peer_roic_ttm(fmp_dir: Path, peer: str) -> float | None:
    """ROIC TTM: stable ``returnOnInvestedCapitalTTM``, legacy ``roicTTM``."""
    ttm = _first_record(fmp_dir / f"{peer}_key_metrics_ttm.json")
    if ttm is not None:
        for key in ("returnOnInvestedCapitalTTM", "roicTTM"):
            v = ttm.get(key)
            if isinstance(v, (int, float)):
                return float(v)
    return None


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
                    float(r["realized_value"]) if r["realized_value"] is not None else None
                ),
                outcome=r["outcome"],
                evaluated_at=_parse_dt(r["evaluated_at"]),
            )
        )
    return out
