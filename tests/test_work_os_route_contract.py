from __future__ import annotations

import pytest
from pydantic import ValidationError

from pipeline.work_os_route_contract import (
    DEFAULT_ROUTE,
    DESTINATION_SURFACE_IDS,
    OriginSnapshot,
    OverlayRoute,
    SurfaceClass,
    classify_surface,
    decode_route,
    encode_route,
    fallback_route,
)
from pipeline.work_os_shell import SCREEN_SPECS


def _route() -> OverlayRoute:
    return OverlayRoute(
        surface="screen-workspace",
        ticker="mELi",
        section="thesis",
        overlay="peek",
        origin=OriginSnapshot(surface="screen-performance", ticker="NU", section="correlation"),
    )


def test_route_normalizes_and_round_trips_canonically() -> None:
    route = _route()

    assert route.ticker == "MELI"
    encoded = encode_route(route)
    assert encoded == "screen-workspace|MELI|thesis|peek|screen-performance|NU|correlation"
    decoded = decode_route(encoded)
    assert decoded == route
    assert decoded is not None
    assert encode_route(decoded) == encoded


def test_route_wire_is_deterministic_and_rejects_ambiguous_delimiters() -> None:
    assert encode_route(_route()) == encode_route(_route())
    with pytest.raises(ValidationError):
        OverlayRoute.model_validate({"surface": "screen-workspace", "overlay": "peek|bad"})


def test_decode_falls_back_for_invalid_or_unknown_state() -> None:
    assert decode_route("not-a-route") is None
    assert decode_route("screen-workspace|MELI|thesis") is None
    assert decode_route("made_up||||||") is None
    assert decode_route("screen-workspace|||peek|made_up||") is None
    assert fallback_route("not-a-route") == DEFAULT_ROUTE
    assert fallback_route("made_up||||||") == DEFAULT_ROUTE
    assert fallback_route(encode_route(_route())) == _route()


def test_origin_snapshot_is_immutable_and_optional_context_is_explicit() -> None:
    origin = OriginSnapshot(surface="screen-cockpit")
    assert origin.ticker is None
    assert origin.section is None
    with pytest.raises(ValidationError):
        OriginSnapshot(surface="screen-cockpit", ticker="bad value")
    with pytest.raises(ValidationError):
        OriginSnapshot.model_validate({"surface": "made_up"})


def test_surface_registry_tracks_shell_destinations_and_rejects_unknown_values() -> None:
    assert classify_surface("peek") is SurfaceClass.OVERLAY
    assert classify_surface("screen-workspace") is SurfaceClass.DESTINATION
    assert classify_surface("risk_drawer") is SurfaceClass.DRAWER
    assert classify_surface("made-up") is SurfaceClass.UNKNOWN
    assert {spec.screen_id for spec in SCREEN_SPECS} == set(DESTINATION_SURFACE_IDS)
    with pytest.raises(ValidationError):
        OverlayRoute.model_validate({"surface": "made_up"})
