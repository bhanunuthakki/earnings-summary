"""Legacy corporate FMP fetches cannot bypass stored scope at their network boundary."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from execution import fetch_fmp_10q_json as reports
from execution import fetch_fmp_historical_data as historical
from net.client import HttpJsonResponse


@pytest.mark.parametrize("entrypoint", ["reports", "historical"])
@pytest.mark.parametrize("owner_requested", [False, True])
@pytest.mark.parametrize(
    ("role", "instrument", "archived", "allowed"),
    [
        ("portfolio", "equity", None, True),
        ("evaluation", "equity", None, True),
        ("watchlist", "adr", None, True),
        ("evaluation", "etf", None, False),
        ("evaluation", None, None, False),
        ("index_member", "equity", None, False),
        ("none", "equity", None, False),
        ("portfolio", "equity", "2026-01-01", False),
    ],
)
def test_corporate_fmp_network_boundary_requires_active_full_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
    owner_requested: bool,
    role: str,
    instrument: str | None,
    archived: str | None,
    allowed: bool,
) -> None:
    database = tmp_path / "scope.db"
    with sqlite3.connect(database) as conn:
        conn.execute(
            "CREATE TABLE tracked_companies (ticker TEXT,list_type TEXT,archived_at TEXT,instrument_type TEXT)"
        )
        conn.execute(
            "INSERT INTO tracked_companies VALUES ('TEST',?,?,?)", (role, archived, instrument)
        )
    monkeypatch.setattr(reports, "DB_PATH", database)
    monkeypatch.setattr(historical.db, "DB_PATH", str(database))
    calls: list[str] = []

    def get(path: str, **_kwargs: object) -> HttpJsonResponse:
        calls.append(path)
        return HttpJsonResponse(status_code=200, payload={"revenue": 1})

    monkeypatch.setattr(reports.FMP_CLIENT, "get_json", get)
    if entrypoint == "reports":
        # Force a candidate past roster selection to prove the network boundary
        # independently enforces identity, role, and instrument applicability.
        def forced_candidate(_tickers: str | None) -> list[str]:
            return ["TEST"]

        monkeypatch.setattr(reports, "_resolve_tickers", forced_candidate)
        _configure_report_cli(monkeypatch, tmp_path, "TEST" if owner_requested else None)
        assert (reports.main() == 0) is allowed
    else:
        payload = historical.fetch_from_fmp(
            "income-statement", {"symbol": "TEST"}, owner_requested=owner_requested
        )
        assert (payload is not None) is allowed
    assert bool(calls) is allowed


def test_explicit_report_ticker_does_not_override_unknown_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "scope.db"
    with sqlite3.connect(database) as conn:
        conn.execute(
            "CREATE TABLE tracked_companies (ticker TEXT,list_type TEXT,archived_at TEXT,instrument_type TEXT)"
        )
    monkeypatch.setattr(reports, "DB_PATH", database)
    _configure_report_cli(monkeypatch, tmp_path, "UNTRACKED")
    calls: list[str] = []

    def get(path: str, **_kwargs: object) -> HttpJsonResponse:
        calls.append(path)
        return HttpJsonResponse(status_code=200, payload={})

    monkeypatch.setattr(reports.FMP_CLIENT, "get_json", get)
    assert reports.main() == 1
    assert calls == []


def _configure_report_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ticker: str | None
) -> None:
    monkeypatch.setenv("FMP_API_KEY", "synthetic-test-key")
    monkeypatch.setattr(reports, "FMP_DIR", tmp_path / "fmp")
    monkeypatch.setattr(reports, "QUARTERS", ("Q2",))
    monkeypatch.setattr(reports, "DELAY_S", 0)
    argv = ["fetch_fmp_10q_json.py", "--start-year", "2026", "--end-year", "2026", "--no-index"]
    if ticker is not None:
        argv.extend(["--tickers", ticker])
    monkeypatch.setattr(sys, "argv", argv)
