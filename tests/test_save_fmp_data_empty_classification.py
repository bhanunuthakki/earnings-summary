"""Unit tests for execution/save_fmp_data.py::fmp_call empty-vs-forbidden logic.

Regression guard for the IPO-coverage bug: a /stable endpoint that returns
HTTP 200 with an empty array (`[]`) is *accessible but has no rows yet* — the
common case for a freshly-IPO'd ticker. It must be recorded as `empty`, NOT
`forbidden`. Before the fix, fmp_call continued the try-ladder past the
200-empty and let the v3/v4 403 fallback overwrite the status (and burned 2
extra calls per empty endpoint).
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_save_fmp_data():
    """Import execution/save_fmp_data.py. The module sys.exit(1)s at import if
    FMP_API_KEY is unset, so seed a dummy key first (fmp_call's network layer is
    monkey-patched, so the key is never actually used)."""
    os.environ.setdefault("FMP_API_KEY", "test-key-unused")
    src = PROJECT_ROOT / "execution" / "save_fmp_data.py"
    spec = importlib.util.spec_from_file_location("save_fmp_data", src)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["save_fmp_data"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_stable_empty_array_records_empty_and_stops_ladder() -> None:
    mod = _load_save_fmp_data()
    calls: list[str] = []

    def fake_http_get(url, params):
        calls.append(url)
        if "/stable/" in url:
            return (200, [], None)  # accessible, no rows yet
        return (403, None, "fmp-err: tier-restricted")

    mod._http_get = fake_http_get  # type: ignore[assignment]
    code, body, err, kind = mod.fmp_call("income-statement", "FRVO", {})

    assert code == 200
    assert body is None
    assert err == "empty-list"
    assert kind == "stable:income-statement"
    # The ladder must STOP at the first /stable probe — no v3/v4 fallback calls.
    assert len(calls) == 1


def test_stable_with_data_returns_body_unchanged() -> None:
    """No regression for a fully-covered ticker (NSP-style): /stable 200 with
    rows returns the body on the first try, exactly as before."""
    mod = _load_save_fmp_data()
    payload = [{"date": "2025-12-31", "revenue": 100}]

    def fake_http_get(url, params):
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

    def fake_http_get(url, params):
        return (403, None, "fmp-err: tier-restricted")

    mod._http_get = fake_http_get  # type: ignore[assignment]
    code, body, _err, kind = mod.fmp_call("owner-earnings", "X", {})

    assert code == 403
    assert body is None
    assert kind is None


def test_empty_on_v3_fallback_still_classifies_empty() -> None:
    """If /stable 403s but a fallback variant returns 200-empty, the endpoint is
    accessible-but-empty — empty must win over the earlier 403."""
    mod = _load_save_fmp_data()

    def fake_http_get(url, params):
        if "/api/v3/" in url:
            return (200, [], None)
        return (403, None, "tier")

    mod._http_get = fake_http_get  # type: ignore[assignment]
    code, body, err, kind = mod.fmp_call("income-statement", "X", {})

    assert code == 200
    assert body is None
    assert err == "empty-list"
    assert kind is not None and kind.startswith("v3-path:")


def test_run_ticker_branch_classifies_empty_as_empty(monkeypatch) -> None:
    """End-to-end at the classification layer: a 200-empty fmp_call result lands
    in the `empty` summary bucket (not `forbidden`)."""
    mod = _load_save_fmp_data()

    # Single tiny job so the runner does one fmp_call.
    monkeypatch.setattr(
        mod, "per_ticker_jobs",
        lambda symbol, list_type="portfolio": [
            {"path": "income-statement", "symbol": symbol, "period": "quarter",
             "suffix": "income_statement_quarterly", "extra": {}, "file_override": None}
        ],
    )
    monkeypatch.setattr(mod, "_list_type_for", lambda ticker: "evaluation")
    monkeypatch.setattr(mod, "_flush_status_batch", lambda rows: None)
    monkeypatch.setattr(mod, "fmp_call",
                        lambda endpoint, ticker, extra: (200, None, "empty-list", "stable:income-statement"))

    summary = mod.run_ticker("FRVO")
    assert summary["empty"] == 1
    assert summary["forbidden"] == 0
    assert summary["ok"] == 0
