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
from llm.contracts import RECENT_DEVELOPMENTS_SCHEMA, RecentDevelopmentsPayload
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
    def malformed_response(_prompt: str, **_kwargs: object) -> str:
        return "not json at all"

    monkeypatch.setattr(structured, "call_llm", malformed_response)
    with pytest.raises(StructuredParseError):
        generate_qa_topics("GOOG", "Q1 2026", _Q)


def test_structured_failure_diagnostics_redact_secrets(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    marker = "sk-" + "redaction-marker-123456789"

    def malformed_response(_prompt: str, **_kwargs: object) -> str:
        return f'not json {{"api_key": "{marker}"}}'

    monkeypatch.setattr(
        structured,
        "call_llm",
        malformed_response,
    )
    with caplog.at_level(logging.WARNING), pytest.raises(StructuredParseError) as exc_info:
        generate_qa_topics("GOOG", "Q1 2026", _Q)
    assert marker not in caplog.text
    assert marker not in exc_info.value.raw_head
    assert "***" in caplog.text


def test_structured_transport_callback_keeps_schema_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "search_status": "events_found",
        "window_start": "2026-06-01",
        "window_end": "2026-06-08",
        "queries_run": ["issuer news"],
        "sources_opened": ["https://example.com/ir"],
        "events": [
            {
                "category": "material_news",
                "tag": "EARNINGS",
                "headline": "Issuer reports results",
                "published_at": "2026-06-06",
                "publisher": "Issuer IR",
                "url": "https://example.com/ir",
                "factual_summary": "Issuer reports results.",
                "thesis_implication": "The report changes the KPI watch.",
                "tone": "neu",
            }
        ],
    }
    calls: list[tuple[str, dict[str, object]]] = []

    def forbidden(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("transport callback must replace only the raw call")

    def transport(prompt: str, **kwargs: object) -> str:
        calls.append((prompt, kwargs))
        return json.dumps(payload)

    monkeypatch.setattr(structured, "call_llm", forbidden)
    result = structured.call_llm_structured(
        "grounded prompt",
        purpose="recent_developments",
        ticker="NU",
        expect="object",
        schema=RECENT_DEVELOPMENTS_SCHEMA,
        transport=transport,
    )
    assert isinstance(result, RecentDevelopmentsPayload)
    assert result.events[0].url == "https://example.com/ir"
    assert calls[0][0] == "grounded prompt"
    assert calls[0][1]["purpose"] == "recent_developments"
    assert calls[0][1]["ticker"] == "NU"
