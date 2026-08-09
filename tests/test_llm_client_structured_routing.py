"""Track B seam 10: the ad-hoc fence-strip blocks in ``llm_client`` now route
through ``call_llm_structured`` — so a chatty first response is RETRIED (not
silently shipped as malformed JSON), and a double failure is LOUD
(``StructuredParseError``) rather than a quietly-broken string.

Exercised through ``generate_qa_topics`` (the simplest converted function) by
stubbing the shared transport ``llm.structured.call_llm`` that
``call_llm_structured`` calls.
"""

from __future__ import annotations

import json
import logging

import pytest

import llm.structured as structured
from llm.structured import StructuredParseError
from llm_client import generate_qa_topics

_Q = [{"id": "0", "analyst": "x", "question": "How are cloud margins trending?"}]
_VALID = '[{"id": "0", "topic": "Cloud margins", "tag": "CLOUD"}]'


def test_qa_topics_recovers_via_one_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fake(prompt: str, **_kw: object) -> str:
        calls["n"] += 1
        # First response is chatty prose (not JSON) → triggers the retry.
        return "Sure! Here are the topics you asked for." if calls["n"] == 1 else _VALID

    monkeypatch.setattr(structured, "call_llm", fake)
    out = generate_qa_topics("GOOG", "Q1 2026", _Q)
    assert calls["n"] == 2  # retried exactly once
    assert json.loads(out) == [{"id": "0", "topic": "Cloud margins", "tag": "CLOUD"}]


def test_qa_topics_raises_loudly_on_double_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(structured, "call_llm", lambda prompt, **_kw: "not json at all")
    with pytest.raises(StructuredParseError):
        generate_qa_topics("GOOG", "Q1 2026", _Q)


def test_structured_failure_diagnostics_redact_secrets(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "sk-secret-value-123456789"
    monkeypatch.setattr(
        structured,
        "call_llm",
        lambda prompt, **_kw: f'not json {{"api_key": "{secret}"}}',
    )
    with caplog.at_level(logging.WARNING), pytest.raises(StructuredParseError) as exc_info:
        generate_qa_topics("GOOG", "Q1 2026", _Q)
    assert secret not in caplog.text
    assert secret not in exc_info.value.raw_head
    assert "***" in caplog.text
