"""The behavioral-rules distiller (tenet-2 Phase 4) — deterministic $0 triage,
citation-validation hallucination guard (drops uncited/miscited candidates),
tension/supersede staging, never-auto-affirm, and degrade-safety. All with an
injected ``call`` (no CLI, no live LLM).
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from owner_profile.store import get_current_profile, list_facts  # noqa: E402
from synthesis.behavior_distill import (  # noqa: E402
    ProposedRule,
    graded_decision_corpus,
    run_behavior_distill,
)

PRIOR_HEAD = "0059_kpi_facts_restatement"

_DECISIONS_DDL = """
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16),
    recommendation_kind VARCHAR(32) NOT NULL,
    conviction VARCHAR(16),
    outcome_label VARCHAR(16) NOT NULL DEFAULT 'pending',
    process_quality VARCHAR(16),
    decided_by VARCHAR(16) NOT NULL DEFAULT 'advisor',
    scope VARCHAR(16) NOT NULL DEFAULT 'ticker',
    falsifier TEXT,
    size_usd FLOAT,
    rationale_excerpt TEXT,
    user_notes TEXT,
    made_at DATETIME NOT NULL,
    created_at DATETIME NOT NULL
);
"""


@pytest.fixture
def db_path(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    db = tmp_path / "portfolio.db"
    migrated_db(db, stamp=PRIOR_HEAD, archived=True, reanchor_to_active_head=True)
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(_DECISIONS_DDL)
        conn.commit()
    finally:
        conn.close()
    return db


def _insert_decision(
    db_path: Path,
    *,
    ticker: str,
    recommendation_kind: str = "sell",
    outcome_label: str = "wrong",
    decided_by: str = "owner",
    made_at: str = "2026-06-01",
) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, outcome_label, decided_by, "
            "made_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (ticker, recommendation_kind, outcome_label, decided_by, made_at, made_at),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def _cite(decision_id: int, outcome: str) -> dict[str, object]:
    return {"decision_id": decision_id, "outcome": outcome}


# --------------------------------------------------------------------------- #
# graded_decision_corpus + $0 short-circuit
# --------------------------------------------------------------------------- #


def test_corpus_empty_on_missing_db(tmp_path: Path) -> None:
    assert graded_decision_corpus(tmp_path / "missing.db") == []
    assert graded_decision_corpus(None) == []


def test_distill_zero_cost_when_no_graded_decisions(db_path: Path) -> None:
    _insert_decision(db_path, ticker="RBRK", outcome_label="pending")
    calls = {"n": 0}

    def spy(_prompt: str) -> list[ProposedRule] | None:
        calls["n"] += 1
        return []

    counts = run_behavior_distill(db_path, call=spy)
    assert counts["candidates"] == 0
    assert calls["n"] == 0  # deterministic $0 short-circuit — the model never ran


def test_corpus_excludes_non_owner_and_pending_rows(db_path: Path) -> None:
    _insert_decision(db_path, ticker="MU", decided_by="owner", outcome_label="wrong")
    _insert_decision(db_path, ticker="TSM", decided_by="advisor", outcome_label="wrong")
    _insert_decision(db_path, ticker="NVDA", decided_by="owner", outcome_label="pending")
    corpus = graded_decision_corpus(db_path)
    assert [c.ticker for c in corpus] == ["MU"]


# --------------------------------------------------------------------------- #
# Citation-validation hallucination guard
# --------------------------------------------------------------------------- #


def test_valid_citations_stage_a_proposed_rule(db_path: Path) -> None:
    a = _insert_decision(db_path, ticker="MU", outcome_label="wrong")
    b = _insert_decision(db_path, ticker="TSM", outcome_label="wrong")

    def call(_prompt: str) -> list[ProposedRule]:
        return [
            {
                "key": "behavior.sell_winners_early",
                "rule_text": "You sell your winners too early.",
                "citations": [_cite(a, "wrong"), _cite(b, "wrong")],
            }
        ]

    counts = run_behavior_distill(db_path, call=call)
    assert counts["proposed"] == 1
    assert counts["skipped_uncited"] == 0
    facts = list_facts(sqlite3.connect(str(db_path)), category="behavioral")
    assert len(facts) == 1
    assert facts[0].status == "proposed"  # NEVER auto-affirmed
    assert facts[0].value["wrong"] == 2
    assert facts[0].value["total"] == 2
    assert sorted(cast("list[int]", facts[0].value["citations"])) == sorted([a, b])


def test_citation_outside_corpus_is_dropped(db_path: Path) -> None:
    a = _insert_decision(db_path, ticker="MU", outcome_label="wrong")

    def call(_prompt: str) -> list[ProposedRule]:
        return [
            {
                "key": "behavior.sell_winners_early",
                "rule_text": "You sell your winners too early.",
                "citations": [_cite(a, "wrong"), _cite(999_999, "wrong")],
            }
        ]

    counts = run_behavior_distill(db_path, call=call)
    assert counts["proposed"] == 1
    conn = sqlite3.connect(str(db_path))
    facts = list_facts(conn, category="behavioral")
    assert facts[0].value["citations"] == [a]  # the hallucinated id is dropped
    assert facts[0].value["total"] == 1


def test_citation_with_wrong_claimed_outcome_is_dropped(db_path: Path) -> None:
    a = _insert_decision(db_path, ticker="MU", outcome_label="wrong")
    b = _insert_decision(db_path, ticker="TSM", outcome_label="correct")

    def call(_prompt: str) -> list[ProposedRule]:
        return [
            {
                "key": "behavior.sell_winners_early",
                "rule_text": "You sell your winners too early.",
                # b is actually graded 'correct' -- the model's claim of 'wrong' is a
                # miscitation and must be dropped, not trusted.
                "citations": [_cite(a, "wrong"), _cite(b, "wrong")],
            }
        ]

    counts = run_behavior_distill(db_path, call=call)
    assert counts["proposed"] == 1
    facts = list_facts(sqlite3.connect(str(db_path)), category="behavioral")
    assert facts[0].value["citations"] == [a]
    assert facts[0].value["wrong"] == 1
    assert facts[0].value["total"] == 1


def test_citation_outcome_tolerates_a_decorated_phrase(db_path: Path) -> None:
    # Real dry-run finding: a model asked for "the graded outcome_label,
    # verbatim" instead wrote "graded wrong" / "graded mixed" -- a decorated
    # phrase around the bare label, not a genuinely different claim. The
    # validator must still recognize the real label inside the phrase (never
    # silently accept an outcome that ISN'T actually in the phrase).
    a = _insert_decision(db_path, ticker="MU", outcome_label="wrong")
    b = _insert_decision(db_path, ticker="TSM", outcome_label="mixed")

    def call(_prompt: str) -> list[ProposedRule]:
        return [
            {
                "key": "behavior.sell_winners_early",
                "rule_text": "You sell your winners too early.",
                "citations": [
                    _cite(a, "graded wrong"),
                    _cite(b, "the outcome was mixed"),
                ],
            }
        ]

    counts = run_behavior_distill(db_path, call=call)
    assert counts["proposed"] == 1
    facts = list_facts(sqlite3.connect(str(db_path)), category="behavioral")
    assert sorted(cast("list[int]", facts[0].value["citations"])) == sorted([a, b])
    assert facts[0].value["wrong"] == 1
    assert facts[0].value["total"] == 2


def test_citation_outcome_ambiguous_decorated_phrase_is_dropped(db_path: Path) -> None:
    a = _insert_decision(db_path, ticker="MU", outcome_label="wrong")

    def call(_prompt: str) -> list[ProposedRule]:
        return [
            {
                "key": "behavior.sell_winners_early",
                "rule_text": "You sell your winners too early.",
                # Contains two distinct labels -- ambiguous, never guessed.
                "citations": [_cite(a, "not correct, actually wrong")],
            }
        ]

    counts = run_behavior_distill(db_path, call=call)
    assert counts["proposed"] == 0
    assert counts["skipped_uncited"] == 1


def test_rule_with_zero_valid_citations_is_skipped_entirely(db_path: Path) -> None:
    _insert_decision(db_path, ticker="MU", outcome_label="wrong")

    def call(_prompt: str) -> list[ProposedRule]:
        return [
            {
                "key": "behavior.groundless",
                "rule_text": "A rule with no real citations.",
                "citations": [_cite(999_999, "wrong")],
            }
        ]

    counts = run_behavior_distill(db_path, call=call)
    assert counts["proposed"] == 0
    assert counts["skipped_uncited"] == 1
    assert list_facts(sqlite3.connect(str(db_path)), category="behavioral") == []


def test_rule_with_empty_text_is_skipped(db_path: Path) -> None:
    a = _insert_decision(db_path, ticker="MU", outcome_label="wrong")

    def call(_prompt: str) -> list[ProposedRule]:
        return [{"key": "behavior.empty", "rule_text": "  ", "citations": [_cite(a, "wrong")]}]

    counts = run_behavior_distill(db_path, call=call)
    assert counts["proposed"] == 0
    assert counts["skipped_uncited"] == 1


def test_malformed_citation_entries_are_dropped_not_crashing(db_path: Path) -> None:
    a = _insert_decision(db_path, ticker="MU", outcome_label="wrong")

    def call(_prompt: str) -> list[ProposedRule]:
        return [
            {
                "key": "behavior.sell_winners_early",
                "rule_text": "You sell your winners too early.",
                "citations": [
                    _cite(a, "wrong"),
                    "not-a-dict",
                    {"decision_id": "not-an-int", "outcome": "wrong"},
                    {"decision_id": a},  # missing outcome
                    None,
                ],
            }
        ]

    counts = run_behavior_distill(db_path, call=call)
    assert counts["proposed"] == 1
    facts = list_facts(sqlite3.connect(str(db_path)), category="behavioral")
    assert facts[0].value["citations"] == [a]


# --------------------------------------------------------------------------- #
# Tension / supersede staging + never-auto-affirm
# --------------------------------------------------------------------------- #


def test_matching_key_against_affirmed_fact_stages_as_tension(db_path: Path) -> None:
    from owner_profile.store import append_fact

    a = _insert_decision(db_path, ticker="MU", outcome_label="wrong")
    conn = sqlite3.connect(str(db_path))
    try:
        append_fact(
            conn,
            category="behavioral",
            key="behavior.sell_winners_early",
            value={},
            narrative="An existing, owner-affirmed rule.",
            provenance="owner",
            status="affirmed",
        )
        conn.commit()
    finally:
        conn.close()

    def call(_prompt: str) -> list[ProposedRule]:
        return [
            {
                "key": "behavior.sell_winners_early",
                "rule_text": "A freshly re-derived version of the same rule.",
                "citations": [_cite(a, "wrong")],
            }
        ]

    counts = run_behavior_distill(db_path, call=call)
    assert counts["proposed"] == 1
    assert counts["tensions"] == 1
    conn = sqlite3.connect(str(db_path))
    grouped = get_current_profile(conn)
    # The new row supersedes but is only 'proposed' -- affirmed profile no
    # longer carries this key until the owner re-ratifies (mirrors a Tier A
    # re-import over an affirmed fact -- the standing store contract).
    assert not any(r.key == "behavior.sell_winners_early" for r in grouped["behavioral"])
    all_rows = list_facts(conn, category="behavioral")
    latest = next(r for r in all_rows if r.key == "behavior.sell_winners_early")
    assert latest.status == "proposed"
    assert latest.superseded_by_id is None or latest.id != latest.superseded_by_id


def test_no_tension_when_no_prior_affirmed_fact(db_path: Path) -> None:
    a = _insert_decision(db_path, ticker="MU", outcome_label="wrong")

    def call(_prompt: str) -> list[ProposedRule]:
        return [
            {
                "key": "behavior.sell_winners_early",
                "rule_text": "You sell your winners too early.",
                "citations": [_cite(a, "wrong")],
            }
        ]

    counts = run_behavior_distill(db_path, call=call)
    assert counts["proposed"] == 1
    assert counts["tensions"] == 0


def test_rerun_with_identical_proposal_is_idempotent_noop(db_path: Path) -> None:
    a = _insert_decision(db_path, ticker="MU", outcome_label="wrong")

    def call(_prompt: str) -> list[ProposedRule]:
        return [
            {
                "key": "behavior.sell_winners_early",
                "rule_text": "You sell your winners too early.",
                "citations": [_cite(a, "wrong")],
            }
        ]

    run_behavior_distill(db_path, call=call)
    run_behavior_distill(db_path, call=call)  # identical value+narrative -> no new row
    facts = list_facts(sqlite3.connect(str(db_path)), category="behavioral")
    assert len(facts) == 1


# --------------------------------------------------------------------------- #
# Degrade-safety
# --------------------------------------------------------------------------- #


def test_empty_proposal_list_leaves_profile_untouched(db_path: Path) -> None:
    _insert_decision(db_path, ticker="MU", outcome_label="wrong")
    counts = run_behavior_distill(db_path, call=lambda _p: [])
    assert counts["proposed"] == 0
    assert list_facts(sqlite3.connect(str(db_path)), category="behavioral") == []


def test_none_result_leaves_profile_untouched(db_path: Path) -> None:
    _insert_decision(db_path, ticker="MU", outcome_label="wrong")
    counts = run_behavior_distill(db_path, call=lambda _p: None)
    assert counts["proposed"] == 0


def test_transient_call_failure_defers_and_tallies(db_path: Path) -> None:
    _insert_decision(db_path, ticker="MU", outcome_label="wrong")

    def boom(_prompt: str) -> list[ProposedRule]:
        raise RuntimeError("llm subprocess timed out")

    counts = run_behavior_distill(db_path, call=boom)  # never raises
    assert counts["deferred_transient"] == 1
    assert counts["proposed"] == 0
    assert list_facts(sqlite3.connect(str(db_path)), category="behavioral") == []


def test_hard_stop_propagates(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _insert_decision(db_path, ticker="MU", outcome_label="wrong")

    class _FakeHardStopError(RuntimeError):
        pass

    def boom(_prompt: str) -> list[ProposedRule]:
        raise _FakeHardStopError("budget exceeded")

    import llm.cli as llm_cli

    def fake_is_hard_stop(exc: BaseException) -> bool:
        return isinstance(exc, _FakeHardStopError)

    monkeypatch.setattr(llm_cli, "is_hard_stop", fake_is_hard_stop)
    with pytest.raises(_FakeHardStopError):
        run_behavior_distill(db_path, call=boom)
