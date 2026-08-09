# pyright: reportPrivateUsage=false
#
# These tests deliberately reach module-private surface (_gemini_setup_verified,
# _gemini_api_key, compare_backends._smoke_prompts) — that IS the unit under
# test. Module-scoped directive per the repo's cli.py precedent.
"""Tests for the Gemini second backend (src/llm/gemini_backend.py) and
call_llm's model-family-based backend-selection logic.

Every test monkeypatches genai.Client (or the backend entry points) —
the suite never spends real API $. Coverage:

  * legacy allowlist symbols (GEMINI_BACKEND_ALLOWED_PURPOSES, env-var merge)
    retained for backward-compat; call_llm no longer consults them;
  * backend selection in call_llm (model-family dispatch: Gemini model id →
    Gemini backend, Claude model id → Claude backend; explicit backend= force);
  * failure policy (Gemini operational failure degrades to Claude;
    forced-gemini failures raise; hard stops always propagate);
  * the API-key contract (genai.Client + client.models.generate_content with the
    resolved model, prompt, and timeout; missing/rejected key → LLMSetupError);
  * usage/cost mapping from usage_metadata into the ledger row;
  * the NotFound self-anneal retry (preview alias 404s → live catalog lookup
    → stable-fallback retry).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar, cast

import pytest
from google.genai import errors as genai_errors

from llm import cli as llm_cli
from llm import gemini_backend

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _bind_genai() -> None:
    """The backend imports the Gemini SDK lazily (``_ensure_genai``); bind
    ``gemini_backend.genai`` up front so the monkeypatches below have a real
    module attribute to patch."""
    gemini_backend._ensure_genai()


# ---------------------------------------------------------------------------
# Helpers


def _no_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize the shared budget gate (single seam used by BOTH backends)."""

    def _noop(purpose: str | None, *, force_budget_bypass: bool) -> None:
        return None

    monkeypatch.setattr(llm_cli, "_enforce_budget_pre_call", _noop)


def _gemini_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend setup verification already ran (API key resolved)."""
    monkeypatch.setattr(gemini_backend, "_gemini_setup_verified", True)
    monkeypatch.setattr(gemini_backend, "_gemini_api_key", "AIza-fake-test-key")


def _fake_response(
    text: str, *, prompt_tokens: int = 0, candidate_tokens: int = 0, cached_tokens: int = 0
) -> SimpleNamespace:
    """A faithful stand-in for genai's GenerateContentResponse."""
    usage = SimpleNamespace(
        prompt_token_count=prompt_tokens,
        candidates_token_count=candidate_tokens,
        cached_content_token_count=cached_tokens,
    )
    return SimpleNamespace(text=text, usage_metadata=usage)


def _record_to(rows: list[dict[str, object]]) -> Callable[..., None]:
    """A typed record_llm_call stand-in that captures every row's kwargs."""

    def _capture(**kw: object) -> None:
        rows.append(dict(kw))

    return _capture


def _record_discard(**kw: object) -> None:
    return None


class _FakeModels:
    def __init__(self, owner: _FakeClient) -> None:
        self.owner = owner

    def generate_content(self, **kwargs: object) -> SimpleNamespace:
        self.owner.calls.append(dict(kwargs))
        outcome = _FakeClient._outcome
        if isinstance(outcome, BaseException):
            raise outcome
        assert outcome is not None
        return outcome

    def list(self) -> list[SimpleNamespace]:
        return list(_FakeClient.catalog)


class _FakeClient:
    """Provider-free stand-in for ``genai.Client`` and its models resource."""

    last_instances: ClassVar[list[_FakeClient]] = []
    _outcome: ClassVar[SimpleNamespace | BaseException | None] = None
    catalog: ClassVar[list[SimpleNamespace]] = []

    def __init__(self, **kwargs: object) -> None:
        self.init_kwargs = dict(kwargs)
        self.calls: list[dict[str, object]] = []
        self.models = _FakeModels(self)
        self.closed = False
        _FakeClient.last_instances.append(self)

    def close(self) -> None:
        self.closed = True

    @classmethod
    def returning(cls, response: SimpleNamespace) -> None:
        cls._outcome = response

    @classmethod
    def raising(cls, exc: BaseException) -> None:
        cls._outcome = exc


@pytest.fixture(autouse=True)
def _reset_fake_model() -> None:
    _FakeClient.last_instances = []
    _FakeClient._outcome = None
    _FakeClient.catalog = []


# ---------------------------------------------------------------------------
# Legacy allowlist symbols (retained for backward-compat; routing no longer
# uses them — Gemini is now selected by model family, not by allowlist).


def test_allowlist_ships_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """GEMINI_BACKEND_ALLOWED_PURPOSES is the legacy routing allowlist; it ships
    empty and stays empty (routing is now model-family-based). Kept as a canary:
    if something accidentally adds to this set, the PR should document why."""
    monkeypatch.delenv(gemini_backend.GEMINI_BACKEND_PURPOSES_ENV_VAR, raising=False)
    assert not gemini_backend.GEMINI_BACKEND_ALLOWED_PURPOSES
    assert not gemini_backend.gemini_allowed_purposes()


def test_env_var_merges_into_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        gemini_backend.GEMINI_BACKEND_PURPOSES_ENV_VAR, "viewspec_compile, bear_case ,"
    )
    allowed = gemini_backend.gemini_allowed_purposes()
    assert "viewspec_compile" in allowed
    assert "bear_case" in allowed
    assert "" not in allowed


# ---------------------------------------------------------------------------
# Gemini model resolution


def test_gemini_model_tiers_follow_claude_table(monkeypatch: pytest.MonkeyPatch) -> None:
    # A purpose explicitly pinned to a Gemini id in LLM_MODELS is returned verbatim
    # (no tier derivation). No PROD purpose is Gemini-pinned since the 2026-07-02
    # un-pin, so exercise the verbatim path with a synthetic pin.
    monkeypatch.setitem(
        llm_cli.LLM_MODELS, "gemini_pinned_purpose", gemini_backend.GEMINI_BACKEND_FAST_MODEL
    )
    assert gemini_backend.gemini_model_for("gemini_pinned_purpose") == (
        gemini_backend.GEMINI_BACKEND_FAST_MODEL
    )
    # viewspec_compile reverted to Haiku in the un-pin; its Gemini MIRROR is still
    # Flash via tier derivation (the derivation path, not a verbatim pin).
    assert llm_cli.LLM_MODELS["viewspec_compile"] == llm_cli.FAST_CLASSIFIER_MODEL
    assert gemini_backend.gemini_model_for("viewspec_compile") == (
        gemini_backend.GEMINI_BACKEND_FAST_MODEL
    )
    # Haiku-tier purposes mirror to Flash via tier derivation.
    monkeypatch.setitem(llm_cli.LLM_MODELS, "haiku_test_purpose", llm_cli.FAST_CLASSIFIER_MODEL)
    assert gemini_backend.gemini_model_for("haiku_test_purpose") == (
        gemini_backend.GEMINI_BACKEND_FAST_MODEL
    )
    # Sonnet / Opus analytical purposes mirror to Pro.
    assert (
        gemini_backend.gemini_model_for("bear_case") == gemini_backend.GEMINI_BACKEND_DEFAULT_MODEL
    )
    assert gemini_backend.gemini_model_for("company_description") == (
        gemini_backend.GEMINI_BACKEND_DEFAULT_MODEL
    )
    # Unknown / absent purposes default to Pro.
    assert gemini_backend.gemini_model_for("not_a_purpose") == (
        gemini_backend.GEMINI_BACKEND_DEFAULT_MODEL
    )
    assert gemini_backend.gemini_model_for(None) == gemini_backend.GEMINI_BACKEND_DEFAULT_MODEL
    # An explicit GEMINI_MODELS pin beats everything.
    monkeypatch.setitem(gemini_backend.GEMINI_MODELS, "viewspec_compile", "gemini-exp-pin")
    assert gemini_backend.gemini_model_for("viewspec_compile") == "gemini-exp-pin"


# ---------------------------------------------------------------------------
# Backend selection in call_llm


def test_call_llm_defaults_to_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(gemini_backend.GEMINI_BACKEND_PURPOSES_ENV_VAR, raising=False)
    calls: list[str] = []

    def _fake_claude(prompt: str, **kw: object) -> str:
        calls.append("claude")
        return "C"

    monkeypatch.setattr(llm_cli, "_call_claude", _fake_claude)

    def _fail(*args: object, **kwargs: object) -> str:
        raise AssertionError("gemini must not be called for non-allowlisted purposes")

    monkeypatch.setattr(gemini_backend, "call_gemini", _fail)
    assert llm_cli.call_llm("p", purpose="bear_case") == "C"
    assert calls == ["claude"]


def test_call_llm_gemini_model_id_routes_to_gemini_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When LLM_MODELS pins a purpose to a Gemini model id, call_llm dispatches
    to the Gemini backend and passes the resolved model id explicitly."""
    monkeypatch.setitem(llm_cli.LLM_MODELS, "viewspec_compile", "gemini-2.5-flash")
    seen: dict[str, object] = {}

    def _fake_gemini(prompt: str, **kwargs: object) -> str:
        seen.update(kwargs, prompt=prompt)
        return "G"

    monkeypatch.setattr(gemini_backend, "call_gemini", _fake_gemini)

    def _fail(prompt: str, **kw: object) -> str:
        raise AssertionError("claude must not be called when gemini succeeds")

    monkeypatch.setattr(llm_cli, "_call_claude", _fail)
    out = llm_cli.call_llm(
        "compile this", purpose="viewspec_compile", ticker="NU", scope="s", run_id="r1"
    )
    assert out == "G"
    assert seen["prompt"] == "compile this"
    assert seen["purpose"] == "viewspec_compile"
    assert seen["ticker"] == "NU"
    assert seen["scope"] == "s"
    assert seen["run_id"] == "r1"
    assert seen["model"] == "gemini-2.5-flash"  # resolved in call_llm, passed explicitly


def test_call_llm_explicit_backend_forces_gemini(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(gemini_backend.GEMINI_BACKEND_PURPOSES_ENV_VAR, raising=False)
    seen: dict[str, object] = {}

    def _fake_gemini(prompt: str, **kwargs: object) -> str:
        seen.update(kwargs)
        return "G"

    monkeypatch.setattr(gemini_backend, "call_gemini", _fake_gemini)
    out = llm_cli.call_llm("p", purpose="bear_case", model="gemini-2.5-flash", backend="gemini")
    assert out == "G"
    assert seen["model"] == "gemini-2.5-flash"  # explicit model rides through


def test_call_llm_explicit_claude_model_stays_on_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit Claude model id dispatches to Claude regardless of purpose."""
    claude_seen: dict[str, object] = {}

    def _fake_claude(prompt: str, **kw: object) -> str:
        claude_seen.update(kw)
        return "C"

    monkeypatch.setattr(llm_cli, "_call_claude", _fake_claude)

    def _fail(*args: object, **kwargs: object) -> str:
        raise AssertionError("claude model id must never route to gemini")

    monkeypatch.setattr(gemini_backend, "call_gemini", _fail)
    assert llm_cli.call_llm("p", purpose="bear_case", model="claude-opus-4-7") == "C"
    assert claude_seen["model"] == "claude-opus-4-7"


def test_call_llm_explicit_gemini_model_routes_to_gemini(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit Gemini model id dispatches to the Gemini backend via family
    detection, without needing backend='gemini' explicitly."""
    seen: dict[str, object] = {}

    def _fake_gemini(prompt: str, **kw: object) -> str:
        seen.update(kw)
        return "G"

    monkeypatch.setattr(gemini_backend, "call_gemini", _fake_gemini)

    def _fail(prompt: str, **kw: object) -> str:
        raise AssertionError("gemini model id must not route to claude")

    monkeypatch.setattr(llm_cli, "_call_claude", _fail)
    out = llm_cli.call_llm("p", purpose="bear_case", model="gemini-3.1-pro-preview")
    assert out == "G"
    assert seen["model"] == "gemini-3.1-pro-preview"


def test_call_llm_unknown_backend_raises() -> None:
    with pytest.raises(ValueError, match="Unknown LLM backend"):
        llm_cli.call_llm("p", purpose="bear_case", backend="grok")


def test_gemini_model_pin_operational_failure_falls_back_to_claude(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pinning a purpose to Gemini must never break the pipeline: a transient
    Gemini failure degrades to Claude (which records its own ledger rows)."""
    monkeypatch.setitem(llm_cli.LLM_MODELS, "viewspec_compile", "gemini-2.5-flash")

    def _boom(prompt: str, **kw: object) -> str:
        raise RuntimeError("gemini transient failure")

    monkeypatch.setattr(gemini_backend, "call_gemini", _boom)
    claude_seen: dict[str, object] = {}

    def _fake_claude(prompt: str, **kw: object) -> str:
        claude_seen.update(kw)
        return "C"

    monkeypatch.setattr(llm_cli, "_call_claude", _fake_claude)
    assert llm_cli.call_llm("p", purpose="viewspec_compile") == "C"
    # The purpose's pinned model is Gemini, so Claude fallback uses DEFAULT_MODEL.
    assert claude_seen["model"] == llm_cli.DEFAULT_MODEL


def test_forced_gemini_operational_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """backend='gemini' means the caller wants GEMINI's answer (the compare
    harness): a failure must surface, never silently switch backends."""

    def _boom(prompt: str, **kw: object) -> str:
        raise RuntimeError("gemini transient failure")

    monkeypatch.setattr(gemini_backend, "call_gemini", _boom)

    def _fail(prompt: str, **kw: object) -> str:
        raise AssertionError("claude must not run on a forced-gemini failure")

    monkeypatch.setattr(llm_cli, "_call_claude", _fail)
    with pytest.raises(RuntimeError, match="gemini transient failure"):
        llm_cli.call_llm("p", purpose="bear_case", backend="gemini")


@pytest.mark.parametrize(
    "exc",
    [
        llm_cli.LLMSetupError("gemini API key missing"),
        llm_cli.LLMBudgetExceeded("hard cap"),
    ],
)
def test_gemini_hard_stops_propagate_without_claude_fallback(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    """Setup + budget errors are hard stops (llm.cli.is_hard_stop): degrading
    to Claude would mask an operator-actionable problem, so they propagate."""
    monkeypatch.setitem(llm_cli.LLM_MODELS, "viewspec_compile", "gemini-2.5-flash")

    def _boom(prompt: str, **kw: object) -> str:
        raise exc

    monkeypatch.setattr(gemini_backend, "call_gemini", _boom)

    def _fail(prompt: str, **kw: object) -> str:
        raise AssertionError("claude must not run on a gemini hard stop")

    monkeypatch.setattr(llm_cli, "_call_claude", _fail)
    with pytest.raises(type(exc)):
        llm_cli.call_llm("p", purpose="viewspec_compile")
    assert llm_cli.is_hard_stop(exc)


# ---------------------------------------------------------------------------
# The Gemini Developer API call contract


def test_call_gemini_api_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end through call_gemini with genai.Client captured: the
    configured API key, the purpose-resolved model, the prompt, and the
    backend default timeout."""
    _no_budget(monkeypatch)
    _gemini_ready(monkeypatch)
    monkeypatch.setattr(gemini_backend.genai, "Client", _FakeClient)
    _FakeClient.returning(_fake_response("PONG"))

    out = gemini_backend.call_gemini("ping prompt", purpose="viewspec_compile")
    assert out == "PONG"
    assert len(_FakeClient.last_instances) == 1
    client = _FakeClient.last_instances[0]
    assert client.init_kwargs["api_key"] == "AIza-fake-test-key"
    http_options = client.init_kwargs["http_options"]
    assert getattr(http_options, "timeout") == gemini_backend.GEMINI_BACKEND_TIMEOUT_SECONDS * 1000
    assert getattr(getattr(http_options, "retry_options"), "attempts") == 1
    assert client.calls == [
        {"model": gemini_backend.GEMINI_BACKEND_FAST_MODEL, "contents": "ping prompt"}
    ]
    assert client.closed


def test_call_gemini_explicit_model_and_timeout_override(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_budget(monkeypatch)
    _gemini_ready(monkeypatch)
    monkeypatch.setattr(gemini_backend.genai, "Client", _FakeClient)
    _FakeClient.returning(_fake_response("hi"))

    gemini_backend.call_gemini("p", model="gemini-2.5-pro", timeout_seconds=42, purpose="bear_case")
    client = _FakeClient.last_instances[0]
    assert client.calls == [{"model": "gemini-2.5-pro", "contents": "p"}]
    assert getattr(client.init_kwargs["http_options"], "timeout") == 42_000


# ---------------------------------------------------------------------------
# Outcomes: usage/cost mapping, ledger, failure classification


def test_call_gemini_records_usage_and_real_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_budget(monkeypatch)
    _gemini_ready(monkeypatch)
    monkeypatch.setattr(gemini_backend.genai, "Client", _FakeClient)
    _FakeClient.returning(
        _fake_response("answer text", prompt_tokens=1010, candidate_tokens=205, cached_tokens=50)
    )
    rows: list[dict[str, object]] = []
    monkeypatch.setattr(gemini_backend, "record_llm_call", _record_to(rows))

    out = gemini_backend.call_gemini("p", purpose="bear_case", ticker="NU", scope="s", run_id="r9")
    assert out == "answer text"
    assert len(rows) == 1
    row = rows[0]
    assert row["model"] == gemini_backend.GEMINI_BACKEND_DEFAULT_MODEL
    assert row["purpose"] == "bear_case"
    assert row["ticker"] == "NU"
    assert row["run_id"] == "r9"
    assert row["response_text"] == "answer text"
    meta = cast("dict[str, object]", row["meta"])
    # Current standard API pricing, including Gemini cache-read discount.
    from llm.model_ladder import estimated_call_usd

    expected = estimated_call_usd(
        gemini_backend.GEMINI_BACKEND_DEFAULT_MODEL,
        1010,
        205,
        cached_input_tokens=50,
    )
    assert meta["total_cost_usd"] == pytest.approx(expected)
    assert expected > 0.0
    usage = cast("dict[str, object]", meta["usage"])
    assert usage["input_tokens"] == 1010
    assert usage["output_tokens"] == 205
    assert usage["cache_read_input_tokens"] == 50


def test_call_gemini_unauthenticated_classifies_as_setup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected API key is deterministic and operator-actionable — it must
    classify as LLMSetupError (hard stop with the key-setup hint), exactly
    like a missing binary on the Claude path."""
    _no_budget(monkeypatch)
    _gemini_ready(monkeypatch)
    monkeypatch.setattr(gemini_backend.genai, "Client", _FakeClient)
    monkeypatch.setattr(gemini_backend, "record_llm_call", _record_discard)
    _FakeClient.raising(
        genai_errors.ClientError(
            401,
            {"error": {"message": "credentials invalid", "status": "UNAUTHENTICATED"}},
        )
    )

    with pytest.raises(llm_cli.LLMSetupError, match="rejected"):
        gemini_backend.call_gemini("p", purpose="bear_case")


def test_call_gemini_invalid_argument_api_key_marker_classifies_as_setup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact shape observed live: InvalidArgument (400) with an
    API_KEY_INVALID reason — the marker string is what promotes this
    otherwise-ambiguous status code to a setup error."""
    _no_budget(monkeypatch)
    _gemini_ready(monkeypatch)
    monkeypatch.setattr(gemini_backend.genai, "Client", _FakeClient)
    monkeypatch.setattr(gemini_backend, "record_llm_call", _record_discard)
    _FakeClient.raising(
        genai_errors.ClientError(
            400,
            {
                "error": {
                    "message": 'API key not valid. [reason: "API_KEY_INVALID"',
                    "status": "INVALID_ARGUMENT",
                }
            },
        )
    )

    with pytest.raises(llm_cli.LLMSetupError, match="API key"):
        gemini_backend.call_gemini("p", purpose="bear_case")


def test_call_gemini_invalid_argument_without_auth_marker_stays_operational(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """InvalidArgument (400) WITHOUT an auth-shaped marker is a genuinely bad
    request (e.g. a malformed param), not a key problem — must NOT be
    misclassified as a setup error the operator can't actually fix by
    rotating a key."""
    _no_budget(monkeypatch)
    _gemini_ready(monkeypatch)
    monkeypatch.setattr(gemini_backend.genai, "Client", _FakeClient)
    rows: list[dict[str, object]] = []
    monkeypatch.setattr(gemini_backend, "record_llm_call", _record_to(rows))
    error = genai_errors.ClientError(
        400,
        {
            "error": {
                "message": "request payload size exceeds the limit",
                "status": "INVALID_ARGUMENT",
            }
        },
    )
    _FakeClient.raising(error)

    with pytest.raises(genai_errors.ClientError):
        gemini_backend.call_gemini("p", purpose="bear_case")
    assert len(rows) == 1


def test_call_gemini_non_auth_failure_stays_operational(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_budget(monkeypatch)
    _gemini_ready(monkeypatch)
    monkeypatch.setattr(gemini_backend.genai, "Client", _FakeClient)
    rows: list[dict[str, object]] = []
    monkeypatch.setattr(gemini_backend, "record_llm_call", _record_to(rows))
    error = genai_errors.ServerError(
        503,
        {
            "error": {
                "message": "overloaded: https://example.test?api_key=secret-value",
                "status": "UNAVAILABLE",
            }
        },
    )
    _FakeClient.raising(error)

    with pytest.raises(genai_errors.ServerError):
        gemini_backend.call_gemini("p", purpose="bear_case")
    assert len(rows) == 1  # the failed attempt still gets its ledger row
    assert "ServerError" in cast("str", rows[0]["error"])
    assert "secret-value" not in cast("str", rows[0]["error"])
    assert "api_key=***" in cast("str", rows[0]["error"])


def test_call_gemini_empty_response_is_operational(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_budget(monkeypatch)
    _gemini_ready(monkeypatch)
    monkeypatch.setattr(gemini_backend.genai, "Client", _FakeClient)
    monkeypatch.setattr(gemini_backend, "record_llm_call", _record_discard)
    _FakeClient.returning(_fake_response("   "))

    with pytest.raises(RuntimeError, match="empty response"):
        gemini_backend.call_gemini("p", purpose="bear_case")


def test_call_gemini_missing_api_key_raises_setup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_budget(monkeypatch)
    monkeypatch.setattr(gemini_backend, "_gemini_setup_verified", False)
    monkeypatch.setattr(gemini_backend, "_gemini_api_key", None)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(llm_cli.LLMSetupError, match="aistudio"):
        gemini_backend.call_gemini("p", purpose="bear_case")


def test_call_gemini_budget_hard_block_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shared budget gate runs BEFORE any API work, and a hard block must
    prevent the call entirely."""

    def _block(purpose: str | None, *, force_budget_bypass: bool) -> None:
        raise llm_cli.LLMBudgetExceeded(f"{purpose}: monthly cap exceeded")

    monkeypatch.setattr(llm_cli, "_enforce_budget_pre_call", _block)
    monkeypatch.setattr(gemini_backend.genai, "Client", _FakeClient)

    with pytest.raises(llm_cli.LLMBudgetExceeded):
        gemini_backend.call_gemini("p", purpose="bear_case")
    assert _FakeClient.last_instances == []  # never constructed past the hard block


# ---------------------------------------------------------------------------
# NotFound self-anneal (preview alias 404s -> live catalog -> stable fallback)


def test_call_gemini_anneals_on_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """A NotFound on the resolved model queries the live catalog for a Flash
    replacement, then retries in order until one succeeds."""
    _no_budget(monkeypatch)
    _gemini_ready(monkeypatch)
    monkeypatch.setattr(gemini_backend, "_effective_fast_model", None)

    def _discover(_client: object) -> str:
        return "gemini-3.2-flash"

    monkeypatch.setattr(gemini_backend, "_discover_api_flash_model", _discover)
    monkeypatch.setattr(gemini_backend, "record_llm_call", _record_discard)

    attempted_models: list[str] = []

    class _AnnealModels:
        def generate_content(self, *, model: str, contents: str) -> SimpleNamespace:
            attempted_models.append(model)
            if model == gemini_backend.GEMINI_BACKEND_FAST_MODEL:
                raise genai_errors.ClientError(
                    404, {"error": {"message": "not found", "status": "NOT_FOUND"}}
                )
            return _fake_response("recovered")

    class _AnnealClient(_FakeClient):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs)
            self.models = _AnnealModels()

    monkeypatch.setattr(gemini_backend.genai, "Client", _AnnealClient)

    out = gemini_backend.call_gemini("p", purpose="viewspec_compile")
    assert out == "recovered"
    assert attempted_models == [
        gemini_backend.GEMINI_BACKEND_FAST_MODEL,
        "gemini-3.2-flash",
    ]
    assert gemini_backend._effective_fast_model == "gemini-3.2-flash"


def test_call_gemini_anneals_to_stable_fallback_when_catalog_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the live catalog lookup itself fails (network, etc.), the sequence
    still ends with the hardcoded stable GA fallback."""
    _no_budget(monkeypatch)
    _gemini_ready(monkeypatch)
    monkeypatch.setattr(gemini_backend, "_effective_fast_model", None)

    def _discover_none(_client: object) -> None:
        return None

    monkeypatch.setattr(gemini_backend, "_discover_api_flash_model", _discover_none)
    monkeypatch.setattr(gemini_backend, "record_llm_call", _record_discard)

    attempted_models: list[str] = []

    class _AnnealModels:
        def generate_content(self, *, model: str, contents: str) -> SimpleNamespace:
            attempted_models.append(model)
            if model != gemini_backend._GEMINI_FAST_MODEL_FALLBACK:
                raise genai_errors.ClientError(
                    404, {"error": {"message": "not found", "status": "NOT_FOUND"}}
                )
            return _fake_response("recovered via stable GA")

    class _AnnealClient(_FakeClient):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs)
            self.models = _AnnealModels()

    monkeypatch.setattr(gemini_backend.genai, "Client", _AnnealClient)

    out = gemini_backend.call_gemini("p", purpose="viewspec_compile")
    assert out == "recovered via stable GA"
    assert attempted_models == [
        gemini_backend.GEMINI_BACKEND_FAST_MODEL,
        gemini_backend._GEMINI_FAST_MODEL_FALLBACK,
    ]


def test_call_gemini_pro_not_found_never_anneals_to_flash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing Pro endpoint must stay in the governed router path.

    Flash alias repair is safe within the Flash family; silently crossing from
    Pro to Flash would bypass the purpose-level capability/eval decision.
    """
    _no_budget(monkeypatch)
    _gemini_ready(monkeypatch)
    monkeypatch.setattr(gemini_backend, "_effective_fast_model", None)
    monkeypatch.setattr(gemini_backend, "record_llm_call", _record_discard)

    attempted_models: list[str] = []

    class _MissingProModels:
        def generate_content(self, *, model: str, contents: str) -> SimpleNamespace:
            attempted_models.append(model)
            raise genai_errors.ClientError(
                404, {"error": {"message": "not found", "status": "NOT_FOUND"}}
            )

        def list(self) -> list[object]:
            raise AssertionError("Pro failures must not query the Flash catalog")

    class _MissingProClient(_FakeClient):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs)
            self.models = _MissingProModels()

    monkeypatch.setattr(gemini_backend.genai, "Client", _MissingProClient)

    with pytest.raises(genai_errors.ClientError) as exc_info:
        gemini_backend.call_gemini(
            "p",
            model="gemini-3.1-pro-preview",
            purpose="bear_case",
        )

    assert exc_info.value.code == 404
    assert attempted_models == ["gemini-3.1-pro-preview"]
    assert gemini_backend._effective_fast_model is None


def test_discover_flash_model_uses_new_sdk_supported_actions() -> None:
    _FakeClient.catalog = [
        SimpleNamespace(name="models/gemini-3.5-pro", supported_actions=["generateContent"]),
        SimpleNamespace(name="models/gemini-3.5-flash", supported_actions=["generateContent"]),
        SimpleNamespace(name="models/gemini-3.6-flash", supported_actions=["countTokens"]),
    ]
    client = _FakeClient(api_key="fake")

    assert gemini_backend._discover_api_flash_model(client) == "gemini-3.5-flash"


# ---------------------------------------------------------------------------
# Usage mapping edge cases


def test_usage_meta_tolerates_junk_or_missing_usage() -> None:
    for payload in (
        None,
        {},
        {"prompt_token_count": "NaN", "candidates_token_count": True},
    ):
        meta = gemini_backend.usage_meta_from_response(cast("dict[str, object] | None", payload))
        usage = cast("dict[str, object]", meta["usage"])
        assert usage["input_tokens"] == 0
        assert usage["output_tokens"] == 0
        assert meta["total_cost_usd"] == 0.0


# ---------------------------------------------------------------------------
# Package layout + compare harness


def test_dependency_lock_keeps_gemini_sdk_cross_platform() -> None:
    lock_text = (PROJECT_ROOT / "requirements.lock").read_text(encoding="utf-8")

    assert "google-genai==2.17.0" in lock_text
    assert "google-generativeai==" not in lock_text
    assert 'pywin32==312 ; sys_platform == "win32"' in lock_text


def test_llm_package_reexports_gemini_backend() -> None:
    import llm

    assert llm.call_gemini is gemini_backend.call_gemini
    assert llm.gemini_allowed_purposes is gemini_backend.gemini_allowed_purposes
    assert llm.gemini_model_for is gemini_backend.gemini_model_for
    assert llm.GEMINI_BACKEND_ALLOWED_PURPOSES is gemini_backend.GEMINI_BACKEND_ALLOWED_PURPOSES
    assert llm.GEMINI_BACKEND_DEFAULT_MODEL.startswith("gemini")


def test_compare_backends_smoke_prompts_are_real_purposes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The smoke set must exercise registered purposes with non-trivial
    prompts, and the harness must record (not raise) backend failures."""
    monkeypatch.syspath_prepend(str(PROJECT_ROOT / "execution"))
    import compare_backends

    prompts = compare_backends._smoke_prompts()
    assert len(prompts) == 3
    for item in prompts:
        assert str(item["purpose"]) in llm_cli.LLM_MODELS  # cheap REGISTERED purposes only
        assert len(str(item["prompt"])) > 200
        assert item["expected"]

    def _boom(prompt: str, **kw: object) -> str:
        raise llm_cli.LLMSetupError("no api key configured")

    monkeypatch.setattr(compare_backends, "call_llm", _boom)
    result = compare_backends._run_one_backend(
        "gemini",
        "p",
        purpose="viewspec_compile",
        ticker=None,
        run_id="r",
        model=None,
        timeout_seconds=None,
        force_budget_bypass=False,
    )
    assert result["ok"] is False
    assert "LLMSetupError" in cast("str", result["error"])
