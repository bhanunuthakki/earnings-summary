"""Unit tests for sources.price — live-price preference stack.

Verifies:
  - FMP cache fallback path reads a valid profile.json
  - FMP cache returns None on missing file
  - FMP cache returns None on malformed price
  - The dataclass exposes source_name so callers can route on it

yfinance path is NOT mocked here (no network in unit tests). The FMP fallback
is verified directly; the chain logic is verified by ordering of the public
function (yfinance first → fmp_cache second).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sources.price import LivePrice, _try_fmp_cache, read_live_price  # noqa: E402
from sources.registry import set_db_path  # noqa: E402


def _write_profile(repo_root: Path, ticker: str, price: float | None) -> Path:
    fmp_dir = repo_root / "data" / "historical" / "fmp"
    fmp_dir.mkdir(parents=True, exist_ok=True)
    path = fmp_dir / f"{ticker}_profile.json"
    payload = [{"price": price}] if price is not None else [{"price": "not-a-number"}]
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_fmp_cache_returns_price_from_valid_profile(tmp_path: Path) -> None:
    # Source log writes go to a real DB; for unit tests we point at a sandbox.
    set_db_path(tmp_path / "nosuch.db")  # missing → log_call no-ops
    _write_profile(tmp_path, "GOOG", 382.08)
    result = _try_fmp_cache(tmp_path, "GOOG")
    assert result is not None
    assert isinstance(result, LivePrice)
    assert result.price == 382.08
    assert result.source_name == "fmp_cache"


def test_fmp_cache_returns_none_on_missing_file(tmp_path: Path) -> None:
    set_db_path(tmp_path / "nosuch.db")
    assert _try_fmp_cache(tmp_path, "NOPE") is None


def test_fmp_cache_returns_none_on_malformed_price(tmp_path: Path) -> None:
    set_db_path(tmp_path / "nosuch.db")
    _write_profile(tmp_path, "BAD", None)  # writes "not-a-number"
    assert _try_fmp_cache(tmp_path, "BAD") is None


def test_fmp_cache_handles_dict_shape_too(tmp_path: Path) -> None:
    """Some FMP endpoints return a top-level dict instead of a list-with-dict."""
    set_db_path(tmp_path / "nosuch.db")
    fmp_dir = tmp_path / "data" / "historical" / "fmp"
    fmp_dir.mkdir(parents=True, exist_ok=True)
    (fmp_dir / "WIX_profile.json").write_text(
        json.dumps({"price": 99.5, "symbol": "WIX"}), encoding="utf-8"
    )
    result = _try_fmp_cache(tmp_path, "WIX")
    assert result is not None
    assert result.price == 99.5


def test_read_live_price_falls_through_to_fmp_when_yfinance_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    """When the yfinance path returns None (e.g. import fails), the stack
    delegates to the FMP cache. This is the cancellation-resilience contract."""
    set_db_path(tmp_path / "nosuch.db")
    _write_profile(tmp_path, "BN", 78.42)
    # Stub _try_yfinance to None to simulate the no-network path.
    import sources.price as price_mod

    monkeypatch.setattr(price_mod, "_try_yfinance", lambda ticker: None)
    result = read_live_price(tmp_path, "BN")
    assert result is not None
    assert result.price == 78.42
    assert result.source_name == "fmp_cache"
