from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from operations.kpi_repair_receipts import canonical_sha256, seal_attempt
from operations.models import (
    DatabaseIdentityObservation,
    DatabaseIdentityRow,
    DatabaseRunsObservation,
    FMPBacklogObservation,
    FMPCircuitObservation,
    JobStepDefinition,
    LLMCallsObservation,
    OperationsRegistry,
    OperationsSnapshot,
    PortfolioTrackerRuntimeObservation,
    ScheduleDefinition,
    ScheduledTaskDefinition,
    SchedulerObservation,
    SchedulerTaskRow,
    SchemaRevisionObservation,
    SchemaRevisionRow,
    ServiceObservation,
    ServiceRow,
    SourceCallsObservation,
    SourcePolicyDefinition,
)
from operations.review_bundle import (
    REVIEW_CODE_IDENTITY_DEPENDENCIES,
    OperationsReviewBundle,
    ReviewSchedulerTask,
    build_operations_review_bundle,
    load_kpi_repair_review,
    review_code_identity,
)

NOW = datetime(2026, 8, 27, 19, tzinfo=UTC)


def _registry() -> OperationsRegistry:
    return OperationsRegistry(
        scheduled_tasks=(
            ScheduledTaskDefinition(
                task_name=r"\earnings-summary\morning",
                xml="cron/morning.xml",
                wrapper="cron/run_morning.bat",
                schedule=ScheduleDefinition(
                    trigger="daily",
                    start_boundary="04:00",
                    repetition_interval=None,
                    days_interval=1,
                    weeks_interval=None,
                    days_of_week=(),
                    days_of_month=(),
                    months=(),
                ),
                service_owned=False,
                scheduler_expectation="required_enabled",
            ),
        ),
        job_steps=(
            JobStepDefinition(
                wrapper="cron/run_morning.bat",
                ordinal=1,
                job="morning",
                raw_lane="portfolio-db",
                effective_lane=("portfolio-db",),
                command=("python", "execution/secret_command.py", "--token", "DO_NOT_LEAK"),
                receipt_ttl_seconds=3600,
            ),
        ),
        services=(),
        llm_model_pins=(),
        llm_purposes=(),
        eval_modes=(),
        source_policy=SourcePolicyDefinition(
            policy_version="1",
            roles=(),
            display_roles=(),
            sources=(),
            collection_modes=(),
            issuers=(),
        ),
        queue_states=(),
        expected_alembic_head="0030",
    )


def _snapshot() -> OperationsSnapshot:
    return OperationsSnapshot(
        observed_at=NOW,
        registry_version="1",
        database_identity=DatabaseIdentityObservation(
            state="current",
            observed_at=NOW,
            evidence_source="C:/private/portfolio.db",
            values=(DatabaseIdentityRow(schema_name="main", file_path="C:/private/portfolio.db"),),
        ),
        schema_revision=SchemaRevisionObservation(
            state="current",
            observed_at=NOW,
            evidence_source="C:/private/portfolio.db",
            value=SchemaRevisionRow(expected_head="0030", actual_heads=("0030",), matches=True),
        ),
        scheduler=SchedulerObservation(
            state="current",
            observed_at=NOW,
            evidence_recorded_at=NOW,
            evidence_source="runtime_receipt",
            values=(
                SchedulerTaskRow(
                    task_name=r"\earnings-summary\morning",
                    state="Ready",
                    registry_match="expected",
                    scheduler_expectation="required_enabled",
                    expectation_match=True,
                    registered_action_sha256="a" * 64,
                    registered_checkout_sha256="b" * 64,
                    registered_wrapper_sha256="c" * 64,
                    wrapper_match=True,
                    last_attempted_at=NOW - timedelta(minutes=10),
                    last_successful_at=NOW - timedelta(minutes=10),
                    next_expected_at=NOW + timedelta(hours=23, minutes=50),
                    last_result=0,
                    attempt_state="succeeded",
                ),
            ),
        ),
        services=ServiceObservation(
            state="current",
            observed_at=NOW,
            evidence_source="runtime_receipt",
            values=(ServiceRow(name="es-dashboard", state="Running", registry_match="expected"),),
        ),
        job_receipts=(),
        database_runs=DatabaseRunsObservation(
            state="current", observed_at=NOW, evidence_source="sqlite"
        ),
        source_calls=SourceCallsObservation(
            state="current", observed_at=NOW, evidence_source="sqlite"
        ),
        llm_calls=LLMCallsObservation(state="current", observed_at=NOW, evidence_source="sqlite"),
        fmp_backlog=FMPBacklogObservation(
            state="current", observed_at=NOW, evidence_source="sqlite"
        ),
        fmp_circuit=FMPCircuitObservation(
            state="current", observed_at=NOW, evidence_source="sqlite"
        ),
        portfolio_tracker_runtime=PortfolioTrackerRuntimeObservation(
            state="current", observed_at=NOW, evidence_source="runtime_receipt"
        ),
    )


def _bundle_with_scheduler_tasks(
    bundle: OperationsReviewBundle, tasks: tuple[ReviewSchedulerTask, ...]
) -> OperationsReviewBundle:
    payload = bundle.model_dump(mode="json", exclude={"content_sha256"})
    payload["scheduler"]["tasks"] = [task.model_dump(mode="json") for task in tasks]
    payload["content_sha256"] = canonical_sha256(payload)
    return OperationsReviewBundle.model_validate(payload)


def test_review_bundle_is_closed_sanitized_and_hash_validated() -> None:
    bundle = build_operations_review_bundle(
        snapshot=_snapshot(),
        registry=_registry(),
        semantic_rows=(),
        serving_origin="https://live-host.example.ts.net",
        code_instance="C:/secret/checkout",
        database_instance="C:/private/portfolio.db",
    )
    encoded = bundle.model_dump_json()
    lowered = encoded.lower()
    assert "do_not_leak" not in lowered
    assert "secret_command" not in lowered
    assert "c:/" not in lowered
    assert "file_path" not in lowered
    assert "command" not in lowered
    assert bundle.scheduler.tasks[0].last_successful_at == NOW - timedelta(minutes=10)
    assert bundle.scheduler.tasks[0].scheduler_expectation == "required_enabled"
    assert bundle.scheduler.tasks[0].wrapper_match is True
    assert bundle.kpi_repair.state == "missing"
    assert OperationsReviewBundle.model_validate_json(encoded) == bundle

    payload = json.loads(encoded)
    payload["schema_revision"]["matches"] = False
    with pytest.raises(ValueError, match="content_sha256"):
        OperationsReviewBundle.model_validate(payload)


def test_review_code_identity_binds_semantic_review_producer_route_model_and_client(
    tmp_path: Path,
) -> None:
    assert tuple(sorted(REVIEW_CODE_IDENTITY_DEPENDENCIES)) == REVIEW_CODE_IDENTITY_DEPENDENCIES
    before = review_code_identity(tmp_path)
    for relative in REVIEW_CODE_IDENTITY_DEPENDENCIES:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(relative, encoding="utf-8")
        after = review_code_identity(tmp_path)
        assert after != before, relative
        before = after


def test_mac_validator_rejects_origin_identity_change_and_staleness() -> None:
    from execution.fetch_windows_review_bundle import (
        ReviewFetchError,
        seal_windows_review_pins,
        validate_bundle,
    )

    bundle = build_operations_review_bundle(
        snapshot=_snapshot(),
        registry=_registry(),
        semantic_rows=(),
        serving_origin="https://live-host.example.ts.net",
        code_instance="checkout",
        database_instance="database",
    )
    pins = seal_windows_review_pins(
        bundle=bundle,
        approved_by="owner",
        approved_at=NOW,
    )
    with pytest.raises(ReviewFetchError, match="trusted host pin"):
        validate_bundle(
            bundle.model_dump_json().encode(),
            origin="https://different.example.ts.net",
            now=NOW,
            max_age=timedelta(minutes=20),
            pins=pins,
        )
    with pytest.raises(ReviewFetchError, match="stale"):
        validate_bundle(
            bundle.model_dump_json().encode(),
            origin="https://live-host.example.ts.net",
            now=NOW + timedelta(hours=1),
            max_age=timedelta(minutes=20),
            pins=pins,
        )
    future_scheduler = bundle.model_copy(
        update={
            "scheduler": bundle.scheduler.model_copy(
                update={
                    "observation": bundle.scheduler.observation.model_copy(
                        update={"evidence_recorded_at": NOW + timedelta(hours=1)}
                    )
                }
            )
        }
    )
    object.__setattr__(
        future_scheduler,
        "content_sha256",
        canonical_sha256(future_scheduler.model_dump(mode="json", exclude={"content_sha256"})),
    )
    with pytest.raises(ReviewFetchError, match="Scheduler receipt is from the future"):
        validate_bundle(
            future_scheduler.model_dump_json().encode(),
            origin="https://live-host.example.ts.net",
            now=NOW,
            max_age=timedelta(minutes=20),
            pins=pins,
        )


def test_mac_validator_rejects_unhealthy_schema_even_when_identity_pins_match() -> None:
    from execution.fetch_windows_review_bundle import (
        ReviewFetchError,
        seal_windows_review_pins,
        validate_bundle,
    )

    origin = "https://live-host.example.ts.net"
    healthy = build_operations_review_bundle(
        snapshot=_snapshot(),
        registry=_registry(),
        semantic_rows=(),
        serving_origin=origin,
        code_instance="checkout-content",
        database_instance="database-lineage",
    )
    pins = seal_windows_review_pins(bundle=healthy, approved_by="owner", approved_at=NOW)
    unhealthy_snapshot = _snapshot().model_copy(
        update={
            "schema_revision": _snapshot().schema_revision.model_copy(
                update={
                    "value": SchemaRevisionRow(
                        expected_head="0030", actual_heads=("0029",), matches=False
                    )
                }
            )
        }
    )
    unhealthy = build_operations_review_bundle(
        snapshot=unhealthy_snapshot,
        registry=_registry(),
        semantic_rows=(),
        serving_origin=origin,
        code_instance="checkout-content",
        database_instance="database-lineage",
    )

    with pytest.raises(ReviewFetchError, match="schema authority is unhealthy"):
        validate_bundle(
            unhealthy.model_dump_json().encode(),
            origin=origin,
            now=NOW,
            max_age=timedelta(minutes=20),
            pins=pins,
        )


def test_mac_validator_rejects_code_database_and_scheduler_pin_drift() -> None:
    from execution.fetch_windows_review_bundle import (
        ReviewFetchError,
        seal_windows_review_pins,
        validate_bundle,
    )

    origin = "https://live-host.example.ts.net"
    bundle = build_operations_review_bundle(
        snapshot=_snapshot(),
        registry=_registry(),
        semantic_rows=(),
        serving_origin=origin,
        code_instance="checkout-content",
        database_instance="database-lineage",
    )
    pins = seal_windows_review_pins(bundle=bundle, approved_by="owner", approved_at=NOW)
    assert (
        validate_bundle(
            bundle.model_dump_json().encode(),
            origin=origin,
            now=NOW,
            max_age=timedelta(minutes=20),
            pins=pins,
        )
        == bundle
    )

    changed = pins.model_copy(update={"code_instance_sha256": "f" * 64})
    object.__setattr__(changed, "content_sha256", pins.content_sha256)
    with pytest.raises(ReviewFetchError, match="code-instance identity changed"):
        validate_bundle(
            bundle.model_dump_json().encode(),
            origin=origin,
            now=NOW,
            max_age=timedelta(minutes=20),
            pins=changed,
        )
    changed_database = pins.model_copy(update={"database_instance_sha256": "e" * 64})
    with pytest.raises(ReviewFetchError, match="database-instance identity changed"):
        validate_bundle(
            bundle.model_dump_json().encode(),
            origin=origin,
            now=NOW,
            max_age=timedelta(minutes=20),
            pins=changed_database,
        )
    changed_task = pins.scheduler_tasks[0].model_copy(
        update={"registered_wrapper_sha256": "d" * 64}
    )
    changed_scheduler = pins.model_copy(update={"scheduler_tasks": (changed_task,)})
    with pytest.raises(ReviewFetchError, match="Scheduler identity changed"):
        validate_bundle(
            bundle.model_dump_json().encode(),
            origin=origin,
            now=NOW,
            max_age=timedelta(minutes=20),
            pins=changed_scheduler,
        )


def test_pin_enrollment_rejects_missing_database_lineage() -> None:
    from execution.fetch_windows_review_bundle import seal_windows_review_pins

    bundle = build_operations_review_bundle(
        snapshot=_snapshot(),
        registry=_registry(),
        semantic_rows=(),
        serving_origin="https://live-host.example.ts.net",
        code_instance="checkout-content",
        database_instance=None,
    )
    with pytest.raises(ValueError, match="unavailable or unhealthy"):
        seal_windows_review_pins(bundle=bundle, approved_by="owner", approved_at=NOW)


def test_pin_enrollment_ignores_unexpected_and_intentional_absent_tasks() -> None:
    from execution.fetch_windows_review_bundle import (
        seal_windows_review_pins,
        validate_pinned_identity,
    )

    bundle = build_operations_review_bundle(
        snapshot=_snapshot(),
        registry=_registry(),
        semantic_rows=(),
        serving_origin="https://live-host.example.ts.net",
        code_instance="checkout-content",
        database_instance="database-lineage",
    )
    unexpected = ReviewSchedulerTask(
        task_name=r"\earnings-summary\unexpected",
        state="Running",
        registry_match="unexpected",
    )
    intentional_absent = ReviewSchedulerTask(
        task_name=r"\earnings-summary\service_owned",
        state="Missing",
        registry_match="expected",
        scheduler_expectation="absent_service_owned",
        expectation_match=True,
    )
    enriched = _bundle_with_scheduler_tasks(
        bundle, (bundle.scheduler.tasks[0], unexpected, intentional_absent)
    )

    pins = seal_windows_review_pins(bundle=enriched, approved_by="owner", approved_at=NOW)

    assert tuple(pin.task_name for pin in pins.scheduler_tasks) == (r"\earnings-summary\morning",)
    validate_pinned_identity(bundle=enriched, pins=pins, now=NOW)


def test_pin_enrollment_keeps_required_missing_and_mismatched_tasks_blocking() -> None:
    from execution.fetch_windows_review_bundle import seal_windows_review_pins

    bundle = build_operations_review_bundle(
        snapshot=_snapshot(),
        registry=_registry(),
        semantic_rows=(),
        serving_origin="https://live-host.example.ts.net",
        code_instance="checkout-content",
        database_instance="database-lineage",
    )
    required_missing = ReviewSchedulerTask(
        task_name=r"\earnings-summary\required_missing",
        state="Missing",
        registry_match="missing",
    )
    with pytest.raises(ValueError, match="incomplete or mismatched"):
        seal_windows_review_pins(
            bundle=_bundle_with_scheduler_tasks(
                bundle, (bundle.scheduler.tasks[0], required_missing)
            ),
            approved_by="owner",
            approved_at=NOW,
        )


@pytest.mark.parametrize(
    "duplicate_tasks",
    [
        ("matching", "mismatched"),
        ("mismatched", "matching"),
    ],
)
def test_pin_enrollment_rejects_duplicate_scheduler_names_before_filtering(
    duplicate_tasks: tuple[str, str],
) -> None:
    from execution.fetch_windows_review_bundle import (
        seal_windows_review_pins,
        validate_pinned_identity,
    )

    bundle = build_operations_review_bundle(
        snapshot=_snapshot(),
        registry=_registry(),
        semantic_rows=(),
        serving_origin="https://live-host.example.ts.net",
        code_instance="checkout-content",
        database_instance="database-lineage",
    )
    matching = bundle.scheduler.tasks[0]
    mismatched = matching.model_copy(
        update={"task_name": matching.task_name.upper(), "wrapper_match": False}
    )
    by_name = {"matching": matching, "mismatched": mismatched}
    duplicate_bundle = _bundle_with_scheduler_tasks(
        bundle,
        tuple(by_name[label] for label in duplicate_tasks),
    )

    with pytest.raises(ValueError, match="duplicate Scheduler task names"):
        seal_windows_review_pins(
            bundle=duplicate_bundle,
            approved_by="owner",
            approved_at=NOW,
        )

    pins = seal_windows_review_pins(bundle=bundle, approved_by="owner", approved_at=NOW)
    with pytest.raises(ValueError, match="duplicate Scheduler task names"):
        validate_pinned_identity(bundle=duplicate_bundle, pins=pins, now=NOW)


def test_required_enabled_missing_task_is_not_intentional_absence() -> None:
    from execution.fetch_windows_review_bundle import seal_windows_review_pins

    bundle = build_operations_review_bundle(
        snapshot=_snapshot(),
        registry=_registry(),
        semantic_rows=(),
        serving_origin="https://live-host.example.ts.net",
        code_instance="checkout-content",
        database_instance="database-lineage",
    )
    required_missing = ReviewSchedulerTask(
        task_name=r"\earnings-summary\required_missing",
        state="Missing",
        registry_match="expected",
        scheduler_expectation="required_enabled",
        expectation_match=True,
    )

    with pytest.raises(ValueError, match="incomplete or mismatched"):
        seal_windows_review_pins(
            bundle=_bundle_with_scheduler_tasks(
                bundle, (bundle.scheduler.tasks[0], required_missing)
            ),
            approved_by="owner",
            approved_at=NOW,
        )

    mismatched = bundle.scheduler.tasks[0].model_copy(update={"wrapper_match": False})
    with pytest.raises(ValueError, match="incomplete or mismatched"):
        seal_windows_review_pins(
            bundle=_bundle_with_scheduler_tasks(bundle, (mismatched,)),
            approved_by="owner",
            approved_at=NOW,
        )


def test_unexpected_to_expected_scheduler_drift_changes_pinned_set() -> None:
    from execution.fetch_windows_review_bundle import (
        ReviewFetchError,
        seal_windows_review_pins,
        validate_bundle,
    )

    origin = "https://live-host.example.ts.net"
    bundle = build_operations_review_bundle(
        snapshot=_snapshot(),
        registry=_registry(),
        semantic_rows=(),
        serving_origin=origin,
        code_instance="checkout-content",
        database_instance="database-lineage",
    )
    unexpected = ReviewSchedulerTask(
        task_name=r"\earnings-summary\drift",
        state="Running",
        registry_match="unexpected",
    )
    observed_unexpected = _bundle_with_scheduler_tasks(
        bundle, (bundle.scheduler.tasks[0], unexpected)
    )
    pins = seal_windows_review_pins(
        bundle=observed_unexpected, approved_by="owner", approved_at=NOW
    )
    expected = unexpected.model_copy(
        update={
            "registry_match": "expected",
            "expectation_match": True,
            "registered_action_sha256": "d" * 64,
            "registered_checkout_sha256": "e" * 64,
            "registered_wrapper_sha256": "f" * 64,
            "wrapper_match": True,
        }
    )
    drifted = _bundle_with_scheduler_tasks(bundle, (bundle.scheduler.tasks[0], expected))

    with pytest.raises(ReviewFetchError, match="task identity set changed"):
        validate_bundle(
            drifted.model_dump_json().encode(),
            origin=origin,
            now=NOW,
            max_age=timedelta(minutes=20),
            pins=pins,
        )


def test_pin_manifest_is_tamper_evident() -> None:
    from execution.fetch_windows_review_bundle import (
        WindowsReviewPins,
        seal_windows_review_pins,
    )

    bundle = build_operations_review_bundle(
        snapshot=_snapshot(),
        registry=_registry(),
        semantic_rows=(),
        serving_origin="https://live-host.example.ts.net",
        code_instance="checkout-content",
        database_instance="database-lineage",
    )
    pins = seal_windows_review_pins(bundle=bundle, approved_by="owner", approved_at=NOW)
    payload = json.loads(pins.model_dump_json())
    payload["code_instance_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="content hash mismatch"):
        WindowsReviewPins.model_validate(payload)


def test_mac_client_requires_independent_pin_manifest() -> None:
    from execution.fetch_windows_review_bundle import main

    with pytest.raises(SystemExit):
        main(["--origin", "https://live-host.example.ts.net"])


def test_kpi_repair_review_is_bounded_sanitized_and_fail_closed(tmp_path: Path) -> None:
    missing = load_kpi_repair_review(repo_root=tmp_path, observed_at=NOW)
    assert missing.state == "missing"

    latest = tmp_path / "data" / "operations" / "kpi_repairs" / "latest.json"
    latest.parent.mkdir(parents=True)
    latest.write_text("not-json", encoding="utf-8")
    assert load_kpi_repair_review(repo_root=tmp_path, observed_at=NOW).state == "invalid"

    receipt = seal_attempt(
        attempt_id="1" * 32,
        logical_idempotency_key_sha256="2" * 64,
        manifest_sha256="3" * 64,
        review_bundle_sha256="4" * 64,
        backup_restore_evidence_id="5" * 64,
        executor_code_sha256="6" * 64,
        mode="apply",
        state="blocked",
        started_at=NOW - timedelta(minutes=2),
        completed_at=NOW - timedelta(minutes=1),
        validated_entries=0,
        inserted_fact_rows=0,
        inserted_context_rows=0,
        blocker_codes=("fact_chain_head_changed",),
        result_fact_head_ids=(),
    )
    latest.write_text(receipt.model_dump_json(), encoding="utf-8")
    review = load_kpi_repair_review(repo_root=tmp_path, observed_at=NOW)
    assert review.state == "current"
    assert review.attempt_state == "blocked"
    assert review.blocker_codes == ("fact_chain_head_changed",)
    encoded = review.model_dump_json()
    assert str(tmp_path) not in encoded
