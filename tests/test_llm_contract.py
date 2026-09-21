"""Production neutral-contract seam, exercised without provider calls."""

from __future__ import annotations

import asyncio
import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

import llm_call_ledger
from llm import cli, codex_backend, structured
from llm.contract import call_llm_structured_enveloped, schema_version
from llm.envelope import LLMCapability, LLMFailureCode, LLMRequestEnvelope, LLMResponseEnvelope
from llm.ledger import capture_call_records, record_llm_call
from llm.prompt_registry import PromptTemplate, RenderedPrompt
from llm.resolver import CapabilityProfile


def test_unknown_purpose_rejected_before_dispatch() -> None:
    with pytest.raises(ValidationError, match="unknown LLM purpose"):
        LLMRequestEnvelope(
            purpose="typo_purpose", prompt="hello", prompt_version="abc", schema_version="def"
        )


def test_versions_are_required_and_nonblank() -> None:
    with pytest.raises(ValidationError):
        LLMRequestEnvelope.model_validate({"purpose": "readme_update", "prompt": "hello"})
    with pytest.raises(ValidationError):
        LLMRequestEnvelope(
            purpose="readme_update", prompt="hello", prompt_version=" ", schema_version="def"
        )


def test_unknown_capability_rejected() -> None:
    with pytest.raises(ValidationError):
        LLMRequestEnvelope.model_validate(
            {
                "purpose": "readme_update",
                "prompt": "hello",
                "prompt_version": "abc",
                "schema_version": "def",
                "capabilities_required": ["magic"],
            }
        )


def test_exact_prompt_bytes_and_unknown_usage_preserved() -> None:
    request = LLMRequestEnvelope(
        purpose="readme_update", prompt="\nhello\n", prompt_version="abc", schema_version="def"
    )
    assert request.prompt == "\nhello\n"
    response = LLMResponseEnvelope(
        purpose="readme_update", content="hello", model="model", provider="provider"
    )
    assert response.cost_usd is None
    assert response.input_tokens is None


class Reply(BaseModel):
    answer: int


_SCHEMA = TypeAdapter(Reply)
_TEMPLATE = PromptTemplate(
    template_id="contract.test", body="\nQuestion {question}\n", variables=("question",)
)


def _request(prompt: RenderedPrompt) -> LLMRequestEnvelope:
    return LLMRequestEnvelope(
        purpose="readme_update",
        prompt=prompt,
        prompt_version=prompt.template_version,
        schema_version=schema_version(_SCHEMA),
        trace_id="contract-test",
        capabilities_required=(LLMCapability.STRUCTURED_OUTPUT,),
        source_evidence_sha256=("a" * 64,),
    )


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
        purpose="readme_update",
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


def test_success_uses_real_ledger_identity_and_preserves_exact_exchange(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = _TEMPLATE.render(question="meaning")
    raw = '  {"answer": 42}\n'

    def transport(actual_prompt: str, **kwargs: object) -> str:
        assert actual_prompt is prompt
        assert kwargs["capability_profile"] == CapabilityProfile(requires_structured_output=True)
        _record(actual_prompt, provider="anthropic", raw=raw)
        return raw

    monkeypatch.setattr(structured, "call_llm", transport)
    request = _request(prompt)
    result = call_llm_structured_enveloped(
        request,
        prompt=prompt,
        schema=_SCHEMA,
        repair_prompt=lambda _error: prompt,
        scope="meta_eval",
    )
    exchange = result.require_exchange()
    assert exchange.prompt is prompt
    assert exchange.raw_response == raw
    assert exchange.value.answer == 42
    assert result.response.provider == "anthropic"
    assert result.response.model == "anthropic-actual-model"
    assert result.response.prompt_version == prompt.template_version
    assert result.response.schema_version == schema_version(_SCHEMA)
    assert result.response.source_evidence_sha256 == ("a" * 64,)
    assert result.response.input_tokens == 10
    assert result.response.cost_usd == 0.01
    assert result.response.retry_count == 0
    assert result.response.effective_prompt_verified is True
    assert result.response.prompt_sha256 == hashlib.sha256(prompt.encode()).hexdigest()
    assert result.response.repair_count == 0
    assert result.response.latency_ms is not None and result.response.latency_ms >= 0
    assert result.response.attempts[0].response_sha256 == hashlib.sha256(raw.encode()).hexdigest()
    assert (
        LLMResponseEnvelope.model_validate_json(result.response.model_dump_json())
        == result.response
    )


@pytest.mark.parametrize(
    ("bad", "code"),
    [
        ("not-json", LLMFailureCode.MALFORMED_OUTPUT),
        ('{"answer": "wrong"}', LLMFailureCode.SCHEMA_VALIDATION_ERROR),
    ],
)
def test_malformed_output_repairs_once_then_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, bad: str, code: LLMFailureCode
) -> None:
    prompt = _TEMPLATE.render(question="meaning")
    calls: list[str] = []

    def transport(actual_prompt: str, **_kwargs: object) -> str:
        calls.append(actual_prompt)
        _record(actual_prompt, provider="openai", raw=bad)
        return bad

    monkeypatch.setattr(structured, "call_llm", transport)
    result = call_llm_structured_enveloped(
        _request(prompt),
        prompt=prompt,
        schema=_SCHEMA,
        repair_prompt=lambda _error: prompt,
        scope="meta_eval",
    )
    assert len(calls) == 2
    assert result.response.failure_code == code
    assert result.response.repair_count == 1
    assert len(result.response.attempts) == 2
    assert not result.response.is_success
    with pytest.raises(structured.StructuredParseError):
        result.require_exchange()


def test_repair_success_preserves_version_and_total_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    prompt = _TEMPLATE.render(question="meaning")
    replies = iter(['{"answer": "bad"}', '{"answer": 42}'])

    def transport(actual_prompt: str, **_kwargs: object) -> str:
        raw = next(replies)
        _record(actual_prompt, provider="openai", raw=raw)
        return raw

    monkeypatch.setattr(structured, "call_llm", transport)
    result = call_llm_structured_enveloped(
        _request(prompt),
        prompt=prompt,
        schema=_SCHEMA,
        repair_prompt=lambda _error: prompt,
        scope="meta_eval",
    )
    assert result.require_exchange().value.answer == 42
    assert result.response.repair_count == 1
    assert result.response.input_tokens == 20
    assert result.response.cost_usd == 0.02


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (RuntimeError("offline"), LLMFailureCode.UNAVAILABLE),
        (cli.LLMBudgetExceeded("cap"), LLMFailureCode.BUDGET_EXCEEDED),
        (cli.LLMSetupError("configuration"), LLMFailureCode.CONFIGURATION_ERROR),
        (ValueError("invalid policy"), LLMFailureCode.CONFIGURATION_ERROR),
        (PermissionError("denied"), LLMFailureCode.AUTH_ERROR),
        (TimeoutError("deadline"), LLMFailureCode.TIMEOUT),
    ],
)
def test_failures_preserve_original_exception_without_repair(
    monkeypatch: pytest.MonkeyPatch, error: Exception, code: LLMFailureCode
) -> None:
    prompt = _TEMPLATE.render(question="meaning")
    calls = 0

    def transport(_prompt: str, **_kwargs: object) -> str:
        nonlocal calls
        calls += 1
        raise error

    monkeypatch.setattr(structured, "call_llm", transport)
    result = call_llm_structured_enveloped(
        _request(prompt),
        prompt=prompt,
        schema=_SCHEMA,
        repair_prompt=lambda _error: pytest.fail("must not repair transport/configuration"),
        scope="meta_eval",
    )
    assert calls == 1
    assert result.response.failure_code == code
    assert result.response.provider is None
    assert result.response.cost_usd is None
    with pytest.raises(type(error)) as raised:
        result.require_exchange()
    assert raised.value is error


@pytest.mark.parametrize(
    "capability", [LLMCapability.WEB, LLMCapability.VISION, LLMCapability.REALTIME]
)
def test_unsupported_capabilities_stop_before_dispatch(capability: LLMCapability) -> None:
    prompt = _TEMPLATE.render(question="meaning")
    request = _request(prompt).model_copy(update={"capabilities_required": (capability,)})
    with pytest.raises(ValueError, match="supports only"):
        call_llm_structured_enveloped(
            request,
            prompt=prompt,
            schema=_SCHEMA,
            repair_prompt=lambda _error: prompt,
            scope="meta_eval",
        )


@pytest.mark.parametrize("field", ["prompt", "prompt_version", "schema_version"])
def test_contract_identity_mismatch_stops_before_dispatch(field: str) -> None:
    prompt = _TEMPLATE.render(question="meaning")
    request = _request(prompt).model_copy(update={field: "different"})
    with pytest.raises(ValueError, match=r"identity|version"):
        call_llm_structured_enveloped(
            request,
            prompt=prompt,
            schema=_SCHEMA,
            repair_prompt=lambda _error: prompt,
            scope="meta_eval",
        )


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


def test_missing_telemetry_never_infers_model_or_zero_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = _TEMPLATE.render(question="meaning")

    def transport(*_args: object, **_kwargs: object) -> str:
        return '{"answer": 42}'

    monkeypatch.setattr(structured, "call_llm", transport)
    result = call_llm_structured_enveloped(
        _request(prompt),
        prompt=prompt,
        schema=_SCHEMA,
        repair_prompt=lambda _error: prompt,
        scope="meta_eval",
    )
    assert result.require_exchange().value.answer == 42
    assert result.response.model is None
    assert result.response.provider is None
    assert result.response.input_tokens is None
    assert result.response.cost_usd is None


def _real_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_effect(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setenv(cli.PRIMARY_SUBSCRIPTION_BACKEND_ENV_VAR, "codex")
    monkeypatch.delenv("LLM_SUBSCRIPTION_FALLBACK_DISABLED", raising=False)
    monkeypatch.setattr(cli, "_enforce_budget_pre_call", no_effect)
    monkeypatch.setattr(cli, "capture_exchange", no_effect)
    monkeypatch.setattr(structured, "call_llm", cli.call_llm)


def test_existing_dispatch_fallback_is_attributed_not_reimplemented(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _real_dispatch(monkeypatch)
    prompt = _TEMPLATE.render(question="meaning")
    calls: list[str] = []

    def codex(actual_prompt: str, **_kwargs: object) -> str:
        calls.append("codex")
        _record(actual_prompt, provider="openai", error="offline")
        raise RuntimeError("offline")

    def claude(actual_prompt: str, **kwargs: object) -> str:
        calls.append("claude")
        assert kwargs["fallback_from_provider"] == "openai"
        assert kwargs["allow_codex_fallback"] is False
        _record(actual_prompt, provider="anthropic", raw='{"answer":42}', fallback="openai")
        return '{"answer":42}'

    monkeypatch.setattr(codex_backend, "call_codex_llm", codex)
    monkeypatch.setattr(cli, "_call_claude", claude)
    result = call_llm_structured_enveloped(
        _request(prompt),
        prompt=prompt,
        schema=_SCHEMA,
        repair_prompt=lambda _error: prompt,
        scope="meta_eval",
    )
    assert result.require_exchange().value.answer == 42
    assert calls == ["codex", "claude"]
    assert [attempt.provider for attempt in result.response.attempts] == ["openai", "anthropic"]
    assert result.response.provider == "anthropic"
    assert result.response.attempts[-1].fallback_from_provider == "openai"
    assert result.response.cost_usd is None  # failed primary did not report usage


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
    prompt = _TEMPLATE.render(question="meaning")
    calls: list[str] = []

    def codex(_prompt: str, **_kwargs: object) -> str:
        calls.append("codex")
        raise error

    def claude(_prompt: str, **_kwargs: object) -> str:
        calls.append("claude")
        return '{"answer":42}'

    monkeypatch.setattr(codex_backend, "call_codex_llm", codex)
    monkeypatch.setattr(cli, "_call_claude", claude)
    result = call_llm_structured_enveloped(
        _request(prompt),
        prompt=prompt,
        schema=_SCHEMA,
        repair_prompt=lambda _error: prompt,
        scope="meta_eval",
    )
    assert calls == ["codex"]
    with pytest.raises(type(error)) as raised:
        result.require_exchange()
    assert raised.value is error


@pytest.mark.parametrize(
    "error", [cli.LLMBudgetExceeded("cap"), cli.LLMSetupError("setup"), ValueError("configuration")]
)
def test_hard_stop_after_operational_fallback_retains_identity(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    _real_dispatch(monkeypatch)
    prompt = _TEMPLATE.render(question="meaning")

    def codex(_prompt: str, **_kwargs: object) -> str:
        raise RuntimeError("offline")

    def claude(_prompt: str, **_kwargs: object) -> str:
        raise error

    monkeypatch.setattr(codex_backend, "call_codex_llm", codex)
    monkeypatch.setattr(cli, "_call_claude", claude)
    result = call_llm_structured_enveloped(
        _request(prompt),
        prompt=prompt,
        schema=_SCHEMA,
        repair_prompt=lambda _error: prompt,
        scope="meta_eval",
    )
    with pytest.raises(type(error)) as raised:
        result.require_exchange()
    assert raised.value is error


def test_source_binding_changes_request_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    prompt = _TEMPLATE.render(question="meaning")

    def transport(*_args: object, **_kwargs: object) -> str:
        return '{"answer": 42}'

    monkeypatch.setattr(structured, "call_llm", transport)
    first = _request(prompt)
    second = first.model_copy(update={"source_evidence_sha256": ("b" * 64,)})
    responses = [
        call_llm_structured_enveloped(
            request,
            prompt=prompt,
            schema=_SCHEMA,
            repair_prompt=lambda _error: prompt,
            scope="meta_eval",
        ).response
        for request in (first, second)
    ]
    assert responses[0].request_sha256 != responses[1].request_sha256
    assert responses[1].source_evidence_sha256 == ("b" * 64,)


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
        cli.call_llm("question", purpose="readme_update", model=model)
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
    assert cli.call_llm("question", purpose="readme_update", model=model) == "operational fallback"
    assert calls == [provider, "claude"]


def test_budget_exception_legacy_alias_preserves_catch_contract() -> None:
    error = cli.LLMBudgetExceeded("cap", check={"allowed": False})
    assert isinstance(error, cli.LLMBudgetExceeded)
    assert cli.is_hard_stop(error)
    assert error.check == {"allowed": False}
    # The public alias is retained; diagnostic class spelling is now compliant.
    assert type(error).__name__ == "LLMBudgetExceededError"


@pytest.mark.parametrize("scope", [None, "portfolio"])
def test_non_evaluation_scope_rejected_before_actual_override_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    scope: str | None,
) -> None:
    from llm import prompt_ab

    _real_dispatch(monkeypatch)
    prompt = _TEMPLATE.render(question="original evidence")
    calls: list[str] = []

    def override(_purpose: str, _scope: str | None, _prompt: str) -> RenderedPrompt:
        calls.append("override")
        return _TEMPLATE.render(question="different evidence")

    def codex(actual_prompt: str, **_kwargs: object) -> str:
        calls.append("codex")
        _record(actual_prompt, provider="openai", raw='{"answer":42}')
        return '{"answer":42}'

    monkeypatch.setattr(prompt_ab, "apply_prompt_override", override)
    monkeypatch.setattr(codex_backend, "call_codex_llm", codex)
    with pytest.raises(ValueError, match="meta_eval"):
        call_llm_structured_enveloped(
            _request(prompt),
            prompt=prompt,
            schema=_SCHEMA,
            repair_prompt=lambda _error: prompt,
            scope=scope,
        )
    assert calls == []


@pytest.mark.parametrize("mismatch", ["bytes", "version"])
def test_actual_dispatch_observed_prompt_mismatch_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    from llm import prompt_ab

    _real_dispatch(monkeypatch)
    prompt = _TEMPLATE.render(question="original evidence")
    changed = (
        _TEMPLATE.render(question="different evidence")
        if mismatch == "bytes"
        else RenderedPrompt(
            str(prompt),
            template_id=prompt.template_id,
            template_version="different-version",
            vars_sha256=prompt.vars_sha256,
        )
    )

    def override(_purpose: str, _scope: str | None, _prompt: str) -> RenderedPrompt:
        # Even if a future hook breaks meta_eval isolation, observed drift fails.
        return changed

    def codex(actual_prompt: str, **_kwargs: object) -> str:
        _record(actual_prompt, provider="openai", raw='{"answer":42}')
        return '{"answer":42}'

    monkeypatch.setattr(prompt_ab, "apply_prompt_override", override)
    monkeypatch.setattr(codex_backend, "call_codex_llm", codex)
    result = call_llm_structured_enveloped(
        _request(prompt),
        prompt=prompt,
        schema=_SCHEMA,
        repair_prompt=lambda _error: prompt,
        scope="meta_eval",
    )
    assert not result.response.is_success
    assert result.response.failure_code == LLMFailureCode.CONFIGURATION_ERROR
    assert result.response.prompt_sha256 is None
    with pytest.raises(ValueError, match="prompt identity"):
        result.require_exchange()


def test_missing_telemetry_does_not_attest_effective_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = _TEMPLATE.render(question="meaning")

    def transport(*_args: object, **_kwargs: object) -> str:
        return '{"answer":42}'

    monkeypatch.setattr(structured, "call_llm", transport)
    result = call_llm_structured_enveloped(
        _request(prompt),
        prompt=prompt,
        schema=_SCHEMA,
        repair_prompt=lambda _error: prompt,
        scope="meta_eval",
    )
    assert result.require_exchange().value.answer == 42
    assert result.response.prompt_sha256 is None
    assert result.response.attestation().effective_prompt_verified is False
