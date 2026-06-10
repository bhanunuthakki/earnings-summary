"""Regression: the historical FMP fetcher retries transient 429/5xx responses.

Without retry a transient rate-limit (429) makes fetch_from_fmp return None and
the caller silently skips that statement (infra-sre L2 finding). Sibling
fetchers already back off; this one did not.
"""

from __future__ import annotations

import pathlib
import sys
import time

import pytest
import requests

_ROOT = pathlib.Path(__file__).resolve().parents[1]
for _p in (_ROOT / "src", _ROOT / "execution"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import fetch_fmp_historical_data as mod  # noqa: E402


class _FakeResp:
    def __init__(self, status_code: int, body: object) -> None:
        self.status_code = status_code
        self._body = body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self) -> object:
        return self._body


def _no_sleep(*_: object) -> None:
    return None


def test_fetch_from_fmp_retries_on_429_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = [_FakeResp(429, None), _FakeResp(200, [{"revenue": 1}])]
    state = {"n": 0}

    def _fake_get(url: str, params: object = None, timeout: object = None) -> _FakeResp:
        resp = responses[state["n"]]
        state["n"] += 1
        return resp

    monkeypatch.setattr(mod.requests, "get", _fake_get)
    monkeypatch.setattr(time, "sleep", _no_sleep)

    out = mod.fetch_from_fmp("income-statement", {"symbol": "AAA"})

    assert out == [{"revenue": 1}], "should return the 200 body after retrying the 429"
    assert state["n"] == 2, "should retry exactly once after the 429"
