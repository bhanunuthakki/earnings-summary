"""Budget tier resolver — the ONE place a research task's $-cap is decided.

Two inputs, both disk-only (no network, no prod-DB write — so a tier decision can
never sit in the same process as a web fetch or an action write):
  * an explicit owner HOT-FLAG (research_hot_flags) — "evaluate this now" → deep;
  * else PORTFOLIO WEIGHT from the materialized-weights cache — a core position
    earns a bigger budget than a small/unheld name.

Every tier cap is <= ``WEB_CEILING_USD`` (the hard ceiling inside
``call_llm_with_web``), so a resolved budget can only ever LOWER the spend, never
raise it above the structural maximum — the S2 invariant (no wrong $-cap).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from portfolio_weights import read_materialized_weights
from user_state._db import open_conn

# The structural maximum any single web-research run may spend (mirrors the hard
# ceiling in llm.cli.call_llm_with_web). No tier may exceed it.
WEB_CEILING_USD = 2.00

# weight (% of book) at/above which a held name earns the standard tier.
_CORE_WEIGHT_PCT = 5.0

_CAPS: dict[str, float] = {"cheap": 0.25, "standard": 1.00, "deep": 2.00}
_DEFAULT_HOT_TTL_HOURS = 72


@dataclass(frozen=True, slots=True)
class BudgetTier:
    name: str  # cheap | standard | deep
    budget_usd: float
    reason: str


def tier_for(weight_pct: float, *, hot_flagged: bool) -> BudgetTier:
    """The pure decision: (weight, hot-flag) → tier. Deterministic, no I/O."""
    if hot_flagged:
        return BudgetTier("deep", _CAPS["deep"], "owner hot-flagged for immediate evaluation")
    if weight_pct >= _CORE_WEIGHT_PCT:
        return BudgetTier(
            "standard", _CAPS["standard"], f"core position ({weight_pct:.1f}% of book)"
        )
    return BudgetTier("cheap", _CAPS["cheap"], f"small/unheld ({weight_pct:.1f}% of book)")


def _naive_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)  # naive-UTC, per the store convention


def portfolio_weight_pct(ticker: str, *, repo_root: Path | None) -> float:
    """``ticker`` → weight in PERCENT from the materialized-weights disk cache;
    0.0 when unknown/unheld or the cache is unavailable (degrades to cheap)."""
    if repo_root is None:
        return 0.0
    try:
        weights = read_materialized_weights(repo_root)
    except Exception:
        return 0.0
    return float(weights.get(ticker.upper(), 0.0)) * 100.0


def is_hot_flagged(ticker: str, *, db_path: Path | str | None = None) -> bool:
    conn = open_conn(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM research_hot_flags "
            "WHERE ticker = ? AND (expires_at IS NULL OR expires_at > ?) LIMIT 1",
            (ticker.upper(), _naive_now().isoformat()),
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def set_hot_flag(
    ticker: str, *, ttl_hours: int = _DEFAULT_HOT_TTL_HOURS, db_path: Path | str | None = None
) -> None:
    """Flag a ticker for immediate (deep-tier) evaluation, expiring after
    ``ttl_hours`` so a stale flag never silently keeps spending at the top tier."""
    now = _naive_now()
    conn = open_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO research_hot_flags (ticker, set_at, expires_at) VALUES (?, ?, ?)",
            (ticker.upper(), now.isoformat(), (now + timedelta(hours=ttl_hours)).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def resolve_tier(
    ticker: str, *, db_path: Path | str | None = None, repo_root: Path | None = None
) -> BudgetTier:
    """Resolve a ticker to its budget tier: hot-flag first, then portfolio weight."""
    return tier_for(
        portfolio_weight_pct(ticker, repo_root=repo_root),
        hot_flagged=is_hot_flagged(ticker, db_path=db_path),
    )
