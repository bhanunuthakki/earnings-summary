from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from calendar_clock import calendar_today
from pipeline.earnings_doorway import (
    POST_EARNINGS_WINDOW_DAYS,
    PRE_EARNINGS_WINDOW_DAYS,
    resolve_earnings_doorway,
)

EVENT_DATE = date(2026, 8, 11)
PRE_ROUTE = "/api/peek/earnings-prep?ticker=NU"
POST_ROUTE = "/api/peek/earnings-readout?ticker=NU"


@pytest.mark.parametrize(
    ("offset", "status", "phase", "route", "label"),
    [
        (-8, "unavailable", None, None, "Earnings artifact unavailable"),
        (-7, "available", "pre", PRE_ROUTE, "Pre-earnings brief →"),
        (90, "available", "post", POST_ROUTE, "Post-earnings readout →"),
        (91, "unavailable", None, None, "Earnings artifact unavailable"),
    ],
)
def test_resolver_enforces_inclusive_window_boundaries(
    offset: int,
    status: str,
    phase: str | None,
    route: str | None,
    label: str,
) -> None:
    resolved = resolve_earnings_doorway(
        today=date.fromordinal(EVENT_DATE.toordinal() + offset),
        event_date=EVENT_DATE,
        pre_route=PRE_ROUTE,
        post_route=POST_ROUTE,
    )

    assert PRE_EARNINGS_WINDOW_DAYS == 7
    assert POST_EARNINGS_WINDOW_DAYS == 90
    assert resolved.status == status
    assert resolved.phase == phase
    assert resolved.route == route
    assert resolved.label == label


def test_t0_keeps_pre_until_a_real_post_artifact_exists() -> None:
    before_post = resolve_earnings_doorway(
        today=EVENT_DATE,
        event_date=EVENT_DATE,
        pre_route=PRE_ROUTE,
        post_route=None,
    )
    after_post = resolve_earnings_doorway(
        today=EVENT_DATE,
        event_date=EVENT_DATE,
        pre_route=PRE_ROUTE,
        post_route=POST_ROUTE,
    )

    assert before_post.phase == "pre"
    assert before_post.route == PRE_ROUTE
    assert after_post.phase == "post"
    assert after_post.route == POST_ROUTE


def test_missing_pre_or_post_artifact_is_pending_without_a_route() -> None:
    pre = resolve_earnings_doorway(
        today=date(2026, 8, 5),
        event_date=EVENT_DATE,
        pre_route=None,
        post_route=None,
    )
    post = resolve_earnings_doorway(
        today=date(2026, 8, 12),
        event_date=EVENT_DATE,
        pre_route=PRE_ROUTE,
        post_route=None,
    )

    assert (pre.status, pre.label, pre.route) == (
        "pending",
        "Pre-earnings brief pending",
        None,
    )
    assert (post.status, post.label, post.route) == (
        "pending",
        "Post-earnings readout pending",
        None,
    )


def test_missing_or_unparseable_calendar_degrades_to_unavailable() -> None:
    missing = resolve_earnings_doorway(
        today=EVENT_DATE,
        event_date=None,
        pre_route=PRE_ROUTE,
        post_route=POST_ROUTE,
    )

    assert missing.status == "unavailable"
    assert missing.route is None
    assert missing.label == "Earnings artifact unavailable"


def test_pacific_business_date_does_not_roll_at_utc_midnight() -> None:
    instant = datetime(2026, 8, 11, 0, 30, tzinfo=UTC)

    pacific_today = calendar_today(instant)
    resolved = resolve_earnings_doorway(
        today=pacific_today,
        event_date=date(2026, 8, 3),
        pre_route=PRE_ROUTE,
        post_route=POST_ROUTE,
    )

    assert pacific_today == date(2026, 8, 10)
    assert resolved.phase == "post"
    assert resolved.status == "available"
