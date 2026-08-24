from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from ask.exchange_store import SessionContextV1, StoredExchangeDataError
from pipeline import evaluation_dialogues


@dataclass(frozen=True)
class _Session:
    id: str
    updated_at: str


@dataclass(frozen=True)
class _ContextRecord:
    context: SessionContextV1


@dataclass(frozen=True)
class _Note:
    ticker: str | None
    created_at: str


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "dialogues.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE tracked_companies (
          user_id TEXT, ticker TEXT, name TEXT, list_type TEXT,
          instrument_type TEXT, archived_at TEXT
        );
        CREATE TABLE discovery_candidates (
          id INTEGER, user_id TEXT, ticker TEXT, status TEXT
        );
        INSERT INTO tracked_companies VALUES
          ('bhanu', 'ZZZ', 'Zeta Stock', 'evaluation', 'equity', NULL),
          ('bhanu', 'AAA', 'Alpha Fund', 'evaluation', 'etf', NULL),
          ('bhanu', 'OLD', 'Archived', 'evaluation', 'equity', '2026-01-01');
        INSERT INTO discovery_candidates VALUES (7, 'bhanu', 'ZZZ', 'built');
        """
    )
    conn.commit()
    conn.close()
    return path


def test_dialogues_are_bounded_sorted_and_join_explicit_candidate_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _db(tmp_path)

    def fake_notes(**_: object) -> list[_Note]:
        return [_Note("ZZZ", "2026-08-20")]

    def fake_sessions(**_: object) -> list[_Session]:
        return [_Session("session-z", "2026-08-21T12:00:00Z")]

    def fake_context(*_args: object, **_kwargs: object) -> _ContextRecord:
        return _ContextRecord(
            SessionContextV1(
                company_ticker="ZZZ", evaluation_candidate_id=7, evaluation_instrument_type="stock"
            )
        )

    monkeypatch.setattr(evaluation_dialogues, "list_notes", fake_notes)
    monkeypatch.setattr(evaluation_dialogues, "list_sessions", fake_sessions)
    monkeypatch.setattr(evaluation_dialogues, "get_session_context", fake_context)

    result = evaluation_dialogues.load_evaluation_dialogues(path)

    assert [item.ticker for item in result.items] == ["AAA", "ZZZ"]
    fund, stock = result.items
    assert fund.instrument_type == "etf"
    assert fund.workup_readiness == "available"
    assert fund.ask_session_link_state == "unlinked"
    assert stock.discovery_candidate_id == 7
    assert stock.ask_session_id == "session-z"
    assert stock.ask_session_link_state == "linked"
    assert stock.open_note_count == 1


def test_dialogues_fail_closed_when_company_source_is_unavailable(tmp_path: Path) -> None:
    result = evaluation_dialogues.load_evaluation_dialogues(tmp_path / "missing.db")

    assert result.state == "unavailable"
    assert result.items == ()
    assert result.reason_codes == ("evaluation_source_unavailable",)


def test_corrupt_session_context_is_skipped_without_hiding_evaluation_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _db(tmp_path)

    def no_notes(**_: object) -> list[_Note]:
        return []

    def broken_session(**_: object) -> list[_Session]:
        return [_Session("broken-session", "2026-08-21T12:00:00Z")]

    def corrupt_context(*_args: object, **_kwargs: object) -> _ContextRecord:
        raise StoredExchangeDataError("stored session context is corrupt")

    monkeypatch.setattr(evaluation_dialogues, "list_notes", no_notes)
    monkeypatch.setattr(evaluation_dialogues, "list_sessions", broken_session)
    monkeypatch.setattr(evaluation_dialogues, "get_session_context", corrupt_context)

    result = evaluation_dialogues.load_evaluation_dialogues(path)

    assert result.state == "available"
    assert [item.ticker for item in result.items] == ["AAA", "ZZZ"]
    assert all(item.ask_session_link_state == "unlinked" for item in result.items)


def test_session_context_preserves_optional_evaluation_identity() -> None:
    context = SessionContextV1(
        company_ticker="AVDV", evaluation_candidate_id=4, evaluation_instrument_type="etf"
    )

    assert context.model_dump(mode="json")["evaluation_candidate_id"] == 4
    assert context.evaluation_instrument_type == "etf"
