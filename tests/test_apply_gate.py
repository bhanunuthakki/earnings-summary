"""Phase-1 Wave-3 tests: the higher-bar gate is ENFORCED on every mutating apply.

A dcf/thesis/code proposal may write only when the gate clears (evidence +
adversarial-survived + oracle) or an explicit steer authorizes -- otherwise
``apply_approved_proposal`` blocks and writes nothing.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import research.apply as apply_mod
from research.apply import _MUTATING_APPLIERS, apply_approved_proposal

_URL_EVIDENCE = json.dumps([{"point": "x", "source_url": "https://sec.gov/x", "date": "2026-06"}])
_VERDICT_SURVIVES = json.dumps({"refuted": False, "confidence": "high"})
_ORACLE_OK = json.dumps({"oracle_ok": True})


def _dcf_prop(**over: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "kind": "dcf",
        "evidence_json": _URL_EVIDENCE,
        "adversarial_verdict": _VERDICT_SURVIVES,
        "artifact_json": _ORACLE_OK,
        "title": "NU DCF",
        "status": "approved",
    }
    base.update(over)
    return SimpleNamespace(**base)


def _patch(monkeypatch, prop: object) -> None:
    monkeypatch.setattr(apply_mod, "get_proposal", lambda _pid, **_k: prop)


def test_cleared_gate_but_no_applier_registered_yet(monkeypatch) -> None:
    # 'code' has no artifact module yet (Wave 4): a cleared gate is still SAFE --
    # it reports "not yet wired" rather than writing anything.
    _patch(monkeypatch, _dcf_prop(kind="code"))
    assert apply_approved_proposal(1) == "code: apply not yet wired"


def test_blocked_without_evidence_doorway(monkeypatch) -> None:
    _patch(monkeypatch, _dcf_prop(evidence_json="[]"))
    assert "blocked (higher bar)" in apply_approved_proposal(1)


def test_blocked_when_adversarially_refuted(monkeypatch) -> None:
    _patch(monkeypatch, _dcf_prop(adversarial_verdict=json.dumps({"refuted": True})))
    assert "blocked" in apply_approved_proposal(1)


def test_blocked_when_oracle_absent_fails_closed(monkeypatch) -> None:
    _patch(monkeypatch, _dcf_prop(artifact_json=json.dumps({})))
    assert "blocked" in apply_approved_proposal(1)


def test_cleared_gate_dispatches_to_registered_applier(monkeypatch) -> None:
    _patch(monkeypatch, _dcf_prop())
    calls: list[int] = []
    monkeypatch.setitem(
        _MUTATING_APPLIERS, "dcf", lambda pid, **_k: calls.append(pid) or "wrote dcf run"
    )
    assert apply_approved_proposal(7) == "wrote dcf run"
    assert calls == [7]


def test_steer_overrides_a_failing_gate(monkeypatch) -> None:
    _patch(
        monkeypatch,
        _dcf_prop(
            evidence_json="[]",
            adversarial_verdict=json.dumps({"refuted": True}),
            artifact_json="{}",
        ),
    )
    calls: list[int] = []
    monkeypatch.setitem(_MUTATING_APPLIERS, "dcf", lambda pid, **_k: calls.append(pid) or "wrote")
    assert apply_approved_proposal(1, steer_authorized=True) == "wrote"
    assert calls == [1]
