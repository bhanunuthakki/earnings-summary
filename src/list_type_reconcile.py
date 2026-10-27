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

Tracker holdings are read through the typed portfolio-tracker v1 API. The
snapshot is already rolled up across accounts, sold securities are absent from
the latest positions, and ``SecurityV1`` carries both the instrument type and
cash-equivalent flag used by the promotion gate.
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
from integrations.portfolio_tracker_v1 import (
    PortfolioSnapshotV1,
    SecuritiesV1Result,
    V1Fetch,
)

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
class TrackerHoldings:
    """Fail-closed holdings projection from the portfolio-tracker v1 API."""

    operating: dict[str, float] = field(default_factory=dict[str, float])
    all_held: dict[str, float] = field(default_factory=dict[str, float])
    unavailable_reason: str | None = None


class PinsUnreadableError(ValueError):
    """The pins store exists but its payload cannot be trusted."""


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
    # Safety latch: the tracker could not prove a complete, current holdings
    # book. When True the plan is forced empty — see compute_reclassification.
    holdings_unavailable: bool = False
    # Why holdings were unavailable. Empty when holdings were usable.
    unavailable_reason: str | None = None

    @property
    def has_changes(self) -> bool:
        return bool(self.promotions or self.demotions)


# ---------------------------------------------------------------------------
# World readers
# ---------------------------------------------------------------------------


def holdings_from_tracker_v1(
    snapshot_fetch: V1Fetch[PortfolioSnapshotV1],
    securities_fetch: V1Fetch[SecuritiesV1Result],
) -> TrackerHoldings:
    """Build holdings from the typed snapshot and security catalogs.

    The tracker only emits current holdings. Any partial, stale, or incomplete
    read is unavailable, never an empty book that would mass-demote the roster.
    """

    def unavailable(reason: str) -> TrackerHoldings:
        return TrackerHoldings(unavailable_reason=reason)

    if not snapshot_fetch.available:
        return unavailable(snapshot_fetch.error or "portfolio snapshot fetch unavailable")
    snapshot = snapshot_fetch.data
    if snapshot is None:
        return unavailable("portfolio snapshot fetch unavailable")
    meta = snapshot.meta
    if meta.as_of is None:
        return unavailable("portfolio snapshot has no holdings as-of date")
    if meta.is_partial:
        return unavailable("portfolio snapshot is partial")
    if meta.is_stale:
        return unavailable("portfolio snapshot is stale")
    if meta.account_coverage.lagging_account_ids:
        return unavailable("portfolio snapshot has lagging accounts")

    accounts = {account.account_id: account for account in snapshot.accounts}
    for account_id in meta.account_coverage.included_account_ids:
        account = accounts.get(account_id)
        if account is None:
            return unavailable(f"included account {account_id} missing from snapshot accounts")
        if account.holdings_as_of is not None:
            if account.holdings_as_of != meta.as_of:
                return unavailable(f"account {account_id} holdings date differs from snapshot")
            continue
        # Live Tracker data has successfully synced investment accounts with no
        # holdings history; strict rejection would fail every run, while an
        # unsynced or warned account is unknown coverage.
        if (
            account.last_successful_sync_at is None
            or account.warnings
            or (account.value is not None and account.value != 0)
        ):
            return unavailable(f"account {account_id} has unknown holdings coverage")

    if not securities_fetch.available:
        return unavailable(securities_fetch.error or "securities fetch unavailable")
    securities_result = securities_fetch.data
    if securities_result is None:
        return unavailable("securities fetch unavailable")
    securities = {security.security_id: security for security in securities_result.securities}

    operating: dict[str, float] = {}
    all_held: dict[str, float] = {}
    for position in snapshot.positions:
        security = securities.get(position.security_id)
        if security is None:
            return unavailable(f"security {position.security_id} missing from securities catalog")
        ticker = position.ticker or security.ticker
        if ticker is None or not ticker.strip():
            return unavailable(f"position {position.security_id} has no ticker")
        lots = position.accounts
        if not lots:
            return unavailable(f"position {ticker.strip()} has no account lots")
        held_lots = [lot for lot in lots if lot.quantity > 0]
        if not held_lots:
            continue
        # Value comes from the long lots, but a missing aggregate means the
        # Tracker could not value the position; treat it as unknown, not zero.
        if position.market_value is None:
            return unavailable(f"position {ticker.strip()} has no market value")
        normalized_ticker = ticker.strip().upper()
        held_market_values = [
            float(lot.market_value) for lot in held_lots if lot.market_value is not None
        ]
        if len(held_market_values) != len(held_lots):
            return unavailable(f"position {ticker.strip()} has an unvalued account lot")
        market_value = sum(held_market_values)
        all_held[normalized_ticker] = all_held.get(normalized_ticker, 0.0) + market_value
        security_type = security.type.strip().lower() if security.type else ""
        if not security.is_cash_equivalent and security_type in EQUITY_SECURITY_TYPES:
            operating[normalized_ticker] = operating.get(normalized_ticker, 0.0) + market_value
    if not all_held:
        return unavailable("portfolio snapshot has zero held positions")
    return TrackerHoldings(operating=operating, all_held=all_held)


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

    Returns ``{}`` when absent. Any present-but-untrusted payload raises
    :class:`PinsUnreadableError`; an absent file is the only valid empty store.
    """
    path = Path(repo_root).joinpath(*_PINS_REL)
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise PinsUnreadableError(f"pins file {path} is unreadable: {exc}") from exc
    except ValueError as exc:
        raise PinsUnreadableError(f"pins file {path} contains invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise PinsUnreadableError(f"pins file {path} top level must be an object")
    entries = cast("dict[str, object]", payload).get("pinned_portfolio")
    if not isinstance(entries, list):
        raise PinsUnreadableError(f"pins file {path} must contain a pinned_portfolio list")
    pins: dict[str, str] = {}
    for raw in cast("list[object]", entries):
        if not isinstance(raw, dict):
            raise PinsUnreadableError(f"pins file {path} has a non-object entry")
        entry = cast("dict[str, object]", raw)
        ticker = entry.get("ticker")
        if not isinstance(ticker, str) or not ticker.strip():
            raise PinsUnreadableError(f"pins file {path} has an entry without a ticker")
        reason = entry.get("reason")
        pins[ticker.strip().upper()] = reason if isinstance(reason, str) else ""
    return pins


# ---------------------------------------------------------------------------
# Compute + apply
# ---------------------------------------------------------------------------


def compute_reclassification(
    *,
    es_conn: sqlite3.Connection,
    holdings: TrackerHoldings,
    pins: set[str] | None = None,
    min_value: float = 100.0,
    user_id: str = DEFAULT_USER_ID,
) -> Reclassification:
    """Diff the desired state (holdings + pins) against ``tracked_companies``.

    Pure read — no writes. Pass the result to :func:`apply_reclassification`.

    **Safety latch:** if tracker holdings are unavailable, the result is forced
    empty with ``holdings_unavailable=True``. Without this, an empty holdings read would make
    EVERY unpinned portfolio name look "not held" and demote the entire book —
    exactly the wrong move on a transient tracker outage. Mirrors
    ``position_lifecycle``'s None-means-no-op guard. A real all-cash book is
    indistinguishable from an outage here, and refusing to act is the safe choice
    either way (demotions never fire on no data).
    """
    pins = pins or set()
    if holdings.unavailable_reason is not None:
        return Reclassification(
            holdings_unavailable=True,
            unavailable_reason=holdings.unavailable_reason,
        )

    all_held = holdings.all_held
    held = {
        ticker: market_value
        for ticker, market_value in holdings.operating.items()
        if market_value > min_value
    }
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
