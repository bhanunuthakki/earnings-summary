"""Phase-1 Wave-3 thesis-edit artifact (MUTATING, behind the higher-bar gate).

``user_state.ledger.append_entry`` is APPEND-ONLY by construction (no update or
delete path), so a drafted thesis edit is held as an inert ``kind='thesis'``
``research_proposal`` and only appended on approve -- history can never be
clobbered. The apply registers behind the W3-1 gate (evidence + adversarial-
survived + oracle). The oracle for an append-only edit is trivially satisfied
(nothing numeric to validate), so the draft sets ``oracle_ok=True``; the real
guard is the evidence doorway + the adversarial assessment.

Dependency-injected (``create_fn`` / ``get_fn`` / ``append_fn``) so the primitives
are unit-testable without a DB.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from research.apply import register_mutating_applier
from research.proposals import create_proposal, get_proposal
from user_state.ledger import append_entry


def draft_thesis_proposal(
    *,
    ticker: str,
    body: str,
    entry_kind: str = "revision",
    title: str | None = None,
    evidence_json: str = "[]",
    adversarial_verdict: str | None = None,
    note_id: int | None = None,
    task_id: int | None = None,
    db_path: Path | str | None = None,
    create_fn: Callable[..., int] = create_proposal,
) -> int | None:
    """Persist an inert ``kind='thesis'`` proposal carrying the drafted ledger entry.

    Returns the proposal id, or None if there is no ticker/body to record.
    """
    body = (body or "").strip()
    ticker = (ticker or "").strip().upper()
    if not body or not ticker:
        return None
    artifact = {"entry_kind": entry_kind, "body": body, "oracle_ok": True}
    return int(
        create_fn(
            task_id=task_id,
            kind="thesis",
            ticker=ticker,
            title=(title or f"Thesis {entry_kind}: {ticker}")[:200],
            body_md=body,
            evidence_json=evidence_json,
            source_note_ids=json.dumps([note_id] if note_id is not None else []),
            artifact_json=json.dumps(artifact),
            adversarial_verdict=adversarial_verdict,
            provenance="derived",
            db_path=db_path,
        )
    )


def apply_thesis_proposal(
    proposal_id: int,
    *,
    db_path: Path | str | None = None,
    get_fn: Callable[..., Any] = get_proposal,
    append_fn: Callable[..., Any] | None = None,
) -> str:
    """The GATED write: append the drafted entry via the append-only ledger.

    Reached only after the higher-bar gate clears (enforced in
    ``apply.apply_approved_proposal``). Raises ``ValueError`` for a non-thesis /
    ticker-less / entry-less proposal.
    """
    prop = get_fn(proposal_id, db_path=db_path)
    if prop is None or getattr(prop, "kind", None) != "thesis":
        raise ValueError(f"proposal {proposal_id} is not a thesis proposal")
    artifact = getattr(prop, "artifact_json", None)
    if not artifact:
        raise ValueError(f"thesis proposal {proposal_id} carries no entry (artifact_json empty)")
    ticker = getattr(prop, "ticker", None)
    if not ticker:
        raise ValueError(f"thesis proposal {proposal_id} has no ticker")
    data = json.loads(artifact)
    appender = append_fn or append_entry
    row = appender(
        ticker=ticker,
        entry_kind=str(data.get("entry_kind") or "revision"),
        body=str(data.get("body") or getattr(prop, "body_md", "")),
        db_path=db_path,
    )
    return f"thesis ledger entry #{getattr(row, 'id', '?')} appended for {ticker}"


register_mutating_applier("thesis", apply_thesis_proposal)
