"""Integration tests for the /actions/* endpoints on comments_server.py.

The registry is injected with `spawn=False` semantics — we never actually
fork subprocesses here. Live-subprocess behavior is covered separately by
the smoke test against `data/portfolio.db` post-merge.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

if TYPE_CHECKING:
    from flask.testing import FlaskClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import comments_server  # noqa: E402

from dispatch_registry import Job, Registry  # noqa: E402


class _NonSpawningRegistry(Registry):
    """Registry that records starts but never spawns a real subprocess."""

    def start(
        self,
        *,
        ticker: str,
        kind: str,
        argv: list[str],
        spawn: bool = True,
        cwd: str | None = None,
        write_sets: list[str] | None = None,
        code_root: str | Path | None = None,
    ) -> Job:
        # Force spawn=False so the test never hits real disk / Python interpreter.
        return super().start(
            ticker=ticker,
            kind=kind,
            argv=argv,
            spawn=False,
            cwd=cwd,
            write_sets=write_sets,
            code_root=code_root,
        )


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


def test_post_refresh_routes_code_and_state_through_separate_roots(client: FlaskClient) -> None:
    resp = client.post(
        "/actions/refresh",
        json={"ticker": "NU", "mode": "stale", "steps": ["build_report"]},
    )
    assert resp.status_code == 201
    registry: Registry = client.application.config["DISPATCH_REGISTRY"]
    job = registry.get(resp.get_json()["job_id"])
    assert job is not None
    code_root = Path(client.application.config["CODE_ROOT"])
    db_path = Path(job.argv[job.argv.index("--db") + 1])
    state_root = db_path.parents[1]
    assert Path(job.argv[2]) == code_root / "execution" / "refresh_dispatch.py"
    assert Path(job.code_repo_root or "") == code_root
    assert job.argv[job.argv.index("--state-root") + 1] == str(state_root)
    assert db_path == state_root / "data" / "portfolio.db"


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


# ----- /actions/dcf-export, /actions/dcf-import, /api/dcf-sheet -----


def test_post_dcf_export_returns_job_metadata(client):
    resp = client.post("/actions/dcf-export", json={"ticker": "nu"})
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["ticker"] == "NU"
    assert body["kind"] == "dcf-export"
    assert body["job_id"].startswith("job_")
    assert body["stream_url"] == f"/actions/stream/{body['job_id']}"


def test_post_dcf_export_threads_share_with_and_new(client):
    reg: Registry = client.application.config["DISPATCH_REGISTRY"]
    resp = client.post(
        "/actions/dcf-export", json={"ticker": "NU", "share_with": "me@x.com", "new": True}
    )
    job = reg.get(resp.get_json()["job_id"])
    assert job is not None
    assert "export" in job.argv
    assert job.argv[job.argv.index("--share-with") + 1] == "me@x.com"
    assert "--new" in job.argv


def test_post_dcf_export_missing_ticker_400(client):
    assert client.post("/actions/dcf-export", json={}).status_code == 400


def test_post_dcf_import_returns_job_metadata(client):
    resp = client.post("/actions/dcf-import", json={"ticker": "NU"})
    assert resp.status_code == 201
    assert resp.get_json()["kind"] == "dcf-import"


def test_post_dcf_import_threads_sheet_id(client):
    reg: Registry = client.application.config["DISPATCH_REGISTRY"]
    resp = client.post("/actions/dcf-import", json={"ticker": "NU", "sheet_id": "SID123"})
    job = reg.get(resp.get_json()["job_id"])
    assert job is not None
    assert "import" in job.argv
    assert job.argv[job.argv.index("--sheet-id") + 1] == "SID123"


def test_post_dcf_import_missing_ticker_400(client):
    assert client.post("/actions/dcf-import", json={}).status_code == 400


def test_api_dcf_sheet_unlinked_returns_null(client):
    resp = client.get("/api/dcf-sheet/TEST")
    assert resp.status_code == 200
    assert resp.get_json()["sheet_id"] is None


def test_api_dcf_sheet_linked_returns_url(client, tmp_path):
    holdings = tmp_path / "micro_thesis" / "holdings" / "TEST.json"
    holdings.parent.mkdir(parents=True, exist_ok=True)
    holdings.write_text(json.dumps({"ticker": "TEST", "dcf_defaults": {"gsheet_id": "SHEET99"}}))
    body = client.get("/api/dcf-sheet/TEST").get_json()
    assert body["sheet_id"] == "SHEET99"
    assert body["url"] == "https://docs.google.com/spreadsheets/d/SHEET99/edit"


def test_dcf_route_redirects_to_sheet_when_linked(client, tmp_path):
    # A linked Sheet wins over a local .xlsx: the brief's /dcf/<T> link opens the
    # editable Google Sheet instead of downloading the workbook.
    holdings = tmp_path / "micro_thesis" / "holdings" / "TEST.json"
    holdings.parent.mkdir(parents=True, exist_ok=True)
    holdings.write_text(json.dumps({"ticker": "TEST", "dcf_defaults": {"gsheet_id": "SHEET99"}}))
    dcf_dir = tmp_path / "dcf"
    dcf_dir.mkdir()
    (dcf_dir / "TEST.xlsx").write_bytes(
        b"PK\x03\x04 fake xlsx bytes"
    )  # present but should be bypassed
    resp = client.get("/dcf/TEST")  # Flask test client does not follow redirects
    assert resp.status_code == 302
    assert resp.headers["Location"] == "https://docs.google.com/spreadsheets/d/SHEET99/edit"


def test_dcf_route_streams_xlsx_when_unlinked(client, tmp_path):
    # No gsheet_id → fall back to streaming the local workbook (200, no redirect).
    dcf_dir = tmp_path / "dcf"
    dcf_dir.mkdir()
    (dcf_dir / "TEST.xlsx").write_bytes(b"PK\x03\x04 fake xlsx bytes")
    resp = client.get("/dcf/TEST")
    assert resp.status_code == 200


def test_run_eval_action_validates_and_starts_job(client: FlaskClient) -> None:
    """/actions/run-eval (llm_evals_plan PR 3): rejects unknown purposes,
    starts a registry job running execution/run_llm_evals.py for known ones."""
    bad = client.post("/actions/run-eval", json={"purpose": "not_a_purpose"})
    assert bad.status_code == 400
    bad_body = cast("dict[str, str]", bad.get_json())
    assert "purpose must be one of" in bad_body["error"]

    resp = client.post("/actions/run-eval", json={"purpose": "bear_case"})
    assert resp.status_code == 201
    body = cast("dict[str, str]", resp.get_json())
    assert body["kind"] == "eval-bear_case"
    assert body["stream_url"] == f"/actions/stream/{body['job_id']}"


def test_evals_panel_route_serves_fragment(client: FlaskClient) -> None:
    """GET /api/panel/evals renders on a minimal DB (no eval tables yet) —
    the run bar must be present so the first eval can be started from it."""
    resp = client.get("/api/panel/evals")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "ev-runbar" in html
    assert "No eval runs recorded yet" in html


def test_position_review_action_starts_job(client: FlaskClient) -> None:
    """/actions/position-review (PR5 — the calibration feeder): starts a
    registry job running execution/review_position.py <T> --verdict, the
    full LLM verdict + behavioral guard path that PERSISTS a gradeable
    position_review memo."""
    resp = client.post("/actions/position-review", json={"ticker": "rbrk"})
    assert resp.status_code == 201
    body = cast("dict[str, str]", resp.get_json())
    assert body["ticker"] == "RBRK"
    assert body["kind"] == "position-review"
    assert body["job_id"].startswith("job_")
    assert body["stream_url"] == f"/actions/stream/{body['job_id']}"


def test_position_review_action_missing_ticker_400(client: FlaskClient) -> None:
    resp = client.post("/actions/position-review", json={})
    assert resp.status_code == 400
    assert "ticker" in cast("dict[str, str]", resp.get_json())["error"]


def test_position_review_action_conflict_for_same_ticker(client: FlaskClient) -> None:
    resp1 = client.post("/actions/position-review", json={"ticker": "RBRK"})
    assert resp1.status_code == 201
    resp2 = client.post("/actions/position-review", json={"ticker": "RBRK"})
    assert resp2.status_code == 409
