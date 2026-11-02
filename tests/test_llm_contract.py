"""Shared dispatch and call-record isolation after README contract retirement."""

from __future__ import annotations

import asyncio
import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

import llm_call_ledger
from llm import cli, codex_backend, structured
from llm.ledger import capture_call_records, record_llm_call


def _record(
    prompt: str,
    *,
    provider: str,
    raw: str | None = None,
    error: str | None = None,
    fallback: str | None = None,
) -> None:
    record_llm_call(
        started_at=datetime.now(UTC),
        elapsed_ms=5,
        model=f"{provider}-actual-model",
        prompt_sha=hashlib.sha256(prompt.encode()).hexdigest(),
        prompt_chars=len(prompt),
        purpose="prompt_variant_propose",
        ticker=None,
        scope="meta_eval",
        run_id="contract-test",
        response_text=raw,
        error=error,
        prompt=prompt,
        provider=provider,
        transport="subscription_cli",
        retry_count=0,
        fallback_from_provider=fallback,
        fallback_from_transport="subscription_cli" if fallback else None,
        meta={"usage": {"input_tokens": 10, "output_tokens": 2}, "total_cost_usd": 0.01}
        if raw
        else None,
    )


@pytest.fixture(autouse=True)
def _no_transport_or_state(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("unmocked provider transport")

    def no_write(_record: llm_call_ledger.LlmCallRecord, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(structured, "call_llm", forbidden)
    monkeypatch.setattr(codex_backend, "call_codex_llm", forbidden)
    monkeypatch.setattr(cli, "_call_claude", forbidden)
    monkeypatch.setattr(llm_call_ledger, "record_call", no_write)


def test_capture_resets_after_failure_and_nesting() -> None:
    with capture_call_records() as outer:
        _record("outer", provider="openai", raw="one")
        with pytest.raises(RuntimeError, match="failure"), capture_call_records() as inner:
            _record("inner", provider="anthropic", raw="two")
            assert len(inner()) == 1
            raise RuntimeError("failure")
        assert [record.provider for record in outer()] == ["openai"]
    with capture_call_records() as fresh:
        assert fresh() == ()


def test_capture_does_not_leak_across_async_or_thread_contexts() -> None:
    async def child() -> None:
        _record("child", provider="anthropic", raw="two")

    async def run() -> None:
        with capture_call_records() as parent:
            _record("parent", provider="openai", raw="one")
            await asyncio.create_task(child())
            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(_record, "thread", provider="google", raw="three").result()
            assert [record.provider for record in parent()] == ["openai"]

    asyncio.run(run())


def _real_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_effect(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setenv(cli.PRIMARY_SUBSCRIPTION_BACKEND_ENV_VAR, "codex")
    monkeypatch.delenv("LLM_SUBSCRIPTION_FALLBACK_DISABLED", raising=False)
    monkeypatch.setattr(cli, "_enforce_budget_pre_call", no_effect)
    monkeypatch.setattr(cli, "capture_exchange", no_effect)
    monkeypatch.setattr(structured, "call_llm", cli.call_llm)


@pytest.mark.parametrize(
    "error",
    [
        cli.LLMBudgetExceeded("cap"),
        cli.LLMSetupError("setup"),
        ValueError("capability/policy invalid"),
    ],
)
def test_actual_dispatch_does_not_fallback_on_budget_or_configuration(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    _real_dispatch(monkeypatch)
    prompt = "meaning"
    calls: list[str] = []

    def codex(_prompt: str, **_kwargs: object) -> str:
        calls.append("codex")
        raise error

    def claude(_prompt: str, **_kwargs: object) -> str:
        calls.append("claude")
        return '{"answer":42}'

    monkeypatch.setattr(codex_backend, "call_codex_llm", codex)
    monkeypatch.setattr(cli, "_call_claude", claude)
    with pytest.raises(type(error)) as raised:
        cli.call_llm(prompt, purpose="prompt_variant_propose", scope="meta_eval")
    assert calls == ["codex"]
    assert raised.value is error


def test_ledger_write_failure_preserves_observed_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_write(_record: llm_call_ledger.LlmCallRecord) -> None:
        raise RuntimeError("isolated DB unavailable")

    monkeypatch.setattr(llm_call_ledger, "record_call", fail_write)
    with capture_call_records() as captured:
        _record("prompt", provider="openai", raw="value")
        assert len(captured()) == 1


@pytest.mark.parametrize(
    "error",
    [cli.LLMBudgetExceeded("cap"), cli.LLMSetupError("setup"), ValueError("configuration/schema")],
)
def test_web_dispatch_preserves_hard_stops(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    _real_dispatch(monkeypatch)
    calls: list[str] = []

    def codex(_prompt: str, **_kwargs: object) -> str:
        calls.append("codex")
        raise error

    def forbidden_claude_setup() -> None:
        calls.append("claude")
        raise AssertionError("hard stop must not reach Claude")

    monkeypatch.setattr(codex_backend, "call_codex_llm", codex)
    monkeypatch.setattr(cli, "_verify_setup_once", forbidden_claude_setup)
    with pytest.raises(type(error)) as raised:
        cli.call_llm_with_web("question", purpose="recent_developments")
    assert raised.value is error
    assert calls == ["codex"]


@pytest.mark.parametrize(
    ("provider", "model"),
    [("gemini", "gemini-3-flash-preview"), ("openrouter", "deepseek/deepseek-chat")],
)
@pytest.mark.parametrize(
    "error",
    [cli.LLMBudgetExceeded("cap"), cli.LLMSetupError("setup"), ValueError("configuration/schema")],
)
def test_other_provider_dispatch_preserves_hard_stops(
    monkeypatch: pytest.MonkeyPatch, provider: str, model: str, error: Exception
) -> None:
    from llm import gemini_backend, openrouter_backend

    _real_dispatch(monkeypatch)
    calls: list[str] = []

    def primary(_prompt: str, **_kwargs: object) -> str:
        calls.append(provider)
        raise error

    def claude(_prompt: str, **_kwargs: object) -> str:
        calls.append("claude")
        return "would conceal hard stop"

    monkeypatch.setattr(gemini_backend, "call_gemini", primary)
    monkeypatch.setattr(openrouter_backend, "call_openrouter", primary)
    monkeypatch.setattr(cli, "_call_claude", claude)
    with pytest.raises(type(error)) as raised:
        cli.call_llm("question", purpose="prompt_variant_propose", model=model)
    assert raised.value is error
    assert calls == [provider]


@pytest.mark.parametrize(
    ("provider", "model", "attribution"),
    [
        ("gemini", "gemini-3-flash-preview", "google"),
        ("openrouter", "deepseek/deepseek-chat", "openrouter"),
    ],
)
def test_other_provider_operational_failure_still_falls_back(
    monkeypatch: pytest.MonkeyPatch, provider: str, model: str, attribution: str
) -> None:
    from llm import gemini_backend, openrouter_backend

    _real_dispatch(monkeypatch)
    calls: list[str] = []

    def primary(_prompt: str, **_kwargs: object) -> str:
        calls.append(provider)
        raise RuntimeError("provider temporarily unavailable")

    def claude(_prompt: str, **kwargs: object) -> str:
        calls.append("claude")
        assert kwargs["fallback_from_provider"] == attribution
        return "operational fallback"

    monkeypatch.setattr(gemini_backend, "call_gemini", primary)
    monkeypatch.setattr(openrouter_backend, "call_openrouter", primary)
    monkeypatch.setattr(cli, "_call_claude", claude)
    assert (
        cli.call_llm("question", purpose="prompt_variant_propose", model=model)
        == "operational fallback"
    )
    assert calls == [provider, "claude"]


def test_budget_exception_legacy_alias_preserves_catch_contract() -> None:
    error = cli.LLMBudgetExceeded("cap", check={"allowed": False})
    assert isinstance(error, cli.LLMBudgetExceeded)
    assert cli.is_hard_stop(error)
    assert error.check == {"allowed": False}
    # The public alias is retained; diagnostic class spelling is now compliant.
    assert type(error).__name__ == "LLMBudgetExceededError"
