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
  * no purpose + no model falls back to DEFAULT_MODEL (legacy behavior).
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from typing import Any

import pytest

import llm_client
from llm import cli


def _good_cli_response(text: str = "ok") -> str:
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


def _noop_record(**_kwargs: object) -> None:
    """Stub for record_llm_call so the post-call ledger write never reaches a
    real DB during these resolution tests."""


@pytest.fixture
def capture_web_cmd(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """Run call_llm_with_web fully offline, capturing the argv handed to the
    CLI so the resolved ``--model`` is observable. Bypasses _verify_setup_once
    (which would call shutil.which), stubs subprocess.run, and no-ops the
    ledger write."""
    monkeypatch.setattr(llm_client, "_setup_verified", True)
    monkeypatch.setattr(llm_client, "_claude_cli_path", r"C:\fake\claude.cmd")

    state: dict[str, Any] = {"cmd": None}

    def _fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        state["cmd"] = cmd
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout=_good_cli_response(), stderr=""
        )

    monkeypatch.setattr(cli.subprocess, "run", _fake_run)
    monkeypatch.setattr(cli, "record_llm_call", _noop_record)
    yield state


def _model_arg(cmd: list[str]) -> str:
    """The value following `--model` in a captured CLI argv."""
    return cmd[cmd.index("--model") + 1]


def test_web_resolves_opus_pinned_purpose_when_model_omitted(
    capture_web_cmd: dict[str, Any],
) -> None:
    """A purpose with an Opus pin (news_structuring) resolves to Opus when no
    explicit model is passed — the PR 3 plumbing enhancement."""
    cli.call_llm_with_web("prompt", purpose="news_structuring", force_budget_bypass=True)
    cmd = capture_web_cmd["cmd"]
    assert cmd is not None
    assert _model_arg(cmd) == "claude-opus-4-7"
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
        purpose="news_structuring",  # Opus-pinned, but the explicit model wins
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
) -> None:
    """No purpose and no model -> DEFAULT_MODEL, matching the prior hard default
    (full backward-compat for legacy callers)."""
    cli.call_llm_with_web("prompt", force_budget_bypass=True)
    cmd = capture_web_cmd["cmd"]
    assert cmd is not None
    assert _model_arg(cmd) == cli.DEFAULT_MODEL
