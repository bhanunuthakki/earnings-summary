"""Phase-1 Wave-2 tests: the higher-bar gate for mutating research artifacts."""

from __future__ import annotations

import json

from research.higher_bar import (
    MUTATING_KINDS,
    HigherBarResult,
    evaluate_higher_bar,
)

_URL_EVIDENCE = json.dumps(
    [{"point": "sub-ARR +32%", "source_url": "https://ir.rubrik.com/x", "date": "2026-06-05"}]
)
_PROSE_EVIDENCE = json.dumps([{"point": "it feels strong", "source_url": "", "date": ""}])


def test_non_mutating_kinds_bypass_the_gate() -> None:
    for kind in ("memo", "view"):
        r = evaluate_higher_bar(kind=kind, evidence_json=_PROSE_EVIDENCE, refuted=True)
        assert r.required is False
        assert r.clears is True  # non-mutating -> one-click regardless


def test_mutating_all_three_clear() -> None:
    r = evaluate_higher_bar(kind="dcf", evidence_json=_URL_EVIDENCE, refuted=False, oracle_ok=True)
    assert r.required is True
    assert (r.evidence_ok, r.adversarial_ok, r.oracle_ok) == (True, True, True)
    assert r.clears is True


def test_mutating_missing_evidence_doorway_blocks() -> None:
    r = evaluate_higher_bar(
        kind="thesis", evidence_json=_PROSE_EVIDENCE, refuted=False, oracle_ok=True
    )
    assert r.evidence_ok is False
    assert r.clears is False
    assert any("doorway" in reason for reason in r.reasons)


def test_mutating_refuted_blocks() -> None:
    r = evaluate_higher_bar(kind="dcf", evidence_json=_URL_EVIDENCE, refuted=True, oracle_ok=True)
    assert r.adversarial_ok is False
    assert r.clears is False


def test_mutating_oracle_fail_blocks() -> None:
    r = evaluate_higher_bar(kind="dcf", evidence_json=_URL_EVIDENCE, refuted=False, oracle_ok=False)
    assert r.oracle_ok is False
    assert r.clears is False


def test_unassessed_adversarial_fails_closed() -> None:
    # refuted=None (never assessed) must NOT clear.
    r = evaluate_higher_bar(kind="dcf", evidence_json=_URL_EVIDENCE, refuted=None, oracle_ok=True)
    assert r.adversarial_ok is False
    assert r.clears is False


def test_steer_overrides_an_unmet_gate() -> None:
    r = evaluate_higher_bar(
        kind="code",
        evidence_json=_PROSE_EVIDENCE,
        refuted=True,
        oracle_ok=False,
        steer_authorized=True,
    )
    assert r.clears is True
    assert any("STEER override" in reason for reason in r.reasons)


def test_note_id_counts_as_a_doorway() -> None:
    ev = json.dumps([{"point": "prior musing", "note_id": 4210}])
    assert evaluate_higher_bar(
        kind="dcf", evidence_json=ev, refuted=False, oracle_ok=True
    ).evidence_ok


def test_non_url_string_in_url_field_is_not_a_doorway() -> None:
    ev = json.dumps([{"point": "x", "source_url": "trust me"}])
    assert (
        evaluate_higher_bar(kind="dcf", evidence_json=ev, refuted=False, oracle_ok=True).evidence_ok
        is False
    )


def test_mutating_kinds_membership() -> None:
    assert {"dcf", "thesis", "code", "ask_thesis_edit", "ask_kpi_edit"} == MUTATING_KINDS
    assert "memo" not in MUTATING_KINDS and "view" not in MUTATING_KINDS


def test_result_is_frozen_dataclass() -> None:
    r = evaluate_higher_bar(kind="memo")
    assert isinstance(r, HigherBarResult)
