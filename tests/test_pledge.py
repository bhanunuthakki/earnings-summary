"""The pre-buy pledge channel + annotation follow-up (W2, grill decision #5)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from research.pledge import (
    annotate_latest_pending,
    build_challenge,
    detect_and_capture_pledge,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0129_commitment_scan_log"
HEAD = "0130_owner_decision_extension"

_PRE_DDL = """
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    recommendation_kind VARCHAR(32) NOT NULL,
    conviction VARCHAR(16),
    source_artifact_id INTEGER,
    source_memo_id INTEGER,
    source_dismissal_id INTEGER,
    rationale_excerpt TEXT,
    user_notes TEXT,
    made_at DATETIME NOT NULL,
    outcome_label VARCHAR(16),
    created_at DATETIME NOT NULL,
    CONSTRAINT ck_decisions_source_present CHECK (
        source_artifact_id IS NOT NULL OR source_memo_id IS NOT NULL
        OR recommendation_kind = 'avoid')
);
CREATE TABLE tenants (id TEXT PRIMARY KEY);
INSERT INTO tenants (id) VALUES ('bhanu');
CREATE TABLE analyst_notes (
    id INTEGER NOT NULL,
    user_id TEXT DEFAULT 'bhanu' NOT NULL,
    ticker TEXT,
    kind TEXT NOT NULL,
    status TEXT DEFAULT 'open' NOT NULL,
    body TEXT NOT NULL,
    anchor_type TEXT,
    anchor_key TEXT,
    source TEXT NOT NULL,
    source_ref TEXT,
    supersedes_id INTEGER,
    resolution_note TEXT,
    context_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT,
    decision_id INTEGER,
    position_entry_id INTEGER,
    link_auto_resolve INTEGER DEFAULT 0 NOT NULL,
    fact_ref TEXT,
    PRIMARY KEY (id),
    CONSTRAINT ck_analyst_notes_kind CHECK (kind IN
        ('question','decision','watch','assumption','observation','musing'))
);
CREATE VIRTUAL TABLE analyst_notes_fts USING fts5(
    body, content='analyst_notes', content_rowid='id');
CREATE TABLE capture_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER,
    note_id INTEGER,
    channel TEXT NOT NULL,
    utterance_summary TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT,
    purpose TEXT,
    model TEXT,
    prompt_sha256 TEXT,
    run_id TEXT,
    cost_usd REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
"""


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "pledge.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_PRE_DDL)
        conn.commit()
    finally:
        conn.close()
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, HEAD)
    return path


def _land_musing(db: Path, body: str) -> int:
    conn = sqlite3.connect(str(db))
    try:
        cur = conn.execute(
            "INSERT INTO analyst_notes (kind, body, source, created_at, updated_at) "
            "VALUES ('musing', ?, 'capture', '2026-07-02T05:00:00', '2026-07-02T05:00:00')",
            (body,),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def _fake_extract(payload: dict[str, object]):
    calls = {"n": 0}

    def call(_text: str) -> dict[str, object]:
        calls["n"] += 1
        return payload

    return call, calls


def test_non_pledge_never_burns_tokens(db: Path) -> None:
    note_id = _land_musing(db, "NU's NPL formation looked fine this quarter")
    call, calls = _fake_extract({})
    assert detect_and_capture_pledge(note_id, db_path=db, extract_call=call) is None
    assert calls["n"] == 0  # regex pre-gate short-circuits


def test_pledge_captures_decision_and_challenge_asks_for_missing(db: Path) -> None:
    note_id = _land_musing(db, "buying NVO ~$30k on the washout, high conviction")
    call, calls = _fake_extract({"ticker": "NVO", "direction": "buy", "conviction": "high"})
    pledge = detect_and_capture_pledge(note_id, channel="telegram", db_path=db, extract_call=call)
    assert pledge is not None and calls["n"] == 1
    assert pledge.ticker == "NVO" and pledge.direction == "buy"
    assert pledge.missing == ("falsifier",)

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM decisions WHERE id=?", (pledge.decision_id,)).fetchone()
        assert row["decided_by"] == "owner"
        assert row["recommendation_kind"] == "initiate"
        assert row["size_usd"] == 30000.0
        assert row["conviction"] == "high" and row["falsifier"] is None
        linked = conn.execute(
            "SELECT decision_id FROM analyst_notes WHERE id=?", (note_id,)
        ).fetchone()[0]
        assert linked == pledge.decision_id
        audit = conn.execute(
            "SELECT detail FROM capture_audit_log WHERE action='captured' ORDER BY id DESC"
        ).fetchone()[0]
        assert audit == f"pledge:decision:{pledge.decision_id}"
    finally:
        conn.close()

    challenge = build_challenge(pledge)  # no repo_root → degrades to the test alone
    assert "catalyst test" in challenge.lower()
    assert "falsifier" in challenge
    assert "Current read" not in challenge

    # Idempotent per note: a re-tap on the same musing captures nothing new
    assert detect_and_capture_pledge(note_id, db_path=db, extract_call=call) is None


def test_build_challenge_embeds_the_plain_renderer_when_repo_root_given(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-analysis embedded in the challenge is the PLAIN Telegram
    variant, not the Markdown chat one — send_message never sets parse_mode,
    so a Markdown body would arrive as a raw asterisk/backtick wall."""
    note_id = _land_musing(db, "buying NVO ~$30k, high conviction, falsifier: none yet")
    call, _ = _fake_extract(
        {"ticker": "NVO", "direction": "buy", "conviction": "high", "falsifier": "none yet"}
    )
    pledge = detect_and_capture_pledge(note_id, db_path=db, extract_call=call)
    assert pledge is not None

    seen: list[object] = []

    def _fake_pre_analysis(repo_root: object, ticker: str, *, db_path: object = None) -> object:
        return object()

    def _fake_render_plain(pre: object) -> str:
        seen.append(pre)
        return "NVO - position review (deterministic read)"

    monkeypatch.setattr(
        "advisor.position_review.build_pre_analysis", _fake_pre_analysis, raising=False
    )
    monkeypatch.setattr(
        "advisor.position_review.render_pre_analysis_plain", _fake_render_plain, raising=False
    )
    challenge = build_challenge(pledge, repo_root=Path("."), db_path=db)
    assert "Current read:" in challenge
    assert "NVO - position review" in challenge
    assert "**" not in challenge
    assert seen  # the plain renderer was actually called


def test_annotation_fills_only_nulls_write_once(db: Path) -> None:
    note_id = _land_musing(db, "adding to NU here")
    call, _ = _fake_extract({"ticker": "NU", "direction": "add", "conviction": None})
    pledge = detect_and_capture_pledge(note_id, db_path=db, extract_call=call)
    assert pledge is not None and set(pledge.missing) == {"conviction", "falsifier"}

    ann_call, _ = _fake_extract({"conviction": "high", "falsifier": "15-90d NPL >5% for 2Q"})
    did = annotate_latest_pending(
        "high conviction, falsifier: 15-90d NPL >5% for 2Q",
        db_path=db,
        extract_call=ann_call,
    )
    assert did == pledge.decision_id

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM decisions WHERE id=?", (did,)).fetchone()
        assert (row["conviction"], row["falsifier"]) == ("high", "15-90d NPL >5% for 2Q")
    finally:
        conn.close()

    # WRITE-ONCE: both fields set → a second annotation has nothing to fill
    again_call, _again = _fake_extract({"conviction": "low", "falsifier": "other"})
    assert (
        annotate_latest_pending("low conviction now", db_path=db, extract_call=again_call) is None
    )
    conn = sqlite3.connect(str(db))
    try:
        assert (
            conn.execute("SELECT conviction FROM decisions WHERE id=?", (did,)).fetchone()[0]
            == "high"
        )
    finally:
        conn.close()


def test_annotation_pre_gates(db: Path) -> None:
    # No pending stub → no extraction even for annotation-shaped text
    call, calls = _fake_extract({"conviction": "high"})
    assert annotate_latest_pending("high conviction", db_path=db, extract_call=call) is None
    assert calls["n"] == 0
    # Non-annotation text → regex pre-gate, zero calls
    note_id = _land_musing(db, "buying NU")
    pcall, _ = _fake_extract({"ticker": "NU", "direction": "buy"})
    detect_and_capture_pledge(note_id, db_path=db, extract_call=pcall)
    ncall, ncalls = _fake_extract({})
    assert annotate_latest_pending("the weather is nice", db_path=db, extract_call=ncall) is None
    assert ncalls["n"] == 0
