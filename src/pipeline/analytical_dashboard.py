"""Analytical portfolio dashboard — cross-ticker view layered on top of dashboard_status.

The Phase 7 deliverable. Whereas dashboard_status.py renders a per-ticker
status table (FMP last-pulled, last build, comments count), this module
adds the cross-ticker analytical panels:

  1. Trigger ladder    — every holding's DCF over/under vs MoS bar,
                          positioned on a SELL ↔ HOLD ↔ ADD rail.
  2. Recent insider     — open-market buys + cluster events across the
                          portfolio/watchlist in last 90d, ranked by
                          conviction signal.
  3. Predictions outcomes — cross-ticker win-rate by source_kind from
                          the predictions table.
  4. Exec comp alignment — when populated: rank holdings by whether
                          comp design rewards thesis KPIs.

Pure reads from the unified data model. The renderer in
`pipeline.dashboard_html` consumes the dict this module returns.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path


@dataclass(slots=True)
class TriggerLadderRow:
    ticker: str
    list_type: str
    over_under_pct: float | None
    mos_bar: float | None
    trigger_status: str | None  # 'sell' | 'trim' | 'hold' | 'initiate_candidate'
    live_price: float | None
    dcf_fair_value: float | None
    verdict: str | None  # from thesis_state


@dataclass(slots=True)
class InsiderEventRow:
    ticker: str
    insider_name: str
    insider_role: str | None
    transaction_date: str
    transaction_type: str
    shares: float
    transaction_value: float | None
    signal_strength: float
    rationale: str


@dataclass(slots=True)
class PredictionOutcomeRow:
    ticker: str
    source_kind: str
    outcome: str
    count: int


@dataclass(slots=True)
class PortfolioLensRow:
    """One per-holding 5-min-reread (or other ticker lens) for cross-ticker
    scanning in the dashboard."""

    ticker: str
    lens: str
    content_md: str
    generated_at: str


@dataclass(slots=True)
class DecisionRow:
    """One recent recommendation from the decisions ledger."""

    id: int
    ticker: str
    recommendation_kind: str
    recommendation_value: float | None
    conviction: str | None
    made_at: str
    outcome_label: str | None
    outcome_pct: float | None
    rationale_excerpt: str | None


@dataclass(slots=True)
class DecisionsPanel:
    """Aggregated decisions view for the dashboard."""

    recent: list[DecisionRow] = field(default_factory=list)
    hit_rate_by_kind: dict[str, dict[str, int]] = field(default_factory=dict)
    calibration_by_conviction: dict[str, dict[str, int]] = field(default_factory=dict)


@dataclass(slots=True)
class LlmBudgetRow:
    """One row of the LLM Spend & Budget panel — per-purpose monthly cap
    + current burn. Populated from the llm_budgets + llm_calls join.
    `headroom_pct` is 1.0 at zero spend and goes negative when over cap."""

    purpose: str
    monthly_cap_usd: float
    current_spend_usd: float
    headroom_pct: float
    warn_threshold_pct: float
    hard_block: bool
    on_exceed: str = "warn"  # cap-exceeded mode (migration 0066): skip | block | warn


@dataclass(slots=True)
class LlmTickerSpendRow:
    """Month-to-date LLM spend for one ticker — the by-ticker companion to the
    by-purpose caps. Aggregated over ALL llm_calls (every purpose, budgeted or
    not), so the by-ticker total can exceed the capped by-purpose total. Calls
    with no ticker attribution land in the synthetic '(unattributed)' bucket."""

    ticker: str
    current_spend_usd: float
    call_count: int


@dataclass(slots=True)
class LlmBudgetPanel:
    """The dashboard's LLM Spend & Budget section — per-purpose rows plus
    the month-to-date totals + projected month-end. Returns empty rows + 0
    totals when the budget tables aren't present (pre-migration repos)."""

    rows: list[LlmBudgetRow] = field(default_factory=list)
    total_spend_mtd_usd: float = 0.0
    projected_month_end_usd: float = 0.0
    month_label: str = ""  # 'YYYY-MM'
    by_ticker: list[LlmTickerSpendRow] = field(default_factory=list)


@dataclass(slots=True)
class AnalyticalDashboard:
    trigger_ladder: list[TriggerLadderRow] = field(default_factory=list)
    insider_events: list[InsiderEventRow] = field(default_factory=list)
    prediction_outcomes: list[PredictionOutcomeRow] = field(default_factory=list)
    portfolio_synthesis_md: str | None = None  # cross_portfolio_synthesis lens output
    per_ticker_reread: list[PortfolioLensRow] = field(default_factory=list)
    decisions: DecisionsPanel = field(default_factory=DecisionsPanel)
    llm_budgets: LlmBudgetPanel = field(default_factory=lambda: LlmBudgetPanel())

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable view for the live ``GET /api/overview`` endpoint.

        Every field is a primitive / list / nested dataclass of primitives —
        the budget panel already converts ``Decimal`` caps to ``float`` at
        build time — so ``dataclasses.asdict`` recurses to a clean JSON-able
        dict with no boundary surprises (Flask's ``jsonify`` chokes on Decimal,
        not on these)."""
        return asdict(self)


# Section keys accepted by ``build_analytical_dashboard(sections=...)``. Each maps
# to one AnalyticalDashboard field / sub-builder so the unified command-center
# shell can lazy-load a single panel without running the other (heavy) queries.
DASHBOARD_SECTIONS: frozenset[str] = frozenset(
    {
        "trigger_ladder",
        "insider_events",
        "prediction_outcomes",
        "portfolio_synthesis",
        "rereads",
        "decisions",
        "llm_budgets",
    }
)


def build_analytical_dashboard(
    db_path: Path,
    *,
    insider_window_days: int = 90,
    insider_top_n: int = 25,
    list_types: tuple[str, ...] = ("portfolio", "watchlist"),
    sections: set[str] | frozenset[str] | None = None,
    ticker: str | None = None,
) -> AnalyticalDashboard:
    """Build the cross-ticker analytical dashboard.

    ``sections`` selects which panels to build (keys in ``DASHBOARD_SECTIONS``);
    unrequested panels keep their empty dataclass default. ``None`` (the default)
    builds everything — the existing behaviour the static export + ``/api/overview``
    rely on. Per-panel selection lets the shell's ``/api/panel/<name>`` fetch one
    panel without running the other six sub-queries (e.g. the 500-row insider scan).

    ``ticker`` scopes the dropdown-driven panels (pre-reads + insiders) to one
    name; the other panels ignore it.
    """
    if not db_path.exists():
        return AnalyticalDashboard()

    def want(name: str) -> bool:
        return sections is None or name in sections

    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        return AnalyticalDashboard(
            trigger_ladder=(
                _build_trigger_ladder(conn, list_types) if want("trigger_ladder") else []
            ),
            insider_events=(
                _build_insider_events(
                    conn, insider_window_days, insider_top_n, list_types, ticker=ticker
                )
                if want("insider_events")
                else []
            ),
            prediction_outcomes=(
                _build_prediction_outcomes(conn, list_types) if want("prediction_outcomes") else []
            ),
            portfolio_synthesis_md=(
                _load_portfolio_synthesis(conn) if want("portfolio_synthesis") else None
            ),
            per_ticker_reread=(
                _load_per_ticker_rereads(conn, ticker=ticker) if want("rereads") else []
            ),
            decisions=(_build_decisions_panel(conn) if want("decisions") else DecisionsPanel()),
            llm_budgets=(
                _build_llm_budget_panel(conn) if want("llm_budgets") else LlmBudgetPanel()
            ),
        )
    finally:
        conn.close()


def _build_decisions_panel(conn: sqlite3.Connection, *, recent_limit: int = 30) -> DecisionsPanel:
    """Recent decisions table + hit-rate aggregates + calibration distribution.
    Returns an empty panel when the decisions table is absent (pre-migration)."""
    has_decisions = (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='decisions'"
        ).fetchone()
        is not None
    )
    if not has_decisions:
        return DecisionsPanel()
    recent_rows = conn.execute(
        """
        SELECT id, ticker, recommendation_kind, recommendation_value, conviction,
               made_at, outcome_label, outcome_pct, rationale_excerpt
        FROM decisions
        ORDER BY made_at DESC
        LIMIT ?
        """,
        (recent_limit,),
    ).fetchall()
    recent = [
        DecisionRow(
            id=int(r["id"]),
            ticker=str(r["ticker"]),
            recommendation_kind=str(r["recommendation_kind"]),
            recommendation_value=_f(r["recommendation_value"]),
            conviction=r["conviction"],
            made_at=str(r["made_at"])[:19],
            outcome_label=r["outcome_label"],
            outcome_pct=_f(r["outcome_pct"]),
            rationale_excerpt=r["rationale_excerpt"],
        )
        for r in recent_rows
    ]
    kind_rows = conn.execute(
        """
        SELECT recommendation_kind,
               COALESCE(outcome_label, 'pending') AS outcome_label,
               COUNT(*) AS n
        FROM decisions
        WHERE made_at >= date('now', '-365 days')
        GROUP BY recommendation_kind, outcome_label
        """
    ).fetchall()
    hit_rate: dict[str, dict[str, int]] = {}
    for r in kind_rows:
        hit_rate.setdefault(str(r["recommendation_kind"]), {})[str(r["outcome_label"])] = int(
            r["n"]
        )
    conv_rows = conn.execute(
        """
        SELECT COALESCE(conviction, 'unstated') AS conv,
               COALESCE(outcome_label, 'pending') AS outcome_label,
               COUNT(*) AS n
        FROM decisions
        WHERE made_at >= date('now', '-180 days')
        GROUP BY conv, outcome_label
        """
    ).fetchall()
    calibration: dict[str, dict[str, int]] = {}
    for r in conv_rows:
        calibration.setdefault(str(r["conv"]), {})[str(r["outcome_label"])] = int(r["n"])
    return DecisionsPanel(
        recent=recent,
        hit_rate_by_kind=hit_rate,
        calibration_by_conviction=calibration,
    )


def _mode_from_row(row: sqlite3.Row, *, has_mode: bool, hard_block: bool) -> str:
    """Read the on_exceed mode from a budget row, deriving it from the legacy
    hard_block bool on pre-0066 DBs (column absent)."""
    if has_mode:
        v = row["on_exceed"]
        if isinstance(v, str) and v in ("skip", "block", "warn"):
            return v
    return "block" if hard_block else "warn"


def _build_llm_budget_panel(conn: sqlite3.Connection) -> LlmBudgetPanel:
    """Cross-purpose LLM spend vs cap. Empty panel when llm_budgets is missing
    (pre-migration repo) so the dashboard still renders.

    Projected month-end = spend MTD × (days in month / days elapsed). Linear
    extrapolation only — good enough to flag "burning too fast" without
    modeling weekly cadence."""
    has_budgets = (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='llm_budgets'"
        ).fetchone()
        is not None
    )
    if not has_budgets:
        return LlmBudgetPanel()
    now = datetime.now(UTC)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_label = f"{now.year:04d}-{now.month:02d}"
    has_mode = (
        conn.execute(
            "SELECT 1 FROM pragma_table_info('llm_budgets') WHERE name = 'on_exceed'"
        ).fetchone()
        is not None
    )
    if has_mode:
        sql = """
        SELECT b.purpose, b.monthly_cap_usd, b.warn_threshold_pct, b.hard_block,
               b.on_exceed, COALESCE(SUM(c.cost_estimate_usd), 0.0) AS spend
        FROM llm_budgets b
        LEFT JOIN llm_calls c ON c.purpose = b.purpose AND c.called_at >= ?
        GROUP BY b.purpose
        """
    else:
        sql = """
        SELECT b.purpose, b.monthly_cap_usd, b.warn_threshold_pct, b.hard_block,
               COALESCE(SUM(c.cost_estimate_usd), 0.0) AS spend
        FROM llm_budgets b
        LEFT JOIN llm_calls c ON c.purpose = b.purpose AND c.called_at >= ?
        GROUP BY b.purpose
        """
    rows = conn.execute(sql, (month_start.isoformat(),)).fetchall()
    panel_rows: list[LlmBudgetRow] = []
    total_spend = 0.0
    for r in rows:
        cap = float(r["monthly_cap_usd"])
        spend = float(r["spend"] or 0.0)
        # Don't count the __default__ row in the totals — it's a fallback
        # cap for unknown purposes, not a separate spend bucket. Its spend
        # is always 0.0 (no LLM call sets purpose="__default__"), but be
        # explicit so future readers don't have to derive it.
        if r["purpose"] != "__default__":
            total_spend += spend
        headroom = 1.0 - (spend / cap) if cap > 0 else 1.0
        hard_block = bool(r["hard_block"])
        panel_rows.append(
            LlmBudgetRow(
                purpose=str(r["purpose"]),
                monthly_cap_usd=cap,
                current_spend_usd=spend,
                headroom_pct=headroom,
                warn_threshold_pct=float(r["warn_threshold_pct"]),
                hard_block=hard_block,
                on_exceed=_mode_from_row(r, has_mode=has_mode, hard_block=hard_block),
            )
        )
    # Sort over-cap first (most over → least), then warn, then ok.
    panel_rows.sort(key=lambda r: (r.headroom_pct, r.purpose))
    # Linear projection. Day 1 (no time elapsed) → projected == spend so
    # we don't divide by zero or blow up the projection.
    elapsed_seconds = (now - month_start).total_seconds()
    if elapsed_seconds < 60:  # < 1 min elapsed in month — projection isn't meaningful
        projected = total_spend
    else:
        # Days in this month
        if now.month == 12:
            next_month_start = now.replace(
                year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0
            )
        else:
            next_month_start = now.replace(
                month=now.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0
            )
        month_total_seconds = (next_month_start - month_start).total_seconds()
        projected = total_spend * (month_total_seconds / elapsed_seconds)
    return LlmBudgetPanel(
        rows=panel_rows,
        total_spend_mtd_usd=total_spend,
        projected_month_end_usd=projected,
        month_label=month_label,
        by_ticker=_build_llm_by_ticker(conn, month_start),
    )


def _build_llm_by_ticker(
    conn: sqlite3.Connection, month_start: datetime
) -> list[LlmTickerSpendRow]:
    """Month-to-date LLM spend grouped by attributed ticker, across every purpose
    (not just budgeted ones). Empty when `llm_calls` lacks a `ticker` column
    (pre-0034 / minimal test schemas). NULL/blank tickers fold into
    '(unattributed)' so the rows still sum to the true MTD total."""
    has_ticker = (
        conn.execute(
            "SELECT 1 FROM pragma_table_info('llm_calls') WHERE name = 'ticker'"
        ).fetchone()
        is not None
    )
    if not has_ticker:
        return []
    trows = conn.execute(
        """
        SELECT COALESCE(NULLIF(TRIM(ticker), ''), '(unattributed)') AS tkr,
               COALESCE(SUM(cost_estimate_usd), 0.0) AS spend,
               COUNT(*) AS calls
        FROM llm_calls
        WHERE called_at >= ?
        GROUP BY tkr
        ORDER BY spend DESC, tkr
        """,
        (month_start.isoformat(),),
    ).fetchall()
    return [
        LlmTickerSpendRow(
            ticker=str(t["tkr"]),
            current_spend_usd=float(t["spend"] or 0.0),
            call_count=int(t["calls"]),
        )
        for t in trows
    ]


def _load_portfolio_synthesis(conn: sqlite3.Connection) -> str | None:
    """The cross_portfolio_synthesis lens output if cached."""
    has_artifacts = (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='llm_artifacts'"
        ).fetchone()
        is not None
    )
    if not has_artifacts:
        return None
    row = conn.execute(
        """
        SELECT content_md FROM llm_artifacts
        WHERE purpose = 'lens:cross_portfolio_synthesis'
          AND scope = 'portfolio'
          AND superseded_by_id IS NULL
        ORDER BY generated_at DESC LIMIT 1
        """
    ).fetchone()
    return row["content_md"] if row else None


def _load_per_ticker_rereads(
    conn: sqlite3.Connection, *, ticker: str | None = None
) -> list[PortfolioLensRow]:
    """The five_min_reread lens output for every portfolio holding that has one
    (or just ``ticker``'s, when given — the dropdown-driven Pre-reads panel)."""
    has_artifacts = (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='llm_artifacts'"
        ).fetchone()
        is not None
    )
    if not has_artifacts:
        return []
    ticker_clause = " AND a.ticker = ?" if ticker else ""
    params: tuple[object, ...] = (ticker.upper(),) if ticker else ()
    rows = conn.execute(
        f"""
        SELECT a.ticker, a.content_md, a.generated_at
        FROM llm_artifacts a
        INNER JOIN tracked_companies tc
          ON tc.ticker = a.ticker
          AND tc.archived_at IS NULL
          AND tc.list_type IN ('portfolio', 'evaluation')
        WHERE a.purpose = 'lens:five_min_reread'
          AND a.superseded_by_id IS NULL{ticker_clause}
        ORDER BY tc.list_type, a.ticker
        """,
        params,
    ).fetchall()
    return [
        PortfolioLensRow(
            ticker=r["ticker"],
            lens="five_min_reread",
            content_md=r["content_md"] or "",
            generated_at=str(r["generated_at"]),
        )
        for r in rows
    ]


def _build_trigger_ladder(
    conn: sqlite3.Connection, list_types: tuple[str, ...]
) -> list[TriggerLadderRow]:
    """All holdings' DCF positioning, sorted by absolute over/under."""
    placeholders = ",".join("?" * len(list_types))
    # Verify dcf_runs exists (project may have a pre-Phase-3 DB)
    has_dcf = (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='dcf_runs'"
        ).fetchone()
        is not None
    )
    if not has_dcf:
        return []

    # The ladder is only relevant for names the owner has an actual thesis on
    # (owner feedback 2026-07-14: "Triggers has so much irrelevant data — it
    # should only be for names where I have a thesis"). Require a non-empty,
    # non-placeholder thesis_state.thesis — the same has_written_thesis
    # predicate the canonical v_thesis_status view uses (migration 0151:
    # 74 prod rows carry the literal "STUB: needs user-authored thesis" and
    # must not count as a thesis). Absent the table, no ladder.
    has_thesis_state = (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='thesis_state'"
        ).fetchone()
        is not None
    )
    if not has_thesis_state:
        return []

    # Subquery: each ticker's latest dcf_runs row (segment_name = NULL = consolidated)
    rows = conn.execute(
        f"""
        WITH latest_dcf AS (
            SELECT ticker, MAX(valuation_date) AS vd
            FROM dcf_runs
            WHERE segment_name IS NULL OR segment_name = ''
            GROUP BY ticker
        )
        SELECT tc.ticker, tc.list_type,
               dr.over_under_pct, dr.mos_bar_used, dr.live_price,
               dr.npv_per_share AS fair_value, ts.breach_status as verdict,
               CASE
                 WHEN dr.over_under_pct IS NULL THEN 'unknown'
                 WHEN dr.over_under_pct > 0.20 THEN 'sell'
                 WHEN dr.over_under_pct > 0.10 THEN 'trim'
                 WHEN dr.mos_bar_used IS NOT NULL AND dr.over_under_pct < -dr.mos_bar_used THEN 'initiate_candidate'
                 ELSE 'hold'
               END AS trigger_status
        FROM tracked_companies tc
        LEFT JOIN latest_dcf ld ON ld.ticker = tc.ticker
        LEFT JOIN dcf_runs dr
          ON dr.ticker = tc.ticker AND dr.valuation_date = ld.vd
          AND (dr.segment_name IS NULL OR dr.segment_name = '')
        JOIN thesis_state ts ON ts.ticker = tc.ticker
        WHERE tc.archived_at IS NULL
          AND tc.list_type IN ({placeholders})
          AND TRIM(COALESCE(ts.thesis, '')) <> ''
          AND ts.thesis NOT LIKE 'STUB:%'
        ORDER BY ABS(COALESCE(dr.over_under_pct, 0)) DESC, tc.ticker
        """,
        list_types,
    ).fetchall()

    return [
        TriggerLadderRow(
            ticker=r["ticker"],
            list_type=r["list_type"],
            over_under_pct=_f(r["over_under_pct"]),
            mos_bar=_f(r["mos_bar_used"]),
            trigger_status=r["trigger_status"],
            live_price=_f(r["live_price"]),
            dcf_fair_value=_f(r["fair_value"]),
            verdict=r["verdict"],
        )
        for r in rows
    ]


def _build_insider_events(
    conn: sqlite3.Connection,
    window_days: int,
    top_n: int,
    list_types: tuple[str, ...],
    *,
    ticker: str | None = None,
) -> list[InsiderEventRow]:
    """Cross-ticker insider activity over `window_days`, ranked by approximation
    of conviction signal (open-market buys + cluster size + transaction value).
    For simplicity here we compute a quick score; the rich
    insider_transactions.conviction_signals() is per-ticker."""
    has_insiders = (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='insider_transactions'"
        ).fetchone()
        is not None
    )
    if not has_insiders:
        return []

    placeholders = ",".join("?" * len(list_types))
    cutoff = (datetime.now(UTC) - timedelta(days=window_days)).isoformat()
    ticker_clause = " AND ticker = ?" if ticker else ""
    active_params: tuple[object, ...] = (
        (*list_types, ticker.upper()) if ticker else tuple(list_types)
    )
    rows = conn.execute(
        f"""
        WITH active AS (
            SELECT ticker FROM tracked_companies
            WHERE archived_at IS NULL AND list_type IN ({placeholders}){ticker_clause}
        ),
        clustered AS (
            -- Same-day cluster count per (ticker, date)
            SELECT it.ticker, DATE(it.transaction_date) AS d, COUNT(*) AS cluster_size
            FROM insider_transactions it
            INNER JOIN active a ON a.ticker = it.ticker
            WHERE it.transaction_date >= ?
            GROUP BY it.ticker, DATE(it.transaction_date)
        )
        SELECT it.ticker, it.insider_name, it.insider_title,
               it.transaction_date, it.transaction_type, it.shares,
               it.transaction_value, it.is_10b5_1,
               COALESCE(c.cluster_size, 1) AS cluster_size
        FROM insider_transactions it
        INNER JOIN active a ON a.ticker = it.ticker
        LEFT JOIN clustered c
          ON c.ticker = it.ticker AND c.d = DATE(it.transaction_date)
        WHERE it.transaction_date >= ?
          AND it.transaction_type IN ('open_market_buy', 'open_market_sell', 'option_exercise')
          -- Discretionary only — drop 10b5-1 sells (pre-scheduled = noise)
          AND NOT (it.transaction_type = 'open_market_sell' AND it.is_10b5_1 = 1)
        ORDER BY it.transaction_date DESC
        LIMIT 500
        """,
        (*active_params, cutoff, cutoff),
    ).fetchall()

    # Score: open-market buys >>> sells; weighted by role and value
    import math

    scored: list[tuple[float, InsiderEventRow]] = []
    for r in rows:
        title = (r["insider_title"] or "").lower()
        role_w = (
            1.0
            if "chief executive" in title
            else 0.85
            if "chief financial" in title
            else 0.7
            if "president" in title
            else 0.5
        )
        type_w = (
            1.0
            if r["transaction_type"] == "open_market_buy"
            else 0.6
            if r["transaction_type"] == "open_market_sell"
            else 0.15
        )
        v = float(r["transaction_value"] or 0)
        size = math.log10(max(v, 1e3)) / 8.0 if v > 0 else 0.3
        cluster_bonus = min(0.2, (int(r["cluster_size"]) - 1) * 0.05)
        strength = min(1.0, role_w * type_w * size + cluster_bonus)

        bits: list[str] = []
        if r["transaction_type"] == "open_market_buy":
            bits.append("open-market buy")
        elif r["transaction_type"] == "open_market_sell":
            bits.append("non-10b5-1 sell")
        else:
            bits.append(r["transaction_type"].replace("_", " "))
        if int(r["cluster_size"]) > 1:
            bits.append(f"cluster ({r['cluster_size']} insiders same day)")
        if r["insider_title"]:
            bits.append((r["insider_title"]).lower())
        rationale = " · ".join(bits)

        scored.append(
            (
                strength,
                InsiderEventRow(
                    ticker=r["ticker"],
                    insider_name=r["insider_name"],
                    insider_role=r["insider_title"],
                    transaction_date=r["transaction_date"][:10],
                    transaction_type=r["transaction_type"],
                    shares=float(r["shares"]),
                    transaction_value=float(r["transaction_value"])
                    if r["transaction_value"] is not None
                    else None,
                    signal_strength=strength,
                    rationale=rationale,
                ),
            )
        )

    scored.sort(key=lambda t: -t[0])
    return [e for _, e in scored[:top_n]]


def _build_prediction_outcomes(
    conn: sqlite3.Connection, list_types: tuple[str, ...]
) -> list[PredictionOutcomeRow]:
    has_predictions = (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='predictions'"
        ).fetchone()
        is not None
    )
    if not has_predictions:
        return []
    placeholders = ",".join("?" * len(list_types))
    rows = conn.execute(
        f"""
        SELECT p.ticker, p.source_kind, p.outcome, COUNT(*) AS n
        FROM predictions p
        INNER JOIN tracked_companies tc
          ON tc.ticker = p.ticker AND tc.archived_at IS NULL
          AND tc.list_type IN ({placeholders})
        GROUP BY p.ticker, p.source_kind, p.outcome
        ORDER BY p.ticker, p.source_kind
        """,
        list_types,
    ).fetchall()
    return [
        PredictionOutcomeRow(
            ticker=r["ticker"],
            source_kind=r["source_kind"],
            outcome=r["outcome"],
            count=int(r["n"]),
        )
        for r in rows
    ]


def _f(v: object) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return None
