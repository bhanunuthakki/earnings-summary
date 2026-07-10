"""ETF onboarding-lite safety rails (directives/etf_data.md).

- allocation.price_history.load_daily_closes: yfinance proxy-store fallback
  (FMP chart wins when both exist; proxy carries ETFs the plan doesn't).
- dcf.universe.dcf_universe: evaluation ETFs are excluded from the DCF
  universe; a pre-0044 substrate without instrument_type keeps old behavior.
- onboard_ticker rails: _saydo_should_run never fires for an ETF (even
  forced); run_etf_onboarding maps ingest outcomes to exit codes.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from allocation.price_history import load_daily_closes  # noqa: E402
from dcf.universe import dcf_universe  # noqa: E402
from execution.onboard_ticker import _saydo_should_run, run_etf_onboarding  # noqa: E402

# ---------------------------------------------------------------------------
# price_history proxy-store fallback
# ---------------------------------------------------------------------------


def _write_proxy(repo_root: Path, ticker: str, rows: list[tuple[str, float]]) -> None:
    d = repo_root / "data" / "factor_proxies"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{ticker}.json").write_text(
        json.dumps({"ticker": ticker, "fetched_at": "2026-07-10T00:00:00", "rows": rows}),
        encoding="utf-8",
    )


def _write_fmp_chart(repo_root: Path, ticker: str, rows: list[dict[str, object]]) -> None:
    d = repo_root / "data" / "historical" / "fmp"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{ticker}_price_chart_10y_div_adj.json").write_text(json.dumps(rows), encoding="utf-8")


def test_load_daily_closes_falls_back_to_proxy_store(tmp_path: Path) -> None:
    _write_proxy(tmp_path, "AVDV", [["2026-07-08", 102.5], ["2026-07-09", 103.1]])
    closes = load_daily_closes("AVDV", tmp_path)
    assert closes == [(date(2026, 7, 8), 102.5), (date(2026, 7, 9), 103.1)]


def test_load_daily_closes_proxy_fallback_without_fmp_dir(tmp_path: Path) -> None:
    # No data/historical/fmp at all — the pre-existing early return must
    # still consult the proxy store.
    _write_proxy(tmp_path, "VWO", [["2026-07-09", 44.2]])
    assert load_daily_closes("vwo", tmp_path) == [(date(2026, 7, 9), 44.2)]


def test_load_daily_closes_fmp_wins_over_proxy(tmp_path: Path) -> None:
    _write_proxy(tmp_path, "AVUV", [["2026-07-09", 1.0]])
    _write_fmp_chart(tmp_path, "AVUV", [{"date": "2026-07-09", "adjClose": 95.5}])
    assert load_daily_closes("AVUV", tmp_path) == [(date(2026, 7, 9), 95.5)]


def test_load_daily_closes_malformed_proxy_is_empty(tmp_path: Path) -> None:
    d = tmp_path / "data" / "factor_proxies"
    d.mkdir(parents=True)
    (d / "BAD.json").write_text("{not json", encoding="utf-8")
    assert load_daily_closes("BAD", tmp_path) == []


# ---------------------------------------------------------------------------
# DCF universe instrument gate
# ---------------------------------------------------------------------------


def _seed_universe_db(repo_root: Path, *, with_instrument_column: bool) -> None:
    d = repo_root / "data"
    d.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(d / "portfolio.db"))
    instrument_col = ", instrument_type VARCHAR" if with_instrument_column else ""
    conn.execute(f"CREATE TABLE tracked_companies (ticker TEXT, list_type TEXT{instrument_col})")
    if with_instrument_column:
        rows = [
            ("NU", "portfolio", "equity"),
            ("VEEV", "evaluation", None),
            ("AVDV", "evaluation", "etf"),
            ("SOXX", "watchlist", "etf"),
        ]
        conn.executemany("INSERT INTO tracked_companies VALUES (?, ?, ?)", rows)
    else:
        conn.executemany(
            "INSERT INTO tracked_companies VALUES (?, ?)",
            [("NU", "portfolio"), ("AVDV", "evaluation")],
        )
    conn.commit()
    conn.close()


def test_dcf_universe_excludes_evaluation_etfs(tmp_path: Path) -> None:
    _seed_universe_db(tmp_path, with_instrument_column=True)
    assert dcf_universe(tmp_path) == ["NU", "VEEV"]  # AVDV (etf) out, NULL-typed VEEV in


def test_dcf_universe_tolerates_missing_instrument_column(tmp_path: Path) -> None:
    _seed_universe_db(tmp_path, with_instrument_column=False)
    assert dcf_universe(tmp_path) == ["AVDV", "NU"]  # old behavior preserved


# ---------------------------------------------------------------------------
# Onboarding rails
# ---------------------------------------------------------------------------


def test_saydo_never_runs_for_etf() -> None:
    assert _saydo_should_run("evaluation", force=False, instrument="etf") is False
    assert _saydo_should_run("evaluation", force=True, instrument="etf") is False
    assert _saydo_should_run("evaluation", force=False, instrument="equity") is True
    assert _saydo_should_run("watchlist", force=False, instrument=None) is False
    assert _saydo_should_run("watchlist", force=True, instrument=None) is True


class _FakeResult:
    def __init__(self, nport_status: str, issuer_status: str) -> None:
        self.nport_status = nport_status
        self.issuer_status = issuer_status
        self.nport_as_of = date(2026, 5, 31)
        self.nport_rows = 3
        self.issuer_rows = 0
        self.issuer_as_of = None
        self.characteristics_applied = False
        self.price_status = "fetched"
        self.price_rows = 1250


def test_run_etf_onboarding_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import etf_sources.ingest as ingest_mod
    from etf_sources.nport import NportParseError

    conn = sqlite3.connect(":memory:")

    monkeypatch.setattr(
        ingest_mod,
        "refresh_published_data",
        lambda c, t, root, **kw: _FakeResult("ingested", "unavailable"),
    )
    assert run_etf_onboarding(conn, "AVDV", tmp_path) == 0

    monkeypatch.setattr(
        ingest_mod,
        "refresh_published_data",
        lambda c, t, root, **kw: _FakeResult("unavailable", "unavailable"),
    )
    assert run_etf_onboarding(conn, "AVDV", tmp_path) == 1

    def halt(c: object, t: str, root: Path, **kw: object) -> object:
        raise NportParseError("drifted")

    monkeypatch.setattr(ingest_mod, "refresh_published_data", halt)
    assert run_etf_onboarding(conn, "AVDV", tmp_path) == 2
    conn.close()
