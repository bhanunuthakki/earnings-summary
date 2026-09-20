"""The historical fetcher routes through the shared retrying FMP client."""

from __future__ import annotations

import pathlib
import sqlite3
from collections.abc import Callable
from typing import cast

import pytest

from execution import fetch_fmp_historical_data as mod
from net.client import HttpJsonResponse


def test_fetch_from_fmp_uses_shared_client_without_mutating_params(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    database = tmp_path / "scope.db"
    with sqlite3.connect(database) as conn:
        conn.execute(
            "CREATE TABLE tracked_companies (ticker TEXT,list_type TEXT,archived_at TEXT,instrument_type TEXT)"
        )
        conn.execute("INSERT INTO tracked_companies VALUES ('AAA','portfolio',NULL,'equity')")
    monkeypatch.setattr(mod.db, "DB_PATH", str(database))
    captured: dict[str, object] = {}

    def _fake_get(path: str, **kwargs: object) -> HttpJsonResponse:
        captured["path"] = path
        captured.update(kwargs)
        return HttpJsonResponse(status_code=200, payload=[{"revenue": 1}])

    monkeypatch.setattr(mod.FMP_CLIENT, "get_json", _fake_get)
    params = {"symbol": "AAA"}

    out = mod.fetch_from_fmp("income-statement", params)

    assert out == [{"revenue": 1}]
    assert captured["path"] == "income-statement"
    assert captured["params"] == {"symbol": "AAA"}
    assert params == {"symbol": "AAA"}


def test_retarget_paths_binds_raw_cache_and_database_to_state_root(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = (mod.DATA_DIR, mod.db.PROJECT_ROOT, mod.db.DATA_DIR, mod.db.DB_PATH, mod.db.FMP_DIR)
    monkeypatch.setenv("FMP_API_KEY", "unit-key")  # pragma: allowlist secret
    try:
        cast(Callable[[pathlib.Path], None], getattr(mod, "_retarget_paths"))(tmp_path)
        assert pathlib.Path(mod.DATA_DIR) == tmp_path / "data/historical/fmp"
        assert pathlib.Path(mod.db.DB_PATH) == tmp_path / "data/portfolio.db"
        assert mod.FMP_API_KEY == "unit-key"  # pragma: allowlist secret
    finally:
        (
            mod.DATA_DIR,
            mod.db.PROJECT_ROOT,
            mod.db.DATA_DIR,
            mod.db.DB_PATH,
            mod.db.FMP_DIR,
        ) = original
