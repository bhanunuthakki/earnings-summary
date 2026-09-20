"""Provider-free contracts for the residual shared-FMP-client migrations."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import cast

import pytest

os.environ.setdefault("FMP_API_KEY", "test-key-unused")

import execution.fetch_fmp_10q_json as reports
import execution.fetch_macro_series as macro
import execution.schedule_pre_earnings_refresh as earnings
from macro_series import REGISTRY
from net.client import HttpJsonResponse, JsonShape, JsonValue


def test_financial_report_fetch_uses_shared_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "scope.db"
    with sqlite3.connect(database) as conn:
        conn.execute(
            "CREATE TABLE tracked_companies (ticker TEXT,list_type TEXT,archived_at TEXT,instrument_type TEXT)"
        )
        conn.execute("INSERT INTO tracked_companies VALUES ('META','portfolio',NULL,'equity')")
    monkeypatch.setattr(reports, "DB_PATH", database)
    captured: dict[str, object] = {}
    body: dict[str, JsonValue] = {
        "Consolidated Balance Sheets": [{"value": 1}],
        "a": 1,
        "b": 2,
        "c": 3,
    }

    def fake_get(path: str, **kwargs: object) -> HttpJsonResponse:
        captured["path"] = path
        captured.update(kwargs)
        return HttpJsonResponse(status_code=200, payload=body)

    monkeypatch.setattr(reports.FMP_CLIENT, "get_json", fake_get)
    monkeypatch.setattr(reports, "API_KEY", "test-key")

    code, payload, error = cast(
        Callable[..., tuple[int, dict[str, JsonValue] | list[JsonValue] | None, str | None]],
        getattr(reports, "_fetch_once"),
    )("META", 2026, "Q2")

    assert (code, payload, error) == (200, body, None)
    assert captured["path"] == "financial-reports-json"
    assert captured["params"] == {"symbol": "META", "year": 2026, "period": "Q2"}
    assert captured["expected"] is JsonShape.ANY


def test_macro_fetch_never_bypasses_shared_recovery_circuit() -> None:
    provider = REGISTRY["fed_funds"].providers[0]
    assert cast(Callable[..., None], getattr(macro, "_fetch_json"))(provider) is None


def test_pre_earnings_calendar_validates_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_get(path: str, **kwargs: object) -> HttpJsonResponse:
        captured["path"] = path
        captured.update(kwargs)
        return HttpJsonResponse(
            status_code=200,
            payload=[{"symbol": "nvo", "date": "2026-08-12"}],
        )

    monkeypatch.setattr(earnings.FMP_CLIENT, "get_json", fake_get)
    monkeypatch.setattr(earnings, "API_KEY", "test-key")

    result = cast(
        Callable[[date, date], list[dict[str, str]]], getattr(earnings, "_fetch_earnings_calendar")
    )(date(2026, 8, 8), date(2026, 8, 15))

    assert result == [{"symbol": "NVO", "date": "2026-08-12"}]
    assert captured["path"] == "earnings-calendar"
    assert captured["expected"] is JsonShape.ARRAY
