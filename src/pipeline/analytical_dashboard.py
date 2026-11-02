"""Analytical portfolio dashboard — cross-ticker view layered on top of dashboard_status.

Pure reads from the unified data model. Retains four panels:

  1. Trigger ladder    — every holding's DCF over/under vs the MoS bar,
                          positioned on a SELL ↔ HOLD ↔ ADD rail.
  2. Portfolio synthesis — cross_portfolio_synthesis lens output.
  3. Per-ticker re-reads — read-owner view per ticker (optionally scoped to
                          one ticker by the caller).
  4. LLM budgets       — read-only spend/cap/headroom panel.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


@dataclass(slots=True)
class TriggerLadderRow:
    ticker: str
    list_type: str
    over_under_pct: float | None
    mos_bar: float | None
    trigger_status: str | None  # 'sell' | 'trim' | 'hold' | 'initiate_candidate' | 'unreviewed'
    live_price: float | None
    dcf_fair_value: float | None
    verdict: str | None  # from thesis_state
    # dcf_runs.sanity_flag (migration 0182): 'outlier' = the model failed the trust
    # gate; the row renders with an explicit unreviewed badge and never a sell/trim
    # signal (a broken model must not trigger an action).
    sanity_flag: str | None = None


@dataclass(slots=True)
class PortfolioLensRow:
    """One per-holding 5-min-reread (or other ticker lens) for cross-ticker
    scanning in the dashboard."""

    ticker: str
    lens: str
    content_md: str
    generated_at: str


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

    rows: list[LlmBudgetRow] = field(default_factory=list[LlmBudgetRow])
    total_spend_mtd_usd: float = 0.0
    projected_month_end_usd: float = 0.0
    month_label: str = ""  # 'YYYY-MM'
    by_ticker: list[LlmTickerSpendRow] = field(default_factory=list[LlmTickerSpendRow])


@dataclass(slots=True)
class AnalyticalDashboard:
    trigger_ladder: list[TriggerLadderRow] = field(default_factory=list[TriggerLadderRow])
    portfolio_synthesis_md: str | None = None  # cross_portfolio_synthesis lens output
    per_ticker_reread: list[PortfolioLensRow] = field(default_factory=list[PortfolioLensRow])
    llm_budgets: LlmBudgetPanel = field(default_factory=lambda: LlmBudgetPanel())


# Section keys accepted by ``build_analytical_dashboard(sections=...)``. Each maps
# to one AnalyticalDashboard field / sub-builder so the unified command-center
# shell can lazy-load a single panel without running the other (heavy) queries.
DASHBOARD_SECTIONS: frozenset[str] = frozenset(
    {
        "trigger_ladder",
        "portfolio_synthesis",
        "rereads",
        "llm_budgets",
    }
)


def build_analytical_dashboard(
    db_path: Path,
    *,
    list_types: tuple[str, ...] = ("portfolio", "watchlist"),
    sections: set[str] | frozenset[str] | None = None,
    ticker: str | None = None,
) -> AnalyticalDashboard:
    """Build the cross-ticker analytical dashboard.

    Builds the four retained panels: the trigger ladder (DCF over/under vs the
    MoS bar), the portfolio synthesis (cross_portfolio_synthesis lens output),
    the per-ticker re-reads, and the LLM budget/spend panel.

    ``sections`` selects which panels to build (keys in ``DASHBOARD_SECTIONS``);
    unrequested panels keep their empty dataclass default. ``None`` (the default)
    retains all four sections. Per-panel selection lets callers fetch one panel
    without running the other sub-queries.

    ``ticker`` scopes only the per-ticker re-reads panel to one name; the other
    panels ignore it.
    """
    if not db_path.exists():
        return AnalyticalDashboard()

    def want(name: str) -> bool:
        return sections is None or name in sections

    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    try:
        conn.row_factory = sqlite3.Row
        return AnalyticalDashboard(
            trigger_ladder=(
                _build_trigger_ladder(conn, list_types) if want("trigger_ladder") else []
            ),
            portfolio_synthesis_md=(
                _load_portfolio_synthesis(conn) if want("portfolio_synthesis") else None
            ),
            per_ticker_reread=(
                _load_per_ticker_rereads(conn, ticker=ticker) if want("rereads") else []
            ),
            llm_budgets=(
                _build_llm_budget_panel(conn) if want("llm_budgets") else LlmBudgetPanel()
            ),
        )
    finally:
        conn.close()


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
    # predicate the canonical v_thesis_status view uses (migration 0152:
    # prod stub rows carry the literal "STUB: needs user-authored thesis",
    # sometimes EMBEDDED mid-text — e.g. ROP — so the marker is matched as a
    # substring, never a prefix). Absent the table, no ladder.
    has_thesis_state = (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='thesis_state'"
        ).fetchone()
        is not None
    )
    if not has_thesis_state:
        return []

    # Subquery: each ticker's latest dcf_runs row (segment_name = NULL =
    # consolidated). ONE row per ticker by construction: the nightly sweep can
    # write several runs on the same valuation_date (prod: FCX/LLY/LITE each
    # carried two on their max date, minutes apart), and the old join on
    # (ticker, valuation_date) fanned out — the owner's ladder showed FCX
    # twice at two live prices. Latest run wins (valuation_date, then rowid —
    # dcf_runs' id is a rowid alias, and test DBs build the table bare) —
    # the render-seam dedupe rule (surface_density_jit_redesign.md D3.2).
    dcf_cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(dcf_runs)")}
    sanity_sel = "dr.sanity_flag" if "sanity_flag" in dcf_cols else "NULL AS sanity_flag"
    rows = conn.execute(
        f"""
        WITH latest_dcf AS (
            SELECT ticker, run_id FROM (
                SELECT ticker, rowid AS run_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY ticker
                           ORDER BY valuation_date DESC, rowid DESC
                       ) AS rn
                FROM dcf_runs
                WHERE segment_name IS NULL OR segment_name = ''
            ) WHERE rn = 1
        )
        SELECT tc.ticker, tc.list_type,
               dr.over_under_pct, dr.mos_bar_used, dr.live_price,
               dr.npv_per_share AS fair_value, ts.breach_status as verdict,
               {sanity_sel},
               CASE
                 WHEN dr.over_under_pct IS NULL THEN 'unknown'
                 WHEN dr.over_under_pct > 0.20 THEN 'sell'
                 WHEN dr.over_under_pct > 0.10 THEN 'trim'
                 WHEN dr.mos_bar_used IS NOT NULL AND dr.over_under_pct < -dr.mos_bar_used THEN 'initiate_candidate'
                 ELSE 'hold'
               END AS trigger_status
        FROM tracked_companies tc
        LEFT JOIN latest_dcf ld ON ld.ticker = tc.ticker
        LEFT JOIN dcf_runs dr ON dr.rowid = ld.run_id
        JOIN thesis_state ts ON ts.ticker = tc.ticker
        WHERE tc.archived_at IS NULL
          AND tc.list_type IN ({placeholders})
          AND TRIM(COALESCE(ts.thesis, '')) <> ''
          AND ts.thesis NOT LIKE '%STUB:%'
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
            # A sanity-flagged model must not emit an action signal — the ladder
            # shows 'unreviewed' instead of sell/trim/hold for that row.
            trigger_status="unreviewed" if r["sanity_flag"] else r["trigger_status"],
            live_price=_f(r["live_price"]),
            dcf_fair_value=_f(r["fair_value"]),
            verdict=r["verdict"],
            sanity_flag=r["sanity_flag"],
        )
        for r in rows
    ]


def _f(v: object) -> float | None:
    if v is None or v == "":
        return None
    if not isinstance(v, str | int | float):
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None
