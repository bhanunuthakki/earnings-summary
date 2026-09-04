# pyright: reportPrivateUsage=false
"""Regression tests for Claude Code subprocess tool isolation (SEC-LLM-001)."""

from __future__ import annotations

import io
import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import ask.context as ask_context
import llm_call_ledger
import llm_client
from ask.narrative_transport import stream_llm_text
from llm import cli


def _json_result(text: str = "answer https://example.test") -> str:
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": text,
            "total_cost_usd": 0.001,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
    )


def _no_op(**_kwargs: object) -> None:
    pass


@pytest.fixture()
def capture_run(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[list[str]]]:
    """Capture buffered Claude argv without invoking a provider or ledger."""
    monkeypatch.setattr(llm_client, "_setup_verified", True)
    monkeypatch.setattr(llm_client, "_claude_cli_path", "/fake/claude")
    monkeypatch.setattr(cli, "quota_block_active", lambda: None)
    monkeypatch.setattr(cli, "record_llm_call", _no_op)
    monkeypatch.setattr(cli, "capture_exchange", _no_op)

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, _json_result(), "")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    yield calls


def test_plain_claude_disables_tools_and_customizations(
    capture_run: list[list[str]],
) -> None:
    cli._call_claude(
        "prompt",
        purpose="bear_case",
        force_budget_bypass=True,
        allow_codex_fallback=False,
    )

    (cmd,) = capture_run
    assert cmd[cmd.index("--safe-mode") : cmd.index("--safe-mode") + 3] == [
        "--safe-mode",
        "--tools",
        "",
    ]
    assert "--allowedTools" not in cmd


def test_web_claude_restricts_tools_and_keeps_budget_controls(
    monkeypatch: pytest.MonkeyPatch,
    capture_run: list[list[str]],
) -> None:
    monkeypatch.setenv("LLM_PRIMARY_SUBSCRIPTION_BACKEND", "claude")

    cli.call_llm_with_web("prompt", purpose="recent_developments", force_budget_bypass=True)

    (cmd,) = capture_run
    safe_mode = cmd.index("--safe-mode")
    assert cmd[safe_mode : safe_mode + 4] == [
        "--safe-mode",
        "--tools",
        "WebSearch",
        "WebFetch",
    ]
    allowed = cmd.index("--allowedTools")
    assert cmd[allowed + 1 : allowed + 3] == ["WebSearch", "WebFetch"]
    assert "--max-budget-usd" in cmd


class _Stdin:
    def write(self, _data: str) -> None:
        pass

    def close(self) -> None:
        pass


class _Proc:
    def __init__(self) -> None:
        self.stdin = _Stdin()
        self.stdout: Iterator[str] = iter(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "answer"}]},
                    }
                ),
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "result": "ignored",
                    }
                ),
            ]
        )
        self.stderr = io.StringIO("")
        self.returncode: int | None = 0

    def poll(self) -> int:
        return self.returncode or 0

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return self.returncode or 0

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


def test_streaming_ask_is_tool_free_even_when_override_requests_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(cli.PRIMARY_SUBSCRIPTION_BACKEND_ENV_VAR, "claude")

    def fake_model_for(_purpose: str) -> str:
        return cli.DEFAULT_MODEL

    def fake_enforce(*_args: object, **_kwargs: object) -> None:
        pass

    def fake_which(_name: str) -> str:
        return "/fake/claude"

    def fake_record(*_args: object, **_kwargs: object) -> int:
        return 1

    monkeypatch.setattr(cli, "_model_for", fake_model_for)
    monkeypatch.setattr(cli, "_enforce_budget_pre_call", fake_enforce)
    monkeypatch.setattr(cli.shutil, "which", fake_which)
    monkeypatch.setattr(llm_call_ledger, "record_call", fake_record)
    monkeypatch.setattr(cli, "capture_exchange", _no_op)
    captured: list[list[str]] = []

    def fake_popen(cmd: list[str], **_kwargs: Any) -> _Proc:
        captured.append(cmd)
        return _Proc()

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)

    events = list(
        cli.stream_llm(
            "prompt",
            purpose="ask_answer",
            allowed_tools=("Read", "Bash", "mcp__dangerous"),
        )
    )

    assert events[-1] == {"type": "final", "text": "answer"}
    (cmd,) = captured
    safe_mode = cmd.index("--safe-mode")
    assert cmd[safe_mode : safe_mode + 3] == ["--safe-mode", "--tools", ""]
    assert "--allowedTools" not in cmd


def test_ask_default_does_not_invite_read_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, object] = {}

    def fake_stream(
        _prompt: str,
        *,
        purpose: str,
        scope: str,
        allowed_tools: tuple[str, ...] = (),
    ) -> Iterator[dict[str, object]]:
        observed["purpose"] = purpose
        observed["scope"] = scope
        observed["allowed_tools"] = allowed_tools
        yield {"type": "final", "text": "ok"}

    monkeypatch.setattr(cli, "stream_llm", fake_stream)
    list(stream_llm_text("prompt", allow_read=True))

    assert observed == {"purpose": "ask_answer", "scope": "ask", "allowed_tools": ()}


def test_portfolio_prompt_does_not_invite_unavailable_read_tool() -> None:
    prompt = ask_context._portfolio_system_context(Path("/tmp/unused"), {})

    assert "Read tool" not in prompt
