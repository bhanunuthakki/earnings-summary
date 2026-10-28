"""Read-only chat context: process tenets, explicit notes and decisions."""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from execution import session_context_pack
from synthesis.insights import record_insight
from synthesis.tenets import record_tenet
from user_state.notes import create_note

PROJECT_ROOT = Path(__file__).resolve().parents[1]


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


def test_pack_excludes_retired_generated_context(db_path: Path) -> None:
    record_insight(
        scope_key="MELI",
        kind="stance",
        body_md="Obsolete generated conviction.",
        source_note_ids=[],
        watermark_id=None,
        db_path=db_path,
    )
    pack = session_context_pack.build_pack(db_path)
    assert "### Stances" not in pack
    assert "### Themes" not in pack
    assert "Obsolete generated conviction." not in pack
    assert "## Owner Profile" not in pack
    assert "## Research Tasks" not in pack


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


def test_pack_ignores_retired_task_metadata_on_explicit_note(db_path: Path) -> None:
    """Old automatic task metadata cannot add a nudge to an explicit note."""
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
    assert "`[wondering]`" not in pack
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


def test_pack_requires_database_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EARNINGS_SUMMARY_DB_PATH", raising=False)
    with pytest.raises(ValueError, match="EARNINGS_SUMMARY_DB_PATH is required"):
        session_context_pack.build_pack()


def test_pack_uses_configured_database(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(db_path))
    record_tenet(body_md="Keep the owner process explicit.", scope_key="process", db_path=db_path)
    assert "Keep the owner process explicit." in session_context_pack.build_pack()
