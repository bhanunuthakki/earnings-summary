# pyright: reportPrivateUsage=false
from __future__ import annotations

import argparse
import sqlite3
import sys
from types import SimpleNamespace
from typing import cast

import pytest

from execution import fetch_sec_xbrl
from models.companies import Company, ListType
from pipeline import sec_xbrl


def _companies(rows: list[tuple[str, ListType]]) -> list[Company]:
    return cast(
        "list[Company]",
        [SimpleNamespace(ticker=ticker, list_type=role) for ticker, role in rows],
    )


def _args(ticker: str | None = None, *, all_mapped: bool = False) -> argparse.Namespace:
    return argparse.Namespace(ticker=ticker, all_mapped=all_mapped)


def test_sec_scheduled_scope_is_portfolio_only_and_priority_ordered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def tracked(_conn: sqlite3.Connection) -> list[Company]:
        return _companies(
            [
                ("WIX", ListType.EVALUATION),
                ("NOW", ListType.WATCHLIST),
                ("META", ListType.PORTFOLIO),
                ("RBRK", ListType.PORTFOLIO),
            ]
        )

    monkeypatch.setattr(fetch_sec_xbrl, "tracked_companies_for_user", tracked)
    with sqlite3.connect(":memory:") as conn:
        assert fetch_sec_xbrl._resolve_tickers(_args(all_mapped=True), conn) == [
            "META",
            "RBRK",
        ]


def test_sec_explicit_request_uses_stored_role_and_cannot_bypass_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def tracked(_conn: sqlite3.Connection, **_kwargs: object) -> list[Company]:
        return _companies(
            [
                ("WIX", ListType.EVALUATION),
                ("NOW", ListType.WATCHLIST),
                ("IDX", ListType.INDEX_MEMBER),
            ]
        )

    monkeypatch.setattr(fetch_sec_xbrl, "tracked_companies_for_user", tracked)
    monkeypatch.setattr(fetch_sec_xbrl, "CIK_MAP", {"WIX": "1", "NOW": "2", "IDX": "3"})
    with sqlite3.connect(":memory:") as conn:
        assert fetch_sec_xbrl._resolve_tickers(_args("WIX"), conn) == ["WIX"]
        assert fetch_sec_xbrl._resolve_tickers(_args("NOW"), conn) == []
        assert fetch_sec_xbrl._resolve_tickers(_args("IDX"), conn) == []
        assert fetch_sec_xbrl._resolve_tickers(_args("UNKNOWN"), conn) == []


def test_sec_documented_foreign_non_filer_emits_an_honest_disposition(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def tracked(_conn: sqlite3.Connection) -> list[Company]:
        return _companies([("NTDOY", ListType.PORTFOLIO)])

    monkeypatch.setattr(fetch_sec_xbrl, "tracked_companies_for_user", tracked)
    with sqlite3.connect(":memory:") as conn:
        assert fetch_sec_xbrl._resolve_tickers(_args(), conn) == []

    stderr = capsys.readouterr().err
    assert '"event": "sec_no_filer_disposition"' in stderr
    assert '"disposition": "documented_non_filer"' in stderr


@pytest.mark.parametrize("status", [401, 403])
def test_companyfacts_boundary_classifies_auth_denial(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    response = SimpleNamespace(
        status_code=status,
        content=b"denied",
        raise_for_status=lambda: pytest.fail("generic HTTP path was used"),
    )
    monkeypatch.setattr(sec_xbrl.requests, "get", lambda *_args, **_kwargs: response)

    with pytest.raises(sec_xbrl.SecCompanyFactsAuthenticationDeniedError) as exc_info:
        sec_xbrl.fetch_companyfacts("0000000001")

    assert exc_info.value.status_code == status


def test_sec_auth_denial_halts_before_the_next_ticker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    calls: list[str] = []
    terminal: dict[str, object] = {}
    monkeypatch.setattr(fetch_sec_xbrl, "open_db", lambda _path: conn)
    monkeypatch.setattr(fetch_sec_xbrl, "_resolve_tickers", lambda _args, _conn: ["META", "RBRK"])
    monkeypatch.setattr(fetch_sec_xbrl, "start_run", lambda *_args, **_kwargs: "run-1")

    def ingest(
        _conn: sqlite3.Connection,
        *,
        ticker: str,
        project_root: object,
        run_id: str,
    ) -> object:
        del project_root, run_id
        calls.append(ticker)
        raise sec_xbrl.SecCompanyFactsAuthenticationDeniedError(403)

    def finish(
        _conn: sqlite3.Connection,
        run_id: str,
        status: object,
        *,
        error_summary: str | None,
    ) -> None:
        terminal.update(run_id=run_id, status=status, error_summary=error_summary)

    monkeypatch.setattr(fetch_sec_xbrl, "ingest_for_ticker", ingest)
    monkeypatch.setattr(fetch_sec_xbrl, "end_run", finish)
    monkeypatch.setattr(sys, "argv", ["fetch_sec_xbrl.py", "--db", "unused.db"])

    assert fetch_sec_xbrl.main() == 1
    assert calls == ["META"]
    assert terminal["error_summary"] == "1 tickers failed"
