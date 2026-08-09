"""Regression: the Gemini fallback must bound its call with a request timeout.

Without a timeout a hung Gemini request blocks an unattended pipeline forever
(infra-sre L2 finding). We assert the structural property — a positive request
timeout is supplied — not an exact value.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

# conftest is intentionally empty; put src on the path so `import llm.fallback`
# resolves whether or not pytest's pythonpath config is active.
_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import llm.fallback as fb  # noqa: E402


def test_gemini_fallback_supplies_a_positive_request_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llm import gemini_backend

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("LLM_FALLBACK_DISABLED", raising=False)
    captured: dict[str, object] = {}

    def _call(prompt: str, **kwargs: object) -> str:
        captured.update({"prompt": prompt, **kwargs})
        return "fallback answer"

    monkeypatch.setattr(gemini_backend, "call_gemini", _call)

    out = fb.try_gemini_fallback("hello", RuntimeError("claude down"))

    assert out == "fallback answer"
    assert captured["prompt"] == "hello"
    timeout = captured.get("timeout_seconds")
    assert isinstance(timeout, (int, float)) and timeout > 0, (
        f"fallback must pass a positive request timeout, got {timeout!r}"
    )


def test_gemini_fallback_redacts_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from llm import gemini_backend

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("LLM_FALLBACK_DISABLED", raising=False)

    def _fail(prompt: str, **kwargs: object) -> str:
        raise RuntimeError("request failed: https://example.test?api_key=secret-value")

    monkeypatch.setattr(gemini_backend, "call_gemini", _fail)

    with pytest.raises(RuntimeError) as caught:
        fb.try_gemini_fallback("hello", RuntimeError("claude down"))
    message = str(caught.value)
    assert "secret-value" not in message
    assert "api_key=***" in message
