"""Default structured-call retry and escalation policy."""

from __future__ import annotations

import pytest

from llm import structured
from llm.structured import StructuredParseError


def _stub_structured_calls(
    monkeypatch: pytest.MonkeyPatch, responses: list[str]
) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def fake_call(prompt: str, **kwargs: object) -> str:
        calls.append({"prompt": prompt, **kwargs})
        return responses.pop(0)

    monkeypatch.setattr(structured, "call_llm", fake_call)

    def resolve_lower_tier(**_kwargs: object) -> tuple[str, str]:
        return ("lower-tier-model", "codex")

    monkeypatch.setattr(
        "llm.resolver.resolve_model_and_backend",
        resolve_lower_tier,
    )
    return calls


def test_default_retries_once_without_tier_escalation(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_structured_calls(monkeypatch, ["not json", "still not json"])

    with pytest.raises(StructuredParseError):
        structured.call_llm_structured(
            "Return an object.",
            purpose="structured_default_test",
            required_keys=("ok",),
        )

    assert len(calls) == 2
    assert all(call["model"] is None for call in calls)


def test_explicit_escalation_opt_in_allows_third_higher_tier_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_structured_calls(monkeypatch, ["not json", "still not json", '{"ok": true}'])

    result = structured.call_llm_structured(
        "Return an object.",
        purpose="structured_explicit_escalation_test",
        required_keys=("ok",),
        max_escalation_tier=1,
    )

    assert result == {"ok": True}
    assert len(calls) == 3
    assert calls[2]["model"] is not None
    assert calls[2]["backend"] is None


@pytest.mark.parametrize("first_body", ['[{"quote":"wrong"}]', "not JSON"])
def test_raw_structured_observer_precedes_shared_schema_and_domain_repair_budget(
    monkeypatch: pytest.MonkeyPatch, first_body: str
) -> None:
    from pydantic import TypeAdapter

    responses = [first_body, '[{"quote":"still wrong"}]']
    calls = _stub_structured_calls(monkeypatch, responses)
    observed: list[tuple[str, str]] = []

    def retain(prompt: str, raw: str) -> None:
        observed.append((prompt, raw))

    def reject(_value: object) -> tuple[bool, str]:
        assert len(observed) == len(calls)
        return False, "quote is not in the original source"

    with pytest.raises(StructuredParseError):
        structured.call_llm_structured_with_raw(
            "EXACT ORIGINAL SOURCE",
            purpose="structured_domain_test",
            schema=TypeAdapter(list[dict[str, str]]),
            expect="array",
            domain_guardrail=reject,
            raw_response_observer=retain,
        )
    assert len(calls) == len(observed) == 2
    assert observed[0] == ("EXACT ORIGINAL SOURCE", first_body)
    assert observed[1][1] == '[{"quote":"still wrong"}]'


def test_raw_observer_failure_stops_without_a_repair_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pydantic import TypeAdapter

    calls = _stub_structured_calls(monkeypatch, ["not JSON"])

    def cannot_retain(_prompt: str, _raw: str) -> None:
        raise ValueError("derived raw response owner is unavailable")

    with pytest.raises(ValueError, match="owner is unavailable"):
        structured.call_llm_structured_with_raw(
            "SOURCE",
            purpose="structured_retention_test",
            schema=TypeAdapter(list[dict[str, str]]),
            expect="array",
            raw_response_observer=cannot_retain,
        )
    assert len(calls) == 1
