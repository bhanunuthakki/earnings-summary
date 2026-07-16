"""Phase-1 W1-3: the research_tasks store + the fire-and-forget detection tap."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from capture import ingest
from capture.matcher import build_roster_index
from research import proposals
from user_state import notes

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0059_kpi_facts_restatement"
ROSTER = build_roster_index(symbols=["NU"], phrases={"nubank": "NU"})


def _cfg(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "ledger.db"
    cfg = _cfg(db)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, "head")
    return db


def _land(db_path: Path, text: str) -> int:
    return (
        ingest.ingest_capture(channel="tray", text=text, roster=ROSTER, db_path=db_path).note_id
        or 0
    )


def _yes(text: str) -> dict[str, object]:
    return {"intent": "wondering", "claim": "do NU margins hold?", "ticker": "NU"}


def test_create_list_and_status(db_path: Path) -> None:
    tid = proposals.create_task(note_id=1, claim="do margins hold?", ticker="NU", db_path=db_path)
    assert tid > 0
    tasks = proposals.list_tasks(status="proposed", db_path=db_path)
    assert len(tasks) == 1
    assert tasks[0].claim == "do margins hold?"
    assert tasks[0].ticker == "NU"
    proposals.set_task_status(tid, "running", db_path=db_path)
    got = proposals.get_task(tid, db_path=db_path)
    assert got is not None and got.status == "running"


def test_tap_creates_task_for_wondering(db_path: Path) -> None:
    note_id = _land(db_path, "do NU's margins still hold up here?")
    tid = proposals.detect_and_create_task(note_id, db_path=db_path, call=_yes)
    assert tid is not None
    task = proposals.get_task(tid, db_path=db_path)
    assert task is not None
    assert task.status == "proposed"
    assert task.note_id == note_id
    assert task.ticker == "NU"


def test_tap_no_task_for_observation(db_path: Path) -> None:
    note_id = _land(db_path, "NU's NPL formation ticked up to 4.5% this quarter")
    calls = {"n": 0}

    def obs(text: str) -> dict[str, object]:
        calls["n"] += 1
        return {"intent": "observation", "claim": "", "ticker": None}

    assert proposals.detect_and_create_task(note_id, db_path=db_path, call=obs) is None
    # No lexical pre-gate anymore — the classifier runs on EVERY owner musing (that
    # is the whole point); a flat observation just yields no task.
    assert calls["n"] == 1
    assert proposals.list_tasks(db_path=db_path) == []


def test_tap_skips_non_musing(db_path: Path) -> None:
    row = notes.create_note(
        ticker="NU",
        kind="decision",
        body="research this: do margins hold?",
        source="manual",
        db_path=db_path,
    )
    assert proposals.detect_and_create_task(row.id, db_path=db_path, call=_yes) is None


def test_tap_enabled_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEDGER_RESEARCH_TAP", raising=False)
    assert proposals.tap_enabled() is True
    monkeypatch.setenv("LEDGER_RESEARCH_TAP", "0")
    assert proposals.tap_enabled() is False


# ---------------------------------------------------------------------------
# Tap observability — every tap on a real musing leaves one 'tapped' audit row
# (before this, a dormant tap and a broken tap were indistinguishable)
# ---------------------------------------------------------------------------


def _tap_audit_rows(db_path: Path) -> list[tuple[str, str | None, str | None]]:
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        return [
            (str(r[0]), r[1], r[2])
            for r in conn.execute(
                "SELECT channel, detail, purpose FROM capture_audit_log "
                "WHERE action='tapped' ORDER BY id"
            ).fetchall()
        ]
    finally:
        conn.close()


def test_tap_audits_chip_engage_observation_and_error(db_path: Path) -> None:
    # 1. wondering → chip: detail task:<id>, purpose recorded (LLM ran)
    note_id = _land(db_path, "do NU's margins still hold up here?")
    tid = proposals.detect_and_create_task(note_id, db_path=db_path, call=_yes, channel="tray")
    # 2. artifact intent → engage:<mode>, no task (routed to the artifact pipeline)
    art_id = _land(db_path, "curious about the takeaways here, take a look")
    assert (
        proposals.detect_and_create_task(
            art_id,
            db_path=db_path,
            call=lambda _t: {"intent": "brief_artifact", "claim": "takeaways?", "ticker": None},
        )
        is None
    )
    # 3. flat observation → no task, but the classifier DID run (no pre-gate)
    obs_id = _land(db_path, "NU's NPL formation ticked up to 4.5% this quarter")
    assert (
        proposals.detect_and_create_task(
            obs_id, db_path=db_path, call=lambda _t: {"intent": "observation", "claim": ""}
        )
        is None
    )

    # 4. classifier raises → error row, tap still never raises
    def boom(_text: str) -> dict[str, object]:
        raise RuntimeError("llm down")

    boom_id = _land(db_path, "should I look into NU's funding costs?")
    assert proposals.detect_and_create_task(boom_id, db_path=db_path, call=boom) is None

    rows = _tap_audit_rows(db_path)
    assert [(r[1] or "").split(":")[0] for r in rows] == [
        "task",
        "engage",
        "observation",
        "error",
    ]
    assert rows[0] == ("tray", f"task:{tid}", "capture_intent")
    assert rows[1][1] == "engage:brief" and rows[1][2] == "capture_intent"
    assert rows[2][1] == "observation" and rows[2][2] == "capture_intent"
    assert rows[3][1] == "error:RuntimeError"

    from capture.audit import recent_tap_counts

    counts = recent_tap_counts(days=7, db_path=db_path)
    assert counts == {"chip": 1, "engage": 1, "trust_zone": 0, "observation": 1, "error": 1}


# ---------------------------------------------------------------------------
# Red-team wave A: source_note_ids is a real field, parsed from the DB column
# ---------------------------------------------------------------------------


def test_proposal_source_note_ids_round_trip(db_path: Path) -> None:
    """The DB column was populated but never mapped — ResearchProposal had no
    field, so the Ledger card's "from your note" backlink was dead by
    construction. A stored JSON array must round-trip into ints."""
    pid = proposals.create_proposal(
        task_id=None,
        kind="memo",
        ticker="NU",
        title="NU margin question",
        body_md="Margins hold.",
        source_note_ids="[54]",
        db_path=db_path,
    )
    got = proposals.get_proposal(pid, db_path=db_path)
    assert got is not None
    assert got.source_note_ids == [54]
    listed = proposals.list_proposals(status="pending", db_path=db_path)
    assert [p.source_note_ids for p in listed] == [[54]]


def test_proposal_source_note_ids_garbage_degrades_to_empty(db_path: Path) -> None:
    for raw in ("not json", '{"a": 1}', '[1, "x", true]', "[]"):
        pid = proposals.create_proposal(
            task_id=None,
            kind="memo",
            ticker="NU",
            title=f"garbage {raw!r}",
            body_md="body",
            source_note_ids=raw,
            db_path=db_path,
        )
        got = proposals.get_proposal(pid, db_path=db_path)
        assert got is not None
        # Non-list / unparseable -> []; a mixed list keeps only real ints
        # (bools are not note ids).
        assert got.source_note_ids == ([1] if raw.startswith("[1") else [])
