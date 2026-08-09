# pyright: reportPrivateUsage=false
#
# These tests drive the classifier + module-private routers directly to pin
# the closed-under-no-fit contract in isolation; the module-level directive
# silences the cross-module private-usage rule, matching the repo convention
# (see test_comment_processor_hardstop.py's header).
"""S5 — the comment classifier is closed under no-fit.

The classifier used to force every unmappable comment into ask_question (the
inert hard fallback). Now it has an explicit ``needs_triage`` terminal: the
LLM may pick it, a garbage / out-of-vocabulary answer falls back to it, its
router parks the comment in the data_fixes backlog for human disposition, and
the notes mirror records it as an open *question* (not an inert observation).
``curate_peers`` is the new peer-curation route.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import process_report_comments as prc  # noqa: E402

import comments  # noqa: E402
from user_state import notes  # noqa: E402

_DATE = date(2026, 5, 18)


def _comment(text: str, *, anchor_type: str = "free_text", key: str = "x") -> comments.Comment:
    return comments.Comment(
        id="c1",
        anchor=comments.Anchor(type=anchor_type, key=key),  # pyright: ignore[reportArgumentType]
        comment=text,
    )


def _canned(answer: str):
    def _f(*_a: object, **_k: object) -> object:
        from llm.structured import StructuredParseError

        try:
            return prc._IntentClassification.model_validate({"intent": answer})
        except ValueError as exc:
            raise StructuredParseError("invalid intent") from exc

    return _f


# ---------------------------------------------------------------------------
# classifier: closed under no-fit
# ---------------------------------------------------------------------------


def test_garbage_answer_falls_back_to_needs_triage(monkeypatch: pytest.MonkeyPatch) -> None:
    # The old fallback was ask_question — an out-of-vocabulary answer now parks
    # the comment for triage instead of mis-routing it.
    monkeypatch.setattr(prc, "call_llm_structured", _canned("¯\\_(ツ)_/¯ not sure"))
    assert prc._classify_intent(_comment("remove this section unless better")) == "needs_triage"


def test_model_may_explicitly_pick_needs_triage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prc, "call_llm_structured", _canned("needs_triage"))
    assert prc._classify_intent(_comment("this is conditional and ambiguous")) == "needs_triage"


def test_peer_comment_classifies_as_curate_peers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prc, "call_llm_structured", _canned("curate_peers"))
    c = _comment("these are shit peers", anchor_type="peer_comp", key="peer_comp")
    assert prc._classify_intent(c) == "curate_peers"


def test_known_intent_still_routes_normally(monkeypatch: pytest.MonkeyPatch) -> None:
    # The new terminal doesn't shadow a clean classification.
    monkeypatch.setattr(prc, "call_llm_structured", _canned("ask_question"))
    assert prc._classify_intent(_comment("what's the NPL trend?")) == "ask_question"


# ---------------------------------------------------------------------------
# needs_triage router → data_fixes backlog
# ---------------------------------------------------------------------------


def test_needs_triage_dry_run_writes_nothing(tmp_path: Path) -> None:
    c = _comment("remove this section unless you select better peers")
    res = prc._route_needs_triage(tmp_path, "NU", c, apply=False)
    assert res["dry_run"] is True
    assert not (tmp_path / "directives" / "data_fixes.md").exists()


def test_needs_triage_apply_parks_in_data_fixes(tmp_path: Path) -> None:
    c = _comment("remove this section unless better", anchor_type="peer_comp", key="peer_comp")
    res = prc._route_needs_triage(tmp_path, "NU", c, apply=True)
    body = (tmp_path / "directives" / "data_fixes.md").read_text(encoding="utf-8")
    assert "TRIAGE" in body
    assert "needs disposition" in body
    assert "NU" in body
    assert res["dry_run"] is False
    assert "data_fixes.md" in str(res["summary"])


# ---------------------------------------------------------------------------
# notes mirror: needs_triage does NOT collapse to observation
# ---------------------------------------------------------------------------


def test_needs_triage_mirrors_as_open_question_not_observation() -> None:
    # The bug this fixes: an unmappable directive collapsed to an inert
    # observation. It is now an open question — an actionable, reconcilable loop.
    assert notes._kind_for_comment("needs_triage", "remove unless better") == "question"
    assert notes._kind_for_comment("needs_triage", "anything at all") != "observation"


def test_curate_peers_mirrors_as_decision() -> None:
    assert notes._kind_for_comment("curate_peers", "pin HOOD and SOFI as peers") == "decision"
