"""End-to-end tests for the earnings-tone trigger's LLM diff pass.

Mocks ``triggers.earnings_tone.call_llm`` so the suite runs against a
local SQLite fixture without hitting the Anthropic API. Verifies:

  * ``should_fire`` is now True for a real candidate (feature flag flipped)
  * ``build_alert`` end-to-end → AlertDraft with the expected shape
  * Cache hit short-circuits the LLM call on the second invocation
  * Malformed JSON on the first attempt → retry, succeed on retry
  * Malformed JSON twice → raises ``EarningsToneLLMError``
  * ``no_material_shifts_detected=True`` round-trips cleanly
  * ``draft_actions`` derives the right ``earnings_prep_append`` /
    ``thesis_update`` / ``bear_append`` mix per shift policy
  * Full pipeline (scan → should_fire → build_alert → draft_actions)
    integrates against a fresh fixture
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from triggers import (
    EarningsToneTrigger,
    UserStateContext,
)
from triggers.earnings_tone import EarningsToneLLMError

# ---------------------------------------------------------------------------
# Schema fixture — minimal subset of migrations 0002 + 0005 + 0019 + 0035
# ---------------------------------------------------------------------------


def _create_schema(conn: sqlite3.Connection) -> None:
    """Create just enough schema for the trigger + artifact-store cache.

    Mirrors the columns the trigger reads/writes; foreign keys are not
    enforced under sqlite's default PRAGMA so we can skip the
    ``llm_calls`` parent table the production schema declares.
    """
    _ = conn.execute(
        "CREATE TABLE documents ("
        + "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        + "fetched_at TEXT NOT NULL"
        + ")"
    )
    _ = conn.execute(
        "CREATE TABLE transcripts ("
        + "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        + "document_id INTEGER NOT NULL, "
        + "ticker TEXT NOT NULL, "
        + "fiscal_period_type TEXT, "
        + "period_end TEXT, "
        + "has_qa_section INTEGER"
        + ")"
    )
    _ = conn.execute(
        "CREATE TABLE transcript_segments ("
        + "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        + "transcript_id INTEGER NOT NULL, "
        + "seq INTEGER NOT NULL, "
        + "speaker TEXT, "
        + "text TEXT NOT NULL"
        + ")"
    )
    _ = conn.execute(
        "CREATE TABLE llm_artifacts ("
        + "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        + "ticker TEXT, "
        + "scope TEXT NOT NULL DEFAULT 'ticker', "
        + "purpose TEXT NOT NULL, "
        + "fiscal_period TEXT, "
        + "content_md TEXT, "
        + "content_json TEXT, "
        + "input_sha256 TEXT NOT NULL, "
        + "output_sha256 TEXT, "
        + "model TEXT, "
        + "prompt_version TEXT NOT NULL DEFAULT 'v1', "
        + "generated_at TEXT NOT NULL, "
        + "expires_at TEXT, "
        + "superseded_by_id INTEGER, "
        + "dirty INTEGER NOT NULL DEFAULT 0, "
        + "dirty_reason TEXT, "
        + "source_doc_ids TEXT, "
        + "parent_artifact_ids TEXT, "
        + "llm_call_id INTEGER"
        + ")"
    )


def _insert_transcript(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    fiscal_period_type: str,
    period_end: str,
    fetched_at: datetime,
    has_qa_section: bool | None = True,
    segments: list[tuple[str, str]] | None = None,
) -> int:
    """Insert a documents + transcripts + segments triple, return transcript id.

    ``segments`` is a list of (speaker, text) tuples in chronological order.
    The default exercises the "first question comes from" boundary so the
    splitter has both prepared and Q&A halves.
    """
    cur = conn.execute(
        "INSERT INTO documents (fetched_at) VALUES (?)",
        (fetched_at.strftime("%Y-%m-%d %H:%M:%S"),),
    )
    doc_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO transcripts "
        + "(document_id, ticker, fiscal_period_type, period_end, has_qa_section) "
        + "VALUES (?, ?, ?, ?, ?)",
        (
            doc_id,
            ticker,
            fiscal_period_type,
            period_end,
            None if has_qa_section is None else (1 if has_qa_section else 0),
        ),
    )
    transcript_id = int(cur.lastrowid) if cur.lastrowid is not None else 0

    if segments is None:
        segments = [
            ("CEO", "Welcome everyone. Revenue grew 25 percent this quarter."),
            ("CFO", "Operating margin expanded 200 basis points."),
            (
                "Operator",
                "Thank you. Our first question comes from an analyst at Bigbank.",
            ),
            ("Analyst", "Can you walk through the cloud margin trajectory?"),
            ("CEO", "We expect continued margin expansion through the year."),
        ]
    for seq, (speaker, text) in enumerate(segments):
        _ = conn.execute(
            "INSERT INTO transcript_segments (transcript_id, seq, speaker, text) "
            + "VALUES (?, ?, ?, ?)",
            (transcript_id, seq, speaker, text),
        )
    # Commit immediately so a separate write connection (the artifact-store
    # cache opened inside build_alert) can read and write without
    # tripping a sqlite lock contention. The fixture's connection stays
    # open for scan() but no longer holds an outstanding transaction.
    conn.commit()
    return transcript_id


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create the test DB and route every ``DB_PATH`` consumer to it.

    Patches ``db.DB_PATH`` so both the trigger's own ``_db_path()``
    helper and the artifact-store's internal lookup resolve to the
    fixture instead of the real ``data/portfolio.db``.
    """
    path = tmp_path / "test.db"
    conn = sqlite3.connect(str(path))
    try:
        _create_schema(conn)
        # WAL journal mode lets the artifact-store's separate write
        # connection coexist with the test's read connection without
        # tripping a sqlite file lock.
        _ = conn.execute("PRAGMA journal_mode=WAL")
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr("db.DB_PATH", str(path))
    return path


@pytest.fixture
def fixture_db(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Yield a connection on the fixture DB.

    Each helper that inserts data commits on its way out so a separate
    write connection (the artifact-store cache) can write to the file
    without contending the test's connection's lock.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# LLM mock helpers
# ---------------------------------------------------------------------------


def _diff_payload(
    *,
    summary: str = "Margin commentary softened materially this quarter.",
    shifts: list[dict[str, object]] | None = None,
    no_shifts: bool = False,
) -> str:
    """Build a canned JSON response string matching the prompt's contract."""
    if no_shifts:
        return json.dumps(
            {
                "summary": "No material tone shifts detected this quarter.",
                "shifts": [],
                "no_material_shifts_detected": True,
            }
        )
    if shifts is None:
        shifts = [
            {
                "topic": "Cloud operating margin trajectory",
                "direction": "softer",
                "confidence": 0.82,
                "citations": [
                    {
                        "kind": "transcript_line",
                        "period": "Q1-2026",
                        "line_number": 12,
                        "excerpt": "we'll see how the year develops",
                    }
                ],
                "related_thesis_kpi": "Cloud Op Margin",
            },
            {
                "topic": "Capex pace commentary",
                "direction": "firmer",
                "confidence": 0.74,
                "citations": [],
                "related_thesis_kpi": None,
            },
        ]
    return json.dumps(
        {
            "summary": summary,
            "shifts": shifts,
            "no_material_shifts_detected": False,
        }
    )


class _StatefulLLM:
    """Tracks call count + returns canned responses in sequence.

    The last entry is reused once the queue is exhausted so a test that
    only sets up one response doesn't index-out-of-bounds on accidental
    re-call (which is itself useful signal — see the cache-hit test that
    asserts call_count remains at 1).
    """

    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.call_count = 0
        self.prompts: list[str] = []

    def __call__(self, prompt: str, **_kwargs: object) -> str:
        self.prompts.append(prompt)
        idx = min(self.call_count, len(self.responses) - 1)
        self.call_count += 1
        return self.responses[idx]


def _seed_five_quarters(conn: sqlite3.Connection, *, ticker: str = "MELI") -> int:
    """Insert current + 4 prior transcripts. Returns the current transcript id."""
    now = datetime.now(UTC).replace(tzinfo=None)
    # 4 priors, oldest first; period_end matches a quarterly cadence.
    priors = [
        ("Q1", "2025-03-31"),
        ("Q2", "2025-06-30"),
        ("Q3", "2025-09-30"),
        ("Q4", "2025-12-31"),
    ]
    for fpt, period_end in priors:
        _ = _insert_transcript(
            conn,
            ticker=ticker,
            fiscal_period_type=fpt,
            period_end=period_end,
            fetched_at=now - timedelta(days=90),
        )
    return _insert_transcript(
        conn,
        ticker=ticker,
        fiscal_period_type="Q1",
        period_end="2026-03-31",
        fetched_at=now - timedelta(hours=1),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_should_fire_true_after_flag_flip(
    fixture_db: sqlite3.Connection,
) -> None:
    """The feature flag now lets scan candidates through should_fire."""
    _ = _seed_five_quarters(fixture_db)
    candidates = EarningsToneTrigger().scan("MELI", fixture_db)
    assert len(candidates) == 1
    state = UserStateContext(
        registered_kpis=[], sizing_intents=[], recent_dismissed_signatures=set()
    )
    assert EarningsToneTrigger().should_fire(candidates[0], state) is True


def test_scan_attaches_earnings_qualitative_condition(fixture_db: sqlite3.Connection) -> None:
    """L9 PR2 — a new transcript is where an 'earnings'-watched qualitative
    condition surfaces. scan attaches it (and a 'both'), but NOT a 'news'-only
    one — that routes to material_news. The fixture has no decisions table; we
    create it here so the other scan tests stay byte-identical."""
    _ = _seed_five_quarters(fixture_db)
    fixture_db.execute(
        "CREATE TABLE decisions ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, recommendation_kind TEXT, "
        "recommendation_value FLOAT, made_at TEXT, source_lens TEXT, outcome_at TEXT, "
        "qualitative_conditions TEXT)"
    )
    fixture_db.execute(
        "INSERT INTO decisions (ticker, recommendation_kind, made_at, qualitative_conditions) "
        "VALUES ('MELI', 'add', '2026-05-01', ?)",
        (
            json.dumps(
                [
                    {"phrase": "tone turns defensive", "watch_for": "earnings", "note": None},
                    {"phrase": "lawsuit filed", "watch_for": "both", "note": None},
                    {"phrase": "CEO departs", "watch_for": "news", "note": None},
                ]
            ),
        ),
    )
    fixture_db.commit()

    candidates = EarningsToneTrigger().scan("MELI", fixture_db)
    assert len(candidates) == 1
    overlay = candidates[0].evidence["thesis_conditions"]
    assert isinstance(overlay, list)
    phrases = [str(cast("dict[str, object]", p)["phrase"]) for p in cast("list[object]", overlay)]
    assert sorted(phrases) == ["lawsuit filed", "tone turns defensive"]


def test_build_alert_round_trip_with_mocked_llm(
    fixture_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: scan → build_alert produces a well-formed AlertDraft."""
    current_id = _seed_five_quarters(fixture_db)
    mock = _StatefulLLM([_diff_payload()])
    monkeypatch.setattr("triggers.earnings_tone.call_llm", mock)

    candidates = EarningsToneTrigger().scan("MELI", fixture_db)
    assert len(candidates) == 1
    alert = EarningsToneTrigger().build_alert(candidates[0], None)

    assert alert.trigger_kind == "earnings_tone"
    assert alert.ticker == "MELI"
    assert alert.memo_text == "Margin commentary softened materially this quarter."
    assert mock.call_count == 1

    # Evidence_json round-trips to the expected dict structure.
    evidence = json.loads(alert.evidence_json)
    assert evidence["transcript_id"] == current_id
    assert evidence["no_material_shifts_detected"] is False
    assert len(evidence["shifts"]) == 2
    assert evidence["shifts"][0]["topic"] == "Cloud operating margin trajectory"
    assert evidence["prior_transcript_ids"]
    assert all(isinstance(t, int) for t in evidence["prior_transcript_ids"])

    # Signature is keyed on the new transcript only, per the spec.
    from alerts.store import compute_signature_sha

    expected_sig = compute_signature_sha("earnings_tone", "MELI", {"transcript_id": current_id})
    assert alert.signature_sha == expected_sig


def test_build_alert_retries_once_on_malformed_json(
    fixture_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First response is non-JSON; retry succeeds and the alert lands."""
    _ = _seed_five_quarters(fixture_db)
    mock = _StatefulLLM(
        [
            "Here is your answer: { I am not JSON",  # first attempt — bad
            _diff_payload(),  # retry — good
        ]
    )
    monkeypatch.setattr("triggers.earnings_tone.call_llm", mock)

    candidates = EarningsToneTrigger().scan("MELI", fixture_db)
    alert = EarningsToneTrigger().build_alert(candidates[0], None)

    assert mock.call_count == 2
    # The retry prompt MUST carry the explicit JSON-only preamble so the
    # model has a chance of recovering — the trigger's contract.
    assert "previous response was not valid JSON" in mock.prompts[1]
    # And the alert still lands cleanly.
    assert alert.memo_text


def test_build_alert_raises_when_malformed_json_twice(
    fixture_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two malformed responses → trigger raises EarningsToneLLMError."""
    _ = _seed_five_quarters(fixture_db)
    mock = _StatefulLLM(["not json", "still not json"])
    monkeypatch.setattr("triggers.earnings_tone.call_llm", mock)

    candidates = EarningsToneTrigger().scan("MELI", fixture_db)
    with pytest.raises(EarningsToneLLMError):
        _ = EarningsToneTrigger().build_alert(candidates[0], None)
    assert mock.call_count == 2


def test_build_alert_handles_no_material_shifts(
    fixture_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the LLM flags no material shifts, the AlertDraft still lands.

    The morning driver (PR-N9) inspects the flag and decides whether to
    actually fire — the trigger's job is only to produce the structured
    evidence + audit trail.
    """
    _ = _seed_five_quarters(fixture_db)
    mock = _StatefulLLM([_diff_payload(no_shifts=True)])
    monkeypatch.setattr("triggers.earnings_tone.call_llm", mock)

    candidates = EarningsToneTrigger().scan("MELI", fixture_db)
    alert = EarningsToneTrigger().build_alert(candidates[0], None)

    evidence = json.loads(alert.evidence_json)
    assert evidence["no_material_shifts_detected"] is True
    assert evidence["shifts"] == []
    assert alert.memo_text == "No material tone shifts detected this quarter."


def test_build_alert_cache_hit_skips_llm_call(
    fixture_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second build_alert against the same transcripts is a cache hit."""
    _ = _seed_five_quarters(fixture_db)
    mock = _StatefulLLM([_diff_payload()])
    monkeypatch.setattr("triggers.earnings_tone.call_llm", mock)

    candidates = EarningsToneTrigger().scan("MELI", fixture_db)

    first_alert = EarningsToneTrigger().build_alert(candidates[0], None)
    assert mock.call_count == 1

    # Second call — same candidate, same prior set, same anchor (none).
    # The artifact-store cache should fire and call_llm must not run again.
    second_alert = EarningsToneTrigger().build_alert(candidates[0], None)
    assert mock.call_count == 1, "cache miss — call_llm was re-invoked"

    # The cached evidence_json carries the same payload (modulo
    # fired_at, which is stamped fresh per call).
    first_evidence = json.loads(first_alert.evidence_json)
    second_evidence = json.loads(second_alert.evidence_json)
    assert first_evidence == second_evidence
    # And signature_sha is deterministic on transcript_id alone.
    assert first_alert.signature_sha == second_alert.signature_sha


def test_draft_actions_full_action_mix(
    fixture_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Confirm the per-shift action policy across the cases:

    - earnings_prep_append fires for EVERY shift (always)
    - thesis_update fires only for directional shifts with confidence ≥ 0.6
    - bear_append fires for adverse (softer/dropped_topic) shifts that
      name a thesis KPI — confidence doesn't gate bear_append
    """
    _ = _seed_five_quarters(fixture_db)
    shifts: list[dict[str, object]] = [
        # Adverse + thesis-touching + confident → all three actions
        {
            "topic": "Cloud margin commentary softened",
            "direction": "softer",
            "confidence": 0.85,
            "citations": [],
            "related_thesis_kpi": "Cloud Op Margin",
        },
        # Firmer + confident but no KPI → earnings_prep + thesis_update
        {
            "topic": "Capex pacing tightened",
            "direction": "firmer",
            "confidence": 0.78,
            "citations": [],
            "related_thesis_kpi": None,
        },
        # Adverse + KPI but LOW confidence → earnings_prep + bear_append
        # (no thesis_update because confidence is below the floor; bear_append
        # has no confidence gate per spec — adverse direction + thesis-relevant
        # KPI is enough to warrant adding to the bear file)
        {
            "topic": "FX color qualitative",
            "direction": "softer",
            "confidence": 0.3,
            "citations": [],
            "related_thesis_kpi": "Reported Revenue Growth",
        },
    ]
    monkeypatch.setattr(
        "triggers.earnings_tone.call_llm",
        _StatefulLLM([_diff_payload(summary="Mixed quarter.", shifts=shifts)]),
    )
    candidates = EarningsToneTrigger().scan("MELI", fixture_db)
    alert = EarningsToneTrigger().build_alert(candidates[0], None)
    actions = EarningsToneTrigger().draft_actions(alert, candidates[0])

    by_kind: dict[str, list[dict[str, Any]]] = {}
    for a in actions:
        by_kind.setdefault(a.action_kind, []).append(dict(a.payload))

    # Every shift → one earnings_prep_append.
    assert len(by_kind.get("earnings_prep_append", [])) == 3

    # Two high-confidence directional shifts → two thesis_updates;
    # the low-confidence one is excluded.
    assert len(by_kind.get("thesis_update", [])) == 2
    thesis_topics = {p["source_shift_topic"] for p in by_kind["thesis_update"]}
    assert thesis_topics == {
        "Cloud margin commentary softened",
        "Capex pacing tightened",
    }

    # Two bear_appends: both softer shifts that named a thesis KPI.
    # Confidence gating belongs to thesis_update only.
    assert len(by_kind.get("bear_append", [])) == 2
    bear_topics = {p["source_shift_topic"] for p in by_kind["bear_append"]}
    assert bear_topics == {
        "Cloud margin commentary softened",
        "FX color qualitative",
    }


def test_draft_actions_returns_empty_when_no_material_shifts(
    fixture_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """no_material_shifts_detected=True short-circuits to []."""
    _ = _seed_five_quarters(fixture_db)
    monkeypatch.setattr(
        "triggers.earnings_tone.call_llm",
        _StatefulLLM([_diff_payload(no_shifts=True)]),
    )
    candidates = EarningsToneTrigger().scan("MELI", fixture_db)
    alert = EarningsToneTrigger().build_alert(candidates[0], None)
    assert EarningsToneTrigger().draft_actions(alert, candidates[0]) == []


def test_full_pipeline_integration_smoke(
    fixture_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Walk scan → should_fire → build_alert → draft_actions end-to-end."""
    _ = _seed_five_quarters(fixture_db)
    mock = _StatefulLLM([_diff_payload()])
    monkeypatch.setattr("triggers.earnings_tone.call_llm", mock)

    trigger = EarningsToneTrigger()
    candidates = trigger.scan("MELI", fixture_db)
    assert len(candidates) == 1

    state = UserStateContext(
        registered_kpis=[], sizing_intents=[], recent_dismissed_signatures=set()
    )
    assert trigger.should_fire(candidates[0], state) is True

    alert = trigger.build_alert(candidates[0], None)
    assert alert.ticker == "MELI"
    assert alert.memo_text

    actions = trigger.draft_actions(alert, candidates[0])
    # Two shifts in the canned default; one is adverse+KPI, one firmer no-KPI.
    # earnings_prep x 2 + thesis_update x 2 + bear_append x 1 = 5 actions.
    assert len(actions) == 5
    kinds = sorted(a.action_kind for a in actions)
    assert kinds == [
        "bear_append",
        "earnings_prep_append",
        "earnings_prep_append",
        "thesis_update",
        "thesis_update",
    ]


def test_build_alert_handles_fewer_than_four_priors(
    fixture_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Newly-tracked ticker with only 2 prior transcripts still produces an
    alert (no padding, no fake data — just fewer prior_periods)."""
    now = datetime.now(UTC).replace(tzinfo=None)
    _ = _insert_transcript(
        fixture_db,
        ticker="MELI",
        fiscal_period_type="Q3",
        period_end="2025-09-30",
        fetched_at=now - timedelta(days=180),
    )
    _ = _insert_transcript(
        fixture_db,
        ticker="MELI",
        fiscal_period_type="Q4",
        period_end="2025-12-31",
        fetched_at=now - timedelta(days=90),
    )
    current_id = _insert_transcript(
        fixture_db,
        ticker="MELI",
        fiscal_period_type="Q1",
        period_end="2026-03-31",
        fetched_at=now - timedelta(hours=2),
    )
    monkeypatch.setattr(
        "triggers.earnings_tone.call_llm",
        _StatefulLLM([_diff_payload()]),
    )
    candidates = EarningsToneTrigger().scan("MELI", fixture_db)
    alert = EarningsToneTrigger().build_alert(candidates[0], None)
    evidence = json.loads(alert.evidence_json)
    assert evidence["transcript_id"] == current_id
    assert len(evidence["prior_transcript_ids"]) == 2
