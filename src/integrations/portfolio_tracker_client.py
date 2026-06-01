"""REST client for the companion portfolio-tracker FastAPI.

Pulls live positions / transactions / accounts from the sibling project's API
(default ``http://localhost:8000``) and derives the two things the tracker's raw
endpoints don't surface directly:

* each holding's ``percent_of_portfolio`` (market value / total book), and
* a per-account ``tax_treatment`` (``taxable`` / ``tax_deferred`` / ``tax_free``
  / ``unknown``) inferred from the Plaid account ``type`` + ``subtype``.

Degrades gracefully: when the tracker isn't running — or any call errors / returns
malformed JSON — :func:`fetch_live_portfolio` returns a ``LivePortfolio`` with
``available=False`` and an ``error`` reason instead of raising, so the dashboard's
Portfolio tab shows a "tracker offline" note rather than a 500.

A best-practices spec for clean first-class endpoints (server-derived percent +
tax_treatment, additive ``/api/v1`` namespace, ETag, pagination) lives at
``../portfolio-tracker/docs/api/positions-v1.md``. Once the tracker ships those,
this client can switch to ``GET /api/v1/portfolio/positions`` and drop the local
joins / derivations below.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import cast

import requests

_DEFAULT_API_URL = "http://localhost:8000"
_TIMEOUT_SECONDS = 4.0

# Tax-treatment buckets, in display order.
TAX_BUCKETS: tuple[str, ...] = ("taxable", "tax_deferred", "tax_free", "unknown")


@dataclass(slots=True)
class TaxLot:
    """One account's slice of a position, tagged with its tax treatment."""

    account_id: int
    account_name: str
    quantity: float
    market_value: float | None
    tax_treatment: str


@dataclass(slots=True)
class LivePosition:
    ticker: str | None
    name: str | None
    quantity: float
    market_value: float | None
    cost_basis: float | None
    unrealized_pnl: float | None
    percent_of_portfolio: float | None
    accounts: list[TaxLot] = field(default_factory=list)


@dataclass(slots=True)
class LiveTransaction:
    date: str
    ticker: str | None
    name: str | None
    type: str
    subtype: str | None
    quantity: float | None
    amount: float | None
    account_name: str


@dataclass(slots=True)
class LivePortfolio:
    """Consolidated live portfolio state, or a degraded marker when the tracker
    is unreachable (``available=False`` + ``error``)."""

    available: bool
    api_url: str
    error: str | None = None
    total_market_value: float = 0.0
    positions: list[LivePosition] = field(default_factory=list)
    transactions: list[LiveTransaction] = field(default_factory=list)
    # Market value summed into each tax bucket (taxable / tax_deferred / tax_free /
    # unknown). Bucketed at the LOT level since one position can span accounts with
    # different treatments (e.g. the same name in a Roth IRA and a brokerage).
    by_tax_treatment: dict[str, float] = field(default_factory=dict)


def tax_treatment(account_type: str | None, subtype: str | None) -> str:
    """Map a Plaid account ``type`` + ``subtype`` to a tax bucket.

    Roth (incl. Roth 401k/IRA) + HSA grow tax-free; traditional 401k/IRA/SEP/etc.
    are tax-deferred; an ordinary brokerage is taxable. The ``roth`` check runs
    first so "roth ira" / "roth 401k" land in ``tax_free``, not ``tax_deferred``.
    """
    s = (subtype or "").strip().lower()
    t = (account_type or "").strip().lower()
    if "roth" in s or s == "hsa":
        return "tax_free"
    if (
        "401k" in s
        or "ira" in s
        or s
        in {
            "403b",
            "457b",
            "sep",
            "simple",
            "pension",
            "keogh",
            "retirement",
            "rrsp",
            "sarsep",
            "profit sharing plan",
        }
    ):
        return "tax_deferred"
    if "brokerage" in s or t == "brokerage":
        return "taxable"
    return "unknown"


def fetch_live_portfolio(
    *,
    api_url: str | None = None,
    timeout: float = _TIMEOUT_SECONDS,
    transactions_limit: int = 25,
) -> LivePortfolio:
    """Fetch + consolidate the live portfolio from the tracker REST API.

    Never raises on a tracker problem — returns ``available=False`` with an
    ``error`` reason so callers can degrade. ``api_url`` falls back to the
    ``PORTFOLIO_TRACKER_API_URL`` env var, then ``http://localhost:8000``.
    """
    base = (api_url or os.environ.get("PORTFOLIO_TRACKER_API_URL") or _DEFAULT_API_URL).rstrip("/")
    try:
        holdings = _get(base, "/api/portfolio/holdings", timeout=timeout)
        items = _get(base, "/api/plaid/items", timeout=timeout)
        # limit is coerced to int, so inlining it into the path is injection-safe
        # (and sidesteps the requests `params` typing).
        txns = _get(
            base, f"/api/portfolio/transactions?limit={int(transactions_limit)}", timeout=timeout
        )
    except requests.RequestException as exc:
        return LivePortfolio(available=False, api_url=base, error=f"{type(exc).__name__}: {exc}")
    except ValueError as exc:  # JSON decode / unexpected shape
        return LivePortfolio(available=False, api_url=base, error=f"bad response: {exc}")

    acct_tax = _account_tax_map(items)
    positions = _build_positions(holdings, acct_tax)
    total_mv = sum((p.market_value or 0.0) for p in positions)
    for p in positions:
        p.percent_of_portfolio = (
            (100.0 * (p.market_value or 0.0) / total_mv) if total_mv > 0 else None
        )
    by_tax = {bucket: 0.0 for bucket in TAX_BUCKETS}
    for p in positions:
        for lot in p.accounts:
            by_tax[lot.tax_treatment] = by_tax.get(lot.tax_treatment, 0.0) + (
                lot.market_value or 0.0
            )
    return LivePortfolio(
        available=True,
        api_url=base,
        total_market_value=total_mv,
        positions=positions,
        transactions=_build_transactions(txns),
        by_tax_treatment=by_tax,
    )


def _get(base: str, path: str, *, timeout: float) -> list[dict[str, object]]:
    resp = requests.get(base + path, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        raise ValueError(f"{path} returned {type(data).__name__}, expected a list")
    return cast("list[dict[str, object]]", data)


def _account_tax_map(items: list[dict[str, object]]) -> dict[int, str]:
    """Flatten ``/api/plaid/items`` → ``{account_id: tax_treatment}``."""
    out: dict[int, str] = {}
    for item in items:
        accounts = item.get("accounts")
        if not isinstance(accounts, list):
            continue
        for acct in cast("list[dict[str, object]]", accounts):
            aid = acct.get("account_id")
            if not isinstance(aid, int):
                continue
            out[aid] = tax_treatment(_s(acct.get("type")), _s(acct.get("subtype")))
    return out


def _build_positions(
    holdings: list[dict[str, object]], acct_tax: dict[int, str]
) -> list[LivePosition]:
    positions: list[LivePosition] = []
    for h in holdings:
        lots: list[TaxLot] = []
        raw_accounts = h.get("accounts")
        if isinstance(raw_accounts, list):
            for acct in cast("list[dict[str, object]]", raw_accounts):
                aid = acct.get("account_id")
                aid_int = aid if isinstance(aid, int) else -1
                lots.append(
                    TaxLot(
                        account_id=aid_int,
                        account_name=_s(acct.get("account_name")) or "?",
                        quantity=_f(acct.get("quantity")) or 0.0,
                        market_value=_f(acct.get("institution_value")),
                        tax_treatment=acct_tax.get(aid_int, "unknown"),
                    )
                )
        positions.append(
            LivePosition(
                ticker=_s(h.get("ticker")),
                name=_s(h.get("name")),
                quantity=_f(h.get("total_quantity")) or 0.0,
                market_value=_f(h.get("total_value")),
                cost_basis=_f(h.get("total_cost_basis")),
                unrealized_pnl=_f(h.get("unrealized_pnl")),
                percent_of_portfolio=None,  # filled in once the book total is known
                accounts=lots,
            )
        )
    return positions


def _build_transactions(txns: list[dict[str, object]]) -> list[LiveTransaction]:
    return [
        LiveTransaction(
            date=_s(t.get("date")) or "",
            ticker=_s(t.get("ticker")),
            name=_s(t.get("name")),
            type=_s(t.get("type")) or "",
            subtype=_s(t.get("subtype")),
            quantity=_f(t.get("quantity")),
            amount=_f(t.get("amount")),
            account_name=_s(t.get("account_name")) or "?",
        )
        for t in txns
    ]


def _f(v: object) -> float | None:
    """Coerce a JSON number / Decimal-as-string to float; None on empty/bad."""
    if v is None or v == "":
        return None
    try:
        return float(cast("str | float | int", v))
    except (ValueError, TypeError):
        return None


def _s(v: object) -> str | None:
    return v if isinstance(v, str) else (str(v) if v is not None else None)
