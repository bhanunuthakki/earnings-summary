"""Forced-response state machine for ``red_team_items`` (PR6 —
monthly_red_team.md Phase 2 "Forced response" bullet).

Every open item must be answered REFUTE / ACCEPT / DEFER before the month
closes. This module owns the ONE transition implementation plus the
artifact-creation side effects; both the Flask cockpit route
(``execution/comments_server.py``'s ``/api/red_team/<id>/respond``) and the
Telegram ``/redteam`` command (``redteam.telegram_cmd``) call :func:`respond`
— no parallel state machine, per the task's "shared service function, no
logic duplication" contract.

State machine
-------------
``open`` and ``deferred`` are the two *answerable* statuses; ``refuted`` /
``accepted`` / ``closed`` are terminal (already answered — a second response
raises :class:`AlreadyRespondedError`).

* REFUTE — requires non-empty ``response_md`` (the owner's reasoning).
  Persists ``status='refuted'`` + ``response_md`` + ``responded_at`` via
  ``redteam.store.set_response``. When the item carries a ticker, the
  reasoning is ALSO written as a ``thesis_ledger_entries`` row (the existing
  append-only ledger write path, ``user_state.ledger.append_entry`` —
  ``thesis_ledger_entries.ticker`` is NOT NULL, so a cross-book item with no
  ticker instead lands a portfolio-level ``analyst_notes`` row; see
  :func:`_refute`).
* ACCEPT — persists ``status='accepted'``. Auto-creates a concrete follow-up
  artifact: a per-name item whose ``proposed_change_md`` reads as a sizing
  change (:func:`_looks_sizing_related`) gets a DRAFT ``position_sizing_intent``
  row (``intent_kind='sizing_note'`` — the schema's own free-text kind, since
  a keyword match can't reliably parse a numeric target/max/stop); everything
  else gets an open ``analyst_notes`` row (surfaces via ``v_thesis_status``'s
  ``open_notes_count``) whose body carries an explicit "manual follow-up"
  marker — the conservative, typed fallback the directive asks for rather
  than guessing a sizing edit that isn't there.
* DEFER — first defer: ``defer_count`` 0->1, ``status='deferred'``
  (``redteam.store.increment_defer``, itself guarded to only fire from
  ``open``). A second defer attempt (item already ``deferred``) is REJECTED
  — :class:`SecondDeferRejectedError` — the item stays exactly as the first
  defer left it (``status='deferred'``, ``defer_count==1``), which is itself
  the persistent "escalated" signal ``redteam.gate.escalated_items`` reads
  for the Home-band banner. The item remains answerable (refute/accept still
  work on a deferred item — only a repeat DEFER is blocked).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from redteam import store
from redteam.models import RedTeamItemRow
from user_state import notes as notes_store
from user_state.ledger import append_entry
from user_state.sizing import append_intent

Action = Literal["refute", "accept", "defer"]
ArtifactKind = Literal["ledger_entry", "sizing_intent", "open_note", "none"]

_TERMINAL_STATUSES = frozenset({"refuted", "accepted", "closed"})

# Deliberately small and conservative (module docstring / directive: "keep
# the mapping conservative and typed ... rather than guessing"). A miss here
# just falls through to the open-note fallback, never a wrong artifact.
_SIZING_KEYWORDS: tuple[str, ...] = (
    "trim",
    "size up",
    "size down",
    "resize",
    "target pct",
    "target %",
    "target_pct",
    "max pct",
    "max %",
    "max_pct",
    "position size",
    "position-sizing",
    "reduce weight",
    "increase weight",
    "reduce position",
    "increase position",
    "cut position",
    "add to position",
    "stop at",
    "stop-loss",
    "stop_at_loss",
    "sizing",
)

_REFUTE_LEDGER_KIND = "red_team_refute"


class RedTeamResponseError(ValueError):
    """Base class for the response state machine's rejections."""


class ItemNotFoundError(LookupError):
    """No ``red_team_items`` row with the given id."""


class AlreadyRespondedError(RedTeamResponseError):
    """The item is already refuted/accepted/closed — one-shot per terminal
    action. A deferred item is NOT terminal (still answerable)."""


class ResponseRequiresTextError(RedTeamResponseError):
    """REFUTE requires non-empty ``response_md``."""


class SecondDeferRejectedError(RedTeamResponseError):
    """A second DEFER on the same item is rejected. Carries the item's
    current (unchanged) row so the caller can render it escalated without a
    second read."""

    def __init__(self, item: RedTeamItemRow) -> None:
        super().__init__(
            f"red_team_items id={item.id} was already deferred once — escalated, "
            "respond with refute or accept"
        )
        self.item = item


@dataclass(slots=True, frozen=True)
class RespondResult:
    """What one :func:`respond` call did — the row after the transition plus
    whatever follow-up artifact was created."""

    item: RedTeamItemRow
    artifact_kind: ArtifactKind
    artifact_id: int | None


def _looks_sizing_related(text: str) -> bool:
    low = text.lower()
    return any(kw in low for kw in _SIZING_KEYWORDS)


def respond(
    *,
    db_path: Path | str | None,
    item_id: int,
    action: Action,
    response_md: str | None = None,
) -> RespondResult:
    """Answer one red-team item. Raises :class:`ItemNotFoundError`,
    :class:`AlreadyRespondedError`, :class:`ResponseRequiresTextError`, or
    :class:`SecondDeferRejectedError` on a rejected transition — callers
    (the Flask route, the Telegram command) translate each into their own
    surface's error shape."""
    item = store.get_item(db_path=db_path, item_id=item_id)
    if item is None:
        raise ItemNotFoundError(f"red_team_items id={item_id} not found")
    if item.status in _TERMINAL_STATUSES:
        raise AlreadyRespondedError(
            f"red_team_items id={item_id} already {item.status} — no further response accepted"
        )
    if action == "refute":
        if response_md is None or not response_md.strip():
            raise ResponseRequiresTextError("REFUTE requires non-empty response_md")
        return _refute(db_path=db_path, item=item, response_md=response_md.strip())
    if action == "accept":
        return _accept(db_path=db_path, item=item)
    if action == "defer":
        if item.status == "deferred":
            raise SecondDeferRejectedError(item)
        return _defer(db_path=db_path, item=item)
    raise ValueError(f"action must be one of refute | accept | defer, got {action!r}")


def _refute(*, db_path: Path | str | None, item: RedTeamItemRow, response_md: str) -> RespondResult:
    updated = store.set_response(
        db_path=db_path, item_id=item.id, status="refuted", response_md=response_md
    )
    if updated is None:  # pragma: no cover - raced away between get_item and set_response
        raise ItemNotFoundError(f"red_team_items id={item.id} vanished mid-response")
    if item.ticker:
        entry = append_entry(
            ticker=item.ticker,
            entry_kind=_REFUTE_LEDGER_KIND,
            body=f"Red Team refute ({item.lens}, {item.run_key}): {response_md}",
            db_path=db_path,
        )
        return RespondResult(item=updated, artifact_kind="ledger_entry", artifact_id=entry.id)
    # Cross-book items carry no ticker; thesis_ledger_entries.ticker is NOT
    # NULL, so there is no row to attach the reasoning to there. A
    # portfolio-level open note is the closest existing durable memory home.
    note = notes_store.create_note(
        ticker=None,
        kind="decision",
        body=f"[red-team refute — cross-book, {item.lens}] {response_md}",
        source="manual",
        context={"red_team_item_id": item.id, "run_key": item.run_key, "action": "refute"},
        db_path=db_path,
    )
    return RespondResult(item=updated, artifact_kind="open_note", artifact_id=note.id)


def _accept(*, db_path: Path | str | None, item: RedTeamItemRow) -> RespondResult:
    updated = store.set_response(
        db_path=db_path, item_id=item.id, status="accepted", response_md=None
    )
    if updated is None:  # pragma: no cover - raced away between get_item and set_response
        raise ItemNotFoundError(f"red_team_items id={item.id} vanished mid-response")
    if item.kind == "per_name" and item.ticker and _looks_sizing_related(item.proposed_change_md):
        row = append_intent(
            ticker=item.ticker,
            intent_kind="sizing_note",
            narrative=f"[red-team accepted, pending owner edit] {item.proposed_change_md}",
            db_path=db_path,
        )
        return RespondResult(item=updated, artifact_kind="sizing_intent", artifact_id=row.id)
    note = notes_store.create_note(
        ticker=item.ticker,
        kind="decision",
        body=f"[red-team accepted — manual follow-up] {item.proposed_change_md}",
        source="manual",
        context={"red_team_item_id": item.id, "run_key": item.run_key, "action": "accept"},
        db_path=db_path,
    )
    return RespondResult(item=updated, artifact_kind="open_note", artifact_id=note.id)


def _defer(*, db_path: Path | str | None, item: RedTeamItemRow) -> RespondResult:
    updated = store.increment_defer(db_path=db_path, item_id=item.id)
    if updated is None:
        # item.status wasn't 'open' by the time the guarded UPDATE ran (raced
        # with a concurrent responder) — re-read and report accurately.
        current = store.get_item(db_path=db_path, item_id=item.id) or item
        if current.status in _TERMINAL_STATUSES:
            raise AlreadyRespondedError(f"red_team_items id={item.id} already {current.status}")
        raise SecondDeferRejectedError(current)
    return RespondResult(item=updated, artifact_kind="none", artifact_id=None)


__all__ = [
    "Action",
    "AlreadyRespondedError",
    "ArtifactKind",
    "ItemNotFoundError",
    "RedTeamResponseError",
    "RespondResult",
    "ResponseRequiresTextError",
    "SecondDeferRejectedError",
    "respond",
]
