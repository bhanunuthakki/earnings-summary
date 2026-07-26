"""P2.1 Decision Draft: migration 0195, the parser, and confirm/correct/dismiss."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from capture.decision_draft import (  # noqa: E402
    DecisionDraft,
    create_draft_row,
    get_draft,
    get_draft_for_note,
    list_pending_drafts,
    parse_note,
    set_draft_status,
)
from capture.decision_draft_actions import (  # noqa: E402
    DraftActionError,
    confirm_draft,
    confirm_tracker_fill_group,
    correct_draft,
    dismiss_draft,
    dismiss_tracker_fill_group,
)

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


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    db_dir = tmp_path / "data"
    db_dir.mkdir(parents=True, exist_ok=True)
    path = db_dir / "portfolio.db"
    _bootstrap_base_tables(path)
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(cfg, "head")
    return path


def _conn(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    return conn


def _insert_note(db_path: Path, *, body: str, channel: str = "telegram") -> int:
    conn = _conn(db_path)
    try:
        now = datetime.now(UTC).replace(tzinfo=None).isoformat()
        cur = conn.execute(
            "INSERT INTO analyst_notes (user_id, ticker, kind, status, body, source, "
            "context_json, created_at, updated_at) VALUES "
            "('bhanu', NULL, 'musing', 'open', ?, 'capture', ?, ?, ?)",
            (body, json.dumps({"channel": channel, "media_kind": "text"}), now, now),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def _seed_roster(db_path: Path, tickers: list[str]) -> None:
    conn = _conn(db_path)
    for t in tickers:
        conn.execute(
            "INSERT INTO tracked_companies (user_id, ticker, name, list_type) "
            "VALUES ('bhanu', ?, ?, 'portfolio')",
            (t, t + " Inc"),
        )
    conn.commit()
    conn.close()


def test_migration_head_creates_decision_drafts_table(db_path: Path) -> None:
    conn = _conn(db_path)
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='decision_drafts'"
    ).fetchone()
    assert row is not None
    view = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='view' AND name='v_decision_journal'"
    ).fetchone()
    assert view is not None and "source_draft_id" in view["sql"]
    conn.close()


def test_status_check_constraint_rejects_unknown_status(db_path: Path) -> None:
    conn = _conn(db_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO decision_drafts (user_id, source_channel, idempotency_key, "
            "original_text, status, created_at, updated_at) VALUES "
            "('bhanu', 'telegram', 'k1', 'x', 'bogus_status', 'now', 'now')"
        )
    conn.close()


def test_confirmed_requires_decision_id_constraint(db_path: Path) -> None:
    conn = _conn(db_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO decision_drafts (user_id, source_channel, idempotency_key, "
            "original_text, status, created_at, updated_at) VALUES "
            "('bhanu', 'telegram', 'k2', 'x', 'confirmed', 'now', 'now')"
        )
    conn.close()


def test_parse_note_musing_writes_no_row(db_path: Path) -> None:
    note_id = _insert_note(db_path, body="market feels toppy today")
    result = parse_note(
        note_id,
        db_path=db_path,
        call=lambda t, roster: {"intent": "musing", "parse_confidence": 0.9},
    )
    assert result is None
    assert get_draft_for_note(note_id, db_path=db_path) is None


def test_parse_note_executed_change_creates_awaiting_confirmation(db_path: Path) -> None:
    _seed_roster(db_path, ["NU"])
    note_id = _insert_note(db_path, body="added to NU today")
    draft_id = parse_note(
        note_id,
        db_path=db_path,
        call=lambda t, roster: {
            "intent": "executed_change",
            "proposed_ticker": "NU",
            "proposed_action": "add",
            "parse_confidence": 0.95,
        },
    )
    assert draft_id is not None
    row = get_draft(draft_id, db_path=db_path)
    assert row is not None
    assert row.status == "awaiting_confirmation"
    assert row.original_text == "added to NU today"
    assert row.draft is not None
    assert row.draft.proposed_ticker == "NU"
    assert row.draft.proposed_action == "add"


def test_parse_note_is_idempotent_on_reprocess(db_path: Path) -> None:
    _seed_roster(db_path, ["NU"])
    note_id = _insert_note(db_path, body="added to NU today")
    calls = {"n": 0}

    def call(t: str, roster: tuple[str, ...]) -> dict[str, object]:
        calls["n"] += 1
        return {"intent": "executed_change", "proposed_ticker": "NU", "proposed_action": "add"}

    id1 = parse_note(note_id, db_path=db_path, call=call)
    id2 = parse_note(note_id, db_path=db_path, call=call)
    assert id1 == id2
    assert calls["n"] == 1


def test_parse_note_hard_parse_failure_preserves_original_text(db_path: Path) -> None:
    note_id = _insert_note(db_path, body="garbled voice transcription xyz")

    def boom(t: str, roster: tuple[str, ...]) -> dict[str, object]:
        raise RuntimeError("model returned unusable output")

    draft_id = parse_note(note_id, db_path=db_path, call=boom)
    assert draft_id is not None
    row = get_draft(draft_id, db_path=db_path)
    assert row is not None
    assert row.status == "parse_failed"
    assert row.original_text == "garbled voice transcription xyz"
    assert row.draft is None


def test_parse_note_drops_off_roster_ticker(db_path: Path) -> None:
    _seed_roster(db_path, ["NU"])
    note_id = _insert_note(db_path, body="bought some ACME today")
    draft_id = parse_note(
        note_id,
        db_path=db_path,
        call=lambda t, roster: {
            "intent": "executed_change",
            "proposed_ticker": "ACME",
            "proposed_action": "buy",
        },
    )
    row = get_draft(draft_id, db_path=db_path)
    assert row is not None and row.draft is not None
    assert row.draft.proposed_ticker is None


def _make_draft(db_path: Path, draft: DecisionDraft, *, text: str = "x") -> int:
    return create_draft_row(
        source_note_id=None,
        source_channel="telegram",
        source_external_id=None,
        idempotency_key=f"test:{text}:{draft.intent}:{draft.proposed_ticker}",
        original_text=text,
        draft=draft,
        status="awaiting_confirmation",
        db_path=db_path,
    )


def test_confirm_executed_change_creates_owner_decision(db_path: Path) -> None:
    _seed_roster(db_path, ["NU"])
    draft_id = _make_draft(
        db_path,
        DecisionDraft(intent="executed_change", proposed_ticker="NU", proposed_action="add"),
    )
    result = confirm_draft(draft_id, db_path=db_path)
    assert result["decision_id"] is not None
    conn = _conn(db_path)
    row = conn.execute(
        "SELECT decided_by, recommendation_kind, ticker FROM decisions WHERE id = ?",
        (result["decision_id"],),
    ).fetchone()
    conn.close()
    assert row["decided_by"] == "owner"
    assert row["recommendation_kind"] == "add"
    assert row["ticker"] == "NU"
    draft_row = get_draft(draft_id, db_path=db_path)
    assert draft_row is not None and draft_row.status == "confirmed"


def test_confirm_is_idempotent(db_path: Path) -> None:
    _seed_roster(db_path, ["NU"])
    draft_id = _make_draft(
        db_path,
        DecisionDraft(intent="executed_change", proposed_ticker="NU", proposed_action="add"),
    )
    first = confirm_draft(draft_id, db_path=db_path)
    second = confirm_draft(draft_id, db_path=db_path)
    assert second["receipt"] == "already_actioned"
    assert second["decision_id"] == first["decision_id"]
    conn = _conn(db_path)
    n = conn.execute("SELECT COUNT(*) FROM decisions WHERE ticker = 'NU'").fetchone()[0]
    conn.close()
    assert n == 1


def _make_tracker_fill(
    db_path: Path,
    *,
    external_id: str,
    amount_usd: float,
    key: str,
) -> int:
    return create_draft_row(
        source_note_id=None,
        source_channel="tracker",
        source_external_id=external_id,
        idempotency_key=key,
        original_text="Tracker-detected buy fill: NU on 2026-07-24",
        draft=DecisionDraft(
            intent="executed_change",
            proposed_ticker="NU",
            proposed_action="buy",
            proposed_amount_usd=amount_usd,
            parse_confidence=1.0,
        ),
        status="awaiting_confirmation",
        db_path=db_path,
    )


def test_confirm_tracker_fill_group_creates_one_aggregated_decision(db_path: Path) -> None:
    _seed_roster(db_path, ["NU"])
    first_id = _make_tracker_fill(
        db_path, external_id="NU:2026-07-24:buy", amount_usd=100.0, key="tracker:a"
    )
    second_id = _make_tracker_fill(
        db_path, external_id="NU:2026-07-24:buy", amount_usd=250.0, key="tracker:b"
    )

    result = confirm_tracker_fill_group(first_id, db_path=db_path)

    conn = _conn(db_path)
    decisions = conn.execute(
        "SELECT id, size_usd FROM decisions WHERE ticker = 'NU' AND recommendation_kind = 'buy'"
    ).fetchall()
    linked = conn.execute(
        "SELECT id, status, decision_id FROM decision_drafts WHERE id IN (?, ?) ORDER BY id",
        (first_id, second_id),
    ).fetchall()
    conn.close()
    assert len(decisions) == 1
    assert decisions[0]["size_usd"] == 350.0
    assert result["fill_count"] == 2
    assert {row["status"] for row in linked} == {"confirmed"}
    assert {row["decision_id"] for row in linked} == {decisions[0]["id"]}


def test_confirm_tracker_fill_group_is_idempotent(db_path: Path) -> None:
    _seed_roster(db_path, ["NU"])
    first_id = _make_tracker_fill(
        db_path, external_id="NU:2026-07-24:buy", amount_usd=100.0, key="tracker:c"
    )
    second_id = _make_tracker_fill(
        db_path, external_id="NU:2026-07-24:buy", amount_usd=250.0, key="tracker:d"
    )

    first = confirm_tracker_fill_group(first_id, db_path=db_path)
    second = confirm_tracker_fill_group(second_id, db_path=db_path)

    assert second["receipt"] == "already_actioned"
    assert second["decision_id"] == first["decision_id"]
    conn = _conn(db_path)
    count = conn.execute(
        "SELECT COUNT(*) FROM decisions WHERE ticker = 'NU' AND recommendation_kind = 'buy'"
    ).fetchone()[0]
    conn.close()
    assert count == 1


def test_dismiss_tracker_fill_group_preserves_rows_without_decision(db_path: Path) -> None:
    first_id = _make_tracker_fill(
        db_path, external_id="NU:2026-07-24:buy", amount_usd=100.0, key="tracker:e"
    )
    second_id = _make_tracker_fill(
        db_path, external_id="NU:2026-07-24:buy", amount_usd=250.0, key="tracker:f"
    )

    result = dismiss_tracker_fill_group(second_id, db_path=db_path)

    conn = _conn(db_path)
    rows = conn.execute(
        "SELECT status, decision_id FROM decision_drafts WHERE id IN (?, ?)",
        (first_id, second_id),
    ).fetchall()
    conn.close()
    assert result["fill_count"] == 2
    assert {row["status"] for row in rows} == {"dismissed"}
    assert {row["decision_id"] for row in rows} == {None}


def test_dismiss_never_creates_a_decision(db_path: Path) -> None:
    draft_id = _make_draft(
        db_path, DecisionDraft(intent="musing", proposed_ticker=None, proposed_action=None)
    )
    result = dismiss_draft(draft_id, db_path=db_path)
    assert result["receipt"] == "dismissed"
    draft_row = get_draft(draft_id, db_path=db_path)
    assert draft_row is not None and draft_row.status == "dismissed"
    assert draft_row.decision_id is None
    result2 = dismiss_draft(draft_id, db_path=db_path)
    assert result2["receipt"] == "already_actioned"


def test_confirm_with_nothing_actionable_raises(db_path: Path) -> None:
    draft_id = _make_draft(
        db_path, DecisionDraft(intent="rationale", proposed_ticker=None, proposed_action=None)
    )
    with pytest.raises(DraftActionError):
        confirm_draft(draft_id, db_path=db_path)


def test_correct_validates_and_overrides_fields(db_path: Path) -> None:
    _seed_roster(db_path, ["NU", "MELI"])
    draft_id = _make_draft(
        db_path,
        DecisionDraft(intent="executed_change", proposed_ticker="NU", proposed_action="add"),
    )
    result = correct_draft(
        draft_id, {"proposed_ticker": "MELI", "proposed_action": "trim"}, db_path=db_path
    )
    conn = _conn(db_path)
    row = conn.execute(
        "SELECT ticker, recommendation_kind FROM decisions WHERE id = ?",
        (result["decision_id"],),
    ).fetchone()
    conn.close()
    assert row["ticker"] == "MELI"
    assert row["recommendation_kind"] == "trim"
    draft_row = get_draft(draft_id, db_path=db_path)
    assert draft_row is not None and draft_row.status == "corrected"


def test_list_pending_drafts_only_returns_awaiting_confirmation(db_path: Path) -> None:
    d1 = _make_draft(db_path, DecisionDraft(intent="musing", parse_confidence=0.1), text="a")
    set_draft_status(d1, "expired", db_path=db_path)
    d2 = _make_draft(
        db_path,
        DecisionDraft(intent="executed_change", proposed_ticker=None, proposed_action=None),
        text="b",
    )
    pending = list_pending_drafts(db_path=db_path)
    assert [p.id for p in pending] == [d2]


def _insert_card_artifact(db_path: Path, ticker: str) -> int:
    conn = _conn(db_path)
    now = datetime.now(UTC).replace(tzinfo=None).isoformat()
    cur = conn.execute(
        "INSERT INTO llm_artifacts (ticker, scope, purpose, prompt_version, content_json, "
        "content_md, input_sha256, generated_at) VALUES "
        "(?, 'ticker', 'investment_decision_card', 'v1', '{}', 'md', 'sha', ?)",
        (ticker, now),
    )
    artifact_id = int(cur.lastrowid or 0)
    conn.commit()
    conn.close()
    return artifact_id


def test_confirm_disposition_reuses_card_action_core(db_path: Path) -> None:
    _seed_roster(db_path, ["RBRK"])
    artifact_id = _insert_card_artifact(db_path, "RBRK")
    draft_id = _make_draft(
        db_path,
        DecisionDraft(
            intent="disposition",
            proposed_ticker="RBRK",
            proposed_action="pass",
            linked_advice_artifact_id=artifact_id,
        ),
    )
    result = confirm_draft(draft_id, db_path=db_path)
    assert result["receipt"] == "pass_recorded"
    conn = _conn(db_path)
    row = conn.execute(
        "SELECT decided_by, advice_artifact_id FROM decisions WHERE id = ?",
        (result["decision_id"],),
    ).fetchone()
    archived = conn.execute(
        "SELECT archived_at FROM tracked_companies WHERE ticker = 'RBRK'"
    ).fetchone()
    conn.close()
    assert row["decided_by"] == "owner"
    assert row["advice_artifact_id"] == artifact_id
    assert archived["archived_at"] is not None
