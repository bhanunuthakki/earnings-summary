# pyright: reportPrivateUsage=false
#
# These tests deliberately reach module-private surface (_gemini_subprocess_env,
# _NULL_CONTEXT_FILENAME, compare_backends._smoke_prompts) — that IS the unit
# under test. Module-scoped directive per the repo's cli.py precedent.
"""Tests for the eval-gated Gemini second backend (src/llm/gemini_backend.py)
and call_llm's backend-selection logic.

Every test monkeypatches subprocess.run (or the backend entry points) — the
suite never spawns a real CLI and never spends. Coverage:

  * the eval gate: GEMINI_BACKEND_ALLOWED_PURPOSES ships EMPTY, env-var merge;
  * backend selection in call_llm (default Claude, allowlist routing,
    explicit force, explicit-model pin stays on Claude, unknown backend);
  * failure policy (allowlist-routed operational failure degrades to Claude;
    forced-gemini failures raise; hard stops always propagate);
  * the no-silent-API-key guard (subprocess env strips keys, forces consumer
    OAuth, trusts the neutral workspace, suppresses context files);
  * the subprocess contract (stdin prompt, UTF-8, absolute path, JSON parse,
    token/usage mapping, $0 subscription cost, ledger rows, auth-failure →
    LLMSetupError classification).
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from llm import cli as llm_cli
from llm import gemini_backend

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Helpers


def _no_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize the shared budget gate (single seam used by BOTH backends)."""

    def _noop(purpose: str | None, *, force_budget_bypass: bool) -> None:
        return None

    monkeypatch.setattr(llm_cli, "_enforce_budget_pre_call", _noop)


def _gemini_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend setup verification already ran (binary resolved)."""
    monkeypatch.setattr(gemini_backend, "_gemini_setup_verified", True)
    monkeypatch.setattr(gemini_backend, "_gemini_cli_path", r"C:\fake\npm\gemini.cmd")


def _success_envelope(text: str, *, models: dict[str, dict[str, int]] | None = None) -> str:
    """A faithful `gemini -o json` success envelope (see parse_gemini_json_output)."""
    payload: dict[str, object] = {"session_id": "abc123", "response": text}
    if models is not None:
        payload["stats"] = {
            "models": {
                name: {"api": {"totalRequests": 1}, "tokens": tokens}
                for name, tokens in models.items()
            }
        }
    return json.dumps(payload)


def _completed(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["gemini"], returncode=0, stdout=stdout, stderr="")


def _record_to(rows: list[dict[str, object]]) -> Callable[..., None]:
    """A typed record_llm_call stand-in that captures every row's kwargs."""

    def _capture(**kw: object) -> None:
        rows.append(dict(kw))

    return _capture


def _record_discard(**kw: object) -> None:
    return None


def _run_returning(stdout: str) -> Callable[..., subprocess.CompletedProcess[str]]:
    """A typed subprocess.run stand-in returning a fixed success envelope."""

    def _run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return _completed(stdout)

    return _run


# ---------------------------------------------------------------------------
# The eval gate: allowlist ships empty


def test_allowlist_ships_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE GATE. Nothing routes to Gemini until judges pass a purpose — the
    code-level allowlist must be empty and stay empty until an eval signs
    off (directives/gemini_backend.md). If this test fails because someone
    added a purpose, the PR must link the eval verdict."""
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
    # Haiku fast-classifier purposes mirror to Flash.
    assert llm_cli.LLM_MODELS["viewspec_compile"] == llm_cli.FAST_CLASSIFIER_MODEL
    assert gemini_backend.gemini_model_for("viewspec_compile") == (
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
    # An explicit GEMINI_MODELS pin beats the tier derivation.
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


def test_call_llm_routes_allowlisted_purpose_to_gemini(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(gemini_backend.GEMINI_BACKEND_PURPOSES_ENV_VAR, "viewspec_compile")
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
    assert seen["model"] is None  # purpose-resolved inside call_gemini


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


def test_call_llm_explicit_model_pin_stays_on_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    """The allowlist only reroutes purpose-resolved calls: an explicit Claude
    model id (a debugging pin) must never be sent to Gemini."""
    monkeypatch.setenv(gemini_backend.GEMINI_BACKEND_PURPOSES_ENV_VAR, "bear_case")
    claude_seen: dict[str, object] = {}

    def _fake_claude(prompt: str, **kw: object) -> str:
        claude_seen.update(kw)
        return "C"

    monkeypatch.setattr(llm_cli, "_call_claude", _fake_claude)

    def _fail(*args: object, **kwargs: object) -> str:
        raise AssertionError("explicit-model calls must stay on claude")

    monkeypatch.setattr(gemini_backend, "call_gemini", _fail)
    assert llm_cli.call_llm("p", purpose="bear_case", model="claude-opus-4-7") == "C"
    assert claude_seen["model"] == "claude-opus-4-7"


def test_call_llm_unknown_backend_raises() -> None:
    with pytest.raises(ValueError, match="Unknown LLM backend"):
        llm_cli.call_llm("p", purpose="bear_case", backend="grok")


def test_allowlisted_gemini_operational_failure_falls_back_to_claude(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enabling a purpose must never be able to break the pipeline: a
    transient Gemini failure on an allowlist-routed call degrades to the
    Claude path (which records its own ledger rows)."""
    monkeypatch.setenv(gemini_backend.GEMINI_BACKEND_PURPOSES_ENV_VAR, "viewspec_compile")

    def _boom(prompt: str, **kw: object) -> str:
        raise RuntimeError("gemini transient failure")

    monkeypatch.setattr(gemini_backend, "call_gemini", _boom)
    claude_seen: dict[str, object] = {}

    def _fake_claude(prompt: str, **kw: object) -> str:
        claude_seen.update(kw)
        return "C"

    monkeypatch.setattr(llm_cli, "_call_claude", _fake_claude)
    assert llm_cli.call_llm("p", purpose="viewspec_compile") == "C"
    # Claude resolves its own model for the purpose (Haiku tier here).
    assert claude_seen["model"] == llm_cli.FAST_CLASSIFIER_MODEL


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
        llm_cli.LLMSetupError("gemini login missing"),
        llm_cli.LLMBudgetExceeded("hard cap"),
    ],
)
def test_gemini_hard_stops_propagate_without_claude_fallback(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    """Setup + budget errors are hard stops (llm.cli.is_hard_stop): degrading
    an allowlist-routed call to Claude would mask an operator-actionable
    problem, so they propagate even though Claude could have answered."""
    monkeypatch.setenv(gemini_backend.GEMINI_BACKEND_PURPOSES_ENV_VAR, "viewspec_compile")

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
# The no-silent-API-key guard


def test_subprocess_env_strips_api_keys_and_forces_consumer_oauth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE BILLING GUARD. Even with every Google credential variable set in
    the parent environment (the repo's .env keeps GEMINI_API_KEY for the
    fallback path), the backend subprocess must see none of them and must be
    forced onto the consumer OAuth (GCA) auth path."""
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "AIza-test-key-2")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", r"C:\creds.json")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    env = gemini_backend._gemini_subprocess_env()
    assert "GEMINI_API_KEY" not in env
    assert "GOOGLE_API_KEY" not in env
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in env
    assert "GOOGLE_GENAI_USE_VERTEXAI" not in env
    assert env["GOOGLE_GENAI_USE_GCA"] == "true"
    assert env["GEMINI_CLI_TRUST_WORKSPACE"] == "true"
    assert env["NO_BROWSER"] == "true"


def test_call_gemini_subprocess_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end through call_gemini with subprocess.run captured: guarded
    env, stdin prompt, UTF-8, absolute binary path, JSON output flags, the
    purpose-resolved model, the backend default timeout, and a neutral cwd
    whose workspace settings suppress context-file injection."""
    _no_budget(monkeypatch)
    _gemini_ready(monkeypatch)
    monkeypatch.setattr(gemini_backend, "_gemini_neutral_cwd_cache", None)
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test-key")
    captured: dict[str, object] = {}

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        captured.update(kwargs)
        return _completed(_success_envelope("PONG"))

    monkeypatch.setattr(subprocess, "run", _fake_run)
    out = gemini_backend.call_gemini("ping prompt", purpose="viewspec_compile")
    assert out == "PONG"
    cmd = cast("list[str]", captured["cmd"])
    assert cmd[0] == r"C:\fake\npm\gemini.cmd"  # absolute path, never bare "gemini"
    assert cmd[cmd.index("-m") + 1] == gemini_backend.GEMINI_BACKEND_FAST_MODEL
    assert cmd[cmd.index("-o") + 1] == "json"
    assert "-p" not in cmd  # prompt rides stdin only
    assert captured["input"] == "ping prompt"
    assert captured["encoding"] == "utf-8"
    assert captured["errors"] == "replace"
    assert captured["timeout"] == gemini_backend.GEMINI_BACKEND_TIMEOUT_SECONDS
    env = cast("dict[str, str]", captured["env"])
    assert "GEMINI_API_KEY" not in env
    assert env["GOOGLE_GENAI_USE_GCA"] == "true"
    # The neutral cwd carries the context-suppression workspace settings.
    cwd = cast("str", captured["cwd"])
    settings = json.loads((Path(cwd) / ".gemini" / "settings.json").read_text(encoding="utf-8"))
    assert settings["context"]["fileName"] == gemini_backend._NULL_CONTEXT_FILENAME


# ---------------------------------------------------------------------------
# Subprocess outcomes: parsing, ledger, failure classification


def test_call_gemini_records_usage_and_zero_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_budget(monkeypatch)
    _gemini_ready(monkeypatch)
    rows: list[dict[str, object]] = []
    monkeypatch.setattr(gemini_backend, "record_llm_call", _record_to(rows))
    envelope = _success_envelope(
        "answer text",
        models={
            "gemini-2.5-pro": {"prompt": 1000, "candidates": 200, "cached": 50},
            # Consumer tier can transparently reroute to flash mid-session.
            "gemini-2.5-flash": {"prompt": 10, "candidates": 5, "cached": 0},
        },
    )
    monkeypatch.setattr(subprocess, "run", _run_returning(envelope))
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
    assert meta["total_cost_usd"] == 0.0  # subscription: measured-free, not unknown
    usage = cast("dict[str, object]", meta["usage"])
    assert usage["input_tokens"] == 1010
    assert usage["output_tokens"] == 205
    assert usage["cache_read_input_tokens"] == 50


def test_call_gemini_auth_failure_classifies_as_setup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing/expired consumer login is deterministic and operator-
    actionable — it must classify as LLMSetupError (hard stop with the
    login hint), exactly like a missing binary on the Claude path."""
    _no_budget(monkeypatch)
    _gemini_ready(monkeypatch)
    monkeypatch.setattr(gemini_backend, "record_llm_call", _record_discard)

    def _fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(
            41,
            ["gemini"],
            output="",
            stderr=(
                "Error authenticating: FatalAuthenticationError: Manual authorization "
                "is required but the current session is non-interactive."
            ),
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)
    with pytest.raises(llm_cli.LLMSetupError, match="one-time interactive login"):
        gemini_backend.call_gemini("p", purpose="bear_case")


def test_call_gemini_json_error_envelope_classifies_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other observed auth shape: exit 41 with the JSON error envelope on
    stdout ('Please set an Auth method...')."""
    _no_budget(monkeypatch)
    _gemini_ready(monkeypatch)
    monkeypatch.setattr(gemini_backend, "record_llm_call", _record_discard)
    envelope = json.dumps(
        {
            "session_id": "x",
            "error": {
                "type": "Error",
                "message": "Please set an Auth method in your settings.json",
                "code": 41,
            },
        }
    )

    def _fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(41, ["gemini"], output=envelope, stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    with pytest.raises(llm_cli.LLMSetupError):
        gemini_backend.call_gemini("p", purpose="bear_case")


def test_call_gemini_non_auth_failure_stays_operational(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_budget(monkeypatch)
    _gemini_ready(monkeypatch)
    rows: list[dict[str, object]] = []
    monkeypatch.setattr(gemini_backend, "record_llm_call", _record_to(rows))

    def _fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, ["gemini"], output="", stderr="quota exceeded")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    with pytest.raises(subprocess.CalledProcessError):
        gemini_backend.call_gemini("p", purpose="bear_case")
    assert len(rows) == 1  # the failed attempt still gets its ledger row
    assert "CalledProcessError" in cast("str", rows[0]["error"])


def test_call_gemini_empty_response_is_operational(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_budget(monkeypatch)
    _gemini_ready(monkeypatch)
    monkeypatch.setattr(gemini_backend, "record_llm_call", _record_discard)
    monkeypatch.setattr(subprocess, "run", _run_returning(_success_envelope("   ")))
    with pytest.raises(RuntimeError, match="empty `response`"):
        gemini_backend.call_gemini("p", purpose="bear_case")


def test_call_gemini_missing_binary_raises_setup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_budget(monkeypatch)
    monkeypatch.setattr(gemini_backend, "_gemini_setup_verified", False)
    monkeypatch.setattr(gemini_backend, "_gemini_cli_path", None)

    def _which_none(name: str) -> None:
        return None

    monkeypatch.setattr(gemini_backend.shutil, "which", _which_none)
    with pytest.raises(llm_cli.LLMSetupError, match="npm install -g @google/gemini-cli"):
        gemini_backend.call_gemini("p", purpose="bear_case")


def test_call_gemini_budget_hard_block_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shared budget gate runs BEFORE any subprocess work, and a hard
    block must prevent the call entirely."""

    def _block(purpose: str | None, *, force_budget_bypass: bool) -> None:
        raise llm_cli.LLMBudgetExceeded(f"{purpose}: monthly cap exceeded")

    monkeypatch.setattr(llm_cli, "_enforce_budget_pre_call", _block)

    def _fail(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("subprocess must not run past a hard budget block")

    monkeypatch.setattr(subprocess, "run", _fail)
    with pytest.raises(llm_cli.LLMBudgetExceeded):
        gemini_backend.call_gemini("p", purpose="bear_case")


# ---------------------------------------------------------------------------
# Envelope parsing + usage mapping


def test_parse_gemini_json_output_success() -> None:
    text, meta = gemini_backend.parse_gemini_json_output(
        _success_envelope("hello", models={"gemini-2.5-pro": {"prompt": 1, "candidates": 2}})
    )
    assert text == "hello"
    assert meta["session_id"] == "abc123"


@pytest.mark.parametrize(
    "stdout, match",
    [
        ("not json at all", "did not return JSON"),
        ("[1, 2]", "non-object JSON"),
        (
            json.dumps({"session_id": "x", "error": {"message": "boom", "code": 41}}),
            "reported error",
        ),
        (json.dumps({"session_id": "x"}), "missing string `response`"),
        (json.dumps({"session_id": "x", "response": 7}), "missing string `response`"),
    ],
)
def test_parse_gemini_json_output_failures(stdout: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        gemini_backend.parse_gemini_json_output(stdout)


def test_usage_meta_tolerates_junk_stats() -> None:
    for payload in (
        {},
        {"stats": "nope"},
        {"stats": {"models": "nope"}},
        {"stats": {"models": {"m": "nope"}}},
        {"stats": {"models": {"m": {"tokens": {"prompt": "NaN", "candidates": True}}}}},
    ):
        meta = gemini_backend.usage_meta_from_gemini_stats(cast("dict[str, object]", payload))
        usage = cast("dict[str, object]", meta["usage"])
        assert usage["input_tokens"] == 0
        assert usage["output_tokens"] == 0
        assert meta["total_cost_usd"] == 0.0


# ---------------------------------------------------------------------------
# Package layout + compare harness


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
        raise llm_cli.LLMSetupError("login missing")

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
