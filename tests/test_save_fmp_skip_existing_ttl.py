"""Tests for the --skip-existing freshness TTL in execution/save_fmp_data.py.

The "stale profile" trap: under --skip-existing the fetcher used to skip ANY
endpoint already marked 'ok', including the time-sensitive ones (profile carries
current price + market cap; DCF/consensus/ratings are as-of-today). A name
promoted from index_member then kept a weeks-old profile snapshot, so the brief's
§1 snapshot + the evaluation "Numbers at a glance" panel showed a stale price
(TECH/RGEN were stuck at the 2026-05-19 snapshot, ~30% below the live tape).

The fix re-fetches a TIME_SENSITIVE endpoint once its cached row ages past
_SKIP_EXISTING_TTL_DAYS; statements (not in the set) keep skipping on a plain
'ok'. These tests pin _snapshot_is_stale's behavior + the endpoint membership.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

# save_fmp_data hard-exits at import if FMP_API_KEY is unset; the TTL helper under
# test makes no network call, so a dummy key is enough to import it. setdefault
# keeps a real key if one is present.
os.environ.setdefault("FMP_API_KEY", "test-key-not-used")

from execution.save_fmp_data import (
    _SKIP_EXISTING_TTL_DAYS,  # pyright: ignore[reportPrivateUsage]
    TIME_SENSITIVE_ENDPOINTS,
    _snapshot_is_stale,  # pyright: ignore[reportPrivateUsage]
)

_NOW = datetime(2026, 6, 23, 12, 0, 0)


def test_price_bearing_endpoints_are_time_sensitive() -> None:
    # profile carries price + market cap (read by §1 snapshot + the eval panel);
    # DCF + market-cap series are as-of-today valuations. All must be re-fetchable.
    assert "profile" in TIME_SENSITIVE_ENDPOINTS
    assert "discounted-cash-flow" in TIME_SENSITIVE_ENDPOINTS
    assert "historical-market-capitalization" in TIME_SENSITIVE_ENDPOINTS
    # Statements only change at earnings — they keep skipping when 'ok'.
    assert "income-statement" not in TIME_SENSITIVE_ENDPOINTS
    assert "balance-sheet-statement" not in TIME_SENSITIVE_ENDPOINTS


def test_fresh_snapshot_is_not_stale() -> None:
    recent = (_NOW - timedelta(hours=12)).isoformat(timespec="seconds")
    assert _snapshot_is_stale(recent, now=_NOW) is False


def test_old_snapshot_is_stale() -> None:
    # The real trap: a ~5-week-old profile snapshot.
    assert _snapshot_is_stale("2026-05-19T17:38:26", now=_NOW) is True


def test_ttl_boundary_is_inclusive() -> None:
    boundary = (_NOW - timedelta(days=_SKIP_EXISTING_TTL_DAYS)).isoformat(timespec="seconds")
    assert _snapshot_is_stale(boundary, now=_NOW) is True
    just_inside = (
        _NOW - timedelta(days=_SKIP_EXISTING_TTL_DAYS) + timedelta(minutes=1)
    ).isoformat(timespec="seconds")
    assert _snapshot_is_stale(just_inside, now=_NOW) is False


def test_missing_or_unparseable_last_pulled_is_stale() -> None:
    # Unknown age -> re-fetch rather than serve a possibly-stale value.
    assert _snapshot_is_stale(None, now=_NOW) is True
    assert _snapshot_is_stale("", now=_NOW) is True
    assert _snapshot_is_stale("not-a-date", now=_NOW) is True
