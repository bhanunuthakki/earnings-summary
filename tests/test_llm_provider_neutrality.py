"""Unit Tests: Provider-Neutral Contracts and Adapter Isolation (BHA-56)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from execution.verify_llm_adapter_isolation import main as run_ast_isolation_check
from llm.envelope import LLMFailureCode, LLMRequestEnvelope, LLMResponseEnvelope


def test_request_envelope_immutability_and_validation() -> None:
    req = LLMRequestEnvelope(
        purpose="peer_selection",
        prompt="Find 5 peers for AAPL",
        temperature=0.2,
        max_tokens=1000,
    )
    assert req.purpose == "peer_selection"
    assert req.temperature == 0.2
    assert req.prompt_version == "v1"

    # Frozen model cannot be mutated
    with pytest.raises(ValidationError):
        req.purpose = "bear_case"  # type: ignore[misc]

    # Extra fields forbidden
    with pytest.raises(ValidationError):
        LLMRequestEnvelope(
            purpose="peer_selection",
            prompt="test",
            unknown_arg="extra",  # type: ignore[call-arg]
        )


def test_response_envelope_telemetry_and_failure_codes() -> None:
    # Success response
    resp = LLMResponseEnvelope(
        purpose="peer_selection",
        content='[{"symbol": "MSFT"}]',
        parsed_payload=[{"symbol": "MSFT"}],
        model="claude-3-7-sonnet",
        provider="anthropic",
        cost_usd=0.0042,
        latency_ms=850.5,
        input_tokens=520,
        output_tokens=85,
    )
    assert resp.is_success is True
    assert resp.failure_code is None
    assert resp.cost_usd == 0.0042

    # Failure response
    err_resp = LLMResponseEnvelope(
        purpose="peer_selection",
        content="",
        model="claude-3-7-sonnet",
        provider="anthropic",
        failure_code=LLMFailureCode.TIMEOUT,
        error_message="Provider timeout after 30s",
    )
    assert err_resp.is_success is False
    assert err_resp.failure_code == LLMFailureCode.TIMEOUT


def test_ast_adapter_isolation_passes() -> None:
    exit_code = run_ast_isolation_check()
    assert exit_code == 0
