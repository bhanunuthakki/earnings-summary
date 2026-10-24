"""Bulk fetch receipts and shared artifact serialization use synthetic inputs."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

import execution.save_fmp_data as fetch


def test_bulk_receipts_include_budget_not_attempted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "synthetic.sqlite"
    database.touch()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "items": [
                    {"ticker": "AAPL", "endpoint": "profile", "period": ""},
                    {"ticker": "MSFT", "endpoint": "profile", "period": ""},
                ]
            }
        )
    )
    output = tmp_path / "receipts.json"
    monkeypatch.setenv("FMP_API_KEY", "unused-test-key")
    monkeypatch.setattr(fetch, "_CALL_COUNTER", 0)
    monkeypatch.setattr(fetch, "_SERVED_COUNTER", 0)
    monkeypatch.setattr(fetch, "_load_snapshot_index", Mock(return_value={}))

    def fetch_one(ticker: str, **kwargs: object) -> dict[str, int]:
        setattr(fetch, "_CALL_COUNTER", 1)
        rows = kwargs["work_receipts"]
        assert isinstance(rows, list)
        rows.append(
            fetch.FmpWorkReceipt(
                ticker=ticker,
                endpoint="profile",
                period="",
                outcome=fetch.FmpWorkReceiptOutcome.EMPTY,
                http_status=200,
                captured_at=getattr(fetch, "_utc_now")(),
            )
        )
        return {"ok": 0, "empty": 1, "forbidden": 0, "error": 0, "skipped": 0, "total": 1}

    monkeypatch.setattr(fetch, "run_ticker", fetch_one)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "save_fmp_data",
            "--repo-root",
            str(tmp_path),
            "--db",
            str(database),
            "--manifest",
            str(manifest),
            "--max-calls",
            "1",
            "--work-receipts",
            str(output),
        ],
    )
    assert fetch.main() == 1
    rows = json.loads(output.read_text())
    assert {(r["ticker"], r["outcome"]) for r in rows} == {
        ("AAPL", "empty"),
        ("MSFT", "budget_not_attempted"),
    }


def test_fetch_cli_stops_on_shared_artifact_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runtime.job_runtime import JobLock

    database = tmp_path / "synthetic.sqlite"
    database.touch()
    monkeypatch.setattr(
        sys,
        "argv",
        ["save_fmp_data", "--repo-root", str(tmp_path), "--db", str(database), "--tickers", "AAPL"],
    )
    monkeypatch.setattr(
        fetch, "_run_cli", Mock(side_effect=AssertionError("fetch entered while busy"))
    )
    with JobLock(tmp_path, "other-fmp-fetch", ["fmp-fetch-artifacts"], wait_s=0):
        assert fetch.main() == 75
    assert not list((tmp_path / ".tmp/job_locks").glob("*.lock"))


@pytest.mark.parametrize(
    "statuses,limit", [([500], 0), ([429], 1), ([429], 2), ([500], 1), ([500], 2), ([403, 500], 2)]
)
def test_attempt_budget_caps_real_transport_retries_and_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
    statuses: list[int],
    limit: int,
) -> None:
    from io import BytesIO

    import requests

    from net.client import HTTP_CLIENT, HostRateBudget, RetryPolicy

    physical_calls: list[str] = []

    def transport(method: str, url: str, **kwargs: object) -> requests.Response:
        status = statuses[min(len(physical_calls), len(statuses) - 1)]
        physical_calls.append(url)
        response = requests.Response()
        response.status_code = status
        response.encoding = "utf-8"
        response.raw = BytesIO(b"{}")
        return response

    monkeypatch.setattr(getattr(HTTP_CLIENT, "_session"), "request", transport)
    monkeypatch.setattr(HTTP_CLIENT, "_sleep", Mock(return_value=None))
    monkeypatch.setattr(HTTP_CLIENT, "_rate_budget", HostRateBudget({}))
    monkeypatch.setattr(fetch, "_FMP_RETRY", RetryPolicy(max_attempts=3, backoff_base_s=0))
    monkeypatch.setattr(fetch, "_MAX_HTTP_ATTEMPTS", limit, raising=False)
    monkeypatch.setattr(fetch, "_CALL_COUNTER", 0)
    monkeypatch.setattr(fetch, "_SERVED_COUNTER", 0)
    monkeypatch.setattr(fetch, "_stable_only", False)
    fetch.fmp_call("cashflow-statement", "AAPL", {})
    assert len(physical_calls) == limit
    assert limit == getattr(fetch, "_CALL_COUNTER")


@pytest.mark.parametrize("valid", [True, False])
def test_fetch_child_reuses_only_valid_artifact_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    valid: bool,
) -> None:
    from runtime.job_runtime import JobLock

    database = tmp_path / "synthetic.sqlite"
    database.touch()
    monkeypatch.setattr(
        sys,
        "argv",
        ["save_fmp_data", "--repo-root", str(tmp_path), "--db", str(database), "--tickers", "AAPL"],
    )
    execute = Mock(return_value=0)
    monkeypatch.setattr(fetch, "_run_cli", execute)
    with JobLock(tmp_path, "parent", ["fmp-fetch-artifacts"], wait_s=0) as owner:
        proof = json.loads(owner.inheritance_proof())
        if not valid:
            proof["fmp-fetch-artifacts"]["token"] = "invalid-test-proof"
        monkeypatch.setenv("EARNINGS_SUMMARY_JOB_LOCK_PROOF", json.dumps(proof))
        assert fetch.main() == (0 if valid else 75)
        assert execute.call_count == (1 if valid else 0)
        assert list((tmp_path / ".tmp/job_locks").glob("*.lock"))
    assert not list((tmp_path / ".tmp/job_locks").glob("*.lock"))


@pytest.mark.parametrize(
    "status,body,error",
    [
        (401, None, "unauthorized"),
        (402, None, "payment-required"),
        (403, None, "Invalid API key for this account"),
        (200, {"unexpected": True}, None),
        (200, None, None),
        (200, dict[str, object](), None),
        (200, [dict[str, object]()], None),
        (200, [None], None),
    ],
)
def test_bulk_stops_after_account_or_schema_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    body: object,
    error: str | None,
) -> None:
    database = tmp_path / "synthetic.sqlite"
    database.touch()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "items": [
                    {"ticker": "AAPL", "endpoint": "profile", "period": ""},
                    {"ticker": "AAPL", "endpoint": "stock-peers", "period": ""},
                    {"ticker": "MSFT", "endpoint": "profile", "period": ""},
                ]
            }
        )
    )
    output = tmp_path / "receipts.json"
    monkeypatch.setenv("FMP_API_KEY", "unused-test-key")
    monkeypatch.setattr(fetch, "_CALL_COUNTER", 0)
    monkeypatch.setattr(fetch, "_SERVED_COUNTER", 0)
    monkeypatch.setattr(fetch, "_load_snapshot_index", Mock(return_value={}))
    monkeypatch.setattr(fetch, "_list_type_for", Mock(return_value="portfolio"))
    monkeypatch.setattr(fetch, "_flush_status_batch", Mock(return_value=None))
    monkeypatch.setattr(fetch.source_calls_log, "log_calls_batch", Mock(return_value=None))
    provider = Mock(return_value=(status, body, error, "stable"))
    monkeypatch.setattr(fetch, "fmp_call", provider)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "save_fmp_data",
            "--repo-root",
            str(tmp_path),
            "--db",
            str(database),
            "--manifest",
            str(manifest),
            "--max-calls",
            "10",
            "--work-receipts",
            str(output),
        ],
    )
    assert fetch.main() == 1
    assert provider.call_count == 1
    rows = json.loads(output.read_text())
    assert len(rows) == 3
    assert sum(r["outcome"] == "prerequisite_not_attempted" for r in rows) == 2


def test_plural_auth_failure_stops_fallback_rungs(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = Mock(return_value=(403, None, "Invalid API key for this account"))
    monkeypatch.setattr(fetch, "_http_get", transport)
    monkeypatch.setattr(fetch, "_MAX_HTTP_ATTEMPTS", None)
    monkeypatch.setattr(fetch, "_stable_only", False)
    fetch.fmp_call("cashflow-statement", "AAPL", stop_on_auth_failure=True)
    assert transport.call_count == 1


def test_real_empty_response_releases_partial_bulk_processing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import sqlite3
    from io import BytesIO

    import fmp_backpop as bulk
    import requests

    from identity import DEFAULT_USER_ID
    from net.client import HTTP_CLIENT, HostRateBudget

    database = tmp_path / "synthetic.sqlite"

    def connection() -> sqlite3.Connection:
        conn = sqlite3.connect(database)
        conn.row_factory = sqlite3.Row
        return conn

    conn = connection()
    conn.execute(
        "CREATE TABLE tracked_companies(ticker TEXT, user_id TEXT, list_type TEXT, archived_at TEXT, instrument_type TEXT)"
    )
    conn.execute(
        "INSERT INTO tracked_companies VALUES ('AAPL', ?, 'portfolio', NULL, 'stock')",
        (DEFAULT_USER_ID,),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(fetch.portfolio_db, "get_connection", connection)
    monkeypatch.setattr(fetch, "_load_snapshot_index", Mock(return_value={}))
    monkeypatch.setattr(fetch, "_flush_status_batch", Mock(return_value=None))
    monkeypatch.setattr(fetch.source_calls_log, "log_calls_batch", Mock(return_value=None))
    monkeypatch.setattr(fetch, "_CALL_COUNTER", 0)
    monkeypatch.setattr(fetch, "_SERVED_COUNTER", 0)
    monkeypatch.setenv("FMP_API_KEY", "unused-test-key")
    transport_calls: list[str] = []

    def transport(method: str, url: str, **kwargs: object) -> requests.Response:
        transport_calls.append(url)
        payload = (
            [] if url.endswith("/stock-peers") else [{"symbol": "AAPL", "companyName": "Synthetic"}]
        )
        response = requests.Response()
        response.status_code = 200
        response.encoding = "utf-8"
        response.raw = BytesIO(json.dumps(payload).encode())
        return response

    monkeypatch.setattr(getattr(HTTP_CLIENT, "_session"), "request", transport)
    monkeypatch.setattr(HTTP_CLIENT, "_rate_budget", HostRateBudget({}))
    monkeypatch.setattr(HTTP_CLIENT, "_measurement_sink", Mock(return_value=False))
    manifest = [
        {"ticker": "AAPL", "endpoint": endpoint, "period": ""}
        for endpoint in ("profile", "stock-peers")
    ]
    monkeypatch.setattr(bulk, "require_db_path", Mock(return_value=database))
    monkeypatch.setattr(bulk, "connect_sqlite", Mock(return_value=connection()))
    monkeypatch.setattr(bulk, "_resolve_tickers", Mock(return_value=[("AAPL", "portfolio")]))
    decision = bulk.TickerDecision("AAPL", "portfolio", False, 0, False, 2, 2, 0)
    monkeypatch.setattr(bulk, "build_manifest", Mock(return_value=(manifest, [decision])))

    def child(
        manifest_path: Path,
        max_calls: int,
        repo_root: Path,
        db_path: Path,
        tickers: list[tuple[str, str]],
        receipt_path: Path,
        lock_proof: str,
    ) -> int:
        monkeypatch.setenv("EARNINGS_SUMMARY_JOB_LOCK_PROOF", lock_proof)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "save_fmp_data",
                "--repo-root",
                str(repo_root),
                "--db",
                str(db_path),
                "--manifest",
                str(manifest_path),
                "--work-receipts",
                str(receipt_path),
                "--max-calls",
                str(max_calls),
            ],
        )
        return fetch.main()

    monkeypatch.setattr(bulk, "_run_fetch", child)
    monkeypatch.setattr(bulk, "open_db", Mock(return_value=sqlite3.connect(":memory:")))
    index = Mock(return_value=1)
    extract = Mock(return_value=0)
    monkeypatch.setattr(bulk, "_run_index", index)
    monkeypatch.setattr(bulk, "_run_extract", extract)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "fmp_backpop",
            "--repo-root",
            str(tmp_path),
            "--db",
            str(database),
            "--apply",
            "--max-calls",
            "2",
        ],
    )
    assert bulk.main() == 0
    assert len(transport_calls) == 2
    assert index.call_count == extract.call_count == 1
    assert '"acquisition_status": "partial_unavailable"' in capsys.readouterr().out
    assert not (tmp_path / "data/historical/fmp/AAPL_peers.json").exists()


def test_catalog_lookup_uses_current_user_active_classified_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlite3

    from identity import DEFAULT_USER_ID

    database = tmp_path / "synthetic.sqlite"

    def connection() -> sqlite3.Connection:
        conn = sqlite3.connect(database)
        conn.row_factory = sqlite3.Row
        return conn

    conn = connection()
    conn.execute(
        "CREATE TABLE tracked_companies(ticker TEXT, user_id TEXT, list_type TEXT, archived_at TEXT, instrument_type TEXT)"
    )
    conn.executemany(
        "INSERT INTO tracked_companies VALUES (?, ?, ?, ?, ?)",
        [
            ("AAPL", "other-user", "index_member", None, "stock"),
            ("AAPL", DEFAULT_USER_ID, "portfolio", None, "stock"),
            ("OLD", DEFAULT_USER_ID, "portfolio", "2020-01-01", "stock"),
        ],
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(fetch.portfolio_db, "get_connection", connection)
    role = getattr(fetch, "_list_type_for")("AAPL")
    assert role == "portfolio"
    assert len(fetch.per_ticker_jobs("AAPL", list_type=role)) > len(
        fetch.per_ticker_jobs("AAPL", list_type="index_member")
    )
    assert getattr(fetch, "_list_type_for")("OLD") == "none"
