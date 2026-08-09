# pyright: reportPrivateUsage=false
# This suite exercises module-private ladder internals (_candidates, _http_get,
# _stable_only, _fmp_get, _run_under_lock) by design.
"""Tier-aware ladder tests for the FMP /api/v3 -> /stable migration (plan 7.1).

On the free tier every /api/v3 and /api/v4 call 403s (v3 deprecated 2025-08-31),
so when FMP_TIER=free (or --stable-only) the try-ladder must drop to the stable
rung + stable aliases only and NEVER request a v3/v4 URL. Legacy/paid tiers keep
the v3/v4 fallback. The cacher propagates the resolved FMP_TIER to the fetcher
subprocess so the cutover is one deliberate flip.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
from datetime import datetime
from pathlib import Path

import pytest

os.environ.setdefault("FMP_API_KEY", "test-key-unused")

import execution.fetch_etf_data as etf
import execution.refresh_cache as rc
import execution.save_fmp_data as sfd
from net.client import HttpCallError, HttpErrorKind, HttpJsonResponse

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# _candidates — stable-only drops v3/v4 but keeps stable aliases (plan 7.1)
# ---------------------------------------------------------------------------


def test_candidates_stable_only_drops_v3_v4(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sfd, "_stable_only", True)
    # symbol'd endpoint
    kinds = [k for k, _u, _p in sfd._candidates("income-statement", "GOOGL", {})]
    assert kinds == ["stable:income-statement"]
    # symbol-less endpoint (other branch of _candidates)
    kinds2 = [k for k, _u, _p in sfd._candidates("financial-scores", None, {})]
    assert kinds2 and all(k.startswith("stable:") for k in kinds2)


def test_candidates_stable_only_keeps_stable_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    """cashflow-statement has a stable-to-stable alias (cash-flow-statement); the
    alias survives the gate, only the v3/v4 rungs are dropped."""
    monkeypatch.setattr(sfd, "_stable_only", True)
    kinds = [k for k, _u, _p in sfd._candidates("cashflow-statement", "GOOGL", {})]
    assert kinds == ["stable:cashflow-statement", "stable:cash-flow-statement"]


def test_candidates_legacy_emits_v3_v4(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sfd, "_stable_only", False)
    kinds = [k for k, _u, _p in sfd._candidates("income-statement", "GOOGL", {})]
    assert "stable:income-statement" in kinds
    assert "v3-path:income-statement" in kinds
    assert "v4-query:income-statement" in kinds


# ---------------------------------------------------------------------------
# fmp_call — NO v3/v4 URL is ever requested on the free tier (plan 7.1 headline)
# ---------------------------------------------------------------------------


def _collect_requested_urls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    requested: list[str] = []

    def fake_http_get(url: str, params: dict[str, object]) -> tuple[int, None, str]:
        requested.append(url)
        return (403, None, "tier-restricted")  # force the ladder to exhaust every rung

    monkeypatch.setattr(sfd, "_http_get", fake_http_get)
    return requested


def test_fmp_call_stable_only_requests_no_v3_v4(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sfd, "_stable_only", True)
    requested = _collect_requested_urls(monkeypatch)
    sfd.fmp_call("income-statement", "GOOGL", {})
    assert requested, "the stable rung must still be requested"
    assert all("/stable/" in u for u in requested)
    assert not any("/api/v3/" in u or "/api/v4/" in u for u in requested)


def test_fmp_call_legacy_requests_v3_and_v4_on_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sfd, "_stable_only", False)
    requested = _collect_requested_urls(monkeypatch)
    sfd.fmp_call("income-statement", "GOOGL", {})
    assert any("/api/v3/" in u for u in requested)
    assert any("/api/v4/" in u for u in requested)


# ---------------------------------------------------------------------------
# FMP_TIER=free enables stable-only at import (deliberate flip, env-driven)
# ---------------------------------------------------------------------------


def test_env_fmp_tier_free_enables_stable_only_at_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FMP_TIER", "free")
    monkeypatch.setenv("FMP_API_KEY", "test-key-unused")
    spec = importlib.util.spec_from_file_location(
        "sfd_free_probe", PROJECT_ROOT / "execution" / "save_fmp_data.py"
    )
    assert spec is not None and spec.loader is not None
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    assert fresh._stable_only is True
    kinds = [k for k, _u, _p in fresh._candidates("income-statement", "G", {})]
    assert kinds == ["stable:income-statement"]


# ---------------------------------------------------------------------------
# fetch_etf_data — same gate on its stable->v3 ladder
# ---------------------------------------------------------------------------


def test_etf_fmp_get_stable_only_skips_v3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(etf, "_STABLE_ONLY", True)
    requested: list[str] = []

    def fake_get(url: str, **_kwargs: object) -> HttpJsonResponse:
        requested.append(url)
        return HttpJsonResponse(status_code=200, payload=[{"ok": 1}])

    monkeypatch.setattr(etf.FMP_CLIENT, "get_url_json", fake_get)
    etf._fmp_get("key", "SOXX", "etf/info")
    assert requested == [f"{etf.FMP_BASE}/stable/etf/info/SOXX"]
    assert not any("/api/v3/" in url for url in requested)


def test_etf_fmp_get_legacy_falls_back_to_v3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(etf, "_STABLE_ONLY", False)
    requested: list[str] = []

    def fake_get(url: str, **_kwargs: object) -> HttpJsonResponse:
        requested.append(url)
        if "/stable/" in url:
            raise HttpCallError(
                kind=HttpErrorKind.CLIENT,
                message="HTTP 404",
                retryable=False,
                status_code=404,
            )
        return HttpJsonResponse(status_code=200, payload=[{"ok": 1}])

    monkeypatch.setattr(etf.FMP_CLIENT, "get_url_json", fake_get)
    etf._fmp_get("key", "SOXX", "etf/info")
    assert any("/api/v3/" in url for url in requested)


# ---------------------------------------------------------------------------
# refresh_cache — free tier registered + FMP_TIER propagated to the subprocess
# ---------------------------------------------------------------------------


def test_free_tier_registered() -> None:
    assert "free" in rc.TIERS
    assert rc.TIERS["free"].calls_per_day == 250
    assert rc.resolve_tier("free").name == "free"


def test_run_under_lock_propagates_fmp_tier_to_subprocess(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The cutover mechanism: a free-tier cacher run propagates FMP_TIER=free to
    the save_fmp_data subprocess, which makes its ladder go stable-only."""
    monkeypatch.setattr(rc, "_maybe_refresh_earnings_hints", lambda: None)
    cache_dir = tmp_path / "cacher"
    cache_dir.mkdir()
    # PROJECT_ROOT is repointed at tmp_path so _run_under_lock's
    # log_path.relative_to(PROJECT_ROOT) resolves for the tmp CACHE_DIR.
    monkeypatch.setattr(rc, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(rc, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(rc, "QUEUE_PATH", cache_dir / "queue.json")

    item = rc.QueueItem(
        ticker="GOOGL",
        list_type="portfolio",
        endpoint="income-statement",
        period="annual",
        suffix="income_statement_annual",
        endpoint_class="statement",
        bucket="missing",
        last_pulled=None,
        last_status=None,
        days_overdue=0,
        priority=0,
    )
    report = rc.AuditReport(generated_at=datetime(2026, 6, 1, 12, 0, 0), items=[item], counts={})

    def fake_audit(*_a: object, **_k: object) -> rc.AuditReport:
        return report

    monkeypatch.setattr(rc, "audit", fake_audit)

    captured: dict[str, object] = {}

    class _FakeProc:
        returncode = 0

    def fake_run(cmd: list[str], **kwargs: object) -> _FakeProc:
        captured["env"] = kwargs.get("env")
        return _FakeProc()

    monkeypatch.setattr(rc.subprocess, "run", fake_run)

    db_path = tmp_path / "portfolio.db"
    db_path.touch()
    args = argparse.Namespace(
        tier="free",
        db=str(db_path),
        only=None,
        tickers=None,
        force=False,
        max_calls=None,
        dry_run=False,
    )
    rc._run_under_lock(args)

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["FMP_TIER"] == "free"
    assert env["FMP_RATE_LIMIT_PER_SEC"] == str(rc.TIERS["free"].calls_per_sec)
