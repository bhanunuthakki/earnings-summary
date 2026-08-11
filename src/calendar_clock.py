"""One business-date boundary for user-facing research calendars."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

__all__ = ["CALENDAR_TIME_ZONE", "calendar_today"]

CALENDAR_TIME_ZONE = ZoneInfo("America/Los_Angeles")


def calendar_today(now: datetime | None = None) -> date:
    """Return the app's current Pacific calendar date.

    Scheduled jobs and HTTP handlers frequently run with UTC-aware clocks,
    while the owner uses the app in Pacific time. Converting the instant
    before taking ``.date()`` keeps an event visible through the owner's local
    end of day. Naive injected values are treated as UTC, matching the repo's
    stored-timestamp convention.
    """
    instant = now or datetime.now(UTC)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=UTC)
    return instant.astimezone(CALENDAR_TIME_ZONE).date()
