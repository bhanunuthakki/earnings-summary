"""Focused tests for the FMP-date refresh connection/commit contract."""

from __future__ import annotations

import pytest

import db as dbmod


class _Cursor:
    def execute(self, sql: str, params: tuple[str, ...]) -> None:
        assert "SELECT ticker FROM tracked_companies" in sql
        assert params == ("bhanu",)

    def fetchall(self) -> list[dict[str, str]]:
        return [{"ticker": "NU"}, {"ticker": "META"}, {"ticker": "RBRK"}]


class _Connection:
    def __init__(self) -> None:
        self.cursor_obj = _Cursor()
        self.commit_count = 0
        self.close_count = 0

    def cursor(self) -> _Cursor:
        return self.cursor_obj

    def commit(self) -> None:
        self.commit_count += 1

    def close(self) -> None:
        self.close_count += 1


def test_refresh_reuses_connection_and_commits_each_ticker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection()
    connections: list[_Connection] = []
    updated: list[tuple[object, str]] = []

    def get_connection() -> _Connection:
        connections.append(connection)
        return connection

    def update(cursor: object, ticker: str) -> None:
        updated.append((cursor, ticker))

    monkeypatch.setattr(dbmod, "get_connection", get_connection)
    monkeypatch.setattr(dbmod, "_update_company_fmp_state", update)

    dbmod.refresh_all_fmp_dates()

    assert connections == [connection]
    assert [ticker for _cursor, ticker in updated] == ["NU", "META", "RBRK"]
    assert all(cursor is connection.cursor_obj for cursor, _ticker in updated)
    assert connection.commit_count == 3
    assert connection.close_count == 1


def test_refresh_keeps_prior_ticker_commits_on_later_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection()
    updated: list[str] = []

    def update(_cursor: object, ticker: str) -> None:
        updated.append(ticker)
        if ticker == "META":
            raise RuntimeError("synthetic refresh failure")

    monkeypatch.setattr(dbmod, "get_connection", lambda: connection)
    monkeypatch.setattr(dbmod, "_update_company_fmp_state", update)

    try:
        dbmod.refresh_all_fmp_dates()
    except RuntimeError as exc:
        assert str(exc) == "synthetic refresh failure"
    else:
        raise AssertionError("refresh should propagate the per-ticker failure")

    assert updated == ["NU", "META"]
    assert connection.commit_count == 1
    assert connection.close_count == 1
