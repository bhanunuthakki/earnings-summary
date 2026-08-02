"""Tests for the thesis-materiality elevation gate (filings.materiality_judgment).

Weighted toward the gate's failure modes:

  * a ticker with NO thesis on file must be skipped without an LLM call —
    the "does this restrict measuring the thesis?" question is unanswerable,
    and a guessed verdict would be a fabricated elevation decision;
  * an LLM failure must leave every candidate NULL (not elevated), never a
    defaulted verdict in either direction;
  * table-shaped backlog excerpts are deferred before the call — counted,
    unjudged, and never charged for;
  * a response missing one id retries only that id once; a second omission
    leaves THAT row NULL while preserving the rest;
  * candidate fetch enforces the elevation eligibility contract (dismissed /
    noise-class / evidence-less rows never reach the judge; re-runs are
    idempotent via the NULL filter);
  * hard stops (budget/setup) propagate instead of degrading.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from filings import materiality_judgment as mj  # noqa: E402
from filings.materiality_judgment import (  # noqa: E402
    JudgmentCandidate,
    MaterialityVerdict,
    ThesisMateriality,
)
from llm.cli import LLMBudgetExceeded  # noqa: E402

_SCHEMA = """
CREATE TABLE disclosure_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    event_type VARCHAR(32) NOT NULL,
    fiscal_year INTEGER,
    fiscal_period VARCHAR(4),
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
    detector_version VARCHAR(32) NOT NULL DEFAULT 'test',
    status VARCHAR(16) NOT NULL DEFAULT 'new',
    created_at DATETIME NOT NULL,
    thesis_materiality VARCHAR(32),
    thesis_materiality_rationale TEXT,
    thesis_materiality_judged_at DATETIME
);
"""

_ANCHOR = (
    "## THESIS ANCHOR (analyst's own framing of this name)\n"
    "**Thesis statement:** Deposit-funded lender compounding via credit cohorts.\n"
    "**Tier-1 KPIs (with break conditions):**\n"
    "- **90-day NPL by vintage** — breaks if formation exceeds 8%"
)

_TABULAR_EXCERPT = (
    "Indonesia operations900: Total3,380 Gold (thousands of recoverable ounces)1,204 "
    "Copper (millions of recoverable pounds)3,001 Revenues11,449 2,118 903 4,201 "
    "DD&A: Totals presented above$11,449 $2,118 $903 $4,201 $1,220 $998"
)


def _insert_event(
    conn: sqlite3.Connection,
    *,
    ticker: str = "NU",
    event_type: str = "metric_discontinued",
    subject: str = "npl_by_vintage",
    verdict: str = "substantive",
    status: str = "new",
    evidence_quote: str | None = "We no longer disclose 90-day NPL by vintage.",
    current_excerpt: str | None = None,
    prior_excerpt: str | None = None,
    thesis_materiality: str | None = None,
    created_at: str = "2026-07-25T10:00:00",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO disclosure_events
        (ticker, event_type, fiscal_year, fiscal_period, canonical_id, subject,
         prior_excerpt, current_excerpt, evidence_quote, verdict, status,
         thesis_materiality, created_at)
        VALUES (?, ?, 2026, 'Q2', 'mdna', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ticker,
            event_type,
            subject,
            prior_excerpt,
            current_excerpt,
            evidence_quote,
            verdict,
            status,
            thesis_materiality,
            created_at,
        ),
    )
    return int(cur.lastrowid or 0)


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript(_SCHEMA)
    connection.commit()
    return connection


def _candidate(event_id: int, excerpt: str, subject: str = "npl_by_vintage") -> JudgmentCandidate:
    return JudgmentCandidate(
        id=event_id,
        ticker="NU",
        event_type="metric_discontinued",
        canonical_id="mdna",
        fiscal_year=2026,
        fiscal_period="Q2",
        subject=subject,
        subject_label=None,
        verdict="substantive",
        excerpt=excerpt,
    )


# ----- candidate fetch: the eligibility contract -----


def test_fetch_excludes_dismissed_noise_class_and_evidence_less_rows(
    conn: sqlite3.Connection,
) -> None:
    keep = _insert_event(conn, subject="keep")
    _insert_event(conn, subject="dismissed", status="dismissed")
    _insert_event(conn, subject="noise", verdict="noise")
    _insert_event(conn, subject="mechanical", verdict="mechanical")
    _insert_event(conn, subject="boiler", verdict="boilerplate_update")
    _insert_event(
        conn, subject="no_evidence", evidence_quote=None, current_excerpt=None, prior_excerpt=None
    )
    got = mj.fetch_judgment_candidates(conn, "NU")
    assert [c.id for c in got] == [keep]


def test_fetch_default_skips_already_judged_rows_and_rejudge_includes_them(
    conn: sqlite3.Connection,
) -> None:
    judged = _insert_event(conn, subject="judged", thesis_materiality="not_material")
    fresh = _insert_event(conn, subject="fresh", created_at="2026-07-26T10:00:00")
    assert [c.id for c in mj.fetch_judgment_candidates(conn, "NU")] == [fresh]
    everything = mj.fetch_judgment_candidates(conn, "NU", only_unjudged=False)
    assert {c.id for c in everything} == {judged, fresh}


def test_fetch_orders_newest_first_and_respects_limit(conn: sqlite3.Connection) -> None:
    _insert_event(conn, subject="old", created_at="2026-01-01T10:00:00")
    mid = _insert_event(conn, subject="mid", created_at="2026-06-01T10:00:00")
    new = _insert_event(conn, subject="new", created_at="2026-07-01T10:00:00")
    got = mj.fetch_judgment_candidates(conn, "NU", limit=2)
    assert [c.id for c in got] == [new, mid]


def test_fetch_falls_back_to_excerpts_for_the_judged_text(conn: sqlite3.Connection) -> None:
    _insert_event(
        conn,
        subject="excerpt_only",
        evidence_quote=None,
        prior_excerpt="We previously reported vintage-level NPL formation.",
    )
    (candidate,) = mj.fetch_judgment_candidates(conn, "NU")
    assert candidate.excerpt.startswith("We previously reported")


# ----- judge: no-thesis skip, tabular deferral, degradation -----


def test_no_thesis_anchor_skips_without_llm_call(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def spy(prompt: str, **kwargs: object) -> dict[str, object]:
        calls.append(prompt)
        return {}

    monkeypatch.setattr(mj, "call_llm_structured", spy)
    outcome = mj.judge_ticker_events("NU", [_candidate(1, "text")], "   ")
    assert outcome.skipped_no_thesis is True
    assert outcome.verdicts == {}
    assert calls == []


def test_tabular_excerpts_deferred_not_judged(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts: list[str] = []

    def fake(prompt: str, **kwargs: object) -> dict[str, object]:
        prompts.append(prompt)
        return {
            "1": {
                "materiality": "restricts_measurement",
                "confidence": 0.9,
                "rationale": "NPL vintage KPI no longer observable",
            }
        }

    monkeypatch.setattr(mj, "call_llm_structured", fake)
    outcome = mj.judge_ticker_events(
        "NU",
        [_candidate(1, "We no longer disclose NPL by vintage."), _candidate(2, _TABULAR_EXCERPT)],
        _ANCHOR,
    )
    assert outcome.deferred_tabular == 1
    assert set(outcome.verdicts) == {1}
    assert len(prompts) == 1
    assert "id=2" not in prompts[0]


def test_prompt_carries_anchor_and_default_bar(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts: list[str] = []

    def fake(prompt: str, **kwargs: object) -> dict[str, object]:
        prompts.append(prompt)
        return {"1": {"materiality": "not_material", "confidence": 0.8, "rationale": "unaffected"}}

    monkeypatch.setattr(mj, "call_llm_structured", fake)
    outcome = mj.judge_ticker_events("NU", [_candidate(1, "reworded risk language")], _ANCHOR)
    assert outcome.verdicts[1].materiality is ThesisMateriality.NOT_MATERIAL
    assert _ANCHOR.splitlines()[0] in prompts[0]
    assert "restricts_measurement" in prompts[0]
    assert "not_material" in prompts[0]


def test_llm_failure_degrades_with_empty_verdicts(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(prompt: str, **kwargs: object) -> dict[str, object]:
        raise ValueError("transport hiccup")

    monkeypatch.setattr(mj, "call_llm_structured", boom)
    outcome = mj.judge_ticker_events("NU", [_candidate(1, "prose change")], _ANCHOR)
    assert outcome.degraded is True
    assert outcome.verdicts == {}
    assert "transport hiccup" in (outcome.degrade_reason or "")


def test_hard_stop_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    def blown(prompt: str, **kwargs: object) -> dict[str, object]:
        raise LLMBudgetExceeded("monthly cap")

    monkeypatch.setattr(mj, "call_llm_structured", blown)
    with pytest.raises(LLMBudgetExceeded):
        mj.judge_ticker_events("NU", [_candidate(1, "prose change")], _ANCHOR)


def test_missing_id_recovers_once_then_stays_null(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts: list[str] = []

    def fake(prompt: str, **kwargs: object) -> dict[str, object]:
        prompts.append(prompt)
        if len(prompts) == 1:
            return {
                "1": {
                    "materiality": "restricts_measurement",
                    "confidence": 0.9,
                    "rationale": "segment KPI aggregated away",
                }
            }
        # Recovery response still omits id 2 and returns junk for id 3.
        return {"3": {"materiality": "invented_value", "confidence": 0.5, "rationale": "x"}}

    monkeypatch.setattr(mj, "call_llm_structured", fake)
    outcome = mj.judge_ticker_events(
        "NU",
        [_candidate(1, "prose one"), _candidate(2, "prose two")],
        _ANCHOR,
    )
    assert set(outcome.verdicts) == {1}
    assert len(prompts) == 2
    assert "id=2" in prompts[1]
    assert "id=1" not in prompts[1]


# ----- persistence -----


def test_write_judgments_persists_by_id_and_leaves_others_null(
    conn: sqlite3.Connection,
) -> None:
    first = _insert_event(conn, subject="first")
    second = _insert_event(conn, subject="second", created_at="2026-07-26T10:00:00")
    written = mj.write_judgments(
        conn,
        [
            MaterialityVerdict(
                event_id=first,
                materiality=ThesisMateriality.RESTRICTS_MEASUREMENT,
                confidence=0.9,
                rationale="tier-1 KPI input dropped",
            )
        ],
    )
    assert written == 1
    rows = dict(
        conn.execute("SELECT id, thesis_materiality FROM disclosure_events ORDER BY id").fetchall()
    )
    assert rows[first] == "restricts_measurement"
    assert rows[second] is None
    stamp = conn.execute(
        "SELECT thesis_materiality_judged_at FROM disclosure_events WHERE id = ?", (first,)
    ).fetchone()[0]
    assert stamp is not None


def test_write_judgments_is_idempotent(conn: sqlite3.Connection) -> None:
    event_id = _insert_event(conn)
    verdict = MaterialityVerdict(
        event_id=event_id,
        materiality=ThesisMateriality.NOT_MATERIAL,
        confidence=0.7,
        rationale="metric remains observable",
    )
    mj.write_judgments(conn, [verdict])
    mj.write_judgments(conn, [verdict])
    row = conn.execute(
        "SELECT thesis_materiality, thesis_materiality_rationale FROM disclosure_events "
        "WHERE id = ?",
        (event_id,),
    ).fetchone()
    assert row == ("not_material", "metric remains observable")


def test_verdict_payload_round_trips_through_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """The judge accepts exactly what the prompt instructs the model to emit."""
    payload = json.loads(
        '{"1": {"materiality": "restricts_measurement", "confidence": 0.85, '
        '"rationale": "cohort disclosure removed"}}'
    )

    def fake(prompt: str, **kwargs: object) -> dict[str, object]:
        return payload

    monkeypatch.setattr(mj, "call_llm_structured", fake)
    outcome = mj.judge_ticker_events("NU", [_candidate(1, "prose change")], _ANCHOR)
    assert outcome.verdicts[1].materiality is ThesisMateriality.RESTRICTS_MEASUREMENT
    assert outcome.verdicts[1].rationale == "cohort disclosure removed"
