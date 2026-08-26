"""call_llm_with_web purpose-based model resolution (news-table plan PR 3).

PR 3 made ``call_llm_with_web`` resolve its model from ``purpose`` (via
``LLM_MODELS`` / ``_model_for``) when no explicit ``model`` is passed.
Previously it hard-defaulted to ``DEFAULT_MODEL`` and ignored ``purpose``,
so a web-enabled caller could not be retuned centrally in ``LLM_MODELS``.

These tests pin that plumbing without touching a real CLI or DB: the
subprocess and the ledger write are both stubbed, and the assertion is on the
``--model`` argument actually handed to the Claude CLI. Covered:
  * an Opus-pinned purpose resolves to Opus when ``model`` is omitted;
  * an explicit ``model`` still wins (backward-compat);
  * recent_developments stays on Sonnet (DEFAULT_MODEL);
  * no purpose normalizes to the metered ``__default__`` attribution key.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from datetime import UTC
from typing import Any

import pytest

import llm_client
from llm import cli


def _good_cli_response(text: str = "ok https://example.test") -> str:
    """Minimal `claude -p --output-format json` success envelope."""
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": text,
            "total_cost_usd": 0.001,
            "usage": {
                "input_tokens": 10,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "output_tokens": 5,
            },
        }
    )


def _fail_if_plain_transport_runs(*_args: object, **_kwargs: object) -> str:
    pytest.fail("grounded web request must not degrade to plain output")


@pytest.fixture
def capture_web_cmd(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """Run call_llm_with_web fully offline, capturing the argv handed to the
    CLI so the resolved ``--model`` is observable. Bypasses _verify_setup_once
    (which would call shutil.which), stubs subprocess.run, and no-ops the
    ledger write."""
    monkeypatch.setattr(llm_client, "_setup_verified", True)
    monkeypatch.setattr(llm_client, "_claude_cli_path", r"C:\fake\claude.cmd")

    state: dict[str, Any] = {"cmd": None, "records": []}

    def _fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        state["cmd"] = cmd
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout=_good_cli_response(), stderr=""
        )

    monkeypatch.setattr(cli.subprocess, "run", _fake_run)
    monkeypatch.setattr(cli, "quota_block_active", lambda: None)
    monkeypatch.setattr(
        cli,
        "record_llm_call",
        lambda **kwargs: state["records"].append(kwargs),
    )
    yield state


def _model_arg(cmd: list[str]) -> str:
    """The value following `--model` in a captured CLI argv."""
    return cmd[cmd.index("--model") + 1]


def test_web_resolves_opus_pinned_purpose_when_model_omitted(
    capture_web_cmd: dict[str, Any],
) -> None:
    """A purpose with an Opus pin resolves to Opus when no explicit model is
    passed — the PR 3 plumbing enhancement. (material_news_classification is
    the exemplar since news_structuring's 2026-07-19 downgrade to the Sonnet
    default.)"""
    cli.call_llm_with_web(
        "prompt", purpose="material_news_classification", force_budget_bypass=True
    )
    cmd = capture_web_cmd["cmd"]
    assert cmd is not None
    assert _model_arg(cmd) == "claude-opus-4-8"
    # Web tools still wired (guards the cmd assembly against regression).
    assert "--allowedTools" in cmd


def test_web_explicit_model_overrides_purpose(
    capture_web_cmd: dict[str, Any],
) -> None:
    """Backward-compat: an explicit `model` arg still wins over purpose
    resolution, exactly as before the PR 3 change."""
    cli.call_llm_with_web(
        "prompt",
        model="claude-sonnet-4-6",
        purpose="material_news_classification",  # Opus-pinned, but the explicit model wins
        force_budget_bypass=True,
    )
    cmd = capture_web_cmd["cmd"]
    assert cmd is not None
    assert _model_arg(cmd) == "claude-sonnet-4-6"


def test_web_recent_developments_stays_on_sonnet(
    capture_web_cmd: dict[str, Any],
) -> None:
    """generate_recent_developments' purpose is pinned to DEFAULT_MODEL, so the
    new purpose-resolution keeps the brief on Sonnet (and avoids the
    unknown-purpose warning)."""
    cli.call_llm_with_web("prompt", purpose="recent_developments", force_budget_bypass=True)
    cmd = capture_web_cmd["cmd"]
    assert cmd is not None
    assert _model_arg(cmd) == cli.DEFAULT_MODEL


def test_web_no_purpose_no_model_defaults_to_sonnet(
    capture_web_cmd: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy omission keeps the model default but never produces NULL spend."""
    budget_purposes: list[str | None] = []

    def capture_budget(purpose: str | None, *, force_budget_bypass: bool) -> None:
        del force_budget_bypass
        budget_purposes.append(purpose)

    monkeypatch.setattr(
        cli,
        "_enforce_budget_pre_call",
        capture_budget,
    )

    cli.call_llm_with_web("prompt")
    cmd = capture_web_cmd["cmd"]
    assert cmd is not None
    assert _model_arg(cmd) == cli.DEFAULT_MODEL
    assert budget_purposes == ["__default__"]
    (record,) = capture_web_cmd["records"]
    assert record["purpose"] == "__default__"


def test_web_call_carries_hard_cost_cap(capture_web_cmd: dict[str, Any]) -> None:
    """The web path — the only agentic, multi-tool call — carries a HARD
    --max-budget-usd ceiling, so a model that ignores the prompt's advisory
    'AT MOST 2 web_search queries' budget still cannot run away on cost
    (the regrade memo's soft-caps gap). The CLI terminates the call at the cap."""
    cli.call_llm_with_web("prompt", purpose="news_structuring", force_budget_bypass=True)
    cmd = capture_web_cmd["cmd"]
    assert cmd is not None
    assert "--max-budget-usd" in cmd
    value = float(cmd[cmd.index("--max-budget-usd") + 1])
    assert value > 0


def test_web_success_records_transport_provenance(
    capture_web_cmd: dict[str, Any],
) -> None:
    cli.call_llm_with_web(
        "prompt",
        purpose="recent_developments",
        force_budget_bypass=True,
    )

    (record,) = capture_web_cmd["records"]
    assert record["provider"] == "anthropic"
    assert record["transport"] == "subscription_cli"
    assert record["attempts"] == 1
    assert record["retries"] == 0
    assert record["response_text"] == "ok https://example.test"


def test_web_failure_allows_plain_degradation_only_when_grounding_not_required(
    monkeypatch: pytest.MonkeyPatch,
    capture_web_cmd: dict[str, Any],
) -> None:
    def _failed_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=["claude"], timeout=1)

    monkeypatch.setattr(cli.subprocess, "run", _failed_run)
    monkeypatch.setattr(cli, "_call_claude", lambda *_args, **_kwargs: "plain fallback")

    result = cli.call_llm_with_web(
        "prompt",
        purpose="recent_developments",
        force_budget_bypass=True,
        require_grounding=False,
    )

    assert result == "plain fallback"
    (record,) = capture_web_cmd["records"]
    assert record["provider"] == "anthropic"
    assert record["transport"] == "subscription_cli"
    assert record["attempts"] == 1
    assert record["retries"] == 0
    assert record["failure_class"] == "timeout"
    assert record["error"].startswith("[timeout]")


def test_web_failure_does_not_return_plain_output_when_grounding_is_required(
    monkeypatch: pytest.MonkeyPatch,
    capture_web_cmd: dict[str, Any],
) -> None:
    def _failed_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=["claude"], timeout=1)

    monkeypatch.setattr(cli.subprocess, "run", _failed_run)
    monkeypatch.setattr(cli, "_call_claude", _fail_if_plain_transport_runs)

    with pytest.raises(subprocess.TimeoutExpired):
        cli.call_llm_with_web(
            "prompt",
            purpose="recent_developments",
            force_budget_bypass=True,
        )

    (record,) = capture_web_cmd["records"]
    assert record["failure_class"] == "timeout"


def test_web_quota_block_records_zero_attempt_provenance(
    monkeypatch: pytest.MonkeyPatch,
    capture_web_cmd: dict[str, Any],
) -> None:
    blocked_until = cli.datetime.now(UTC)
    monkeypatch.setattr(cli, "quota_block_active", lambda: blocked_until)

    with pytest.raises(cli.LLMQuotaExhausted):
        cli.call_llm_with_web(
            "prompt",
            purpose="recent_developments",
            force_budget_bypass=True,
        )

    assert capture_web_cmd["cmd"] is None
    (record,) = capture_web_cmd["records"]
    assert record["provider"] == "anthropic"
    assert record["transport"] == "subscription_cli"
    assert record["attempts"] == 0
    assert record["retries"] == 0
    assert record["failure_class"] == "quota_blocked"
    assert record["error"].startswith("[quota_blocked]")
