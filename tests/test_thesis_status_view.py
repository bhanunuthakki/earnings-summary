"""Tests for the ``v_thesis_status`` view (alembic 0143) and its read helper
``user_state.thesis_status.read_thesis_status``.

The view is the earnings-summary-owned read contract the companion
portfolio-tracker consumes for its monthly CIO brief, so these tests pin the
three signals the brief leans on — does a written thesis exist, is the ledger
non-empty (a "last decision" exists), and how many live notes/questions are
open — plus the two edge cases that matter across the repo boundary: a
thesis-only ticker must read ``has_written_thesis=1`` even with zero ledger/notes,
and a DB without the view must degrade to ``{}`` rather than raise.
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from user_state.ledger import append_entry  # noqa: E402
from user_state.notes import create_note  # noqa: E402
from user_state.thesis_status import read_thesis_status  # noqa: E402


def _seed_thesis_state(db_path: Path, ticker: str, thesis: str, breach: str) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO thesis_state(ticker, thesis, last_updated, breach_status, "
            "raw_json, ingested_at) VALUES (?, ?, ?, ?, ?, ?)",
            (ticker, thesis, "2026-06-01T00:00:00", breach, "{}", "2026-06-01T00:00:00"),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_db(db_path: Path) -> None:
    # NU: a real thesis + one ledger entry + one open question + one open musing.
    _seed_thesis_state(db_path, "NU", "NIM holds despite mix shift.", "ok")
    append_entry(ticker="NU", entry_kind="thesis_update", body="entry 1", db_path=db_path)
    create_note(
        ticker="NU", kind="question", body="What is deposit beta trending to?", db_path=db_path
    )
    create_note(ticker="NU", kind="musing", body="Store-of-value optionality?", db_path=db_path)

    # WIX: thesis-only — a written thesis but NO ledger, NO notes. Must still
    # read has_written_thesis=1 (the case that trips a naive ledger-only query).
    _seed_thesis_state(db_path, "WIX", "Self-creators funnel compounding.", "warn")

    # EMPT: a thesis_state row with an EMPTY thesis → has_written_thesis=0, but
    # still present in the view (it has a source-table footprint).
    _seed_thesis_state(db_path, "EMPT", "   ", "ok")

    # STB: the bulk-onboarding placeholder (74 such rows on prod). Non-empty
    # text, but NOT a written thesis — 0151 excludes it from the predicate.
    _seed_thesis_state(db_path, "STB", "STUB: needs user-authored thesis", "ok")


@pytest.fixture
def db(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    p = tmp_path / "data" / "portfolio.db"
    migrated_db(p)
    _seed_db(p)
    return p


def test_read_thesis_status_rich_ticker(db: Path) -> None:
    out = read_thesis_status(["NU", "WIX", "EMPT", "FLKR"], db_path=db)

    nu = out["NU"]
    assert nu.has_written_thesis is True
    assert nu.breach_status == "ok"
    assert nu.ledger_entry_count == 1
    assert nu.last_ledger_at is not None
    assert nu.open_notes_count == 2
    assert nu.open_questions_count == 1


def test_thesis_only_ticker_reads_has_thesis(db: Path) -> None:
    wix = read_thesis_status(["WIX"], db_path=db)["WIX"]
    assert wix.has_written_thesis is True
    assert wix.breach_status == "warn"
    assert wix.ledger_entry_count == 0
    assert wix.last_ledger_at is None
    assert wix.open_notes_count == 0


def test_empty_thesis_reads_false_but_present(db: Path) -> None:
    out = read_thesis_status(["EMPT"], db_path=db)
    assert "EMPT" in out
    assert out["EMPT"].has_written_thesis is False


def test_stub_thesis_reads_false_but_present(db: Path) -> None:
    """Red-team wave A: the literal "STUB: needs user-authored thesis"
    placeholder defeated every has-a-thesis predicate. It must read
    has_written_thesis=0 (while staying visible in the view — the row has a
    footprint), exactly like an empty thesis."""
    out = read_thesis_status(["STB", "NU"], db_path=db)
    assert "STB" in out
    assert out["STB"].has_written_thesis is False
    # A real thesis still reads 1 — the stub filter must not over-exclude.
    assert out["NU"].has_written_thesis is True


def test_embedded_stub_marker_reads_false_innocent_words_do_not(db: Path) -> None:
    """Red-team wave B: prod carries stub rows with the marker EMBEDDED
    mid-text (live example, ROP: "Roper Technologies — diversified industrial
    software. STUB: needs user-authored thesis…"). 0151's prefix predicate
    missed them; 0152 matches the literal ``STUB:`` token as a substring.
    Prose that merely contains stub-ish words (stubborn, STUBHUB — no colon)
    must NOT be excluded."""
    _seed_thesis_state(db, "EMB", "Real sentence. STUB: needs user-authored thesis", "ok")
    _seed_thesis_state(
        db, "SBRN", "A stubbornly durable moat; the STUBHUB comp is irrelevant.", "ok"
    )

    out = read_thesis_status(["EMB", "SBRN"], db_path=db)
    assert out["EMB"].has_written_thesis is False  # embedded marker excluded
    assert out["SBRN"].has_written_thesis is True  # innocent words survive


def test_unknown_ticker_absent_and_case_insensitive(db: Path) -> None:
    out = read_thesis_status(["nu", "ZZZZ"], db_path=db)
    assert "NU" in out  # lowercase input resolves to the canonical upper key
    assert "ZZZZ" not in out  # no footprint → absent, not a zero-row


def test_empty_tickers_short_circuits(db: Path) -> None:
    assert read_thesis_status([], db_path=db) == {}


def test_missing_view_degrades_to_empty(tmp_path: Path) -> None:
    # A DB file with no v_thesis_status (never migrated) must not raise.
    db = tmp_path / "bare.db"
    sqlite3.connect(str(db)).close()
    assert read_thesis_status(["NU"], db_path=db) == {}
