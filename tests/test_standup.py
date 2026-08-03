"""Tests for the proactive analyst standup (close_the_loops L9).

Covers the four deterministic watchers, the ledger's dedup / rate-limit reads,
the memory stub, the framing + compose seam, the eval gate's threshold, and the
end-to-end ``run_standup`` orchestration (deliver, suppress, dedup, day-cap,
memory caveat). Every LLM seam is injected with a fake — the compose engine
(``events_fn``) and the eval judge (``judge_caller``) — so no test spends or
launches the real ``claude -p`` subprocess (conftest also blocks the transport).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from evals.rubric_judge import load_rubric
from standup import StandupConfig, collect_signals, run_standup
from standup.compose import ComposedMessage, EventsFn, compose, frame_question
from standup.gate import gate_message
from standup.ledger import (
    STATUS_DELIVERED,
    STATUS_DELIVERED_CAVEAT,
    STATUS_JUDGE_FAILED,
    STATUS_SUPPRESSED_EVAL,
    delivered_today_count,
    recently_composed_signatures,
    record,
    signature_sha,
    within_cooldown,
)
from standup.memory import distill_conclusion, recall_conclusion
from standup.signals import StandupSignal

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUBRIC_PATH = PROJECT_ROOT / "evals" / "rubrics" / "ask_advisory_answer.md"

_NOW = datetime(2026, 6, 14, 12, 0, 0)

_SCHEMA = """
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    recommendation_kind VARCHAR(32) NOT NULL,
    recommendation_value FLOAT,
    conviction VARCHAR(16),
    source_lens VARCHAR(64),
    made_at DATETIME NOT NULL,
    outcome_at DATETIME,
    outcome_label VARCHAR(16),
    decision_conditions TEXT,
    conditions_extracted_at DATETIME,
    created_at DATETIME
);
CREATE TABLE analyst_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    ticker TEXT,
    kind TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE dcf_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    valuation_date TEXT,
    npv_per_share NUMERIC,
    live_price NUMERIC,
    created_at DATETIME
);
CREATE TABLE position_sizing_intent (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    intent_kind TEXT NOT NULL,
    intent_value FLOAT,
    narrative TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE tracked_companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'bhanu',
    ticker TEXT NOT NULL,
    list_type TEXT,
    archived_at TIMESTAMP
);
CREATE TABLE ask_sessions (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL DEFAULT 'portfolio',
    title TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE ask_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES ask_sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    citations_json TEXT,
    model TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE standup_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'bhanu',
    ticker TEXT,
    signal_kind TEXT NOT NULL,
    signature_sha TEXT NOT NULL,
    status TEXT NOT NULL,
    score REAL,
    session_id TEXT,
    turn_id INTEGER,
    conclusion TEXT,
    headline TEXT,
    evidence_json TEXT,
    created_at TEXT NOT NULL
);
"""

USER = "bhanu"


def _events(*events: dict[str, object]) -> EventsFn:
    """A fake engine seam returning a fixed event stream (typed for the gate)."""

    def fn(question: str, tickers: list[str]) -> list[dict[str, object]]:
        return list(events)

    return fn


def _raise_events(question: str, tickers: list[str]) -> list[dict[str, object]]:
    raise RuntimeError("engine down")


def _iso(days_ago: float) -> str:
    return (_NOW - timedelta(days=days_ago)).isoformat()


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture
def rubric() -> object:
    return load_rubric(RUBRIC_PATH, purpose="ask_advisory_answer")


def _conn(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _add_tracked(db: Path, ticker: str, list_type: str = "portfolio") -> None:
    conn = _conn(db)
    try:
        conn.execute(
            "INSERT INTO tracked_companies (user_id, ticker, list_type) VALUES (?, ?, ?)",
            (USER, ticker, list_type),
        )
        conn.commit()
    finally:
        conn.close()


def _add_note(db: Path, *, ticker: str | None, kind: str, body: str, days_ago: float) -> None:
    conn = _conn(db)
    try:
        conn.execute(
            "INSERT INTO analyst_notes (user_id, ticker, kind, body, status, created_at) "
            "VALUES (?, ?, ?, ?, 'open', ?)",
            (USER, ticker, kind, body, _iso(days_ago)),
        )
        conn.commit()
    finally:
        conn.close()


def _add_dcf(db: Path, ticker: str, *, fv: float, px: float, days_ago: float) -> None:
    conn = _conn(db)
    try:
        conn.execute(
            "INSERT INTO dcf_runs (ticker, valuation_date, npv_per_share, live_price, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (ticker, _iso(days_ago)[:10], fv, px, _iso(days_ago)),
        )
        conn.commit()
    finally:
        conn.close()


def _add_target(db: Path, ticker: str, target_pct: float) -> None:
    conn = _conn(db)
    try:
        conn.execute(
            "INSERT INTO position_sizing_intent "
            "(user_id, ticker, intent_kind, intent_value, created_at) "
            "VALUES (?, ?, 'target_weight_pct', ?, ?)",
            (USER, ticker, target_pct, _iso(10)),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Watchers
# ---------------------------------------------------------------------------


def test_journal_watcher_detects_stale_open_and_skips_fresh(db: Path) -> None:
    _add_note(db, ticker="NU", kind="question", body="Is the NPL trend structural?", days_ago=60)
    _add_note(db, ticker="MELI", kind="watch", body="fresh note", days_ago=5)
    conn = _conn(db)
    try:
        signals = collect_signals(conn, user_id=USER, now=_NOW, config=StandupConfig())
    finally:
        conn.close()
    journal = [s for s in signals if s.kind == "journal_open_item"]
    assert [s.ticker for s in journal] == ["NU"]
    assert "unresolved" in journal[0].headline
    assert journal[0].materiality >= 0.4


def test_dcf_watcher_trips_only_when_stale_and_mispriced(db: Path) -> None:
    _add_tracked(db, "NU")
    _add_tracked(db, "MELI")
    _add_tracked(db, "AMZN")
    _add_tracked(db, "GOOG")
    # stale + mispriced 25% over → trips
    _add_dcf(db, "NU", fv=10.0, px=12.5, days_ago=200)
    # stale but only 5% off → quiet
    _add_dcf(db, "MELI", fv=100.0, px=105.0, days_ago=200)
    # fresh (10d) though mispriced → quiet
    _add_dcf(db, "AMZN", fv=100.0, px=150.0, days_ago=10)
    # implausible (3x) → treated as a mis-modeled run, quiet
    _add_dcf(db, "GOOG", fv=10.0, px=40.0, days_ago=200)
    conn = _conn(db)
    try:
        signals = collect_signals(conn, user_id=USER, now=_NOW, config=StandupConfig())
    finally:
        conn.close()
    dcf = [s for s in signals if s.kind == "dcf_staleness"]
    assert [s.ticker for s in dcf] == ["NU"]
    assert dcf[0].evidence["mispricing_pct"] == pytest.approx(25.0, abs=0.5)


def _add_decision(
    db: Path,
    *,
    ticker: str,
    kind: str,
    made_at_days_ago: float,
    conviction: str | None = None,
    value: float | None = None,
    outcome_at_days_ago: float | None = None,
) -> None:
    conn = _conn(db)
    try:
        conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, recommendation_value, conviction, "
            "made_at, outcome_at, created_at) VALUES (?,?,?,?,?,?,?)",
            (
                ticker,
                kind,
                value,
                conviction,
                _iso(made_at_days_ago),
                _iso(outcome_at_days_ago) if outcome_at_days_ago is not None else None,
                _iso(made_at_days_ago),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_verdict_due_watcher_trips_on_elapsed_ungraded_call(db: Path) -> None:
    # NU: ungraded, horizon elapsed (45d) → due. MELI: ungraded but fresh (5d) →
    # quiet. WIX: already graded → never due (outcome recorded).
    _add_decision(db, ticker="NU", kind="add", value=8, conviction="high", made_at_days_ago=45)
    _add_decision(db, ticker="MELI", kind="trim", made_at_days_ago=5)
    _add_decision(db, ticker="WIX", kind="hold", made_at_days_ago=90, outcome_at_days_ago=2)
    conn = _conn(db)
    try:
        signals = collect_signals(conn, user_id=USER, now=_NOW, config=StandupConfig())
    finally:
        conn.close()
    due = [s for s in signals if s.kind == "decision_verdict_due"]
    assert [s.ticker for s in due] == ["NU"]
    assert "due for a verdict" in due[0].headline
    assert due[0].evidence["recommendation_kind"] == "ADD"


def test_verdict_due_frames_a_grade_my_call_question(db: Path) -> None:
    _add_decision(db, ticker="NU", kind="add", made_at_days_ago=45)
    conn = _conn(db)
    try:
        signals = collect_signals(conn, user_id=USER, now=_NOW, config=StandupConfig())
    finally:
        conn.close()
    due = next(s for s in signals if s.kind == "decision_verdict_due")
    q = frame_question(due)
    assert "still ungraded" in q
    assert "did that call play out" in q.lower()


def _set_weights(monkeypatch: pytest.MonkeyPatch, weights: dict[str, float]) -> None:
    def fake(conn: sqlite3.Connection, repo_root: Path | None) -> dict[str, float]:
        return weights

    monkeypatch.setattr("standup.signals._current_weights_pct", fake)


def test_drift_watcher_trips_beyond_threshold(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _add_tracked(db, "NU")
    _add_tracked(db, "MELI")
    _add_target(db, "NU", 8.0)
    _add_target(db, "MELI", 5.0)
    _set_weights(monkeypatch, {"NU": 13.0, "MELI": 5.5})
    conn = _conn(db)
    try:
        signals = collect_signals(conn, user_id=USER, now=_NOW, config=StandupConfig())
    finally:
        conn.close()
    drift = [s for s in signals if s.kind == "position_drift"]
    assert [s.ticker for s in drift] == ["NU"]  # MELI 0.5pp is within 3pp threshold
    assert drift[0].evidence["drift_pp"] == pytest.approx(5.0)
    assert "overweight" in drift[0].headline


def test_drift_falls_back_to_materialized_cache_when_tracker_offline(
    db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Driven through the public watcher: the tracker raises, so the disk cache
    # (fractions) is read and scaled to percent — a 13% live weight vs an 8%
    # target trips drift even with :8000 down.
    _add_tracked(db, "NU")
    _add_target(db, "NU", 8.0)

    def offline() -> object:
        raise ConnectionError("offline")

    monkeypatch.setattr("integrations.portfolio_tracker_client.fetch_live_portfolio", offline)
    data_dir = db.parent / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "portfolio_weights.json").write_text(
        json.dumps({"computed_at": _iso(0), "weights": {"NU": 0.13}}),
        encoding="utf-8",
    )
    conn = _conn(db)
    try:
        signals = collect_signals(
            conn, user_id=USER, repo_root=db.parent, now=_NOW, config=StandupConfig()
        )
    finally:
        conn.close()
    drift = [s for s in signals if s.kind == "position_drift"]
    assert len(drift) == 1
    assert drift[0].evidence["current_pct"] == pytest.approx(13.0)


def test_decision_condition_watcher_maps_candidate(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Insert an open decision so the watcher's universe query returns NU, then
    # fake the break-rule scan (its own engine is covered by the trigger tests).
    from triggers.base import TriggerCandidate
    from triggers.decision_condition import DecisionConditionTrigger

    conn0 = _conn(db)
    try:
        conn0.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, made_at, decision_conditions) "
            "VALUES ('NU', 'add', ?, '[{\"metric\": \"x\"}]')",
            (_iso(40),),
        )
        conn0.commit()
    finally:
        conn0.close()

    candidate = TriggerCandidate(
        ticker="NU",
        kind="decision_condition",
        key="7:0:2026-03-31",
        evidence={
            "decision_id": 7,
            "condition_index": 0,
            "condition_label": "NPL 90d+ >= 7 percent",
            "decision_summary": "the 2026-02-01 ADD decision (memo)",
            "latest_value": 8.1,
            "unit": "percent",
            "period_end": "2026-03-31",
        },
        computed_at=_NOW,
    )

    def fake_scan(
        self: DecisionConditionTrigger, ticker: str, conn: sqlite3.Connection
    ) -> list[TriggerCandidate]:
        return [candidate]

    monkeypatch.setattr(DecisionConditionTrigger, "scan", fake_scan)

    conn = _conn(db)
    try:
        signals = collect_signals(conn, user_id=USER, now=_NOW, config=StandupConfig())
    finally:
        conn.close()
    dc = [s for s in signals if s.kind == "decision_condition"]
    assert len(dc) == 1
    assert dc[0].materiality == 1.0  # the analyst's own bar — highest
    assert dc[0].signature == "7:0:2026-03-31"
    assert "NPL 90d+ >= 7 percent" in dc[0].headline


def test_collect_signals_ranks_by_materiality_and_survives_a_failing_watcher(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_note(db, ticker="NU", kind="question", body="open q", days_ago=60)

    # A watcher raising must not sink the others.
    def boom(conn: sqlite3.Connection, **kwargs: object) -> list[StandupSignal]:
        raise RuntimeError("boom")

    monkeypatch.setattr("standup.signals._dcf_staleness_signals", boom)
    conn = _conn(db)
    try:
        signals = collect_signals(conn, user_id=USER, now=_NOW, config=StandupConfig())
    finally:
        conn.close()
    assert any(s.kind == "journal_open_item" for s in signals)
    # ranked highest materiality first
    assert signals == sorted(signals, key=lambda s: (-s.materiality, s.kind, s.ticker or ""))


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


def _signal(
    kind: str = "dcf_staleness", ticker: str | None = "NU", sig: str = "dcf:NU:x"
) -> StandupSignal:
    return StandupSignal(
        kind=kind, ticker=ticker, signature=sig, headline="h", materiality=0.9, evidence={"a": 1}
    )


def test_ledger_dedup_and_rate_limits(db: Path) -> None:
    conn = _conn(db)
    try:
        s = _signal()
        record(conn, user_id=USER, signal=s, status=STATUS_DELIVERED, now=_NOW, score=0.8)
        # dedup: the signature is now within the window
        sigs = recently_composed_signatures(conn, user_id=USER, now=_NOW, within_days=7)
        assert signature_sha(s) in sigs
        # but not after the window
        sigs_old = recently_composed_signatures(
            conn, user_id=USER, now=_NOW + timedelta(days=30), within_days=7
        )
        assert signature_sha(s) not in sigs_old
        # per-day count
        assert delivered_today_count(conn, user_id=USER, now=_NOW) == 1
        assert delivered_today_count(conn, user_id=USER, now=_NOW + timedelta(days=1)) == 0
        # per-name cooldown
        assert within_cooldown(conn, user_id=USER, ticker="NU", now=_NOW, cooldown_days=3)
        assert not within_cooldown(
            conn, user_id=USER, ticker="NU", now=_NOW + timedelta(days=5), cooldown_days=3
        )
        # book-level signal is never cooldown-suppressed by name
        assert not within_cooldown(conn, user_id=USER, ticker=None, now=_NOW, cooldown_days=3)
    finally:
        conn.close()


def test_ledger_dedup_excludes_compose_failed_and_includes_suppressed(db: Path) -> None:
    conn = _conn(db)
    try:
        from standup.ledger import STATUS_COMPOSE_FAILED

        failed = _signal(sig="dcf:NU:failed")
        suppressed = _signal(sig="dcf:NU:supp")
        record(conn, user_id=USER, signal=failed, status=STATUS_COMPOSE_FAILED, now=_NOW)
        record(
            conn,
            user_id=USER,
            signal=suppressed,
            status=STATUS_SUPPRESSED_EVAL,
            now=_NOW,
            score=0.4,
        )
        sigs = recently_composed_signatures(conn, user_id=USER, now=_NOW, within_days=7)
        assert signature_sha(suppressed) in sigs  # eval-suppressed → don't re-pay
        assert signature_sha(failed) not in sigs  # transient → retry allowed
    finally:
        conn.close()


def test_ledger_record_strips_inline_markdown_from_scalars(db: Path) -> None:
    """headline/conclusion are scalars rendered plain in the standup thread —
    an LLM-extracted ``**bold**`` must not persist there."""
    conn = _conn(db)
    try:
        s = StandupSignal(
            kind="decision_condition",
            ticker="NU",
            signature="dcf:NU:md",
            headline="**NU**: a condition you set just tripped",
            materiality=0.9,
            evidence={},
        )
        row_id = record(
            conn,
            user_id=USER,
            signal=s,
            status=STATUS_DELIVERED,
            now=_NOW,
            score=0.8,
            conclusion="Deposit beta *fell* 40bps QoQ.",
        )
        headline, conclusion = conn.execute(
            "SELECT headline, conclusion FROM standup_messages WHERE id = ?", (row_id,)
        ).fetchone()
        assert headline == "NU: a condition you set just tripped"
        assert conclusion == "Deposit beta fell 40bps QoQ."
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Memory stub
# ---------------------------------------------------------------------------


def test_distill_conclusion_takes_lead_sentence() -> None:
    text = "NU's NPL print is one quarter and within guidance. The thesis holds for now."
    assert distill_conclusion(text) == "NU's NPL print is one quarter and within guidance."
    assert distill_conclusion("") == ""


def test_recall_conclusion_returns_latest_delivered(db: Path) -> None:
    conn = _conn(db)
    try:
        s = _signal()
        record(
            conn,
            user_id=USER,
            signal=s,
            status=STATUS_DELIVERED,
            now=_NOW,
            score=0.8,
            conclusion="Prior read: thesis intact.",
        )
        prior = recall_conclusion(conn, user_id=USER, ticker="NU")
        assert prior is not None
        assert prior.conclusion == "Prior read: thesis intact."
        assert recall_conclusion(conn, user_id=USER, ticker="ZZZ") is None
        assert recall_conclusion(conn, user_id=USER, ticker=None) is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Framing + compose
# ---------------------------------------------------------------------------


def test_frame_question_includes_condition_and_memory_caveat() -> None:
    from standup.memory import PriorConclusion

    sig = StandupSignal(
        kind="decision_condition",
        ticker="NU",
        signature="7:0:2026-03-31",
        headline="h",
        materiality=1.0,
        evidence={
            "condition_label": "NPL 90d+ >= 7 percent",
            "latest_value": 8.1,
            "unit": "percent",
            "period_end": "2026-03-31",
            "decision_summary": "the 2026-02-01 ADD decision",
        },
    )
    q = frame_question(sig, prior=PriorConclusion("thesis intact", "2026-05-01", "dcf_staleness"))
    assert "NPL 90d+ >= 7 percent" in q
    assert "PRIOR" in q and "re-derive" in q  # the hazard caveat
    assert "Do NOT give a buy / sell / trim stance" in q  # no-stance guard


def test_compose_returns_message_on_narrative_and_none_otherwise() -> None:
    sig = _signal()
    narrative = _events(
        {"type": "final", "route": "narrative", "text": "Grounded brief [1]."},
        {
            "type": "citations",
            "items": [{"n": 1, "kind": "dcf", "label": "NU DCF", "confidence": 0.7}],
        },
    )
    msg = compose(sig, events_fn=narrative)
    assert isinstance(msg, ComposedMessage)
    assert msg.answer == "Grounded brief [1]."
    assert msg.citations[0]["label"] == "NU DCF"
    # data-routed, empty, error, and raising all yield None
    assert compose(sig, events_fn=_events({"type": "final", "route": "data", "text": "x"})) is None
    assert (
        compose(sig, events_fn=_events({"type": "final", "route": "narrative", "text": ""})) is None
    )
    assert compose(sig, events_fn=_events({"type": "error", "error": "boom"})) is None
    assert compose(sig, events_fn=_raise_events) is None


# ---------------------------------------------------------------------------
# Eval gate
# ---------------------------------------------------------------------------

_FACETS = (
    "grounding_correctness",
    "risk_reward_balance",
    "calibration_vs_evidence",
    "followup_usefulness",
)


def _judge(score: float):
    def caller(prompt: str, **kwargs: object) -> str:
        return json.dumps(
            {"facet_scores": {f: score for f in _FACETS}, "rationale": "test verdict"}
        )

    return caller


def test_gate_passes_high_score_and_suppresses_below_floor(rubric: object) -> None:
    sig = _signal()
    composed = ComposedMessage(question="q", answer="a [1]", citations=[{"n": 1, "label": "x"}])
    high = gate_message(sig, composed, rubric=rubric, min_score=0.75, caller=_judge(1.0))  # type: ignore[arg-type]
    assert high.passed and high.score == pytest.approx(1.0)
    assert set(high.facet_scores) == set(_FACETS)
    # 0.6 < rubric threshold (0.70) → fails
    low = gate_message(sig, composed, rubric=rubric, min_score=0.75, caller=_judge(0.6))  # type: ignore[arg-type]
    assert not low.passed
    # passes the rubric (0.72) but below the standup floor (0.80) → suppressed
    mid = gate_message(sig, composed, rubric=rubric, min_score=0.80, caller=_judge(0.72))  # type: ignore[arg-type]
    assert not mid.passed and mid.score == pytest.approx(0.72)


def test_gate_marks_judge_failed_on_call_exception(rubric: object) -> None:
    """The '9 of 12 suppressed' incident: a raising judge call (the observed
    Claude-CLI CalledProcessError) must be distinguishable from a genuine low
    score — GateOutcome.judge_failed flags it."""
    sig = _signal()
    composed = ComposedMessage(question="q", answer="a [1]", citations=[{"n": 1, "label": "x"}])

    def raising_caller(prompt: str, **kw: object) -> str:
        raise RuntimeError("claude cli exit 1")

    outcome = gate_message(sig, composed, rubric=rubric, min_score=0.75, caller=raising_caller)  # type: ignore[arg-type]
    assert outcome.judge_failed is True
    # score=None, not 0.0: a failed judge CALL measured nothing (the
    # judge_infra contract from the July-2026 transport hardening).
    assert outcome.score is None
    assert not outcome.passed


def test_gate_marks_judge_failed_on_unparseable_verdict(rubric: object) -> None:
    sig = _signal()
    composed = ComposedMessage(question="q", answer="a [1]", citations=[{"n": 1, "label": "x"}])

    def bad_caller(prompt: str, **kw: object) -> str:
        return "not json at all"

    outcome = gate_message(sig, composed, rubric=rubric, min_score=0.75, caller=bad_caller)  # type: ignore[arg-type]
    assert outcome.judge_failed is True


# ---------------------------------------------------------------------------
# End-to-end orchestration
# ---------------------------------------------------------------------------


def _fixed_events(captured: list[str]) -> EventsFn:
    def events_fn(question: str, tickers: list[str]) -> list[dict[str, object]]:
        captured.append(question)
        out: list[dict[str, object]] = [
            {
                "type": "final",
                "route": "narrative",
                "text": "NU NPLs at 8% [1] cleared your 7% bar; it is one print [1], so calibrate. "
                "Counter-case: provisioning may absorb it. Next: pull the Q provisioning line.",
            },
            {
                "type": "citations",
                "items": [{"n": 1, "kind": "fact", "label": "NU NPL", "confidence": 0.8}],
            },
        ]
        return out

    return events_fn


def test_run_standup_delivers_and_persists_thread(db: Path, rubric: object) -> None:
    _add_tracked(db, "NU")
    _add_dcf(db, "NU", fv=10.0, px=13.0, days_ago=200)  # stale + 30% over
    captured: list[str] = []
    report = run_standup(
        db_path=db,
        repo_root=db.parent,
        events_fn=_fixed_events(captured),
        rubric=rubric,  # type: ignore[arg-type]
        now=_NOW,
        user_id=USER,
        judge_caller=_judge(1.0),
        config=StandupConfig(),
    )
    assert report.delivered == 1
    assert report.briefs[0].ticker == "NU"
    assert report.briefs[0].score == pytest.approx(1.0)

    conn = _conn(db)
    try:
        # the rolling thread carries [user headline] + [assistant brief]
        turns = conn.execute("SELECT role, text, model FROM ask_turns ORDER BY id").fetchall()
        assert [t["role"] for t in turns] == ["user", "assistant"]
        assert turns[1]["model"] == "standup/ask_answer"
        # the ledger recorded a delivered row with a distilled conclusion
        row = conn.execute(
            "SELECT status, score, ticker, conclusion, turn_id FROM standup_messages"
        ).fetchone()
        assert row["status"] == STATUS_DELIVERED
        assert row["ticker"] == "NU"
        assert row["conclusion"] and row["turn_id"] is not None
    finally:
        conn.close()


def test_run_standup_suppresses_low_score_and_writes_no_thread(db: Path, rubric: object) -> None:
    _add_tracked(db, "NU")
    _add_dcf(db, "NU", fv=10.0, px=13.0, days_ago=200)
    report = run_standup(
        db_path=db,
        repo_root=db.parent,
        events_fn=_fixed_events([]),
        rubric=rubric,  # type: ignore[arg-type]
        now=_NOW,
        user_id=USER,
        judge_caller=_judge(0.5),  # below the bar
        config=StandupConfig(),
    )
    assert report.delivered == 0
    assert report.suppressed_eval == 1
    conn = _conn(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM ask_turns").fetchone()[0] == 0
        assert (
            conn.execute("SELECT status FROM standup_messages").fetchone()[0]
            == STATUS_SUPPRESSED_EVAL
        )
    finally:
        conn.close()


def test_run_standup_respects_per_day_cap(db: Path, rubric: object) -> None:
    for t, fv, px in [("NU", 10.0, 13.0), ("MELI", 10.0, 13.5), ("AMZN", 10.0, 14.0)]:
        _add_tracked(db, t)
        _add_dcf(db, t, fv=fv, px=px, days_ago=200)
    report = run_standup(
        db_path=db,
        repo_root=db.parent,
        events_fn=_fixed_events([]),
        rubric=rubric,  # type: ignore[arg-type]
        now=_NOW,
        user_id=USER,
        judge_caller=_judge(1.0),
        config=StandupConfig(max_per_day=1),
    )
    assert report.delivered == 1
    assert report.day_capped == 2


def test_run_standup_dedup_skips_second_run(db: Path, rubric: object) -> None:
    _add_tracked(db, "NU")
    _add_dcf(db, "NU", fv=10.0, px=13.0, days_ago=200)

    first = run_standup(
        db_path=db,
        repo_root=db.parent,
        events_fn=_fixed_events([]),
        rubric=rubric,  # type: ignore[arg-type]
        now=_NOW,
        user_id=USER,
        judge_caller=_judge(1.0),
        config=StandupConfig(),
    )
    assert first.delivered == 1
    second = run_standup(
        db_path=db,
        repo_root=db.parent,
        events_fn=_fixed_events([]),
        rubric=rubric,  # type: ignore[arg-type]
        now=_NOW,
        user_id=USER,
        judge_caller=_judge(1.0),
        config=StandupConfig(),
    )
    assert second.delivered == 0
    assert second.deduped == 1


def test_run_standup_injects_memory_caveat_on_subsequent_brief(db: Path, rubric: object) -> None:
    # Deliver once (records a conclusion), then a DIFFERENT trip for the same name
    # with cooldown disabled — the second framing must carry the prior caveat.
    _add_tracked(db, "NU")
    _add_dcf(db, "NU", fv=10.0, px=13.0, days_ago=200)
    cfg = StandupConfig(per_name_cooldown_days=0)
    run_standup(
        db_path=db,
        repo_root=db.parent,
        events_fn=_fixed_events([]),
        rubric=rubric,  # type: ignore[arg-type]
        now=_NOW,
        user_id=USER,
        judge_caller=_judge(1.0),
        config=cfg,
    )
    # a new, different signature for NU (a stale journal note)
    _add_note(db, ticker="NU", kind="assumption", body="provisioning normalizes by Q4", days_ago=60)
    captured: list[str] = []
    report = run_standup(
        db_path=db,
        repo_root=db.parent,
        events_fn=_fixed_events(captured),
        rubric=rubric,  # type: ignore[arg-type]
        now=_NOW,
        user_id=USER,
        judge_caller=_judge(1.0),
        config=cfg,
    )
    assert report.delivered == 1
    assert any("PRIOR" in q and "re-derive" in q for q in captured)


# ---------------------------------------------------------------------------
# Deliver-with-caveat tier + the judge-failed retry fix (PR2, Deliverable 3)
# ---------------------------------------------------------------------------


def test_run_standup_caveat_tier_delivers_with_prefix(db: Path, rubric: object) -> None:
    _add_tracked(db, "NU")
    _add_dcf(db, "NU", fv=10.0, px=13.0, days_ago=200)
    # 0.65 is genuinely scored (a real, parsed verdict) — in [caveat_floor=0.60, min_score=0.75)
    report = run_standup(
        db_path=db,
        repo_root=db.parent,
        events_fn=_fixed_events([]),
        rubric=rubric,  # type: ignore[arg-type]
        now=_NOW,
        user_id=USER,
        judge_caller=_judge(0.65),
        config=StandupConfig(),
    )
    assert report.delivered == 0
    assert report.delivered_caveat == 1
    conn = _conn(db)
    try:
        row = conn.execute("SELECT status FROM standup_messages").fetchone()
        turn = conn.execute("SELECT text FROM ask_turns WHERE role = 'assistant'").fetchone()
    finally:
        conn.close()
    assert row["status"] == STATUS_DELIVERED_CAVEAT
    assert turn["text"].startswith("(below the usual bar")


def test_run_standup_below_caveat_floor_still_fully_suppressed(db: Path, rubric: object) -> None:
    _add_tracked(db, "NU")
    _add_dcf(db, "NU", fv=10.0, px=13.0, days_ago=200)
    # 0.3 is genuinely scored but below caveat_floor=0.60 -> stays suppressed
    report = run_standup(
        db_path=db,
        repo_root=db.parent,
        events_fn=_fixed_events([]),
        rubric=rubric,  # type: ignore[arg-type]
        now=_NOW,
        user_id=USER,
        judge_caller=_judge(0.3),
        config=StandupConfig(),
    )
    assert report.delivered == 0 and report.delivered_caveat == 0
    assert report.suppressed_eval == 1
    conn = _conn(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM ask_turns").fetchone()[0] == 0
        assert (
            conn.execute("SELECT status FROM standup_messages").fetchone()[0]
            == STATUS_SUPPRESSED_EVAL
        )
    finally:
        conn.close()


def test_run_standup_judge_failed_is_not_dedup_locked_and_retries(db: Path, rubric: object) -> None:
    """The actual 2026-07 incident, reproduced: a raising judge call must
    record STATUS_JUDGE_FAILED (not STATUS_SUPPRESSED_EVAL) and must NOT be
    excluded from retry on the very next run."""
    _add_tracked(db, "NU")
    _add_dcf(db, "NU", fv=10.0, px=13.0, days_ago=200)

    def raising_caller(prompt: str, **kw: object) -> str:
        raise RuntimeError("claude cli exit 1")

    report1 = run_standup(
        db_path=db,
        repo_root=db.parent,
        events_fn=_fixed_events([]),
        rubric=rubric,  # type: ignore[arg-type]
        now=_NOW,
        user_id=USER,
        judge_caller=raising_caller,
        config=StandupConfig(),
    )
    assert report1.delivered == 0
    assert report1.judge_failed == 1
    conn = _conn(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM ask_turns").fetchone()[0] == 0
        assert (
            conn.execute("SELECT status FROM standup_messages").fetchone()[0] == STATUS_JUDGE_FAILED
        )
    finally:
        conn.close()

    # Same trip, same run instant (well within dedup_days=7) — a genuinely
    # working judge on the NEXT run must still get a chance (no dedup lockout).
    report2 = run_standup(
        db_path=db,
        repo_root=db.parent,
        events_fn=_fixed_events([]),
        rubric=rubric,  # type: ignore[arg-type]
        now=_NOW,
        user_id=USER,
        judge_caller=_judge(1.0),
        config=StandupConfig(),
    )
    assert report2.deduped == 0
    assert report2.delivered == 1
