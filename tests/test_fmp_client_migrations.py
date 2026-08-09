"""Provider-free contracts for fetchers migrated to the shared FMP client."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

import execution.fetch_fmp_earnings_calendar as earnings
from net.client import HttpJsonResponse, JsonShape


def test_earnings_fetch_routes_through_shared_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_get(path: str, **kwargs: object) -> HttpJsonResponse:
        captured["path"] = path
        captured.update(kwargs)
        return HttpJsonResponse(status_code=200, payload=[{"symbol": "NVO"}])

    monkeypatch.setattr(earnings.FMP_CLIENT, "get_json", fake_get)
    monkeypatch.setattr(earnings, "FMP_API_KEY", "test-key")

    result = earnings.fetch_earnings("NVO", 12)

    assert result == [{"symbol": "NVO"}]
    assert captured["path"] == "earnings"
    assert captured["expected"] is JsonShape.ARRAY
    params = captured["params"]
    assert isinstance(params, Mapping)
    assert params == {"symbol": "NVO", "limit": 12}
    assert "apikey" not in params


def test_earnings_fetch_rejects_array_with_non_object_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get(*_args: object, **_kwargs: object) -> HttpJsonResponse:
        return HttpJsonResponse(status_code=200, payload=["refused"])

    monkeypatch.setattr(
        earnings.FMP_CLIENT,
        "get_json",
        fake_get,
    )

    assert earnings.fetch_earnings("NVO", 12) is None
