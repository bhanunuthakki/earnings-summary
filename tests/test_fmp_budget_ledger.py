"""Ledger semantics of save_fmp_data's HTTP counters.

The daily budget file (`.tmp/cacher/budget_<date>.json`) must record only
attempts FMP actually SERVED. A 429 is a quota rejection, not consumption —
on the 2026-07-16 dead-quota morning the 03:00 cron logged 252 all-429
attempts into `calls_made`, so when the provider window reset at ~12:15 PT
the day's real drain fast-exited on "budget exhausted" without pulling
anything. These tests pin the split between _CALL_COUNTER (attempts, drives
per-run --max-calls caps) and _SERVED_COUNTER (spends the ledger).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# save_fmp_data hard-exits at import when the key is absent (cron safety);
# the tests never make real calls. Same pattern as test_fmp_tier_ladder.
os.environ.setdefault("FMP_API_KEY", "test-key-unused")

import execution.save_fmp_data as sfd  # noqa: E402


class _FakeResponse:
    def __init__(self, status_code: int, body: object = None, text: str = "") -> None:
        self.status_code = status_code
        self._body = body if body is not None else []
        self.text = text

    def json(self) -> object:
        return self._body


@pytest.fixture(autouse=True)
def _reset_counters(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sfd, "_CALL_COUNTER", 0)
    monkeypatch.setattr(sfd, "_SERVED_COUNTER", 0)
    monkeypatch.setattr(sfd._BUCKET, "acquire", lambda: None)
    monkeypatch.setattr(sfd.time, "sleep", lambda _s: None)
    yield


def _serve(monkeypatch: pytest.MonkeyPatch, responses: list[_FakeResponse]) -> None:
    it = iter(responses)

    def fake_get(url: str, params: dict[str, Any], timeout: object) -> _FakeResponse:
        return next(it)

    monkeypatch.setattr(sfd.SESSION, "get", fake_get)


def test_all_429s_count_zero_served(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dead-quota call burns retry attempts but must not spend the ledger."""
    _serve(
        monkeypatch,
        [_FakeResponse(429, {"Error Message": "Limit Reach"}) for _ in range(3)],
    )
    code, body, err = sfd._http_get("https://x/stable/profile", {})
    assert code == 429
    assert body is None and err is not None
    assert sfd._CALL_COUNTER == 3
    assert sfd._SERVED_COUNTER == 0


def test_429_then_success_counts_one_served(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, [_FakeResponse(429), _FakeResponse(200, [{"symbol": "NU"}])])
    code, body, err = sfd._http_get("https://x/stable/profile", {})
    assert (code, err) == (200, None)
    assert body == [{"symbol": "NU"}]
    assert sfd._CALL_COUNTER == 2
    assert sfd._SERVED_COUNTER == 1


def test_served_4xx_spends_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-429 errors were served by FMP and count against its quota."""
    _serve(monkeypatch, [_FakeResponse(403, {"Error Message": "Legacy"})])
    code, _body, _err = sfd._http_get("https://x/api/v3/profile", {})
    assert code == 403
    assert sfd._CALL_COUNTER == 1
    assert sfd._SERVED_COUNTER == 1


def test_network_error_counts_served_conservatively(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request that may have reached FMP overcounts rather than undercounts."""

    def raise_get(url: str, params: dict[str, Any], timeout: object) -> _FakeResponse:
        raise sfd.requests.ConnectionError("boom")

    monkeypatch.setattr(sfd.SESSION, "get", raise_get)
    code, _body, err = sfd._http_get("https://x/stable/profile", {})
    assert code == 0
    assert err is not None and err.startswith("network:")
    assert sfd._CALL_COUNTER == 1
    assert sfd._SERVED_COUNTER == 1
