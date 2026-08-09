"""Tests for LLM-drafted exit post-mortems (B6, src/synthesis/exit_postmortem.py).

Builds a fully alembic-migrated DB (every real migration executes, in order,
against the bootstrap tables ``db.py``'s ``init_db()`` normally creates —
same pattern as ``tests/test_tenet_accountability.py`` / ``test_decision_journal_view.py``)
so ``position_entries`` (0088), ``analyst_notes.position_entry_id`` (0093),
``decisions`` (0046) and ``v_decision_journal`` (0179) all exist for real.

Every LLM call is an injected stub (zero live LLM) and the tracker is either
left unreachable (default — no server listening) or explicitly monkeypatched,
so no test ever makes a real network call.

Covers: pending derivation (all-three-NULL closed rows only), draft
validation (enum coercion with deterministic fallback, empty text -> None),
write-once application, the ``llm_draft`` provenance marker + revert gate,
batch summary composition, and the transient-defer / hard-stop contract.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from synthesis.exit_postmortem import (
    Draft,
    _compose_batch_summary,  # unit-tested directly
    apply_draft,
    draft_postmortem,
    drafted_awaiting_glance,
    gather_evidence,
    is_awaiting_glance,
    pending_postmortems,
    revert_draft,
    run_postmortem_drafts,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Verbatim from tests/test_tenet_accountability.py / test_decision_journal_view.py:
# the three tables db.py's init_db() creates OUTSIDE alembic (every migration
# from 0001 on assumes these already exist).
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


@pytest.fixture
def db_path(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    db = tmp_path / "data" / "ledger.db"
    return migrated_db(db)


def _insert_entry(
    db_path: Path,
    *,
    ticker: str = "NU",
    entry_date: str | None = "2026-01-01",
    entry_price: float | None = 10.0,
    exit_date: str | None = "2026-06-01",
    exit_price: float | None = 12.0,
    exit_reason: str | None = None,
    lessons: str | None = None,
    outcome_vs_thesis: str | None = None,
    thesis_excerpt: str | None = "LatAm digital bank with a deposit moat.",
) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        now = "2026-06-01T00:00:00"
        cur = conn.execute(
            """
            INSERT INTO position_entries
                (user_id, ticker, entry_date, entry_price, entry_thesis_excerpt,
                 exit_date, exit_price, exit_reason, lessons, outcome_vs_thesis,
                 source, created_at, updated_at)
            VALUES ('bhanu', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reconciler', ?, ?)
            """,
            (
                ticker,
                entry_date,
                entry_price,
                thesis_excerpt,
                exit_date,
                exit_price,
                exit_reason,
                lessons,
                outcome_vs_thesis,
                now,
                now,
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def _insert_decision(
    db_path: Path,
    *,
    ticker: str = "NU",
    kind: str = "add",
    made_at: str,
    decided_by: str = "owner",
    conviction: str | None = None,
    falsifier: str | None = None,
) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            """
            INSERT INTO decisions
                (ticker, recommendation_kind, decided_by, conviction, falsifier,
                 made_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (ticker, kind, decided_by, conviction, falsifier, made_at, made_at),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def _get_entry_row(db_path: Path, entry_id: int) -> sqlite3.Row:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM position_entries WHERE id = ?", (entry_id,)).fetchone()
        assert row is not None
        return row
    finally:
        conn.close()


def _entry(db_path: Path, entry_id: int):
    from position_lifecycle import get_entry

    entry = get_entry(entry_id, db_path=db_path)
    assert entry is not None
    return entry


# ---------------------------------------------------------------------------
# pending_postmortems — derived, no stamp column
# ---------------------------------------------------------------------------


def test_pending_postmortems_only_all_three_null_closed_rows(db_path: Path) -> None:
    pending_id = _insert_entry(db_path, ticker="NU")
    # Open row — never pending regardless of grading fields.
    _insert_entry(db_path, ticker="MELI", exit_date=None, exit_price=None)
    # Closed but partially graded — not pending (would get stuck half-filled).
    _insert_entry(db_path, ticker="WIX", exit_reason="took profits")
    # Closed and fully graded — not pending.
    _insert_entry(
        db_path,
        ticker="NOW",
        exit_reason="thesis broke",
        lessons="watch it earlier",
        outcome_vs_thesis="broke",
    )

    pending = pending_postmortems(db_path)
    assert [e.id for e in pending] == [pending_id]
    assert pending[0].ticker == "NU"


# ---------------------------------------------------------------------------
# gather_evidence
# ---------------------------------------------------------------------------


def test_gather_evidence_scopes_to_ticker_and_entry_date(db_path: Path) -> None:
    entry_id = _insert_entry(db_path, ticker="NU", entry_date="2026-03-01")
    before_id = _insert_decision(db_path, ticker="NU", made_at="2026-01-01T00:00:00")
    other_ticker_id = _insert_decision(db_path, ticker="MELI", made_at="2026-04-01T00:00:00")
    in_window_id = _insert_decision(db_path, ticker="NU", made_at="2026-04-01T00:00:00")

    entry = _entry(db_path, entry_id)
    evidence = gather_evidence(entry, db_path=db_path)

    ids = {d.decision_id for d in evidence.decisions}
    assert in_window_id in ids
    assert before_id not in ids
    assert other_ticker_id not in ids


def test_gather_evidence_no_decisions_is_empty(db_path: Path) -> None:
    entry_id = _insert_entry(db_path, ticker="ZZZZ")
    entry = _entry(db_path, entry_id)
    evidence = gather_evidence(entry, db_path=db_path)
    assert evidence.decisions == []


def test_gather_evidence_tracker_down_degrades_alpha_to_none(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import integrations.portfolio_tracker_client as tracker_client

    def _raise(*args: object, **kwargs: object) -> object:
        raise ConnectionError("tracker unreachable")

    monkeypatch.setattr(tracker_client, "fetch_portfolio_analytics", _raise)
    entry_id = _insert_entry(db_path, ticker="NU")
    _insert_decision(db_path, ticker="NU", made_at="2026-04-01T00:00:00")
    entry = _entry(db_path, entry_id)
    evidence = gather_evidence(entry, db_path=db_path)
    assert evidence.alpha_since_entry is None


# ---------------------------------------------------------------------------
# draft_postmortem — validation, enum coercion, transient/hard-stop
# ---------------------------------------------------------------------------


def test_draft_postmortem_skips_no_evidence_zero_calls(db_path: Path) -> None:
    entry_id = _insert_entry(db_path, ticker="ZZZZ")
    entry = _entry(db_path, entry_id)

    def _spy(prompt: str) -> dict[str, object] | None:
        raise AssertionError("must not be called when there is no decision trail")

    assert draft_postmortem(entry, db_path=db_path, call=_spy) is None


def test_draft_postmortem_valid_response(db_path: Path) -> None:
    entry_id = _insert_entry(db_path, ticker="NU")
    _insert_decision(db_path, ticker="NU", made_at="2026-04-01T00:00:00")
    entry = _entry(db_path, entry_id)

    def _stub(prompt: str) -> dict[str, object]:
        return {
            "exit_reason": "Funding costs eroded the margin thesis.",
            "lessons": "Watch deposit beta earlier next time.",
            "outcome_vs_thesis": "broke",
        }

    draft = draft_postmortem(entry, db_path=db_path, call=_stub)
    assert draft is not None
    assert draft.exit_reason == "Funding costs eroded the margin thesis."
    assert draft.outcome_vs_thesis == "broke"


def test_draft_postmortem_invalid_outcome_falls_back_to_inferred(
    db_path: Path,
) -> None:
    entry_id = _insert_entry(db_path, ticker="NU")
    _insert_decision(db_path, ticker="NU", made_at="2026-04-01T00:00:00")
    entry = _entry(db_path, entry_id)
    # No thesis_evaluations table content -> the deterministic inferred
    # outcome is None too, so an invalid label from the model must discard
    # the whole draft (never fabricate an enum member).

    def _stub(prompt: str) -> dict[str, object]:
        return {
            "exit_reason": "Took profits.",
            "lessons": "Size up earlier.",
            "outcome_vs_thesis": "vibes",  # not a real OUTCOME_VOCAB member
        }

    assert draft_postmortem(entry, db_path=db_path, call=_stub) is None


def test_draft_postmortem_invalid_outcome_uses_inferred_when_available(
    db_path: Path,
) -> None:
    entry_id = _insert_entry(db_path, ticker="NU", exit_date="2026-06-10")
    _insert_decision(db_path, ticker="NU", made_at="2026-04-01T00:00:00")
    # thesis_evaluations already exists for real (alembic 0016, part of the
    # head chain) — seed a row through it directly rather than re-declaring
    # the table (0053 added rule_evaluations_json NOT NULL).
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO thesis_evaluations "
        "(ticker, evaluated_at, overall_status, rule_evaluations_json, run_id) "
        "VALUES ('NU', '2026-05-01T00:00:00', 'ok', '[]', 'r1')"
    )
    conn.commit()
    conn.close()
    entry = _entry(db_path, entry_id)

    def _stub(prompt: str) -> dict[str, object]:
        return {
            "exit_reason": "Took profits.",
            "lessons": "Size up earlier.",
            "outcome_vs_thesis": "",
        }

    draft = draft_postmortem(entry, db_path=db_path, call=_stub)
    assert draft is not None
    assert draft.outcome_vs_thesis == "played_out"  # the deterministic breach-history read


def test_draft_postmortem_empty_text_returns_none(db_path: Path) -> None:
    entry_id = _insert_entry(db_path, ticker="NU")
    _insert_decision(db_path, ticker="NU", made_at="2026-04-01T00:00:00")
    entry = _entry(db_path, entry_id)

    def _stub(prompt: str) -> dict[str, object]:
        return {"exit_reason": "", "lessons": "Size up earlier.", "outcome_vs_thesis": "broke"}

    assert draft_postmortem(entry, db_path=db_path, call=_stub) is None


def test_draft_postmortem_transient_failure_returns_none(db_path: Path) -> None:
    entry_id = _insert_entry(db_path, ticker="NU")
    _insert_decision(db_path, ticker="NU", made_at="2026-04-01T00:00:00")
    entry = _entry(db_path, entry_id)

    def _boom(prompt: str) -> dict[str, object]:
        raise RuntimeError("transient CLI failure")

    assert draft_postmortem(entry, db_path=db_path, call=_boom) is None


def test_draft_postmortem_hard_stop_propagates(db_path: Path) -> None:
    from llm.cli import LLMSetupError

    entry_id = _insert_entry(db_path, ticker="NU")
    _insert_decision(db_path, ticker="NU", made_at="2026-04-01T00:00:00")
    entry = _entry(db_path, entry_id)

    def _hard_stop(prompt: str) -> dict[str, object]:
        raise LLMSetupError("claude CLI not found")

    with pytest.raises(LLMSetupError):
        draft_postmortem(entry, db_path=db_path, call=_hard_stop)


# ---------------------------------------------------------------------------
# apply_draft — write-once + provenance marker
# ---------------------------------------------------------------------------


def test_apply_draft_writes_all_three_and_lands_provenance_note(db_path: Path) -> None:
    entry_id = _insert_entry(db_path, ticker="NU")
    entry = _entry(db_path, entry_id)
    draft = Draft(
        exit_reason="Thesis broke.", lessons="Watch it earlier.", outcome_vs_thesis="broke"
    )

    assert apply_draft(entry, draft, db_path=db_path) is True

    row = _get_entry_row(db_path, entry_id)
    assert row["exit_reason"] == "Thesis broke."
    assert row["lessons"] == "Watch it earlier."
    assert row["outcome_vs_thesis"] == "broke"
    assert is_awaiting_glance(entry_id, db_path=db_path) is True
    assert [e.id for e in drafted_awaiting_glance(db_path)] == [entry_id]


def test_apply_draft_write_once_preserves_manually_graded_field(db_path: Path) -> None:
    # An owner already typed exit_reason before the sweep ran; apply_draft
    # must never clobber it, only fill the two still-NULL fields.
    entry_id = _insert_entry(db_path, ticker="NU", exit_reason="owner's own words")
    entry = _entry(db_path, entry_id)
    draft = Draft(
        exit_reason="LLM-generated reason", lessons="LLM lesson", outcome_vs_thesis="broke"
    )

    assert apply_draft(entry, draft, db_path=db_path) is True

    row = _get_entry_row(db_path, entry_id)
    assert row["exit_reason"] == "owner's own words"  # untouched
    assert row["lessons"] == "LLM lesson"
    assert row["outcome_vs_thesis"] == "broke"


def test_apply_draft_noop_when_already_fully_graded(db_path: Path) -> None:
    entry_id = _insert_entry(
        db_path,
        ticker="NU",
        exit_reason="x",
        lessons="y",
        outcome_vs_thesis="mixed",
    )
    entry = _entry(db_path, entry_id)
    draft = Draft(exit_reason="new", lessons="new", outcome_vs_thesis="broke")
    assert apply_draft(entry, draft, db_path=db_path) is False

    row = _get_entry_row(db_path, entry_id)
    assert row["exit_reason"] == "x"  # unchanged


# ---------------------------------------------------------------------------
# revert_draft — only reverts the llm_draft marker
# ---------------------------------------------------------------------------


def test_revert_draft_reverts_llm_draft(db_path: Path) -> None:
    entry_id = _insert_entry(db_path, ticker="NU")
    entry = _entry(db_path, entry_id)
    draft = Draft(exit_reason="a", lessons="b", outcome_vs_thesis="broke")
    apply_draft(entry, draft, db_path=db_path)

    assert revert_draft(entry_id, db_path=db_path) is True
    row = _get_entry_row(db_path, entry_id)
    assert row["exit_reason"] is None
    assert row["lessons"] is None
    assert row["outcome_vs_thesis"] is None


def test_revert_draft_never_touches_owner_grading(db_path: Path) -> None:
    # A manually-graded row carries no llm_draft provenance note at all.
    entry_id = _insert_entry(
        db_path,
        ticker="NU",
        exit_reason="owner wrote this",
        lessons="owner's lesson",
        outcome_vs_thesis="played_out",
    )
    assert revert_draft(entry_id, db_path=db_path) is False
    row = _get_entry_row(db_path, entry_id)
    assert row["exit_reason"] == "owner wrote this"


# ---------------------------------------------------------------------------
# batch summary composition
# ---------------------------------------------------------------------------


def test_compose_batch_summary() -> None:
    text = _compose_batch_summary([("NU", "broke"), ("WIX", "played_out")])
    assert "Drafted 2 exit post-mortems" in text
    assert "NU (broke)" in text
    assert "WIX (played_out)" in text
    assert "revertible" in text


def test_compose_batch_summary_singular() -> None:
    text = _compose_batch_summary([("NU", "broke")])
    assert "Drafted 1 exit post-mortem:" in text
    assert "post-mortems" not in text.split(":")[0]


# ---------------------------------------------------------------------------
# run_postmortem_drafts — pacing, batch, counts
# ---------------------------------------------------------------------------


def test_run_postmortem_drafts_paces_to_max_per_run(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PORTFOLIO_TRACKER_API_URL", raising=False)
    tickers = ["NU", "MELI", "WIX", "NOW", "VEEV"]
    for t in tickers:
        _insert_entry(db_path, ticker=t)
        _insert_decision(db_path, ticker=t, made_at="2026-04-01T00:00:00")

    def _stub(prompt: str) -> dict[str, object]:
        return {"exit_reason": "x", "lessons": "y", "outcome_vs_thesis": "mixed"}

    counts = run_postmortem_drafts(db_path, call=_stub, max_per_run=2)
    assert counts["pending"] == 5
    assert counts["drafted"] == 2
    assert len(pending_postmortems(db_path)) == 3  # the rest stay pending


def test_run_postmortem_drafts_batch_drafts_all_and_sends_summary(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PORTFOLIO_TRACKER_API_URL", raising=False)
    for t in ("NU", "MELI", "WIX"):
        _insert_entry(db_path, ticker=t)
        _insert_decision(db_path, ticker=t, made_at="2026-04-01T00:00:00")

    def _stub(prompt: str) -> dict[str, object]:
        return {"exit_reason": "x", "lessons": "y", "outcome_vs_thesis": "mixed"}

    sent: list[tuple[str, str, str]] = []

    def _fake_send(token: str, chat_id: str, text: str, **kwargs: object) -> object:
        sent.append((token, chat_id, text))
        return object()

    import capture.telegram as telegram_mod
    import capture.token_store as token_store_mod

    monkeypatch.setattr(telegram_mod, "send_message", _fake_send)
    monkeypatch.setattr(token_store_mod, "load_token", lambda *a, **k: "fake-token")
    monkeypatch.setattr(token_store_mod, "load_chat_id", lambda *a, **k: "123")

    counts = run_postmortem_drafts(db_path, call=_stub, batch=True)
    assert counts["pending"] == 3
    assert counts["drafted"] == 3
    assert pending_postmortems(db_path) == []
    assert len(sent) == 1
    assert "Drafted 3 exit post-mortems" in sent[0][2]


def test_run_postmortem_drafts_skips_no_evidence(db_path: Path) -> None:
    _insert_entry(db_path, ticker="ZZZZ")  # no decisions on record

    def _spy(prompt: str) -> dict[str, object]:
        raise AssertionError("must not be called")

    counts = run_postmortem_drafts(db_path, call=_spy)
    assert counts["pending"] == 1
    assert counts["skipped"] == 1
    assert counts["drafted"] == 0


def test_run_postmortem_drafts_tallies_deferred_transient(db_path: Path) -> None:
    _insert_entry(db_path, ticker="NU")
    _insert_decision(db_path, ticker="NU", made_at="2026-04-01T00:00:00")

    def _boom(prompt: str) -> dict[str, object]:
        raise RuntimeError("transient")

    counts = run_postmortem_drafts(db_path, call=_boom)
    assert counts["deferred_transient"] == 1
    assert counts["drafted"] == 0
    # Still pending — retried next sweep.
    assert len(pending_postmortems(db_path)) == 1


def test_run_postmortem_drafts_hard_stop_propagates(db_path: Path) -> None:
    from llm.cli import LLMSetupError

    _insert_entry(db_path, ticker="NU")
    _insert_decision(db_path, ticker="NU", made_at="2026-04-01T00:00:00")

    def _hard_stop(prompt: str) -> dict[str, object]:
        raise LLMSetupError("claude CLI not found")

    with pytest.raises(LLMSetupError):
        run_postmortem_drafts(db_path, call=_hard_stop)


def test_run_postmortem_drafts_no_pending_is_a_noop(db_path: Path) -> None:
    counts = run_postmortem_drafts(db_path)
    assert counts == {"pending": 0, "drafted": 0, "deferred_transient": 0, "skipped": 0}
