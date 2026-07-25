"""Tests for the P4 transcript longitudinal tracker
(``src/transcripts/longitudinal.py`` + ``src/transcripts/transcript_judgment.py``).

Weighted toward failure modes measured against the real book
(``docs/design/disclosure_change_build_stack.md`` P4 verification):

1. ~97% of real transcripts are single-blob aggregator pulls with speaker
   names embedded in raw text (no colon, no newline) — turn parsing must
   recover names from that shape, including multi-token names with lower-
   case particles ("Martin de los Santos"), and must degrade to
   ``parse_coverage="unattributed"`` rather than guess when no structure is
   recoverable at all.
2. Management-vs-analyst role classification is a frequency heuristic with
   no external roster — a single-question analyst must never be classified
   MANAGEMENT just because they happened to rank in the top few names of a
   short call.
3. The KPI/topic-presence candidate generator's single largest real-book
   noise source was ``kpi_definitions.definition_origin='capture'`` rows
   (auto-extracted XBRL footnote captions, ~9,600 of ~14,000 rows) and
   single generic words ("net", "gross") reducing to trivially-always-
   present "phrases" — both must be filtured out before ANY candidate is
   built, not after.
4. Both LLM judgment calls (``judge_call``, ``triage_topics``) must degrade
   to an EMPTY verdict map on ordinary failure (never a fabricated
   "answered"/"irrelevant" default for every candidate), and must
   propagate hard stops untouched.
5. ``write_transcript_events`` must be idempotent and must raise
   ``HardStopError`` (not KeyError/AttributeError) on a missing migration.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from filings.models import HardStopError  # noqa: E402
from transcripts import transcript_judgment as tj  # noqa: E402
from transcripts.longitudinal import (  # noqa: E402
    ANALYST_ROSTER_OVERLAP_FLOOR,
    RosterEntry,
    SpeakerRole,
    TranscriptEvent,
    Turn,
    _parse_aggregator_blob,  # pyright: ignore[reportPrivateUsage]  # internal seam under test
    _parse_whisper_blob,  # pyright: ignore[reportPrivateUsage]  # internal seam under test
    _split_kpi_phrases,  # pyright: ignore[reportPrivateUsage]  # internal seam under test
    classify_roles,
    extract_kpi_terms,
    find_kpi_mention,
    jaccard,
    load_call_turns,
    nearest_earnings_surprise,
    pair_qa_exchanges,
    resolve_exec_role,
    roster_names_by_role,
    tone_shift_is_abnormal,
    write_transcript_events,
)
from transcripts.transcript_judgment import build_qa_judgment_prompt, judge_call  # noqa: E402

_DISCLOSURE_EVENTS_SCHEMA = """
CREATE TABLE disclosure_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    event_type VARCHAR(32) NOT NULL,
    form VARCHAR(8),
    fiscal_year INTEGER,
    fiscal_period VARCHAR(4),
    prior_fiscal_year INTEGER,
    prior_fiscal_period VARCHAR(4),
    source_ref VARCHAR(255),
    source_doc_id INTEGER,
    canonical_id VARCHAR(64),
    subject VARCHAR(255) NOT NULL,
    subject_label TEXT,
    prior_excerpt TEXT,
    current_excerpt TEXT,
    evidence_quote TEXT,
    materiality FLOAT,
    verdict VARCHAR(24) NOT NULL DEFAULT 'unclassified',
    interpretation_md TEXT,
    confidence FLOAT,
    detector_version VARCHAR(32) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'new',
    created_at DATETIME NOT NULL,
    CONSTRAINT uq_disclosure_events UNIQUE
        (ticker, event_type, fiscal_year, fiscal_period, subject, detector_version)
);
"""


def _events_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_DISCLOSURE_EVENTS_SCHEMA)
    return conn


def _full_conn(tmp_path: Path) -> sqlite3.Connection:
    """A connection with transcripts/transcript_segments/kpi_definitions/
    exec_comp_packages/earnings_surprises — enough to exercise every
    read path in longitudinal.py against a real (small, synthetic) schema."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE transcripts (
            id INTEGER PRIMARY KEY, ticker TEXT, fiscal_period_type TEXT, period_end TEXT
        );
        CREATE TABLE transcript_segments (
            id INTEGER PRIMARY KEY, transcript_id INTEGER, seq INTEGER,
            speaker TEXT, speaker_role TEXT, text TEXT
        );
        CREATE TABLE kpi_definitions (
            id INTEGER PRIMARY KEY, ticker TEXT, name TEXT, unit TEXT,
            definition_origin TEXT DEFAULT 'analyst'
        );
        CREATE TABLE exec_comp_packages (
            id INTEGER PRIMARY KEY, ticker TEXT, executive_name TEXT, role TEXT,
            is_ceo INTEGER DEFAULT 0
        );
        CREATE TABLE earnings_surprises (
            id INTEGER PRIMARY KEY, ticker TEXT, release_date TEXT, eps_surprise_pct REAL
        );
        """
    )
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Aggregator blob turn parsing (roic.ai "<Letter> <Name>" convention)
# ---------------------------------------------------------------------------

_MELI_LIKE_BLOB = """=== SYNTHESIZED QUARTERLY UPDATE — Q&A SEGMENT ONLY ===
Generated by execution/fetch_qa_transcript.py from a free aggregator.

Ticker:    MELI
Period:    Q1 2025

=== Q&A SEGMENT ===
begin the question-and-answer session. [Operator Instructions] And the first question will come from Andrew Ruben with Morgan Stanley. Please go ahead.

A Andrew Ruben Hey, thanks for taking my question. How is Argentina trending?

A Ariel Szarfsztejn Hey Andrew, this is Ariel. Argentina had a great quarter.

M Martin de los Santos And just to complement, Andrew, we saw margin expansion too.

Operator Our next question comes from Kaio Prato with Bank of America.

B Kaio Prato Can you comment on take rate trends?

A Ariel Szarfsztejn Sure, take rate improved sequentially.
"""


def test_aggregator_blob_recovers_speaker_names() -> None:
    turns = _parse_aggregator_blob(_MELI_LIKE_BLOB)
    speakers = [t.speaker for t in turns]
    assert "Operator" in speakers
    assert "Andrew Ruben" in speakers
    assert "Ariel Szarfsztejn" in speakers
    # Multi-token name with a lowercase particle ("de los") must be
    # recovered whole, not truncated at "Martin".
    assert "Martin de los Santos" in speakers


def test_aggregator_blob_stops_name_capture_at_prose() -> None:
    turns = _parse_aggregator_blob(_MELI_LIKE_BLOB)
    martin_turns = [t for t in turns if t.speaker == "Martin de los Santos"]
    assert martin_turns
    # "And just to complement..." must be BODY text, not swallowed into the
    # captured name.
    assert martin_turns[0].text.startswith("And just to complement")


def test_whisper_blob_parses_anonymized_speakers() -> None:
    text = (
        "Speaker 1 (00:06:15): Good afternoon everyone, welcome to the call.\n"
        "Speaker 2 (00:07:02): Thanks, revenue grew nicely this quarter.\n"
        "Speaker 3 (00:08:40): Can you unpack the margin drivers?\n"
    )
    turns = _parse_whisper_blob(text)
    assert [t.speaker for t in turns] == ["Speaker 1", "Speaker 2", "Speaker 3"]
    assert turns[1].text.startswith("Thanks, revenue grew")


def test_unstructured_blob_degrades_to_unattributed(tmp_path: Path) -> None:
    conn = _full_conn(tmp_path)
    conn.execute(
        "INSERT INTO transcripts (id, ticker, fiscal_period_type, period_end) "
        "VALUES (1, 'ZZZ', 'Q1', '2025-03-31')"
    )
    conn.execute(
        "INSERT INTO transcript_segments (transcript_id, seq, speaker, text) "
        "VALUES (1, 0, NULL, 'just a wall of prose with no speaker markers at all whatsoever')"
    )
    conn.commit()
    turns, coverage = load_call_turns(conn, 1)
    assert coverage == "unattributed"
    assert len(turns) == 1
    assert turns[0].speaker is None


def test_two_or_more_stored_segments_trusted_verbatim(tmp_path: Path) -> None:
    conn = _full_conn(tmp_path)
    conn.execute(
        "INSERT INTO transcripts (id, ticker, fiscal_period_type, period_end) "
        "VALUES (1, 'ZZZ', 'Q1', '2025-03-31')"
    )
    conn.execute(
        "INSERT INTO transcript_segments (transcript_id, seq, speaker, text) VALUES (1, 0, 'Operator', 'welcome')"
    )
    conn.execute(
        "INSERT INTO transcript_segments (transcript_id, seq, speaker, text) VALUES (1, 1, 'Jane CEO', 'thanks all')"
    )
    conn.commit()
    turns, coverage = load_call_turns(conn, 1)
    assert coverage == "segmented"
    assert [t.speaker for t in turns] == ["Operator", "Jane CEO"]


# ---------------------------------------------------------------------------
# Role classification — frequency heuristic, no external roster required
# ---------------------------------------------------------------------------


def test_single_question_analyst_not_classified_management() -> None:
    turns = [
        Turn(seq=0, speaker="Operator", text="first question from Alice Analyst"),
        Turn(seq=1, speaker="Alice Analyst", text="What is your outlook?"),
        Turn(seq=2, speaker="Bob CEO", text="Outlook is strong."),
        Turn(seq=3, speaker="Operator", text="next question from Carol Analyst"),
        Turn(seq=4, speaker="Carol Analyst", text="Any color on margins?"),
        Turn(seq=5, speaker="Bob CEO", text="Margins improving."),
        Turn(seq=6, speaker="Operator", text="next question from Dave Analyst"),
        Turn(seq=7, speaker="Dave Analyst", text="Capex plans?"),
        Turn(seq=8, speaker="Bob CEO", text="Capex steady."),
    ]
    roster = classify_roles(turns)
    assert roster["Bob CEO"].role is SpeakerRole.MANAGEMENT
    assert roster["Alice Analyst"].role is SpeakerRole.ANALYST
    assert roster["Carol Analyst"].role is SpeakerRole.ANALYST
    assert roster["Dave Analyst"].role is SpeakerRole.ANALYST
    assert roster["Operator"].role is SpeakerRole.OPERATOR


def test_exec_comp_join_resolves_ceo_when_populated(tmp_path: Path) -> None:
    conn = _full_conn(tmp_path)
    conn.execute(
        "INSERT INTO exec_comp_packages (ticker, executive_name, role, is_ceo) "
        "VALUES ('ZZZ', 'Robert Ford', 'CEO', 1)"
    )
    conn.commit()
    assert resolve_exec_role(conn, "ZZZ", "Robert Ford") == "CEO"


def test_exec_comp_join_returns_none_when_table_empty(tmp_path: Path) -> None:
    """The real book's exec_comp_packages is empty for every ticker as of
    this writing — resolve_exec_role must return None (honest degrade),
    never a guessed role."""
    conn = _full_conn(tmp_path)
    assert resolve_exec_role(conn, "ZZZ", "Robert Ford") is None


def test_analyst_who_asks_several_followups_not_promoted_to_management() -> None:
    """Regression for a real NU miscall found during verification: an
    analyst introduced by name ("please open the line for Jorge Kuri from
    Morgan Stanley") who then asks 3 follow-ups in one call ranked high
    enough by raw frequency to get misclassified MANAGEMENT. The explicit
    introduction must win regardless of how many follow-ups they ask."""
    turns = [
        Turn(
            seq=0,
            speaker="Guilherme Souto",
            text="Please open the line for Jorge Kuri from Morgan Stanley.",
        ),
        Turn(seq=1, speaker="Jorge Kuri", text="First question about margins."),
        Turn(seq=2, speaker="Guilherme Lago", text="Margins are strong."),
        Turn(seq=3, speaker="Jorge Kuri", text="Follow-up on that."),
        Turn(seq=4, speaker="Guilherme Lago", text="Sure, here is more detail."),
        Turn(seq=5, speaker="Jorge Kuri", text="One more follow-up."),
        Turn(seq=6, speaker="Guilherme Lago", text="Happy to clarify."),
    ]
    roster = classify_roles(turns)
    assert roster["Jorge Kuri"].role is SpeakerRole.ANALYST
    assert roster["Guilherme Lago"].role is SpeakerRole.MANAGEMENT


def test_introduction_with_honorific_prefix_still_matches() -> None:
    """ "Mr./Ms./Mrs./Dr." before the name must not blank out the extracted
    name (the trailing period breaks the bare name-token regex)."""
    turns = [
        Turn(
            seq=0,
            speaker="Guilherme Souto",
            text="could you please open the line for Mr. Mario Pierry from Bank of America.",
        ),
        Turn(seq=1, speaker="Mario Pierry", text="Question about credit quality."),
        Turn(seq=2, speaker="Guilherme Lago", text="Credit quality is stable."),
        Turn(seq=3, speaker="Mario Pierry", text="Follow-up."),
        Turn(seq=4, speaker="Guilherme Lago", text="More detail here."),
    ]
    roster = classify_roles(turns)
    assert roster["Mario Pierry"].role is SpeakerRole.ANALYST


# ---------------------------------------------------------------------------
# Q&A pairing
# ---------------------------------------------------------------------------


def _roster(names_roles: dict[str, SpeakerRole]) -> dict[str, RosterEntry]:
    return {
        name: RosterEntry(name=name, role=role, turn_count=1, total_chars=10)
        for name, role in names_roles.items()
    }


def test_multiple_management_speakers_merge_into_one_answer() -> None:
    roster = _roster(
        {
            "Operator": SpeakerRole.OPERATOR,
            "Alice": SpeakerRole.ANALYST,
            "Bob": SpeakerRole.MANAGEMENT,
            "Carol": SpeakerRole.MANAGEMENT,
        }
    )
    turns = [
        Turn(seq=0, speaker="Alice", text="How is Argentina?"),
        Turn(seq=1, speaker="Bob", text="Great quarter."),
        Turn(seq=2, speaker="Carol", text="And margin expanded too."),
    ]
    exchanges = pair_qa_exchanges(turns, roster)
    assert len(exchanges) == 1
    assert exchanges[0].analyst_name == "Alice"
    assert exchanges[0].answer_speakers == ("Bob", "Carol")
    assert "Great quarter" in exchanges[0].answer_text
    assert "margin expanded" in exchanges[0].answer_text


def test_followup_question_by_same_analyst_creates_new_exchange() -> None:
    roster = _roster({"Alice": SpeakerRole.ANALYST, "Bob": SpeakerRole.MANAGEMENT})
    turns = [
        Turn(seq=0, speaker="Alice", text="First question."),
        Turn(seq=1, speaker="Bob", text="First answer."),
        Turn(seq=2, speaker="Alice", text="Follow-up question."),
        Turn(seq=3, speaker="Bob", text="Follow-up answer."),
    ]
    exchanges = pair_qa_exchanges(turns, roster)
    assert len(exchanges) == 2
    assert exchanges[0].question_text == "First question."
    assert exchanges[1].question_text == "Follow-up question."


def test_unknown_speaker_turns_never_guessed_onto_either_side() -> None:
    roster = _roster({"Alice": SpeakerRole.ANALYST, "Bob": SpeakerRole.MANAGEMENT})
    turns = [
        Turn(seq=0, speaker=None, text="unattributed noise"),
        Turn(seq=1, speaker="Alice", text="Question?"),
        Turn(seq=2, speaker="Bob", text="Answer."),
    ]
    exchanges = pair_qa_exchanges(turns, roster)
    assert len(exchanges) == 1
    assert "unattributed noise" not in exchanges[0].question_text
    assert "unattributed noise" not in exchanges[0].answer_text


def test_management_speaking_with_no_open_question_is_ignored() -> None:
    roster = _roster({"Bob": SpeakerRole.MANAGEMENT})
    turns = [Turn(seq=0, speaker="Bob", text="opening remarks with no analyst yet")]
    assert pair_qa_exchanges(turns, roster) == []


# ---------------------------------------------------------------------------
# KPI/topic phrase extraction — the real-book noise source
# ---------------------------------------------------------------------------


def test_semicolon_joined_kpi_name_splits_into_multiple_phrases() -> None:
    phrases = _split_kpi_phrases("GMV; items sold; TPV; fintech MAU; credit NPL; take rate")
    assert "GMV" in phrases
    assert "take rate" in phrases
    assert "fintech MAU" in phrases


def test_generic_single_word_clause_dropped() -> None:
    phrases = _split_kpi_phrases("Property and equipment, Net")
    assert "net" not in [p.lower() for p in phrases]
    assert any("Property and equipment" in p for p in phrases)


def test_formula_like_name_filtered_by_length() -> None:
    phrases = _split_kpi_phrases("Revenue YoY Growth Deceleration (YoY of YoY growth rate)")
    # The parenthetical is stripped; the remaining clause is long/rare in
    # spoken language but still under the length cap, so it may survive —
    # the CONTRACT under test is that it is not silently duplicated by the
    # stripped parenthetical content leaking back in.
    assert all("YoY of YoY" not in p for p in phrases)


def test_extract_kpi_terms_excludes_capture_origin_rows(tmp_path: Path) -> None:
    conn = _full_conn(tmp_path)
    conn.execute(
        "INSERT INTO kpi_definitions (ticker, name, definition_origin) "
        "VALUES ('ZZZ', 'GMV; take rate', 'analyst')"
    )
    conn.execute(
        "INSERT INTO kpi_definitions (ticker, name, definition_origin) "
        "VALUES ('ZZZ', 'BALANCE SHEET COMPONENTS - Schedule of Accounts Receivable, Net', 'capture')"
    )
    conn.commit()
    terms = extract_kpi_terms(conn, "ZZZ")
    names = [name for name, _ in terms]
    assert "GMV; take rate" in names
    assert not any("BALANCE SHEET COMPONENTS" in n for n in names)


def test_find_kpi_mention_returns_excerpt_or_none() -> None:
    text = "Analysts asked about take rate trends across the quarter."
    assert find_kpi_mention(text, ["take rate"]) is not None
    assert find_kpi_mention(text, ["fintech MAU"]) is None


# ---------------------------------------------------------------------------
# Earnings-surprise join
# ---------------------------------------------------------------------------


def test_nearest_earnings_surprise_within_window(tmp_path: Path) -> None:
    conn = _full_conn(tmp_path)
    conn.execute(
        "INSERT INTO earnings_surprises (ticker, release_date, eps_surprise_pct) "
        "VALUES ('ZZZ', '2025-04-20', 12.5)"
    )
    conn.commit()
    info = nearest_earnings_surprise(conn, "ZZZ", datetime(2025, 3, 31))
    assert info is not None
    assert info.eps_surprise_pct == 12.5


def test_nearest_earnings_surprise_none_outside_window(tmp_path: Path) -> None:
    conn = _full_conn(tmp_path)
    conn.execute(
        "INSERT INTO earnings_surprises (ticker, release_date, eps_surprise_pct) "
        "VALUES ('ZZZ', '2026-01-01', 12.5)"
    )
    conn.commit()
    assert nearest_earnings_surprise(conn, "ZZZ", datetime(2025, 3, 31)) is None


# ---------------------------------------------------------------------------
# Tone-shift proxy (documented NOT to be a fitted OLS residual)
# ---------------------------------------------------------------------------


def test_tone_shift_below_materiality_is_not_abnormal() -> None:
    assert tone_shift_is_abnormal(0.1, 10.0) is False


def test_tone_shift_opposite_sign_of_surprise_is_abnormal() -> None:
    assert tone_shift_is_abnormal(-0.5, 15.0) is True  # tone dropped despite a beat


def test_tone_shift_large_move_on_small_surprise_is_abnormal() -> None:
    assert tone_shift_is_abnormal(0.5, 0.5) is True  # big move, in-line results


def test_tone_shift_same_sign_material_surprise_not_abnormal() -> None:
    assert tone_shift_is_abnormal(0.5, 20.0) is False  # tone up, big beat — consistent


def test_tone_shift_no_surprise_data_still_surfaces() -> None:
    # No control variable available — still surfaced (as "unexplained"),
    # never silently dropped.
    assert tone_shift_is_abnormal(0.5, None) is True


# ---------------------------------------------------------------------------
# Roster helpers
# ---------------------------------------------------------------------------


def test_jaccard_identical_sets_is_one() -> None:
    assert jaccard({"a", "b"}, {"a", "b"}) == 1.0


def test_jaccard_disjoint_sets_is_zero() -> None:
    assert jaccard({"a"}, {"b"}) == 0.0
    assert jaccard({"a"}, {"b"}) < ANALYST_ROSTER_OVERLAP_FLOOR


def test_roster_names_by_role_filters_correctly() -> None:
    roster = _roster({"Alice": SpeakerRole.ANALYST, "Bob": SpeakerRole.MANAGEMENT})
    assert roster_names_by_role(roster, SpeakerRole.ANALYST) == {"Alice"}
    assert roster_names_by_role(roster, SpeakerRole.MANAGEMENT) == {"Bob"}


# ---------------------------------------------------------------------------
# disclosure_events writer
# ---------------------------------------------------------------------------


def _sample_event(**overrides: object) -> TranscriptEvent:
    base = TranscriptEvent(
        ticker="ZZZ",
        event_type="qa_nonanswer_rate_shift",
        fiscal_year=2025,
        fiscal_period="Q1",
        subject="qa_nonanswer_rate",
        evidence_quote="Q: ... A: ...",
    )
    return base.model_copy(update=overrides)


def test_write_transcript_events_idempotent() -> None:
    conn = _events_conn()
    try:
        n1 = write_transcript_events(conn, [_sample_event(materiality=20.0)])
        n2 = write_transcript_events(conn, [_sample_event(materiality=25.0)])
        assert n1 == 1
        assert n2 == 1
        rows = conn.execute("SELECT materiality FROM disclosure_events").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 25.0
    finally:
        conn.close()


def test_write_transcript_events_missing_table_raises_hard_stop() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        with pytest.raises(HardStopError):
            write_transcript_events(conn, [_sample_event()])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# LLM judgment — degrade honestly, propagate hard stops
# ---------------------------------------------------------------------------


def _snapshot_with_one_exchange(tmp_path: Path):
    from transcripts.longitudinal import CallSnapshot, QAExchange

    return CallSnapshot(
        transcript_id=1,
        ticker="ZZZ",
        fiscal_period_type="Q1",
        fiscal_year=2025,
        period_end=datetime(2025, 3, 31),
        turns=[],
        roster={},
        exchanges=[
            QAExchange(
                index=0,
                analyst_name="Alice",
                question_text="What is your outlook?",
                answer_speakers=("Bob",),
                answer_text="We are confident.",
            )
        ],
        parse_coverage="reparsed_aggregator",
    )


def test_judge_call_degrades_on_llm_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("simulated transient LLM failure")

    monkeypatch.setattr(tj, "call_llm_structured", _boom)
    snap = _snapshot_with_one_exchange(tmp_path)
    result = judge_call(snap, db_path=tmp_path / "nonexistent.db")
    assert result.degraded is True
    assert result.questions == {}
    assert result.tone == {}


def test_judge_call_propagates_hard_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from llm.cli import LLMSetupError

    def _boom(*args: object, **kwargs: object) -> object:
        raise LLMSetupError("claude CLI not found")

    monkeypatch.setattr(tj, "call_llm_structured", _boom)
    snap = _snapshot_with_one_exchange(tmp_path)
    with pytest.raises(LLMSetupError):
        judge_call(snap, db_path=tmp_path / "nonexistent.db")


def test_judge_call_no_exchanges_short_circuits_without_llm_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("must not call the LLM when there are no Q&A exchanges")

    monkeypatch.setattr(tj, "call_llm_structured", _boom)
    from transcripts.longitudinal import CallSnapshot

    snap = CallSnapshot(
        transcript_id=1,
        ticker="ZZZ",
        fiscal_period_type="Q1",
        fiscal_year=2025,
        period_end=datetime(2025, 3, 31),
        turns=[],
        roster={},
        exchanges=[],
        parse_coverage="unattributed",
    )
    result = judge_call(snap, db_path=tmp_path / "nonexistent.db")
    assert result.questions == {}
    assert result.degraded is False


def test_judge_call_parses_valid_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake(*args: object, **kwargs: object) -> object:
        return {
            "questions": {"0": {"non_answer": True, "rationale": "dodged the question"}},
            "tone": {"Bob": {"score": 0.4, "rationale": "confident phrasing"}},
        }

    monkeypatch.setattr(tj, "call_llm_structured", _fake)
    snap = _snapshot_with_one_exchange(tmp_path)
    result = judge_call(snap, db_path=tmp_path / "nonexistent.db")
    assert result.degraded is False
    assert result.questions["0"].non_answer is True
    assert result.tone["Bob"].score == pytest.approx(0.4)


def test_triage_topics_degrades_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("simulated transient LLM failure")

    monkeypatch.setattr(tj, "call_llm_structured", _boom)
    outcome = tj.triage_topics("ZZZ", ["GMV", "take rate"])
    assert outcome.degraded is True
    assert outcome.verdicts == {}


def test_triage_topics_empty_candidates_skips_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("must not call the LLM with zero candidates")

    monkeypatch.setattr(tj, "call_llm_structured", _boom)
    outcome = tj.triage_topics("ZZZ", [])
    assert outcome.verdicts == {}
    assert outcome.degraded is False


def test_build_qa_judgment_prompt_never_leaks_more_than_max_exchanges() -> None:
    from transcripts.longitudinal import CallSnapshot, QAExchange

    many = [
        QAExchange(
            index=i,
            analyst_name=f"A{i}",
            question_text="q" * 10,
            answer_speakers=("Bob",),
            answer_text="a" * 10,
        )
        for i in range(50)
    ]
    snap = CallSnapshot(
        transcript_id=1,
        ticker="ZZZ",
        fiscal_period_type="Q1",
        fiscal_year=2025,
        period_end=datetime(2025, 3, 31),
        turns=[],
        roster={},
        exchanges=many,
        parse_coverage="reparsed_aggregator",
    )
    _prompt, n_included = build_qa_judgment_prompt(snap)
    assert n_included <= 30
    assert n_included < len(many)
