"""Tests for the weekly Tenet-accountability pass (B5, src/synthesis/tenet_accountability.py).

Builds a fully alembic-migrated DB (every real migration executes, in order,
against the bootstrap tables ``db.py``'s ``init_db()`` normally creates —
same pattern as ``tests/test_decision_journal_view.py``) so both ``decisions``
(alembic 0046, extended by 0130) and ``v_decision_journal`` (alembic 0179)
exist for real — a stamp-past-a-mid-chain-revision fixture (the
``session_distill``/``tenet_distill`` tests' shortcut) silently SKIPS the
``decisions`` table's creation, since 0046 predates their PRIOR_HEAD.

Every LLM call is an injected stub (zero live LLM) and the tracker is
explicitly monkeypatched to fail wherever a test cares about its behavior,
so no test ever makes a real network call.

Covers: evidence gathering (decided_by/as_of filtering, cap), the
deterministic grounding gate (fabricated ids dropped), the zero-LLM
no-evidence skip, the tracker-down alpha degrade, the meta read-merge-write
(preserves unrelated keys), ``run_accountability``'s counts, and the
``--dry-run`` CLI path.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from synthesis.insights import InsightRow
from synthesis.tenet_accountability import (
    Assessment,
    Evidence,
    assess_tenet,
    gather_evidence,
    persist_assessment,
    run_accountability,
)
from synthesis.tenets import record_tenet

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Verbatim from tests/test_decision_journal_view.py: the three tables
# db.py's init_db() creates OUTSIDE alembic (every migration from 0001 on
# assumes these already exist).
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


def _insert_decision(
    db_path: Path,
    *,
    ticker: str = "NU",
    kind: str = "add",
    made_at: str,
    decided_by: str = "owner",
    conviction: str | None = None,
    falsifier: str | None = None,
    outcome_label: str | None = None,
    outcome_pct: float | None = None,
    source_artifact_id: int | None = None,
) -> int:
    """``ck_decisions_source_present`` requires an advisor-decided row to carry
    provenance (``source_artifact_id``/``source_memo_id``, or
    ``recommendation_kind='avoid'``) — an owner-decided row is exempt. Tests
    that seed a non-owner decision must pass ``source_artifact_id`` (any int;
    no real FK enforcement) to satisfy the constraint."""
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            """
            INSERT INTO decisions
                (ticker, recommendation_kind, decided_by, conviction, falsifier,
                 outcome_label, outcome_pct, made_at, created_at, source_artifact_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ticker,
                kind,
                decided_by,
                conviction,
                falsifier,
                outcome_label,
                outcome_pct,
                made_at,
                made_at,
                source_artifact_id,
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def _seed_tenet(db_path: Path, *, as_of_override: str | None = None) -> InsightRow:
    tenet = record_tenet(
        body_md="I let a working thesis run; I don't trim on fear.",
        scope_key="exit-discipline",
        db_path=db_path,
    )
    if as_of_override is not None:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "UPDATE insight_notes SET as_of = ? WHERE id = ?", (as_of_override, tenet.id)
            )
            conn.commit()
        finally:
            conn.close()
        from synthesis.insights import get_insight

        refreshed = get_insight(tenet.id, db_path=db_path)
        assert refreshed is not None
        return refreshed
    return tenet


# ---------------------------------------------------------------------------
# gather_evidence
# ---------------------------------------------------------------------------


def test_gather_evidence_filters_by_decided_by_and_as_of(db_path: Path) -> None:
    tenet = _seed_tenet(db_path, as_of_override="2026-06-01T00:00:00")
    before_id = _insert_decision(db_path, made_at="2026-05-01T00:00:00")  # before as_of
    advisor_id = _insert_decision(
        db_path, made_at="2026-06-15T00:00:00", decided_by="advisor", source_artifact_id=1
    )  # not owner
    after_id = _insert_decision(db_path, made_at="2026-06-15T00:00:00")  # in window

    evidence = gather_evidence(tenet, db_path=db_path)

    ids = {d.decision_id for d in evidence.decisions}
    assert after_id in ids
    assert before_id not in ids
    assert advisor_id not in ids


def test_gather_evidence_caps_and_orders_newest_first(db_path: Path) -> None:
    tenet = _seed_tenet(db_path, as_of_override="2026-01-01T00:00:00")
    ids = [_insert_decision(db_path, made_at=f"2026-02-{d:02d}T00:00:00") for d in range(1, 35)]
    evidence = gather_evidence(tenet, db_path=db_path)
    assert len(evidence.decisions) == 30
    assert evidence.decisions[0].decision_id == ids[-1]  # newest first


def test_gather_evidence_no_tracker_degrades_alpha_to_none(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import integrations.portfolio_tracker_client as tracker_client

    def _raise(*args: object, **kwargs: object) -> object:
        raise ConnectionError("tracker unreachable")

    monkeypatch.setattr(tracker_client, "fetch_portfolio_analytics", _raise)
    tenet = _seed_tenet(db_path)
    evidence = gather_evidence(tenet, db_path=db_path)
    # No reachable tracker — the client degrades position_alpha to None, and
    # this module degrades the whole alpha map to None on top of that.
    assert evidence.alpha_by_ticker is None


# ---------------------------------------------------------------------------
# assess_tenet — grounding + skip + transient degrade
# ---------------------------------------------------------------------------


def test_assess_tenet_skips_no_evidence_zero_calls(db_path: Path) -> None:
    tenet = _seed_tenet(db_path)

    def _spy(prompt: str) -> dict[str, object] | None:
        raise AssertionError("must not be called when there is no evidence")

    evidence = Evidence(decisions=[], alpha_by_ticker=None)
    assert assess_tenet(tenet, evidence, call=_spy) is None


def test_assess_tenet_drops_fabricated_citations(db_path: Path) -> None:
    tenet = _seed_tenet(db_path, as_of_override="2026-06-01T00:00:00")
    real_id = _insert_decision(db_path, made_at="2026-06-10T00:00:00")
    evidence = gather_evidence(tenet, db_path=db_path)
    assert [d.decision_id for d in evidence.decisions] == [real_id]

    def _stub(prompt: str) -> dict[str, object]:
        return {
            "upheld": [],
            "violated": [f"D{real_id}", "D9999"],  # D9999 never shown — fabricated
            "est_cost_usd": -1200.0,
            "one_liner": "Held through the drawdown despite the falsifier.",
        }

    assessment = assess_tenet(tenet, evidence, call=_stub)
    assert assessment is not None
    assert assessment.violated == [real_id]
    assert assessment.upheld == []
    assert assessment.est_cost_usd == -1200.0


def test_assess_tenet_double_cited_id_prefers_upheld(db_path: Path) -> None:
    tenet = _seed_tenet(db_path, as_of_override="2026-06-01T00:00:00")
    did = _insert_decision(db_path, made_at="2026-06-10T00:00:00")
    evidence = gather_evidence(tenet, db_path=db_path)

    def _stub(prompt: str) -> dict[str, object]:
        return {
            "upheld": [f"D{did}"],
            "violated": [f"D{did}"],
            "est_cost_usd": None,
            "one_liner": "x",
        }

    assessment = assess_tenet(tenet, evidence, call=_stub)
    assert assessment is not None
    assert assessment.upheld == [did]
    assert assessment.violated == []


def test_assess_tenet_empty_one_liner_synthesizes_fallback(db_path: Path) -> None:
    tenet = _seed_tenet(db_path, as_of_override="2026-06-01T00:00:00")
    did = _insert_decision(db_path, made_at="2026-06-10T00:00:00")
    evidence = gather_evidence(tenet, db_path=db_path)

    def _stub(prompt: str) -> dict[str, object]:
        return {"upheld": [f"D{did}"], "violated": [], "est_cost_usd": None, "one_liner": "   "}

    assessment = assess_tenet(tenet, evidence, call=_stub)
    assert assessment is not None
    assert "upheld 1 / violated 0" in assessment.one_liner
    assert "2026-06-01" in assessment.one_liner


def test_assess_tenet_transient_failure_returns_none(db_path: Path) -> None:
    tenet = _seed_tenet(db_path, as_of_override="2026-06-01T00:00:00")
    _insert_decision(db_path, made_at="2026-06-10T00:00:00")
    evidence = gather_evidence(tenet, db_path=db_path)

    def _boom(prompt: str) -> dict[str, object]:
        raise RuntimeError("transient CLI failure")

    assert assess_tenet(tenet, evidence, call=_boom) is None


def test_assess_tenet_hard_stop_propagates(db_path: Path) -> None:
    from llm.cli import LLMSetupError

    tenet = _seed_tenet(db_path, as_of_override="2026-06-01T00:00:00")
    _insert_decision(db_path, made_at="2026-06-10T00:00:00")
    evidence = gather_evidence(tenet, db_path=db_path)

    def _hard_stop(prompt: str) -> dict[str, object]:
        raise LLMSetupError("claude CLI not found")

    with pytest.raises(LLMSetupError):
        assess_tenet(tenet, evidence, call=_hard_stop)


def test_tracker_down_still_assesses_with_no_cost(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tracker network problem must never block the pass — only drop the
    alpha figure (assess_tenet then judges on process, not P&L)."""
    import requests

    import integrations.portfolio_tracker_client as tracker_client

    def _raise(*args: object, **kwargs: object) -> object:
        raise requests.exceptions.ConnectionError("tracker unreachable")

    monkeypatch.setattr(tracker_client.requests, "get", _raise)
    monkeypatch.setenv("PORTFOLIO_TRACKER_API_URL", "http://127.0.0.1:8001")

    tenet = _seed_tenet(db_path, as_of_override="2026-06-01T00:00:00")
    did = _insert_decision(db_path, made_at="2026-06-10T00:00:00")
    evidence = gather_evidence(tenet, db_path=db_path)
    assert evidence.alpha_by_ticker is None

    seen_prompts: list[str] = []

    def _stub(prompt: str) -> dict[str, object]:
        seen_prompts.append(prompt)
        return {
            "upheld": [f"D{did}"],
            "violated": [],
            "est_cost_usd": None,
            "one_liner": "Held true.",
        }

    assessment = assess_tenet(tenet, evidence, call=_stub)
    assert assessment is not None
    assert assessment.est_cost_usd is None
    assert "unavailable" in seen_prompts[0]


# ---------------------------------------------------------------------------
# persist_assessment
# ---------------------------------------------------------------------------


def test_persist_assessment_merges_meta_preserving_tensions(db_path: Path) -> None:
    tenet = _seed_tenet(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE insight_notes SET meta_json = ? WHERE id = ?",
            (json.dumps({"tensions": [7]}), tenet.id),
        )
        conn.commit()
    finally:
        conn.close()

    assessment = Assessment(upheld=[1], violated=[2], est_cost_usd=-500.0, one_liner="verdict")
    persist_assessment(tenet, assessment, db_path=db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT meta_json FROM insight_notes WHERE id = ?", (tenet.id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    meta = json.loads(row[0])
    assert meta["tensions"] == [7]
    assert meta["accountability"]["upheld"] == [1]
    assert meta["accountability"]["violated"] == [2]
    assert meta["accountability"]["est_cost_usd"] == -500.0
    assert meta["accountability"]["one_liner"] == "verdict"


def test_persist_assessment_replaces_prior_accountability_wholesale(db_path: Path) -> None:
    tenet = _seed_tenet(db_path)
    persist_assessment(
        tenet,
        Assessment(upheld=[1], violated=[], est_cost_usd=None, one_liner="first"),
        db_path=db_path,
    )
    persist_assessment(
        tenet,
        Assessment(upheld=[], violated=[2, 3], est_cost_usd=-100.0, one_liner="second"),
        db_path=db_path,
    )
    from synthesis.insights import get_insight

    refreshed = get_insight(tenet.id, db_path=db_path)
    assert refreshed is not None
    assert refreshed.meta["accountability"]["one_liner"] == "second"
    assert refreshed.meta["accountability"]["violated"] == [2, 3]


# ---------------------------------------------------------------------------
# run_accountability
# ---------------------------------------------------------------------------


def test_run_accountability_counts(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PORTFOLIO_TRACKER_API_URL", raising=False)
    # Tenet 1: no evidence at all -> skipped_no_evidence.
    record_tenet(body_md="Circle of competence only.", scope_key="circle", db_path=db_path)
    # Tenet 2: has evidence -> assessed.
    tenet2 = _seed_tenet(db_path, as_of_override="2026-06-01T00:00:00")
    did = _insert_decision(db_path, made_at="2026-06-10T00:00:00")

    def _stub(prompt: str) -> dict[str, object]:
        return {"upheld": [f"D{did}"], "violated": [], "est_cost_usd": None, "one_liner": "ok"}

    counts = run_accountability(db_path, call=_stub)
    assert counts["tenets"] == 2
    assert counts["assessed"] == 1
    assert counts["skipped_no_evidence"] == 1
    assert counts["deferred_transient"] == 0

    from synthesis.insights import get_insight

    refreshed = get_insight(tenet2.id, db_path=db_path)
    assert refreshed is not None
    assert refreshed.meta["accountability"]["upheld"] == [did]


def test_run_accountability_tallies_deferred_transient(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PORTFOLIO_TRACKER_API_URL", raising=False)
    _seed_tenet(db_path, as_of_override="2026-06-01T00:00:00")
    _insert_decision(db_path, made_at="2026-06-10T00:00:00")

    def _boom(prompt: str) -> dict[str, object]:
        raise RuntimeError("transient")

    counts = run_accountability(db_path, call=_boom)
    assert counts["tenets"] == 1
    assert counts["assessed"] == 0
    assert counts["deferred_transient"] == 1


def test_run_accountability_hard_stop_propagates(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llm.cli import LLMSetupError

    monkeypatch.delenv("PORTFOLIO_TRACKER_API_URL", raising=False)
    _seed_tenet(db_path, as_of_override="2026-06-01T00:00:00")
    _insert_decision(db_path, made_at="2026-06-10T00:00:00")

    def _hard_stop(prompt: str) -> dict[str, object]:
        raise LLMSetupError("claude CLI not found")

    with pytest.raises(LLMSetupError):
        run_accountability(db_path, call=_hard_stop)


# ---------------------------------------------------------------------------
# CLI --dry-run
# ---------------------------------------------------------------------------


def test_runner_dry_run_lists_candidates_zero_llm(
    db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sys.path.insert(0, str(PROJECT_ROOT / "execution"))
    import run_tenet_accountability

    record_tenet(body_md="Circle of competence only.", scope_key="circle", db_path=db_path)

    rc = run_tenet_accountability.main(["--dry-run", "--db-path", str(db_path)])
    assert rc == 0
    captured = capsys.readouterr()
    assert "candidate: tenet_id=" in captured.err
    assert "0 LLM calls" in captured.err
