"""ETF onboarding-lite safety rails (directives/etf_data.md).

- allocation.price_history.load_daily_closes: yfinance proxy-store fallback
  (FMP chart wins when both exist; proxy carries ETFs the plan doesn't).
- dcf.universe.dcf_universe: evaluation ETFs are excluded from the DCF
  universe; a pre-0044 substrate without instrument_type keeps old behavior.
- onboard_ticker rails: _saydo_should_run never fires for an ETF (even
  forced); company-fit refresh excludes the retired ETF tail.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import cast

import pytest

from allocation.price_history import load_daily_closes
from dcf.universe import dcf_universe
from execution import onboard_ticker as onboard_ticker_mod

_saydo_should_run = cast(Callable[..., bool], getattr(onboard_ticker_mod, "_saydo_should_run"))

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
    _write_proxy(tmp_path, "AVDV", [("2026-07-08", 102.5), ("2026-07-09", 103.1)])
    closes = load_daily_closes("AVDV", tmp_path)
    assert closes == [(date(2026, 7, 8), 102.5), (date(2026, 7, 9), 103.1)]


def test_load_daily_closes_proxy_fallback_without_fmp_dir(tmp_path: Path) -> None:
    # No data/historical/fmp at all — the pre-existing early return must
    # still consult the proxy store.
    _write_proxy(tmp_path, "VWO", [("2026-07-09", 44.2)])
    assert load_daily_closes("vwo", tmp_path) == [(date(2026, 7, 9), 44.2)]


def test_load_daily_closes_fmp_wins_over_proxy(tmp_path: Path) -> None:
    _write_proxy(tmp_path, "AVUV", [("2026-07-09", 1.0)])
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


def test_refresh_candidate_fit_rejects_retired_synthesis_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from execution import refresh_candidate_fit as refresh_mod

    db_path = tmp_path / "portfolio.db"
    db_path.write_bytes(b"")

    def fail_connect(*args: object, **kwargs: object) -> object:
        raise AssertionError("retired flag must be rejected before opening the DB")

    monkeypatch.setattr(refresh_mod, "connect_sqlite", fail_connect)
    monkeypatch.setattr(
        sys,
        "argv",
        ["refresh_candidate_fit.py", "--refresh-synthesis", "--db-path", str(db_path)],
    )

    with pytest.raises(SystemExit) as exc:
        refresh_mod.main()
    assert exc.value.code == 2


def test_refresh_candidate_fit_ordinary_path_keeps_company_materializer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from execution import refresh_candidate_fit as refresh_mod

    db_path = tmp_path / "portfolio.db"
    db_path.write_bytes(b"")
    calls: list[tuple[object, ...]] = []

    class _Conn:
        def close(self) -> None:
            calls.append(("close",))

    conn = _Conn()

    def fake_connect(path: str, role: object) -> _Conn:
        calls.append(("connect", path, role))
        return conn

    def fake_candidate(c: object, repo_root: Path, *, db_path: Path, api_url: str | None) -> int:
        calls.append(("candidate", c, repo_root, db_path, api_url))
        return 7

    monkeypatch.setattr(refresh_mod, "connect_sqlite", fake_connect)
    monkeypatch.setattr(refresh_mod, "materialize_candidate_fit", fake_candidate)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "refresh_candidate_fit.py",
            "--repo-root",
            str(tmp_path),
            "--db-path",
            str(db_path),
            "--api-url",
            "http://stub.invalid",
        ],
    )

    assert refresh_mod.main() == 0
    assert [call[0] for call in calls] == ["connect", "candidate", "close"]
    assert calls[1] == ("candidate", conn, tmp_path, db_path, "http://stub.invalid")
    out = capsys.readouterr().out
    assert "names=7" in out and "etf_scores" not in out
