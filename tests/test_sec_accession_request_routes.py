"""A scoped SEC request uses retained evidence and never fetches on a read."""

import errno
import json
import os
import sqlite3
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from flask import Flask, g
from flask.testing import FlaskClient

from dispatch_registry import Job, Registry
from execution import comments_server_sec_accession_routes as routes
from execution import refresh_sec_accession as cli
from pipeline.sec_accession_request import load_bound_request
from provenance.immutable_artifact import publish_text_no_clobber
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from tests.test_sec_native_capture import (
    INVENTORY_KEY,
    FakeResponse,
    FakeSession,
    seed_sec_capture_inventory,
)

START_SUBPROCESS = Job.start_subprocess


def payload() -> dict[str, object]:
    return {
        "request_id": "source-acme",
        "ticker": "ACME",
        "cik": "0000000001",
        "accession_number": "0000000001-26-000001",
        "analysis": {
            "purpose": "Read the reported annual period",
            "issuer_id": "issuer-acme",
            "inventory_key": INVENTORY_KEY,
            "required_period_ends": ["2025-12-31"],
            "require_latest_period": True,
            "cutoff_at": "2026-07-28T10:00:00+00:00",
            "observed_through": "2026-07-28T10:00:00+00:00",
        },
    }


@pytest.fixture
def adapter(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[FlaskClient, Registry, Path]]:
    seed = seed_sec_capture_inventory(tmp_path, migrated_db)
    database = Path(seed.execute("PRAGMA database_list").fetchone()[2])
    seed.close()
    app = Flask(__name__)
    registry = Registry(repo_root=tmp_path)

    def read_db() -> sqlite3.Connection:
        if "request_read_db" not in g:
            g.request_read_db = connect_sqlite(database, role=SQLiteConnectionRole.READ_ONLY)
        return g.request_read_db

    @app.teardown_request
    def close_db(_error: BaseException | None) -> None:
        conn = g.pop("request_read_db", None)
        if conn is not None:
            conn.close()

    routes.register_sec_accession_routes(
        app,
        routes.SecAccessionRouteContext(
            state_root=tmp_path,
            code_root=Path(__file__).resolve().parents[1],
            db_path=database,
            registry=registry,
            get_read_db=read_db,
        ),
    )

    def fake_argv(*_args: object, **_kwargs: object) -> list[str]:
        return ["managed"]

    def fake_spawn(_self: Job) -> None:
        return None

    def fake_running(job: Job) -> bool:
        return job.exit_code is None and job.startup_disposition["state"] != "launch_failed"

    monkeypatch.setattr(routes, "managed_python_argv", fake_argv)
    monkeypatch.setattr(Job, "start_subprocess", fake_spawn)
    monkeypatch.setattr(Job, "is_running", property(fake_running))

    def no_transport() -> None:
        raise AssertionError("read or preflight created transport")

    monkeypatch.setattr(cli.requests, "Session", no_transport)
    yield app.test_client(), registry, database


def plan(client: FlaskClient) -> dict[str, object]:
    response = client.post("/actions/sec-accession/plan", json=payload())
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def apply_body(planned: dict[str, object], **changes: object) -> dict[str, object]:
    return {
        "plan_sha256": planned["plan_sha256"],
        "request_sha256": planned["request_sha256"],
        "scope_sha256": planned["scope_sha256"],
        "action": "apply",
        **changes,
    }


def test_plan_status_restore_no_fetch_and_exact_apply(
    adapter: tuple[FlaskClient, Registry, Path], tmp_path: Path
) -> None:
    client, registry, database = adapter
    before = sqlite3.connect(database)
    counts = before.execute("SELECT COUNT(*) FROM evidence_source_observations").fetchone()
    before.close()
    planned = plan(client)
    assert planned["cancellation"] == "unavailable"
    assert planned["financial_readiness"] == "missing"
    assert registry.list_jobs() == []
    assert client.get("/actions/sec-accession/source-acme").get_json()["state"] == "planned"
    assert (
        client.post(
            "/actions/sec-accession/source-acme/apply", json=apply_body(planned)
        ).status_code
        == 202
    )
    job = registry.list_jobs()[0]
    assert (
        client.post(
            "/actions/sec-accession/source-acme/apply", json=apply_body(planned)
        ).status_code
        == 409
    )
    assert len(registry.list_jobs()) == 1
    reserved = client.get("/actions/sec-accession/source-acme").get_json()
    assert reserved["state"] == "completion_unconfirmed"
    assert reserved["launch_confirmed"] is False
    # Lose only volatile progress, not the retained request or its attempt identity.
    client.application.config["SEC_ACCESSION_REGISTRY"] = Registry(repo_root=tmp_path)
    status = client.get("/actions/sec-accession/source-acme").get_json()
    assert status["state"] == "completion_unconfirmed"
    assert status["request_id"] == "source-acme"
    assert client.get("/actions/sec-accession/source-acme").get_json() == status
    assert (
        client.post(
            "/actions/sec-accession/source-acme/apply",
            json=apply_body(planned, action="resume", resume_from=status["attempt_id"]),
        ).status_code
        == 409
    )
    assert client.application.config["SEC_ACCESSION_REGISTRY"].list_jobs() == []
    conn = sqlite3.connect(database)
    assert conn.execute("SELECT COUNT(*) FROM evidence_source_observations").fetchone() == counts
    conn.close()
    assert job["is_running"] is True


@pytest.mark.parametrize(
    "field,value",
    [
        ("cik", "0000000002"),
        ("capture_batch_size", True),
        ("max_document_bytes", 0),
        ("repo_root", "/tmp/client-root"),
        ("ticker", "OTHER"),
        ("max_members", 251),
    ],
)
def test_invalid_plan_refuses_without_spawn(
    adapter: tuple[FlaskClient, Registry, Path], field: str, value: object
) -> None:
    client, registry, _database = adapter
    body = payload() | {field: value}
    assert client.post("/actions/sec-accession/plan", json=body).status_code in (400, 409)
    assert registry.list_jobs() == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("required_period_ends", ["2024-12-31"]),
        ("issuer_id", "issuer-other"),
        ("require_latest_period", "true"),
        ("unexpected", 1),
        ("purpose", " "),
        ("cutoff_at", "2026-07-28T10:00:00"),
    ],
)
def test_declared_scope_is_enforced(
    adapter: tuple[FlaskClient, Registry, Path], field: str, value: object
) -> None:
    client, registry, _database = adapter
    body = payload()
    analysis = body["analysis"]
    assert isinstance(analysis, dict)
    analysis[field] = value
    assert client.post("/actions/sec-accession/plan", json=body).status_code in (400, 409)
    assert registry.list_jobs() == []


def test_changed_plan_and_commitments_refuse_before_spawn(
    adapter: tuple[FlaskClient, Registry, Path], tmp_path: Path
) -> None:
    client, registry, _database = adapter
    planned = plan(client)
    for field in ("plan_sha256", "request_sha256", "scope_sha256"):
        response = client.post(
            "/actions/sec-accession/source-acme/apply",
            json=apply_body(planned, **{field: "0" * 64}),
        )
        assert response.status_code == 409
    artifact = tmp_path / ".tmp/operations/runtime/sec-accession-refresh/source-acme/plan.json"
    artifact.write_text(
        artifact.read_text().replace('"capture_batch_size":25', '"capture_batch_size":2')
    )
    assert (
        client.post(
            "/actions/sec-accession/source-acme/apply", json=apply_body(planned)
        ).status_code
        == 409
    )
    assert registry.list_jobs() == []


def test_partial_resume_reuses_exact_plan_and_status_never_inspects_blobs(
    adapter: tuple[FlaskClient, Registry, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, registry, database = adapter
    planned = plan(client)
    response = client.post("/actions/sec-accession/source-acme/apply", json=apply_body(planned))
    accepted = response.get_json()
    job = registry.get(accepted["job_id"])
    assert job is not None
    job.exit_code = 2
    bound, frozen, _snapshots = load_bound_request(tmp_path, "source-acme")
    monkeypatch.setattr(cli, "sec_user_agent", lambda: "research-agent test@example.test")
    session = FakeSession([FakeResponse(status_code=503)])
    monkeypatch.setattr(cli.requests, "Session", lambda: session)
    args = [
        "--db",
        str(database),
        "--repo-root",
        str(tmp_path),
        "--request-id",
        "source-acme",
        "--apply",
        "--plan-sha256",
        frozen.commitment,
        "--request-sha256",
        bound.commitment,
        "--attempt-id",
        accepted["attempt_id"],
    ]
    assert cli.main(args) == 2
    first = client.get("/actions/sec-accession/source-acme").get_json()
    assert first["state"] == "partial"
    assert len(session.calls) == 1
    assert (
        client.post(
            "/actions/sec-accession/source-acme/apply", json=apply_body(planned)
        ).status_code
        == 409
    )
    resumed = client.post(
        "/actions/sec-accession/source-acme/apply",
        json=apply_body(planned, action="resume", resume_from=accepted["attempt_id"]),
    )
    assert resumed.status_code == 202
    assert (
        client.get("/actions/sec-accession/source-acme").get_json()["financial_readiness"]
        == "missing"
    )
    assert len(session.calls) == 1


def test_status_bounds_valid_attempt_population(
    adapter: tuple[FlaskClient, Registry, Path], tmp_path: Path
) -> None:
    client, _registry, _database = adapter
    plan(client)
    operation = tmp_path / ".tmp/operations/runtime/sec-accession-refresh/source-acme"
    attempts = operation / "attempts"
    bound, frozen, _snapshots = load_bound_request(tmp_path, "source-acme")
    for index in range(33):
        publish_text_no_clobber(
            attempts / f"{index:032x}.started.json",
            json.dumps(
                {
                    "attempt_id": f"{index:032x}",
                    "request_id": "source-acme",
                    "request_sha256": bound.commitment,
                    "plan_sha256": frozen.commitment,
                    "recorded_at": "2026-07-28T10:00:00+00:00",
                    "state": "running",
                }
            ),
        )
    assert client.get("/actions/sec-accession/source-acme").status_code == 409


def test_known_no_child_failure_has_durable_status_and_exact_retry(
    adapter: tuple[FlaskClient, Registry, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, registry, _database = adapter
    planned = plan(client)
    monkeypatch.setattr(Job, "start_subprocess", START_SUBPROCESS)
    with patch(
        "dispatch_registry.subprocess.Popen", side_effect=FileNotFoundError("private detail")
    ):
        refused = client.post("/actions/sec-accession/source-acme/apply", json=apply_body(planned))
    assert refused.status_code == 409
    current = client.get("/actions/sec-accession/source-acme").get_json()
    assert current["state"] == "dispatch_failed"
    assert current["job_progress"]["startup"]["state"] == "launch_failed"
    assert current["job_progress"]["exit_code"] is None
    assert "private detail" not in str(current)
    assert not registry.list_jobs()[0]["is_running"]

    def fake_spawn(_job: Job) -> None:
        return None

    monkeypatch.setattr(Job, "start_subprocess", fake_spawn)
    resumed = client.post(
        "/actions/sec-accession/source-acme/apply",
        json=apply_body(planned, action="resume", resume_from=current["attempt_id"]),
    )
    assert resumed.status_code == 202
    assert len(registry.list_jobs()) == 2


def test_success_keeps_new_observation_time_and_readiness_missing(
    adapter: tuple[FlaskClient, Registry, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, registry, database = adapter
    planned = plan(client)
    accepted = client.post(
        "/actions/sec-accession/source-acme/apply", json=apply_body(planned)
    ).get_json()
    bound, frozen, _snapshots = load_bound_request(tmp_path, "source-acme")
    monkeypatch.setattr(cli, "sec_user_agent", lambda: "research-agent test@example.test")
    session = FakeSession([FakeResponse()])
    monkeypatch.setattr(cli.requests, "Session", lambda: session)
    args = [
        "--db",
        str(database),
        "--repo-root",
        str(tmp_path),
        "--request-id",
        "source-acme",
        "--apply",
        "--plan-sha256",
        frozen.commitment,
        "--request-sha256",
        bound.commitment,
        "--attempt-id",
        accepted["attempt_id"],
    ]
    assert cli.main(args) == 0
    job = registry.get(accepted["job_id"])
    assert job is not None
    job.exit_code = 0
    value = client.get("/actions/sec-accession/source-acme").get_json()
    assert value["state"] == "succeeded" and value["financial_readiness"] == "missing"
    assert value["cancellation"] == "unavailable"
    assert (
        client.post(
            "/actions/sec-accession/source-acme/apply", json=apply_body(planned)
        ).status_code
        == 409
    )
    assert len(session.calls) == 1
    conn = sqlite3.connect(database)
    observed = conn.execute(
        "SELECT observed_at FROM evidence_source_observations WHERE source_kind='sec_filing'"
    ).fetchone()
    assert observed is not None
    assert datetime.fromisoformat(observed[0]) > bound.scope.request.cutoff_at
    assert conn.execute("SELECT COUNT(*) FROM financial_facts").fetchone()[0] == 0
    conn.close()


def test_unknown_and_failed_status_never_replays_or_verifies_blobs(
    adapter: tuple[FlaskClient, Registry, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from pipeline import sec_accession_refresh

    client, registry, _database = adapter
    assert client.get("/actions/sec-accession/unknown").status_code == 404
    plan(client)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("HTTP read inspected document bytes")

    monkeypatch.setattr(sec_accession_refresh, "inspect_accession_refresh", forbidden)
    for _index in range(2):
        assert client.get("/actions/sec-accession/source-acme").status_code == 200
    assert registry.list_jobs() == []


def test_status_refuses_symlink_binding(
    adapter: tuple[FlaskClient, Registry, Path], tmp_path: Path
) -> None:
    client, _registry, _database = adapter
    plan(client)
    operation = tmp_path / ".tmp/operations/runtime/sec-accession-refresh/source-acme"
    request_file = operation / "request.json"
    original = operation / "retained-request.json"
    request_file.rename(original)
    request_file.symlink_to(original)
    assert client.get("/actions/sec-accession/source-acme").status_code == 409


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX special-file regression")
def test_status_rejects_nonregular_metadata_before_open(
    adapter: tuple[FlaskClient, Registry, Path], tmp_path: Path
) -> None:
    client, _registry, _database = adapter
    plan(client)
    artifact = tmp_path / ".tmp/operations/runtime/sec-accession-refresh/source-acme/plan.json"
    artifact.unlink()
    os.mkfifo(artifact)
    with patch("pipeline.sec_accession_request.os.open") as opened:
        assert client.get("/actions/sec-accession/source-acme").status_code == 409
        opened.assert_not_called()


@pytest.mark.skipif(os.name != "posix", reason="POSIX replacement-at-open regression")
def test_status_refuses_fifo_replacement_at_open_and_closes_descriptor(
    adapter: tuple[FlaskClient, Registry, Path], tmp_path: Path
) -> None:
    client, _registry, _database = adapter
    plan(client)
    artifact = tmp_path / ".tmp/operations/runtime/sec-accession-refresh/source-acme/plan.json"
    real_open = os.open
    descriptors: list[int] = []

    def replace_at_open(
        path: str | os.PathLike[str], flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        if Path(path) == artifact:
            artifact.unlink()
            os.mkfifo(artifact)
            # Check before the real syscall so the pre-repair case cannot hang on this FIFO.
            assert flags & os.O_NONBLOCK, "metadata open must be nonblocking before type check"
            descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
            descriptors.append(descriptor)
            return descriptor
        return real_open(path, flags, mode, dir_fd=dir_fd)

    with patch("pipeline.sec_accession_request.os.open", side_effect=replace_at_open):
        assert client.get("/actions/sec-accession/source-acme").status_code == 409
    assert len(descriptors) == 1
    with pytest.raises(OSError) as closed:
        os.fstat(descriptors[0])
    assert closed.value.errno == errno.EBADF


def test_registered_server_keeps_origin_guard_and_one_read_connection(
    adapter: tuple[FlaskClient, Registry, Path], tmp_path: Path
) -> None:
    import comments_server

    _client, registry, database = adapter
    with patch.object(comments_server, "connect_sqlite", wraps=connect_sqlite) as opened:
        app = comments_server.create_app(
            tmp_path,
            db_path=database,
            code_root=Path(__file__).resolve().parents[1],
            registry=registry,
        )
        client = app.test_client()
        hostile = client.post(
            "/actions/sec-accession/plan",
            json=payload(),
            headers={"Origin": "https://untrusted.example"},
        )
        assert hostile.status_code == 403
        assert opened.call_count == 0
        planned = plan(client)
        assert planned["state"] == "planned"
        assert opened.call_count == 1
        assert opened.call_args.args == (database,)
        assert opened.call_args.kwargs["role"] == SQLiteConnectionRole.READ_ONLY
        assert client.get("/actions/sec-accession/source-acme").status_code == 200
        assert opened.call_count == 2
        assert registry.list_jobs() == []


def test_registered_server_refuses_implicit_database(
    adapter: tuple[FlaskClient, Registry, Path], tmp_path: Path
) -> None:
    import comments_server

    _client, registry, _database = adapter
    client = comments_server.create_app(tmp_path, registry=registry).test_client()
    with patch.object(comments_server, "connect_sqlite") as opened:
        assert client.post("/actions/sec-accession/plan", json=payload()).status_code == 409
        assert opened.call_count == 0
    assert registry.list_jobs() == []
