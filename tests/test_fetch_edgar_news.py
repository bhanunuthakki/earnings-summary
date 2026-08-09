"""Tests for the additive EDGAR filings feed (``execution/fetch_edgar_news.py``).

Hermetic — no SEC traffic. Covers:

  * map_filings_to_news_rows — the pure submissions-JSON -> NewsRow mapping:
    form filtering (8-K / 13D / 13G in BOTH the legacy 'SC' and post-Dec-2024
    'SCHEDULE' spellings; Form 4 / 10-Q noise ignored), item-code headlines,
    the filing-date window, acceptanceDateTime -> canonical UTC (with the
    filingDate-midnight fallback), archive URL construction, source_feed tags.
  * resolve_cik — registry lookup from the on-disk cache (dot->dash share-class
    normalization), no network when the cache is fresh.
  * fetch_edgar_news_for_ticker — end-to-end against pre-seeded cache files
    with the network seam (_get_json) rigged to fail loudly if touched; cold
    cache + dead network degrades to [].
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

import execution.fetch_edgar_news as edgarnews
from news.store import (
    SOURCE_FEED_EDGAR_8K,
    SOURCE_FEED_EDGAR_13D,
    SOURCE_FEED_EDGAR_13G,
    NewsRow,
)
from pipeline.row_validation import RowValidationDriftError

CIK = "0001326801"


def _payload(
    forms: list[str],
    *,
    items: list[str] | None = None,
    filing_dates: list[str] | None = None,
    acceptance: list[str] | None = None,
    primary_docs: list[str] | None = None,
    name: str = "Meta Platforms, Inc.",
) -> dict[str, object]:
    n = len(forms)
    return {
        "cik": "1326801",
        "name": name,
        "filings": {
            "recent": {
                "form": forms,
                "accessionNumber": [f"0001-26-{i:06d}" for i in range(n)],
                "filingDate": filing_dates or ["2026-06-10"] * n,
                "acceptanceDateTime": acceptance or ["2026-06-10T20:16:13.000Z"] * n,
                "items": items or [""] * n,
                "primaryDocument": primary_docs or ["doc.htm"] * n,
            }
        },
    }


def _map(payload: dict[str, object], since: str = "2026-06-08") -> list[NewsRow]:
    return edgarnews.map_filings_to_news_rows(payload, ticker="META", cik=CIK, since_date=since)


# ---------------------------------------------------------------------------
# Mapping: forms, headlines, item codes
# ---------------------------------------------------------------------------


def test_maps_8k_with_item_codes_to_edgar_8k_row() -> None:
    rows = edgarnews.map_filings_to_news_rows(
        _payload(["8-K"], items=["2.01,9.01"]), ticker="META", cik=CIK, since_date="2026-06-08"
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.source_feed == SOURCE_FEED_EDGAR_8K
    assert row.ticker == "META"
    assert (
        row.headline
        == "8-K 2.01, 9.01: completed acquisition or disposition — Meta Platforms, Inc."
    )
    assert row.published_at == "2026-06-10 20:16:13"
    assert row.url == "https://www.sec.gov/Archives/edgar/data/1326801/000126000000/doc.htm"
    assert row.source == "SEC EDGAR"
    assert row.snippet is not None and "accession 0001-26-000000" in row.snippet


def test_parallel_array_drift_halts_above_drop_threshold() -> None:
    payload = _payload(["8-K"] * 20)
    filings = cast("dict[str, object]", payload["filings"])
    recent = cast("dict[str, object]", filings["recent"])
    accessions = cast("list[str]", recent["accessionNumber"])
    recent["accessionNumber"] = accessions[:17]

    with pytest.raises(RowValidationDriftError, match="3/20"):
        _map(payload)


def test_8k_headline_names_first_non_boilerplate_item() -> None:
    """9.01 (exhibits) never names the filing — the material item does, even
    when 9.01 is listed first by the filer."""
    (row,) = _map(_payload(["8-K"], items=["9.01, 5.02"]))
    assert row.headline.startswith("8-K 9.01, 5.02: executive or director change")


def test_8k_with_only_boilerplate_or_unknown_items_degrades_gracefully() -> None:
    only_901, unknown = _map(_payload(["8-K", "8-K"], items=["9.01", "6.03"]))
    assert "financial statements and exhibits" in only_901.headline
    assert "other disclosure" in unknown.headline


def test_8k_without_items_still_maps() -> None:
    (row,) = _map(_payload(["8-K"], items=[""]))
    assert row.headline == "8-K: filing — Meta Platforms, Inc."


@pytest.mark.parametrize(
    ("form", "feed", "fragment"),
    [
        ("SC 13D", SOURCE_FEED_EDGAR_13D, "SC 13D: activist stake (>5%) disclosed"),
        ("SCHEDULE 13D/A", SOURCE_FEED_EDGAR_13D, "SC 13D/A: activist stake (>5%) amended"),
        ("SC 13G", SOURCE_FEED_EDGAR_13G, "SC 13G: passive stake (>5%) disclosed"),
        ("SCHEDULE 13G/A", SOURCE_FEED_EDGAR_13G, "SC 13G/A: passive stake (>5%) amended"),
    ],
)
def test_maps_ownership_forms_both_spellings(form: str, feed: str, fragment: str) -> None:
    """Legacy 'SC 13D/G' and post-Dec-2024 'SCHEDULE 13D/G' both map, with
    /A amendments phrased as amendments."""
    (row,) = _map(_payload([form]))
    assert row.source_feed == feed
    assert row.headline == f"{fragment} — Meta Platforms, Inc."


def test_ignores_non_target_forms() -> None:
    assert _map(_payload(["4", "144", "10-Q", "DEF 14A", "S-8"])) == []


def test_filing_date_window_is_inclusive_lexical() -> None:
    rows = _map(
        _payload(["8-K", "8-K"], filing_dates=["2026-06-08", "2026-06-07"]),
        since="2026-06-08",
    )
    assert len(rows) == 1  # 06-07 is out of window; 06-08 (== since) stays


# ---------------------------------------------------------------------------
# Timestamps + URLs
# ---------------------------------------------------------------------------


def test_acceptance_datetime_without_millis_parses() -> None:
    (row,) = _map(_payload(["8-K"], acceptance=["2026-06-10T20:16:13Z"]))
    assert row.published_at == "2026-06-10 20:16:13"


def test_missing_acceptance_falls_back_to_filing_date_midnight() -> None:
    """The date is real; midnight is the conservative floor — never fabricated
    intra-day recency."""
    (row,) = _map(_payload(["8-K"], acceptance=[""], filing_dates=["2026-06-09"]))
    assert row.published_at == "2026-06-09 00:00:00"


def test_missing_primary_document_links_filing_folder() -> None:
    (row,) = _map(_payload(["8-K"], primary_docs=[""]))
    assert row.url == "https://www.sec.gov/Archives/edgar/data/1326801/000126000000/"


def test_malformed_payload_degrades_to_empty() -> None:
    assert edgarnews.map_filings_to_news_rows(None, ticker="META", cik=CIK, since_date="x") == []
    assert edgarnews.map_filings_to_news_rows({}, ticker="META", cik=CIK, since_date="x") == []
    assert (
        edgarnews.map_filings_to_news_rows(
            {"filings": {"recent": "bogus"}}, ticker="META", cik=CIK, since_date="x"
        )
        == []
    )


# ---------------------------------------------------------------------------
# CIK resolution + cached end-to-end fetch (no network)
# ---------------------------------------------------------------------------

_REGISTRY = {
    "0": {"cik_str": 1326801, "ticker": "META", "title": "Meta Platforms, Inc."},
    "1": {"cik_str": 1067983, "ticker": "BRK-B", "title": "Berkshire Hathaway Inc"},
}


def _seed_cache(cache_dir: Path, payload: dict[str, object]) -> None:
    (cache_dir / "submissions").mkdir(parents=True, exist_ok=True)
    (cache_dir / "company_tickers.json").write_text(json.dumps(_REGISTRY), encoding="utf-8")
    (cache_dir / "submissions" / f"CIK{CIK}.json").write_text(json.dumps(payload), encoding="utf-8")


def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(url: str, *, user_agent: str) -> object:
        raise AssertionError(f"network touched: {url}")

    monkeypatch.setattr(edgarnews, "_get_json", _boom)


def test_resolve_cik_from_fresh_cache_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_cache(tmp_path, _payload(["8-K"]))
    _no_network(monkeypatch)
    assert edgarnews.resolve_cik("META", cache_dir=tmp_path) == CIK
    # Share-class tickers: registry uses dashes, callers may use dots.
    assert edgarnews.resolve_cik("BRK.B", cache_dir=tmp_path) == "0001067983"
    assert edgarnews.resolve_cik("ZZZQ", cache_dir=tmp_path) is None


def test_fetch_for_ticker_serves_from_fresh_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_cache(tmp_path, _payload(["8-K", "SCHEDULE 13G"], items=["2.02,9.01", ""]))
    _no_network(monkeypatch)
    rows = edgarnews.fetch_edgar_news_for_ticker("META", days=3650, cache_dir=tmp_path)
    assert [r.source_feed for r in rows] == [SOURCE_FEED_EDGAR_8K, SOURCE_FEED_EDGAR_13G]


def test_fetch_for_ticker_cold_cache_dead_network_degrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Additive feed discipline: unreachable SEC + nothing cached -> [] (and
    the dispatcher's primary feeds proceed untouched)."""
    import requests

    def _down(url: str, *, user_agent: str) -> object:
        raise requests.ConnectionError("sec.gov unreachable")

    monkeypatch.setattr(edgarnews, "_get_json", _down)
    assert edgarnews.fetch_edgar_news_for_ticker("META", cache_dir=tmp_path) == []


def test_stale_cache_refetches_and_rewrites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_cache(tmp_path, _payload(["8-K"]))
    fresh_payload = _payload(["SC 13D"])
    calls: list[str] = []

    def _fake(url: str, *, user_agent: str) -> object:
        calls.append(url)
        assert "@" in user_agent  # SEC fair access: a contact must be present
        return _REGISTRY if "company_tickers" in url else fresh_payload

    monkeypatch.setattr(edgarnews, "_get_json", _fake)
    sub_cache = tmp_path / "submissions" / f"CIK{CIK}.json"
    rows = edgarnews.fetch_edgar_news_for_ticker(
        "META", days=3650, cache_dir=tmp_path, force_refresh=True
    )
    assert [r.source_feed for r in rows] == [SOURCE_FEED_EDGAR_13D]
    assert len(calls) == 2  # registry + submissions, both bypassing the TTL
    assert json.loads(sub_cache.read_text(encoding="utf-8"))["filings"]["recent"]["form"] == [
        "SC 13D"
    ]
