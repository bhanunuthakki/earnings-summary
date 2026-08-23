from __future__ import annotations

import pytest
from pydantic import ValidationError

from pipeline.work_os_route_contract import (
    DEFAULT_ROUTE,
    OriginSnapshot,
    OverlayRoute,
    SurfaceClass,
    classify_surface,
    decode_route,
    encode_route,
    fallback_route,
)


def _route() -> OverlayRoute:
    return OverlayRoute(
        surface="company_desk",
        ticker="mELi",
        section="thesis",
        overlay="peek",
        origin=OriginSnapshot(surface="performance_risk", ticker="NU", section="correlation"),
    )


def test_route_normalizes_and_round_trips_canonically() -> None:
    route = _route()

    assert route.ticker == "MELI"
    encoded = encode_route(route)
    assert encoded == "company_desk|MELI|thesis|peek|performance_risk|NU|correlation"
    assert decode_route(encoded) == route
    assert encode_route(decode_route(encoded)) == encoded


def test_route_wire_is_deterministic_and_rejects_ambiguous_delimiters() -> None:
    assert encode_route(_route()) == encode_route(_route())
    with pytest.raises(ValidationError):
        OverlayRoute(surface="company_desk", overlay="peek|bad")


def test_decode_falls_back_for_invalid_or_unknown_state() -> None:
    assert decode_route("not-a-route") is None
    assert decode_route("company_desk|MELI|thesis") is None
    assert fallback_route("not-a-route") == DEFAULT_ROUTE
    assert fallback_route(encode_route(_route())) == _route()


def test_origin_snapshot_is_immutable_and_optional_context_is_explicit() -> None:
    origin = OriginSnapshot(surface="cockpit")
    assert origin.ticker is None
    assert origin.section is None
    with pytest.raises(ValidationError):
        OriginSnapshot(surface="cockpit", ticker="bad value")


def test_surface_registry_has_closed_classification_and_unknown_fallback() -> None:
    assert classify_surface("peek") is SurfaceClass.OVERLAY
    assert classify_surface("company_desk") is SurfaceClass.DESTINATION
    assert classify_surface("risk_drawer") is SurfaceClass.DRAWER
    assert classify_surface("made-up") is SurfaceClass.UNKNOWN
