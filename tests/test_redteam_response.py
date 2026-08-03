"""The forced-response state machine (src/redteam/response.py, PR6 —
monthly_red_team.md Phase 2 "Forced response" bullet): REFUTE requires text
and writes a ledger entry (or an open note for a ticker-less cross-book
item); ACCEPT auto-creates a sizing intent when the proposal reads as sizing,
otherwise an open note; DEFER-once/second-defer-reject; every terminal
transition rejects a further response.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from redteam import response, store  # noqa: E402
from redteam.models import Kind, RedTeamLLMItem  # noqa: E402
from redteam.response import Action  # noqa: E402
from user_state import notes  # noqa: E402
from user_state.ledger import list_entries  # noqa: E402
from user_state.sizing import list_intents  # noqa: E402

PRIOR_HEAD = "0059_kpi_facts_restatement"


@pytest.fixture
def db_path(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    return migrated_db(tmp_path / "redteam_response.db", stamp=PRIOR_HEAD)


def _insert_item(
    db_path: Path,
    *,
    ticker: str | None = "NU",
    kind: Kind = "per_name",
    proposed_change_md: str = "Note the risk in the thesis.",
    run_key: str = "red_team_2026_08",
) -> int:
    return store.insert_item(
        db_path=db_path,
        run_key=run_key,
        ticker=ticker,
        lens="fx_translation" if ticker else "factor_block",
        kind=kind,
        item=RedTeamLLMItem(
            attack_md="Attack text.",
            question_md="Question text?",
            proposed_change_md=proposed_change_md,
            severity="high",
        ),
    )


# ---------------------------------------------------------------------------
# REFUTE
# ---------------------------------------------------------------------------


def test_refute_requires_non_empty_response_md(db_path: Path) -> None:
    item_id = _insert_item(db_path)
    with pytest.raises(response.ResponseRequiresTextError):
        response.respond(db_path=db_path, item_id=item_id, action="refute", response_md=None)
    with pytest.raises(response.ResponseRequiresTextError):
        response.respond(db_path=db_path, item_id=item_id, action="refute", response_md="   ")
    # rejected — item is untouched
    row = store.get_item(db_path=db_path, item_id=item_id)
    assert row is not None
    assert row.status == "open"


def test_refute_persists_status_and_writes_ledger_entry(db_path: Path) -> None:
    item_id = _insert_item(db_path, ticker="NU")
    result = response.respond(
        db_path=db_path,
        item_id=item_id,
        action="refute",
        response_md="The FX exposure is already hedged per the 10-K.",
    )
    assert result.item.status == "refuted"
    assert result.item.response_md == "The FX exposure is already hedged per the 10-K."
    assert result.item.responded_at is not None
    assert result.artifact_kind == "ledger_entry"
    entries = list_entries(ticker="NU", entry_kind="red_team_refute", db_path=db_path)
    assert len(entries) == 1
    assert "already hedged" in entries[0].body
    assert entries[0].id == result.artifact_id


def test_refute_on_cross_book_item_writes_open_note_not_ledger(db_path: Path) -> None:
    item_id = _insert_item(db_path, ticker=None, kind="cross_book")
    result = response.respond(
        db_path=db_path,
        item_id=item_id,
        action="refute",
        response_md="Clusters overlap less than implied.",
    )
    assert result.item.status == "refuted"
    assert result.artifact_kind == "open_note"
    assert result.artifact_id is not None
    note = notes.get_note(result.artifact_id, db_path=db_path)
    assert note is not None
    assert note.ticker is None
    assert "Clusters overlap" in note.body


# ---------------------------------------------------------------------------
# ACCEPT
# ---------------------------------------------------------------------------


def test_accept_sizing_related_per_name_item_creates_draft_sizing_intent(db_path: Path) -> None:
    item_id = _insert_item(
        db_path, ticker="NU", proposed_change_md="Trim the position to reduce weight by 2pp."
    )
    result = response.respond(db_path=db_path, item_id=item_id, action="accept")
    assert result.item.status == "accepted"
    assert result.artifact_kind == "sizing_intent"
    intents = list_intents(ticker="NU", db_path=db_path)
    assert len(intents) == 1
    assert intents[0].intent_kind == "sizing_note"
    assert intents[0].intent_value is None
    assert intents[0].narrative is not None
    assert "[red-team accepted, pending owner edit]" in intents[0].narrative
    assert intents[0].id == result.artifact_id


def test_accept_non_sizing_item_creates_open_note_with_manual_follow_up_marker(
    db_path: Path,
) -> None:
    item_id = _insert_item(
        db_path, ticker="NU", proposed_change_md="Add a soft rule tracking NPL trajectory."
    )
    result = response.respond(db_path=db_path, item_id=item_id, action="accept")
    assert result.item.status == "accepted"
    assert result.artifact_kind == "open_note"
    assert result.artifact_id is not None
    note = notes.get_note(result.artifact_id, db_path=db_path)
    assert note is not None
    assert note.ticker == "NU"
    assert note.status == "open"
    assert "manual follow-up" in note.body
    assert list_intents(ticker="NU", db_path=db_path) == []


def test_accept_cross_book_item_never_creates_sizing_intent(db_path: Path) -> None:
    # Even if the proposal text uses sizing-shaped words, cross-book items
    # (no single ticker) never map to position_sizing_intent.
    item_id = _insert_item(
        db_path, ticker=None, kind="cross_book", proposed_change_md="Trim book-wide LatAm exposure."
    )
    result = response.respond(db_path=db_path, item_id=item_id, action="accept")
    assert result.artifact_kind == "open_note"


# ---------------------------------------------------------------------------
# DEFER
# ---------------------------------------------------------------------------


def test_first_defer_sets_status_deferred_and_bumps_count(db_path: Path) -> None:
    item_id = _insert_item(db_path)
    result = response.respond(db_path=db_path, item_id=item_id, action="defer")
    assert result.item.status == "deferred"
    assert result.item.defer_count == 1
    assert result.artifact_kind == "none"


def test_second_defer_is_rejected(db_path: Path) -> None:
    item_id = _insert_item(db_path)
    response.respond(db_path=db_path, item_id=item_id, action="defer")
    with pytest.raises(response.SecondDeferRejectedError) as excinfo:
        response.respond(db_path=db_path, item_id=item_id, action="defer")
    assert excinfo.value.item.status == "deferred"
    assert excinfo.value.item.defer_count == 1
    # defer_count did NOT increment further on the rejected attempt
    row = store.get_item(db_path=db_path, item_id=item_id)
    assert row is not None
    assert row.defer_count == 1


def test_deferred_item_is_still_answerable_by_refute_or_accept(db_path: Path) -> None:
    item_id = _insert_item(db_path)
    response.respond(db_path=db_path, item_id=item_id, action="defer")
    result = response.respond(
        db_path=db_path, item_id=item_id, action="refute", response_md="Answering after a defer."
    )
    assert result.item.status == "refuted"


# ---------------------------------------------------------------------------
# Terminal-state rejection + missing item
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["refute", "accept", "defer"])
def test_terminal_status_rejects_any_further_response(db_path: Path, action: str) -> None:
    item_id = _insert_item(db_path)
    response.respond(db_path=db_path, item_id=item_id, action="accept")
    with pytest.raises(response.AlreadyRespondedError):
        response.respond(
            db_path=db_path,
            item_id=item_id,
            action=cast("Action", action),
            response_md="text" if action == "refute" else None,
        )


def test_unknown_item_id_raises_item_not_found(db_path: Path) -> None:
    with pytest.raises(response.ItemNotFoundError):
        response.respond(db_path=db_path, item_id=999999, action="accept")
