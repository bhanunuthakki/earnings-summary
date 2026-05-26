"""Tests for execution/schedule_pre_earnings_refresh.py.

Focus is the yfinance/per-ticker fallback wired into `schedule()`. The FMP
universe call, watched-ticker DB read, and hints file are all monkeypatched
so the tests stay hermetic.
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from sources.earnings_calendar import NextEarnings

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "execution"))

import schedule_pre_earnings_refresh as sper


def _patch_common(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, watched: set[str]) -> Path:
    """Wire schedule() to a tmp hints file + fixed watched set."""
    hints_path = tmp_path / "forced_stale.json"
    monkeypatch.setattr(sper, "HINTS_PATH", hints_path)
    monkeypatch.setattr(sper, "_watched_tickers", lambda: watched)
    return hints_path


def test_fmp_match_skips_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When FMP returns a match for a watched ticker, next_earnings_date is not called."""
    hints_path = _patch_common(monkeypatch, tmp_path, {"NU"})
    earnings_date = (date.today() + timedelta(days=3)).isoformat()
    monkeypatch.setattr(
        sper, "_fetch_earnings_calendar",
        lambda start, end: [{"symbol": "NU", "date": earnings_date}],
    )

    calls: list[str] = []

    def _spy(_root: Path, ticker: str) -> NextEarnings | None:
        calls.append(ticker)
        return None

    monkeypatch.setattr(sper, "next_earnings_date", _spy)

    summary = sper.schedule()

    assert calls == []  # fallback not invoked
    assert summary["fallback_matches"] == []
    assert hints_path.exists()
    hints = json.loads(hints_path.read_text(encoding="utf-8"))
    assert "NU" in hints


def test_fmp_empty_yfinance_returns_event(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """FMP returns nothing for the watched ticker; yfinance fallback fires."""
    hints_path = _patch_common(monkeypatch, tmp_path, {"NU"})
    monkeypatch.setattr(sper, "_fetch_earnings_calendar", lambda start, end: [])

    yf_date = date.today() + timedelta(days=5)
    monkeypatch.setattr(
        sper, "next_earnings_date",
        lambda _root, ticker: NextEarnings(expected_date=yf_date, source_name="yfinance", confirmed=False)
        if ticker == "NU" else None,
    )

    summary = sper.schedule()

    assert summary["fallback_matches"] == [
        {"symbol": "NU", "date": yf_date.isoformat(), "source": "yfinance"}
    ]
    hints = json.loads(hints_path.read_text(encoding="utf-8"))
    assert "NU" in hints
    new_hints = summary["new_or_updated_hints"]
    assert isinstance(new_hints, list)
    assert new_hints[0]["source"] == "yfinance"


def test_both_empty_no_hint_no_exception(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """FMP empty AND yfinance returns None — no hint written, no exception."""
    hints_path = _patch_common(monkeypatch, tmp_path, {"NU"})
    monkeypatch.setattr(sper, "_fetch_earnings_calendar", lambda start, end: [])
    monkeypatch.setattr(sper, "next_earnings_date", lambda _root, _t: None)

    summary = sper.schedule()

    assert summary["fallback_matches"] == []
    assert summary["new_or_updated_hints"] == []
    # Hint file gets written even if empty (existing behaviour for prune pass)
    assert hints_path.exists()
    assert json.loads(hints_path.read_text(encoding="utf-8")) == {}


def test_fmp_rate_limit_falls_through_to_yfinance(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When the FMP universe call raises (rate limit / network), every watched
    ticker funnels into the per-ticker fallback path instead of crashing."""
    hints_path = _patch_common(monkeypatch, tmp_path, {"NU"})

    def _boom(start: date, end: date) -> list[dict[str, str]]:
        raise RuntimeError("rate limited")

    monkeypatch.setattr(sper, "_fetch_earnings_calendar", _boom)

    yf_date = date.today() + timedelta(days=2)
    monkeypatch.setattr(
        sper, "next_earnings_date",
        lambda _root, _t: NextEarnings(expected_date=yf_date, source_name="yfinance", confirmed=False),
    )

    summary = sper.schedule()

    assert summary["calendar_entries"] == 0
    assert len(summary["fallback_matches"]) == 1
    assert summary["fallback_matches"][0]["symbol"] == "NU"
    assert "NU" in json.loads(hints_path.read_text(encoding="utf-8"))


def test_fallback_filters_outside_window(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """yfinance can return dates further out than the lookahead window;
    those must be dropped so we don't write hints the FMP scan wouldn't have."""
    _patch_common(monkeypatch, tmp_path, {"NU"})
    monkeypatch.setattr(sper, "_fetch_earnings_calendar", lambda start, end: [])

    # 30 days out — well beyond LOOKAHEAD_DAYS (7).
    far_date = date.today() + timedelta(days=30)
    monkeypatch.setattr(
        sper, "next_earnings_date",
        lambda _root, _t: NextEarnings(expected_date=far_date, source_name="yfinance", confirmed=False),
    )

    summary = sper.schedule()
    assert summary["fallback_matches"] == []
    assert summary["new_or_updated_hints"] == []


def test_fallback_isolates_per_ticker_exception(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """If next_earnings_date raises for one ticker, the rest still run."""
    _patch_common(monkeypatch, tmp_path, {"NU", "MELI"})
    monkeypatch.setattr(sper, "_fetch_earnings_calendar", lambda start, end: [])

    good_date = date.today() + timedelta(days=4)

    def _half_broken(_root: Path, ticker: str) -> NextEarnings | None:
        if ticker == "NU":
            raise RuntimeError("yahoo schema flip")
        return NextEarnings(expected_date=good_date, source_name="yfinance", confirmed=False)

    monkeypatch.setattr(sper, "next_earnings_date", _half_broken)

    summary = sper.schedule()
    fallback = summary["fallback_matches"]
    assert isinstance(fallback, list)
    assert {m["symbol"] for m in fallback} == {"MELI"}
