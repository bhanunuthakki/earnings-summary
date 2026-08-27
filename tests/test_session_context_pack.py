"""Tests for the Claude-session bridge's READ surface (B8,
execution/session_context_pack.py).

One fully-migrated tmp-db fixture (the ``test_decision_journal_view.py``
bootstrap-DDL + ``command.upgrade(cfg, "head")`` pattern — every real
migration runs in order against the bootstrap tables ``db.py``'s
``init_db()`` normally creates, so the pack is validated against the true
production schema). Seeds one tenet, one stance, one open musing, one owner
decision; calls ``build_pack`` directly (no subprocess) and asserts each
section renders its seed, the empty-section behavior for research-task
prompts (no ``research_tasks`` row carries a ``session_prompt`` in its
``task_metadata_json`` in this fixture), and that the whole build is zero-LLM.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from alembic.config import Config

from execution import session_context_pack
from synthesis.insights import record_insight
from synthesis.tenets import record_tenet
from user_state.notes import create_note

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Verbatim from tests/test_decision_journal_view.py — the three tables
# db.py:init_db() creates outside alembic; every migration from 0001 on
# assumes these already exist. Duplicated per the repo's
# duplicate-simple-shared-logic convention rather than importing a sibling
# test module.
_BOOTSTRAP_DDL = """
CREATE TABLE IF NOT EXISTS tracked_companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT DEFAULT 'bhanu',
    ticker TEXT NOT NULL,
    name TEXT NOT NULL,
    list_type TEXT NOT NULL CHECK(list_type IN (
        'portfolio', 'watchlist', 'evaluation', 'none', 'etf', 'index_member'
    )),
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    sec_validated BOOLEAN DEFAULT 0,
    ir_url TEXT DEFAULT NULL,
    model_url TEXT DEFAULT NULL,
    publishes_release BOOLEAN DEFAULT 0,
    publishes_slides BOOLEAN DEFAULT 0,
    publishes_transcript BOOLEAN DEFAULT 0,
    fmp_data_upto TEXT DEFAULT NULL,
    manual_data_quarters TEXT DEFAULT '[]',
    fmp_data_saved BOOLEAN DEFAULT 0,
    UNIQUE(user_id, ticker)
);
CREATE TABLE IF NOT EXISTS quarterly_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    year INTEGER NOT NULL,
    quarter TEXT NOT NULL,
    has_release_file    BOOLEAN DEFAULT 0,
    has_slides_file     BOOLEAN DEFAULT 0,
    has_transcript_file BOOLEAN DEFAULT 0,
    has_audio_file      BOOLEAN DEFAULT 0,
    step_audio_transcribed BOOLEAN DEFAULT 0,
    step_llm_summarized    BOOLEAN DEFAULT 0,
    step_saydo_analyzed    BOOLEAN DEFAULT 0,
    step_thesis_updated    BOOLEAN DEFAULT 0,
    UNIQUE(ticker, year, quarter)
);
CREATE TABLE IF NOT EXISTS fmp_endpoint_status (
    ticker         TEXT    NOT NULL,
    endpoint       TEXT    NOT NULL,
    period         TEXT    NOT NULL DEFAULT '',
    status         TEXT    NOT NULL,
    http_code      INTEGER,
    record_count   INTEGER,
    earliest_date  TEXT,
    latest_date    TEXT,
    file_path      TEXT,
    file_bytes     INTEGER,
    error_msg      TEXT,
    last_pulled    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ticker, endpoint, period)
);
"""


def _bootstrap_base_tables(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_BOOTSTRAP_DDL)
        conn.commit()
    finally:
        conn.close()


def _cfg(db: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    return cfg


@pytest.fixture
def db_path(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    db_dir = tmp_path / "data"
    db_dir.mkdir(parents=True, exist_ok=True)
    db = db_dir / "portfolio.db"
    return migrated_db(db)


def _insert_owner_decision(
    db_path: Path,
    *,
    ticker: str,
    kind: str,
    made_at: str,
    falsifier: str | None,
) -> int:
    """A minimal ``decided_by='owner'``, ungraded (``outcome_label`` NULL)
    decision row — the shape ``v_decision_journal`` (0179) reads from."""
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "INSERT INTO decisions "
            "(ticker, recommendation_kind, decided_by, made_at, created_at, scope, falsifier) "
            "VALUES (?, ?, 'owner', ?, ?, 'ticker', ?)",
            (ticker, kind, made_at, made_at, falsifier),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# build_pack — section rendering
# ---------------------------------------------------------------------------


def test_pack_renders_header_with_timestamp_and_freshness(db_path: Path) -> None:
    pack = session_context_pack.build_pack(db_path)
    assert "# Research Session Context Pack" in pack
    assert "_Generated (machine, naive-UTC):" in pack
    assert "_DB freshness (max updated_at across sources):" in pack


def test_pack_renders_seeded_tenet(db_path: Path) -> None:
    record_tenet(
        body_md="Let winning theses run; trim on a double when conviction fades.",
        scope_key="exit-discipline",
        db_path=db_path,
    )
    pack = session_context_pack.build_pack(db_path)
    assert "### Tenets" in pack
    assert "Let winning theses run" in pack
    assert "`[tenet:exit-discipline]`" in pack


def test_pack_renders_seeded_stance(db_path: Path) -> None:
    record_insight(
        scope_key="MELI",
        kind="stance",
        body_md="Conviction intact through the guidance cut.",
        source_note_ids=[],
        watermark_id=None,
        db_path=db_path,
    )
    pack = session_context_pack.build_pack(db_path)
    assert "### Stances" in pack
    assert "**MELI**" in pack
    assert "Conviction intact through the guidance cut." in pack


def test_pack_renders_no_themes_as_empty_state(db_path: Path) -> None:
    pack = session_context_pack.build_pack(db_path)
    # No theme insight seeded in this fixture at all — the section still
    # renders with the explicit empty-state note, never a crash or omission.
    themes_idx = pack.index("### Themes")
    next_section_idx = pack.index("## Open Questions")
    themes_block = pack[themes_idx:next_section_idx]
    assert "_None recorded._" in themes_block


def test_pack_renders_open_musing(db_path: Path) -> None:
    create_note(
        ticker=None,
        kind="musing",
        body="should I trim the winner into strength?",
        source="manual",
        db_path=db_path,
    )
    pack = session_context_pack.build_pack(db_path)
    assert "## Open Questions & Wonderings" in pack
    assert "should I trim the winner into strength?" in pack


def test_pack_open_questions_empty_state(db_path: Path) -> None:
    pack = session_context_pack.build_pack(db_path)
    q_idx = pack.index("## Open Questions & Wonderings")
    d_idx = pack.index("## Open Decisions")
    block = pack[q_idx:d_idx]
    assert "_None open._" in block


def test_pack_renders_owner_decision_with_falsifier(db_path: Path) -> None:
    _insert_owner_decision(
        db_path,
        ticker="NVDA",
        kind="add",
        made_at="2026-07-01T00:00:00",
        falsifier="GPU demand rolls over 2 quarters straight",
    )
    pack = session_context_pack.build_pack(db_path)
    assert "## Open Decisions & Falsifiers (owner, ungraded)" in pack
    assert "**NVDA**" in pack
    assert "add" in pack
    assert "GPU demand rolls over 2 quarters straight" in pack


def test_pack_research_task_prompts_empty_when_none_carry_session_prompt(
    db_path: Path,
) -> None:
    """No research_tasks row in this fixture carries a session_prompt in its
    task_metadata_json, so the section renders an explicit 'none' note."""
    pack = session_context_pack.build_pack(db_path)
    assert "## Research Tasks -> Claude Session" in pack
    section_idx = pack.index("## Research Tasks -> Claude Session")
    profile_idx = pack.index("## Owner Profile")
    block = pack[section_idx:profile_idx]
    assert ("_None pending._" in block) or ("task_metadata_json` column" in block)


def test_pack_renders_session_prompt_from_task_metadata_json(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        now = "2026-07-20T00:00:00"
        conn.execute(
            "INSERT INTO research_tasks "
            "(claim, ticker, status, task_metadata_json, created_at, updated_at) "
            "VALUES (?, ?, 'proposed', ?, ?, ?)",
            (
                "does NU's NIM hold up?",
                "NU",
                json.dumps({"session_prompt": "# Research NU NIM\n\nUse primary sources."}),
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    pack = session_context_pack.build_pack(db_path)

    assert "### Task #" in pack
    assert "_Claim:_ does NU's NIM hold up?" in pack
    assert "# Research NU NIM\n\nUse primary sources." in pack


def test_pack_ignores_invalid_or_missing_task_metadata(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        now = "2026-07-20T00:00:00"
        for metadata in ("not-json", json.dumps(["not", "an", "object"]), None):
            conn.execute(
                "INSERT INTO research_tasks "
                "(claim, ticker, status, task_metadata_json, created_at, updated_at) "
                "VALUES (?, ?, 'proposed', ?, ?, ?)",
                ("unrenderable task", "NU", metadata, now, now),
            )
        conn.commit()
    finally:
        conn.close()

    pack = session_context_pack.build_pack(db_path)
    section_idx = pack.index("## Research Tasks -> Claude Session")
    profile_idx = pack.index("## Owner Profile")
    block = pack[section_idx:profile_idx]

    assert "_None pending._" in block
    assert "unrenderable task" not in block


def test_pack_limits_to_newest_valid_task_prompts(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        now = "2026-07-20T00:00:00"
        for index in range(12):
            conn.execute(
                "INSERT INTO research_tasks "
                "(claim, ticker, status, task_metadata_json, created_at, updated_at) "
                "VALUES (?, ?, 'proposed', ?, ?, ?)",
                (
                    f"valid task {index}",
                    "NU",
                    json.dumps({"session_prompt": f"Prompt {index}"}),
                    now,
                    now,
                ),
            )
        # Newest row is malformed; it must not displace the newest ten valid
        # prompts or make the guarded JSON predicate raise.
        conn.execute(
            "INSERT INTO research_tasks "
            "(claim, ticker, status, task_metadata_json, created_at, updated_at) "
            "VALUES (?, ?, 'proposed', ?, ?, ?)",
            ("invalid newest task", "NU", "not-json", now, now),
        )
        conn.commit()
    finally:
        conn.close()

    pack = session_context_pack.build_pack(db_path)
    section_idx = pack.index("## Research Tasks -> Claude Session")
    profile_idx = pack.index("## Owner Profile")
    block = pack[section_idx:profile_idx]

    assert block.count("### Task #") == 10
    for index in range(2, 12):
        assert f"Prompt {index}" in block
    assert "\nPrompt 0\n" not in block
    assert "\nPrompt 1\n" not in block
    assert "invalid newest task" not in block


def test_pack_owner_profile_empty_state(db_path: Path) -> None:
    pack = session_context_pack.build_pack(db_path)
    assert "## Owner Profile (affirmed)" in pack
    assert "_None affirmed yet._" in pack


def test_pack_wondering_flag_on_note_with_linked_research_task(db_path: Path) -> None:
    """A musing with a linked research_tasks row (the 'wondering' outcome of
    research.proposals.detect_and_create_task) is flagged inline — the note
    itself never carries a context marker, so the flag is derived from the
    join, not read off the note."""
    note = create_note(
        ticker=None,
        kind="musing",
        body="does the NU NPL trend really hold up under stress?",
        source="manual",
        db_path=db_path,
    )
    conn = sqlite3.connect(str(db_path))
    try:
        now = "2026-07-20T00:00:00"
        conn.execute(
            "INSERT INTO research_tasks (note_id, claim, ticker, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'proposed', ?, ?)",
            (note.id, "does the NPL trend hold", "NU", now, now),
        )
        conn.commit()
    finally:
        conn.close()
    pack = session_context_pack.build_pack(db_path)
    assert "`[wondering]`" in pack
    assert "does the NU NPL trend really hold up under stress?" in pack


# ---------------------------------------------------------------------------
# Zero-LLM guarantee
# ---------------------------------------------------------------------------


def test_build_pack_never_calls_the_llm(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The pack is a pure read — no code path may fire an LLM call. Force any
    call through llm.structured to raise, then build a fully-seeded pack and
    confirm it completes without ever tripping that raise."""
    import llm.structured as llm_structured

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("session_context_pack must never call the LLM")

    monkeypatch.setattr(llm_structured, "call_llm_structured", _boom)

    record_tenet(body_md="Let winners run.", scope_key="exit-discipline", db_path=db_path)
    record_insight(
        scope_key="MELI",
        kind="stance",
        body_md="Conviction intact.",
        source_note_ids=[],
        watermark_id=None,
        db_path=db_path,
    )
    create_note(
        ticker=None, kind="musing", body="a captured thought", source="manual", db_path=db_path
    )
    _insert_owner_decision(
        db_path, ticker="NVDA", kind="add", made_at="2026-07-01T00:00:00", falsifier="x"
    )

    pack = session_context_pack.build_pack(db_path)
    assert "# Research Session Context Pack" in pack


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def test_main_writes_to_out_file(
    db_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = tmp_path / "pack.md"
    argv = [
        "session_context_pack.py",
        "--db-path",
        str(db_path),
        "--out",
        str(out),
    ]
    monkeypatch.setattr(sys, "argv", argv)
    rc = session_context_pack.main()
    assert rc == 0
    assert out.exists()
    assert "# Research Session Context Pack" in out.read_text(encoding="utf-8")
