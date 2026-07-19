"""Phase 3: the threaded live-price prefetch (`compute.metrics_engine.io.
prefetch_live_prices` + `cached_price_reader`) that
execution/compute_derived_metrics.py runs before its per-ticker compute
loop -- mirroring src/dcf/reprice.py's `_fetch_prices` bounded-concurrency
contract. Injected fake price readers only; no network.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

from compute.metrics_engine.io import cached_price_reader, prefetch_live_prices
from sources.price import LivePrice

_ROOT = Path(".")


def _fixed(price: float) -> LivePrice:
    return LivePrice(price=price, fetched_at=datetime.now(UTC), source_name="fake")


def test_prefetch_returns_one_entry_per_ticker() -> None:
    out = prefetch_live_prices(
        _ROOT, ["AAA", "BBB", "CCC"], price_reader=lambda _r, _t: _fixed(10.0)
    )
    assert set(out) == {"AAA", "BBB", "CCC"}
    assert all(v is not None and v.price == 10.0 for v in out.values())


def test_prefetch_degrades_a_ticker_that_raises() -> None:
    events: list[str] = []

    def flaky(repo_root: Path, ticker: str) -> LivePrice | None:
        if ticker == "BAD":
            raise RuntimeError("network exploded")
        return _fixed(5.0)

    def log(event: str, **fields: object) -> None:
        events.append(event)

    out = prefetch_live_prices(_ROOT, ["GOOD", "BAD"], price_reader=flaky, log=log)
    assert out["GOOD"] is not None
    assert out["BAD"] is None
    assert "price_prefetch_error" in events


def test_prefetch_degrades_a_ticker_that_hangs() -> None:
    """A ticker whose fetch blows its per-future timeout budget maps to None
    rather than hanging the whole prefetch (mirrors reprice.py's own
    ticker-timeout contract)."""

    def slow(repo_root: Path, ticker: str) -> LivePrice | None:
        if ticker == "SLOW":
            time.sleep(1.0)
        return _fixed(5.0)

    out = prefetch_live_prices(_ROOT, ["FAST", "SLOW"], price_reader=slow, timeout_s=0.05)
    assert out["FAST"] is not None
    assert out["SLOW"] is None


def test_prefetch_empty_ticker_list_returns_empty_dict() -> None:
    def never_called(repo_root: Path, ticker: str) -> LivePrice | None:
        raise AssertionError("must not be called for an empty scope")

    assert prefetch_live_prices(_ROOT, [], price_reader=never_called) == {}


def test_cached_price_reader_returns_the_wrapped_price() -> None:
    live = _fixed(42.0)
    reader = cached_price_reader(live)
    assert reader(_ROOT, "ANY") is live
    none_reader = cached_price_reader(None)
    assert none_reader(_ROOT, "ANY") is None
