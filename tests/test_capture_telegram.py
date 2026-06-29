"""The Ledger Telegram client (parsing) + token store."""

from __future__ import annotations

from pathlib import Path

import pytest

from capture import telegram, token_store


def test_parse_text_update() -> None:
    u = telegram.parse_update(
        {
            "update_id": 5,
            "message": {"chat": {"id": 111}, "message_id": 2, "text": "NU looks cheap"},
        }
    )
    assert u.kind == "text"
    assert u.text == "NU looks cheap"
    assert u.chat_id == 111
    assert u.update_id == 5


def test_parse_voice_update() -> None:
    u = telegram.parse_update(
        {
            "update_id": 6,
            "message": {"chat": {"id": 111}, "message_id": 3, "voice": {"file_id": "AbC"}},
        }
    )
    assert u.kind == "voice"
    assert u.voice_file_id == "AbC"


def test_parse_callback_update() -> None:
    u = telegram.parse_update(
        {
            "update_id": 7,
            "callback_query": {
                "id": "q1",
                "data": "research:42:approve",
                "message": {"chat": {"id": 111}, "message_id": 9},
            },
        }
    )
    assert u.kind == "callback"
    assert u.callback_data == "research:42:approve"
    assert u.callback_query_id == "q1"
    assert u.chat_id == 111


def test_parse_other_update() -> None:
    u = telegram.parse_update(
        {"update_id": 8, "message": {"chat": {"id": 1}, "message_id": 4, "photo": [{}]}}
    )
    assert u.kind == "other"


def test_next_offset_cursor() -> None:
    updates = [telegram.Update(5, "text"), telegram.Update(9, "text"), telegram.Update(7, "voice")]
    assert telegram.next_offset(updates) == 10
    assert telegram.next_offset([]) is None


def test_inline_keyboard() -> None:
    kb = telegram.inline_keyboard(
        [[("approve", "research:1:approve"), ("reject", "research:1:reject")]]
    )
    assert kb == {
        "inline_keyboard": [
            [
                {"text": "approve", "callback_data": "research:1:approve"},
                {"text": "reject", "callback_data": "research:1:reject"},
            ]
        ]
    }


def test_get_updates_parses_via_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_request(url: str, timeout: float = 60) -> object:
        return [{"update_id": 3, "message": {"chat": {"id": 1}, "message_id": 2, "text": "hi"}}]

    monkeypatch.setattr(telegram, "_request", _fake_request)
    updates = telegram.get_updates("tok", offset=1)
    assert len(updates) == 1
    assert updates[0].text == "hi"
    assert updates[0].update_id == 3


def test_load_token_reads_stripped(tmp_path: Path) -> None:
    p = tmp_path / "telegram_bot_token"
    p.write_text("123456:ABC-def\n", encoding="utf-8")
    assert token_store.load_token(p) == "123456:ABC-def"


def test_load_token_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(token_store.CaptureSetupError):
        token_store.load_token(tmp_path / "absent")


def test_load_token_empty_raises(tmp_path: Path) -> None:
    p = tmp_path / "empty"
    p.write_text("   \n", encoding="utf-8")
    with pytest.raises(token_store.CaptureSetupError):
        token_store.load_token(p)


def test_load_token_from_json_object(tmp_path: Path) -> None:
    # base file absent; the .json sibling holds {"token": "..."}
    (tmp_path / "telegram_bot_token.json").write_text('{"token": "999:JSON-tok"}', encoding="utf-8")
    assert token_store.load_token(tmp_path / "telegram_bot_token") == "999:JSON-tok"


def test_load_token_bare_string_in_json_file(tmp_path: Path) -> None:
    (tmp_path / "telegram_bot_token.json").write_text("888:bare\n", encoding="utf-8")
    assert token_store.load_token(tmp_path / "telegram_bot_token") == "888:bare"


def test_load_token_json_missing_token_key_raises(tmp_path: Path) -> None:
    (tmp_path / "telegram_bot_token.json").write_text('{"nope": "x"}', encoding="utf-8")
    with pytest.raises(token_store.CaptureSetupError):
        token_store.load_token(tmp_path / "telegram_bot_token")
