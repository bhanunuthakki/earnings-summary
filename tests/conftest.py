"""Shared pytest fixtures for the earnings-summary test suite."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_real_chat_llm_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite never spends: any test that reaches the claude-CLI chat
    transport unpatched fails loudly instead of launching a real subprocess.
    (The ask engine's narrative route makes this reachable from plain
    endpoint tests — e.g. an unrecognized query falls through to narrative.)
    Tests that exercise these paths monkeypatch the seams themselves."""
    import chat_session

    def _blocked(*_a: object, **_k: object) -> object:
        raise AssertionError(
            "real chat-LLM transport invoked in a test — monkeypatch "
            "chat_session.stream_llm_text or "
            "chat_session.build_chat_response.stream_response"
        )

    monkeypatch.setattr(chat_session, "stream_llm_text", _blocked)
    monkeypatch.setattr(chat_session.build_chat_response, "stream_response", _blocked)
