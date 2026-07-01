"""Phase-1 apply dispatch: the SEPARATE explicit write step per artifact kind.

``proposals.act_on_proposal`` is write-free by construction -- a one-tap approve
only flips status. This module is the deliberate later write both surfaces (the
web ``/api/research/proposal/<id>/act`` route and the Telegram callback) invoke
AFTER an approve, dispatching by proposal kind:

  - view  -> the real saved view (NON-mutating in the higher-bar sense)
  - memo  -> nothing (a memo IS the artifact; approve just files it)
  - dcf / thesis / code -> MUTATING: the Wave-2 higher-bar gate (evidence +
    adversarial-survived + oracle, or an explicit steer) must clear BEFORE the
    kind-specific applier is allowed to write. Waves 3/4 register their gated
    writer via ``register_mutating_applier``; until then a cleared gate yields
    "apply not yet wired", and an uncleared gate writes NOTHING.

It performs NO web fetch, so approve-then-apply never forms a lethal trifecta.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import cast

from research.higher_bar import MUTATING_KINDS, evaluate_higher_bar
from research.proposals import get_proposal

# Kind -> the GATED writer, registered by Wave 3/4 (dcf, thesis, code). A mutating
# kind absent here is "not yet wired" even once its gate clears.
ApplierFn = Callable[..., str]
_MUTATING_APPLIERS: dict[str, ApplierFn] = {}


def register_mutating_applier(kind: str, applier: ApplierFn) -> None:
    """Wave 3/4 register their gated writer here (dcf/thesis/code)."""
    _MUTATING_APPLIERS[kind] = applier


def _json_field(raw: object, key: str) -> object:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        obj: object = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if isinstance(obj, dict):
        return cast("dict[str, object]", obj).get(key)
    return None


def _refuted_of(prop: object) -> bool | None:
    """The stored adversarial verdict's ``refuted`` (None = unassessed -> fails closed)."""
    val = _json_field(getattr(prop, "adversarial_verdict", None), "refuted")
    return bool(val) if isinstance(val, bool) else None


def _oracle_ok_of(prop: object) -> bool | None:
    """The type-specific oracle result the draft stored in ``artifact_json.oracle_ok``.

    Wave 3 sets it after its deterministic check (DCF Python recompute succeeded /
    thesis append-only / code golden). Absent -> None -> the gate fails closed."""
    val = _json_field(getattr(prop, "artifact_json", None), "oracle_ok")
    return bool(val) if isinstance(val, bool) else None


def apply_approved_proposal(
    proposal_id: int, *, db_path: Path | str | None = None, steer_authorized: bool = False
) -> str:
    """Perform the kind-specific live write for a just-approved proposal.

    Non-mutating kinds (view/memo) write immediately. MUTATING kinds
    (dcf/thesis/code) must clear the higher-bar gate first -- a blocked gate
    returns a note and writes NOTHING. Returns a short human note to echo.
    """
    prop = get_proposal(proposal_id, db_path=db_path)
    if prop is None:
        return ""
    kind = getattr(prop, "kind", "")
    if kind == "view":
        from research.view_artifact import apply_view_proposal

        row = apply_view_proposal(proposal_id, db_path=db_path)
        return f"saved view: {getattr(row, 'name', None) or getattr(prop, 'title', 'view')}"
    if kind == "memo":
        return ""
    if kind in MUTATING_KINDS:
        gate = evaluate_higher_bar(
            kind=kind,
            evidence_json=str(getattr(prop, "evidence_json", None) or "[]"),
            refuted=_refuted_of(prop),
            oracle_ok=_oracle_ok_of(prop),
            steer_authorized=steer_authorized,
        )
        if not gate.clears:
            return "blocked (higher bar): " + "; ".join(gate.reasons)
        applier = _MUTATING_APPLIERS.get(kind)
        if applier is None:
            return f"{kind}: apply not yet wired"
        return applier(proposal_id, db_path=db_path)
    return f"{kind}: apply not yet wired"
