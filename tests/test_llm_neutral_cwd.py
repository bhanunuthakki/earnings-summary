"""Both LLM subprocess paths run the `claude -p` CLI from a NEUTRAL cwd + env.

The plain path (`call_llm` -> `_call_claude`) and the WebSearch/WebFetch path
(`call_llm_with_web`) both shell out to `claude -p`. If that subprocess inherits
a cwd containing the project's `.mcp.json`, the CLI tries to boot every
configured MCP server on startup and hangs (~5 min, then killed). Both paths
must therefore run from a neutral directory that has no `.mcp.json`.

This is the regression guard for that invariant. The web path historically
passed no `cwd`, so it inherited the caller's — and a caller whose cwd was the
repo root (e.g. `build_artifacts` launched by `refresh_dispatch.py` for the §8
"Recent Developments" web brief) would hang. WebSearch and WebFetch are
built-in Claude tools that do not need the project `.mcp.json`, so the neutral
cwd does not break the web path.

The env is neutralized for the same reason the cwd is (2026-07 incident): an
ambient `ANTHROPIC_BASE_URL` — a User-scope leftover from the interactive
Headroom-proxy wrapper, or the process-scoped one inherited when the server is
launched from inside a `ch` session — silently reroutes every pipeline call to
a proxy that is usually not running. Both paths must strip it; rerouting this
app's transport is opt-in via `ES_CLAUDE_BASE_URL` only.

The test stays fully offline: setup verification is bypassed, subprocess.run is
stubbed (capturing the kwargs handed to it), and the ledger write is no-oped.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator
from typing import Any

import pytest

import llm_client
from llm import cli


def _good_cli_response(text: str = "ok https://example.test") -> str:
    """Minimal `claude -p --output-format json` success envelope so neither path
    falls through to the Gemini fallback."""
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
    real DB during this test."""


@pytest.fixture
def capture_run_calls(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    """Run LLM calls fully offline, capturing the kwargs handed to each
    subprocess.run invocation so `cwd` is observable. Bypasses _verify_setup_once
    (which would call shutil.which), stubs subprocess.run with a success
    envelope, and no-ops the ledger write."""
    monkeypatch.setattr(llm_client, "_setup_verified", True)
    monkeypatch.setattr(llm_client, "_claude_cli_path", r"C:\fake\claude.cmd")

    calls: list[dict[str, Any]] = []

    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout=_good_cli_response(), stderr=""
        )

    monkeypatch.setattr(cli.subprocess, "run", _fake_run)
    monkeypatch.setattr(cli, "record_llm_call", _noop_record)
    yield calls


def test_both_paths_run_in_neutral_cwd(capture_run_calls: list[dict[str, Any]]) -> None:
    """The plain (`call_llm` -> `_call_claude`) and web (`call_llm_with_web`)
    paths both run `claude -p` from the SAME neutral cwd — an existing temp dir
    with no project `.mcp.json`, so the nested CLI never boots the project's MCP
    servers and hangs. Guards against the web path diverging from the plain path
    (its historical bug: passing no `cwd` and inheriting the caller's)."""
    # Plain path (exercised via the public wrapper, which delegates to
    # _call_claude) then the web path. Each appends one subprocess.run call.
    cli.call_llm("prompt", purpose="bear_case", force_budget_bypass=True)
    cli.call_llm_with_web("prompt", purpose="bear_case", force_budget_bypass=True)

    assert len(capture_run_calls) == 2
    plain_cwd = capture_run_calls[0].get("cwd")
    web_cwd = capture_run_calls[1].get("cwd")

    # Both paths pass a cwd, and the SAME one — they share _neutral_subprocess_cwd().
    assert plain_cwd is not None, "plain path passed no cwd to subprocess.run"
    assert web_cwd is not None, "web path passed no cwd to subprocess.run"
    assert web_cwd == plain_cwd

    # The cwd is neutral: an existing directory with no project `.mcp.json`, so
    # the nested `claude -p` doesn't boot MCP servers. (The repo root — the cwd a
    # caller like refresh_dispatch.py would otherwise leak in — DOES have one.)
    assert os.path.isdir(web_cwd)
    assert not os.path.exists(os.path.join(web_cwd, ".mcp.json"))


def test_both_paths_strip_ambient_anthropic_base_url(
    capture_run_calls: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An inherited ANTHROPIC_BASE_URL (User-scope leftover or a `ch`-session
    leak) must NOT reach the `claude -p` subprocess — it reroutes every
    pipeline call to the Headroom proxy, which is usually not running
    (the 2026-07-03..13 all-purposes outage)."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:8787")
    monkeypatch.delenv(cli.ES_CLAUDE_BASE_URL_VAR, raising=False)
    monkeypatch.setattr(cli, "_stripped_base_url_logged", False)

    cli.call_llm("prompt", purpose="bear_case", force_budget_bypass=True)
    cli.call_llm_with_web("prompt", purpose="bear_case", force_budget_bypass=True)

    assert len(capture_run_calls) == 2
    for kwargs in capture_run_calls:
        env = kwargs.get("env")
        assert env is not None, "path passed no env to subprocess.run"
        assert "ANTHROPIC_BASE_URL" not in env
        # The rest of the environment still flows through (auth, PATH, etc.).
        assert "PATH" in env or "Path" in env


def test_explicit_es_claude_base_url_is_honored(
    capture_run_calls: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rerouting this app's transport is an explicit app-level decision:
    ES_CLAUDE_BASE_URL is re-pinned onto ANTHROPIC_BASE_URL for the subprocess,
    and wins over any ambient value."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:8787")
    monkeypatch.setenv(cli.ES_CLAUDE_BASE_URL_VAR, "https://gateway.example/claude")

    cli.call_llm("prompt", purpose="bear_case", force_budget_bypass=True)

    env = capture_run_calls[0].get("env")
    assert env is not None
    assert env["ANTHROPIC_BASE_URL"] == "https://gateway.example/claude"
