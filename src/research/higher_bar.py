"""The higher-bar gate for mutating research artifacts (Phase-1 Wave 2).

A mutating artifact's *apply* affordance renders ONLY when ALL THREE clear, OR an
explicit steer authorizes it (Phase-1 plan §3 Wave 2, §4.4):

  - **evidence-gated** — >=1 concrete ``source_url`` / ``fact_ref`` / ``news_id`` /
    ``note_id`` / URL doorway in ``evidence_json`` (Law 2: a doorway, not prose).
  - **adversarial-survived** — the ``research_adversarial_assess`` verdict did NOT
    refute the claim (``refuted is False``). Unassessed (``None``) fails closed.
  - **deterministic numeric/oracle** — type-specific, computed by the caller (DCF
    Python recompute; view ``from_dict`` validation; code golden + CI) and passed
    in as a bool.

Non-mutating artifacts (``memo``, ``view`` preview) never require it — approve is
one-click. This module is the pure-logic substrate; Wave 3 wires it into the
DCF/thesis apply path, Wave 4 into the code leg. It holds no DB/LLM/web handle by
design, so it can never itself be part of a lethal-trifecta write.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import cast

# Only these kinds mutate live state on approve -> must clear the higher bar.
MUTATING_KINDS: frozenset[str] = frozenset({"dcf", "thesis", "code"})

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
