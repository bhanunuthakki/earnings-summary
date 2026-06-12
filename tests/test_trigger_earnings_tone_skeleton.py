"""Skeleton tests for the earnings-tone trigger.

Locks down the half of the trigger that doesn't depend on the LLM
plumbing:

  * Concrete class satisfies the runtime_checkable Trigger Protocol
  * Class attributes match the persistence-stable contract
  * ``scan()`` joins transcripts → documents and only flags transcripts
    whose ``documents.fetched_at`` falls in the last 24h
  * Missing ``transcripts`` table → ``[]`` rather than raise
  * ``should_fire()`` is feature-flag aware (PR-N8 flipped the flag on,
    so a real candidate now fires through this gate)
  * The prompt template file exists and parses as valid Jinja2

PR-N8 wired the LLM diff pass; end-to-end ``build_alert`` /
``draft_actions`` coverage lives in ``test_trigger_earnings_tone.py``
where the LLM call is mocked.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jinja2
import pytest

from triggers import (
    Cadence,
    EarningsToneTrigger,
    Trigger,
    UserStateContext,
)

PROMPT_PATH = (
    Path(__file__).resolve().parents[1] / "src" / "triggers" / "_prompts" / "earnings_tone_diff.txt"
)


def _create_transcripts_schema(conn: sqlite3.Connection) -> None:
    """Minimal schema mirror of migrations 0002 + 0005.

    We only need the columns the trigger reads: ``transcripts.id``,
    ``ticker``, ``fiscal_period_type``, ``period_end``, ``document_id``
    and ``documents.id``, ``fetched_at``. Skipping the full
    ``documents`` shape keeps the fixture readable; the FK isn't
    enforced under sqlite's default pragma anyway.
    """
    _ = conn.execute(
        "CREATE TABLE documents (id INTEGER PRIMARY KEY AUTOINCREMENT, fetched_at TEXT NOT NULL)"
    )
    _ = conn.execute(
        "CREATE TABLE transcripts ("
        + "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        + "document_id INTEGER NOT NULL, "
        + "ticker TEXT NOT NULL, "
        + "fiscal_period_type TEXT, "
        + "period_end TEXT"
        + ")"
    )


def _insert_transcript(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    fiscal_period_type: str,
    period_end: str,
    fetched_at: datetime,
) -> int:
    """Insert a documents + transcripts pair, return the transcript id."""
    cur = conn.execute(
        "INSERT INTO documents (fetched_at) VALUES (?)",
        (fetched_at.strftime("%Y-%m-%d %H:%M:%S"),),
    )
    doc_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO transcripts "
        + "(document_id, ticker, fiscal_period_type, period_end) "
        + "VALUES (?, ?, ?, ?)",
        (doc_id, ticker, fiscal_period_type, period_end),
    )
    return int(cur.lastrowid) if cur.lastrowid is not None else 0


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(str(tmp_path / "tone.db"))
    _create_transcripts_schema(conn)
    try:
        yield conn
    finally:
        conn.close()


def test_satisfies_runtime_checkable_protocol() -> None:
    assert isinstance(EarningsToneTrigger(), Trigger)


def test_class_attributes_match_contract() -> None:
    assert EarningsToneTrigger.kind == "earnings_tone"
    assert EarningsToneTrigger.cadence is Cadence.ON_EARNINGS


def test_scan_returns_empty_when_no_transcripts_table(tmp_path: Path) -> None:
    conn = sqlite3.connect(str(tmp_path / "empty.db"))
    try:
        assert EarningsToneTrigger().scan("MELI", conn) == []
    finally:
        conn.close()


def test_scan_ignores_transcript_fetched_more_than_24h_ago(
    db: sqlite3.Connection,
) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    _ = _insert_transcript(
        db,
        ticker="MELI",
        fiscal_period_type="Q1",
        period_end="2026-03-31",
        fetched_at=now - timedelta(hours=25),
    )
    assert EarningsToneTrigger().scan("MELI", db) == []


def test_scan_emits_candidate_for_fresh_transcript(
    db: sqlite3.Connection,
) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    transcript_id = _insert_transcript(
        db,
        ticker="MELI",
        fiscal_period_type="Q1",
        period_end="2026-03-31",
        fetched_at=now - timedelta(hours=1),
    )

    candidates = EarningsToneTrigger().scan("MELI", db)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.ticker == "MELI"
    assert candidate.kind == "earnings_tone"
    assert candidate.key == "MELI:Q1:2026"
    assert candidate.evidence["transcript_id"] == transcript_id
    assert candidate.evidence["fiscal_period"] == "2026"
    assert candidate.evidence["fiscal_period_type"] == "Q1"
    assert isinstance(candidate.evidence["published_at"], str)
    assert candidate.evidence["published_at"]  # non-empty


def test_scan_picks_most_recent_transcript_when_multiple_in_window(
    db: sqlite3.Connection,
) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    _ = _insert_transcript(
        db,
        ticker="MELI",
        fiscal_period_type="Q4",
        period_end="2025-12-31",
        fetched_at=now - timedelta(hours=10),
    )
    newer_id = _insert_transcript(
        db,
        ticker="MELI",
        fiscal_period_type="Q1",
        period_end="2026-03-31",
        fetched_at=now - timedelta(hours=2),
    )
    candidates = EarningsToneTrigger().scan("MELI", db)
    assert len(candidates) == 1
    assert candidates[0].evidence["transcript_id"] == newer_id
    assert candidates[0].key == "MELI:Q1:2026"


def test_scan_filters_by_ticker(db: sqlite3.Connection) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    _ = _insert_transcript(
        db,
        ticker="GOOG",
        fiscal_period_type="Q1",
        period_end="2026-03-31",
        fetched_at=now - timedelta(hours=1),
    )
    assert EarningsToneTrigger().scan("MELI", db) == []


def test_should_fire_returns_true_for_real_candidate_after_flag_flip(
    db: sqlite3.Connection,
) -> None:
    """PR-N8 flipped ``_FEATURE_ENABLED`` on; a real scan candidate now
    passes the gate. The morning driver in PR-N9 layers signature_sha
    dedup + dismissal-set membership on top — those are out of scope
    for this unit test."""
    now = datetime.now(UTC).replace(tzinfo=None)
    _ = _insert_transcript(
        db,
        ticker="MELI",
        fiscal_period_type="Q1",
        period_end="2026-03-31",
        fetched_at=now - timedelta(hours=1),
    )
    candidates = EarningsToneTrigger().scan("MELI", db)
    assert len(candidates) == 1
    user_state = UserStateContext(
        registered_kpis=[],
        sizing_intents=[],
        recent_dismissed_signatures=set(),
    )
    assert EarningsToneTrigger().should_fire(candidates[0], user_state) is True


def test_prompt_template_exists_and_parses_as_jinja2() -> None:
    assert PROMPT_PATH.is_file(), f"Expected prompt template at {PROMPT_PATH}"
    text = PROMPT_PATH.read_text(encoding="utf-8")
    assert text.strip(), "Prompt template must be non-empty"
    env = jinja2.Environment(autoescape=False)
    # Raises jinja2.TemplateSyntaxError on malformed templates.
    _ = env.parse(text)
