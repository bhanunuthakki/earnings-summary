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

import hashlib
import importlib.util
import os
import sqlite3
import sys
from datetime import datetime, timedelta
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


def test_fmp_provider_error_body_is_redacted_before_fetcher_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "runtime-secret-canary"

    def rejected(*_args: object, **_kwargs: object) -> HttpJsonResponse:
        raise HttpCallError(
            kind=HttpErrorKind.AUTH,
            message="HTTP 401",
            retryable=False,
            status_code=401,
            payload={"Error Message": f"invalid apikey={canary}"},
        )

    monkeypatch.setattr(sfd.FMP_CLIENT, "get_url_json", rejected)

    _code, _body, error = sfd._http_get(
        "https://financialmodelingprep.com/stable/profile",
        {"symbol": "RBRK"},
    )

    assert error is not None
    assert canary not in error


def test_typed_403_receipt_distinguishes_endpoint_from_account_failure() -> None:
    assert (
        sfd._forbidden_outcome("Restricted endpoint: upgrade your plan")
        is sfd.FmpWorkReceiptOutcome.ENDPOINT_FORBIDDEN
    )
    assert (
        sfd._forbidden_outcome("Invalid API key for this account")
        is sfd.FmpWorkReceiptOutcome.ACCOUNT_FORBIDDEN
    )
    assert (
        sfd._forbidden_outcome(
            "Restricted Endpoint: This endpoint is not available under your current subscription."
        )
        is sfd.FmpWorkReceiptOutcome.ENDPOINT_FORBIDDEN
    )
    assert (
        sfd._forbidden_outcome("Invalid API KEY. Please create a Free API Key.")
        is sfd.FmpWorkReceiptOutcome.ACCOUNT_FORBIDDEN
    )


@pytest.mark.parametrize(
    "receipt_outcome,expected",
    [
        (sfd.FmpWorkReceiptOutcome.EMPTY, rc.OutcomeCode.ENDPOINT_EMPTY),
        (sfd.FmpWorkReceiptOutcome.CONTRACT_ERROR, rc.OutcomeCode.CLIENT_CONTRACT_ERROR),
    ],
)
def test_dispatch_preserves_typed_empty_and_contract_outcomes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    receipt_outcome: sfd.FmpWorkReceiptOutcome,
    expected: rc.OutcomeCode,
) -> None:
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(rc, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(rc, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(rc, "FMP_DIR", tmp_path / "data" / "historical" / "fmp")
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    item = rc.QueueItem(
        ticker="RBRK",
        list_type="portfolio",
        endpoint="income-statement",
        period="quarter",
        suffix="income_statement_quarterly",
        endpoint_class="statement",
        bucket="missing",
        last_pulled=None,
        last_status=None,
        days_overdue=0,
        priority=0,
    )
    planned = rc.PlannedWork(
        work_id="a" * 64,
        ticker="RBRK",
        priority=300,
        endpoint_key="income_statement_quarterly",
        period_key="quarter",
        cache_generation_id="refresh:2026-08-12",
        policy_sha256="b" * 64,
        execution_mode=rc.ExecutionMode.LIVE,
        lease_token="lease-token",
        lease_expires_at=rc._utc_now() + timedelta(minutes=5),
    )

    class _FakeProc:
        returncode = 1

    def fake_run(cmd: list[str], **_kwargs: object) -> _FakeProc:
        receipt_path = Path(cmd[cmd.index("--work-receipt") + 1])
        sfd._write_work_receipt(
            receipt_path,
            sfd.FmpWorkReceipt(
                ticker="RBRK",
                endpoint="income-statement",
                period="quarter",
                outcome=receipt_outcome,
                http_status=200,
                captured_at=rc._utc_now(),
            ),
        )
        return _FakeProc()

    monkeypatch.setattr(rc.subprocess, "run", fake_run)
    outcome = rc._dispatch_one(
        connection,
        item,
        planned,
        tier=rc.TIERS["basic"],
        auth=rc.FmpAuthConfig(api_key="test-key", source="environment"),
        db_path=tmp_path / "portfolio.db",
        log_path=cache_dir / "dispatch.log",
    )
    connection.close()
    assert outcome.outcome_code is expected
    assert outcome.http_status == 200


def test_single_work_fetch_returns_nonzero_when_typed_receipt_is_all_failed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        '{"items":[{"ticker":"RBRK","endpoint":"income-statement","period":"quarter"}]}',
        encoding="utf-8",
    )
    receipt_path = tmp_path / "receipt.json"
    monkeypatch.setattr(sfd, "FMP_DIR", tmp_path / "fmp")
    monkeypatch.setattr(sfd, "SNAP_DIR", tmp_path / "snap")
    monkeypatch.setattr(sfd, "SECTOR_DIR", tmp_path / "sector")
    monkeypatch.setattr(sfd, "_BUDGET_DIR", tmp_path / "budget")

    def empty_snapshot_index() -> dict[str, str]:
        return {}

    monkeypatch.setattr(sfd, "_load_snapshot_index", empty_snapshot_index)

    def failed_ticker(
        ticker: str,
        **kwargs: object,
    ) -> dict[str, int]:
        receipts = kwargs["work_receipts"]
        assert isinstance(receipts, list)
        receipts.append(
            sfd.FmpWorkReceipt(
                ticker=ticker,
                endpoint="income-statement",
                period="quarter",
                outcome=sfd.FmpWorkReceiptOutcome.ACCOUNT_FORBIDDEN,
                http_status=403,
                captured_at=sfd._utc_now(),
            )
        )
        return {"ok": 0, "empty": 0, "forbidden": 1, "error": 0, "skipped": 0, "total": 1}

    monkeypatch.setattr(sfd, "run_ticker", failed_ticker)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "save_fmp_data.py",
            "--manifest",
            str(manifest),
            "--work-receipt",
            str(receipt_path),
        ],
    )
    assert sfd.main() == 1
    receipt = sfd.FmpWorkReceipt.model_validate_json(receipt_path.read_text(encoding="utf-8"))
    assert receipt.outcome is sfd.FmpWorkReceiptOutcome.ACCOUNT_FORBIDDEN


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


def test_fmp_call_stops_variant_ladder_after_exhausted_429(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sfd, "_stable_only", False)
    requested: list[str] = []

    def quota_exhausted(url: str, _params: dict[str, object]) -> tuple[int, None, str]:
        requested.append(url)
        return (429, None, "http: quota exhausted")

    monkeypatch.setattr(sfd, "_http_get", quota_exhausted)
    code, body, error, kind = sfd.fmp_call("cashflow-statement", "V", {})

    assert (code, body, error) == (429, None, "http: quota exhausted")
    assert kind == "stable:cashflow-statement"
    assert len(requested) == 1


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


def test_recovery_dispatch_propagates_fmp_tier_to_subprocess(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The cutover mechanism: a free-tier cacher run propagates FMP_TIER=free to
    the save_fmp_data subprocess, which makes its ladder go stable-only."""
    cache_dir = tmp_path / "cacher"
    cache_dir.mkdir()
    monkeypatch.setattr(rc, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(rc, "CACHE_DIR", cache_dir)

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
    captured: dict[str, object] = {}
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE fmp_endpoint_status "
        "(ticker TEXT,endpoint TEXT,period TEXT,status TEXT,http_code INTEGER,"
        "file_path TEXT,last_pulled TEXT)"
    )
    raw_path = tmp_path / "data" / "historical" / "fmp" / "GOOGL_income_statement_annual.json"
    raw_path.parent.mkdir(parents=True)
    raw_path.write_text("[]", encoding="utf-8")

    class _FakeProc:
        returncode = 0

    def fake_run(cmd: list[str], **kwargs: object) -> _FakeProc:
        captured["env"] = kwargs.get("env")
        receipt_path = Path(cmd[cmd.index("--work-receipt") + 1])
        sfd._write_work_receipt(
            receipt_path,
            sfd.FmpWorkReceipt(
                ticker="GOOGL",
                endpoint="income-statement",
                period="annual",
                outcome=sfd.FmpWorkReceiptOutcome.SUCCESS,
                http_status=200,
                file_path="data/historical/fmp/GOOGL_income_statement_annual.json",
                content_sha256=hashlib.sha256(raw_path.read_bytes()).hexdigest(),
                captured_at=rc._utc_now(),
            ),
        )
        return _FakeProc()

    monkeypatch.setattr(rc.subprocess, "run", fake_run)

    planned = rc.PlannedWork(
        work_id="a" * 64,
        ticker="GOOGL",
        priority=300,
        endpoint_key="income_statement_annual",
        period_key="annual",
        cache_generation_id="refresh:2026-08-12",
        policy_sha256="b" * 64,
        execution_mode=rc.ExecutionMode.LIVE,
        lease_token="lease-token",
        lease_expires_at=datetime.now() + timedelta(minutes=5),
    )
    rc._dispatch_one(
        connection,
        item,
        planned,
        tier=rc.TIERS["free"],
        auth=rc.FmpAuthConfig(api_key="unused-test-key", source="environment"),
        db_path=tmp_path / "portfolio.db",
        log_path=cache_dir / "dispatch.log",
    )
    connection.close()

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["FMP_TIER"] == "free"
    assert env["FMP_RATE_LIMIT_PER_SEC"] == str(rc.TIERS["free"].calls_per_sec)
