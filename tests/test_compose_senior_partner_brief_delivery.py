from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

import execution.compose_senior_partner_brief as sender
from advisor.senior_partner_brief import BriefResult, SeniorPartnerBrief
from capture import telegram


def _brief() -> SeniorPartnerBrief:
    return SeniorPartnerBrief(as_of="x", iso_year=2026, iso_week=30, input_sha="sha")


def test_sender_keyboard_always_contains_absolute_mobile_inbox_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        sender,
        "private_mobile_inbox_url",
        lambda: "https://desktop.example.ts.net/mobile/inbox",
    )

    markup = sender.build_delivery_keyboard(_brief(), artifact_id=2104, db_path=tmp_path / "x.db")

    rows = cast("list[list[dict[str, object]]]", markup["inline_keyboard"])
    buttons = [button for row in rows for button in row]
    assert {
        "text": "Review in Inbox",
        "url": "https://desktop.example.ts.net/mobile/inbox",
    } in buttons
    assert not any(
        str(button.get("callback_data", "")).startswith("spb:review") for button in buttons
    )


def test_sender_refuses_telegram_delivery_without_private_mobile_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sender, "private_mobile_inbox_url", lambda: None)

    with pytest.raises(RuntimeError, match="refusing Telegram delivery"):
        sender.build_delivery_keyboard(_brief(), artifact_id=2104, db_path=tmp_path / "x.db")


def test_sender_exits_nonzero_when_telegram_delivery_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "portfolio.db"
    db_path.touch()
    result = BriefResult(
        brief=_brief(),
        artifact_id=2104,
        cache_hit=False,
        selection_mode="llm",
    )
    recorded_statuses: list[str] = []

    def fake_compose(_db_path: Path, _repo_root: Path) -> BriefResult:
        return result

    def never_delivered(_db_path: Path, *, iso_year: int, iso_week: int) -> bool:
        return False

    def fake_load_chat_id(_path: Path) -> int:
        return 5

    def fake_reply_markup(
        _brief: SeniorPartnerBrief,
        *,
        artifact_id: int | None,
        db_path: Path,
    ) -> dict[str, object]:
        return {}

    def record_status(
        _db_path: Path,
        *,
        iso_year: int,
        iso_week: int,
        status: str,
        headline: str,
        artifact_id: int | None,
    ) -> None:
        recorded_statuses.append(status)

    monkeypatch.setattr(sender, "compose_brief", fake_compose)
    monkeypatch.setattr(sender, "_already_delivered_this_week", never_delivered)
    monkeypatch.setattr(sender.token_store, "load_token", lambda: "token")
    monkeypatch.setattr(sender.token_store, "load_chat_id", fake_load_chat_id)
    monkeypatch.setattr(sender, "build_delivery_keyboard", fake_reply_markup)
    monkeypatch.setattr(sender, "_record_standup_message", record_status)

    def fail_send(*_args: object, **_kwargs: object) -> None:
        raise telegram.TelegramError("temporary delivery failure")

    monkeypatch.setattr(telegram, "send_message", fail_send)

    exit_code = sender.main(["--repo-root", str(tmp_path), "--db-path", str(db_path)])

    assert exit_code == 1
    assert recorded_statuses == ["compose_failed"]
