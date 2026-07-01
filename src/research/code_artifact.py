"""Phase-1 Wave-4 code-change artifact -- the transitive-taint injection firebreak.

The code leg is the highest-stakes artifact: an approved code change could mutate
the repo. Two non-negotiable safety properties live here (Phase-1 plan §4.3/§4.5):

  1. TRANSITIVE-TAINT HARD-REFUSE (S4). A code request is hard-refused if its
     proposal -- or ANY proposal up its ``tainted_by_proposal_id`` derivation
     chain -- carries fetched (``provenance='contains_fetched'``) content. This
     severs the laundered path *fetched-deck -> memo -> re-utterance("add this
     feature") -> code*. The refuse is NOT overridable by a steer (unlike the
     higher-bar gate), and is checked at BOTH draft and apply time.

  2. NO AUTONOMOUS LIVE MUTATION. Even a cleared gate never auto-spawns+merges a
     coder here -- ``oracle_ok`` is False at draft (the code oracle is golden+CI,
     verified out-of-band), so the higher-bar gate blocks auto-apply, and the
     apply itself only ever reports that a human-reviewed spawn is required. The
     real spawned coder (scrubbed-fixture bind, network-less, worktree diff) is a
     reviewed, out-of-band step -- never a one-tap live write.

So a code proposal is inert; approve reports "review required" or "REFUSED",
never a repo mutation.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from research.apply import register_mutating_applier
from research.proposals import create_proposal, get_proposal


def transitive_taint(
    proposal_id: int,
    *,
    get_fn: Callable[..., Any] = get_proposal,
    db_path: Path | str | None = None,
) -> int | None:
    """The id of the first fetched-tainted proposal up the derivation chain
    (including this one), or None. Cycle-safe via a visited set."""
    seen: set[int] = set()
    current: int | None = proposal_id
    while current is not None and current not in seen:
        seen.add(current)
        prop = get_fn(current, db_path=db_path)
        if prop is None:
            return None
        if getattr(prop, "provenance", None) == "contains_fetched":
            return current
        nxt = getattr(prop, "tainted_by_proposal_id", None)
        current = int(nxt) if isinstance(nxt, int) else None
    return None


def draft_code_proposal(
    *,
    spawn_prompt: str,
    ticker: str | None = None,
    files_touched: Iterable[str] | None = None,
    title: str | None = None,
    evidence_json: str = "[]",
    adversarial_verdict: str | None = None,
    note_id: int | None = None,
    task_id: int | None = None,
    tainted_by_proposal_id: int | None = None,
    db_path: Path | str | None = None,
    create_fn: Callable[..., int] = create_proposal,
    get_fn: Callable[..., Any] = get_proposal,
) -> int | None:
    """Persist an inert ``kind='code'`` proposal.

    Refuses (returns None) if the SOURCE is transitively tainted -- a
    fetched-derived request never becomes a code chip in the first place.
    """
    spawn_prompt = (spawn_prompt or "").strip()
    if not spawn_prompt:
        return None
    if tainted_by_proposal_id is not None and (
        transitive_taint(tainted_by_proposal_id, get_fn=get_fn, db_path=db_path) is not None
    ):
        return None  # S4: fetched-tainted source -> never draft a code request
    artifact = {
        "spawn_prompt": spawn_prompt,
        "files_touched": list(files_touched or []),
        # code oracle = golden + diff-aware CI, verified OUT OF BAND -> the gate
        # blocks any auto-apply; a code change is never one-tap.
        "oracle_ok": False,
    }
    return int(
        create_fn(
            task_id=task_id,
            kind="code",
            ticker=ticker,
            title=(title or "Code change proposal")[:200],
            body_md=f"Proposed code change (REVIEW REQUIRED, never auto-applied):\n\n{spawn_prompt}",
            evidence_json=evidence_json,
            source_note_ids=json.dumps([note_id] if note_id is not None else []),
            artifact_json=json.dumps(artifact),
            adversarial_verdict=adversarial_verdict,
            provenance="derived",
            tainted_by_proposal_id=tainted_by_proposal_id,
            db_path=db_path,
        )
    )


def apply_code_proposal(
    proposal_id: int,
    *,
    db_path: Path | str | None = None,
    get_fn: Callable[..., Any] = get_proposal,
) -> str:
    """Reached only after the higher-bar gate clears (which, with oracle_ok=False,
    means only via an explicit steer). HARD-REFUSES a transitively tainted request
    (non-overridable), and NEVER auto-mutates -- it reports that a human-reviewed
    spawn is required."""
    prop = get_fn(proposal_id, db_path=db_path)
    if prop is None or getattr(prop, "kind", None) != "code":
        raise ValueError(f"proposal {proposal_id} is not a code proposal")
    tainted = transitive_taint(proposal_id, get_fn=get_fn, db_path=db_path)
    if tainted is not None:
        return (
            f"REFUSED: code request carries transitive taint (fetched proposal "
            f"#{tainted}) -- injection firebreak (non-overridable)"
        )
    return (
        "code change requires a human-reviewed spawn (never auto-applied): "
        "route via the spawn_task review path with a scrubbed-fixture, network-less coder"
    )


register_mutating_applier("code", apply_code_proposal)
