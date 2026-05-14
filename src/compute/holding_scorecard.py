"""Per-holding scorecard: thesis status streak + commitment hit rate + recent KPIs +
EPS/Revenue analyst-surprise beat rate.

Combines the four signals built in earlier phases into a single executive view:
  1. Thesis evaluator: current breach status + streak length (from thesis_history)
  2. Say-Do: % of management commitments that HIT or BEAT vs. MISSed
  3. Analyst surprise: EPS/Revenue beat rate over the last N reported quarters
  4. Most recent kpi_facts row per registered KPI (for context)

Pure read-only — all writes happen in upstream modules.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import db
from compute.earnings_surprise import SurpriseScorecard, surprise_scorecard_for
from compute.say_do import CommitmentOutcome
from compute.thesis_history import StreakSummary, streak_summary
from models.kpis import BreachStatus


@dataclass(frozen=True)
class CommitmentScorecard:
    """Aggregated Say-Do counts for a ticker."""

    total: int
    hit: int
    beat: int
    miss: int
    no_data: int
    hit_rate_pct: Decimal | None  # (hit + beat) / matched * 100; None if no matched commitments


@dataclass(frozen=True)
class RecentKpi:
    """Most recent kpi_facts observation for one KPI name."""

    name: str
    value: Decimal
    period_end: datetime
    fiscal_period_type: str


@dataclass(frozen=True)
class HoldingScorecard:
    """Single-holding rollup spanning thesis status, Say-Do, analyst surprise,
    and recent KPIs."""

    ticker: str
    breach_status: BreachStatus | None
    streak: StreakSummary | None
    commitments: CommitmentScorecard
    surprise: SurpriseScorecard
    recent_kpis: tuple[RecentKpi, ...]


def _commitment_scorecard(
    conn: sqlite3.Connection, ticker: str
) -> CommitmentScorecard:
    """Aggregate management_commitments outcomes for a ticker."""
    cur = conn.execute(
        "SELECT outcome, COUNT(*) AS n "
        "FROM management_commitments WHERE ticker = ? GROUP BY outcome",
        (ticker.upper(),),
    )
    counts: dict[str | None, int] = {row["outcome"]: int(row["n"]) for row in cur.fetchall()}
    total = sum(counts.values())
    hit = counts.get(CommitmentOutcome.HIT.value, 0)
    beat = counts.get(CommitmentOutcome.BEAT.value, 0)
    miss = counts.get(CommitmentOutcome.MISS.value, 0)
    no_data = counts.get(CommitmentOutcome.NO_DATA.value, 0)
    matched = hit + beat + miss
    hit_rate = (
        (Decimal(hit + beat) / Decimal(matched) * Decimal(100)).quantize(Decimal("0.1"))
        if matched > 0
        else None
    )
    return CommitmentScorecard(
        total=total,
        hit=hit,
        beat=beat,
        miss=miss,
        no_data=no_data,
        hit_rate_pct=hit_rate,
    )


def _recent_kpis(
    conn: sqlite3.Connection, ticker: str, limit: int = 8
) -> list[RecentKpi]:
    """Return the most-recent kpi_facts row per KPI name for the ticker.

    Joins to kpi_definitions for the canonical name. Limit caps the number of
    distinct KPIs returned (most-recently-updated first).
    """
    cur = conn.execute(
        """
        SELECT kd.name AS name, kf.value AS value,
               kf.period_end AS period_end, kf.fiscal_period_type AS fpt
        FROM kpi_facts kf
        JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id
        WHERE kf.ticker = ?
          AND kf.id IN (
              SELECT MAX(kf2.id) FROM kpi_facts kf2
              WHERE kf2.ticker = ?
              GROUP BY kf2.kpi_definition_id
          )
        ORDER BY kf.period_end DESC, kd.name
        LIMIT ?
        """,
        (ticker.upper(), ticker.upper(), limit),
    )
    out: list[RecentKpi] = []
    for row in cur.fetchall():
        pe = row["period_end"]
        if isinstance(pe, str):
            pe = datetime.fromisoformat(pe)
        out.append(
            RecentKpi(
                name=row["name"],
                value=Decimal(str(row["value"])),
                period_end=pe,
                fiscal_period_type=row["fpt"],
            )
        )
    return out


def _current_breach_status(
    conn: sqlite3.Connection, ticker: str
) -> BreachStatus | None:
    """Read the current snapshot from thesis_state."""
    cur = conn.execute(
        "SELECT breach_status FROM thesis_state WHERE ticker = ? LIMIT 1",
        (ticker.upper(),),
    )
    row = cur.fetchone()
    if row is None or row["breach_status"] is None:
        return None
    return BreachStatus(row["breach_status"])


def scorecard_for(
    conn: sqlite3.Connection,
    ticker: str,
    *,
    recent_kpi_limit: int = 8,
    surprise_lookback_quarters: int = 8,
) -> HoldingScorecard:
    """Compose all four layers into a single HoldingScorecard.

    `surprise_lookback_quarters` matches the default in `backfill_earnings_surprises.py`
    so the scorecard window aligns with the cache window — the typical street
    view of "last 8 quarters' beat rate".
    """
    return HoldingScorecard(
        ticker=ticker.upper(),
        breach_status=_current_breach_status(conn, ticker),
        streak=streak_summary(conn, ticker),
        commitments=_commitment_scorecard(conn, ticker),
        surprise=surprise_scorecard_for(
            conn, ticker, lookback_quarters=surprise_lookback_quarters
        ),
        recent_kpis=tuple(_recent_kpis(conn, ticker, limit=recent_kpi_limit)),
    )


def portfolio_scorecards(
    conn: sqlite3.Connection, *, recent_kpi_limit: int = 8
) -> list[HoldingScorecard]:
    """Scorecard for every ticker in thesis_state.

    Scope is "tickers with an active thesis record" — narrower than
    `briefed_scorecards()` but the historical entry point. Kept for callers
    that specifically want the thesis-tracked subset.
    """
    cur = conn.execute("SELECT ticker FROM thesis_state ORDER BY ticker")
    tickers = [row["ticker"] for row in cur.fetchall()]
    return [scorecard_for(conn, t, recent_kpi_limit=recent_kpi_limit) for t in tickers]


def briefed_scorecards(
    conn: sqlite3.Connection,
    *,
    recent_kpi_limit: int = 8,
    surprise_lookback_quarters: int = 8,
) -> list[HoldingScorecard]:
    """Scorecard for every active portfolio + evaluation ticker (the brief set).

    Uses `db.BRIEFED_LIST_TYPES_SQL` so the scope matches what gets full-brief
    treatment elsewhere in the pipeline. Watchlist is excluded — it's a holding
    pen and doesn't carry the data depth needed for scorecard signals.
    """
    cur = conn.execute(
        f"SELECT ticker FROM tracked_companies "
        f"WHERE list_type IN {db.BRIEFED_LIST_TYPES_SQL} "
        f"AND archived_at IS NULL "
        f"ORDER BY ticker"
    )
    tickers = [row["ticker"] for row in cur.fetchall()]
    return [
        scorecard_for(
            conn, t,
            recent_kpi_limit=recent_kpi_limit,
            surprise_lookback_quarters=surprise_lookback_quarters,
        )
        for t in tickers
    ]
