"""The Ledger Gmail adapter — pure message parsing (no google libs / network)."""

from __future__ import annotations

import base64

from capture import gmail


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def test_extract_text_simple() -> None:
    msg = {"payload": {"mimeType": "text/plain", "body": {"data": _b64url("NU looks cheap")}}}
    assert gmail.extract_text(msg) == "NU looks cheap"


def test_extract_text_prefers_plain_in_multipart() -> None:
    msg = {
        "payload": {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/html", "body": {"data": _b64url("<p>ignored</p>")}},
                {"mimeType": "text/plain", "body": {"data": _b64url("Nubank credit worry")}},
            ],
        }
    }
    assert gmail.extract_text(msg) == "Nubank credit worry"


def test_extract_text_none_when_no_plain() -> None:
    assert (
        gmail.extract_text({"payload": {"mimeType": "text/html", "body": {"data": _b64url("x")}}})
        == ""
    )
    assert gmail.extract_text({}) == ""


def test_list_audio_parts() -> None:
    msg = {
        "payload": {
            "mimeType": "multipart/mixed",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64url("see attached")}},
                {"mimeType": "audio/ogg", "filename": "memo.oga", "body": {"attachmentId": "A1"}},
            ],
        }
    }
    assert gmail.list_audio_parts(msg) == [("memo.oga", "A1")]
    assert gmail.list_audio_parts({}) == []


def test_label_id() -> None:
    labels = [{"name": "Capture/Inbox", "id": "L1"}, {"name": "Capture/Done", "id": "L2"}]
    assert gmail._label_id(labels, "Capture/Inbox") == "L1"
    assert gmail._label_id(labels, "Capture/Done") == "L2"
    assert gmail._label_id(labels, "Nope") is None


def test_b64url_decode_tolerates_bad_padding() -> None:
    assert gmail._b64url_decode(_b64url("hello world")) == b"hello world"
    assert gmail._b64url_decode("!!!not base64!!!") == b""
