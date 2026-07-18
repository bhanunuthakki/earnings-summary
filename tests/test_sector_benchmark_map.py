"""Unit tests for compute.sector_benchmark_map (docs/design/
comparable_sets_bottoms_up.md §4)."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from compute.sector_benchmark_map import get_benchmark_proxy  # noqa: E402


def test_known_industry_resolves() -> None:
    proxy = get_benchmark_proxy("Semiconductors")
    assert proxy is not None
    assert proxy.etf == "SMH"
    assert proxy.sector_etf == "XLK"


def test_unmapped_industry_returns_none() -> None:
    assert get_benchmark_proxy("Some Made-Up Industry") is None


def test_none_industry_returns_none() -> None:
    assert get_benchmark_proxy(None) is None


def test_credit_services_has_no_dedicated_etf_but_has_sector_fallback() -> None:
    proxy = get_benchmark_proxy("Credit Services")
    assert proxy is not None
    assert proxy.etf is None
    assert proxy.sector_etf == "XLF"
