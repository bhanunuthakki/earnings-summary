"""Hermetic tests for signals.takeaway (S11 podcast takeaway summarizer).

All LLM transport is monkeypatched — no real Claude calls. The DB is built
with ``signals_only()`` (alembic 0094 stamp → 0102 upgrade) plus a minimal
``tracked_companies`` stub so the fixture is self-contained.

Coverage:
  * NULL body → summarized
  * Short body (< _BODY_TOO_SHORT) → summarized
  * Long body (>= _BODY_TOO_SHORT) → skipped
  * Non-media_appearance signal → skipped by WHERE clause
  * LLM returns empty string → not written (summarize_one returns False)
  * LLM response truncated at 400 chars
  * Transient LLM error → logs warning, returns False
  * Hard stop (LLMSetupError) → propagates out of summarize_pending
  * Hard stop (LLMBudgetExceeded) → propagates and breaks loop
  * summarize_pending limit param
  * summarize_pending returns count of updates
  * Missing DB → returns 0 gracefully
  * Missing signals table → returns 0 gracefully
  * Prompt content: show name / title / raw body / version tag
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import signals.takeaway as takeaway_mod
from llm.cli import LLMBudgetExceeded, LLMSetupError
from signals.takeaway import _BODY_TOO_SHORT, PURPOSE, summarize_one, summarize_pending
from tests._signals_fixtures import signals_only

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _build_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test.db"
    signals_only(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS tracked_companies "
            "(ticker TEXT NOT NULL, name TEXT NOT NULL, archived_at TEXT)"
        )
        conn.commit()
    finally:
        conn.close()
    return db_path
_URL_SEQ = 0


def _insert_signal(
    db_path: Path,
    *,
    ticker: str = "NU",
    firm: str = "Invest Like the Best",
    title: str = "NU CEO on fintech moats",
    body: str | None = None,
    published_at: str = "2026-06-12T10:00:00",
    signal_type: str = "media_appearance",
) -> int:
    global _URL_SEQ
    _URL_SEQ += 1
    url = f"https://example.com/ep/{_URL_SEQ}"
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            """INSERT INTO signals
               (ticker, signal_type, firm, title, body, url,
                published_at, event_date, weight, cadence, source_feed, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 0.7, 'event', 'podcast_rss',
                       '2026-06-12T10:00:00')""",
            (ticker, signal_type, firm, title, body, url, published_at),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def _get_body(db_path: Path, signal_id: int) -> str | None:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT body FROM signals WHERE id = ?", (signal_id,)).fetchone()
        return str(row[0]) if row and row[0] is not None else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# summarize_one
# ---------------------------------------------------------------------------


def test_summarize_one_null_body(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = _build_db(tmp_path)
    sid = _insert_signal(db_path, body=None)
    monkeypatch.setattr(
        takeaway_mod, "call_llm", lambda *a, **k: "NU is expanding credit. Watch NIM."
    )

    conn = sqlite3.connect(str(db_path))
    try:
        result = summarize_one(
            conn,
            signal_id=sid,
            show_name="Invest Like the Best",
            title="NU CEO on fintech moats",
            raw_body=None,
        )
        conn.commit()
    finally:
        conn.close()

    assert result is True
    assert _get_body(db_path, sid) == "NU is expanding credit. Watch NIM."


def test_summarize_one_short_body(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = _build_db(tmp_path)
    short = "Tune in to hear the CEO discuss NU."
    sid = _insert_signal(db_path, body=short)
    monkeypatch.setattr(
        takeaway_mod, "call_llm", lambda *a, **k: "NU CEO bullish on Brazil credit."
    )

    conn = sqlite3.connect(str(db_path))
    try:
        result = summarize_one(
            conn,
            signal_id=sid,
            show_name="Invest Like the Best",
            title="NU CEO",
            raw_body=short,
        )
        conn.commit()
    finally:
        conn.close()

    assert result is True
    body = _get_body(db_path, sid)
    assert body == "NU CEO bullish on Brazil credit."


def test_summarize_one_empty_llm_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = _build_db(tmp_path)
    sid = _insert_signal(db_path, body=None)
    monkeypatch.setattr(takeaway_mod, "call_llm", lambda *a, **k: "   ")

    conn = sqlite3.connect(str(db_path))
    try:
        result = summarize_one(conn, signal_id=sid, show_name="BG2", title="episode", raw_body=None)
    finally:
        conn.close()

    assert result is False
    assert _get_body(db_path, sid) is None


def test_summarize_one_truncates_at_400_chars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _build_db(tmp_path)
    sid = _insert_signal(db_path, body=None)
    long_response = "A" * 500
    monkeypatch.setattr(takeaway_mod, "call_llm", lambda *a, **k: long_response)

    conn = sqlite3.connect(str(db_path))
    try:
        summarize_one(conn, signal_id=sid, show_name="BG2", title="ep", raw_body=None)
        conn.commit()
    finally:
        conn.close()

    body = _get_body(db_path, sid)
    assert body is not None
    assert len(body) == 400


def test_summarize_one_transient_error_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _build_db(tmp_path)
    sid = _insert_signal(db_path, body=None)
    monkeypatch.setattr(
        takeaway_mod,
        "call_llm",
        lambda *a, **k: (_ for _ in ()).throw(TimeoutError("claude timed out")),
    )

    conn = sqlite3.connect(str(db_path))
    try:
        result = summarize_one(conn, signal_id=sid, show_name="BG2", title="ep", raw_body=None)
    finally:
        conn.close()

    assert result is False
    assert _get_body(db_path, sid) is None


def test_summarize_one_llm_setup_error_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _build_db(tmp_path)
    sid = _insert_signal(db_path, body=None)
    monkeypatch.setattr(
        takeaway_mod,
        "call_llm",
        lambda *a, **k: (_ for _ in ()).throw(LLMSetupError("claude not found")),
    )

    conn = sqlite3.connect(str(db_path))
    try:
        with pytest.raises(LLMSetupError):
            summarize_one(conn, signal_id=sid, show_name="BG2", title="ep", raw_body=None)
    finally:
        conn.close()


def test_summarize_one_prompt_includes_show_title_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _build_db(tmp_path)
    sid = _insert_signal(db_path, body="short rss copy")
    captured: list[str] = []

    def capture(prompt: str, **kwargs: object) -> str:
        captured.append(prompt)
        return "A relevant takeaway for the portfolio."

    monkeypatch.setattr(takeaway_mod, "call_llm", capture)
    conn = sqlite3.connect(str(db_path))
    try:
        summarize_one(
            conn,
            signal_id=sid,
            show_name="Acquired",
            title="LVMH: the luxury empire",
            raw_body="short rss copy",
        )
    finally:
        conn.close()

    assert captured
    prompt = captured[0]
    assert "Acquired" in prompt
    assert "LVMH: the luxury empire" in prompt
    assert "short rss copy" in prompt
    assert "podcast_takeaway_summary" in prompt
    assert PURPOSE == "podcast_takeaway_summary"


def test_summarize_one_prompt_no_description_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _build_db(tmp_path)
    sid = _insert_signal(db_path, body=None)
    captured: list[str] = []

    def capture(prompt: str, **kwargs: object) -> str:
        captured.append(prompt)
        return "No description but still a good take."

    monkeypatch.setattr(takeaway_mod, "call_llm", capture)
    conn = sqlite3.connect(str(db_path))
    try:
        summarize_one(conn, signal_id=sid, show_name="Odd Lots", title="Macro ep", raw_body=None)
    finally:
        conn.close()

    assert captured
    assert "No description provided" in captured[0]


# ---------------------------------------------------------------------------
# summarize_pending
# ---------------------------------------------------------------------------


def test_summarize_pending_null_body(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = _build_db(tmp_path)
    _insert_signal(db_path, body=None)
    monkeypatch.setattr(
        takeaway_mod, "call_llm", lambda *a, **k: "NIM expansion signals pricing power."
    )
    written = summarize_pending(db_path)
    assert written == 1


def test_summarize_pending_skips_long_body(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = _build_db(tmp_path)
    long_body = "X" * _BODY_TOO_SHORT  # exactly at threshold → NOT below it
    _insert_signal(db_path, body=long_body)
    called = [False]

    def fail_if_called(*a: object, **k: object) -> str:
        called[0] = True
        return "should not be called"

    monkeypatch.setattr(takeaway_mod, "call_llm", fail_if_called)
    written = summarize_pending(db_path)
    assert written == 0
    assert not called[0]


def test_summarize_pending_skips_non_media_appearance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _build_db(tmp_path)
    _insert_signal(db_path, signal_type="consensus_rating", body=None)
    monkeypatch.setattr(takeaway_mod, "call_llm", lambda *a, **k: "should not be called")
    written = summarize_pending(db_path)
    assert written == 0


def test_summarize_pending_counts_updates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = _build_db(tmp_path)
    for i in range(3):
        _insert_signal(db_path, ticker=f"T{i}", body=None, published_at=f"2026-06-1{i}T10:00:00")
    monkeypatch.setattr(takeaway_mod, "call_llm", lambda *a, **k: "Takeaway text here.")
    written = summarize_pending(db_path)
    assert written == 3


def test_summarize_pending_respects_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = _build_db(tmp_path)
    for i in range(5):
        _insert_signal(db_path, ticker=f"T{i}", body=None, published_at=f"2026-06-1{i}T10:00:00")
    monkeypatch.setattr(takeaway_mod, "call_llm", lambda *a, **k: "Takeaway.")
    written = summarize_pending(db_path, limit=2)
    assert written == 2


def test_summarize_pending_hard_stop_breaks_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _build_db(tmp_path)
    for i in range(3):
        _insert_signal(db_path, ticker=f"T{i}", body=None, published_at=f"2026-06-1{i}T10:00:00")
    calls = [0]

    def hard_stop_after_one(prompt: str, **k: object) -> str:
        calls[0] += 1
        if calls[0] == 1:
            return "First takeaway written."
        raise LLMBudgetExceeded("cap exceeded")

    monkeypatch.setattr(takeaway_mod, "call_llm", hard_stop_after_one)
    written = summarize_pending(db_path)
    assert written == 1
    assert calls[0] == 2


def test_summarize_pending_missing_db(tmp_path: Path) -> None:
    result = summarize_pending(tmp_path / "nonexistent.db")
    assert result == 0


def test_summarize_pending_missing_signals_table(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.db"
    conn = sqlite3.connect(str(db_path))
    conn.close()
    result = summarize_pending(db_path)
    assert result == 0


def test_summarize_pending_mixed_bodies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = _build_db(tmp_path)
    long_body = "Y" * _BODY_TOO_SHORT
    _insert_signal(db_path, ticker="NU", body=None, published_at="2026-06-12T10:00:00")
    _insert_signal(db_path, ticker="WIX", body="short", published_at="2026-06-11T10:00:00")
    _insert_signal(db_path, ticker="TSM", body=long_body, published_at="2026-06-10T10:00:00")
    monkeypatch.setattr(takeaway_mod, "call_llm", lambda *a, **k: "Meaningful takeaway.")
    written = summarize_pending(db_path)
    assert written == 2
