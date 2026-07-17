"""The Ledger capture poller — poll_once orchestration over a mocked Telegram."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from capture import poller, telegram, transcribe
from capture.matcher import build_roster_index
from user_state import notes

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0059_kpi_facts_restatement"
ROSTER = build_roster_index(symbols=["NU", "MELI"], phrases={"nubank": "NU"})


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


def test_load_save_offset_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "offset.json"
    assert poller.load_offset(p) is None
    poller.save_offset(p, 42)
    assert poller.load_offset(p) == 42


def test_poll_once_ingests_text_and_advances_offset(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updates = [
        telegram.Update(update_id=10, kind="text", chat_id=1, text="Nubank looks compelling"),
        telegram.Update(update_id=11, kind="text", chat_id=1, text="the market feels toppy"),
    ]
    monkeypatch.setattr(telegram, "get_updates", lambda token, offset=None, timeout=50: updates)
    offset_path = tmp_path / "offset.json"
    counts = poller.poll_once(
        "tok",
        db_path=db_path,
        offset_path=offset_path,
        audio_dir=tmp_path / "audio",
        roster=ROSTER,
        confirm=False,
    )
    assert counts["updates"] == 2
    assert counts.get("landed") == 2
    assert len(notes.list_notes(kind="musing", db_path=db_path)) == 2
    assert json.loads(offset_path.read_text(encoding="utf-8"))["offset"] == 12


def test_poll_once_dedups_on_replay(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updates = [telegram.Update(update_id=20, kind="text", chat_id=1, text="MELI looks cheap")]
    monkeypatch.setattr(telegram, "get_updates", lambda token, offset=None, timeout=50: updates)
    offset_path = tmp_path / "offset.json"
    audio = tmp_path / "audio"
    poller.poll_once(
        "t", db_path=db_path, offset_path=offset_path, audio_dir=audio, roster=ROSTER, confirm=False
    )
    counts = poller.poll_once(
        "t", db_path=db_path, offset_path=offset_path, audio_dir=audio, roster=ROSTER, confirm=False
    )
    assert counts.get("duplicate") == 1
    assert len(notes.list_notes(kind="musing", db_path=db_path)) == 1


def test_poll_once_voice_downloads_lands_and_purges_audio(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updates = [telegram.Update(update_id=30, kind="voice", chat_id=1, voice_file_id="F1")]
    monkeypatch.setattr(telegram, "get_updates", lambda token, offset=None, timeout=50: updates)
    monkeypatch.setattr(telegram, "get_file_path", lambda token, file_id: "voice/f1.oga")

    def _download(token: str, file_path: str, dest: object) -> Path:
        out = Path(str(dest))
        out.write_bytes(b"\x00")
        return out

    monkeypatch.setattr(telegram, "download_file", _download)
    monkeypatch.setattr(transcribe, "transcribe", lambda path: "thinking about Nubank credit")
    audio = tmp_path / "audio"
    counts = poller.poll_once(
        "t",
        db_path=db_path,
        offset_path=tmp_path / "offset.json",
        audio_dir=audio,
        roster=ROSTER,
        confirm=False,
    )
    assert counts.get("landed") == 1
    musings = notes.list_notes(kind="musing", db_path=db_path)
    assert len(musings) == 1
    assert musings[0].ticker == "NU"
    assert not (audio / "tg_30.oga").exists()  # audio purged once landed


def test_poll_once_needs_ticker_offers_inline_candidates(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ambiguous musing (≥2 roster tickers) must not dead-end at 'go open
    the Ledger' — the confirm carries one set-ticker button per candidate
    (``st:<ticker>:<note_id>``), answerable from the thread."""
    updates = [
        telegram.Update(update_id=90, kind="text", chat_id=1, text="NU vs MELI - add to which?")
    ]

    def _get_updates(
        token: str, offset: int | None = None, timeout: int = 50
    ) -> list[telegram.Update]:
        return updates

    sent: list[tuple[str, object]] = []

    def _send(
        token: str, chat_id: int, text: str, reply_markup: object = None, **k: object
    ) -> None:
        sent.append((text, reply_markup))

    monkeypatch.setattr(telegram, "get_updates", _get_updates)
    monkeypatch.setattr(telegram, "send_message", _send)
    counts = poller.poll_once(
        "tok",
        db_path=db_path,
        offset_path=tmp_path / "offset.json",
        audio_dir=tmp_path / "audio",
        roster=ROSTER,
        confirm=True,
    )
    assert counts.get("landed") == 1
    musings = notes.list_notes(kind="musing", db_path=db_path)
    assert len(musings) == 1 and musings[0].ticker is None
    note_id = musings[0].id
    assert len(sent) == 1
    text, markup = sent[0]
    assert text == "Captured. Which ticker?"
    flat = str(markup)
    assert f"st:NU:{note_id}" in flat and f"st:MELI:{note_id}" in flat


def test_poll_once_skips_bot_commands(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updates = [
        telegram.Update(update_id=40, kind="text", chat_id=1, text="/start"),
        telegram.Update(update_id=41, kind="text", chat_id=1, text="Nubank looks good here"),
    ]
    monkeypatch.setattr(telegram, "get_updates", lambda token, offset=None, timeout=50: updates)
    sent: list[str] = []
    monkeypatch.setattr(
        telegram, "send_message", lambda token, chat_id, text, **k: sent.append(text)
    )
    counts = poller.poll_once(
        "tok",
        db_path=db_path,
        offset_path=tmp_path / "offset.json",
        audio_dir=tmp_path / "audio",
        roster=ROSTER,
        confirm=True,
    )
    assert counts.get("command") == 1
    musings = notes.list_notes(kind="musing", db_path=db_path)
    assert len(musings) == 1  # only the real thought landed, not /start
    assert "Nubank looks good" in musings[0].body
    assert any("capture is live" in s for s in sent)  # /start got a greeting


def test_poll_once_review_command_replies_with_pre_analysis(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The coach's pings say "/review {ticker}" — the poller must answer that
    call-to-action in-channel instead of silently swallowing it."""
    updates = [telegram.Update(update_id=50, kind="text", chat_id=1, text="/review NU")]
    monkeypatch.setattr(telegram, "get_updates", lambda token, offset=None, timeout=50: updates)
    sent: list[str] = []
    monkeypatch.setattr(
        telegram, "send_message", lambda token, chat_id, text, **k: sent.append(text)
    )
    seen_calls: list[tuple[object, str, bool]] = []

    def _fake_reply_text(repo_root: object, text: str, *, plain: bool = False) -> str:
        seen_calls.append((repo_root, text, plain))
        return "NU - position review (deterministic read)"

    monkeypatch.setattr(
        "advisor.position_review.review_reply_text", _fake_reply_text, raising=False
    )
    counts = poller.poll_once(
        "tok",
        db_path=db_path,
        offset_path=tmp_path / "offset.json",
        audio_dir=tmp_path / "audio",
        roster=ROSTER,
        confirm=True,
    )
    assert counts.get("command_review") == 1
    assert counts.get("command") is None  # /review is its own bucket, not "command"
    assert len(sent) == 1
    assert "NU" in sent[0]
    assert seen_calls and seen_calls[0][1] == "/review NU"
    assert seen_calls[0][2] is True  # the poller's /review reply uses the plain renderer
    # no capture/ingest occurred for a slash command
    assert notes.list_notes(kind="musing", db_path=db_path) == []


def test_poll_once_review_command_degrades_on_failure(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updates = [telegram.Update(update_id=51, kind="text", chat_id=1, text="/review BADTICKER")]
    monkeypatch.setattr(telegram, "get_updates", lambda token, offset=None, timeout=50: updates)
    sent: list[str] = []
    monkeypatch.setattr(
        telegram, "send_message", lambda token, chat_id, text, **k: sent.append(text)
    )

    def _boom(repo_root: object, text: str, *, plain: bool = False) -> str:
        raise RuntimeError("cockpit unavailable")

    monkeypatch.setattr("advisor.position_review.review_reply_text", _boom, raising=False)
    counts = poller.poll_once(
        "tok",
        db_path=db_path,
        offset_path=tmp_path / "offset.json",
        audio_dir=tmp_path / "audio",
        roster=ROSTER,
        confirm=True,
    )
    assert counts.get("command_review") == 1
    assert len(sent) == 1
    assert "Couldn't build a review" in sent[0] and "BADTICKER" in sent[0]


def _seed_red_team_item(
    db_path: Path, *, ticker: str = "NU", proposed_change_md: str = "Trim on FX risk."
) -> int:
    from redteam import store
    from redteam.models import RedTeamLLMItem

    return store.insert_item(
        db_path=db_path,
        run_key="red_team_2026_08",
        ticker=ticker,
        lens="fx_translation",
        kind="per_name",
        item=RedTeamLLMItem(
            attack_md="Attack text.",
            question_md="Question text?",
            proposed_change_md=proposed_change_md,
            severity="high",
        ),
    )


def test_poll_once_redteam_list_command_replies_with_numbered_items(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "/redteam" (PR6) — the poller must list open items in-channel, LLM-free,
    the same shape /review already gets."""
    _seed_red_team_item(db_path)
    updates = [telegram.Update(update_id=70, kind="text", chat_id=1, text="/redteam")]
    monkeypatch.setattr(telegram, "get_updates", lambda token, offset=None, timeout=50: updates)
    sent: list[str] = []
    monkeypatch.setattr(
        telegram, "send_message", lambda token, chat_id, text, **k: sent.append(text)
    )
    counts = poller.poll_once(
        "tok",
        db_path=db_path,
        offset_path=tmp_path / "offset.json",
        audio_dir=tmp_path / "audio",
        roster=ROSTER,
        confirm=True,
    )
    assert counts.get("command_redteam") == 1
    assert counts.get("command") is None  # its own bucket, not "command"
    assert len(sent) == 1
    assert "red_team_2026_08" in sent[0]
    assert "1. [HIGH] NU" in sent[0]
    assert notes.list_notes(kind="musing", db_path=db_path) == []


def test_poll_once_redteam_accept_command_responds_via_shared_state_machine(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "/redteam 1 accept" answers item #1 through redteam.response.respond —
    the SAME function the Flask /api/red_team/<id>/respond route calls."""
    from redteam import store

    item_id = _seed_red_team_item(db_path)
    updates = [telegram.Update(update_id=71, kind="text", chat_id=1, text="/redteam 1 accept")]
    monkeypatch.setattr(telegram, "get_updates", lambda token, offset=None, timeout=50: updates)
    sent: list[str] = []
    monkeypatch.setattr(
        telegram, "send_message", lambda token, chat_id, text, **k: sent.append(text)
    )
    counts = poller.poll_once(
        "tok",
        db_path=db_path,
        offset_path=tmp_path / "offset.json",
        audio_dir=tmp_path / "audio",
        roster=ROSTER,
        confirm=True,
    )
    assert counts.get("command_redteam") == 1
    assert "Accepted #1" in sent[0]
    row = store.get_item(db_path=db_path, item_id=item_id)
    assert row is not None
    assert row.status == "accepted"


def test_poll_once_redteam_refute_without_text_asks_for_reasoning(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_red_team_item(db_path)
    updates = [telegram.Update(update_id=72, kind="text", chat_id=1, text="/redteam 1 refute")]
    monkeypatch.setattr(telegram, "get_updates", lambda token, offset=None, timeout=50: updates)
    sent: list[str] = []
    monkeypatch.setattr(
        telegram, "send_message", lambda token, chat_id, text, **k: sent.append(text)
    )
    poller.poll_once(
        "tok",
        db_path=db_path,
        offset_path=tmp_path / "offset.json",
        audio_dir=tmp_path / "audio",
        roster=ROSTER,
        confirm=True,
    )
    assert "REFUTE needs your reasoning" in sent[0]


def test_poll_once_redteam_refute_with_text_writes_ledger(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from user_state.ledger import list_entries

    _seed_red_team_item(db_path)
    updates = [
        telegram.Update(
            update_id=73, kind="text", chat_id=1, text="/redteam 1 refute Already hedged."
        )
    ]
    monkeypatch.setattr(telegram, "get_updates", lambda token, offset=None, timeout=50: updates)
    sent: list[str] = []
    monkeypatch.setattr(
        telegram, "send_message", lambda token, chat_id, text, **k: sent.append(text)
    )
    poller.poll_once(
        "tok",
        db_path=db_path,
        offset_path=tmp_path / "offset.json",
        audio_dir=tmp_path / "audio",
        roster=ROSTER,
        confirm=True,
    )
    assert "Refuted #1" in sent[0]
    entries = list_entries(ticker="NU", entry_kind="red_team_refute", db_path=db_path)
    assert len(entries) == 1
    assert "Already hedged" in entries[0].body


def test_poll_once_redteam_second_defer_reports_escalation(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_red_team_item(db_path)
    monkeypatch.setattr(
        telegram,
        "get_updates",
        lambda token, offset=None, timeout=50: [
            telegram.Update(update_id=74, kind="text", chat_id=1, text="/redteam 1 defer")
        ],
    )
    sent: list[str] = []
    monkeypatch.setattr(
        telegram, "send_message", lambda token, chat_id, text, **k: sent.append(text)
    )
    poller.poll_once(
        "tok",
        db_path=db_path,
        offset_path=tmp_path / "offset.json",
        audio_dir=tmp_path / "audio",
        roster=ROSTER,
        confirm=True,
    )
    assert "Deferred #1" in sent[0]

    # A second /redteam with a fresh listing re-numbers against the still-open
    # (deferred) item as #1 again; deferring it a second time must escalate.
    monkeypatch.setattr(
        telegram,
        "get_updates",
        lambda token, offset=None, timeout=50: [
            telegram.Update(update_id=75, kind="text", chat_id=1, text="/redteam 1 defer")
        ],
    )
    poller.poll_once(
        "tok",
        db_path=db_path,
        offset_path=tmp_path / "offset.json",
        audio_dir=tmp_path / "audio",
        roster=ROSTER,
        confirm=True,
    )
    assert "already deferred once" in sent[1]
    assert "escalated" in sent[1]


def test_poll_once_redteam_out_of_range_index_replies_with_usage(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_red_team_item(db_path)
    updates = [telegram.Update(update_id=76, kind="text", chat_id=1, text="/redteam 9 accept")]
    monkeypatch.setattr(telegram, "get_updates", lambda token, offset=None, timeout=50: updates)
    sent: list[str] = []
    monkeypatch.setattr(
        telegram, "send_message", lambda token, chat_id, text, **k: sent.append(text)
    )
    poller.poll_once(
        "tok",
        db_path=db_path,
        offset_path=tmp_path / "offset.json",
        audio_dir=tmp_path / "audio",
        roster=ROSTER,
        confirm=True,
    )
    assert "No item #9" in sent[0]


def test_poll_once_redteam_list_when_no_run_yet(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updates = [telegram.Update(update_id=77, kind="text", chat_id=1, text="/redteam")]
    monkeypatch.setattr(telegram, "get_updates", lambda token, offset=None, timeout=50: updates)
    sent: list[str] = []
    monkeypatch.setattr(
        telegram, "send_message", lambda token, chat_id, text, **k: sent.append(text)
    )
    poller.poll_once(
        "tok",
        db_path=db_path,
        offset_path=tmp_path / "offset.json",
        audio_dir=tmp_path / "audio",
        roster=ROSTER,
        confirm=True,
    )
    assert "No red-team run yet" in sent[0]


def test_poll_once_unknown_command_replies_instead_of_vanishing(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updates = [telegram.Update(update_id=60, kind="text", chat_id=1, text="/foobar")]
    monkeypatch.setattr(telegram, "get_updates", lambda token, offset=None, timeout=50: updates)
    sent: list[str] = []
    monkeypatch.setattr(
        telegram, "send_message", lambda token, chat_id, text, **k: sent.append(text)
    )
    counts = poller.poll_once(
        "tok",
        db_path=db_path,
        offset_path=tmp_path / "offset.json",
        audio_dir=tmp_path / "audio",
        roster=ROSTER,
        confirm=True,
    )
    assert counts.get("command") == 1
    assert len(sent) == 1
    assert "Not a command here" in sent[0] and "/review" in sent[0]
    assert notes.list_notes(kind="musing", db_path=db_path) == []


def test_poll_once_answers_a_question(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A question sent to the bot gets ANSWERED in-thread via the shared answer
    core (the overhaul) — not just 'Captured.'. The engine is faked so no live
    model call runs; the real answer_capture plumbing (store + return) executes."""
    from collections.abc import Iterator

    updates = [
        telegram.Update(update_id=80, kind="text", chat_id=1, text="What's my cost basis on MELI?")
    ]
    monkeypatch.setattr(telegram, "get_updates", lambda token, offset=None, timeout=50: updates)
    sent: list[str] = []
    monkeypatch.setattr(
        telegram, "send_message", lambda token, chat_id, text, **k: sent.append(text)
    )

    def _stub(*_a: object, **_k: object) -> Iterator[dict[str, object]]:
        yield {"type": "final", "text": "Your MELI cost basis is $1,240.", "route": "narrative"}

    monkeypatch.setattr("onmymind.respond.respond_turn", _stub)
    counts = poller.poll_once(
        "tok",
        db_path=db_path,
        offset_path=tmp_path / "offset.json",
        audio_dir=tmp_path / "audio",
        roster=ROSTER,
        confirm=True,
    )
    assert counts.get("landed") == 1
    assert any("cost basis is $1,240" in s for s in sent)  # the answer reached the thread
    musings = notes.list_notes(kind="musing", db_path=db_path)
    assert (musings[0].context or {}).get("ledger_answer", {}).get("text") == (
        "Your MELI cost basis is $1,240."
    )


def test_poll_once_musing_not_answered(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plain reflection (not a question) never spins the engine and gets no
    answer message — only the ordinary capture confirm."""
    from collections.abc import Iterator

    updates = [telegram.Update(update_id=81, kind="text", chat_id=1, text="MELI looks cheap here")]
    monkeypatch.setattr(telegram, "get_updates", lambda token, offset=None, timeout=50: updates)
    sent: list[str] = []
    monkeypatch.setattr(
        telegram, "send_message", lambda token, chat_id, text, **k: sent.append(text)
    )
    engine_calls = {"n": 0}

    def _stub(*_a: object, **_k: object) -> Iterator[dict[str, object]]:
        engine_calls["n"] += 1
        yield {"type": "final", "text": "should not run", "route": "narrative"}

    monkeypatch.setattr("onmymind.respond.respond_turn", _stub)
    poller.poll_once(
        "tok",
        db_path=db_path,
        offset_path=tmp_path / "offset.json",
        audio_dir=tmp_path / "audio",
        roster=ROSTER,
        confirm=True,
    )
    assert engine_calls["n"] == 0  # the answer core self-gates before the engine
    assert not any("should not run" in s for s in sent)


def test_poll_once_start_command_still_greets(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/start behavior is unchanged by the unknown-command reply."""
    updates = [telegram.Update(update_id=70, kind="text", chat_id=1, text="/start")]
    monkeypatch.setattr(telegram, "get_updates", lambda token, offset=None, timeout=50: updates)
    sent: list[str] = []
    monkeypatch.setattr(
        telegram, "send_message", lambda token, chat_id, text, **k: sent.append(text)
    )
    counts = poller.poll_once(
        "tok",
        db_path=db_path,
        offset_path=tmp_path / "offset.json",
        audio_dir=tmp_path / "audio",
        roster=ROSTER,
        confirm=True,
    )
    assert counts.get("command") == 1
    assert len(sent) == 1
    assert "capture is live" in sent[0]
    assert "Not a command here" not in sent[0]


# ---------------------------------------------------------------------------
# Card-reply routing parity (a free-text REPLY to an On My Mind card → the SAME
# handle_reply core the web reply box calls — one brain, two mouths).
# ---------------------------------------------------------------------------


def test_parse_update_carries_reply_to_message_id() -> None:
    reply = telegram.parse_update(
        {
            "update_id": 1,
            "message": {
                "message_id": 9,
                "chat": {"id": 1},
                "text": "dig in",
                "reply_to_message": {"message_id": 7},
            },
        }
    )
    assert reply.kind == "text" and reply.reply_to_message_id == 7
    plain = telegram.parse_update(
        {"update_id": 2, "message": {"message_id": 10, "chat": {"id": 1}, "text": "yo"}}
    )
    assert plain.kind == "text" and plain.reply_to_message_id is None


def _seed_card(db_path: Path, body: str, mid: int, *, ticker: str | None = None) -> int:
    """A landed card whose Telegram message id is stamped on the note — what
    ``_stash_card_mid`` writes when the poller sends the ladder card."""
    row = notes.create_note(
        body=body,
        kind="musing",
        ticker=ticker,
        source="capture",
        context={"channel": "telegram", "telegram_message_id": mid},
        db_path=db_path,
    )
    return row.id


def _reply_update(update_id: int, text: str, reply_to: int) -> telegram.Update:
    return telegram.Update(
        update_id=update_id, kind="text", chat_id=1, text=text, reply_to_message_id=reply_to
    )


def _stub_reply_intent(monkeypatch: pytest.MonkeyPatch, intent: str) -> None:
    import onmymind.reply as reply_mod
    from onmymind.reply import ReplyVerdict

    monkeypatch.setattr(reply_mod, "classify_reply", lambda c, r, **kw: ReplyVerdict(intent=intent))


def test_poll_once_card_reply_action_routes_through_core(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    note_id = _seed_card(db_path, "RBRK monetizes data volume", 500)
    monkeypatch.setattr(
        telegram,
        "get_updates",
        lambda token, offset=None, timeout=50: [_reply_update(100, "dig into this", 500)],
    )
    _stub_reply_intent(monkeypatch, "research")
    sent: list[str] = []
    monkeypatch.setattr(
        telegram, "send_message", lambda token, chat_id, text, **k: sent.append(text)
    )
    counts = poller.poll_once(
        "tok",
        db_path=db_path,
        offset_path=tmp_path / "offset.json",
        audio_dir=tmp_path / "audio",
        roster=ROSTER,
        confirm=True,
    )
    assert counts.get("card_reply") == 1
    assert any("Sent to research" in s for s in sent)  # the action receipt
    note = notes.get_note(note_id, db_path=db_path)
    assert note is not None and (note.context or {}).get("ladder") == "incorporated"
    # the reply text was routed, NOT re-captured as a new musing
    assert len(notes.list_notes(kind="musing", db_path=db_path)) == 1


def test_poll_once_card_reply_note_appends_owner_thread(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    note_id = _seed_card(db_path, "NU keeps compounding", 501)
    monkeypatch.setattr(
        telegram,
        "get_updates",
        lambda token, offset=None, timeout=50: [_reply_update(101, "context: earnings 8/12", 501)],
    )
    _stub_reply_intent(monkeypatch, "note")
    sent: list[str] = []
    monkeypatch.setattr(
        telegram, "send_message", lambda token, chat_id, text, **k: sent.append(text)
    )
    counts = poller.poll_once(
        "tok",
        db_path=db_path,
        offset_path=tmp_path / "offset.json",
        audio_dir=tmp_path / "audio",
        roster=ROSTER,
        confirm=True,
    )
    assert counts.get("card_reply") == 1
    assert any(s == "Noted." for s in sent)
    note = notes.get_note(note_id, db_path=db_path)
    assert note is not None and (note.context or {}).get("owner_replies") == [
        "context: earnings 8/12"
    ]
    assert len(notes.list_notes(kind="musing", db_path=db_path)) == 1


def test_poll_once_card_reply_chat_answers_via_shared_engine(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 'question' reply is answered in-thread through the SAME ask engine the
    capture answer tap uses — the reply text is answered, not just filed."""
    from collections.abc import Iterator

    _seed_card(db_path, "NU keeps compounding", 502)
    monkeypatch.setattr(
        telegram,
        "get_updates",
        lambda token, offset=None, timeout=50: [_reply_update(102, "what changed since?", 502)],
    )
    _stub_reply_intent(monkeypatch, "question")
    sent: list[str] = []
    monkeypatch.setattr(
        telegram, "send_message", lambda token, chat_id, text, **k: sent.append(text)
    )

    def _stub(*_a: object, **_k: object) -> Iterator[dict[str, object]]:
        yield {"type": "final", "text": "Nothing material changed.", "route": "narrative"}

    monkeypatch.setattr("onmymind.respond.respond_turn", _stub)
    counts = poller.poll_once(
        "tok",
        db_path=db_path,
        offset_path=tmp_path / "offset.json",
        audio_dir=tmp_path / "audio",
        roster=ROSTER,
        confirm=True,
    )
    assert counts.get("card_reply") == 1
    assert any("Nothing material changed." in s for s in sent)
    assert len(notes.list_notes(kind="musing", db_path=db_path)) == 1


def test_poll_once_card_reply_unknown_message_id_falls_through(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reply pointing at a message id no live note carries is NOT a card reply
    — it falls through to an ordinary capture (capture is never hijacked)."""
    _seed_card(db_path, "NU keeps compounding", 503)
    monkeypatch.setattr(
        telegram,
        "get_updates",
        lambda token, offset=None, timeout=50: [
            _reply_update(103, "Nubank looks compelling", 999)  # 999 stamped on no note
        ],
    )
    counts = poller.poll_once(
        "tok",
        db_path=db_path,
        offset_path=tmp_path / "offset.json",
        audio_dir=tmp_path / "audio",
        roster=ROSTER,
        confirm=False,
    )
    assert counts.get("card_reply") is None
    assert counts.get("landed") == 1  # the reply became a normal capture
    musings = notes.list_notes(kind="musing", db_path=db_path)
    assert len(musings) == 2 and any("Nubank looks compelling" in m.body for m in musings)


def test_poll_once_card_reply_classifier_failure_degrades(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A classifier failure fails OPEN inside handle_reply (chat mode) — the
    reply is consumed, never re-captured, and never crashes the loop."""
    import onmymind.reply as reply_mod

    _seed_card(db_path, "NU keeps compounding", 504)
    monkeypatch.setenv("LEDGER_ANSWER", "0")  # keep the degrade path off the engine

    def _boom(*a: object, **kw: object) -> object:
        raise RuntimeError("model down")

    monkeypatch.setattr(reply_mod, "classify_reply", _boom)
    monkeypatch.setattr(
        telegram,
        "get_updates",
        lambda token, offset=None, timeout=50: [_reply_update(105, "hmm", 504)],
    )
    sent: list[str] = []
    monkeypatch.setattr(
        telegram, "send_message", lambda token, chat_id, text, **k: sent.append(text)
    )
    counts = poller.poll_once(
        "tok",
        db_path=db_path,
        offset_path=tmp_path / "offset.json",
        audio_dir=tmp_path / "audio",
        roster=ROSTER,
        confirm=True,
    )
    assert counts.get("card_reply") == 1  # consumed, not re-captured
    assert len(notes.list_notes(kind="musing", db_path=db_path)) == 1


def test_poll_once_card_reply_send_failure_never_propagates(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing reply-send is suppressed — the routing already happened."""
    note_id = _seed_card(db_path, "old thought", 506)
    _stub_reply_intent(monkeypatch, "dismiss")
    monkeypatch.setattr(
        telegram,
        "get_updates",
        lambda token, offset=None, timeout=50: [_reply_update(107, "drop this", 506)],
    )

    def _boom_send(*a: object, **k: object) -> object:
        raise telegram.TelegramError("network down")

    monkeypatch.setattr(telegram, "send_message", _boom_send)
    counts = poller.poll_once(  # must not raise
        "tok",
        db_path=db_path,
        offset_path=tmp_path / "offset.json",
        audio_dir=tmp_path / "audio",
        roster=ROSTER,
        confirm=True,
    )
    assert counts.get("card_reply") == 1
    note = notes.get_note(note_id, db_path=db_path)
    assert note is not None and note.status == "archived"  # dismiss acted before the send failed
