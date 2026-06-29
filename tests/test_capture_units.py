"""The Ledger deterministic capture units: matcher, scrub, transcribe (mocked)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from capture import matcher, scrub, transcribe
from capture.matcher import build_roster_index, match_ticker


def _roster() -> matcher.RosterIndex:
    return build_roster_index(
        symbols=["NU", "MELI", "GOOGL"], phrases={"nubank": "NU", "mercadolibre": "MELI"}
    )


def test_match_roster_exact_symbol() -> None:
    assert match_ticker("MELI looks cheap here", _roster()).ticker == "MELI"


def test_match_alias_phrase() -> None:
    res = match_ticker("been thinking about Nubank's NPL formation", _roster())
    assert res.ticker == "NU"
    assert res.needs_ticker is False


def test_match_googl_canonicalizes_to_goog() -> None:
    assert match_ticker("GOOGL antitrust overhang again", _roster()).ticker == "GOOG"


def test_match_none_when_no_roster_mention() -> None:
    res = match_ticker("the market feels toppy and i'm uneasy", _roster())
    assert res.ticker is None
    assert res.needs_ticker is False
    assert res.candidates == ()


def test_match_multiple_is_needs_ticker() -> None:
    res = match_ticker("NU and MELI both look compelling", _roster())
    assert res.ticker is None
    assert res.needs_ticker is True
    assert res.candidates == ("MELI", "NU")


def test_lowercase_symbol_does_not_false_match() -> None:
    r = build_roster_index(symbols=["NOW"], phrases={"servicenow": "NOW"})
    # the common word "now" must NOT attribute to the NOW ticker
    assert match_ticker("i'll add more now that it dipped", r).ticker is None
    # the distinctive phrase does
    assert match_ticker("servicenow guidance was strong", r).ticker == "NOW"
    # a typed uppercase symbol does
    assert match_ticker("NOW at a 52-week low", r).ticker == "NOW"


def test_load_roster_from_tracked_companies(tmp_path: Path) -> None:
    db = tmp_path / "roster.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE tracked_companies (user_id TEXT, ticker TEXT, name TEXT, archived_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO tracked_companies (user_id, ticker, name, archived_at) VALUES (?, ?, ?, ?)",
        [
            ("bhanu", "MELI", "MercadoLibre Inc", None),
            ("bhanu", "ZZZ", "Archived Co", "2026-01-01"),
        ],
    )
    conn.commit()
    conn.close()
    r = matcher.load_roster(db)
    assert match_ticker("MELI", r).ticker == "MELI"
    assert match_ticker("mercadolibre inc earnings", r).ticker == "MELI"
    assert match_ticker("ZZZ", r).ticker is None  # archived excluded


def test_load_roster_degrades_without_db(tmp_path: Path) -> None:
    r = matcher.load_roster(tmp_path / "absent.db")
    assert match_ticker("nubank is interesting", r).ticker == "NU"


def test_scrub_redacts_pii() -> None:
    res = scrub.scrub(
        "call 415-555-0172 or email jane.doe@example.com, ssn 123-45-6789, card 4111111111111111"
    )
    assert "[phone]" in res.text
    assert "[email]" in res.text
    assert "[ssn]" in res.text
    assert "[account]" in res.text
    assert "jane.doe@example.com" not in res.text
    assert res.total >= 4


def test_scrub_preserves_tickers_amounts_years() -> None:
    s = "NU revenue grew to $1,234,567 in 2025, up 12% YoY; FCF margin 18.5%"
    res = scrub.scrub(s)
    assert res.text == s
    assert res.total == 0


class _Seg:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeModel:
    def transcribe(self, path: str, beam_size: int = 1) -> tuple[list[_Seg], None]:
        return ([_Seg(" hello "), _Seg("world")], None)


def test_transcribe_joins_segments_and_loads_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcribe._MODEL = None
    loads = {"n": 0}

    def _load() -> object:
        loads["n"] += 1
        return _FakeModel()

    monkeypatch.setattr(transcribe, "_load_model", _load)
    audio = tmp_path / "m.oga"
    audio.write_bytes(b"\x00")
    assert transcribe.transcribe(audio) == "hello world"
    assert transcribe.transcribe(audio) == "hello world"
    assert loads["n"] == 1  # model loaded once, reused
    transcribe._MODEL = None


def test_transcribe_missing_file_raises() -> None:
    transcribe._MODEL = None
    with pytest.raises(transcribe.TranscriptionError):
        transcribe.transcribe("/no/such/file.oga")


def test_transcribe_empty_result_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    transcribe._MODEL = None

    class _Empty:
        def transcribe(self, path: str, beam_size: int = 1) -> tuple[list[_Seg], None]:
            return ([], None)

    monkeypatch.setattr(transcribe, "_load_model", lambda: _Empty())
    audio = tmp_path / "e.oga"
    audio.write_bytes(b"\x00")
    with pytest.raises(transcribe.TranscriptionError):
        transcribe.transcribe(audio)
    transcribe._MODEL = None
