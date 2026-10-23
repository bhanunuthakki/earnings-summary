"""XBRL scope gaps must remain visible without invalid-row coercion."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast

import pytest

from execution import fetch_sec_xbrl
from identity import DEFAULT_USER_ID
from models.runs import StageStatus
from pipeline import queries, sec_xbrl
from pipeline.run_accounting import PipelineRunSuppressedError

resolve_tickers = cast(
    Callable[[argparse.Namespace, sqlite3.Connection], list[str]],
    getattr(fetch_sec_xbrl, "_resolve_tickers"),
)


@pytest.fixture
def roster() -> Iterator[sqlite3.Connection]:
    # This isolated read-contract fixture does not need application migrations.
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE tracked_companies (id INTEGER,user_id TEXT,ticker TEXT,name TEXT,"
        "list_type TEXT,added_at TEXT,sec_validated INTEGER,ir_url TEXT,instrument_type TEXT,"
        "filing_regime TEXT,fiscal_year_end TEXT,fmp_data_saved INTEGER,fmp_data_upto TEXT,"
        "archived_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO tracked_companies (id,user_id,ticker,name,list_type,instrument_type,"
        "filing_regime) VALUES (?,?,?,?,?,?,?)",
        [
            (1, DEFAULT_USER_ID, "BROKEN", "Synthetic", "portfolio", "invalid", "20-F"),
            (2, DEFAULT_USER_ID, "META", "Synthetic", "portfolio", "equity", "10-K"),
            (3, DEFAULT_USER_ID, "VEEV", "Synthetic", "watchlist", "equity", "10-K"),
            (4, DEFAULT_USER_ID, "IDX", "Synthetic", "index_member", "equity", "10-K"),
        ],
    )
    yield conn
    conn.close()


def test_strict_reader_still_rejects_invalid_identity(roster: sqlite3.Connection) -> None:
    with pytest.raises(ValueError):
        queries.tracked_companies_for_user(roster)


def test_valid_rows_continue_with_visible_gap_without_repair(
    roster: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    assert resolve_tickers(argparse.Namespace(ticker=None), roster) == ["META", "VEEV"]
    event = json.loads(capsys.readouterr().err)
    assert event["reason_code"] == "tracked_company_invalid"
    assert event["ticker"] == "BROKEN"
    assert event["fields"] == ["instrument_type"]
    assert (
        roster.execute("SELECT instrument_type FROM tracked_companies WHERE id=1").fetchone()[0]
        == "invalid"
    )


@pytest.mark.parametrize(
    "ticker,expected", [("META", ["META"]), ("BROKEN", []), ("IDX", []), ("UNKNOWN", [])]
)
def test_explicit_scope_never_broadens(
    roster: sqlite3.Connection, capsys: pytest.CaptureFixture[str], ticker: str, expected: list[str]
) -> None:
    assert resolve_tickers(argparse.Namespace(ticker=ticker), roster) == expected
    events = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert any(e.get("reason_code") == "tracked_company_invalid" for e in events) == (
        ticker == "BROKEN"
    )


def test_corrected_identity_recovers_on_next_read(
    roster: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    assert resolve_tickers(argparse.Namespace(ticker=None), roster) == ["META", "VEEV"]
    capsys.readouterr()
    roster.execute("UPDATE tracked_companies SET instrument_type='adr' WHERE id=1")
    assert resolve_tickers(argparse.Namespace(ticker=None), roster) == ["BROKEN", "META", "VEEV"]
    assert capsys.readouterr().err == ""


def test_missing_roster_is_explicit_and_has_no_targets(capsys: pytest.CaptureFixture[str]) -> None:
    with sqlite3.connect(":memory:") as conn:
        assert resolve_tickers(argparse.Namespace(ticker=None), conn) == []
    assert json.loads(capsys.readouterr().err)["reason_code"] == "tracked_roster_unavailable"


@pytest.mark.parametrize("mode", ["mixed", "missing", "suppressed", "empty"])
def test_cli_scope_gap_cannot_be_reported_as_success(
    roster: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    mode: str,
) -> None:
    if mode == "missing":
        roster.execute("DROP TABLE tracked_companies")
    elif mode == "empty":
        roster.execute("DELETE FROM tracked_companies")
    database = tmp_path / "fixture.db"
    database.touch()
    calls: list[str] = []
    statuses: list[StageStatus] = []

    def start(*_args: object, **_kwargs: object) -> str:
        if mode == "suppressed":
            raise PipelineRunSuppressedError("key", "attempt", StageStatus.OK)
        return "fixture-run"

    def finish(
        _conn: sqlite3.Connection, _run: str, status: StageStatus, **_kwargs: object
    ) -> None:
        statuses.append(status)

    def ingest(
        _conn: sqlite3.Connection, *, ticker: str, **_kwargs: object
    ) -> sec_xbrl.IngestStats:
        calls.append(ticker)
        return sec_xbrl.IngestStats(accessions_inserted=0, facts_inserted=1)

    def open_fixture(_path: Path) -> sqlite3.Connection:
        return roster

    def no_delay(_seconds: float) -> None:
        pass

    monkeypatch.setattr(fetch_sec_xbrl, "open_db", open_fixture)
    monkeypatch.setattr(fetch_sec_xbrl, "start_run", start)
    monkeypatch.setattr(fetch_sec_xbrl, "end_run", finish)
    monkeypatch.setattr(fetch_sec_xbrl, "ingest_for_ticker", ingest)
    monkeypatch.setattr(fetch_sec_xbrl.time, "sleep", no_delay)
    monkeypatch.setattr(sys, "argv", ["fetch_sec_xbrl.py", "--db", str(database)])
    assert fetch_sec_xbrl.main() == (0 if mode == "empty" else 1)
    assert calls == (["META", "VEEV"] if mode == "mixed" else [])
    result = json.loads(capsys.readouterr().out)
    assert result["scope_issues"] == (
        []
        if mode == "empty"
        else [
            {"reason_code": "tracked_roster_unavailable"}
            if mode == "missing"
            else {
                "reason_code": "tracked_company_invalid",
                "row_id": 1,
                "ticker": "BROKEN",
                "fields": ["instrument_type"],
            }
        ]
    )
    if mode == "suppressed":
        assert statuses == []
        assert result["status"] == "already_done"
    else:
        assert statuses == [StageStatus.OK if mode == "empty" else StageStatus.FAILED]
        assert result["failed"] == 0
        assert result["status"] == (StageStatus.OK if mode == "empty" else StageStatus.FAILED).value
