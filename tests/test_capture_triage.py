"""capture_triage (B3) — the primary gate on the capture->answer path.

Unit tests for the classifier (injected ``call=``, no live LLM ever) plus
integration tests over ``onmymind.respond.answer_capture``/``will_answer``
with the classifier itself stubbed (the same pattern
``tests/test_capture_poller.py`` uses for ``respond_turn``). The golden set
(``evals/golden/capture_triage.json``) and its loader/grader round out the
eval-harness wiring.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import capture.triage as triage_mod  # noqa: E402
import onmymind.respond as respond_mod  # noqa: E402
from capture.triage import (  # noqa: E402
    ROUTES,
    TriageVerdict,
    classify_capture_triage,
    classify_capture_triage_for_eval,
)
from evals.golden_classifiers import (  # noqa: E402
    ClassifierCase,
    grade_capture_triage_case,
    load_capture_triage_golden,
)
from synthesis.tenets import record_tenet  # noqa: E402
from user_state.notes import create_note, get_note  # noqa: E402

GOLDEN_PATH = PROJECT_ROOT / "evals" / "golden" / "capture_triage.json"
PRIOR_HEAD = "0059_kpi_facts_restatement"


def _cfg(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "ledger.db"
    cfg = _cfg(db)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, "head")
    return db


def _capture_note(db: Path, body: str, *, kind: str = "musing") -> int:
    row = create_note(body=body, kind=kind, ticker=None, source="capture", db_path=db)
    return row.id


# ----------------------------------------------------------------------------
# classify_capture_triage — route flows
# ----------------------------------------------------------------------------


def test_routes_enum_is_the_closed_set() -> None:
    assert set(ROUTES) == {"answer_now", "contradiction", "plain"}


def test_answer_now_route_via_injected_call(tmp_path: Path) -> None:
    missing_db = tmp_path / "missing.db"

    def call(_note_body: str, _context: str) -> dict[str, object]:
        return {"route": "answer_now", "why": "asks a direct question"}

    v = classify_capture_triage("What's my cost basis on MELI?", db_path=missing_db, call=call)
    assert v.route == "answer_now" and not v.degraded
    assert v.conflict_token is None and v.conflict_kind is None


def test_plain_route_via_injected_call(tmp_path: Path) -> None:
    missing_db = tmp_path / "missing.db"

    def call(_note_body: str, _context: str) -> dict[str, object]:
        return {"route": "plain"}

    v = classify_capture_triage("Add to watchlist: Dassault", db_path=missing_db, call=call)
    assert v.route == "plain" and not v.degraded


def test_contradiction_route_resolves_conflict_fields(db_path: Path) -> None:
    tenet = record_tenet(
        body_md="I cap any single name at 15% of the book.",
        status="current",
        db_path=db_path,
    )
    token = f"T{tenet.id}"

    def call(_note_body: str, context: str) -> dict[str, object]:
        assert token in context  # the rendered context must actually show the tenet
        return {"route": "contradiction", "conflicts_with": token, "why": "sizing discipline"}

    v = classify_capture_triage("doubling down on MELI here", db_path=db_path, call=call)
    assert v.route == "contradiction" and not v.degraded
    assert v.conflict_token == token
    assert v.conflict_kind == "tenet"
    assert v.conflict_id == tenet.id
    assert v.conflict_body.startswith("I cap any single name")
    assert v.why == "sizing discipline"


def test_contradiction_with_fabricated_token_falls_back_to_regex(tmp_path: Path) -> None:
    missing_db = tmp_path / "missing.db"

    def call(_note_body: str, _context: str) -> dict[str, object]:
        # T999 was never rendered (context is empty on a missing DB) — a
        # fabricated citation must never manufacture a contradiction.
        return {"route": "contradiction", "conflicts_with": "T999", "why": "??"}

    plain = classify_capture_triage("MELI looks cheap here", db_path=missing_db, call=call)
    assert plain.route == "plain" and plain.conflict_token is None and not plain.degraded

    question = classify_capture_triage(
        "What's my cost basis on MELI?", db_path=missing_db, call=call
    )
    assert question.route == "answer_now" and question.conflict_token is None


def test_contradiction_missing_conflicts_with_falls_back(tmp_path: Path) -> None:
    missing_db = tmp_path / "missing.db"

    def call(_note_body: str, _context: str) -> dict[str, object]:
        return {"route": "contradiction"}  # no conflicts_with at all

    v = classify_capture_triage("MELI looks cheap here", db_path=missing_db, call=call)
    assert v.route == "plain" and v.conflict_kind is None


def test_unknown_route_falls_back_to_regex(tmp_path: Path) -> None:
    missing_db = tmp_path / "missing.db"
    v = classify_capture_triage(
        "MELI looks cheap here", db_path=missing_db, call=lambda *_a: {"route": "banana"}
    )
    assert v.route == "plain" and not v.degraded


def test_transient_failure_degrades_to_regex_with_flag(tmp_path: Path) -> None:
    missing_db = tmp_path / "missing.db"

    def boom(_note_body: str, _context: str) -> dict[str, object]:
        raise RuntimeError("LLM layer unavailable")

    plain = classify_capture_triage("MELI looks cheap here", db_path=missing_db, call=boom)
    assert plain.route == "plain" and plain.degraded

    answer = classify_capture_triage("What's my cost basis on MELI?", db_path=missing_db, call=boom)
    assert answer.route == "answer_now" and answer.degraded


def test_classify_capture_triage_never_raises(tmp_path: Path) -> None:
    missing_db = tmp_path / "missing.db"

    def boom(*_a: object, **_k: object) -> dict[str, object]:
        raise ValueError("anything at all")

    # Must not raise even for a hard-stop-shaped exception — the classifier
    # sits on the capture-landing path and must never break a landed capture.
    v = classify_capture_triage("x", db_path=missing_db, call=boom)
    assert v.route in ROUTES


# ----------------------------------------------------------------------------
# eval-harness entry point
# ----------------------------------------------------------------------------


def test_eval_entry_returns_pinned_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(_note_body: str, context: str) -> dict[str, object]:
        assert "T12" in context
        return {"route": "contradiction", "conflicts_with": "T12", "why": "x"}

    monkeypatch.setattr(triage_mod, "_default_call", fake)
    case = {
        "text": "doubling down on MELI",
        "context": {
            "tenets": [
                {"id": 12, "scope_key": "tenet:sizing", "body": "cap at 15%", "as_of": "2026-03-14"}
            ]
        },
    }
    out = classify_capture_triage_for_eval(case)
    assert out == {"route": "contradiction", "conflicts_with": "T12"}


def test_eval_entry_grounding_gate_over_case_context(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(_note_body: str, _context: str) -> dict[str, object]:
        return {"route": "contradiction", "conflicts_with": "T999"}  # not in the case's context

    monkeypatch.setattr(triage_mod, "_default_call", fake)
    case = {"text": "doubling down on MELI", "context": {}}
    out = classify_capture_triage_for_eval(case)
    assert out == {"route": "plain", "conflicts_with": None}


# ----------------------------------------------------------------------------
# onmymind.respond.answer_capture / will_answer — triage wired in
# ----------------------------------------------------------------------------


def test_answer_capture_contradiction_persists_challenge_and_tension_ref(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    note_id = _capture_note(db_path, "doubling down on MELI here")
    verdict = TriageVerdict(
        route="contradiction",
        conflict_token="T12",
        why="sizing discipline",
        conflict_kind="tenet",
        conflict_id=12,
        conflict_body="I cap any single name at 15% of the book.",
        conflict_as_of="2026-03-14",
    )
    monkeypatch.setattr(respond_mod, "classify_capture_triage", lambda *_a, **_k: verdict)

    result = respond_mod.answer_capture(note_id, repo_root=tmp_path, db_path=db_path)

    assert result is not None
    assert "cap any single name at 15% of the book" in result
    assert "sizing discipline" in result
    note = get_note(note_id, db_path=db_path)
    assert note is not None
    ctx = note.context or {}
    assert ctx.get("ledger_answer", {}).get("status") == "ok"
    assert ctx.get("ledger_answer", {}).get("kind") == "contradiction"
    assert ctx.get("ledger_answer", {}).get("text") == result
    assert ctx.get("tension_ref") == {"kind": "tenet", "id": 12, "why": "sizing discipline"}
    assert ctx.get("ledger_answer_pending") is False


def test_answer_capture_contradiction_decision_kind_label(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    note_id = _capture_note(db_path, "adding more BN right here")
    verdict = TriageVerdict(
        route="contradiction",
        conflict_token="D3",
        why="",
        conflict_kind="decision",
        conflict_id=3,
        conflict_body="BN trim decision — falsifier: fee growth reaccelerates",
        conflict_as_of="",
        conflict_ticker="BN",
    )
    monkeypatch.setattr(respond_mod, "classify_capture_triage", lambda *_a, **_k: verdict)
    result = respond_mod.answer_capture(note_id, repo_root=tmp_path, db_path=db_path)
    assert result is not None
    assert "open BN decision" in result


def test_answer_capture_plain_clears_pending_no_answer(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    note_id = _capture_note(db_path, "MELI looks cheap here")
    from user_state.notes import patch_note_context

    patch_note_context(note_id, {"ledger_answer_pending": True}, db_path=db_path)
    monkeypatch.setattr(
        respond_mod, "classify_capture_triage", lambda *_a, **_k: TriageVerdict(route="plain")
    )

    result = respond_mod.answer_capture(note_id, repo_root=tmp_path, db_path=db_path)

    assert result is None
    note = get_note(note_id, db_path=db_path)
    assert note is not None
    ctx = note.context or {}
    assert "ledger_answer" not in ctx
    assert ctx.get("ledger_answer_pending") is False


def test_answer_capture_failure_breadcrumb(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An engine failure must leave a diagnosable trace on the note — the
    2026-07 zero-fire incident's root cause (silent clear, no breadcrumb)."""
    note_id = _capture_note(db_path, "What's my cost basis on MELI?")
    monkeypatch.setattr(
        respond_mod,
        "classify_capture_triage",
        lambda *_a, **_k: TriageVerdict(route="answer_now"),
    )

    def boom(*_a: object, **_k: object) -> object:
        raise RuntimeError("engine exploded")

    monkeypatch.setattr(respond_mod, "build_portfolio_pack", boom)

    result = respond_mod.answer_capture(note_id, repo_root=tmp_path, db_path=db_path)

    assert result is None
    note = get_note(note_id, db_path=db_path)
    assert note is not None
    ctx = note.context or {}
    assert ctx.get("ledger_answer") == {"status": "failed", "error": "RuntimeError"}
    assert ctx.get("ledger_answer_pending") is False


def test_will_answer_no_longer_requires_question_shaped_text(db_path: Path) -> None:
    plain_id = _capture_note(db_path, "MELI looks cheap here")
    assert respond_mod.will_answer(plain_id, db_path=db_path) is True

    observation_id = _capture_note(db_path, "flat statement", kind="observation")
    assert respond_mod.will_answer(observation_id, db_path=db_path) is True

    decision_id = _capture_note(db_path, "a decision note", kind="decision")
    assert respond_mod.will_answer(decision_id, db_path=db_path) is False  # not an answerable kind


def test_will_answer_respects_needs_ticker_and_enabled_gate(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from user_state.notes import patch_note_context

    note_id = _capture_note(db_path, "add to which one?")
    patch_note_context(note_id, {"needs_ticker": True}, db_path=db_path)
    assert respond_mod.will_answer(note_id, db_path=db_path) is False

    other_id = _capture_note(db_path, "plain thought")
    monkeypatch.setenv("LEDGER_ANSWER", "0")
    assert respond_mod.will_answer(other_id, db_path=db_path) is False


# ----------------------------------------------------------------------------
# golden set + eval-harness graders
# ----------------------------------------------------------------------------


def test_golden_json_loads() -> None:
    cases = load_capture_triage_golden(GOLDEN_PATH)
    assert len(cases) >= 13
    routes = {c.expected["route"] for c in cases}  # type: ignore[index]
    assert routes == {"answer_now", "contradiction", "plain"}
    assert any(c.case_id == "ct-001-cost-basis-verbatim" for c in cases)
    assert any(c.case_id == "ct-002-why-cant-you-tell-me-verbatim" for c in cases)


def test_grade_capture_triage_case_partial_credit_on_wrong_token() -> None:
    case = ClassifierCase(
        "ct-x",
        {"case": {"text": "doubling MELI", "context": {}}},
        {"route": "contradiction", "conflicts_with": "T12"},
    )
    exact = grade_capture_triage_case(
        case, fn=lambda _c: {"route": "contradiction", "conflicts_with": "T12"}
    )
    assert exact.passed and exact.score == 1.0

    wrong_token = grade_capture_triage_case(
        case, fn=lambda _c: {"route": "contradiction", "conflicts_with": "T99"}
    )
    assert not wrong_token.passed and wrong_token.score == 0.5

    wrong_route = grade_capture_triage_case(case, fn=lambda _c: {"route": "plain"})
    assert not wrong_route.passed and wrong_route.score == 0.0
