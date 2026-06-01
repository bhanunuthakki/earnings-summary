# pyright: reportPrivateUsage=false
# This suite checks module-private resolver/fetch internals (_resolve_url,
# _fetch_json, _populate_one_series) by design.
"""Plan 7.6: macro-series providers are all on /stable after the migration.

The legacy /api/v3 `historical-price-full` fallbacks were retired; every registry
provider now resolves to a /stable URL, and a stable flat-list round-trip parses
to the same series rows the stable primary always produced.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("FMP_API_KEY", "test-key-unused")

import execution.fetch_macro_series as fms
from macro_series import REGISTRY, ProviderSpec


def test_all_registry_providers_resolve_to_stable() -> None:
    for series in REGISTRY.values():
        for provider in series.providers:
            url = fms._resolve_url(provider)
            assert url.startswith(fms.FMP_STABLE), (series.series_id, provider.path, url)
            assert "/api/v3" not in url and "/api/v4" not in url


def test_no_legacy_historical_price_full_pin_remains() -> None:
    paths = [p.path for s in REGISTRY.values() for p in s.providers]
    assert not any("historical-price-full" in p for p in paths)


def test_resolve_url_never_emits_v3_even_for_a_legacy_path() -> None:
    """Defensive: even a v3-style path no longer routes to /api/v3 — the resolver
    has no v3/v4 branch left, so a stray legacy path lands on /stable."""
    url = fms._resolve_url(ProviderSpec(kind="fmp_historical", path="historical-price-full/^VIX"))
    assert "/api/v3" not in url
    assert url.startswith(fms.FMP_STABLE)


def test_stable_flat_list_round_trip_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stable historical-price-eod/full flat list parses to (date, value) rows —
    the same parse the stable primary always did (mocked round-trip, dry-run)."""
    vix = REGISTRY["vix"]
    payload: list[dict[str, object]] = [
        {"date": "2026-05-29", "close": 18.5, "open": 18.0},
        {"date": "2026-05-28", "close": 19.0, "open": 19.2},
    ]

    def fake_fetch(provider: ProviderSpec, *, sleep_seconds: float = 0.0) -> object:
        return payload

    monkeypatch.setattr(fms, "_fetch_json", fake_fetch)
    n = fms._populate_one_series(vix, dry_run=True, sleep_seconds=0.0)
    assert n == 2  # both rows parsed (date + close present)
