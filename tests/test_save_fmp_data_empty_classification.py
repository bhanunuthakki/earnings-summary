"""Unit tests for execution/save_fmp_data.py::fmp_call envelope handling.

Regression guard for the IPO-coverage bug: a /stable endpoint that returns
HTTP 200 with an empty array (`[]`) is *accessible but has no rows yet* — the
common case for a freshly-IPO'd ticker. The transport must return that exact
envelope and stop the fallback ladder so the shared pre-write contract can
reject it as `too_short`. Before the fix, fmp_call converted `[]` to a sentinel
that bypassed the validator.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_save_fmp_data():
    """Import execution/save_fmp_data.py with the FMP env vars isolated.

    - The module sys.exit(1)s at import if FMP_API_KEY is unset, so seed a dummy
      key (fmp_call's network layer is monkey-patched, so it's never used).
    - `_stable_only` is read from FMP_TIER at module load, and FMP_TIER=free can
      leak in from a .env before that read happens: llm_client.py's bare
      load_dotenv() (run when an earlier test in the same pytest process imports
      it) walks up past worktree roots to the main repo's .env, and the module's
      own load_dotenv(PROJECT_ROOT / ".env") hits it directly on a main
      checkout. Pin FMP_TIER to a paid tier for the exec so the v3/v4 fallback
      ladder these tests assert stays enabled (load_dotenv never overrides vars
      that are already set), then restore both vars — the module captures them
      into constants at import, so nothing re-reads the env afterwards.
    """
    src = PROJECT_ROOT / "execution" / "save_fmp_data.py"
    spec = importlib.util.spec_from_file_location("save_fmp_data", src)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["save_fmp_data"] = mod
    saved = {key: os.environ.get(key) for key in ("FMP_TIER", "FMP_API_KEY")}
    os.environ["FMP_TIER"] = "premium"
    os.environ.setdefault("FMP_API_KEY", "test-key-unused")
    try:
        spec.loader.exec_module(mod)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return mod


def test_stable_empty_array_returns_envelope_and_stops_ladder() -> None:
    mod = _load_save_fmp_data()
    calls: list[str] = []

    def fake_http_get(url: str, params: dict[str, object]) -> tuple[int, object | None, str | None]:
        calls.append(url)
        if "/stable/" in url:
            return (200, [], None)  # accessible, no rows yet
        return (403, None, "fmp-err: tier-restricted")

    mod._http_get = fake_http_get  # type: ignore[assignment]
    code, body, err, kind = mod.fmp_call("income-statement", "FRVO", {})

    assert code == 200
    assert body == []
    assert err is None
    assert kind == "stable:income-statement"
    # The ladder must STOP at the first /stable probe — no v3/v4 fallback calls.
    assert len(calls) == 1


def test_stable_with_data_returns_body_unchanged() -> None:
    """No regression for a fully-covered ticker (NSP-style): /stable 200 with
    rows returns the body on the first try, exactly as before."""
    mod = _load_save_fmp_data()
    payload = [{"date": "2025-12-31", "revenue": 100}]

    def fake_http_get(url: str, params: dict[str, object]) -> tuple[int, object | None, str | None]:
        if "/stable/" in url:
            return (200, payload, None)
        return (403, None, "tier")

    mod._http_get = fake_http_get  # type: ignore[assignment]
    code, body, err, kind = mod.fmp_call("income-statement", "NSP", {})

    assert code == 200
    assert body == payload
    assert err is None
    assert kind == "stable:income-statement"


def test_all_variants_403_returns_forbidden() -> None:
    """A genuinely tier-restricted endpoint (every variant 403s) still reports
    403 — the empty fix must not swallow real forbiddens."""
    mod = _load_save_fmp_data()

    def fake_http_get(url: str, params: dict[str, object]) -> tuple[int, object | None, str | None]:
        return (403, None, "fmp-err: tier-restricted")

    mod._http_get = fake_http_get  # type: ignore[assignment]
    code, body, _err, kind = mod.fmp_call("owner-earnings", "X", {})

    assert code == 403
    assert body is None
    assert kind is None


def test_empty_on_v3_fallback_returns_envelope_for_shared_validation() -> None:
    """If /stable 403s but a fallback variant returns 200-empty, the endpoint is
    accessible-but-empty, and its envelope must reach shared validation."""
    mod = _load_save_fmp_data()

    def fake_http_get(url: str, params: dict[str, object]) -> tuple[int, object | None, str | None]:
        if "/api/v3/" in url:
            return (200, [], None)
        return (403, None, "tier")

    mod._http_get = fake_http_get  # type: ignore[assignment]
    code, body, err, kind = mod.fmp_call("income-statement", "X", {})

    assert code == 200
    assert body == []
    assert err is None
    assert kind is not None and kind.startswith("v3-path:")


def test_run_ticker_stops_remaining_endpoints_after_429(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_save_fmp_data()
    jobs: list[dict[str, object]] = [
        {
            "path": endpoint,
            "symbol": "V",
            "period": "quarter",
            "suffix": endpoint.replace("-", "_"),
            "extra": {},
            "file_override": None,
        }
        for endpoint in ("income-statement", "balance-sheet-statement")
    ]
    calls: list[str] = []

    def fake_jobs(_symbol: str, list_type: str = "portfolio") -> list[dict[str, object]]:
        del list_type
        return jobs

    def quota_exhausted(
        endpoint: str, _ticker: str, _extra: dict[str, object]
    ) -> tuple[int, None, str, str]:
        calls.append(endpoint)
        return (429, None, "http: quota exhausted", f"stable:{endpoint}")

    def fake_list_type(_ticker: str) -> str:
        return "evaluation"

    def fake_flush(_rows: object) -> None:
        return None

    monkeypatch.setattr(mod, "per_ticker_jobs", fake_jobs)
    monkeypatch.setattr(mod, "_list_type_for", fake_list_type)
    monkeypatch.setattr(mod, "_flush_status_batch", fake_flush)
    monkeypatch.setattr(mod, "fmp_call", quota_exhausted)

    summary = mod.run_ticker("V")

    assert calls == ["income-statement"]
    assert summary["error"] == 1
    assert summary["skipped"] == 1
