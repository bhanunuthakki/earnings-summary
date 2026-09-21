"""Read current, frozen comparable membership without resolving or mutating it."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import cast

from compute.comparable_sets import METHOD_VERSION, comparable_set_id


@dataclass(frozen=True)
class FrozenComparableSet:
    source_id: str
    resolved_at: str
    members: tuple[tuple[str, str], ...]


def read_frozen_comparable_set(
    conn: sqlite3.Connection, ticker: str, *, as_of: date | None = None
) -> FrozenComparableSet | None:
    """Use the current rule version only; context-only and closed members cannot rank."""
    source_id = comparable_set_id(ticker.upper(), METHOD_VERSION)
    row = conn.execute(
        "SELECT resolved_at FROM comparable_sets WHERE comparable_set_id = ?",
        (source_id,),
    ).fetchone()
    if row is None:
        return None
    cutoff = as_of or datetime.now(UTC).date()
    if datetime.fromisoformat(str(row[0])).date() > cutoff:
        return None
    today = cutoff.isoformat()
    rows = conn.execute(
        "SELECT member_ticker, membership_reason FROM comparable_set_members "
        "WHERE comparable_set_id = ? AND context_only = 0 AND valid_from <= ? "
        "AND (valid_to IS NULL OR valid_to > ?) ORDER BY member_ticker",
        (source_id, today, today),
    ).fetchall()
    members = tuple((str(r[0]), str(r[1])) for r in cast("list[tuple[object, object]]", rows))
    return FrozenComparableSet(source_id, str(row[0]), members)
