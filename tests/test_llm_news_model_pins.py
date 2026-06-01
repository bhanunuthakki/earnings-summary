"""Model-pin + web-resolution tests for the news/material-news LLM purposes.

Two contracts (plan §3.3, §6.8):

  * **LLM_MODELS pins** — ``material_news_classification`` and
    ``news_structuring`` resolve to Opus (``claude-opus-4-7``); the
    ``recent_developments`` web brief stays on ``DEFAULT_MODEL``.

  * **call_llm_with_web purpose->model resolution** — with ``model`` omitted the
    web path now resolves the model from ``purpose`` (mirroring ``call_llm``), so
    the news-structuring fallback gets Opus without threading a model id through.
    An explicit ``model`` still overrides.

The web test stubs the claude CLI subprocess (no real LLM) the same way
``test_llm_client_budget_integration`` does, and asserts on the ``--model``
argument the resolver placed in the command line.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from typing import Any, cast

import pytest

import llm_client
from llm import cli
from llm.cli import DEFAULT_MODEL, LLM_MODELS, call_llm_with_web

_OPUS = "claude-opus-4-7"


# ---------------------------------------------------------------------------
# LLM_MODELS pins (the registration — _model_for just reads this dict)
# ---------------------------------------------------------------------------


def test_material_news_classification_pinned_to_opus() -> None:
    assert LLM_MODELS["material_news_classification"] == _OPUS


def test_news_structuring_pinned_to_opus() -> None:
    assert LLM_MODELS["news_structuring"] == _OPUS


def test_recent_developments_stays_on_default_model() -> None:
    # Pinned explicitly so the web path's new purpose-resolution keeps it on
    # Sonnet rather than warning + defaulting.
    assert LLM_MODELS["recent_developments"] == DEFAULT_MODEL


# ---------------------------------------------------------------------------
# call_llm_with_web purpose -> model resolution
# ---------------------------------------------------------------------------


def _good_cli_response(text: str = "ok") -> str:
    """Mimic the `claude -p --output-format json` envelope."""
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


def _noop(*_a: object, **_k: object) -> None:
    """Typed no-op standing in for the budget gate / ledger write."""


@pytest.fixture
def captured_web_cmd(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """Stub the web CLI call and capture the argv. Bypasses setup verification,
    the budget gate, and the ledger write so the test is hermetic — only the
    model-resolution path is exercised."""
    monkeypatch.setattr(llm_client, "_setup_verified", True)
    monkeypatch.setattr(llm_client, "_claude_cli_path", r"C:\fake\claude.cmd")
    monkeypatch.setattr(cli, "_enforce_budget_pre_call", _noop)
    monkeypatch.setattr(cli, "record_llm_call", _noop)

    state: dict[str, Any] = {"cmd": None}

    def _fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        cmd = cast("list[str]", args[0]) if args else cast("list[str]", kwargs["args"])
        state["cmd"] = list(cmd)
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout=_good_cli_response("ok"), stderr=""
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)
    yield state


def _model_arg(cmd: list[str]) -> str:
    """The token following ``--model`` in the captured argv."""
    return cmd[cmd.index("--model") + 1]


def test_web_resolves_opus_from_purpose_when_model_omitted(
    captured_web_cmd: dict[str, Any],
) -> None:
    out = call_llm_with_web("prompt", purpose="news_structuring")
    assert out == "ok"
    assert _model_arg(cast("list[str]", captured_web_cmd["cmd"])) == _OPUS


def test_web_explicit_model_overrides_purpose(captured_web_cmd: dict[str, Any]) -> None:
    # The escape hatch: an explicit model wins even when a purpose is pinned.
    _ = call_llm_with_web("prompt", model=DEFAULT_MODEL, purpose="news_structuring")
    assert _model_arg(cast("list[str]", captured_web_cmd["cmd"])) == DEFAULT_MODEL


def test_web_recent_developments_resolves_to_default(captured_web_cmd: dict[str, Any]) -> None:
    # Backward-compat: the existing web caller keeps its Sonnet model.
    _ = call_llm_with_web("prompt", purpose="recent_developments")
    assert _model_arg(cast("list[str]", captured_web_cmd["cmd"])) == DEFAULT_MODEL
