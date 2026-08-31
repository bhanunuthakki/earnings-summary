from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar
from unittest.mock import Mock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import comments_server  # noqa: E402
from comments_server_panel_cache import (  # noqa: E402
    PanelCacheEntry,
    PanelCacheHit,
    PanelCacheReservation,
    PanelResponseCache,
)

import runtime.portfolio_tracker as portfolio_tracker_runtime  # noqa: E402
from integrations.portfolio_tracker_v1 import HealthV1, V1Fetch  # noqa: E402
from operations.attention import (  # noqa: E402
    EvidenceIdentity,
    EvidenceKind,
    FindingKind,
    derive_finding_id,
)
from operations.kpi_semantic_review_export import (  # noqa: E402
    encoded_kpi_semantic_review_export,
    payload_sha256,
    seal_kpi_semantic_review_export,
)
from operations.models import OperationsRegistry, OperationsSnapshot  # noqa: E402
from operations.paths import (  # noqa: E402
    portfolio_tracker_activation_receipt_path,
    portfolio_tracker_receipt_path,
    scheduler_receipt_path,
    service_receipt_path,
)
from operations.registry import build_operations_registry  # noqa: E402
from operations.snapshot import collect_operations_snapshot  # noqa: E402
from pipeline.kpi_semantic_review import KpiSemanticReviewBatch  # noqa: E402
from pipeline.operations_panel import OperationsPanelView, render_operations_panel  # noqa: E402
from runtime.portfolio_tracker import (  # noqa: E402
    ListenerObservation,
    RuntimeConfig,
    RuntimeReceipt,
    write_runtime_receipt,
)

ATTENTION_NOW = datetime(2026, 8, 24, 19, 0, tzinfo=UTC)
ATTENTION_FINGERPRINT = "b" * 64
ATTENTION_FINDING_ID = derive_finding_id(
    owner="scheduler.collect_operations_runtime_observations",
    kind=FindingKind.RUNTIME_HEALTH,
    evidence=EvidenceIdentity(
        kind=EvidenceKind.RUNTIME_RECEIPT,
        fingerprint_sha256=ATTENTION_FINGERPRINT,
        version="v1",
        reference="operations.runtime.pair.latest.json",
        reference_sha256="c" * 64,
    ),
)


@pytest.fixture
def attention_db_path(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    path = migrated_db(tmp_path / "attention-actions.db")
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO operations_attention_findings(
                finding_id,owner,kind,evidence_kind,evidence_fingerprint_sha256,evidence_version,
                evidence_reference,evidence_reference_sha256,severity,health,lifecycle,opened_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ATTENTION_FINDING_ID,
                "scheduler.collect_operations_runtime_observations",
                "runtime_health",
                "runtime_receipt",
                ATTENTION_FINGERPRINT,
                "v1",
                "operations.runtime.pair.latest.json",
                "c" * 64,
                "warning",
                "degraded",
                "open",
                ATTENTION_NOW.isoformat(),
                ATTENTION_NOW.isoformat(),
            ),
        )
    return path


def _attention_payload(action: str, *, key: str = "operator-action-1") -> dict[str, object]:
    occurred_at = ATTENTION_NOW + timedelta(minutes=1)
    payload: dict[str, object] = {
        "finding_id": ATTENTION_FINDING_ID,
        "evidence_fingerprint_sha256": ATTENTION_FINGERPRINT,
        "idempotency_key": key,
        "occurred_at": occurred_at.isoformat(),
    }
    if action == "acknowledge":
        payload.update(
            reason={"code": "evidence_reviewed", "reference_sha256": "d" * 64},
            acknowledge_until=(occurred_at + timedelta(hours=1)).isoformat(),
        )
    elif action == "snooze":
        payload.update(
            reason={"code": "investigation_in_progress", "reference_sha256": "d" * 64},
            snooze_until=(occurred_at + timedelta(hours=1)).isoformat(),
        )
    return payload


def test_operations_cache_invalidation_leaves_unrelated_panel_entries_fresh() -> None:
    cache = PanelResponseCache(ttl_seconds=30, max_entries=4)
    operations = cache.get_or_reserve("/api/panel/operations")
    overview = cache.get_or_reserve("/api/panel/overview")
    assert isinstance(operations, PanelCacheReservation)
    assert isinstance(overview, PanelCacheReservation)
    entry = PanelCacheEntry(body=b"panel", content_type="text/html", etag='"etag"')
    cache.store(operations, entry)
    cache.store(overview, entry)

    cache.invalidate_prefix("/api/panel/operations")

    assert isinstance(cache.get_or_reserve("/api/panel/operations"), PanelCacheReservation)
    assert isinstance(cache.get_or_reserve("/api/panel/overview"), PanelCacheHit)


def test_operations_cache_invalidation_cancels_only_matching_in_flight_builds() -> None:
    cache = PanelResponseCache(ttl_seconds=30, max_entries=4)
    operations = cache.get_or_reserve("/api/panel/operations")
    overview = cache.get_or_reserve("/api/panel/overview")
    assert isinstance(operations, PanelCacheReservation)
    assert isinstance(overview, PanelCacheReservation)

    cache.invalidate_prefix("/api/panel/operations")

    replacement = cache.get_or_reserve("/api/panel/operations")
    assert isinstance(replacement, PanelCacheReservation)
    entry = PanelCacheEntry(body=b"panel", content_type="text/html", etag='"etag"')
    cache.store(operations, entry)
    cache.store(overview, entry)
    cache.store(replacement, entry)
    assert isinstance(cache.get_or_reserve("/api/panel/overview"), PanelCacheHit)
    assert isinstance(cache.get_or_reserve("/api/panel/operations"), PanelCacheHit)


@pytest.mark.parametrize("action", ("acknowledge", "snooze"))
def test_operations_attention_action_applies_closed_suppression_actions(
    attention_db_path: Path, action: str
) -> None:
    response = (
        comments_server.create_app(attention_db_path.parent, db_path=attention_db_path)
        .test_client()
        .post(f"/api/operations/attention/{action}", json=_attention_payload(action))
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["receipt"]["result_state"] == "applied"
    assert body["receipt"]["action"] == action
    with sqlite3.connect(attention_db_path) as conn:
        assert conn.execute(
            "SELECT actor,lifecycle FROM operations_attention_action_receipts "
            "JOIN operations_attention_findings USING(finding_id)"
        ).fetchone() == (comments_server.DEFAULT_USER_ID, f"{action}d")


def test_operations_attention_action_resolves_only_through_writer(attention_db_path: Path) -> None:
    with sqlite3.connect(attention_db_path) as conn:
        conn.execute(
            "UPDATE operations_attention_findings SET health='healthy' WHERE finding_id=?",
            (ATTENTION_FINDING_ID,),
        )

    response = (
        comments_server.create_app(attention_db_path.parent, db_path=attention_db_path)
        .test_client()
        .post("/api/operations/attention/resolve", json=_attention_payload("resolve"))
    )

    assert response.status_code == 200
    assert response.get_json()["receipt"]["result_state"] == "applied"
    with sqlite3.connect(attention_db_path) as conn:
        assert conn.execute(
            "SELECT lifecycle FROM operations_attention_findings WHERE finding_id=?",
            (ATTENTION_FINDING_ID,),
        ).fetchone() == ("resolved",)


def test_operations_attention_action_rejects_invalid_shape_and_spoofed_actor(
    attention_db_path: Path,
) -> None:
    client = comments_server.create_app(
        attention_db_path.parent, db_path=attention_db_path
    ).test_client()
    missing_reason = _attention_payload("acknowledge")
    missing_reason.pop("reason")
    spoofed_actor = _attention_payload("acknowledge", key="actor-spoof")
    spoofed_actor["actor"] = "attacker"

    invalid = client.post("/api/operations/attention/acknowledge", json=missing_reason)
    spoofed = client.post("/api/operations/attention/acknowledge", json=spoofed_actor)
    unknown = client.post("/api/operations/attention/detected", json=_attention_payload("resolve"))

    assert invalid.status_code == 400
    assert spoofed.status_code == 400
    assert unknown.status_code == 404
    with sqlite3.connect(attention_db_path) as conn:
        assert conn.execute(
            "SELECT count(*) FROM operations_attention_action_receipts"
        ).fetchone() == (0,)


def test_operations_attention_action_preserves_replay_conflict_and_targeted_cache_invalidation(
    attention_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    panel_cache = Mock()
    monkeypatch.setattr(comments_server, "PanelResponseCache", Mock(return_value=panel_cache))
    client = comments_server.create_app(
        attention_db_path.parent, db_path=attention_db_path
    ).test_client()
    payload = _attention_payload("acknowledge")

    applied = client.post("/api/operations/attention/acknowledge", json=payload)
    replayed = client.post("/api/operations/attention/acknowledge", json=payload)
    conflict_payload = _attention_payload("acknowledge")
    conflict_payload["acknowledge_until"] = (ATTENTION_NOW + timedelta(hours=2)).isoformat()
    conflicted = client.post("/api/operations/attention/acknowledge", json=conflict_payload)
    stale_payload = _attention_payload("acknowledge", key="stale-evidence")
    stale_payload["evidence_fingerprint_sha256"] = "e" * 64
    rejected = client.post("/api/operations/attention/acknowledge", json=stale_payload)

    assert applied.status_code == 200
    assert replayed.status_code == 200
    assert replayed.get_json()["receipt"]["result_state"] == "replayed"
    assert conflicted.status_code == 409
    assert conflicted.get_json()["receipt"]["result_state"] == "conflict"
    assert rejected.status_code == 409
    assert rejected.get_json()["receipt"]["result_state"] == "rejected"
    assert panel_cache.invalidate_prefix.call_count == 3
    panel_cache.invalidate_prefix.assert_called_with("/api/panel/operations")
    panel_cache.clear.assert_not_called()


def test_operations_panel_route_is_get_only_app_cached_and_one_connection_per_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    build_registry = Mock(return_value=registry)

    def collect(
        current: OperationsRegistry,
        *,
        repo_root: Path,
        conn: sqlite3.Connection,
        observed_at: datetime,
        scheduler_receipt_path: Path,
        service_receipt_path: Path,
    ) -> OperationsSnapshot:
        return collect_operations_snapshot(
            current,
            repo_root=repo_root,
            conn=conn,
            observed_at=observed_at,
            scheduler_receipt_path=scheduler_receipt_path,
            service_receipt_path=service_receipt_path,
        )

    def render(view: OperationsPanelView) -> str:
        return render_operations_panel(view)

    collect_snapshot = Mock(side_effect=collect)
    render_panel = Mock(side_effect=render)
    connections: list[sqlite3.Connection] = []
    connection_options: list[dict[str, object]] = []

    def connect(_path: Path, **kwargs: object) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        connections.append(conn)
        connection_options.append(kwargs)
        return conn

    monkeypatch.setattr(comments_server, "build_operations_registry", build_registry)
    monkeypatch.setattr(comments_server, "collect_operations_snapshot", collect_snapshot)
    monkeypatch.setattr(comments_server, "render_operations_panel", render_panel)
    monkeypatch.setattr(comments_server, "connect_sqlite", connect)

    client = comments_server.create_app(tmp_path).test_client()
    first = client.get("/api/panel/operations")
    second = client.get("/api/panel/operations")
    rejected = client.post("/api/panel/operations")

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.headers["X-Panel-Cache"] == "hit"
    assert rejected.status_code == 405
    assert build_registry.call_count == 1
    build_registry.assert_called_once_with(comments_server.PROJECT_ROOT.resolve())
    assert collect_snapshot.call_count == 1
    assert render_panel.call_count == 1
    assert len(connections) == 1
    assert connection_options == [
        {"role": comments_server.SQLiteConnectionRole.READ_ONLY, "schema_preflight": False}
    ]


def test_operations_registry_comes_from_code_root_not_minimal_runtime_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_root = tmp_path / "runtime"
    code_root = tmp_path / "deployed-code"
    runtime_root.mkdir()
    code_root.mkdir()
    registry = build_operations_registry(PROJECT_ROOT)
    build_registry = Mock(return_value=registry)
    monkeypatch.setattr(comments_server, "build_operations_registry", build_registry)

    app = comments_server.create_app(runtime_root, code_root=code_root)

    assert app.config["OPERATIONS_REGISTRY"] is registry
    build_registry.assert_called_once_with(code_root.resolve())


def test_operations_review_bundle_loads_kpi_repair_from_state_root() -> None:
    source = (PROJECT_ROOT / "execution" / "comments_server.py").read_text(encoding="utf-8")
    route = source.split("def operations_review_bundle_api():", maxsplit=1)[1].split(
        '@app.route("/api/panel/<name>"', maxsplit=1
    )[0]

    repair_call = route.split("kpi_repair=load_kpi_repair_review(", maxsplit=1)[1].split(
        "),", maxsplit=1
    )[0]
    assert "repo_root=repo_root" in repair_call
    assert "resolved_code_root" not in repair_call


def test_start_tracker_route_uses_separate_state_root_activation_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker_root = tmp_path.parent / "portfolio-tracker"
    tracker_root.mkdir(exist_ok=True)
    monkeypatch.setenv("PORTFOLIO_TRACKER_ROOT", str(tracker_root))
    captured: dict[str, object] = {}
    now = datetime.now(UTC)
    health = HealthV1.model_validate(
        {
            "status": "ok",
            "schema_version": "1.0.0",
            "generated_at": now.isoformat(),
            "database_ok": True,
            "migration_version": "0023",
            "providers": [],
            "active_account_count": 1,
            "latest_snapshot_date": now.date().isoformat(),
            "is_stale": False,
            "links": {},
        }
    )

    class _Manager:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def ensure_running(self, **kwargs: object) -> RuntimeReceipt:
            captured["ensure_running_kwargs"] = kwargs
            config = captured["config"]
            assert isinstance(config, RuntimeConfig)
            return RuntimeReceipt(
                idempotency_key=config.idempotency_key,
                lifecycle_state="started",
                recorded_at=now,
                listener=ListenerObservation(
                    healthy=True,
                    owner="portfolio-tracker-service",
                    pid=123,
                    job_id="job_tracker",
                    health=health,
                ),
            )

    monkeypatch.setattr(comments_server, "PortfolioTrackerRuntimeManager", _Manager)
    monkeypatch.setenv("PORTFOLIO_TRACKER_API_URL", "http://127.0.0.1:8123")
    client = comments_server.create_app(tmp_path).test_client()
    response = client.post("/actions/start-tracker")

    assert response.status_code == 200
    config = captured["config"]
    assert isinstance(config, RuntimeConfig)
    assert config.idempotency_key.startswith("portfolio-tracker-activation:")
    assert captured["ensure_running_kwargs"] == {
        "receipt_path": portfolio_tracker_activation_receipt_path(tmp_path),
        "receipt_writer": comments_server.write_tracker_activation_receipt,
    }
    assert not portfolio_tracker_receipt_path(tmp_path).exists()


def test_start_tracker_route_rejects_injected_success_with_nonpositive_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker_root = tmp_path.parent / "portfolio-tracker"
    tracker_root.mkdir(exist_ok=True)
    monkeypatch.setenv("PORTFOLIO_TRACKER_ROOT", str(tracker_root))
    monkeypatch.setenv("PORTFOLIO_TRACKER_API_URL", "http://127.0.0.1:8000")
    now = datetime.now(UTC)
    health = HealthV1.model_validate(
        {
            "status": "ok",
            "schema_version": "1.0.0",
            "generated_at": now.isoformat(),
            "database_ok": True,
            "migration_version": "0023",
            "providers": [],
            "active_account_count": 1,
            "latest_snapshot_date": now.date().isoformat(),
            "is_stale": False,
            "links": {},
        }
    )

    class _Manager:
        def __init__(self, **_: object) -> None:
            pass

        def ensure_running(self, **_: object) -> RuntimeReceipt:
            return RuntimeReceipt(
                idempotency_key="portfolio-tracker-activation:2026-08-29",
                lifecycle_state="started",
                recorded_at=now,
                listener=ListenerObservation.model_construct(
                    healthy=True,
                    owner="portfolio-tracker-service",
                    pid=0,
                    health=health,
                ),
            )

    monkeypatch.setattr(comments_server, "PortfolioTrackerRuntimeManager", _Manager)

    response = comments_server.create_app(tmp_path).test_client().post("/actions/start-tracker")

    assert response.status_code == 503
    assert "health/ownership proof" in response.get_json()["error"]


def test_start_tracker_route_persists_safe_scheduler_failure_without_overwriting_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker_root = tmp_path.parent / "portfolio-tracker"
    tracker_root.mkdir(exist_ok=True)
    monkeypatch.setenv("PORTFOLIO_TRACKER_ROOT", str(tracker_root))
    monkeypatch.setenv("PORTFOLIO_TRACKER_API_URL", "http://127.0.0.1:8000")

    class _Client:
        def __init__(self, **_: object) -> None:
            pass

        def probe_v1(self) -> V1Fetch[HealthV1]:
            return V1Fetch(available=False, endpoint="/health")

    def fail_scheduler_start() -> None:
        raise portfolio_tracker_runtime.SchedulerActivationError("scheduler_start_nonzero")

    monkeypatch.setattr(comments_server, "TrackerV1Client", _Client)
    monkeypatch.setattr(
        comments_server,
        "start_portfolio_tracker_scheduler_task",
        fail_scheduler_start,
    )

    response = comments_server.create_app(tmp_path).test_client().post("/actions/start-tracker")

    assert response.status_code == 503
    assert response.get_json()["error"] == "scheduler_start_nonzero"
    activation = portfolio_tracker_runtime.TrackerActivationReceipt.model_validate_json(
        portfolio_tracker_activation_receipt_path(tmp_path).read_bytes()
    )
    assert activation.lifecycle_state == "failed"
    assert activation.failure_code == "scheduler_start_nonzero"
    assert activation.scheduler_task_name == r"\earnings-summary\portfolio_tracker_api"
    assert activation.idempotency_key.startswith("portfolio-tracker-activation:")
    assert not portfolio_tracker_receipt_path(tmp_path).exists()


def test_start_tracker_route_requires_explicit_api_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker_root = tmp_path.parent / "portfolio-tracker"
    tracker_root.mkdir(exist_ok=True)
    monkeypatch.setenv("PORTFOLIO_TRACKER_ROOT", str(tracker_root))
    monkeypatch.delenv("PORTFOLIO_TRACKER_API_URL", raising=False)

    response = comments_server.create_app(tmp_path).test_client().post("/actions/start-tracker")

    assert response.status_code == 400
    assert "PORTFOLIO_TRACKER_API_URL is required" in response.get_json()["error"]


def test_start_tracker_route_rejects_non_loopback_bind_from_parser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker_root = tmp_path.parent / "portfolio-tracker"
    tracker_root.mkdir(exist_ok=True)
    monkeypatch.setenv("PORTFOLIO_TRACKER_ROOT", str(tracker_root))
    monkeypatch.setenv("PORTFOLIO_TRACKER_API_URL", "http://127.0.0.1:8123")

    def unsafe_parser(_api_url: str) -> tuple[str, int]:
        return "0.0.0.0", 8123

    monkeypatch.setattr(
        comments_server,
        "parse_tracker_bind_url",
        unsafe_parser,
    )

    response = comments_server.create_app(tmp_path).test_client().post("/actions/start-tracker")

    assert response.status_code == 400
    assert "cannot be safely bound" in response.get_json()["error"]


def test_start_tracker_route_rejects_healthy_listener_without_matching_registry_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker_root = tmp_path.parent / "portfolio-tracker"
    tracker_root.mkdir(exist_ok=True)
    monkeypatch.setenv("PORTFOLIO_TRACKER_ROOT", str(tracker_root))
    now = datetime.now(UTC)
    health = HealthV1.model_validate(
        {
            "status": "ok",
            "schema_version": "1.0.0",
            "generated_at": now.isoformat(),
            "database_ok": True,
            "migration_version": "0023",
            "providers": [],
            "active_account_count": 1,
            "latest_snapshot_date": now.date().isoformat(),
            "is_stale": False,
            "links": {},
        }
    )

    class _Client:
        def __init__(self, **_: object) -> None:
            pass

        def probe_v1(self) -> V1Fetch[HealthV1]:
            return V1Fetch(available=True, endpoint="/health", data=health)

    class _Registry:
        def __init__(self, **_: object) -> None:
            pass

        def list_jobs(self) -> list[dict[str, object]]:
            return []

        def get(self, _job_id: str) -> None:
            return None

        def start(self, **_: object) -> None:
            pytest.fail("unrelated healthy listener must not start a tracker job")

    monkeypatch.setenv("PORTFOLIO_TRACKER_API_URL", "http://127.0.0.1:8123")
    monkeypatch.setattr(comments_server, "TrackerV1Client", _Client)
    monkeypatch.setattr(comments_server, "Registry", _Registry)

    response = comments_server.create_app(tmp_path).test_client().post("/actions/start-tracker")

    assert response.status_code == 503
    assert response.get_json()["error"] == "listener_owner_unverified"
    activation = portfolio_tracker_runtime.TrackerActivationReceipt.model_validate_json(
        portfolio_tracker_activation_receipt_path(tmp_path).read_bytes()
    )
    assert activation.lifecycle_state == "failed"
    assert activation.failure_code == "listener_owner_unverified"


def test_start_tracker_route_accepts_fresh_supervisor_owned_listener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "runtime-state"
    code_root = tmp_path / "deployed-code"
    tracker_root = tmp_path / "portfolio-tracker"
    state_root.mkdir()
    code_root.mkdir()
    tracker_root.mkdir()
    monkeypatch.setenv("PORTFOLIO_TRACKER_ROOT", str(tracker_root))
    monkeypatch.setenv("PORTFOLIO_TRACKER_API_URL", "http://127.0.0.1:8000")
    now = datetime.now(UTC)
    health = HealthV1.model_validate(
        {
            "status": "ok",
            "schema_version": "1.0.0",
            "generated_at": now.isoformat(),
            "database_ok": True,
            "migration_version": "0023",
            "providers": [],
            "active_account_count": 1,
            "latest_snapshot_date": now.date().isoformat(),
            "is_stale": False,
            "links": {},
        }
    )
    expected_receipt_path = portfolio_tracker_receipt_path(state_root)
    expected_lease_path = expected_receipt_path.with_name("portfolio-tracker.activation.lease")
    write_runtime_receipt(
        expected_receipt_path,
        RuntimeReceipt(
            idempotency_key="portfolio-tracker-refresh:2026-08-27",
            lifecycle_state="already_running",
            recorded_at=now,
            listener=ListenerObservation(
                healthy=True,
                owner="portfolio-tracker-service",
                pid=4242,
                health_checked_at=now,
                health=health,
            ),
        ),
    )

    class _Client:
        def __init__(self, **_: object) -> None:
            pass

        def probe_v1(self) -> V1Fetch[HealthV1]:
            return V1Fetch(available=True, endpoint="/health", data=health)

    class _Registry:
        def __init__(self, **_: object) -> None:
            pass

        def list_jobs(self) -> list[dict[str, object]]:
            return []

        def get(self, _job_id: str) -> None:
            return None

        def start(self, **_: object) -> None:
            pytest.fail("a supervisor-owned listener must not start a second tracker job")

    def endpoint_owner_matches(
        host: str, port: int, pid: int, *, require_exclusive: bool = False
    ) -> bool:
        assert (host, port, pid) == ("127.0.0.1", 8000, 4242)
        assert require_exclusive is True
        return True

    receipt_reads: list[Path] = []
    lease_paths: list[Path] = []
    real_read_supervisor_listener_ownership = comments_server.read_supervisor_listener_ownership
    real_atomic_file_lease = comments_server.AtomicFileLease

    def read_supervisor_listener_ownership(
        receipt_path: Path,
        *,
        listener_owner: str,
        bind_host: str,
        bind_port: int,
        observed_at: datetime,
    ) -> ListenerObservation | None:
        receipt_reads.append(receipt_path)
        return real_read_supervisor_listener_ownership(
            receipt_path,
            listener_owner=listener_owner,
            bind_host=bind_host,
            bind_port=bind_port,
            observed_at=observed_at,
        )

    def atomic_file_lease(path: Path) -> portfolio_tracker_runtime.AtomicFileLease:
        lease_paths.append(path)
        return real_atomic_file_lease(path)

    monkeypatch.setattr(comments_server, "TrackerV1Client", _Client)
    monkeypatch.setattr(
        comments_server,
        "read_supervisor_listener_ownership",
        read_supervisor_listener_ownership,
    )
    monkeypatch.setattr(comments_server, "AtomicFileLease", atomic_file_lease)
    monkeypatch.setattr(
        portfolio_tracker_runtime, "endpoint_owner_matches_pid", endpoint_owner_matches
    )
    monkeypatch.setattr(comments_server, "Registry", _Registry)
    response = (
        comments_server.create_app(
            state_root,
            code_root=code_root,
            operations_registry=build_operations_registry(PROJECT_ROOT),
        )
        .test_client()
        .post("/actions/start-tracker")
    )

    assert response.status_code == 200
    assert response.get_json()["lifecycle_state"] == "already_running"
    assert receipt_reads == [expected_receipt_path]
    assert lease_paths == [expected_lease_path]
    assert not portfolio_tracker_receipt_path(code_root).exists()


def test_start_tracker_route_rejects_exited_popen_even_when_registry_job_says_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker_root = tmp_path.parent / "portfolio-tracker"
    tracker_root.mkdir(exist_ok=True)
    monkeypatch.setenv("PORTFOLIO_TRACKER_ROOT", str(tracker_root))
    now = datetime.now(UTC)
    health = HealthV1.model_validate(
        {
            "status": "ok",
            "schema_version": "1.0.0",
            "generated_at": now.isoformat(),
            "database_ok": True,
            "migration_version": "0023",
            "providers": [],
            "active_account_count": 1,
            "latest_snapshot_date": now.date().isoformat(),
            "is_stale": False,
            "links": {},
        }
    )

    class _Client:
        def __init__(self, **_: object) -> None:
            pass

        def probe_v1(self) -> V1Fetch[HealthV1]:
            return V1Fetch(available=True, endpoint="/health", data=health)

    class _ExitedProcess:
        pid = 4242

        def poll(self) -> int:
            return 0

    class _Job:
        job_id = "job-exited"
        is_running = True
        cwd = str(tracker_root)
        argv: ClassVar[list[str]] = [
            sys.executable,
            "-m",
            "uvicorn",
            "app",
            "--host",
            "127.0.0.1",
            "--port",
            "8123",
        ]
        _process = _ExitedProcess()

    class _Registry:
        def __init__(self, **_: object) -> None:
            pass

        def list_jobs(self) -> list[dict[str, object]]:
            return [{"kind": "tracker-server", "is_running": True, "job_id": "job-exited"}]

        def get(self, _job_id: str) -> _Job:
            return _Job()

        def start(self, **_: object) -> None:
            pytest.fail("an exited Popen cannot prove a live listener")

    monkeypatch.setenv("PORTFOLIO_TRACKER_API_URL", "http://127.0.0.1:8123")
    monkeypatch.setattr(comments_server, "TrackerV1Client", _Client)
    monkeypatch.setattr(comments_server, "Registry", _Registry)

    response = comments_server.create_app(tmp_path).test_client().post("/actions/start-tracker")

    assert response.status_code == 503
    assert response.get_json()["error"] == "listener_owner_unverified"


def test_operations_route_uses_runtime_root_canonical_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    captured: dict[str, object] = {}

    def collect(
        current: OperationsRegistry,
        *,
        repo_root: Path,
        conn: sqlite3.Connection,
        observed_at: datetime,
        scheduler_receipt_path: Path,
        service_receipt_path: Path,
    ) -> OperationsSnapshot:
        captured.update(
            repo_root=repo_root,
            conn=conn,
            observed_at=observed_at,
            scheduler_receipt_path=scheduler_receipt_path,
            service_receipt_path=service_receipt_path,
        )
        return collect_operations_snapshot(
            current,
            repo_root=repo_root,
            conn=conn,
            observed_at=observed_at,
            scheduler_receipt_path=scheduler_receipt_path,
            service_receipt_path=service_receipt_path,
        )

    monkeypatch.setattr(comments_server, "collect_operations_snapshot", collect)

    def connect(_path: Path, **_kwargs: object) -> sqlite3.Connection:
        return sqlite3.connect(":memory:")

    monkeypatch.setattr(comments_server, "connect_sqlite", connect)
    client = comments_server.create_app(tmp_path, operations_registry=registry).test_client()

    response = client.get("/api/panel/operations")

    assert response.status_code == 200
    assert captured["repo_root"] == tmp_path
    assert captured["scheduler_receipt_path"] == (
        tmp_path / ".tmp" / "operations" / "runtime" / "scheduler.latest.json"
    )
    assert captured["service_receipt_path"] == (
        tmp_path / ".tmp" / "operations" / "runtime" / "services.latest.json"
    )
    assert isinstance(captured["observed_at"], datetime)
    assert captured["observed_at"].tzinfo is UTC


def test_operations_review_bundle_uses_configured_private_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured_origin = "https://review.example.ts.net"
    captured: dict[str, object] = {}

    class _Bundle:
        content_sha256 = "a" * 64

        def model_dump(self, *, mode: str) -> dict[str, object]:
            assert mode == "json"
            return {"serving_origin": captured["serving_origin"]}

    def configured_private_origin(**_: object) -> str:
        return configured_origin

    def connect(*_: object, **__: object) -> sqlite3.Connection:
        return sqlite3.connect(":memory:")

    def semantic_rows(*_: object, **__: object) -> tuple[()]:
        return ()

    def no_database_lineage(_: sqlite3.Connection) -> None:
        return None

    def no_repair_review(**_: object) -> None:
        return None

    def snapshot(*_: object, **__: object) -> Mock:
        return Mock()

    monkeypatch.setattr(comments_server, "private_mobile_origin", configured_private_origin)
    monkeypatch.setattr(comments_server, "connect_sqlite", connect)
    monkeypatch.setattr(comments_server, "scoped_kpi_definitions", semantic_rows)
    monkeypatch.setattr(comments_server, "database_lineage_identity", no_database_lineage)
    monkeypatch.setattr(comments_server, "load_kpi_repair_review", no_repair_review)
    monkeypatch.setattr(comments_server, "collect_operations_snapshot", snapshot)

    def build_bundle(**kwargs: object) -> _Bundle:
        captured.update(kwargs)
        return _Bundle()

    monkeypatch.setattr(comments_server, "build_operations_review_bundle", build_bundle)
    response = (
        comments_server.create_app(tmp_path)
        .test_client()
        .get("/api/operations/review-bundle", base_url="http://127.0.0.1:7421")
    )

    assert response.status_code == 200
    assert captured["serving_origin"] == configured_origin


def test_operations_review_bundle_fails_closed_without_private_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supplied_origin = (
        "https://user%40example.com@desktop.example.ts.net:99999/private?token=hidden#fragment"
    )
    monkeypatch.setenv("EARNINGS_SUMMARY_PRIVATE_BASE_URL", supplied_origin)
    response = (
        comments_server.create_app(tmp_path)
        .test_client()
        .get("/api/operations/review-bundle", base_url="https://review.example.ts.net")
    )

    assert response.status_code == 503
    assert "refusing to emit an identity" in response.get_json()["error"]
    assert supplied_origin not in response.get_data(as_text=True)
    assert "user%40example.com" not in response.get_data(as_text=True)


def test_operations_review_bundle_fails_closed_for_loopback_http_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def loopback_private_origin(**_: object) -> str:
        return "http://127.0.0.1:7421"

    monkeypatch.setattr(comments_server, "private_mobile_origin", loopback_private_origin)
    response = (
        comments_server.create_app(tmp_path)
        .test_client()
        .get("/api/operations/review-bundle", base_url="http://127.0.0.1:7421")
    )

    assert response.status_code == 503
    assert "not configured as HTTPS" in response.get_json()["error"]


def test_kpi_semantic_review_route_serves_only_precomputed_current_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "product-state"
    database = state_root / "data" / "portfolio.db"
    observed_at = datetime.now(UTC)
    batch_payload: dict[str, object] = {
        "schema_version": "kpi_semantic_review.v3",
        "user_id": comments_server.DEFAULT_USER_ID,
        "ticker": "NU",
        "observed_at": observed_at,
        "limit": 1_000,
        "total_items": 0,
        "truncated": False,
        "state_counts": {},
        "items": (),
    }
    batch = KpiSemanticReviewBatch.model_validate(
        {**batch_payload, "content_sha256": payload_sha256(batch_payload)}
    )
    export = seal_kpi_semantic_review_export(
        review=batch,
        code_instance_sha256=hashlib.sha256(b"bounded-code-authority").hexdigest(),
        database_instance_sha256="b" * 64,
        schema_revision="0035",
    )
    payload = encoded_kpi_semantic_review_export(export)
    captured: dict[str, object] = {}

    def load_export(**kwargs: object):
        captured.update(kwargs)
        return export, payload

    def bounded_code_identity(_: Path) -> str:
        return "bounded-code-authority"

    monkeypatch.setattr(comments_server, "review_code_identity", bounded_code_identity)
    monkeypatch.setattr(comments_server, "load_current_kpi_semantic_review_export", load_export)
    app = comments_server.create_app(tmp_path, db_path=database, code_root=PROJECT_ROOT)
    response = app.test_client().get(
        "/api/operations/kpi-semantic-review/NU",
        base_url="https://review.example.ts.net",
    )

    assert response.status_code == 200
    assert response.data == payload
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["ETag"] == f'"{export.content_sha256}"'
    assert captured["ticker"] == "NU"
    assert captured["root"] == (state_root / "data" / "operations" / "kpi_semantic_reviews")
    assert (
        app.test_client()
        .post(
            "/api/operations/kpi-semantic-review/NU",
            base_url="https://review.example.ts.net",
        )
        .status_code
        == 405
    )


def test_kpi_semantic_review_route_fails_closed_without_current_portfolio_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from operations.kpi_semantic_review_export import KpiSemanticReviewExportError

    def unavailable(**_: object):
        raise KpiSemanticReviewExportError("ticker is outside the current portfolio export")

    monkeypatch.setattr(comments_server, "load_current_kpi_semantic_review_export", unavailable)
    response = (
        comments_server.create_app(tmp_path)
        .test_client()
        .get(
            "/api/operations/kpi-semantic-review/NOW",
            base_url="https://review.example.ts.net",
        )
    )

    assert response.status_code == 404
    assert response.get_json()["error"] == "semantic review artifact not found"


@pytest.mark.parametrize(
    ("case", "expected"),
    (("current", "Current"), ("stale", "Stale"), ("missing", "Missing"), ("invalid", "Invalid")),
)
def test_operations_route_projects_cached_scheduler_receipt_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expected: str,
) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    scheduler = scheduler_receipt_path(tmp_path)
    services = service_receipt_path(tmp_path)
    if case != "missing":
        scheduler.parent.mkdir(parents=True)
        if case == "invalid":
            scheduler.write_text("not json", encoding="utf-8")
            services.write_text("not json", encoding="utf-8")
        else:
            observed = (
                "2020-01-01T00:00:00+00:00" if case == "stale" else datetime.now(UTC).isoformat()
            )
            scheduler.write_text(
                json.dumps(
                    {
                        "schema_version": "1",
                        "observed_at": observed,
                        "tasks": [
                            {"task_name": task.task_name, "state": "Ready"}
                            for task in registry.scheduled_tasks
                        ],
                    }
                ),
                encoding="utf-8",
            )
            services.write_text(
                json.dumps(
                    {
                        "schema_version": "1",
                        "observed_at": observed,
                        "services": [
                            {"name": service.name, "state": "Running"}
                            for service in registry.services
                        ],
                    }
                ),
                encoding="utf-8",
            )

    def connect(_path: Path, **_kwargs: object) -> sqlite3.Connection:
        return sqlite3.connect(":memory:")

    monkeypatch.setattr(comments_server, "connect_sqlite", connect)
    client = comments_server.create_app(tmp_path, operations_registry=registry).test_client()

    response = client.get("/api/panel/operations")

    assert response.status_code == 200
    assert expected in response.get_data(as_text=True)


def test_readme_update_action_dispatches_preview_and_exact_approved_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code_root = tmp_path / "code"
    runtime_root = tmp_path / "runtime"
    code_root.mkdir()
    runtime_root.mkdir()
    (code_root / "README.md").write_text("# Current\n", encoding="utf-8")
    registry = build_operations_registry(PROJECT_ROOT)
    jobs = Mock()
    jobs.start.return_value = Mock(
        job_id="job_readme",
        ticker="_REPO",
        kind="readme-preview",
        started_at=datetime(2026, 8, 13, tzinfo=UTC),
    )

    def fake_managed_python_argv(*args: object) -> list[str]:
        return [str(arg) for arg in args]

    monkeypatch.setattr(comments_server, "managed_python_argv", fake_managed_python_argv)

    app = comments_server.create_app(
        runtime_root,
        code_root=code_root,
        db_path=runtime_root / "portfolio.db",
        registry=jobs,
        operations_registry=registry,
    )
    client = app.test_client()

    preview = client.post("/actions/readme-update", json={"action": "preview"})

    assert preview.status_code == 201
    preview_call = jobs.start.call_args
    assert preview_call.kwargs["ticker"] == "_REPO"
    assert preview_call.kwargs["kind"] == "readme-preview"
    assert preview_call.kwargs["write_sets"] == ["portfolio-db", "readme-updater"]
    assert "--apply-run" not in preview_call.kwargs["argv"]

    run_id = "d" * 32
    monkeypatch.setattr(
        comments_server,
        "collect_readme_governance_status",
        Mock(return_value=Mock(state="approved_preview", run_id=run_id, can_apply=True)),
    )
    jobs.start.return_value.kind = "readme-apply"
    applied = client.post("/actions/readme-update", json={"action": "apply", "run_id": run_id})

    assert applied.status_code == 201
    apply_call = jobs.start.call_args
    assert apply_call.kwargs["kind"] == "readme-apply"
    assert apply_call.kwargs["argv"][-2:] == ["--apply-run", run_id]
    assert apply_call.kwargs["write_sets"] == ["portfolio-db", "readme-updater"]


def test_readme_apply_route_rejects_unknown_or_stale_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    jobs = Mock()
    monkeypatch.setattr(
        comments_server,
        "collect_readme_governance_status",
        Mock(return_value=Mock(state="stale", run_id="e" * 32, can_apply=False)),
    )
    client = comments_server.create_app(
        tmp_path, registry=jobs, operations_registry=registry
    ).test_client()

    invalid = client.post(
        "/actions/readme-update", json={"action": "apply", "run_id": "../receipt"}
    )
    stale = client.post("/actions/readme-update", json={"action": "apply", "run_id": "e" * 32})

    assert invalid.status_code == 400
    assert stale.status_code == 409
    jobs.start.assert_not_called()


def test_readme_update_action_rejects_non_object_json(tmp_path: Path) -> None:
    registry = build_operations_registry(PROJECT_ROOT)
    jobs = Mock()
    client = comments_server.create_app(
        tmp_path, registry=jobs, operations_registry=registry
    ).test_client()

    response = client.post("/actions/readme-update", json=["preview"])

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON request body must be an object"
    jobs.start.assert_not_called()
