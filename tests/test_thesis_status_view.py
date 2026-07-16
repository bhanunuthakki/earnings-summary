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
from pathlib import Path

from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from user_state.ledger import append_entry  # noqa: E402
from user_state.notes import create_note  # noqa: E402
from user_state.thesis_status import read_thesis_status  # noqa: E402

_PRIOR_HEAD = "0059_kpi_facts_restatement"


# thesis_state is created by migration 0008; the test stamps at 0059, so it's
# marked-applied-but-not-run. Create it here to match the 0008 schema (prod's
# shape) so the lazily-bound view resolves it. raw_json is NOT NULL by schema.
_THESIS_STATE_DDL = """
CREATE TABLE thesis_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR NOT NULL,
    thesis TEXT,
    last_updated DATETIME,
    breach_status VARCHAR,
    raw_json TEXT NOT NULL,
    ingested_at DATETIME NOT NULL,
    CONSTRAINT uq_thesis_state_ticker UNIQUE (ticker)
)
"""


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


def _build_db(db_path: Path) -> None:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.stamp(cfg, _PRIOR_HEAD)

    # thesis_state must exist BEFORE the chain runs (as it does on prod, where
    # 0008 ran for real): 0143 now creates the view only when all its source
    # tables are present — a dangling view breaks every later batch-migration
    # rename (the 2026-07-16 CI incident).
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(_THESIS_STATE_DDL)
        conn.commit()
    finally:
        conn.close()

    command.upgrade(cfg, "head")

    # NU: a real thesis + two ledger entries + one open question + one open musing.
    _seed_thesis_state(db_path, "NU", "NIM holds despite mix shift.", "ok")
    append_entry(ticker="NU", entry_kind="thesis_update", body="entry 1", db_path=db_path)
    append_entry(ticker="NU", entry_kind="earnings_prep_append", body="entry 2", db_path=db_path)
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


def test_read_thesis_status_rich_ticker(tmp_path: Path) -> None:
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    _build_db(db)

    out = read_thesis_status(["NU", "WIX", "EMPT", "FLKR"], db_path=db)

    nu = out["NU"]
    assert nu.has_written_thesis is True
    assert nu.breach_status == "ok"
    assert nu.ledger_entry_count == 2
    assert nu.last_ledger_at is not None
    assert nu.open_notes_count == 2
    assert nu.open_questions_count == 1


def test_thesis_only_ticker_reads_has_thesis(tmp_path: Path) -> None:
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    _build_db(db)

    wix = read_thesis_status(["WIX"], db_path=db)["WIX"]
    assert wix.has_written_thesis is True
    assert wix.breach_status == "warn"
    assert wix.ledger_entry_count == 0
    assert wix.last_ledger_at is None
    assert wix.open_notes_count == 0


def test_empty_thesis_reads_false_but_present(tmp_path: Path) -> None:
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    _build_db(db)

    out = read_thesis_status(["EMPT"], db_path=db)
    assert "EMPT" in out
    assert out["EMPT"].has_written_thesis is False


def test_stub_thesis_reads_false_but_present(tmp_path: Path) -> None:
    """Red-team wave A: the literal "STUB: needs user-authored thesis"
    placeholder defeated every has-a-thesis predicate. It must read
    has_written_thesis=0 (while staying visible in the view — the row has a
    footprint), exactly like an empty thesis."""
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    _build_db(db)

    out = read_thesis_status(["STB", "NU"], db_path=db)
    assert "STB" in out
    assert out["STB"].has_written_thesis is False
    # A real thesis still reads 1 — the stub filter must not over-exclude.
    assert out["NU"].has_written_thesis is True


def test_unknown_ticker_absent_and_case_insensitive(tmp_path: Path) -> None:
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    _build_db(db)

    out = read_thesis_status(["nu", "ZZZZ"], db_path=db)
    assert "NU" in out  # lowercase input resolves to the canonical upper key
    assert "ZZZZ" not in out  # no footprint → absent, not a zero-row


def test_empty_tickers_short_circuits(tmp_path: Path) -> None:
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    _build_db(db)
    assert read_thesis_status([], db_path=db) == {}


def test_missing_view_degrades_to_empty(tmp_path: Path) -> None:
    # A DB file with no v_thesis_status (never migrated) must not raise.
    db = tmp_path / "bare.db"
    sqlite3.connect(str(db)).close()
    assert read_thesis_status(["NU"], db_path=db) == {}
