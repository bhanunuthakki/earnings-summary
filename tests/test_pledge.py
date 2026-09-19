"""The pre-buy pledge channel + annotation follow-up (W2, grill decision #5)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from research.pledge import (
    annotate_latest_pending,
    build_challenge,
    detect_and_capture_pledge,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def db(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    return migrated_db(tmp_path / "pledge.db")


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


def test_pledge_gate_catches_natural_sell_idioms(db: Path) -> None:
    """The sell-winners-early idioms — the exact pattern the pledge tap exists
    to coach — must pass the regex pre-gate; an imminent sell rarely announces
    itself as 'selling'."""
    phrases = [
        "taking profits on NVO here",
        "lightening up on MELI into the print",
        "lightening NU a touch this week",
        "exiting RBRK this morning",
        "dumping my WIX position today",
        "cutting the position in half on NOW",
        "cutting my NVO position back to 3%",
    ]
    for body in phrases:
        note_id = _land_musing(db, body)
        call, calls = _fake_extract({"ticker": "NVO", "direction": "sell"})
        pledge = detect_and_capture_pledge(note_id, db_path=db, extract_call=call)
        assert calls["n"] == 1, body  # the gate passed → extraction ran
        assert pledge is not None and pledge.direction == "sell", body


def test_pledge_gate_still_skips_company_narrative(db: Path) -> None:
    """Company narrative reusing the same verbs ('exiting the quarter with...',
    'cutting costs') must stay a zero-token miss — the gate exists so a
    non-pledge never burns an extraction call."""
    for body in (
        "RBRK exiting the quarter with $1.3B ARR",
        "NU exiting FY26 with NPLs stable",
        "MELI is cutting costs aggressively in logistics",
        "guide implies WIX exiting Q4 at a higher run-rate",
    ):
        note_id = _land_musing(db, body)
        call, calls = _fake_extract({})
        assert detect_and_capture_pledge(note_id, db_path=db, extract_call=call) is None, body
        assert calls["n"] == 0, body


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
