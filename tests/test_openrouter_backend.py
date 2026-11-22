# pyright: reportPrivateUsage=false
#
# These tests deliberately reach module-private surface (_openrouter_setup_verified,
# _openrouter_api_key, _provider_routing) — that IS the unit under test.
# Module-scoped directive per the repo's cli.py / gemini_backend.py precedent.
"""Tests for the OpenRouter third backend (src/llm/openrouter_backend.py) and
call_llm's model-family dispatch to it.

Every test monkeypatches requests.post (or the backend entry points) — the suite
never makes a real HTTP call and never spends. Coverage:

  * model resolution (pin -> LLM_MODELS OpenRouter id -> default);
  * backend selection in call_llm (an OpenRouter model id / slash-namespaced id
    routes to the OpenRouter backend; explicit backend= force);
  * failure policy (operational failure degrades to Claude; forced-openrouter
    failures raise; auth/credit + budget hard stops propagate);
  * the request contract (endpoint, bearer auth, provider pinning for model
    identity, usage.include, prompt rides through byte-identical);
  * usage/cost mapping (prefers OpenRouter's REAL charged cost over an estimate);
  * the model-identity guardrail (provider routing config + env overrides);
  * the cost-ladder wiring (OpenRouter family + opt-in cheaper_candidates).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
import requests

from llm import cli as llm_cli
from llm import openrouter_backend
from llm.model_ladder import OPENROUTER, cheaper_candidates, family_of

PROJECT_ROOT = Path(__file__).resolve().parents[1]

_DEEPSEEK = "deepseek/deepseek-chat"


# ---------------------------------------------------------------------------
# Helpers


def _no_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    def _noop(purpose: str | None, *, force_budget_bypass: bool) -> None:
        return None

    monkeypatch.setattr(llm_cli, "_enforce_budget_pre_call", _noop)


def _ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(openrouter_backend, "_openrouter_setup_verified", True)
    monkeypatch.setattr(openrouter_backend, "_openrouter_api_key", "sk-or-test-key")


class _FakeResponse:
    """Stand-in for requests.Response."""

    def __init__(self, *, status_code: int = 200, payload: object = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self) -> object:
        return self._payload


def _ok_payload(
    text: str,
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cost: float | None = None,
) -> dict[str, object]:
    usage: dict[str, object] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }
    if cost is not None:
        usage["cost"] = cost
    return {
        "id": "gen-abc",
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": usage,
    }


def _record_to(rows: list[dict[str, object]]) -> Callable[..., None]:
    def _capture(**kw: object) -> None:
        rows.append(dict(kw))

    return _capture


def _record_discard(**kw: object) -> None:
    return None


def _post_returning(resp: _FakeResponse) -> Callable[..., _FakeResponse]:
    def _post(url: str, **kwargs: object) -> _FakeResponse:
        return resp

    return _post


# ---------------------------------------------------------------------------
# Cost ladder wiring


def test_openrouter_models_are_registered_and_opt_in() -> None:
    """The seed candidates are in the ladder (so dispatch + explicit compare work)
    but are EXCLUDED from the automatic sweep by default (opt-in)."""
    assert family_of(_DEEPSEEK) == OPENROUTER
    assert family_of("qwen/qwen-2.5-72b-instruct") == OPENROUTER
    # Cheaper than Opus, but NOT surfaced unless include_openrouter=True.
    default_cands = cheaper_candidates("claude-opus-4-8")
    assert _DEEPSEEK not in default_cands
    opted_in = cheaper_candidates("claude-opus-4-8", include_openrouter=True)
    assert _DEEPSEEK in opted_in


# ---------------------------------------------------------------------------
# Model resolution


def test_openrouter_model_for(monkeypatch: pytest.MonkeyPatch) -> None:
    # Default when nothing is pinned.
    assert openrouter_backend.openrouter_model_for(None) == (
        openrouter_backend.OPENROUTER_BACKEND_DEFAULT_MODEL
    )
    assert openrouter_backend.openrouter_model_for("not_a_purpose") == (
        openrouter_backend.OPENROUTER_BACKEND_DEFAULT_MODEL
    )
    # An OpenRouter id already pinned in LLM_MODELS is returned verbatim.
    monkeypatch.setitem(llm_cli.LLM_MODELS, "some_purpose", _DEEPSEEK)
    assert openrouter_backend.openrouter_model_for("some_purpose") == _DEEPSEEK
    # An explicit OPENROUTER_MODELS pin beats everything.
    monkeypatch.setitem(openrouter_backend.OPENROUTER_MODELS, "some_purpose", "qwen/qwen-x")
    assert openrouter_backend.openrouter_model_for("some_purpose") == "qwen/qwen-x"


# ---------------------------------------------------------------------------
# Backend selection in call_llm


def test_call_llm_openrouter_model_id_routes_to_openrouter_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(llm_cli.LLM_MODELS, "viewspec_compile", _DEEPSEEK)
    seen: dict[str, object] = {}

    def _fake_or(prompt: str, **kwargs: object) -> str:
        seen.update(kwargs, prompt=prompt)
        return "OR"

    monkeypatch.setattr(openrouter_backend, "call_openrouter", _fake_or)

    def _fail(prompt: str, **kw: object) -> str:
        raise AssertionError("claude must not be called when openrouter succeeds")

    monkeypatch.setattr(llm_cli, "_call_claude", _fail)
    out = llm_cli.call_llm("compile", purpose="viewspec_compile", ticker="NU", run_id="r1")
    assert out == "OR"
    assert seen["prompt"] == "compile"
    assert seen["model"] == _DEEPSEEK
    assert seen["purpose"] == "viewspec_compile"
    assert seen["ticker"] == "NU"


def test_call_llm_explicit_openrouter_model_routes_without_backend_arg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def _fake_or(prompt: str, **kw: object) -> str:
        seen.update(kw)
        return "OR"

    monkeypatch.setattr(openrouter_backend, "call_openrouter", _fake_or)

    def _fail(prompt: str, **kw: object) -> str:
        raise AssertionError("openrouter model id must not route to claude")

    monkeypatch.setattr(llm_cli, "_call_claude", _fail)
    out = llm_cli.call_llm("p", purpose="bear_case", model=_DEEPSEEK)
    assert out == "OR"
    assert seen["model"] == _DEEPSEEK


def test_call_llm_explicit_backend_forces_openrouter(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def _fake_or(prompt: str, **kw: object) -> str:
        seen.update(kw)
        return "OR"

    monkeypatch.setattr(openrouter_backend, "call_openrouter", _fake_or)
    out = llm_cli.call_llm("p", purpose="bear_case", model=_DEEPSEEK, backend="openrouter")
    assert out == "OR"
    assert seen["model"] == _DEEPSEEK


def test_call_llm_unknown_backend_still_raises() -> None:
    with pytest.raises(ValueError, match="Unknown LLM backend"):
        llm_cli.call_llm("p", purpose="bear_case", backend="grok")


def test_openrouter_operational_failure_degrades_to_claude(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pinned OpenRouter purpose must never break the pipeline: a transient
    failure degrades to Claude (re-resolved to a Claude model)."""
    monkeypatch.setitem(llm_cli.LLM_MODELS, "viewspec_compile", _DEEPSEEK)

    def _boom(prompt: str, **kw: object) -> str:
        raise RuntimeError("openrouter 503")

    monkeypatch.setattr(openrouter_backend, "call_openrouter", _boom)
    claude_seen: dict[str, object] = {}

    def _fake_claude(prompt: str, **kw: object) -> str:
        claude_seen.update(kw)
        return "C"

    monkeypatch.setattr(llm_cli, "_call_claude", _fake_claude)
    assert llm_cli.call_llm("p", purpose="viewspec_compile") == "C"
    # The pinned model was OpenRouter, so the Claude fallback re-resolves to DEFAULT.
    assert claude_seen["model"] == llm_cli.DEFAULT_MODEL


def test_forced_openrouter_operational_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(prompt: str, **kw: object) -> str:
        raise RuntimeError("openrouter 503")

    monkeypatch.setattr(openrouter_backend, "call_openrouter", _boom)

    def _fail(prompt: str, **kw: object) -> str:
        raise AssertionError("claude must not run on a forced-openrouter failure")

    monkeypatch.setattr(llm_cli, "_call_claude", _fail)
    with pytest.raises(RuntimeError, match="openrouter 503"):
        llm_cli.call_llm("p", purpose="bear_case", model=_DEEPSEEK, backend="openrouter")


@pytest.mark.parametrize(
    "exc",
    [
        llm_cli.LLMSetupError("openrouter key missing"),
        llm_cli.LLMBudgetExceeded("hard cap"),
    ],
)
def test_openrouter_hard_stops_propagate_without_claude_fallback(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    monkeypatch.setitem(llm_cli.LLM_MODELS, "viewspec_compile", _DEEPSEEK)

    def _boom(prompt: str, **kw: object) -> str:
        raise exc

    monkeypatch.setattr(openrouter_backend, "call_openrouter", _boom)

    def _fail(prompt: str, **kw: object) -> str:
        raise AssertionError("claude must not run on an openrouter hard stop")

    monkeypatch.setattr(llm_cli, "_call_claude", _fail)
    with pytest.raises(type(exc)):
        llm_cli.call_llm("p", purpose="viewspec_compile")
    assert llm_cli.is_hard_stop(exc)


# ---------------------------------------------------------------------------
# The request contract + model-identity guardrail


def test_call_openrouter_request_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """Endpoint, bearer auth, the provider-pinning config (model identity), the
    usage.include flag, and the prompt riding through byte-identical."""
    _no_budget(monkeypatch)
    _ready(monkeypatch)
    monkeypatch.setattr(openrouter_backend, "record_llm_call", _record_discard)
    # Deterministic provider routing (clear any ambient env overrides).
    monkeypatch.delenv("OPENROUTER_PROVIDER_ONLY", raising=False)
    monkeypatch.delenv("OPENROUTER_DATA_COLLECTION", raising=False)
    captured: dict[str, object] = {}

    def _fake_post(url: str, **kwargs: object) -> _FakeResponse:
        captured["url"] = url
        captured.update(kwargs)
        return _FakeResponse(payload=_ok_payload("PONG"))

    monkeypatch.setattr(openrouter_backend.requests, "post", _fake_post)
    out = openrouter_backend.call_openrouter(
        "ping prompt", model=_DEEPSEEK, purpose="viewspec_compile"
    )
    assert out == "PONG"
    assert captured["url"] == openrouter_backend._OPENROUTER_ENDPOINT
    headers = cast("dict[str, str]", captured["headers"])
    assert headers["Authorization"] == "Bearer sk-or-test-key"
    body = cast("dict[str, object]", json.loads(cast("str", captured["data"])))
    assert body["model"] == _DEEPSEEK
    messages = cast("list[dict[str, object]]", body["messages"])
    assert messages == [{"role": "user", "content": "ping prompt"}]  # byte-identical prompt
    provider = cast("dict[str, object]", body["provider"])
    assert provider["allow_fallbacks"] is False  # model-identity: no silent reroute
    assert provider["data_collection"] == "deny"  # governance default
    assert "quantizations" in provider  # precision floor pins identity
    usage_req = cast("dict[str, object]", body["usage"])
    assert usage_req["include"] is True  # ask for real cost
    assert captured["timeout"] == openrouter_backend.OPENROUTER_BACKEND_TIMEOUT_SECONDS


def test_provider_only_env_pins_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    """OPENROUTER_PROVIDER_ONLY hard-pins the upstream provider for a rigorous
    graded eval (the strongest model-identity lever)."""
    monkeypatch.setenv("OPENROUTER_PROVIDER_ONLY", "DeepInfra, Fireworks")
    routing = openrouter_backend._provider_routing()
    assert routing["only"] == ["DeepInfra", "Fireworks"]
    assert routing["allow_fallbacks"] is False


def test_data_collection_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_DATA_COLLECTION", "allow")
    routing = openrouter_backend._provider_routing()
    assert routing["data_collection"] == "allow"


# ---------------------------------------------------------------------------
# Usage / cost mapping + ledger


def test_call_openrouter_records_real_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    """When OpenRouter returns a real charged cost, the ledger uses it verbatim
    (more accurate than the ladder estimate)."""
    _no_budget(monkeypatch)
    _ready(monkeypatch)
    rows: list[dict[str, object]] = []
    monkeypatch.setattr(openrouter_backend, "record_llm_call", _record_to(rows))
    payload = _ok_payload("answer", prompt_tokens=1000, completion_tokens=200, cost=0.00042)
    monkeypatch.setattr(
        openrouter_backend.requests, "post", _post_returning(_FakeResponse(payload=payload))
    )
    out = openrouter_backend.call_openrouter(
        "p", model=_DEEPSEEK, purpose="bear_case", ticker="NU", run_id="r9"
    )
    assert out == "answer"
    assert len(rows) == 1
    row = rows[0]
    assert row["model"] == _DEEPSEEK
    assert row["ticker"] == "NU"
    assert row["run_id"] == "r9"
    meta = cast("dict[str, object]", row["meta"])
    assert meta["total_cost_usd"] == pytest.approx(0.00042)  # OpenRouter's real cost, verbatim
    usage = cast("dict[str, object]", meta["usage"])
    assert usage["input_tokens"] == 1000
    assert usage["output_tokens"] == 200


def test_usage_meta_falls_back_to_estimate_when_no_cost() -> None:
    """No cost in the response -> fall back to the ladder estimate (non-zero for
    a ranked model)."""
    from llm.model_ladder import estimated_call_usd

    meta = openrouter_backend.usage_meta_from_openrouter(
        {"prompt_tokens": 1000, "completion_tokens": 200}, model=_DEEPSEEK
    )
    expected = estimated_call_usd(_DEEPSEEK, 1000, 200)
    assert meta["total_cost_usd"] == pytest.approx(expected)
    assert expected > 0.0


def test_usage_meta_tolerates_junk() -> None:
    for payload in (None, {}, {"prompt_tokens": "NaN", "completion_tokens": True, "cost": "free"}):
        meta = openrouter_backend.usage_meta_from_openrouter(
            cast("dict[str, object] | None", payload), model=_DEEPSEEK
        )
        usage = cast("dict[str, object]", meta["usage"])
        assert usage["input_tokens"] == 0
        assert usage["output_tokens"] == 0


# ---------------------------------------------------------------------------
# Failure classification


def test_auth_status_classifies_as_setup_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """401/402/403 are deterministic operator problems -> LLMSetupError (hard stop)."""
    _no_budget(monkeypatch)
    _ready(monkeypatch)
    monkeypatch.setattr(openrouter_backend, "record_llm_call", _record_discard)
    monkeypatch.setattr(
        openrouter_backend.requests,
        "post",
        _post_returning(_FakeResponse(status_code=401, text="invalid api key")),
    )
    with pytest.raises(llm_cli.LLMSetupError, match="OPENROUTER_API_KEY"):
        openrouter_backend.call_openrouter("p", model=_DEEPSEEK, purpose="bear_case")


def test_out_of_credits_402_classifies_as_setup_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_budget(monkeypatch)
    _ready(monkeypatch)
    monkeypatch.setattr(openrouter_backend, "record_llm_call", _record_discard)
    monkeypatch.setattr(
        openrouter_backend.requests,
        "post",
        _post_returning(_FakeResponse(status_code=402, text="insufficient credits")),
    )
    with pytest.raises(llm_cli.LLMSetupError):
        openrouter_backend.call_openrouter("p", model=_DEEPSEEK, purpose="bear_case")


def test_rate_limit_429_stays_operational(monkeypatch: pytest.MonkeyPatch) -> None:
    """429 is transient — must stay operational (RuntimeError), NOT a hard stop,
    so a pinned purpose degrades to Claude rather than crashing the pipeline."""
    _no_budget(monkeypatch)
    _ready(monkeypatch)
    rows: list[dict[str, object]] = []
    monkeypatch.setattr(openrouter_backend, "record_llm_call", _record_to(rows))
    monkeypatch.setattr(
        openrouter_backend.requests,
        "post",
        _post_returning(_FakeResponse(status_code=429, text="rate limited")),
    )
    with pytest.raises(RuntimeError):
        openrouter_backend.call_openrouter("p", model=_DEEPSEEK, purpose="bear_case")
    assert len(rows) == 1  # the failed attempt still gets its ledger row
    assert not llm_cli.is_hard_stop(RuntimeError("x"))


def test_error_envelope_in_200_is_operational(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenRouter can report an error inside a 200 body — treat as operational."""
    _no_budget(monkeypatch)
    _ready(monkeypatch)
    monkeypatch.setattr(openrouter_backend, "record_llm_call", _record_discard)
    payload: dict[str, object] = {"error": {"message": "model unavailable", "code": 503}}
    monkeypatch.setattr(
        openrouter_backend.requests, "post", _post_returning(_FakeResponse(payload=payload))
    )
    with pytest.raises(ValueError, match="error envelope"):
        openrouter_backend.call_openrouter("p", model=_DEEPSEEK, purpose="bear_case")


def test_empty_content_is_operational(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_budget(monkeypatch)
    _ready(monkeypatch)
    monkeypatch.setattr(openrouter_backend, "record_llm_call", _record_discard)
    monkeypatch.setattr(
        openrouter_backend.requests,
        "post",
        _post_returning(_FakeResponse(payload=_ok_payload("  "))),
    )
    with pytest.raises(RuntimeError, match="empty content"):
        openrouter_backend.call_openrouter("p", model=_DEEPSEEK, purpose="bear_case")


def test_network_error_is_operational_and_ledgered(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_budget(monkeypatch)
    _ready(monkeypatch)
    rows: list[dict[str, object]] = []
    monkeypatch.setattr(openrouter_backend, "record_llm_call", _record_to(rows))

    def _boom(url: str, **kwargs: object) -> _FakeResponse:
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(openrouter_backend.requests, "post", _boom)
    with pytest.raises(requests.RequestException):
        openrouter_backend.call_openrouter("p", model=_DEEPSEEK, purpose="bear_case")
    assert len(rows) == 1
    assert "ConnectionError" in cast("str", rows[0]["error"])


def test_missing_api_key_raises_setup_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_budget(monkeypatch)
    monkeypatch.setattr(openrouter_backend, "_openrouter_setup_verified", False)
    monkeypatch.setattr(openrouter_backend, "_openrouter_api_key", None)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(llm_cli.LLMSetupError, match="OpenRouter API key"):
        openrouter_backend.call_openrouter("p", model=_DEEPSEEK, purpose="bear_case")


def test_budget_hard_block_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    def _block(purpose: str | None, *, force_budget_bypass: bool) -> None:
        raise llm_cli.LLMBudgetExceeded(f"{purpose}: cap exceeded")

    monkeypatch.setattr(llm_cli, "_enforce_budget_pre_call", _block)

    def _fail(url: str, **kwargs: object) -> _FakeResponse:
        raise AssertionError("no HTTP past a hard budget block")

    monkeypatch.setattr(openrouter_backend.requests, "post", _fail)
    with pytest.raises(llm_cli.LLMBudgetExceeded):
        openrouter_backend.call_openrouter("p", model=_DEEPSEEK, purpose="bear_case")


# ---------------------------------------------------------------------------
# Package layout


def test_llm_package_reexports_openrouter_backend() -> None:
    import llm

    assert llm.call_openrouter is openrouter_backend.call_openrouter
    assert llm.openrouter_model_for is openrouter_backend.openrouter_model_for
    assert (
        llm.OPENROUTER_BACKEND_ALLOWED_PURPOSES
        is openrouter_backend.OPENROUTER_BACKEND_ALLOWED_PURPOSES
    )
    assert "/" in llm.OPENROUTER_BACKEND_DEFAULT_MODEL  # provider/model slug
