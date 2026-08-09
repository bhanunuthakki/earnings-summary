"""Tests for the primary FMP stock-news feed (``execution/fetch_fmp_news.py``).

Covers (plan §6.5), all hermetic (shared FMP client mocked — no live FMP):

  * FmpStockNewsRecord validates a real sample; a drifted shape (``link`` not
    ``url`` / missing ``publishedDate``) raises -> the fetcher dumps + halts.
  * fetch -> map -> persist round-trip lands correctly-mapped rows tagged
    ``source_feed='fmp_stock_news'``.
  * ET -> UTC normalization: winter EST 08:30 -> 13:30Z, summer EDT 08:30 ->
    12:30Z (the DST boundary).
  * The raw (status, body) is carried back for the dispatcher's ``_fmp_refused``:
    a 401 + ``{"Error Message": ...}`` body AND the HTTP-200-with-a-dict-body
    refusal gotcha; a genuine 200 + ``[]`` is no-news, not refusal.
  * A record whose date can't be parsed is dropped (never timestamp-fabricated).

All `fetch_news_for_ticker` cases go through the public entry point rather than
poking the internal mapper, so they exercise the same path the dispatcher uses.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import cast

import pytest
from alembic.config import Config
from pydantic import ValidationError

import execution.fetch_fmp_news as fmpnews
from alembic import command
from execution.fetch_fmp_news import fetch_news_for_ticker, to_utc
from models.fmp_payloads import FmpStockNewsRecord
from net.client import HttpCallError, HttpErrorKind, HttpJsonResponse, JsonValue

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# One article in FMP's stable stock-news shape. publishedDate is US/Eastern.
_SAMPLE: dict[str, str] = {
    "symbol": "AAPL",
    "publishedDate": "2026-01-15 08:30:00",  # winter -> EST (UTC-5)
    "title": "Apple unveils new product line",
    "url": "https://news.example/aapl-1",
    "text": "A major product announcement reshaping the segment.",
    "site": "Reuters",
    "publisher": "Reuters",
    "image": "https://img.example/1.png",
}


def _patch_get(monkeypatch: pytest.MonkeyPatch, status: int, payload: object) -> None:
    json_payload = cast(JsonValue, payload)

    def _fake_get(*_a: object, **_k: object) -> HttpJsonResponse:
        if status >= 400:
            kind = (
                HttpErrorKind.AUTH
                if status in {401, 403}
                else HttpErrorKind.PLAN
                if status == 402
                else HttpErrorKind.RATE_LIMIT
                if status == 429
                else HttpErrorKind.TRANSIENT
            )
            raise HttpCallError(
                kind=kind,
                message=f"HTTP {status}",
                retryable=status == 429 or status >= 500,
                status_code=status,
                payload=json_payload,
            )
        return HttpJsonResponse(status_code=status, payload=json_payload)

    monkeypatch.setattr(fmpnews.FMP_CLIENT, "get_json", _fake_get)


# ---------------------------------------------------------------------------
# ET -> UTC
# ---------------------------------------------------------------------------


def test_to_utc_winter_est() -> None:
    # EST = UTC-5: 08:30 ET -> 13:30 UTC
    assert to_utc("2026-01-15 08:30:00") == "2026-01-15 13:30:00"


def test_to_utc_summer_edt() -> None:
    # EDT = UTC-4: 08:30 ET -> 12:30 UTC (DST boundary)
    assert to_utc("2026-07-15 08:30:00") == "2026-07-15 12:30:00"


def test_to_utc_rejects_non_canonical() -> None:
    with pytest.raises(ValueError, match=r"time data|unconverted"):
        to_utc("2026-01-15T08:30:00")


# ---------------------------------------------------------------------------
# FmpStockNewsRecord — validation / drift
# ---------------------------------------------------------------------------


def test_record_validates_real_sample() -> None:
    rec = FmpStockNewsRecord.model_validate(_SAMPLE)
    assert rec.symbol == "AAPL"
    assert rec.title == _SAMPLE["title"]
    assert rec.url == _SAMPLE["url"]


def test_record_drift_missing_published_date_raises() -> None:
    bad = {k: v for k, v in _SAMPLE.items() if k != "publishedDate"}
    with pytest.raises(ValidationError):
        FmpStockNewsRecord.model_validate(bad)


def test_record_drift_link_instead_of_url_raises() -> None:
    bad: dict[str, str] = {k: v for k, v in _SAMPLE.items() if k != "url"}
    bad["link"] = "https://news.example/aapl-1"  # renamed field -> url now missing
    with pytest.raises(ValidationError):
        FmpStockNewsRecord.model_validate(bad)


# ---------------------------------------------------------------------------
# fetch_news_for_ticker — fetch/validate/map + (status, body) for the dispatcher
# ---------------------------------------------------------------------------


def test_fetch_ok_maps_and_converts(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_get(monkeypatch, 200, [_SAMPLE])
    res = fetch_news_for_ticker("AAPL", api_key="key")
    assert res.status == 200
    assert res.error is None
    assert len(res.rows) == 1
    row = res.rows[0]
    assert row.ticker == "AAPL"
    assert row.headline == _SAMPLE["title"]
    assert row.url == _SAMPLE["url"]
    assert row.snippet == _SAMPLE["text"]
    assert row.source == _SAMPLE["site"]
    assert row.source_feed == "fmp_stock_news"
    assert row.published_at == "2026-01-15 13:30:00"  # ET -> UTC


def test_fetch_empty_array_is_no_news(monkeypatch: pytest.MonkeyPatch) -> None:
    # 200 + [] is a genuine no-news signal, NOT a refusal — no rows, no error.
    _patch_get(monkeypatch, 200, [])
    res = fetch_news_for_ticker("AAPL", api_key="key")
    assert res.status == 200
    assert res.rows == []
    assert res.error is None


def test_fetch_200_non_list_body_carried_for_dispatcher(monkeypatch: pytest.MonkeyPatch) -> None:
    # The HTTP-200-with-a-dict-body refusal gotcha: status is 200 but the body is
    # not an array. The fetch returns rows=[] + the raw body so the dispatcher's
    # _fmp_refused (PR5) can treat the non-list body as a refusal and fall back.
    err_body = {"Error Message": "Limit Reach. Upgrade your plan."}
    _patch_get(monkeypatch, 200, err_body)
    res = fetch_news_for_ticker("AAPL", api_key="key")
    assert res.status == 200
    assert res.body == err_body
    assert res.rows == []
    assert res.error is None  # not drift, not auth — the dispatcher decides


def test_fetch_drops_unparseable_date(monkeypatch: pytest.MonkeyPatch) -> None:
    bad_date = dict(_SAMPLE)
    bad_date["publishedDate"] = "not-a-date"
    _patch_get(monkeypatch, 200, [_SAMPLE, bad_date])
    res = fetch_news_for_ticker("AAPL", api_key="key")
    # The good record maps; the un-timestampable one is dropped, never fabricated.
    assert res.error is None
    assert len(res.rows) == 1
    assert res.rows[0].url == _SAMPLE["url"]


def test_fetch_auth_refusal_carries_status_and_body(monkeypatch: pytest.MonkeyPatch) -> None:
    err_body = {"Error Message": "Invalid API KEY"}
    _patch_get(monkeypatch, 401, err_body)
    res = fetch_news_for_ticker("AAPL", api_key="")
    assert res.status == 401
    assert res.body == err_body  # the dispatcher's _fmp_refused reads this
    assert res.rows == []
    assert res.error is not None


def test_fetch_rate_limited_exhausts_then_returns(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_get(monkeypatch, 429, {"Error Message": "Limit Reach"})
    res = fetch_news_for_ticker("AAPL", api_key="key")
    assert res.status == 429
    assert res.rows == []
    assert res.error is not None


def test_fetch_schema_drift_dumps_and_halts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(fmpnews, "_VALIDATION_DUMP_DIR", tmp_path / "dumps")
    _patch_get(monkeypatch, 200, [{"symbol": "AAPL"}])  # missing required fields
    res = fetch_news_for_ticker("AAPL", api_key="key")
    assert res.rows == []
    assert res.error is not None
    assert "schema_drift" in res.error
    dumps = list((tmp_path / "dumps").glob("AAPL_stock_news_*.json"))
    assert len(dumps) == 1


# ---------------------------------------------------------------------------
# run() — full standalone fetch -> persist round-trip against the real schema
# ---------------------------------------------------------------------------


def _build_config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def news_db(tmp_path: Path) -> Path:
    db = tmp_path / "fmp_news.db"
    cfg = _build_config(db)
    command.stamp(cfg, "0064_queued_actions")
    command.upgrade(cfg, "0065_news")
    return db


def test_run_persists_mapped_rows(news_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fmpnews, "FMP_API_KEY", "fake-key")
    _patch_get(monkeypatch, 200, [_SAMPLE])

    rc = fmpnews.run(["AAPL"], db_path=str(news_db), days=2, limit=50)
    assert rc == 0

    conn = sqlite3.connect(str(news_db))
    try:
        row = conn.execute(
            "SELECT ticker, headline, url, published_at, source, source_feed FROM news"
        ).fetchone()
    finally:
        conn.close()
    assert row == (
        "AAPL",
        _SAMPLE["title"],
        _SAMPLE["url"],
        "2026-01-15 13:30:00",
        _SAMPLE["site"],
        "fmp_stock_news",
    )


def test_run_without_api_key_returns_error(news_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fmpnews, "FMP_API_KEY", "")
    rc = fmpnews.run(["AAPL"], db_path=str(news_db), days=2, limit=50)
    assert rc == 1
