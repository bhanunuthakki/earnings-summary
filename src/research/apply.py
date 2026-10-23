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
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from research.proposals import get_proposal

if TYPE_CHECKING:
    from research.proposals import ResearchProposal


# ---------------------------------------------------------------------------
# The higher-bar gate for mutating research artifacts (Phase-1 Wave 2) — folded
# in from the former ``research.higher_bar``.
#
# A mutating artifact's *apply* affordance renders ONLY when ALL THREE clear, OR
# an explicit steer authorizes it (Phase-1 plan §3 Wave 2, §4.4):
#
#   - **evidence-gated** — >=1 concrete ``source_url`` / ``fact_ref`` /
#     ``news_id`` / ``note_id`` / URL doorway in ``evidence_json`` (Law 2: a
#     doorway, not prose).
#   - **adversarial-survived** — the ``research_adversarial_assess`` verdict
#     did NOT refute the claim (``refuted is False``). Unassessed (``None``)
#     fails closed.
#   - **deterministic numeric/oracle** — type-specific, computed by the caller
#     (DCF Python recompute; view ``from_dict`` validation; code golden + CI)
#     and passed in as a bool.
#
# Non-mutating artifacts (``memo``, ``view`` preview) never require it —
# approve is one-click. Holds no DB/LLM/web handle by design, so it can never
# itself be part of a lethal-trifecta write.
# ---------------------------------------------------------------------------

# Only these kinds mutate live state on approve -> must clear the higher bar.
MUTATING_KINDS: frozenset[str] = frozenset(
    {
        "dcf",
        "thesis",
        "code",
        "ask_thesis_edit",
        "ask_kpi_edit",
        "owner_thesis_edit",
        "owner_kpi_edit",
    }
)

# Keys in an evidence item that count as a concrete doorway (Law 2).
_DOORWAY_KEYS: tuple[str, ...] = ("source_url", "url", "fact_ref", "news_id", "note_id")
_URL_RE = re.compile(r"https?://", re.IGNORECASE)


def _has_concrete_doorway(evidence_json: str) -> bool:
    """True iff ``evidence_json`` carries >=1 concrete doorway (not prose).

    Handles both the Wave-1 shape (a JSON list of ``{point, source_url, date}``
    findings) and a ``{"findings": [...]}`` wrapper, and is forward-compatible
    with fact_ref/news_id/note_id doorways later artifacts attach.
    """
    try:
        doc: object = json.loads(evidence_json or "[]")
    except (ValueError, TypeError):
        return False
    if isinstance(doc, list):
        items: list[object] = cast("list[object]", doc)
    elif isinstance(doc, dict):
        inner = cast("dict[str, object]", doc).get("findings")
        items = cast("list[object]", inner) if isinstance(inner, list) else [doc]
    else:
        return False
    for item in items:
        if not isinstance(item, dict):
            continue
        d = cast("dict[str, object]", item)
        for key in _DOORWAY_KEYS:
            val = d.get(key)
            if isinstance(val, bool):
                continue
            if isinstance(val, int) and key.endswith("_id") and val:
                return True
            if isinstance(val, str) and val.strip():
                # a non-URL string in a url field is prose, not a doorway
                if key in ("source_url", "url") and not _URL_RE.search(val):
                    continue
                return True
    return False


@dataclass(frozen=True)
class HigherBarResult:
    kind: str
    required: bool  # whether this kind needs the gate at all
    clears: bool  # may the apply affordance render?
    evidence_ok: bool
    adversarial_ok: bool
    oracle_ok: bool
    steer_authorized: bool
    reasons: tuple[str, ...] = ()


def evaluate_higher_bar(
    *,
    kind: str,
    evidence_json: str = "[]",
    refuted: bool | None = None,
    oracle_ok: bool | None = None,
    steer_authorized: bool = False,
) -> HigherBarResult:
    """Decide whether a proposal's *apply* affordance may render.

    Fails closed: an unassessed adversarial verdict (``refuted is None``) or a
    missing oracle result (``oracle_ok`` falsy) does NOT clear the gate. Only an
    explicit ``steer_authorized`` can override an unmet gate (owner intent).
    """
    normalized_kind = (kind or "").strip().lower()
    required = normalized_kind in MUTATING_KINDS

    if not required:
        # memo / view — non-mutating; approve is always one-click.
        return HigherBarResult(
            kind=normalized_kind,
            required=False,
            clears=True,
            evidence_ok=True,
            adversarial_ok=True,
            oracle_ok=True,
            steer_authorized=steer_authorized,
            reasons=("non-mutating artifact — higher bar not required",),
        )

    evidence_ok = _has_concrete_doorway(evidence_json)
    adversarial_ok = refuted is False  # survives == NOT refuted; None fails closed
    oracle_ok_bool = bool(oracle_ok)
    all_clear = evidence_ok and adversarial_ok and oracle_ok_bool
    clears = all_clear or steer_authorized

    reasons: list[str] = []
    if not evidence_ok:
        reasons.append(
            "no concrete evidence doorway (Law 2): needs a source_url/fact_ref/"
            "news_id/note_id, not prose"
        )
    if not adversarial_ok:
        reasons.append("adversarial assessment refuted the claim or was not run (fails closed)")
    if not oracle_ok_bool:
        reasons.append("deterministic numeric/oracle check did not pass")
    if steer_authorized and not all_clear:
        reasons.append("STEER override: owner explicitly authorized despite an unmet gate")
    if all_clear:
        reasons.append("all three gates cleared")

    return HigherBarResult(
        kind=normalized_kind,
        required=True,
        clears=clears,
        evidence_ok=evidence_ok,
        adversarial_ok=adversarial_ok,
        oracle_ok=oracle_ok_bool,
        steer_authorized=steer_authorized,
        reasons=tuple(reasons),
    )


# Kind -> the GATED writer, registered by Wave 3/4 (dcf, thesis, code). A mutating
# kind absent here is "not yet wired" even once its gate clears.
@dataclass(frozen=True, slots=True)
class MutationApplyResult:
    message: str
    applied: bool
    target_postcondition_sha256: str | None = None


ApplierFn = Callable[..., str | MutationApplyResult]
_MUTATING_APPLIERS: dict[str, ApplierFn] = {}


def register_mutating_applier(kind: str, applier: ApplierFn) -> None:
    """Wave 3/4 register their gated writer here (dcf/thesis/code)."""
    _MUTATING_APPLIERS[kind] = applier


def _load_applier(kind: str) -> ApplierFn | None:
    """Lazy-import ``research.<kind>_artifact`` (it self-registers) then look up.

    Keeps apply.py decoupled from the concrete artifact modules -- a mutating
    kind's writer is discovered by convention only when first needed.
    """
    import importlib

    try:
        module = (
            "research.ask_proposal_artifact"
            if kind in {"ask_thesis_edit", "ask_kpi_edit", "owner_thesis_edit", "owner_kpi_edit"}
            else f"research.{kind}_artifact"
        )
        importlib.import_module(module)
    except Exception:
        return None
    return _MUTATING_APPLIERS.get(kind)


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
    result = apply_governed_proposal(
        prop,
        proposal_id=proposal_id,
        db_path=db_path,
        steer_authorized=steer_authorized,
    )
    return result.message if isinstance(result, MutationApplyResult) else result


def apply_governed_proposal(
    prop: ResearchProposal | None,
    *,
    proposal_id: int,
    db_path: Path | str | None = None,
    steer_authorized: bool = False,
    **applier_context: object,
) -> str | MutationApplyResult:
    """Gate and dispatch a proposal, optionally within its caller-owned transaction."""

    if prop is None:
        return ""
    kind = getattr(prop, "kind", "")
    if kind == "view":
        from research.view_artifact import apply_view_proposal

        row = apply_view_proposal(proposal_id, db_path=db_path)
        return f"saved view: {getattr(row, 'name', None) or getattr(prop, 'title', 'view')}"
    if kind == "memo":
        return ""
    if kind == "question":
        from research.question_artifact import apply_question_proposal

        note = apply_question_proposal(proposal_id, db_path=db_path)
        return f"open question #{note.id} persisted for {note.ticker or 'portfolio'}"
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
        applier = _MUTATING_APPLIERS.get(kind) or _load_applier(kind)
        if applier is None:
            return f"{kind}: apply not yet wired"
        if kind in {"ask_thesis_edit", "ask_kpi_edit", "owner_thesis_edit", "owner_kpi_edit"}:
            return applier(proposal_id, db_path=db_path, proposal=prop, **applier_context)
        return applier(proposal_id, db_path=db_path)
    return f"{kind}: apply not yet wired"
