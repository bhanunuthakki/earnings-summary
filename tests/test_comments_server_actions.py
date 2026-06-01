"""Integration tests for the /actions/* endpoints on comments_server.py.

The registry is injected with `spawn=False` semantics — we never actually
fork subprocesses here. Live-subprocess behavior is covered separately by
the smoke test against `data/portfolio.db` post-merge.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import comments_server  # noqa: E402

from dispatch_registry import Registry  # noqa: E402


class _NonSpawningRegistry(Registry):
    """Registry that records starts but never spawns a real subprocess."""

    def start(self, *, ticker, kind, argv, spawn=True):  # type: ignore[override]
        # Force spawn=False so the test never hits real disk / Python interpreter.
        return super().start(ticker=ticker, kind=kind, argv=argv, spawn=False)


def _create_min_schema(conn):
    conn.executescript(
        """
        CREATE TABLE tracked_companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER DEFAULT 1,
            ticker TEXT NOT NULL, name TEXT NOT NULL, list_type TEXT NOT NULL,
            added_at TIMESTAMP, sec_validated INTEGER DEFAULT 0,
            ir_url TEXT, instrument_type TEXT, filing_regime TEXT,
            fiscal_year_end TEXT, fmp_data_saved INTEGER DEFAULT 0,
            fmp_data_upto TEXT, archived_at TIMESTAMP,
            UNIQUE(user_id, ticker)
        );
        CREATE TABLE transcripts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, document_id INTEGER,
            ticker TEXT NOT NULL, call_date TIMESTAMP, fiscal_period_type TEXT,
            period_end TIMESTAMP, source_url TEXT, has_qa_section INTEGER
        );
        CREATE TABLE thesis_evaluations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL,
            evaluated_at TIMESTAMP NOT NULL, overall_status TEXT NOT NULL,
            rule_evaluations_json TEXT, run_id TEXT
        );
        CREATE TABLE fmp_endpoint_status (
            ticker TEXT, endpoint TEXT, period TEXT, status TEXT,
            http_code INTEGER, record_count INTEGER, earliest_date TEXT,
            latest_date TEXT, file_path TEXT, file_bytes INTEGER,
            error_msg TEXT, last_pulled TIMESTAMP
        );
        """
    )
    conn.commit()


@pytest.fixture
def client(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    conn = sqlite3.connect(str(data_dir / "portfolio.db"))
    _create_min_schema(conn)
    conn.close()
    app = comments_server.create_app(tmp_path, registry=_NonSpawningRegistry())
    return app.test_client()


def test_post_refresh_returns_job_metadata(client):
    resp = client.post("/actions/refresh", json={"ticker": "NU", "mode": "stale"})
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["ticker"] == "NU"
    assert body["kind"] == "refresh-stale"
    assert body["job_id"].startswith("job_")
    assert body["stream_url"] == f"/actions/stream/{body['job_id']}"


def test_post_refresh_defaults_to_stale_mode(client):
    resp = client.post("/actions/refresh", json={"ticker": "NU"})
    assert resp.status_code == 201
    assert resp.get_json()["kind"] == "refresh-stale"


def test_post_refresh_uppercases_ticker(client):
    resp = client.post("/actions/refresh", json={"ticker": "goog"})
    assert resp.get_json()["ticker"] == "GOOG"


def test_post_refresh_rejects_unknown_mode(client):
    resp = client.post("/actions/refresh", json={"ticker": "NU", "mode": "wild"})
    assert resp.status_code == 400
    assert "mode" in resp.get_json()["error"]


def test_post_refresh_missing_ticker_400(client):
    resp = client.post("/actions/refresh", json={})
    assert resp.status_code == 400


def test_post_refresh_conflict_for_same_ticker_same_kind(client):
    """Second refresh for the same (ticker, mode) while the first is still running → 409."""
    resp1 = client.post("/actions/refresh", json={"ticker": "NU", "mode": "stale"})
    assert resp1.status_code == 201
    resp2 = client.post("/actions/refresh", json={"ticker": "NU", "mode": "stale"})
    assert resp2.status_code == 409
    assert "already running" in resp2.get_json()["error"]


def test_post_refresh_allows_different_mode_for_same_ticker(client):
    """A user might want to upgrade a running stale refresh to a full refresh."""
    r1 = client.post("/actions/refresh", json={"ticker": "NU", "mode": "stale"})
    assert r1.status_code == 201
    r2 = client.post("/actions/refresh", json={"ticker": "NU", "mode": "full"})
    assert r2.status_code == 201


def test_stream_unknown_job_404(client):
    resp = client.get("/actions/stream/job_doesnotexist")
    assert resp.status_code == 404


def test_stream_returns_event_stream_mimetype(client, tmp_path):
    r1 = client.post("/actions/refresh", json={"ticker": "NU", "mode": "stale"})
    job_id = r1.get_json()["job_id"]

    # Force-complete the job so the SSE stream terminates quickly.
    app_registry: Registry = client.application.config["DISPATCH_REGISTRY"]
    job = app_registry.get(job_id)
    assert job is not None
    job.lines.append("test line one")
    job._done.set()
    job.exit_code = 0

    resp = client.get(f"/actions/stream/{job_id}")
    assert resp.mimetype == "text/event-stream"
    body = resp.get_data(as_text=True)
    assert '"event": "start"' in body
    assert '"event": "log"' in body
    assert "test line one" in body
    assert '"event": "done"' in body
    assert '"exit_code": 0' in body


def test_list_jobs_returns_snapshots_of_all(client):
    client.post("/actions/refresh", json={"ticker": "NU", "mode": "stale"})
    client.post("/actions/refresh", json={"ticker": "GOOG", "mode": "full"})
    resp = client.get("/actions/jobs")
    payload = resp.get_json()
    tickers = {j["ticker"] for j in payload["jobs"]}
    assert tickers == {"NU", "GOOG"}


# ----- /actions/refresh-ir (IR-spreadsheet KPI refresh) -----


def test_post_refresh_ir_returns_job_metadata(client):
    resp = client.post("/actions/refresh-ir", json={"ticker": "NU", "quarters": 8})
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["ticker"] == "NU"
    assert body["kind"] == "refresh-ir"
    assert body["job_id"].startswith("job_")
    assert body["stream_url"] == f"/actions/stream/{body['job_id']}"


def test_post_refresh_ir_uppercases_ticker_and_defaults_quarters(client):
    resp = client.post("/actions/refresh-ir", json={"ticker": "nu"})
    assert resp.status_code == 201
    assert resp.get_json()["ticker"] == "NU"


def test_post_refresh_ir_missing_ticker_400(client):
    resp = client.post("/actions/refresh-ir", json={})
    assert resp.status_code == 400


def test_post_refresh_ir_rejects_non_int_quarters(client):
    resp = client.post("/actions/refresh-ir", json={"ticker": "NU", "quarters": "lots"})
    assert resp.status_code == 400


# ----- PR C: per-step selection + --force (budget-bypass is owned by #215) -----


def _refresh_argv(client, **body) -> list[str]:
    resp = client.post("/actions/refresh", json=body)
    assert resp.status_code == 201, resp.get_data(as_text=True)
    reg: Registry = client.application.config["DISPATCH_REGISTRY"]
    job = reg.get(resp.get_json()["job_id"])
    assert job is not None
    return job.argv


def test_post_refresh_threads_force_flag(client):
    assert "--force" in _refresh_argv(client, ticker="NU", mode="full", force=True)


def test_post_refresh_threads_steps_as_csv(client):
    argv = _refresh_argv(client, ticker="NU", steps=["dcf", "fmp"])
    assert "--steps" in argv
    assert argv[argv.index("--steps") + 1] == "dcf,fmp"


def test_post_refresh_omits_step_flags_by_default(client):
    argv = _refresh_argv(client, ticker="NU", mode="stale")
    assert "--steps" not in argv
    assert "--force" not in argv


def test_post_refresh_rejects_unknown_step(client):
    resp = client.post("/actions/refresh", json={"ticker": "NU", "steps": ["bogus"]})
    assert resp.status_code == 400
    assert "unknown step" in resp.get_json()["error"]


def test_post_refresh_rejects_non_list_steps(client):
    resp = client.post("/actions/refresh", json={"ticker": "NU", "steps": "dcf"})
    assert resp.status_code == 400
