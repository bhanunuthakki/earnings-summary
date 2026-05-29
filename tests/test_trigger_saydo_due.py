"""End-to-end tests for the SayDo-due trigger.

Mirrors ``test_trigger_kpi_inflection`` for the LLM-mock plumbing
(``monkeypatch.setattr("triggers.saydo_due.call_llm", ...)``) and the same
deterministic-signal contract: the verdict is computed in code, so the alert
MUST fire even when the LLM is down — the LLM only adds an optional context
line. Verifies:

  * Protocol conformance + kind/cadence
  * signature_key_evidence keys on (kpi_name, period_target)
  * verdict math: ge/le/lt strict pass-fail, eq tolerance band (MET/MIXED/MISSED)
  * outcome normalization: hit/beat/met → MET, miss → MISSED, no_data → ungradeable
  * scan: due+gradeable → 1 candidate (read & computed verdicts); not-yet-due,
    no-realized, and outside-recency → []; prior_miss_count counts prior misses
  * should_fire: True for MET / MISSED / MIXED
  * build_alert: deterministic memo with the LLM DOWN (alert still fires,
    llm_context_available=False); LLM UP appends a context line and caches it
    (second build is a cache hit, no re-invoke); repeat-miss adds the tally line
  * draft_actions: MISSED adds bear_append; MET does not
  * full pipeline integration smoke (a missed WIX commitment coming due)
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from alerts.store import compute_signature_sha
from triggers import SayDoDueTrigger, UserStateContext
from triggers.base import AlertDraft, Cadence, ThesisAnchor, Trigger, TriggerCandidate
from triggers.saydo_due import Verdict

# Computed once; day-level offsets are immune to the few-second skew between
# this and scan()'s own ``now``.
_NOW = datetime.now(UTC).replace(tzinfo=None)


def _ts(days_from_now: int) -> str:
    """ISO timestamp ``days_from_now`` from now (negative = past)."""
    return (_NOW + timedelta(days=days_from_now)).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Schema + fixtures
# ---------------------------------------------------------------------------


def _create_schema(conn: sqlite3.Connection) -> None:
    """management_commitments (alembic 0017) + the llm_artifacts cache table."""
    _ = conn.execute(
        "CREATE TABLE management_commitments ("
        + "id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, "
        + "period_made TIMESTAMP NOT NULL, transcript_segment_id INTEGER NOT NULL, "
        + "period_target TIMESTAMP NOT NULL, kpi_name TEXT NOT NULL, "
        + "comparator TEXT NOT NULL, target_value NUMERIC NOT NULL, unit TEXT NOT NULL, "
        + "narrative TEXT NOT NULL, realized_value NUMERIC, realized_doc_id INTEGER, "
        + "outcome TEXT, evaluated_at TIMESTAMP, created_at TIMESTAMP NOT NULL"
        + ")"
    )
    _ = conn.execute(
        "CREATE TABLE llm_artifacts ("
        + "id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, "
        + "scope TEXT NOT NULL DEFAULT 'ticker', purpose TEXT NOT NULL, "
        + "fiscal_period TEXT, content_md TEXT, content_json TEXT, "
        + "input_sha256 TEXT NOT NULL, output_sha256 TEXT, model TEXT, "
        + "prompt_version TEXT NOT NULL DEFAULT 'v1', generated_at TEXT NOT NULL, "
        + "expires_at TEXT, superseded_by_id INTEGER, dirty INTEGER NOT NULL DEFAULT 0, "
        + "dirty_reason TEXT, source_doc_ids TEXT, parent_artifact_ids TEXT, "
        + "llm_call_id INTEGER"
        + ")"
    )


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create the test DB and route every ``DB_PATH`` consumer to it."""
    path = tmp_path / "test.db"
    conn = sqlite3.connect(str(path))
    try:
        _create_schema(conn)
        _ = conn.execute("PRAGMA journal_mode=WAL")
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr("db.DB_PATH", str(path))
    return path


@pytest.fixture
def fixture_conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Yield a connection on the fixture DB (used as scan()'s ``db`` arg)."""
    conn = sqlite3.connect(str(db_path))
    try:
        yield conn
    finally:
        conn.close()


def _seed_commitment(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    kpi_name: str,
    period_target: str,
    comparator: str = "ge",
    target_value: float = 30.0,
    unit: str = "percent",
    narrative: str = "Revenue growth above 30% by year-end",
    realized_value: float | None = None,
    outcome: str | None = None,
    period_made: str | None = None,
) -> None:
    """Insert one management_commitments row and commit."""
    made = period_made if period_made is not None else _ts(-300)
    _ = conn.execute(
        "INSERT INTO management_commitments "
        + "(ticker, period_made, transcript_segment_id, period_target, kpi_name, "
        + " comparator, target_value, unit, narrative, realized_value, outcome, created_at) "
        + "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ticker,
            made,
            1,
            period_target,
            kpi_name,
            comparator,
            target_value,
            unit,
            narrative,
            realized_value,
            outcome,
            made,
        ),
    )
    conn.commit()


def _empty_state() -> UserStateContext:
    return UserStateContext(
        registered_kpis=[], sizing_intents=[], recent_dismissed_signatures=set()
    )


def _make_candidate(
    *,
    ticker: str = "WIX",
    kpi_name: str = "Revenue YoY Growth",
    period_made: str = "2025-02-15",
    period_target: str = "2025-12-31",
    comparator: str = "ge",
    target_value: float = 30.0,
    realized_value: float = 22.0,
    unit: str = "percent",
    narrative: str = "Revenue growth to reaccelerate above 30% by year-end",
    outcome: str = Verdict.MISSED.value,
    prior_miss_count: int = 0,
) -> TriggerCandidate:
    evidence: dict[str, object] = {
        "kpi_name": kpi_name,
        "period_made": period_made,
        "period_target": period_target,
        "comparator": comparator,
        "target_value": target_value,
        "realized_value": realized_value,
        "unit": unit,
        "narrative": narrative,
        "outcome": outcome,
        "prior_miss_count": prior_miss_count,
    }
    return TriggerCandidate(
        ticker=ticker,
        kind="saydo_due",
        key=f"{ticker}:{kpi_name}:{period_target}",
        evidence=evidence,
        computed_at=datetime.now(UTC).replace(tzinfo=None),
    )


def _make_alert(*, ticker: str = "WIX", memo_text: str = "memo") -> AlertDraft:
    return AlertDraft(
        trigger_kind="saydo_due",
        ticker=ticker,
        fired_at=datetime.now(UTC).replace(tzinfo=None),
        evidence_json="{}",
        signature_sha="0" * 64,
        memo_text=memo_text,
    )


class _StatefulLLM:
    """Tracks call count + returns canned plain-text context in sequence."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.call_count = 0

    def __call__(self, prompt: str, **_kwargs: object) -> str:
        idx = min(self.call_count, len(self.responses) - 1)
        self.call_count += 1
        return self.responses[idx]


def _raise_llm(*_args: object, **_kwargs: object) -> str:
    raise RuntimeError("LLM unavailable for test")


def _stub_anchor(*_args: object, **_kwargs: object) -> str:
    return ""


# ---------------------------------------------------------------------------
# Protocol / signature
# ---------------------------------------------------------------------------


def test_protocol_conformance_and_metadata() -> None:
    trigger = SayDoDueTrigger()
    assert isinstance(trigger, Trigger)
    assert trigger.kind == "saydo_due"
    assert trigger.cadence == Cadence.CALENDAR_DRIVEN


def test_signature_key_evidence_keys_on_kpi_and_target() -> None:
    candidate = _make_candidate(kpi_name="GMV growth", period_target="2025-12-31")
    key_evidence = SayDoDueTrigger().signature_key_evidence(candidate)
    assert dict(key_evidence) == {
        "kpi_name": "GMV growth",
        "period_target": "2025-12-31",
    }


# ---------------------------------------------------------------------------
# Verdict math + outcome normalization (deterministic core, via public scan)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("comparator", "target", "realized", "expected"),
    [
        ("ge", 30.0, 32.0, Verdict.MET.value),
        ("ge", 30.0, 28.0, Verdict.MISSED.value),
        (">=", 30.0, 30.0, Verdict.MET.value),  # symbol form, boundary
        ("le", 5.0, 4.0, Verdict.MET.value),
        ("lt", 5.0, 5.0, Verdict.MISSED.value),
        ("eq", 100.0, 103.0, Verdict.MET.value),  # within 5%
        ("eq", 100.0, 108.0, Verdict.MIXED.value),  # 5-10% off
        ("eq", 100.0, 120.0, Verdict.MISSED.value),  # beyond 10%
    ],
)
def test_scan_computes_verdict_from_comparator(
    fixture_conn: sqlite3.Connection,
    comparator: str,
    target: float,
    realized: float,
    expected: str,
) -> None:
    # outcome NULL → verdict computed inline from the comparator arithmetic.
    _seed_commitment(
        fixture_conn,
        ticker="ZT",
        kpi_name="K",
        period_target=_ts(-15),
        comparator=comparator,
        target_value=target,
        realized_value=realized,
        outcome=None,
    )
    candidates = SayDoDueTrigger().scan("ZT", fixture_conn)
    assert len(candidates) == 1
    assert candidates[0].evidence["outcome"] == expected


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("hit", Verdict.MET.value),
        ("beat", Verdict.MET.value),
        ("met", Verdict.MET.value),
        ("miss", Verdict.MISSED.value),
    ],
)
def test_scan_reads_stored_outcome_over_computing(
    fixture_conn: sqlite3.Connection, stored: str, expected: str
) -> None:
    # realized (104) < target (110) would compute MISSED, so a stored MET-family
    # value proves the READ path wins over recomputation.
    _seed_commitment(
        fixture_conn,
        ticker="ZT",
        kpi_name="K",
        period_target=_ts(-15),
        comparator="ge",
        target_value=110.0,
        realized_value=104.0,
        outcome=stored,
    )
    candidates = SayDoDueTrigger().scan("ZT", fixture_conn)
    assert len(candidates) == 1
    assert candidates[0].evidence["outcome"] == expected


def test_scan_unrecognized_outcome_falls_through_to_compute(
    fixture_conn: sqlite3.Connection,
) -> None:
    # A populated-but-unrecognized outcome string doesn't block grading; the
    # verdict is recomputed from the arithmetic (ge, 30, 32 → MET).
    _seed_commitment(
        fixture_conn,
        ticker="ZT",
        kpi_name="K",
        period_target=_ts(-15),
        comparator="ge",
        target_value=30.0,
        realized_value=32.0,
        outcome="weird_status",
    )
    candidates = SayDoDueTrigger().scan("ZT", fixture_conn)
    assert len(candidates) == 1
    assert candidates[0].evidence["outcome"] == Verdict.MET.value


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------


def test_scan_due_gradeable_computes_verdict(
    fixture_conn: sqlite3.Connection,
) -> None:
    # outcome NULL → verdict computed inline from comparator arithmetic.
    _seed_commitment(
        fixture_conn,
        ticker="WIX",
        kpi_name="Revenue YoY Growth",
        period_target=_ts(-15),
        comparator="ge",
        target_value=30.0,
        realized_value=32.0,
        outcome=None,
    )
    candidates = SayDoDueTrigger().scan("WIX", fixture_conn)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.kind == "saydo_due"
    ev = candidate.evidence
    assert ev["kpi_name"] == "Revenue YoY Growth"
    assert ev["outcome"] == Verdict.MET.value
    assert ev["target_value"] == pytest.approx(30.0)
    assert ev["realized_value"] == pytest.approx(32.0)
    assert ev["prior_miss_count"] == 0
    assert candidate.key.startswith("WIX:Revenue YoY Growth:")


def test_scan_not_yet_due_returns_empty(fixture_conn: sqlite3.Connection) -> None:
    # period_target is in the future → not due → no candidate.
    _seed_commitment(
        fixture_conn,
        ticker="WIX",
        kpi_name="Revenue YoY Growth",
        period_target=_ts(60),
        realized_value=32.0,
    )
    assert SayDoDueTrigger().scan("WIX", fixture_conn) == []


def test_scan_no_realized_value_returns_empty(
    fixture_conn: sqlite3.Connection,
) -> None:
    # Due, but data not in yet (realized_value NULL) → not gradeable → skip.
    _seed_commitment(
        fixture_conn,
        ticker="WIX",
        kpi_name="Revenue YoY Growth",
        period_target=_ts(-15),
        realized_value=None,
        outcome=None,
    )
    assert SayDoDueTrigger().scan("WIX", fixture_conn) == []


def test_scan_outside_recency_window_returns_empty(
    fixture_conn: sqlite3.Connection,
) -> None:
    # Due and gradeable, but the target arrived long ago → not surfaced (anti-flood).
    _seed_commitment(
        fixture_conn,
        ticker="WIX",
        kpi_name="Revenue YoY Growth",
        period_target=_ts(-200),
        realized_value=18.0,
        outcome=None,
    )
    assert SayDoDueTrigger().scan("WIX", fixture_conn) == []


def test_scan_missing_table_returns_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A DB without the management_commitments table → [] (best-effort), no raise.
    path = tmp_path / "bare.db"
    conn = sqlite3.connect(str(path))
    conn.commit()
    monkeypatch.setattr("db.DB_PATH", str(path))
    try:
        assert SayDoDueTrigger().scan("WIX", conn) == []
    finally:
        conn.close()


def test_scan_prior_miss_count_counts_prior_misses(
    fixture_conn: sqlite3.Connection,
) -> None:
    # Two earlier misses (outside the recency window) + one due miss in-window.
    _seed_commitment(
        fixture_conn,
        ticker="WIX",
        kpi_name="Revenue YoY Growth",
        period_target=_ts(-250),
        comparator="ge",
        target_value=30.0,
        realized_value=10.0,
        outcome=None,
    )
    _seed_commitment(
        fixture_conn,
        ticker="WIX",
        kpi_name="Revenue YoY Growth",
        period_target=_ts(-200),
        comparator="ge",
        target_value=30.0,
        realized_value=12.0,
        outcome=None,
    )
    _seed_commitment(
        fixture_conn,
        ticker="WIX",
        kpi_name="Revenue YoY Growth",
        period_target=_ts(-10),
        comparator="ge",
        target_value=30.0,
        realized_value=18.0,
        outcome=None,
    )
    candidates = SayDoDueTrigger().scan("WIX", fixture_conn)
    # Only the in-window commitment is surfaced; the two old ones count as prior.
    assert len(candidates) == 1
    assert candidates[0].evidence["outcome"] == Verdict.MISSED.value
    assert candidates[0].evidence["prior_miss_count"] == 2


# ---------------------------------------------------------------------------
# should_fire
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verdict", [Verdict.MET, Verdict.MISSED, Verdict.MIXED])
def test_should_fire_true_for_every_verdict(verdict: Verdict) -> None:
    candidate = _make_candidate(outcome=verdict.value)
    assert SayDoDueTrigger().should_fire(candidate, _empty_state()) is True


def test_should_fire_false_for_non_verdict_outcome() -> None:
    # Defensive: an outcome that isn't one of the three verdicts does not fire.
    candidate = _make_candidate(outcome="no_data")
    assert SayDoDueTrigger().should_fire(candidate, _empty_state()) is False


# ---------------------------------------------------------------------------
# build_alert — deterministic memo first; LLM context is best-effort
# ---------------------------------------------------------------------------


def test_build_alert_fires_when_llm_down(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM raises → the alert STILL fires with the deterministic factual memo;
    llm_context_available is False. This is the core contract."""
    monkeypatch.setattr("triggers.saydo_due.call_llm", _raise_llm)
    monkeypatch.setattr("triggers.saydo_due.load_thesis_anchor", _stub_anchor)
    candidate = _make_candidate(
        kpi_name="Revenue YoY Growth",
        target_value=30.0,
        realized_value=22.0,
        outcome=Verdict.MISSED.value,
    )
    alert = SayDoDueTrigger().build_alert(candidate, None)

    assert alert.trigger_kind == "saydo_due"
    assert alert.ticker == "WIX"
    assert alert.memo_text is not None
    # The factual core carries the verdict + the numbers it was graded on.
    assert "Revenue YoY Growth" in alert.memo_text
    assert Verdict.MISSED.value in alert.memo_text
    assert "22" in alert.memo_text
    assert "30" in alert.memo_text

    ev = json.loads(alert.evidence_json)
    assert ev["llm_context_available"] is False
    assert ev["outcome"] == Verdict.MISSED.value
    assert ev["prior_miss_count"] == 0
    expected_sig = compute_signature_sha(
        "saydo_due", "WIX", {"kpi_name": "Revenue YoY Growth", "period_target": "2025-12-31"}
    )
    assert alert.signature_sha == expected_sig


def test_build_alert_appends_llm_context_and_caches(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLM succeeds → context line appended; a second build is a cache hit and
    does NOT re-invoke the LLM."""
    context_line = "A missed reacceleration promise dents management credibility."
    mock = _StatefulLLM([context_line])
    monkeypatch.setattr("triggers.saydo_due.call_llm", mock)
    monkeypatch.setattr("triggers.saydo_due.load_thesis_anchor", _stub_anchor)
    candidate = _make_candidate()

    first = SayDoDueTrigger().build_alert(candidate, None)
    assert mock.call_count == 1
    assert first.memo_text is not None
    assert context_line in first.memo_text

    ev1 = json.loads(first.evidence_json)
    assert ev1["llm_context_available"] is True

    # Second build — same candidate, same inputs → artifact-store cache hit.
    second = SayDoDueTrigger().build_alert(candidate, None)
    assert mock.call_count == 1, "cache miss — call_llm was re-invoked"
    assert second.memo_text == first.memo_text


def test_build_alert_empty_llm_response_degrades_to_factual(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blank LLM response is treated as no context (not appended)."""
    monkeypatch.setattr("triggers.saydo_due.call_llm", _StatefulLLM(["   "]))
    monkeypatch.setattr("triggers.saydo_due.load_thesis_anchor", _stub_anchor)
    candidate = _make_candidate()
    alert = SayDoDueTrigger().build_alert(candidate, None)

    ev = json.loads(alert.evidence_json)
    assert ev["llm_context_available"] is False
    assert alert.memo_text is not None and alert.memo_text.endswith(".")


def test_build_alert_repeat_miss_adds_tally_line(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A MISSED verdict with prior misses on record adds an ordinal tally line."""
    monkeypatch.setattr("triggers.saydo_due.call_llm", _raise_llm)
    monkeypatch.setattr("triggers.saydo_due.load_thesis_anchor", _stub_anchor)
    candidate = _make_candidate(outcome=Verdict.MISSED.value, prior_miss_count=2)
    alert = SayDoDueTrigger().build_alert(candidate, None)
    assert alert.memo_text is not None
    # prior_miss_count=2 → this is the 3rd miss.
    assert "3rd" in alert.memo_text


def test_build_alert_met_has_no_tally_line(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A MET verdict never carries the repeat-miss tally, even with priors."""
    monkeypatch.setattr("triggers.saydo_due.call_llm", _raise_llm)
    monkeypatch.setattr("triggers.saydo_due.load_thesis_anchor", _stub_anchor)
    candidate = _make_candidate(outcome=Verdict.MET.value, realized_value=33.0, prior_miss_count=2)
    alert = SayDoDueTrigger().build_alert(candidate, None)
    assert alert.memo_text is not None
    assert "miss for" not in alert.memo_text


# ---------------------------------------------------------------------------
# draft_actions
# ---------------------------------------------------------------------------


def test_draft_actions_missed_adds_bear_append() -> None:
    candidate = _make_candidate(outcome=Verdict.MISSED.value)
    alert = _make_alert(memo_text="WIX ... Verdict: MISSED (realized: 22%).")
    actions = SayDoDueTrigger().draft_actions(alert, candidate)

    kinds = sorted(a.action_kind for a in actions)
    assert kinds == ["bear_append", "earnings_prep_append", "thesis_update"]
    by_kind = {a.action_kind: a.payload for a in actions}
    assert by_kind["earnings_prep_append"]["kpi_name"] == "Revenue YoY Growth"
    assert by_kind["bear_append"]["kpi_name"] == "Revenue YoY Growth"
    assert by_kind["thesis_update"]["outcome"] == Verdict.MISSED.value


def test_draft_actions_mixed_adds_bear_append() -> None:
    candidate = _make_candidate(outcome=Verdict.MIXED.value)
    alert = _make_alert()
    kinds = sorted(a.action_kind for a in SayDoDueTrigger().draft_actions(alert, candidate))
    assert kinds == ["bear_append", "earnings_prep_append", "thesis_update"]


def test_draft_actions_met_has_no_bear_append() -> None:
    candidate = _make_candidate(outcome=Verdict.MET.value, realized_value=33.0)
    alert = _make_alert()
    actions = SayDoDueTrigger().draft_actions(alert, candidate)
    kinds = sorted(a.action_kind for a in actions)
    assert kinds == ["earnings_prep_append", "thesis_update"]
    by_kind = {a.action_kind: a.payload for a in actions}
    assert by_kind["thesis_update"]["outcome"] == Verdict.MET.value


# ---------------------------------------------------------------------------
# Full pipeline integration smoke
# ---------------------------------------------------------------------------


def test_full_pipeline_integration_smoke(
    fixture_conn: sqlite3.Connection, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A WIX commitment ('revenue >= 30% by year-end') that just came due and
    was missed → scan → should_fire → build_alert → draft_actions returns the
    expected shape (bear + thesis + earnings-prep)."""
    _seed_commitment(
        fixture_conn,
        ticker="WIX",
        kpi_name="Revenue YoY Growth",
        period_target=_ts(-12),
        comparator="ge",
        target_value=30.0,
        realized_value=18.0,
        narrative="Revenue growth to reaccelerate above 30% by year-end",
        outcome=None,
    )
    monkeypatch.setattr(
        "triggers.saydo_due.call_llm",
        _StatefulLLM(["A broken reacceleration promise challenges the growth thesis."]),
    )
    monkeypatch.setattr("triggers.saydo_due.load_thesis_anchor", _stub_anchor)

    trigger = SayDoDueTrigger()
    candidates = trigger.scan("WIX", fixture_conn)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.evidence["outcome"] == Verdict.MISSED.value

    assert trigger.should_fire(candidate, _empty_state()) is True

    alert = trigger.build_alert(candidate, None)
    assert alert.ticker == "WIX"
    assert alert.memo_text is not None and "18" in alert.memo_text

    actions = trigger.draft_actions(alert, candidate)
    kinds = sorted(a.action_kind for a in actions)
    assert kinds == ["bear_append", "earnings_prep_append", "thesis_update"]


def test_build_alert_accepts_structured_anchor_argument(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Protocol's ThesisAnchor arg is accepted (and ignored — the markdown
    anchor is loaded from disk); passing one must not change the outcome."""
    monkeypatch.setattr("triggers.saydo_due.call_llm", _raise_llm)
    monkeypatch.setattr("triggers.saydo_due.load_thesis_anchor", _stub_anchor)
    candidate = _make_candidate()
    anchor = ThesisAnchor(
        ticker="WIX",
        thesis_statement="Durable 30%+ growth compounding.",
        key_driver="Revenue YoY Growth",
        tier_1_kpis=[],
        business_model_rules=[],
    )
    alert = SayDoDueTrigger().build_alert(candidate, anchor)
    assert alert.trigger_kind == "saydo_due"
    assert alert.memo_text is not None
