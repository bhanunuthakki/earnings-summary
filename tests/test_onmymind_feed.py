"""On My Mind (P1) — the reverse-chron capture feed + the action ladder.

Covers the read model (keyset paging, batched Wondering badge, kind allow-list),
the one action core (dismiss / save / discuss / incorporate) with idempotent
incorporate, the panel wiring behind LEDGER_ONMYMIND, and the Telegram callbacks.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from capture import ingest, research_notify, telegram
from capture.matcher import build_roster_index
from onmymind.feed import FeedItem, act_on_feed_item, load_feed, onmymind_enabled
from pipeline.ledger_panel import render_ledger_panel, render_onmymind_list
from research import proposals
from user_state import notes

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0059_kpi_facts_restatement"
ROSTER = build_roster_index(symbols=["NU"], phrases={"nubank": "NU"})


def _cfg(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "ledger.db"
    cfg = _cfg(db)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, "head")
    return db


@pytest.fixture(autouse=True)
def _flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    # Default the flag OFF for every test; the panel-wiring tests opt in.
    monkeypatch.delenv("LEDGER_ONMYMIND", raising=False)


def _musing(db_path: Path, text: str) -> int:
    return (
        ingest.ingest_capture(channel="tray", text=text, roster=ROSTER, db_path=db_path).note_id
        or 0
    )


def _yes(_text: str) -> dict[str, object]:
    return {"is_wondering": True, "claim": "do NU margins hold?", "ticker": "NU"}


def _ids(page_items: list[FeedItem]) -> list[int]:
    return [i.note.id for i in page_items]


# ---------------------------------------------------------------------------
# Read model
# ---------------------------------------------------------------------------


def test_flag_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEDGER_ONMYMIND", raising=False)
    assert onmymind_enabled() is False
    monkeypatch.setenv("LEDGER_ONMYMIND", "1")
    assert onmymind_enabled() is True


def test_feed_unifies_musings_and_readings_newest_first(db_path: Path) -> None:
    _musing(db_path, "the macro tape feels off")
    ingest.ingest_reading(
        channel="telegram", url="https://example.com/deck", external_ref="tg:1", db_path=db_path
    )
    _musing(db_path, "NU credit cycle looks early")
    page = load_feed(db_path=db_path)
    bodies = [i.note.body for i in page.items]
    assert "NU credit cycle looks early" in bodies
    assert "https://example.com/deck" in bodies
    assert "the macro tape feels off" in bodies
    # newest first: the last-landed musing tops the feed
    assert page.items[0].note.body == "NU credit cycle looks early"
    # readings carry their shape
    link = next(i for i in page.items if i.item_type == "link")
    assert link.note.body == "https://example.com/deck"


def test_intent_kind_excluded_from_feed(db_path: Path) -> None:
    # source='capture' also carries `intent` (standing intents) — the feed's kind
    # allow-list must keep them out (they have their own surface).
    notes.create_note(
        ticker=None,
        kind="intent",
        body="build an options sleeve",
        source="capture",
        db_path=db_path,
    )
    _musing(db_path, "a real thought")
    bodies = [i.note.body for i in load_feed(db_path=db_path).items]
    assert "a real thought" in bodies
    assert "build an options sleeve" not in bodies


def test_manual_notes_excluded_from_feed(db_path: Path) -> None:
    # Only source='capture' is On My Mind; a typed `manual` observation is not.
    notes.create_note(
        ticker="NU", kind="observation", body="a manual note", source="manual", db_path=db_path
    )
    _musing(db_path, "a captured thought")
    bodies = [i.note.body for i in load_feed(db_path=db_path).items]
    assert "a captured thought" in bodies
    assert "a manual note" not in bodies


def test_keyset_pagination_no_dup_or_skip(db_path: Path) -> None:
    ids = [_musing(db_path, f"thought {i}") for i in range(5)]
    seen: list[int] = []
    cursor: str | None = None
    for _ in range(10):  # generous safety bound
        page = load_feed(cursor=cursor, limit=2, db_path=db_path)
        assert len(page.items) <= 2
        seen.extend(_ids(page.items))
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    # every id exactly once, newest (highest id) first
    assert seen == sorted(ids, reverse=True)


def test_keyset_is_stable_across_a_midscroll_capture(db_path: Path) -> None:
    older = [_musing(db_path, f"old {i}") for i in range(3)]
    page1 = load_feed(limit=2, db_path=db_path)
    assert _ids(page1.items) == [older[2], older[1]]
    # a fresh capture lands ABOVE the cursor mid-scroll…
    _musing(db_path, "brand new")
    page2 = load_feed(cursor=page1.next_cursor, limit=2, db_path=db_path)
    # …the next page still continues from the cursor — the newcomer is not
    # injected into it, and nothing older is skipped.
    assert _ids(page2.items) == [older[0]]


def test_wondering_badge_is_batched(db_path: Path) -> None:
    flat = _musing(db_path, "flat observation about the tape")
    wonder = _musing(db_path, "do NU's margins still hold up here?")
    proposals.detect_and_create_task(wonder, db_path=db_path, call=_yes)
    by_id = {i.note.id: i for i in load_feed(db_path=db_path).items}
    w = by_id[wonder].wondering
    assert w is not None
    assert w.ticker == "NU"
    assert by_id[flat].wondering is None


def test_dismissed_items_leave_the_feed(db_path: Path) -> None:
    keep = _musing(db_path, "keep this one")
    drop = _musing(db_path, "dismiss this one")
    act_on_feed_item(drop, "dismiss", db_path=db_path)
    ids = _ids(load_feed(db_path=db_path).items)
    assert keep in ids
    assert drop not in ids


# ---------------------------------------------------------------------------
# The action core
# ---------------------------------------------------------------------------


def test_dismiss_archives(db_path: Path) -> None:
    nid = _musing(db_path, "meh")
    res = act_on_feed_item(nid, "dismiss", db_path=db_path)
    assert res.ok and res.removed
    note = notes.get_note(nid, db_path=db_path)
    assert note is not None and note.status == "archived"


def test_save_sets_ladder_and_stays_visible(db_path: Path) -> None:
    nid = _musing(db_path, "read again later")
    res = act_on_feed_item(nid, "save", db_path=db_path)
    assert res.ok and res.ladder == "saved"
    note = notes.get_note(nid, db_path=db_path)
    assert note is not None and (note.context or {}).get("ladder") == "saved"
    assert nid in _ids(load_feed(db_path=db_path).items)


def test_discuss_hands_off_to_a_thread(db_path: Path) -> None:
    ticker_note = _musing(db_path, "NU credit cycle looks early")  # roster → NU
    res = act_on_feed_item(ticker_note, "discuss", db_path=db_path)
    assert res.ok and res.ladder == "discuss"
    assert res.thread_url == "/ticker/NU"
    portfolio_note = _musing(db_path, "the whole tape feels frothy")
    res2 = act_on_feed_item(portfolio_note, "discuss", db_path=db_path)
    assert res2.thread_url == "/"


def test_incorporate_creates_task_and_is_idempotent(db_path: Path) -> None:
    nid = _musing(db_path, "do NU's margins still hold?")
    r1 = act_on_feed_item(nid, "incorporate", db_path=db_path)
    assert r1.ok and r1.task_id is not None
    r2 = act_on_feed_item(nid, "incorporate", db_path=db_path)
    assert r2.task_id == r1.task_id  # a second tap reuses the same task
    mine = [t for t in proposals.list_tasks(db_path=db_path) if t.note_id == nid]
    assert len(mine) == 1
    note = notes.get_note(nid, db_path=db_path)
    assert note is not None and (note.context or {}).get("ladder") == "incorporated"


def test_incorporate_reuses_the_auto_tap_task(db_path: Path) -> None:
    # The auto-tap and the incorporate button must converge on one task per note.
    nid = _musing(db_path, "do NU's margins still hold?")
    tid = proposals.detect_and_create_task(nid, db_path=db_path, call=_yes)
    assert tid is not None
    res = act_on_feed_item(nid, "incorporate", db_path=db_path)
    assert res.task_id == tid
    mine = [t for t in proposals.list_tasks(db_path=db_path) if t.note_id == nid]
    assert len(mine) == 1


def test_action_on_missing_note_is_not_ok(db_path: Path) -> None:
    for verb in ("dismiss", "save", "discuss", "incorporate"):
        res = act_on_feed_item(999999, verb, db_path=db_path)
        assert res.ok is False


def test_unknown_verb_raises(db_path: Path) -> None:
    nid = _musing(db_path, "x")
    with pytest.raises(ValueError, match="unknown ladder verb"):
        act_on_feed_item(nid, "bogus", db_path=db_path)


# ---------------------------------------------------------------------------
# Panel wiring (behind LEDGER_ONMYMIND)
# ---------------------------------------------------------------------------


def test_panel_shows_onmymind_when_flag_on(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEDGER_ONMYMIND", "1")
    _musing(db_path, "a thought")
    html = render_ledger_panel(db_path)
    assert "On My Mind" in html
    assert "data-om-verb" in html  # the ladder buttons rendered
    # On My Mind subsumes the plain list — the Musings header is suppressed.
    assert ">Musings<" not in html


def test_panel_unchanged_when_flag_off(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEDGER_ONMYMIND", raising=False)
    _musing(db_path, "a thought")
    html = render_ledger_panel(db_path)
    assert "On My Mind" not in html
    assert ">Musings<" in html  # the original list stands


def test_render_list_renders_readings_and_load_more(db_path: Path) -> None:
    ingest.ingest_reading(
        channel="telegram", url="https://example.com/x", external_ref="tg:1", db_path=db_path
    )
    ingest.ingest_reading(
        channel="telegram",
        local_path="/tmp/deck.pdf",
        file_name="deck.pdf",
        caption="a great read",
        external_ref="tg:2",
        db_path=db_path,
    )
    for i in range(3):
        _musing(db_path, f"thought {i}")
    html = render_onmymind_list(db_path, cursor=None, user_id=notes.DEFAULT_USER_ID)
    assert "https://example.com/x" in html
    assert "deck.pdf" in html and "a great read" in html
    # 5 items > default page (30)? no — but exercise the empty-cursor path renders cards
    assert "data-om-id" in html


def test_render_list_empty_state(db_path: Path) -> None:
    html = render_onmymind_list(db_path)
    assert "Nothing on your mind yet" in html


# ---------------------------------------------------------------------------
# Telegram callbacks
# ---------------------------------------------------------------------------


class _Spy:
    def __init__(self) -> None:
        self.answers: list[str | None] = []

    def send(self, token: str, chat_id: int, text: str, *, reply_markup: object = None) -> object:
        return {}

    def answer(self, token: str, cqid: str, *, text: str | None = None) -> object:
        self.answers.append(text)
        return {}


def test_onmymind_keyboard_carries_callback_data() -> None:
    flat = str(research_notify.onmymind_keyboard(42))
    for verb in ("om:incorporate:42", "om:discuss:42", "om:save:42", "om:dismiss:42"):
        assert verb in flat


def test_dispatch_om_save(db_path: Path) -> None:
    nid = _musing(db_path, "keep this")
    spy = _Spy()
    upd = telegram.Update(
        update_id=1,
        kind="callback",
        chat_id=5,
        callback_data=f"om:save:{nid}",
        callback_query_id="cq",
    )
    status = research_notify.dispatch_callback(
        "tok", upd, db_path=db_path, send=spy.send, answer=spy.answer
    )
    assert status == "om_save"
    note = notes.get_note(nid, db_path=db_path)
    assert note is not None and (note.context or {}).get("ladder") == "saved"
    assert spy.answers and "Saved" in (spy.answers[0] or "")


def test_dispatch_om_incorporate_creates_task(db_path: Path) -> None:
    nid = _musing(db_path, "do NU's margins hold?")
    spy = _Spy()
    upd = telegram.Update(
        update_id=1,
        kind="callback",
        chat_id=5,
        callback_data=f"om:incorporate:{nid}",
        callback_query_id="cq",
    )
    status = research_notify.dispatch_callback(
        "tok", upd, db_path=db_path, send=spy.send, answer=spy.answer
    )
    assert status == "om_incorporate"
    assert proposals.get_task_for_note(nid, db_path=db_path) is not None


def test_dispatch_om_unknown_verb_is_safe(db_path: Path) -> None:
    spy = _Spy()
    upd = telegram.Update(
        update_id=1, kind="callback", chat_id=5, callback_data="om:bogus:1", callback_query_id="cq"
    )
    status = research_notify.dispatch_callback(
        "tok", upd, db_path=db_path, send=spy.send, answer=spy.answer
    )
    assert status is None
    assert spy.answers == ["Unrecognized action."]
