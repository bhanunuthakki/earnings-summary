"""Pure PRE/POST earnings doorway selection for user-facing research surfaces."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict

PRE_EARNINGS_WINDOW_DAYS = 7
POST_EARNINGS_WINDOW_DAYS = 90

EarningsPhase = Literal["pre", "post"]
EarningsDoorwayStatus = Literal["available", "pending", "unavailable"]


class EarningsDoorway(BaseModel):
    """One honest earnings doorway state for a company and event."""

    model_config = ConfigDict(frozen=True)

    status: EarningsDoorwayStatus
    phase: EarningsPhase | None = None
    event_date: date | None = None
    label: str
    route: str | None = None


def resolve_earnings_doorway(
    *,
    today: date,
    event_date: date | None,
    pre_route: str | None,
    post_route: str | None,
) -> EarningsDoorway:
    """Choose PRE or POST from dates and already-verified artifact routes.

    ``today`` is injected so the caller owns the application calendar. Routes
    are capability evidence: an absent route means the corresponding persisted
    artifact is absent, so the result can never create a speculative link.
    """

    if event_date is None:
        return EarningsDoorway(
            status="unavailable",
            label="Earnings artifact unavailable",
        )

    days_after_event = (today - event_date).days
    if days_after_event < -PRE_EARNINGS_WINDOW_DAYS or days_after_event > (
        POST_EARNINGS_WINDOW_DAYS
    ):
        return EarningsDoorway(
            status="unavailable",
            event_date=event_date,
            label="Earnings artifact unavailable",
        )

    if days_after_event == 0 and post_route is not None:
        return EarningsDoorway(
            status="available",
            phase="post",
            event_date=event_date,
            label="Post-earnings readout →",
            route=post_route,
        )

    if days_after_event <= 0:
        if pre_route is not None:
            return EarningsDoorway(
                status="available",
                phase="pre",
                event_date=event_date,
                label="Pre-earnings brief →",
                route=pre_route,
            )
        return EarningsDoorway(
            status="pending",
            phase="pre",
            event_date=event_date,
            label="Pre-earnings brief pending",
        )

    if post_route is not None:
        return EarningsDoorway(
            status="available",
            phase="post",
            event_date=event_date,
            label="Post-earnings readout →",
            route=post_route,
        )
    return EarningsDoorway(
        status="pending",
        phase="post",
        event_date=event_date,
        label="Post-earnings readout pending",
    )


__all__ = [
    "POST_EARNINGS_WINDOW_DAYS",
    "PRE_EARNINGS_WINDOW_DAYS",
    "EarningsDoorway",
    "resolve_earnings_doorway",
]
