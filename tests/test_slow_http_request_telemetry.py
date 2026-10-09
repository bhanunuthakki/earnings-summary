"""M2 telemetry contract: Server-Timing everywhere + the slow_http_request log.

VAL-SERVE-029: every response -- 200s, 304s and error responses -- carries the
``Server-Timing: app;dur=...`` header, and any request whose server-side
duration reaches 500ms emits exactly one well-formed ``slow_http_request``
JSON record with ``method``, ``path``, ``status``, ``duration_ms``, the
``panel_cache`` state and a correlation id. The synthetic cases pin the two
contract-named shapes:

* a builder-slow cold request (the renderer sleeps 600ms) reports
  ``panel_cache: "miss"`` and leaves exactly one record, while a fast
  cache-served follow-up adds none;
* a request that is slow while being served from the panel cache reports
  ``panel_cache: "hit"``;
* a slow request whose builder raises still gets the 500 response through the
  error handler, with the header on the 500 and exactly one record carrying
  ``status: 500``.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

if TYPE_CHECKING:
    from flask import Flask

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "execution"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import comments_server
from comments_server_panel_cache import (
    PanelCacheBusy,
    PanelCacheHit,
    PanelCacheReservation,
    PanelResponseCache,
)

from tests.comments_server_seams import patch_server_seam

OVERVIEW_PATH = "/api/panel/overview"
# Comfortably above the 500ms telemetry threshold so a slow host cannot flake
# the record's presence, and far below pytest's own timeouts.
SLOW_SECONDS = 0.6


class _LogRecorder(logging.Handler):
    """Collect raw log messages so the JSON telemetry records can be parsed."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.fixture
def app(tmp_path: Path, migrated_db: Callable[..., Path]) -> Flask:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    migrated_db(data_dir / "portfolio.db")
    return comments_server.create_app(tmp_path)


def _attach(app: Flask) -> _LogRecorder:
    recorder = _LogRecorder()
    # The real server configures INFO on this logger; under pytest the root
    # logger sits at WARNING, which would drop the records before any handler.
    app.logger.setLevel(logging.INFO)
    app.logger.addHandler(recorder)
    return recorder


def _slow_records(recorder: _LogRecorder) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for message in recorder.messages:
        # The same logger carries plain-text messages (the unhandled-exception
        # error line); only the telemetry records are JSON.
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        record = cast("dict[str, object]", payload)
        if record.get("event") == "slow_http_request":
            records.append(record)
    return records


def _assert_well_formed(record: dict[str, object], *, status: int, panel_cache: str) -> None:
    """The record carries every contract field with the observed values."""
    assert record.get("event") == "slow_http_request"
    assert record.get("method") == "GET"
    assert record.get("path") == OVERVIEW_PATH
    assert record.get("status") == status
    duration_ms = record.get("duration_ms")
    assert isinstance(duration_ms, (int, float))
    assert not isinstance(duration_ms, bool)
    assert duration_ms >= 500.0
    assert record.get("panel_cache") == panel_cache
    correlation_id = record.get("correlation_id")
    assert isinstance(correlation_id, str)
    assert correlation_id


def _server_timing_dur_ms(app: Flask, status: int) -> float:
    """Fetch the overview and return its Server-Timing duration in ms."""
    with app.test_client() as client:
        response = client.get(OVERVIEW_PATH)
        assert response.status_code == status
        timing = response.headers.get("Server-Timing", "")
        assert timing.startswith("app;dur=")
        return float(timing.removeprefix("app;dur="))


def test_builder_slow_request_emits_exactly_one_record_as_miss(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _attach(app)

    def slow_overview(*_args: object, **_kwargs: object) -> str:
        time.sleep(SLOW_SECONDS)
        return '<section class="panel">overview</section>'

    patch_server_seam(monkeypatch, "render_overview_panel", slow_overview)

    with app.test_client() as client:
        # A fast non-panel request must not emit anything yet.
        assert client.get("/healthz").status_code == 200
        assert _slow_records(recorder) == []

        response = client.get(OVERVIEW_PATH)
        assert response.status_code == 200
        assert response.headers.get("X-Panel-Cache") == "miss"
        timing = response.headers.get("Server-Timing", "")
        assert timing.startswith("app;dur=")
        assert float(timing.removeprefix("app;dur=")) >= 500.0

    records = _slow_records(recorder)
    assert len(records) == 1
    _assert_well_formed(records[0], status=200, panel_cache="miss")

    # A fast follow-up served from the panel cache adds no further record.
    with app.test_client() as client:
        hit = client.get(OVERVIEW_PATH)
        assert hit.status_code == 200
        assert hit.headers.get("X-Panel-Cache") == "hit"
    assert len(_slow_records(recorder)) == 1


def test_cache_served_slow_request_reports_hit(app: Flask, monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _attach(app)

    def fast_overview(*_args: object, **_kwargs: object) -> str:
        return '<section class="panel">overview</section>'

    patch_server_seam(monkeypatch, "render_overview_panel", fast_overview)

    with app.test_client() as client:
        first = client.get(OVERVIEW_PATH)
        assert first.status_code == 200
        assert first.headers.get("X-Panel-Cache") == "miss"
        assert _slow_records(recorder) == []

        original_get_or_reserve = PanelResponseCache.get_or_reserve

        def slow_on_hit(
            cache: PanelResponseCache, key: str
        ) -> PanelCacheHit | PanelCacheReservation | PanelCacheBusy:
            lookup = original_get_or_reserve(cache, key)
            if isinstance(lookup, PanelCacheHit):
                time.sleep(SLOW_SECONDS)
            return lookup

        monkeypatch.setattr(PanelResponseCache, "get_or_reserve", slow_on_hit)

        hit = client.get(OVERVIEW_PATH)
        assert hit.status_code == 200
        assert hit.headers.get("X-Panel-Cache") == "hit"
        timing = hit.headers.get("Server-Timing", "")
        assert timing.startswith("app;dur=")
        assert float(timing.removeprefix("app;dur=")) >= 500.0

    records = _slow_records(recorder)
    assert len(records) == 1
    _assert_well_formed(records[0], status=200, panel_cache="hit")


def test_slow_500_response_carries_server_timing_and_one_record(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _attach(app)

    def broken_overview(*_args: object, **_kwargs: object) -> str:
        time.sleep(SLOW_SECONDS)
        raise RuntimeError("synthetic builder failure")

    patch_server_seam(monkeypatch, "render_overview_panel", broken_overview)

    dur_ms = _server_timing_dur_ms(app, 500)
    assert dur_ms >= 500.0

    records = _slow_records(recorder)
    assert len(records) == 1
    _assert_well_formed(records[0], status=500, panel_cache="miss")
