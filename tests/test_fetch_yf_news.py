"""Free yfinance journalism feed (execution/fetch_yf_news.py) + the flip of the
paid WebSearch+LLM path from default to opt-in.

Why this feed exists, measured 2026-07-25 (lifetime):
    websearch_opus  $453.39 / 1,081 calls / 79 stored rows = $5.74 per row
    yfinance .news  $0.00   / ~0.3s per ticker / 10 items per ticker
93% of paid calls stored nothing — the job re-searched the same window daily
and `(ticker, url)` dedup discarded the re-found URLs. Root cause upstream:
FMP's stock-news endpoint 402s, so the LLM fallback silently became primary.

No network: yfinance payloads are fixtures.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Never

import pytest

from pipeline.row_validation import RowValidationDriftError

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load() -> object:
    spec = importlib.util.spec_from_file_location(
        "fetch_yf_news_under_test", PROJECT_ROOT / "execution" / "fetch_yf_news.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fetch_yf_news_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


yfnews = _load()


def _recent_iso(hours: int = 2) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _modern_item(title: str = "NU beats on revenue", **over: object) -> dict[str, object]:
    """The CURRENT yfinance shape (nested under `content`), verified live."""
    content: dict[str, object] = {
        "title": title,
        "pubDate": _recent_iso(),
        "provider": {"displayName": "Reuters"},
        "canonicalUrl": {"url": "https://reuters.com/a"},
        "summary": "Revenue rose 12% YoY.",
    }
    content.update(over)
    return {"content": content}


def test_modern_nested_shape_maps_to_a_row() -> None:
    rows = yfnews.rows_for_ticker("nu", [_modern_item()], days=7)
    assert len(rows) == 1
    row = rows[0]
    assert row.ticker == "NU"  # upper-cased
    assert row.headline == "NU beats on revenue"
    assert row.url == "https://reuters.com/a"
    assert row.source == "Reuters"
    assert row.source_feed == "yf_news"
    # The store's exact contract: UTC 'YYYY-MM-DD HH:MM:SS', no 'T', no zone.
    datetime.strptime(row.published_at, "%Y-%m-%d %H:%M:%S")


def test_legacy_flat_shape_still_maps() -> None:
    """Older yfinance builds returned a flat dict with an epoch timestamp. An
    unofficial API drifts; reading both shapes is cheaper than an outage."""
    epoch = int((datetime.now(UTC) - timedelta(hours=1)).timestamp())
    rows = yfnews.rows_for_ticker(
        "META",
        [
            {
                "title": "Meta ships a thing",
                "link": "https://example.com/meta",
                "publisher": "Bloomberg",
                "providerPublishTime": epoch,
            }
        ],
        days=7,
    )
    assert len(rows) == 1
    assert rows[0].source == "Bloomberg"
    assert rows[0].url == "https://example.com/meta"


@pytest.mark.parametrize(
    ("item", "why"),
    [
        (_modern_item(pubDate=None), "undateable"),
        (_modern_item(pubDate="not a date"), "unparseable date"),
        (_modern_item(canonicalUrl=None, clickThroughUrl=None), "no url (the dedup key)"),
        (_modern_item(title=""), "empty headline"),
        ({"content": {}}, "empty content"),
        ("not a dict", "wrong type"),
    ],
)
def test_unusable_items_are_dropped_never_guessed(item: object, why: str) -> None:
    """The 'never fabricate a date' rule the LLM ingester was given applies to
    the free feed too: a story we cannot date or link is dropped, not invented."""
    assert yfnews.rows_for_ticker("NU", [item], days=7) == [], why


def test_items_outside_the_window_are_dropped() -> None:
    old = _modern_item(
        pubDate=(datetime.now(UTC) - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    assert yfnews.rows_for_ticker("NU", [old], days=7) == []


def test_one_bad_item_does_not_lose_the_others() -> None:
    items = [_modern_item("good one"), {"content": {}}, _modern_item("second good")]
    rows = yfnews.rows_for_ticker("NU", items, days=7)
    assert {r.headline for r in rows} == {"good one", "second good"}


def test_fetch_degrades_to_empty_on_transport_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """yfinance is unofficial — this feed must never take the pipeline down."""
    import builtins

    real_import = builtins.__import__

    def boom(name: str, *a: object, **kw: object) -> object:
        if name == "yfinance":
            raise RuntimeError("yfinance exploded")
        return real_import(name, *a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", boom)
    assert yfnews.fetch_news_for_ticker("NU") == []


def test_fetch_many_survives_a_failing_ticker() -> None:
    def flaky(ticker: str, *, days: int) -> list[object]:
        if ticker == "BAD":
            raise RuntimeError("nope")
        return yfnews.rows_for_ticker(ticker, [_modern_item(f"{ticker} news")], days=days)

    rows = yfnews.fetch_many(["NU", "BAD", "META"], days=7, fetcher=flaky)
    assert {r.ticker for r in rows} == {"NU", "META"}


def test_fetch_many_never_swallows_batch_schema_drift() -> None:
    from execution.fetch_yf_news import fetch_many

    def drifted(_ticker: str, *, days: int) -> Never:
        del days
        raise RowValidationDriftError("provider contract changed")

    with pytest.raises(RowValidationDriftError, match="contract changed"):
        fetch_many(["NU"], days=7, fetcher=drifted)


# ---------------------------------------------------------------------------
# The default flip
# ---------------------------------------------------------------------------


def test_websearch_is_no_longer_the_default_and_yf_news_is_wired() -> None:
    src = (PROJECT_ROOT / "execution" / "fetch_news.py").read_text(encoding="utf-8")
    # The paid LLM path is opt-in now.
    assert 'default="none"' in src
    assert '"none", "portfolio", "all"' in src
    # ...and the free feed actually runs in the additive fan-out.
    assert "_safe_yf_news" in src
    assert "import execution.fetch_yf_news as yfnews" in src


def test_scope_none_gates_every_ticker_out() -> None:
    """An EMPTY frozenset is the 'gate everyone out' signal; None means 'no
    gate'. Confusing the two would silently restore full-book LLM spend."""
    src = (PROJECT_ROOT / "execution" / "fetch_news.py").read_text(encoding="utf-8")
    assert 'websearch_scope == "none"' in src
    assert "eligible = frozenset()" in src
