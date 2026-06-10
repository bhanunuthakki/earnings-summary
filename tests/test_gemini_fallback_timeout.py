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


class _FakeResponse:
    text = "fallback answer"


class _RecordingModel:
    """Stand-in for genai.GenerativeModel that records generate_content kwargs."""

    last_kwargs: dict[str, object] | None = None

    def __init__(self, model_id: str) -> None:
        self.model_id = model_id

    def generate_content(self, prompt: str, **kwargs: object) -> _FakeResponse:
        _RecordingModel.last_kwargs = dict(kwargs)
        return _FakeResponse()


def _noop(**_: object) -> None:
    return None


def test_gemini_fallback_supplies_a_positive_request_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("LLM_FALLBACK_DISABLED", raising=False)
    monkeypatch.setattr(fb.genai, "configure", _noop)
    monkeypatch.setattr(fb.genai, "GenerativeModel", _RecordingModel)
    _RecordingModel.last_kwargs = None

    out = fb.try_gemini_fallback("hello", RuntimeError("claude down"))

    assert out == "fallback answer"
    kwargs = _RecordingModel.last_kwargs
    assert kwargs is not None, "generate_content was never called"
    request_options = kwargs.get("request_options")
    assert isinstance(request_options, dict), f"no request_options passed: {kwargs!r}"
    timeout = request_options.get("timeout")
    assert isinstance(timeout, (int, float)) and timeout > 0, (
        f"fallback must pass a positive request timeout, got {timeout!r}"
    )
