# pyright: reportPrivateUsage=false
"""The historical fetcher routes through the shared retrying FMP client."""

from __future__ import annotations

import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
for _p in (_ROOT / "src", _ROOT / "execution"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import fetch_fmp_historical_data as mod  # noqa: E402

from net.client import HttpJsonResponse  # noqa: E402


def test_fetch_from_fmp_uses_shared_client_without_mutating_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        mod._retarget_paths(tmp_path)
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
