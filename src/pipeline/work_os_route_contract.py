"""Pure route/history contracts for the Work OS shell.

The browser shell currently owns navigation, but its route values are strings.
This module provides the small typed seam needed before history-aware overlays
can be wired into that shell.  It deliberately has no Flask, database, or DOM
dependencies: parsing a URL is a boundary concern and an invalid state must
degrade to a known destination rather than inventing a route.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Final, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator

DestinationSurfaceId: TypeAlias = Literal[
    "screen-cockpit",
    "screen-performance",
    "screen-workspace",
    "screen-brief-library",
    "screen-analytics-playground",
    "screen-audit-log",
    "screen-execution-queue",
]
TransientSurfaceId: TypeAlias = Literal["risk_drawer", "peek"]
SurfaceId: TypeAlias = DestinationSurfaceId | TransientSurfaceId

_IDENTIFIER = r"^[a-z][a-z0-9_-]*$"
_TICKER = r"^[A-Z][A-Z0-9.=-]{0,14}$"
_WIRE_SEPARATOR = "|"


class SurfaceClass(StrEnum):
    """Navigation role used to decide whether a route is history-worthy."""

    DESTINATION = "destination"
    DRAWER = "drawer"
    OVERLAY = "overlay"
    UNKNOWN = "unknown"


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OriginSnapshot(_ClosedModel):
    """The route beneath a transient surface, captured at open time."""

    surface: DestinationSurfaceId
    ticker: str | None = Field(default=None, max_length=15)
    section: str | None = Field(default=None, max_length=48, pattern=_IDENTIFIER)

    @field_validator("ticker")
    @classmethod
    def _ticker_uppercase(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if not normalized or not re.fullmatch(_TICKER, normalized):
            raise ValueError("ticker must be a canonical symbol")
        return normalized


class OverlayRoute(_ClosedModel):
    """Canonical identity for one destination or transient overlay route."""

    surface: DestinationSurfaceId
    ticker: str | None = Field(default=None, max_length=15)
    section: str | None = Field(default=None, max_length=48, pattern=_IDENTIFIER)
    overlay: TransientSurfaceId | None = None
    origin: OriginSnapshot | None = None

    @field_validator("ticker")
    @classmethod
    def _ticker_uppercase(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if not normalized or not re.fullmatch(_TICKER, normalized):
            raise ValueError("ticker must be a canonical symbol")
        return normalized


DEFAULT_ROUTE = OverlayRoute(surface="screen-cockpit")

DESTINATION_SURFACE_IDS: Final[tuple[DestinationSurfaceId, ...]] = (
    "screen-cockpit",
    "screen-performance",
    "screen-workspace",
    "screen-brief-library",
    "screen-analytics-playground",
    "screen-audit-log",
    "screen-execution-queue",
)
_SURFACE_CLASSES: Final[dict[str, SurfaceClass]] = {
    **{surface: SurfaceClass.DESTINATION for surface in DESTINATION_SURFACE_IDS},
    "risk_drawer": SurfaceClass.DRAWER,
    "peek": SurfaceClass.OVERLAY,
}


def classify_surface(surface: str) -> SurfaceClass:
    """Return a closed classification; unknown values never become overlays."""

    return _SURFACE_CLASSES.get(surface.strip().lower(), SurfaceClass.UNKNOWN)


def encode_route(route: OverlayRoute) -> str:
    """Encode a route into a stable, delimiter-safe wire value.

    Empty fields are retained so the format is positional and deterministic.
    The model rejects the separator in identifiers before this function runs.
    """

    origin = route.origin
    fields = (
        route.surface,
        route.ticker or "",
        route.section or "",
        route.overlay or "",
        origin.surface if origin else "",
        origin.ticker if origin and origin.ticker else "",
        origin.section if origin and origin.section else "",
    )
    return _WIRE_SEPARATOR.join(fields)


def decode_route(value: str) -> OverlayRoute | None:
    """Decode only the exact current wire shape; malformed state returns None."""

    fields = value.split(_WIRE_SEPARATOR)
    if len(fields) != 7 or any("\x00" in field for field in fields):
        return None
    surface, ticker, section, overlay, origin_surface, origin_ticker, origin_section = fields
    try:
        origin = (
            OriginSnapshot.model_validate(
                {
                    "surface": origin_surface,
                    "ticker": origin_ticker or None,
                    "section": origin_section or None,
                }
            )
            if origin_surface
            else None
        )
        return OverlayRoute.model_validate(
            {
                "surface": surface,
                "ticker": ticker or None,
                "section": section or None,
                "overlay": overlay or None,
                "origin": origin,
            }
        )
    except (TypeError, ValueError):
        return None


def fallback_route(value: str | None) -> OverlayRoute:
    """Resolve persisted history state to a safe route when decoding fails."""

    decoded = decode_route(value) if value else None
    return decoded or DEFAULT_ROUTE


__all__ = [
    "DEFAULT_ROUTE",
    "DESTINATION_SURFACE_IDS",
    "DestinationSurfaceId",
    "OriginSnapshot",
    "OverlayRoute",
    "SurfaceClass",
    "SurfaceId",
    "classify_surface",
    "decode_route",
    "encode_route",
    "fallback_route",
]
