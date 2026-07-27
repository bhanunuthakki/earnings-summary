from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

import execution.compose_senior_partner_brief as sender
from advisor.senior_partner_brief import SeniorPartnerBrief


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

    markup = sender._telegram_reply_markup(_brief(), artifact_id=2104, db_path=tmp_path / "x.db")

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
        sender._telegram_reply_markup(_brief(), artifact_id=2104, db_path=tmp_path / "x.db")
