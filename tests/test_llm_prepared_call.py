"""Prepared calls seal the effective prompt and identify the successful route."""

from __future__ import annotations

import hashlib
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest

import llm_client
from llm import cli, codex_backend, prompt_ab


def _configure(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    def no_op(*args: object, **kwargs: object) -> None:
        return None

    def prompt_override(purpose: str, scope: str, prompt: str) -> str:
        return prompt + " effective"

    monkeypatch.setattr(cli, "_enforce_budget_pre_call", no_op)
    monkeypatch.setattr(cli, "subscription_fallback_disabled", lambda: False)
    monkeypatch.setattr(prompt_ab, "apply_prompt_override", prompt_override)

    def respond(prompt: str, **kwargs: object) -> str:
        seen.append(prompt)
        return "answer:" + prompt

    monkeypatch.setattr(codex_backend, "call_codex_llm", respond)
    return seen


def test_prepared_call_resolves_prompt_once_and_returns_physical_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _configure(monkeypatch)
    prepared = cli.prepare_llm_call(
        "source", purpose="transcript_summary", model="gpt-5.6-terra", backend="codex"
    )
    assert prepared.prompt == "source effective"
    assert seen == []

    def repeated_prompt(*args: object, **kwargs: object) -> str:
        pytest.fail("prompt resolved twice")

    monkeypatch.setattr(prompt_ab, "apply_prompt_override", repeated_prompt)
    result = cli.execute_prepared_call(prepared, ticker="SYNTH")
    assert seen == [prepared.prompt]
    assert result.receipt.prompt_sha256 == hashlib.sha256(prepared.prompt.encode()).hexdigest()
    assert result.receipt.response_sha256 == hashlib.sha256(result.text.encode()).hexdigest()
    assert result.receipt.model == "gpt-5.6-terra"
    assert result.receipt.backend == "codex"
    assert result.receipt.purpose == "transcript_summary"
    assert result.receipt.generation_id


def test_prepared_receipt_records_fallback_not_initial_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)
    # A model-family selected route permits the established subscription fallback.
    prepared = cli.prepare_llm_call("source", purpose="transcript_summary", model="gpt-5.6-terra")

    def fail(*args: object, **kwargs: object) -> str:
        raise RuntimeError("synthetic transport unavailable")

    monkeypatch.setattr(codex_backend, "call_codex_llm", fail)

    monkeypatch.setattr(cli, "_verify_setup_once", lambda: None)
    monkeypatch.setattr(llm_client, "resolved_claude_cli_path", lambda: "synthetic-claude")
    monkeypatch.setattr(cli, "quota_block_active", lambda: None)

    def record_call(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(cli, "record_llm_call", record_call)
    physical_calls: list[object] = []

    def physical(command: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        physical_calls.append(command)
        assert kwargs["input"] == prepared.prompt
        return subprocess.CompletedProcess(
            ["synthetic-claude"],
            0,
            stdout=json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": "fallback answer",
                    "usage": {},
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(cli.subprocess, "run", physical)
    result = cli.execute_prepared_call(prepared)
    assert len(physical_calls) == 1
    assert result.receipt.backend == "claude"
    assert result.receipt.model.startswith("claude-")
    assert result.receipt.model != prepared.model


def test_prepared_call_rejects_changed_fallback_policy_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _configure(monkeypatch)
    prepared = cli.prepare_llm_call(
        "source", purpose="transcript_summary", model="gpt-5.6-terra", backend="codex"
    )
    monkeypatch.setattr(cli, "subscription_fallback_disabled", lambda: True)
    with pytest.raises(ValueError, match="policy changed"):
        cli.execute_prepared_call(prepared)
    assert seen == []


def test_prepared_receipts_are_local_to_concurrent_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)

    def execute(n: int) -> cli.LLMCallResult:
        return cli.execute_prepared_call(
            cli.prepare_llm_call(
                str(n), purpose="transcript_summary", model="gpt-5.6-terra", backend="codex"
            )
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(execute, [1, 2]))
    assert {r.text for r in results} == {"answer:1 effective", "answer:2 effective"}
    assert len({r.receipt.generation_id for r in results}) == 2


def test_prepared_call_rejects_configuration_change_during_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)
    prepared = cli.prepare_llm_call(
        "source", purpose="transcript_summary", model="gpt-5.6-terra", backend="codex"
    )

    def changed(prompt: str, **kwargs: object) -> str:
        monkeypatch.setitem(cli.LLM_MODELS, "transcript_summary", "claude-opus-4-8")
        return "answer"

    monkeypatch.setattr(codex_backend, "call_codex_llm", changed)
    with pytest.raises(ValueError, match="configuration changed"):
        cli.execute_prepared_call(prepared)


def test_explicit_claude_backend_disables_subscription_and_metered_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)
    observed: list[object] = []

    def respond(_prompt: str, **kwargs: object) -> str:
        observed.append(kwargs["allow_codex_fallback"])
        return "answer"

    monkeypatch.setattr(cli, "_call_claude", respond)
    assert (
        cli.call_llm(
            "source", purpose="transcript_summary", model=cli.DEFAULT_MODEL, backend="claude"
        )
        == "answer"
    )
    assert observed == [False]
