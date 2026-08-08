"""P2.1 Decision Draft: migration 0195, the parser, and confirm/correct/dismiss."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

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
    correct_tracker_fill_group,
    dismiss_draft,
    dismiss_tracker_fill_group,
)
from decision_extractor import reconcile_decision_actions  # noqa: E402
from integrations.portfolio_tracker_client import LivePortfolio, LiveTransaction  # noqa: E402

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
def db_path(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    db_dir = tmp_path / "data"
    db_dir.mkdir(parents=True, exist_ok=True)
    path = db_dir / "portfolio.db"
    return migrated_db(path)


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


def test_confirm_using_confirmed_tracker_id_processes_late_sibling(db_path: Path) -> None:
    _seed_roster(db_path, ["NU"])
    first_id = _make_tracker_fill(
        db_path,
        external_id="NU:2026-07-24:buy",
        amount_usd=100.0,
        key="tracker:stale-confirm-a",
    )
    first = confirm_tracker_fill_group(first_id, db_path=db_path)
    late_id = _make_tracker_fill(
        db_path,
        external_id="NU:2026-07-24:buy",
        amount_usd=250.0,
        key="tracker:stale-confirm-b",
    )

    result = confirm_draft(first_id, db_path=db_path)

    conn = _conn(db_path)
    decision = conn.execute(
        "SELECT size_usd FROM decisions WHERE id = ?",
        (first["decision_id"],),
    ).fetchone()
    late = conn.execute(
        "SELECT status, decision_id FROM decision_drafts WHERE id = ?",
        (late_id,),
    ).fetchone()
    conn.close()
    assert result["receipt"] == "decision_aggregate_corrected"
    assert decision["size_usd"] == 350.0
    assert late["status"] == "confirmed"
    assert late["decision_id"] == first["decision_id"]


def test_correct_using_confirmed_tracker_id_processes_late_sibling(db_path: Path) -> None:
    _seed_roster(db_path, ["NU"])
    first_id = _make_tracker_fill(
        db_path,
        external_id="NU:2026-07-24:buy",
        amount_usd=100.0,
        key="tracker:stale-correct-a",
    )
    first = confirm_tracker_fill_group(first_id, db_path=db_path)
    late_id = _make_tracker_fill(
        db_path,
        external_id="NU:2026-07-24:buy",
        amount_usd=250.0,
        key="tracker:stale-correct-b",
    )

    result = correct_draft(
        first_id,
        {"proposed_amount_usd": 300.0},
        db_path=db_path,
    )

    conn = _conn(db_path)
    decision = conn.execute(
        "SELECT size_usd FROM decisions WHERE id = ?",
        (first["decision_id"],),
    ).fetchone()
    late = conn.execute(
        "SELECT status, decision_id FROM decision_drafts WHERE id = ?",
        (late_id,),
    ).fetchone()
    conn.close()
    assert result["receipt"] == "tracker_group_corrected"
    assert decision["size_usd"] == 300.0
    assert late["status"] == "corrected"
    assert late["decision_id"] == first["decision_id"]


def _race_actions(
    monkeypatch: pytest.MonkeyPatch,
    action_calls: tuple[Callable[[], dict[str, object]], Callable[[], dict[str, object]]],
) -> list[dict[str, object]]:
    """Release two action requests together before either reads draft state."""
    import capture.decision_draft_actions as action_module

    real_open = action_module.open_conn
    barrier = threading.Barrier(2)
    local = threading.local()

    def synchronized_open(db: Path | str | None) -> sqlite3.Connection:
        conn = real_open(db)
        if not getattr(local, "initial_open_complete", False):
            local.initial_open_complete = True
            barrier.wait(timeout=10)
        return conn

    monkeypatch.setattr(action_module, "open_conn", synchronized_open)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(action) for action in action_calls]
        return [future.result(timeout=15) for future in futures]


def _race_action(
    monkeypatch: pytest.MonkeyPatch,
    action: Callable[[], dict[str, object]],
) -> list[dict[str, object]]:
    return _race_actions(monkeypatch, (action, action))


def _race_reconcile_and_action(
    monkeypatch: pytest.MonkeyPatch,
    action: Callable[[], dict[str, object]],
    reconcile: Callable[[], dict[str, int]],
) -> tuple[dict[str, object], dict[str, int]]:
    import capture.decision_draft_actions as action_module
    import decision_extractor as extractor_module

    real_action_open = action_module.open_conn
    real_reconcile_open = extractor_module._open
    barrier = threading.Barrier(2)
    action_local = threading.local()
    reconcile_local = threading.local()

    def synchronized_action_open(db: Path | str | None) -> sqlite3.Connection:
        conn = real_action_open(db)
        if not getattr(action_local, "initial_open_complete", False):
            action_local.initial_open_complete = True
            barrier.wait(timeout=10)
        return conn

    def synchronized_reconcile_open(db: Path | str | None) -> sqlite3.Connection | None:
        conn = real_reconcile_open(db)
        if not getattr(reconcile_local, "initial_open_complete", False):
            reconcile_local.initial_open_complete = True
            barrier.wait(timeout=10)
        return conn

    monkeypatch.setattr(action_module, "open_conn", synchronized_action_open)
    monkeypatch.setattr(extractor_module, "_open", synchronized_reconcile_open)
    with ThreadPoolExecutor(max_workers=2) as executor:
        action_future = executor.submit(action)
        reconcile_future = executor.submit(reconcile)
        return action_future.result(timeout=15), reconcile_future.result(timeout=15)


def test_confirm_draft_serializes_two_requests(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_roster(db_path, ["NU"])
    draft_id = _make_draft(
        db_path,
        DecisionDraft(
            intent="executed_change",
            proposed_ticker="NU",
            proposed_action="buy",
            proposed_amount_usd=100.0,
        ),
    )

    results = _race_action(monkeypatch, lambda: confirm_draft(draft_id, db_path=db_path))

    conn = _conn(db_path)
    decisions = conn.execute(
        "SELECT id FROM decisions WHERE ticker = 'NU' AND recommendation_kind = 'buy'"
    ).fetchall()
    conn.close()
    assert len(decisions) == 1
    assert {result["decision_id"] for result in results} == {decisions[0]["id"]}
    assert {result["receipt"] for result in results} == {
        "decision_recorded",
        "already_actioned",
    }


def test_confirm_tracker_group_serializes_two_requests(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_roster(db_path, ["NU"])
    draft_id = _make_tracker_fill(
        db_path,
        external_id="NU:2026-07-24:buy",
        amount_usd=100.0,
        key="tracker:race-initial",
    )

    results = _race_action(
        monkeypatch,
        lambda: confirm_tracker_fill_group(draft_id, db_path=db_path),
    )

    conn = _conn(db_path)
    decisions = conn.execute(
        "SELECT id FROM decisions WHERE ticker = 'NU' AND recommendation_kind = 'buy'"
    ).fetchall()
    conn.close()
    assert len(decisions) == 1
    assert {result["decision_id"] for result in results} == {decisions[0]["id"]}
    assert {result["receipt"] for result in results} == {
        "decision_recorded",
        "already_actioned",
    }


def test_generic_and_group_confirm_share_initial_tracker_action(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_roster(db_path, ["NU"])
    draft_id = _make_tracker_fill(
        db_path,
        external_id="NU:2026-07-24:buy",
        amount_usd=100.0,
        key="tracker:race-mixed-initial",
    )

    results = _race_actions(
        monkeypatch,
        (
            lambda: confirm_draft(draft_id, db_path=db_path),
            lambda: confirm_tracker_fill_group(draft_id, db_path=db_path),
        ),
    )

    conn = _conn(db_path)
    decisions = conn.execute(
        "SELECT id FROM decisions WHERE ticker = 'NU' AND recommendation_kind = 'buy'"
    ).fetchall()
    conn.close()
    assert len(decisions) == 1
    assert {result["decision_id"] for result in results} == {decisions[0]["id"]}
    assert {result["receipt"] for result in results} == {
        "decision_recorded",
        "already_actioned",
    }


def test_confirm_tracker_fill_group_corrects_aggregate_for_late_fill(db_path: Path) -> None:
    _seed_roster(db_path, ["NU"])
    first_id = _make_tracker_fill(
        db_path, external_id="NU:2026-07-24:buy", amount_usd=100.0, key="tracker:late-a"
    )
    first = confirm_tracker_fill_group(first_id, db_path=db_path)
    late_id = _make_tracker_fill(
        db_path, external_id="NU:2026-07-24:buy", amount_usd=250.0, key="tracker:late-b"
    )

    corrected = confirm_tracker_fill_group(late_id, db_path=db_path)

    conn = _conn(db_path)
    decision = conn.execute(
        "SELECT size_usd, user_notes FROM decisions WHERE id = ?",
        (first["decision_id"],),
    ).fetchone()
    linked = conn.execute(
        "SELECT status, decision_id FROM decision_drafts WHERE id IN (?, ?) ORDER BY id",
        (first_id, late_id),
    ).fetchall()
    conn.close()
    assert corrected["receipt"] == "decision_aggregate_corrected"
    assert corrected["added_fill_count"] == 1
    assert decision["size_usd"] == 350.0
    assert "attached 1 late fill(s)" in decision["user_notes"]
    assert {row["status"] for row in linked} == {"confirmed"}
    assert {row["decision_id"] for row in linked} == {first["decision_id"]}


def test_confirm_late_tracker_fill_serializes_two_requests(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_roster(db_path, ["NU"])
    first_id = _make_tracker_fill(
        db_path,
        external_id="NU:2026-07-24:buy",
        amount_usd=100.0,
        key="tracker:race-late-a",
    )
    first = confirm_tracker_fill_group(first_id, db_path=db_path)
    late_id = _make_tracker_fill(
        db_path,
        external_id="NU:2026-07-24:buy",
        amount_usd=250.0,
        key="tracker:race-late-b",
    )

    results = _race_action(
        monkeypatch,
        lambda: confirm_tracker_fill_group(late_id, db_path=db_path),
    )

    conn = _conn(db_path)
    decisions = conn.execute(
        "SELECT id, size_usd FROM decisions WHERE ticker = 'NU' AND recommendation_kind = 'buy'"
    ).fetchall()
    conn.close()
    assert len(decisions) == 1
    assert decisions[0]["id"] == first["decision_id"]
    assert decisions[0]["size_usd"] == 350.0
    assert {result["decision_id"] for result in results} == {first["decision_id"]}
    assert {result["receipt"] for result in results} == {
        "decision_aggregate_corrected",
        "already_actioned",
    }


def test_generic_and_group_confirm_share_late_tracker_action(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_roster(db_path, ["NU"])
    first_id = _make_tracker_fill(
        db_path,
        external_id="NU:2026-07-24:buy",
        amount_usd=100.0,
        key="tracker:race-mixed-late-a",
    )
    first = confirm_tracker_fill_group(first_id, db_path=db_path)
    late_id = _make_tracker_fill(
        db_path,
        external_id="NU:2026-07-24:buy",
        amount_usd=250.0,
        key="tracker:race-mixed-late-b",
    )

    results = _race_actions(
        monkeypatch,
        (
            lambda: confirm_draft(late_id, db_path=db_path),
            lambda: confirm_tracker_fill_group(late_id, db_path=db_path),
        ),
    )

    conn = _conn(db_path)
    decisions = conn.execute(
        "SELECT id, size_usd FROM decisions WHERE ticker = 'NU' AND recommendation_kind = 'buy'"
    ).fetchall()
    conn.close()
    assert len(decisions) == 1
    assert decisions[0]["id"] == first["decision_id"]
    assert decisions[0]["size_usd"] == 350.0
    assert {result["decision_id"] for result in results} == {first["decision_id"]}
    assert {result["receipt"] for result in results} == {
        "decision_aggregate_corrected",
        "already_actioned",
    }


def test_generic_correct_and_group_confirm_share_initial_tracker_action(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_roster(db_path, ["NU"])
    first_id = _make_tracker_fill(
        db_path,
        external_id="NU:2026-07-24:buy",
        amount_usd=100.0,
        key="tracker:race-correct-initial-a",
    )
    _make_tracker_fill(
        db_path,
        external_id="NU:2026-07-24:buy",
        amount_usd=250.0,
        key="tracker:race-correct-initial-b",
    )

    results = _race_actions(
        monkeypatch,
        (
            lambda: correct_draft(
                first_id,
                {"proposed_amount_usd": 300.0},
                db_path=db_path,
            ),
            lambda: confirm_tracker_fill_group(first_id, db_path=db_path),
        ),
    )

    conn = _conn(db_path)
    decisions = conn.execute(
        "SELECT id, size_usd FROM decisions WHERE ticker = 'NU' AND recommendation_kind = 'buy'"
    ).fetchall()
    drafts = conn.execute(
        "SELECT status, decision_id FROM decision_drafts "
        "WHERE source_external_id = 'NU:2026-07-24:buy'"
    ).fetchall()
    conn.close()
    assert len(decisions) == 1
    assert decisions[0]["size_usd"] in {300.0, 350.0}
    assert {row["decision_id"] for row in drafts} == {decisions[0]["id"]}
    assert {row["status"] for row in drafts} in ({"corrected"}, {"confirmed"})
    assert {result["decision_id"] for result in results} == {decisions[0]["id"]}


def test_generic_correct_and_group_confirm_share_late_tracker_action(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_roster(db_path, ["NU"])
    first_id = _make_tracker_fill(
        db_path,
        external_id="NU:2026-07-24:buy",
        amount_usd=100.0,
        key="tracker:race-correct-late-a",
    )
    first = confirm_tracker_fill_group(first_id, db_path=db_path)
    late_id = _make_tracker_fill(
        db_path,
        external_id="NU:2026-07-24:buy",
        amount_usd=250.0,
        key="tracker:race-correct-late-b",
    )

    results = _race_actions(
        monkeypatch,
        (
            lambda: correct_draft(
                late_id,
                {"proposed_amount_usd": 300.0},
                db_path=db_path,
            ),
            lambda: confirm_tracker_fill_group(late_id, db_path=db_path),
        ),
    )

    conn = _conn(db_path)
    decisions = conn.execute(
        "SELECT id, size_usd FROM decisions WHERE ticker = 'NU' AND recommendation_kind = 'buy'"
    ).fetchall()
    late = conn.execute(
        "SELECT status, decision_id FROM decision_drafts WHERE id = ?",
        (late_id,),
    ).fetchone()
    conn.close()
    assert len(decisions) == 1
    assert decisions[0]["id"] == first["decision_id"]
    assert decisions[0]["size_usd"] in {300.0, 350.0}
    assert decisions[0]["size_usd"] != 550.0
    assert late["status"] in {"corrected", "confirmed"}
    assert late["decision_id"] == first["decision_id"]
    assert {result["decision_id"] for result in results} == {first["decision_id"]}


def test_correct_tracker_group_defaults_to_current_decision_plus_pending(
    db_path: Path,
) -> None:
    _seed_roster(db_path, ["NU"])
    first_id = _make_tracker_fill(
        db_path,
        external_id="NU:2026-07-24:buy",
        amount_usd=100.0,
        key="tracker:correct-default-a",
    )
    first = confirm_tracker_fill_group(first_id, db_path=db_path)
    late_id = _make_tracker_fill(
        db_path,
        external_id="NU:2026-07-24:buy",
        amount_usd=250.0,
        key="tracker:correct-default-b",
    )

    result = correct_draft(
        late_id,
        {"proposed_rationale": "Keep the computed aggregate."},
        db_path=db_path,
    )

    conn = _conn(db_path)
    decision = conn.execute(
        "SELECT size_usd FROM decisions WHERE id = ?",
        (first["decision_id"],),
    ).fetchone()
    conn.close()
    assert result["receipt"] == "tracker_group_corrected"
    assert result["decision_id"] == first["decision_id"]
    assert decision["size_usd"] == 350.0


def test_correct_tracker_group_updates_shared_decision_and_future_late_fill(
    db_path: Path,
) -> None:
    _seed_roster(db_path, ["NU"])
    first_id = _make_tracker_fill(
        db_path,
        external_id="NU:2026-07-24:buy",
        amount_usd=100.0,
        key="tracker:correct-group-a",
    )
    first = confirm_tracker_fill_group(first_id, db_path=db_path)
    late_id = _make_tracker_fill(
        db_path,
        external_id="NU:2026-07-24:buy",
        amount_usd=250.0,
        key="tracker:correct-group-b",
    )

    corrected = correct_tracker_fill_group(
        late_id,
        {
            "proposed_ticker": "NU",
            "proposed_action": "buy",
            "proposed_amount_usd": 300.0,
            "proposed_rationale": "Broker total corrected.",
        },
        db_path=db_path,
    )
    newest_id = _make_tracker_fill(
        db_path,
        external_id="NU:2026-07-24:buy",
        amount_usd=50.0,
        key="tracker:correct-group-c",
    )
    after_late = confirm_tracker_fill_group(newest_id, db_path=db_path)

    conn = _conn(db_path)
    decisions = conn.execute(
        "SELECT id, size_usd, rationale_excerpt FROM decisions "
        "WHERE ticker = 'NU' AND recommendation_kind = 'buy'"
    ).fetchall()
    drafts = conn.execute(
        "SELECT status, decision_id FROM decision_drafts "
        "WHERE source_external_id = 'NU:2026-07-24:buy' ORDER BY id"
    ).fetchall()
    conn.close()
    assert len(decisions) == 1
    assert decisions[0]["id"] == first["decision_id"]
    assert decisions[0]["size_usd"] == 350.0
    assert decisions[0]["rationale_excerpt"] == "Broker total corrected."
    assert corrected["receipt"] == "tracker_group_corrected"
    assert after_late["receipt"] == "decision_aggregate_corrected"
    assert {row["decision_id"] for row in drafts} == {first["decision_id"]}
    assert [row["status"] for row in drafts] == ["confirmed", "corrected", "confirmed"]


def test_correct_tracker_group_propagates_categories_but_preserves_fill_amounts(
    db_path: Path,
) -> None:
    _seed_roster(db_path, ["NU", "MELI"])
    first_id = _make_tracker_fill(
        db_path,
        external_id="NU:2026-07-24:buy",
        amount_usd=100.0,
        key="tracker:correct-category-a",
    )
    first = confirm_tracker_fill_group(first_id, db_path=db_path)
    late_id = _make_tracker_fill(
        db_path,
        external_id="NU:2026-07-24:buy",
        amount_usd=250.0,
        key="tracker:correct-category-b",
    )

    correct_tracker_fill_group(
        late_id,
        {
            "proposed_ticker": "MELI",
            "proposed_action": "sell",
            "proposed_amount_usd": 300.0,
        },
        db_path=db_path,
    )
    newest_id = _make_tracker_fill(
        db_path,
        external_id="NU:2026-07-24:buy",
        amount_usd=50.0,
        key="tracker:correct-category-c",
    )
    confirm_tracker_fill_group(newest_id, db_path=db_path)

    conn = _conn(db_path)
    decision = conn.execute(
        "SELECT ticker, recommendation_kind, size_usd FROM decisions WHERE id = ?",
        (first["decision_id"],),
    ).fetchone()
    raw_drafts = conn.execute(
        "SELECT draft_json FROM decision_drafts "
        "WHERE source_external_id = 'NU:2026-07-24:buy' ORDER BY id"
    ).fetchall()
    conn.close()
    decoded = [json.loads(row["draft_json"]) for row in raw_drafts]
    assert decision["ticker"] == "MELI"
    assert decision["recommendation_kind"] == "sell"
    assert decision["size_usd"] == 350.0
    assert {row["proposed_ticker"] for row in decoded} == {"MELI"}
    assert {row["proposed_action"] for row in decoded} == {"sell"}
    assert [row["proposed_amount_usd"] for row in decoded] == [100.0, 250.0, 50.0]


def _provider_fill(*, day: str, transaction_id: str, amount_usd: float) -> LiveTransaction:
    return LiveTransaction(
        date=day,
        ticker="NU",
        name=None,
        type="buy",
        subtype=None,
        quantity=1.0,
        amount=amount_usd,
        account_name="x",
        transaction_id=transaction_id,
    )


def test_reconcile_late_provider_fill_reaches_group_aggregate(db_path: Path) -> None:
    _seed_roster(db_path, ["NU"])
    now = datetime.now(UTC)
    day = now.date().isoformat()
    first_fill = _provider_fill(day=day, transaction_id="txn-a", amount_usd=100.0)
    reconcile_decision_actions(
        db_path=db_path,
        portfolio=LivePortfolio(
            available=True,
            api_url="http://tracker.test",
            transactions=[first_fill],
        ),
        now=now,
    )
    conn = _conn(db_path)
    first_id = int(
        conn.execute(
            "SELECT id FROM decision_drafts WHERE source_provider_id = 'txn-a'"
        ).fetchone()[0]
    )
    conn.close()
    first = confirm_tracker_fill_group(first_id, db_path=db_path)

    late_fill = _provider_fill(day=day, transaction_id="txn-b", amount_usd=250.0)
    reconcile_decision_actions(
        db_path=db_path,
        portfolio=LivePortfolio(
            available=True,
            api_url="http://tracker.test",
            transactions=[first_fill, late_fill],
        ),
        now=now,
    )

    conn = _conn(db_path)
    provider_rows = conn.execute(
        "SELECT id, source_provider_id FROM decision_drafts "
        "WHERE source_channel = 'tracker' ORDER BY id"
    ).fetchall()
    conn.close()
    assert {row["source_provider_id"] for row in provider_rows} == {"txn-a", "txn-b"}
    late_id = next(int(row["id"]) for row in provider_rows if row["source_provider_id"] == "txn-b")

    corrected = confirm_tracker_fill_group(late_id, db_path=db_path)

    conn = _conn(db_path)
    decision = conn.execute(
        "SELECT size_usd FROM decisions WHERE id = ?",
        (first["decision_id"],),
    ).fetchone()
    conn.close()
    assert corrected["receipt"] == "decision_aggregate_corrected"
    assert decision["size_usd"] == 350.0


def test_reconcile_promotes_confirmed_legacy_over_expired_provider_twin(
    db_path: Path,
) -> None:
    _seed_roster(db_path, ["NU"])
    day = "2026-07-24"
    legacy_fill = LiveTransaction(
        date=day,
        ticker="NU",
        name=None,
        type="buy",
        subtype=None,
        quantity=1.0,
        amount=100.0,
        account_name="Brokerage",
    )
    reconcile_decision_actions(
        db_path=db_path,
        portfolio=LivePortfolio(
            available=True,
            api_url="http://tracker.test",
            transactions=[legacy_fill],
        ),
    )
    conn = _conn(db_path)
    legacy_id = int(
        conn.execute("SELECT id FROM decision_drafts WHERE source_channel = 'tracker'").fetchone()[
            0
        ]
    )
    conn.close()
    confirmed = confirm_tracker_fill_group(legacy_id, db_path=db_path)
    legacy_row = get_draft(legacy_id, db_path=db_path)
    assert legacy_row is not None and legacy_row.draft is not None

    provider_key = "tracker-id:" + hashlib.sha256(b"id|txn-a").hexdigest()[:20]
    provider_id = create_draft_row(
        source_note_id=None,
        source_channel="tracker",
        source_external_id=legacy_row.source_external_id,
        source_provider_id="txn-a",
        idempotency_key=provider_key,
        original_text=legacy_row.original_text,
        draft=legacy_row.draft,
        status="expired",
        db_path=db_path,
    )
    provider_fill = _provider_fill(day=day, transaction_id="txn-a", amount_usd=100.0)
    reconcile_decision_actions(
        db_path=db_path,
        portfolio=LivePortfolio(
            available=True,
            api_url="http://tracker.test",
            transactions=[provider_fill],
        ),
    )

    provider_row = get_draft(provider_id, db_path=db_path)
    archived_legacy = get_draft(legacy_id, db_path=db_path)
    assert provider_row is not None
    assert provider_row.status == "confirmed"
    assert provider_row.decision_id == confirmed["decision_id"]
    assert archived_legacy is not None and archived_legacy.status == "expired"

    late_fill = _provider_fill(day=day, transaction_id="txn-b", amount_usd=50.0)
    reconcile_decision_actions(
        db_path=db_path,
        portfolio=LivePortfolio(
            available=True,
            api_url="http://tracker.test",
            transactions=[provider_fill, late_fill],
        ),
    )
    conn = _conn(db_path)
    late_id = int(
        conn.execute(
            "SELECT id FROM decision_drafts WHERE source_provider_id = 'txn-b'"
        ).fetchone()[0]
    )
    conn.close()
    result = confirm_tracker_fill_group(late_id, db_path=db_path)

    conn = _conn(db_path)
    decisions = conn.execute(
        "SELECT id, size_usd FROM decisions WHERE ticker = 'NU' AND recommendation_kind = 'buy'"
    ).fetchall()
    conn.close()
    assert len(decisions) == 1
    assert decisions[0]["id"] == confirmed["decision_id"]
    assert decisions[0]["size_usd"] == 150.0
    assert result["decision_id"] == confirmed["decision_id"]


def test_reconcile_and_confirm_never_orphan_tracker_decision(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_roster(db_path, ["NU"])
    day = "2026-07-24"
    legacy_fill = LiveTransaction(
        date=day,
        ticker="NU",
        name=None,
        type="buy",
        subtype=None,
        quantity=1.0,
        amount=100.0,
        account_name="Brokerage",
    )
    reconcile_decision_actions(
        db_path=db_path,
        portfolio=LivePortfolio(
            available=True,
            api_url="http://tracker.test",
            transactions=[legacy_fill],
        ),
    )
    conn = _conn(db_path)
    legacy_id = int(
        conn.execute("SELECT id FROM decision_drafts WHERE source_channel = 'tracker'").fetchone()[
            0
        ]
    )
    conn.close()
    dismiss_tracker_fill_group(legacy_id, db_path=db_path)
    legacy_row = get_draft(legacy_id, db_path=db_path)
    assert legacy_row is not None and legacy_row.draft is not None

    provider_id = create_draft_row(
        source_note_id=None,
        source_channel="tracker",
        source_external_id=legacy_row.source_external_id,
        source_provider_id="txn-a",
        idempotency_key="tracker-id:" + hashlib.sha256(b"id|txn-a").hexdigest()[:20],
        original_text=legacy_row.original_text,
        draft=legacy_row.draft,
        status="awaiting_confirmation",
        db_path=db_path,
    )
    provider_fill = _provider_fill(day=day, transaction_id="txn-a", amount_usd=100.0)
    action_result, _ = _race_reconcile_and_action(
        monkeypatch,
        lambda: confirm_tracker_fill_group(provider_id, db_path=db_path),
        lambda: reconcile_decision_actions(
            db_path=db_path,
            portfolio=LivePortfolio(
                available=True,
                api_url="http://tracker.test",
                transactions=[provider_fill],
            ),
        ),
    )

    provider_row = get_draft(provider_id, db_path=db_path)
    archived_legacy = get_draft(legacy_id, db_path=db_path)
    conn = _conn(db_path)
    decisions = conn.execute(
        "SELECT id FROM decisions WHERE ticker = 'NU' AND recommendation_kind = 'buy'"
    ).fetchall()
    conn.close()
    assert provider_row is not None
    assert archived_legacy is not None and archived_legacy.status == "expired"
    if decisions:
        assert len(decisions) == 1
        assert provider_row.status == "confirmed"
        assert provider_row.decision_id == decisions[0]["id"]
        assert action_result["decision_id"] == decisions[0]["id"]
    else:
        assert provider_row.status == "dismissed"
        assert provider_row.decision_id is None
        assert action_result["receipt"] == "already_actioned"


def test_tracker_group_confirmation_rolls_back_orphan_decision(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_roster(db_path, ["NU"])
    draft_id = _make_tracker_fill(
        db_path, external_id="NU:2026-07-24:buy", amount_usd=100.0, key="tracker:atomic"
    )
    import capture.decision_draft_actions as actions

    original_write = cast("Callable[..., int]", getattr(actions, "_write_owner_decision"))

    def fail_after_insert(*args: object, **kwargs: object) -> int:
        original_write(*args, **kwargs)
        raise RuntimeError("injected failure after decision insert")

    monkeypatch.setattr(actions, "_write_owner_decision", fail_after_insert)
    with pytest.raises(RuntimeError, match="injected failure"):
        confirm_tracker_fill_group(draft_id, db_path=db_path)

    conn = _conn(db_path)
    assert conn.execute("SELECT COUNT(*) FROM decisions WHERE ticker = 'NU'").fetchone()[0] == 0
    draft = conn.execute(
        "SELECT status, decision_id FROM decision_drafts WHERE id = ?", (draft_id,)
    ).fetchone()
    conn.close()
    assert draft["status"] == "awaiting_confirmation"
    assert draft["decision_id"] is None

    monkeypatch.setattr(actions, "_write_owner_decision", original_write)
    result = confirm_tracker_fill_group(draft_id, db_path=db_path)

    conn = _conn(db_path)
    count = conn.execute(
        "SELECT COUNT(*) FROM decisions WHERE ticker = 'NU' AND recommendation_kind = 'buy'"
    ).fetchone()[0]
    conn.close()
    assert count == 1
    assert result["decision_id"] is not None


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


def test_dismiss_late_tracker_fill_preserves_confirmed_decision(db_path: Path) -> None:
    _seed_roster(db_path, ["NU"])
    first_id = _make_tracker_fill(
        db_path,
        external_id="NU:2026-07-24:buy",
        amount_usd=100.0,
        key="tracker:dismiss-late-a",
    )
    first = confirm_tracker_fill_group(first_id, db_path=db_path)
    late_id = _make_tracker_fill(
        db_path,
        external_id="NU:2026-07-24:buy",
        amount_usd=250.0,
        key="tracker:dismiss-late-b",
    )

    result = dismiss_tracker_fill_group(late_id, db_path=db_path)

    conn = _conn(db_path)
    rows = conn.execute(
        "SELECT id, status, decision_id FROM decision_drafts WHERE id IN (?, ?) ORDER BY id",
        (first_id, late_id),
    ).fetchall()
    decision = conn.execute(
        "SELECT size_usd FROM decisions WHERE id = ?",
        (first["decision_id"],),
    ).fetchone()
    conn.close()
    assert result["receipt"] == "dismissed"
    assert result["fill_count"] == 1
    assert rows[0]["status"] == "confirmed"
    assert rows[0]["decision_id"] == first["decision_id"]
    assert rows[1]["status"] == "dismissed"
    assert rows[1]["decision_id"] is None
    assert decision["size_usd"] == 100.0


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


@pytest.mark.parametrize("ticker", ["NU", None])
def test_rationale_draft_does_not_attach_beyond_lookback(db_path: Path, ticker: str | None) -> None:
    if ticker:
        _seed_roster(db_path, [ticker])
    old = (datetime.now(UTC).replace(tzinfo=None) - timedelta(days=31)).isoformat()
    conn = _conn(db_path)
    conn.execute(
        "INSERT INTO decisions "
        "(ticker, recommendation_kind, decided_by, scope, made_at, created_at) "
        "VALUES (?, 'hold', 'owner', ?, ?, ?)",
        (ticker, "ticker" if ticker else "portfolio", old, old),
    )
    conn.commit()
    conn.close()
    draft_id = _make_draft(
        db_path,
        DecisionDraft(
            intent="rationale",
            proposed_ticker=ticker,
            proposed_rationale="Old context should not absorb this note.",
        ),
        text=f"old-rationale-{ticker}",
    )

    with pytest.raises(DraftActionError, match="nothing decision-shaped"):
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
