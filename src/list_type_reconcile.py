"""Reconcile ``tracked_companies.list_type`` against the tracker's live holdings.

Rule (user directive 2026-06-15):

  * An **operating company held > $threshold** is a ``portfolio`` name.
  * A ``portfolio`` name **not** held > $threshold is demoted to ``evaluation``.
    Nothing is lost — ``evaluation`` is in ``db.BRIEFED_LIST_TYPES``, so the row,
    its brief, KPIs, thesis and decisions all stay exactly as they were; only the
    tier label changes.
  * **ETFs, funds, options and cash are never promoted**, no matter how large the
    position — the platform's briefs / DCF / KPIs only apply to operating
    companies. The promotion side uses an explicit equity-type allowlist
    (``EQUITY_SECURITY_TYPES``) so a $50k index-ETF position can never masquerade
    as a research name.
  * Names in the pins store (``data/portfolio_pins.json``) stay in ``portfolio``
    regardless of holdings — conviction names the analyst wants briefed at the
    portfolio tier without a live position.

State-based and idempotent, mirroring :mod:`position_lifecycle`: there is no
single chokepoint for ``list_type`` writes (scripts, manual SQL, future UI all
flip it), so this recomputes the desired state from the world (holdings + pins)
and writes only the diff. A second run against the same world is a no-op.

Promotion only ever touches names **already tracked** (``watchlist`` /
``evaluation``). An untracked held equity is *reported* (``untracked_held``) for
manual onboarding, never auto-added — onboarding has SEC-validation and
issuer-registry side effects this reconciler must not trigger blind.

The tracker holdings are read from the companion ``portfolio-tracker`` SQLite
(read-only); the ``securities.type`` column is why we read the DB directly rather
than the live API, which doesn't carry the instrument type we gate promotion on.
After applying a diff the caller is expected to run
``issuer_registry.sync_all`` (the documented reconcile behind raw ``list_type``
flips) and, if desired, ``sync_position_lifecycle``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from identity import DEFAULT_USER_ID

# Plaid ``securities.type`` codes that denote an *operating company* — the only
# instruments the research platform can brief. Observed in the tracker DB:
#   cs       common stock        (MELI, BN, NOW, NU, RBRK, VEEV, WIX, ...)
#   equity   plain equity        (GOOGL, UBER, BKNG, ...)
#   ad       ADR                 (NVO, TSM, SE, ...)
# Deliberately EXCLUDED (never promoted): et/etf (ETFs), oef (open-end / money
# funds), derivative (options), cash, and NULL/blank junk rows.
EQUITY_SECURITY_TYPES: frozenset[str] = frozenset({"cs", "equity", "ad", "adr"})

# Tier a portfolio name without a qualifying position is demoted to. Both this
# and ``portfolio`` are in db.BRIEFED_LIST_TYPES, so the demotion preserves all
# briefing depth — see module docstring.
DEMOTION_LIST_TYPE = "evaluation"

_PINS_REL = ("data", "portfolio_pins.json")


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Reclassification:
    """The computed diff between the world (holdings + pins) and the DB.

    Every list is sorted by ticker for stable, diff-friendly output.
    """

    # (ticker, from_list_type, market_value) — promoted to ``portfolio``.
    promotions: list[tuple[str, str, float]] = field(default_factory=list[tuple[str, str, float]])
    # (ticker, market_value) — demoted to ``evaluation`` (mv is what's held, may be 0).
    demotions: list[tuple[str, float]] = field(default_factory=list[tuple[str, float]])
    # (ticker, market_value) — in ``portfolio``, not held, but pinned → kept.
    pinned_kept: list[tuple[str, float]] = field(default_factory=list[tuple[str, float]])
    # (ticker, security_type, market_value) — held equity > threshold but UNTRACKED.
    # Reported, never auto-added.
    untracked_held: list[tuple[str, str, float]] = field(
        default_factory=list[tuple[str, str, float]]
    )
    # ``portfolio`` names that are correctly held > threshold (no change).
    unchanged_portfolio: list[str] = field(default_factory=list[str])
    # Safety latch: the tracker showed ZERO holdings (absent / empty / failed
    # sync). When True the plan is forced empty — see compute_reclassification.
    holdings_unavailable: bool = False

    @property
    def has_changes(self) -> bool:
        return bool(self.promotions or self.demotions)


# ---------------------------------------------------------------------------
# World readers
# ---------------------------------------------------------------------------


def _held_operating_companies(
    tracker_conn: sqlite3.Connection, min_value: float
) -> dict[str, float]:
    """``{TICKER: market_value}`` for operating companies held above ``min_value``.

    Latest snapshot per (account, security), summed across accounts; filtered to
    the equity-type allowlist and non-cash-equivalents. ETFs/funds/options/cash
    never appear here, so they can never be promoted.
    """
    allowed_types = tuple(sorted(EQUITY_SECURITY_TYPES))
    type_placeholders = ",".join("?" for _ in allowed_types)
    rows = tracker_conn.execute(
        f"""
        WITH latest AS (
            SELECT hs.account_id, hs.security_id, hs.quantity, hs.institution_value,
                   ROW_NUMBER() OVER (
                       PARTITION BY hs.account_id, hs.security_id
                       ORDER BY hs.snapshot_date DESC
                   ) AS rn
            FROM holdings_snapshots hs
        )
        SELECT UPPER(s.ticker)                        AS ticker,
               SUM(COALESCE(l.institution_value, 0)) AS mv
        FROM latest l
        JOIN securities s ON s.security_id = l.security_id
        WHERE l.rn = 1
          AND l.quantity > 0
          AND s.ticker IS NOT NULL
          AND TRIM(s.ticker) <> ''
          AND COALESCE(s.is_cash_equivalent, 0) = 0
          AND LOWER(COALESCE(s.type, '')) IN ({type_placeholders})
        GROUP BY UPPER(s.ticker)
        """,  # nosec B608 -- placeholders only; the SQL shape is module-owned
        allowed_types,
    ).fetchall()
    held: dict[str, float] = {}
    for r in rows:
        mv = float(r["mv"] or 0.0)
        if mv <= min_value:
            continue
        held[str(r["ticker"])] = mv
    return held


def _all_held_value(tracker_conn: sqlite3.Connection) -> dict[str, float]:
    """``{TICKER: market_value}`` for everything held (any type) — used only to
    annotate demotions with what (if anything) is actually held."""
    rows = tracker_conn.execute(
        """
        WITH latest AS (
            SELECT hs.account_id, hs.security_id, hs.quantity, hs.institution_value,
                   ROW_NUMBER() OVER (
                       PARTITION BY hs.account_id, hs.security_id
                       ORDER BY hs.snapshot_date DESC
                   ) AS rn
            FROM holdings_snapshots hs
        )
        SELECT UPPER(s.ticker)                       AS ticker,
               SUM(COALESCE(l.institution_value, 0)) AS mv
        FROM latest l
        JOIN securities s ON s.security_id = l.security_id
        WHERE l.rn = 1 AND l.quantity > 0
          AND s.ticker IS NOT NULL AND TRIM(s.ticker) <> ''
        GROUP BY UPPER(s.ticker)
        """
    ).fetchall()
    return {str(r["ticker"]): float(r["mv"] or 0.0) for r in rows}


def _active_tracked(es_conn: sqlite3.Connection, user_id: str) -> dict[str, str]:
    """``{TICKER: list_type}`` for the user's active (non-archived) tracked rows."""
    rows = es_conn.execute(
        "SELECT UPPER(ticker) AS ticker, list_type FROM tracked_companies "
        "WHERE user_id = ? AND archived_at IS NULL",
        (user_id,),
    ).fetchall()
    return {str(r["ticker"]): str(r["list_type"]) for r in rows}


def load_pins(repo_root: Path) -> dict[str, str]:
    """Read ``data/portfolio_pins.json`` → ``{TICKER: reason}``.

    Format::

        {"pinned_portfolio": [{"ticker": "META", "reason": "..."}]}

    Returns ``{}`` when absent or malformed — an empty pins store means the rule
    applies with no exceptions (every unheld portfolio name demotes).
    """
    path = Path(repo_root).joinpath(*_PINS_REL)
    if not path.exists():
        return {}
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    entries = cast("dict[str, object]", payload).get("pinned_portfolio")
    if not isinstance(entries, list):
        return {}
    pins: dict[str, str] = {}
    for raw in cast("list[object]", entries):
        if not isinstance(raw, dict):
            continue
        entry = cast("dict[str, object]", raw)
        ticker = entry.get("ticker")
        if isinstance(ticker, str) and ticker.strip():
            reason = entry.get("reason")
            pins[ticker.strip().upper()] = reason if isinstance(reason, str) else ""
    return pins


# ---------------------------------------------------------------------------
# Compute + apply
# ---------------------------------------------------------------------------


def compute_reclassification(
    *,
    es_conn: sqlite3.Connection,
    tracker_conn: sqlite3.Connection,
    pins: set[str] | None = None,
    min_value: float = 100.0,
    user_id: str = DEFAULT_USER_ID,
) -> Reclassification:
    """Diff the desired state (holdings + pins) against ``tracked_companies``.

    Pure read — no writes. Pass the result to :func:`apply_reclassification`.

    **Safety latch:** if the tracker shows *zero* holdings of any kind (absent /
    empty DB, or a failed sync), the result is forced empty with
    ``holdings_unavailable=True``. Without this, an empty holdings read would make
    EVERY unpinned portfolio name look "not held" and demote the entire book —
    exactly the wrong move on a transient tracker outage. Mirrors
    ``position_lifecycle``'s None-means-no-op guard. A real all-cash book is
    indistinguishable from an outage here, and refusing to act is the safe choice
    either way (demotions never fire on no data).
    """
    pins = pins or set()
    all_held = _all_held_value(tracker_conn)
    if not all_held:
        return Reclassification(holdings_unavailable=True)

    held = _held_operating_companies(tracker_conn, min_value)
    tracked = _active_tracked(es_conn, user_id)
    portfolio = {t for t, lt in tracked.items() if lt == "portfolio"}

    promotions: list[tuple[str, str, float]] = []
    untracked_held: list[tuple[str, str, float]] = []
    for ticker, mv in held.items():
        current = tracked.get(ticker)
        if current is None:
            # Held operating company we don't track at all — surface it, don't
            # auto-onboard. (Security type is "equity-class" by construction here.)
            untracked_held.append((ticker, "equity", mv))
        elif current != "portfolio":
            promotions.append((ticker, current, mv))

    demotions: list[tuple[str, float]] = []
    pinned_kept: list[tuple[str, float]] = []
    unchanged: list[str] = []
    for ticker in portfolio:
        if ticker in held:
            unchanged.append(ticker)
        elif ticker in pins:
            pinned_kept.append((ticker, all_held.get(ticker, 0.0)))
        else:
            demotions.append((ticker, all_held.get(ticker, 0.0)))

    return Reclassification(
        promotions=sorted(promotions),
        demotions=sorted(demotions),
        pinned_kept=sorted(pinned_kept),
        untracked_held=sorted(untracked_held),
        unchanged_portfolio=sorted(unchanged),
    )


def apply_reclassification(
    *,
    es_conn: sqlite3.Connection,
    plan: Reclassification,
    user_id: str = DEFAULT_USER_ID,
) -> dict[str, int]:
    """Write the plan's promotions/demotions to ``tracked_companies``.

    Membership and the governed brief queue are updated together. Identity and
    historical research remain preserved. The
    function commits once and is idempotent on an empty plan.
    """
    promoted = demoted = 0
    for ticker, _from, _mv in plan.promotions:
        cur = es_conn.execute(
            "UPDATE tracked_companies SET list_type = 'portfolio', "
            "brief_dirty = 1 "
            "WHERE user_id = ? AND UPPER(ticker) = ? AND archived_at IS NULL",
            (user_id, ticker),
        )
        promoted += cur.rowcount
    for ticker, _mv in plan.demotions:
        cur = es_conn.execute(
            "UPDATE tracked_companies SET list_type = ?, "
            "brief_dirty = 1 "
            "WHERE user_id = ? AND UPPER(ticker) = ? AND archived_at IS NULL",
            (DEMOTION_LIST_TYPE, user_id, ticker),
        )
        demoted += cur.rowcount
    es_conn.commit()
    return {"promoted": promoted, "demoted": demoted}
