"""Canonical, fact-aware unit resolution for analyst KPI definitions."""

from __future__ import annotations

import sqlite3

from models.facts import Unit
from models.unit_convert import same_family

_RATE_NAME_TOKENS = (
    "growth",
    "yoy",
    "y/y",
    "deceleration",
    "decel",
    "retention",
    "churn",
    "margin",
    "yield",
    "penetration",
    "take rate",
    "attach rate",
    "%",
)
_DOLLAR_LEVEL_NAME_TOKENS = (
    "remaining performance obligation",
    "rpo",
    "backlog",
    "bookings",
    "deferred revenue",
    "gross merchandise",
    "gmv",
    "payment volume",
    "tpv",
    "assets under management",
    "aum",
    "deposits",
    "loans outstanding",
)


def infer_unit(name: str) -> Unit:
    """Use the established no-facts bootstrap heuristic for a new definition."""

    normalized = name.lower()
    if any(token in normalized for token in _RATE_NAME_TOKENS):
        return Unit.PERCENT
    if any(
        token in normalized
        for token in (
            "customers",
            "customer count",
            "millions",
            "headcount",
            "subscribers",
            "users",
            "accounts",
            "members",
            "merchants",
        )
    ):
        return Unit.COUNT
    if any(
        token in normalized
        for token in ("usd", "$", "dollar", "arpac", "arpu", "cost-to-serve", "cost to serve")
    ) or any(token in normalized for token in _DOLLAR_LEVEL_NAME_TOKENS):
        return Unit.ACTUAL
    if "bps" in normalized or "basis point" in normalized:
        return Unit.BPS
    if "ratio" in normalized and "%" not in normalized and "rate" not in normalized:
        return Unit.RATIO
    return Unit.PERCENT


def dominant_fact_unit(connection: sqlite3.Connection, ticker: str, name: str) -> Unit | None:
    """Return the modal recorded fact unit, breaking ties by latest period."""

    row = connection.execute(
        "SELECT f.unit FROM kpi_facts f "
        "JOIN kpi_definitions d ON d.id=f.kpi_definition_id "
        "WHERE d.ticker=? AND d.name=? AND f.unit IS NOT NULL "
        "GROUP BY f.unit ORDER BY COUNT(*) DESC, MAX(f.period_end) DESC LIMIT 1",
        (ticker.upper(), name),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    try:
        return Unit(str(row[0]))
    except ValueError:
        return None


def resolve_definition_unit(connection: sqlite3.Connection, ticker: str, name: str) -> Unit:
    """Prefer recorded facts, then a valid existing definition, then bootstrap inference."""

    row = connection.execute(
        "SELECT unit FROM kpi_definitions WHERE ticker=? AND name=?",
        (ticker.upper(), name),
    ).fetchone()
    try:
        existing = Unit(str(row[0])) if row is not None and row[0] is not None else None
    except ValueError:
        existing = None
    fact = dominant_fact_unit(connection, ticker, name)
    if fact is not None and (existing is None or not same_family(existing, fact)):
        return fact
    return existing or fact or infer_unit(name)
