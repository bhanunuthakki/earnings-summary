from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

import pytest

import provenance.gc_recovery as recovery_module
from provenance.atomic_cutover import (
    ActivationMode,
    ActivationReceipt,
    activation_payload_sha256,
    quiescence_payload_sha256,
)
from provenance.atomic_cutover import (
    DatabaseVerification as CutoverDatabaseVerification,
)
from provenance.atomic_cutover import (
    ListenerObservation as CutoverListenerObservation,
)
from provenance.atomic_cutover import (
    QuiescenceReceipt as CutoverQuiescenceReceipt,
)
from provenance.atomic_cutover import (
    ServiceObservation as CutoverServiceObservation,
)
from provenance.atomic_cutover import (
    TaskObservation as CutoverTaskObservation,
)
from provenance.gc_recovery import (
    GcRecoveryAdmissionReceipt,
    GcRecoveryError,
    GcRecoveryOutcome,
    GcRecoveryReceipt,
    LiveProcessCensus,
    ProcessCensusObservation,
    QuiescedListenerObservation,
    QuiescedServiceObservation,
    QuiescedTaskObservation,
    RecoveryBaselineAuthority,
    RecoveryProcessCensus,
    RecoveryQuiescenceRegistry,
    RecoveryRuntimeAuthority,
    RecoveryTerminalEvidence,
    audit_gc_recovery,
    publish_gc_recovery_audit,
    validate_gc_recovery_replay,
)

REVISION = "0270_financial_facts_supersedes_index"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
_REAL_WRITE_DENIAL_FENCE = cast(
    Callable[[tuple[Path, ...]], AbstractContextManager[str]],
    getattr(recovery_module, "_write_denial_fence"),
)
_PARSE_WINDOWS_COMMAND_LINE = cast(
    Callable[[str], tuple[str, ...]],
    getattr(recovery_module, "_parse_windows_command_line"),
)
TRIGGER_SQL = (
    "CREATE TRIGGER trg_financial_facts_observation_delete "
    "BEFORE DELETE ON financial_facts BEGIN SELECT RAISE(ABORT, "
    "'financial fact history is append-only after cutover'); END"
)


def _database(path: Path, *, include_fact_one: bool = True, include_link: bool = True) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            f"""
            PRAGMA foreign_keys = ON;
            CREATE TABLE alembic_version (version_num TEXT NOT NULL);
            INSERT INTO alembic_version VALUES ('{REVISION}');
            CREATE TABLE financial_facts (
                id INTEGER PRIMARY KEY,
                ticker TEXT NOT NULL,
                value REAL NOT NULL,
                supersedes_id INTEGER,
                FOREIGN KEY (supersedes_id) REFERENCES financial_facts(id)
                    ON UPDATE NO ACTION ON DELETE NO ACTION
            );
            CREATE INDEX ix_0270_financial_facts_supersedes_id
                ON financial_facts(supersedes_id);
            CREATE TABLE metric_computation_attempts (
                id INTEGER PRIMARY KEY,
                ticker TEXT NOT NULL,
                input_fingerprint TEXT NOT NULL
            );
            CREATE TABLE fact_observation_revisions (
                fact_table TEXT NOT NULL,
                fact_row_id INTEGER NOT NULL,
                fact_revision INTEGER NOT NULL,
                observation_id TEXT NOT NULL,
                PRIMARY KEY (fact_table, fact_row_id, fact_revision)
            );
            CREATE TABLE legacy_fact_evidence_match_revisions (
                match_revision_id TEXT PRIMARY KEY,
                fact_table TEXT NOT NULL,
                fact_row_id INTEGER NOT NULL,
                outcome TEXT NOT NULL
            );
            CREATE TABLE fact_selection_decisions (
                decision_id TEXT PRIMARY KEY,
                target_table TEXT NOT NULL,
                target_row_id INTEGER NOT NULL,
                selection_state TEXT NOT NULL
            );
            """
        )
        if include_fact_one:
            conn.execute("INSERT INTO financial_facts VALUES (1, 'AAA', 10.5, NULL)")
        conn.execute("INSERT INTO financial_facts VALUES (2, 'AAA', 20.5, NULL)")
        conn.execute(
            "INSERT INTO metric_computation_attempts VALUES (1, 'AAA', 'a' || zeroblob(31))"
        )
        if include_link:
            conn.execute(
                "INSERT INTO fact_observation_revisions VALUES "
                "('financial_facts', 1, 1, 'observation-1')"
            )
            conn.execute(
                "INSERT INTO legacy_fact_evidence_match_revisions VALUES "
                "('match-1', 'financial_facts', 1, 'accepted')"
            )
            conn.execute(
                "INSERT INTO fact_selection_decisions VALUES "
                "('decision-1', 'financial_facts', 1, 'included')"
            )
        conn.execute(TRIGGER_SQL)


def _archive(path: Path, *, fact_value: float = 10.5, conflicting: bool = False) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE financial_facts (
                id INTEGER,
                ticker TEXT,
                value REAL,
                supersedes_id INTEGER
            );
            CREATE TABLE metric_computation_attempts (
                id INTEGER,
                ticker TEXT,
                input_fingerprint TEXT
            );
            CREATE TABLE gc_manifest (
                run_at TEXT NOT NULL,
                policy TEXT NOT NULL,
                source_table TEXT NOT NULL,
                rows_archived INTEGER NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO financial_facts VALUES (1, 'AAA', ?, NULL)",
            (fact_value,),
        )
        if conflicting:
            conn.execute("INSERT INTO financial_facts VALUES (1, 'AAA', 99.0, NULL)")
        conn.execute(
            "INSERT INTO gc_manifest VALUES "
            "('2026-08-01T21:11:00', 'facts-depth', 'financial_facts', 1)"
        )


def _paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    current = tmp_path / "current.db"
    baseline = tmp_path / "baseline.db"
    archive = tmp_path / "archive" / "portfolio_gc_archive.db"
    archive.parent.mkdir(exist_ok=True)
    return current, baseline, archive


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha(value: object) -> str:
    payload = json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _revision(database: Path) -> str:
    with sqlite3.connect(database) as conn:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    assert row is not None
    return str(row[0])


def _runtime_commit_and_tasks() -> tuple[str, tuple[str, ...]]:
    head = subprocess.run(  # nosec B603 -- fixed git command with fixed arguments
        ["git", "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    manifest_payload = subprocess.run(  # nosec B603 -- fixed git command; validated HEAD
        ["git", "-C", str(PROJECT_ROOT), "show", f"{head}:cron/task_manifest.json"],
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout
    manifest_raw: object = json.loads(manifest_payload)
    assert isinstance(manifest_raw, dict)
    manifest = cast(dict[str, object], manifest_raw)
    raw_tasks = manifest.get("tasks")
    assert isinstance(raw_tasks, list)
    tasks: list[str] = []
    for item_raw in cast(list[object], raw_tasks):
        assert isinstance(item_raw, dict)
        item = cast(dict[str, object], item_raw)
        task_name = item.get("task_name")
        assert isinstance(task_name, str)
        tasks.append(task_name)
    return head, tuple(tasks)


def _write_sealed_model(path: Path, value: object) -> None:
    model_dump_json = getattr(value, "model_dump_json")
    path.write_bytes((cast(Callable[[], str], model_dump_json)() + "\n").encode())


def _write_activation_receipt(path: Path, receipt: ActivationReceipt) -> None:
    payload = json.dumps(
        receipt.model_dump(mode="json"),
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    path.write_text(payload + "\n", encoding="utf-8", newline="\n")


def _write_cutover_quiescence(path: Path, receipt: CutoverQuiescenceReceipt) -> None:
    payload = json.dumps(
        receipt.model_dump(mode="json"),
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    path.write_text(payload + "\n", encoding="utf-8", newline="\n")


def _reported_deletions(current: Path, baseline: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in (
        "financial_facts",
        "metric_computation_attempts",
        "fact_observation_revisions",
        "legacy_fact_evidence_match_revisions",
        "fact_selection_decisions",
    ):
        with sqlite3.connect(current) as current_conn, sqlite3.connect(baseline) as baseline_conn:
            current_rows = set(current_conn.execute(f'SELECT * FROM "{table}"').fetchall())
            baseline_rows = set(baseline_conn.execute(f'SELECT * FROM "{table}"').fetchall())
        missing = len(baseline_rows - current_rows)
        if missing:
            counts[table] = missing
    return counts


def _live_census() -> LiveProcessCensus:
    unsealed = LiveProcessCensus(
        schema_version="gc-recovery-live-process-census/v1",
        captured_at=datetime.now(UTC),
        collector="powershell-get-ciminstance-win32-process/v1",
        exit_code=0,
        processes=(
            ProcessCensusObservation(
                pid=1,
                parent_pid=0,
                image_name="System",
                command_line_status="ok",
                command_line="System",
                working_directory=None,
            ),
            ProcessCensusObservation(
                pid=2,
                parent_pid=1,
                image_name="observer.exe",
                command_line_status="ok",
                command_line="observer.exe",
                working_directory=str(PROJECT_ROOT.resolve()),
            ),
        ),
        census_sha256="0" * 64,
    )
    return unsealed.model_copy(update={"census_sha256": unsealed.computed_census_sha256()})


@pytest.fixture(autouse=True)
def _controlled_live_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    import provenance.gc_recovery as recovery

    monkeypatch.setattr(recovery, "_collect_live_process_census", _live_census)

    @contextmanager
    def synthetic_windows_fence(
        _paths: tuple[Path, ...],
    ) -> Generator[str, None, None]:
        yield "windows-deny-write"

    monkeypatch.setattr(recovery, "_write_denial_fence", synthetic_windows_fence)


def _admission(
    current: Path,
    baseline: Path,
    archive: Path,
    *,
    terminal_status: Literal["complete", "failed", "unknown"] = "complete",
    validity: timedelta = timedelta(minutes=29),
) -> tuple[Path, str]:
    authority = current.parent / "baseline-authority.json"
    activation_receipt_path = current.parent / "activation-receipt.json"
    activation_quiescence_path = current.parent / "activation-quiescence.json"
    runtime_source = PROJECT_ROOT / "execution" / "db_gc.py"
    runtime_authority_path = current.parent / "runtime-authority.json"
    event_log = current.parent / "gc-events.log"
    report = current.parent / "gc-report.json"
    terminal_path = current.parent / "gc-terminal.json"
    quiescence_path = current.parent / "quiescence-registry.json"
    census_path = current.parent / "process-census.json"
    admission = current.parent / "gc-admission.json"
    now = datetime.now(UTC)
    captured_at = now - timedelta(seconds=1)
    baseline_captured_at = now - timedelta(hours=2)
    runtime_git_commit, tasks = _runtime_commit_and_tasks()
    baseline_sha256 = _sha256(baseline)
    activated_sha256 = _sha256(current)
    activation_started_at = now - timedelta(hours=3)
    quiescence_unsealed = CutoverQuiescenceReceipt(
        schema_version="1",
        captured_at=activation_started_at - timedelta(minutes=5),
        valid_until=activation_started_at + timedelta(minutes=5),
        live_database=str(current.resolve()),
        live_database_sha256=baseline_sha256,
        expected_task_paths=tasks,
        tasks=tuple(
            CutoverTaskObservation(path=path, state="Disabled", enabled=False) for path in tasks
        ),
        expected_service_names=("es-dashboard", "es-poller"),
        services=(
            CutoverServiceObservation(name="es-dashboard", state="Stopped"),
            CutoverServiceObservation(name="es-poller", state="Stopped"),
        ),
        expected_listener_endpoints=("127.0.0.1:7421",),
        listeners=(
            CutoverListenerObservation(
                host="127.0.0.1",
                port=7421,
                listening=False,
                pid=None,
            ),
        ),
        receipt_sha256="0" * 64,
    )
    activation_quiescence = quiescence_unsealed.model_copy(
        update={"receipt_sha256": quiescence_payload_sha256(quiescence_unsealed)}
    )
    _write_cutover_quiescence(activation_quiescence_path, activation_quiescence)
    candidate_path = current.parent / "activation-candidate.db"
    active_verification = CutoverDatabaseVerification(
        database=str(current.resolve()),
        sha256=activated_sha256,
        alembic_revision=REVISION,
        quick_check=("ok",),
        integrity_check=("ok",),
        foreign_key_violations=0,
    )
    candidate_verification = active_verification.model_copy(
        update={"database": str(candidate_path.resolve())}
    )
    activation_unsealed = ActivationReceipt(
        schema_version="1",
        mode=ActivationMode.APPLY,
        status="activated",
        activation_mechanism="windows_replace_file",
        repo_root=str(PROJECT_ROOT.resolve()),
        live_database=str(current.resolve()),
        candidate_database=str(candidate_path.resolve()),
        rollback_database=str(baseline.resolve()),
        failed_candidate_database=None,
        receipt_path=str(activation_receipt_path.resolve()),
        expected_alembic_head=REVISION,
        quiescence_receipt_sha256=activation_quiescence.receipt_sha256,
        live_sha256_before=baseline_sha256,
        candidate_sha256=activated_sha256,
        active_sha256_after=activated_sha256,
        rollback_sha256=baseline_sha256,
        candidate_precheck=candidate_verification,
        active_postcheck=active_verification,
        rollback_restored=False,
        failure=None,
        started_at=activation_started_at,
        completed_at=baseline_captured_at,
        receipt_sha256="0" * 64,
    )
    activation_receipt = activation_unsealed.model_copy(
        update={"receipt_sha256": activation_payload_sha256(activation_unsealed)}
    )
    _write_activation_receipt(activation_receipt_path, activation_receipt)
    baseline_unsealed = RecoveryBaselineAuthority(
        schema_version="gc-recovery-baseline-authority/v1",
        baseline_database=str(baseline.resolve()),
        baseline_database_sha256=baseline_sha256,
        baseline_revision=_revision(baseline),
        baseline_captured_at=baseline_captured_at,
        baseline_capture_method="activation-rollback-snapshot",
        baseline_quick_check="ok",
        baseline_integrity_check="ok",
        baseline_foreign_key_violations=0,
        activated_database_sha256=activated_sha256,
        activation_receipt_artifact=str(activation_receipt_path.resolve()),
        activation_receipt_artifact_sha256=_sha256(activation_receipt_path),
        activation_receipt_artifact_size_bytes=activation_receipt_path.stat().st_size,
        activation_quiescence_artifact=str(activation_quiescence_path.resolve()),
        activation_quiescence_artifact_sha256=_sha256(activation_quiescence_path),
        activation_quiescence_artifact_size_bytes=activation_quiescence_path.stat().st_size,
        receipt_sha256="0" * 64,
    )
    baseline_authority = baseline_unsealed.model_copy(
        update={"receipt_sha256": baseline_unsealed.computed_receipt_sha256()}
    )
    _write_sealed_model(authority, baseline_authority)
    rows_deleted = _reported_deletions(current, baseline)
    if rows_deleted.get("financial_facts", 0):
        event_log.write_text(
            '{"event":"gc_append_only_guard_window"}\n{"event":"gc_batch_commit"}\n',
            encoding="utf-8",
        )
    else:
        event_log.write_bytes(b"")
    operation_argv = (
        "python",
        "execution/db_gc.py",
        "--apply",
        "--db-path",
        str(current.resolve()),
        "--policies",
        "facts-depth",
        "--include-portfolio",
    )
    report.write_text(
        json.dumps(
            {
                "run_at": (now - timedelta(minutes=2)).isoformat(),
                "db_path": str(current.resolve()),
                "archive_path": str(archive.resolve()),
                "apply": True,
                "policies": [
                    {
                        "policy": "facts-depth",
                        "applied": True,
                        "rows_deleted": rows_deleted,
                        "rows_updated": {},
                        "detail": {},
                    }
                ],
                "facts_depth_apply_preflight": {
                    "schema_version": "gc-facts-depth-apply-preflight/v1",
                    "foreign_keys_enabled": True,
                    "self_fk_target_table": "financial_facts",
                    "self_fk_from_column": "supersedes_id",
                    "self_fk_to_column": "id",
                    "lookup_index_name": "ix_0270_financial_facts_supersedes_id",
                    "lookup_index_columns": ["supersedes_id"],
                    "lookup_index_unique": False,
                    "lookup_index_origin": "c",
                    "lookup_index_partial": False,
                    "sqlite_version": sqlite3.sqlite_version,
                    "lookup_query_plan": ["SEARCH financial_facts USING INDEX"],
                },
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    runtime_authority = RecoveryRuntimeAuthority(
        schema_version="gc-recovery-runtime-authority/v1",
        repository=str(PROJECT_ROOT.resolve()),
        git_commit=runtime_git_commit,
        db_gc_artifact=str(runtime_source.resolve()),
        db_gc_sha256=_sha256(runtime_source),
    )
    _write_sealed_model(runtime_authority_path, runtime_authority)
    terminal_exit_code = (
        0 if terminal_status == "complete" else 1 if terminal_status == "failed" else None
    )
    terminal = RecoveryTerminalEvidence(
        schema_version="gc-recovery-terminal/v1",
        captured_at=now - timedelta(minutes=2),
        working_directory=str(PROJECT_ROOT.resolve()),
        command_argv_sha256=_canonical_sha(operation_argv),
        stdout_sha256=_sha256(report),
        stderr_sha256=_sha256(event_log),
        status=terminal_status,
        exit_code=terminal_exit_code,
    )
    _write_sealed_model(terminal_path, terminal)
    quiescence = RecoveryQuiescenceRegistry(
        schema_version="gc-recovery-quiescence/v1",
        captured_at=captured_at,
        tasks=tuple(
            QuiescedTaskObservation(path=path, state="Disabled", enabled=False) for path in tasks
        ),
        services=(
            QuiescedServiceObservation(name="es-dashboard", state="Stopped"),
            QuiescedServiceObservation(name="es-poller", state="Stopped"),
        ),
        listeners=(
            QuiescedListenerObservation(
                host="127.0.0.1",
                port=7421,
                listening=False,
                pid=None,
            ),
        ),
    )
    _write_sealed_model(quiescence_path, quiescence)
    census = RecoveryProcessCensus(
        schema_version="gc-recovery-process-census/v1",
        captured_at=captured_at,
        scope="all-process-command-lines/v1",
        command_sha256="c" * 64,
        snapshot_complete=True,
        inventory_source="windows-all-process-command-lines/v1",
        processes=(
            ProcessCensusObservation(
                pid=1,
                parent_pid=0,
                image_name="System",
                command_line_status="ok",
                command_line="System",
                working_directory=None,
            ),
            ProcessCensusObservation(
                pid=2,
                parent_pid=1,
                image_name="observer.exe",
                command_line_status="ok",
                command_line="observer.exe",
                working_directory=str(PROJECT_ROOT.resolve()),
            ),
        ),
    )
    _write_sealed_model(census_path, census)
    fields: dict[str, object] = {
        "schema_version": "gc-recovery-admission/v1",
        "captured_at": captured_at,
        "valid_until": captured_at + validity,
        "current_database": str(current.resolve()),
        "current_database_sha256": _sha256(current),
        "current_revision": _revision(current),
        "baseline_database": str(baseline.resolve()),
        "baseline_database_sha256": _sha256(baseline),
        "baseline_revision": _revision(baseline),
        "baseline_captured_at": baseline_captured_at,
        "baseline_checkpointed": True,
        "baseline_sidecars_absent": True,
        "baseline_capture_method": "activation-rollback-snapshot",
        "baseline_quick_check": "ok",
        "baseline_integrity_check": "ok",
        "baseline_foreign_key_violations": 0,
        "activated_database_sha256": activated_sha256,
        "baseline_authority_artifact": str(authority.resolve()),
        "baseline_authority_artifact_sha256": _sha256(authority),
        "archive_database": str(archive.resolve()),
        "archive_database_sha256": _sha256(archive),
        "operation_started_at": now - timedelta(hours=1),
        "operation_working_directory": str(PROJECT_ROOT.resolve()),
        "operation_command_argv": operation_argv,
        "operation_database": str(current.resolve()),
        "operation_archive_database": str(archive.resolve()),
        "operation_policy": "facts-depth",
        "operation_include_portfolio": True,
        "runtime_git_commit": runtime_git_commit,
        "runtime_db_gc_sha256": _sha256(runtime_source),
        "runtime_repository": str(PROJECT_ROOT.resolve()),
        "runtime_db_gc_artifact": str(runtime_source.resolve()),
        "runtime_db_gc_artifact_size_bytes": runtime_source.stat().st_size,
        "runtime_authority_artifact": str(runtime_authority_path.resolve()),
        "runtime_authority_artifact_sha256": _sha256(runtime_authority_path),
        "runtime_authority_artifact_size_bytes": runtime_authority_path.stat().st_size,
        "event_log_artifact": str(event_log.resolve()),
        "event_log_artifact_sha256": _sha256(event_log),
        "event_log_size_bytes": event_log.stat().st_size,
        "report_artifact": str(report.resolve()),
        "report_artifact_sha256": _sha256(report),
        "report_size_bytes": report.stat().st_size,
        "terminal_status": terminal_status,
        "terminal_exit_code": terminal_exit_code,
        "terminal_artifact": str(terminal_path.resolve()),
        "terminal_artifact_sha256": _sha256(terminal_path),
        "terminal_artifact_size_bytes": terminal_path.stat().st_size,
        "quiescence_registry_artifact": str(quiescence_path.resolve()),
        "quiescence_registry_artifact_sha256": _sha256(quiescence_path),
        "quiescence_registry_artifact_size_bytes": quiescence_path.stat().st_size,
        "expected_task_paths": tasks,
        "disabled_task_paths": tasks,
        "expected_service_names": ("es-dashboard", "es-poller"),
        "stopped_service_names": ("es-dashboard", "es-poller"),
        "expected_listener_endpoints": ("127.0.0.1:7421",),
        "inactive_listener_endpoints": ("127.0.0.1:7421",),
        "process_census_scope": "all-process-command-lines/v1",
        "process_census_command_sha256": "c" * 64,
        "process_census_artifact": str(census_path.resolve()),
        "process_census_artifact_sha256": _sha256(census_path),
        "process_census_artifact_size_bytes": census_path.stat().st_size,
        "process_census_total_count": 2,
        "process_command_line_access_denied_count": 0,
        "database_writer_matches": (),
        "receipt_sha256": "0" * 64,
    }
    unsealed = GcRecoveryAdmissionReceipt.model_validate(fields)
    sealed = unsealed.model_copy(update={"receipt_sha256": unsealed.computed_receipt_sha256()})
    admission.write_bytes((sealed.model_dump_json() + "\n").encode())
    return admission, _sha256(admission)


def _reseal_admission(
    path: Path,
    receipt: GcRecoveryAdmissionReceipt,
    **updates: object,
) -> str:
    unsealed = receipt.model_copy(update={**updates, "receipt_sha256": "0" * 64})
    sealed = unsealed.model_copy(update={"receipt_sha256": unsealed.computed_receipt_sha256()})
    path.write_bytes((sealed.model_dump_json() + "\n").encode())
    return _sha256(path)


def _activation_receipt_sha(admission: Path) -> str:
    receipt = GcRecoveryAdmissionReceipt.model_validate_json(admission.read_bytes())
    authority = RecoveryBaselineAuthority.model_validate_json(
        Path(receipt.baseline_authority_artifact).read_bytes()
    )
    return authority.activation_receipt_artifact_sha256


def _audit(
    current: Path,
    baseline: Path,
    archive: Path,
    *,
    expected_baseline_revision: str = REVISION,
    terminal_status: Literal["complete", "failed", "unknown"] = "complete",
):
    admission, admission_sha = _admission(
        current,
        baseline,
        archive,
        terminal_status=terminal_status,
    )
    return audit_gc_recovery(
        current,
        baseline_database=baseline,
        archive_database=archive,
        admission_receipt_path=admission,
        expected_admission_receipt_sha256=admission_sha,
        expected_activation_receipt_sha256=_activation_receipt_sha(admission),
        expected_current_revision=REVISION,
        expected_baseline_revision=expected_baseline_revision,
    )


def test_exact_target_plane_parity_classifies_rollback_or_noop(tmp_path: Path) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current)
    _database(baseline)
    _archive(archive)

    receipt = _audit(current, baseline, archive)

    assert receipt.outcome is GcRecoveryOutcome.ROLLED_BACK_OR_NOOP
    assert receipt.recovery_ready is True
    assert receipt.financial_facts.missing_from_current_count == 0
    assert receipt.blockers == ()
    assert receipt.report_sha256 == receipt.computed_report_sha256()


def test_windows_write_denial_fence_blocks_concurrent_overwrite(tmp_path: Path) -> None:
    artifact = tmp_path / "fenced-artifact.json"
    artifact.write_text("sealed\n", encoding="utf-8")

    with _REAL_WRITE_DENIAL_FENCE((artifact,)) as mode:
        if os.name != "nt":
            assert mode == "posix-advisory"
            return
        assert mode == "windows-deny-write"
        with pytest.raises(OSError):
            artifact.write_text("mutated\n", encoding="utf-8")

    assert artifact.read_text(encoding="utf-8") == "sealed\n"


def test_legacy_archive_without_row_identity_blocks_recoverable_commit(
    tmp_path: Path,
) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current, include_fact_one=False, include_link=True)
    _database(baseline)
    _archive(archive)

    receipt = _audit(current, baseline, archive)

    assert receipt.outcome is GcRecoveryOutcome.AMBIGUOUS
    assert receipt.recovery_ready is False
    assert receipt.financial_facts.missing_from_current_count == 1
    assert receipt.financial_facts.missing_exact_in_archive_count == 1
    assert receipt.financial_facts.missing_conflicting_archive_count == 0
    assert receipt.governed_linked_candidate_count == 1
    assert receipt.provenance_planes[0].lost_row_count == 0
    assert "archive_rows_lack_run_identity" in receipt.blockers


def test_lost_nonarchived_provenance_blocks_recovery(tmp_path: Path) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current, include_fact_one=False, include_link=False)
    _database(baseline)
    _archive(archive)

    receipt = _audit(current, baseline, archive)

    assert receipt.outcome is GcRecoveryOutcome.AMBIGUOUS
    assert receipt.recovery_ready is False
    assert receipt.governed_linked_candidate_count == 1
    assert {plane.table: plane.lost_row_count for plane in receipt.provenance_planes} == {
        "fact_observation_revisions": 1,
        "fact_selection_decisions": 1,
        "legacy_fact_evidence_match_revisions": 1,
    }
    assert "nonarchived_provenance_rows_lost" in receipt.blockers


def test_multiple_governance_revisions_count_one_missing_fact(tmp_path: Path) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current, include_fact_one=False, include_link=True)
    _database(baseline)
    _archive(archive)
    for database in (current, baseline):
        with sqlite3.connect(database) as conn:
            conn.execute(
                "INSERT INTO fact_observation_revisions VALUES "
                "('financial_facts', 1, 2, 'observation-2')"
            )

    receipt = _audit(current, baseline, archive)

    assert receipt.financial_facts.missing_from_current_count == 1
    assert receipt.governed_linked_candidate_count == 1
    assert "deletion_candidates_not_governed_linked" not in receipt.blockers


def test_provenance_loss_before_fact_delete_is_not_misclassified_as_rollback(
    tmp_path: Path,
) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current, include_fact_one=True, include_link=False)
    _database(baseline)
    _archive(archive)

    receipt = _audit(current, baseline, archive)

    assert receipt.financial_facts.missing_from_current_count == 0
    assert receipt.outcome is GcRecoveryOutcome.AMBIGUOUS
    assert receipt.recovery_ready is False
    assert "nonarchived_provenance_rows_lost" in receipt.blockers


def test_conflicting_archive_payload_blocks_recovery(tmp_path: Path) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current, include_fact_one=False, include_link=True)
    _database(baseline)
    _archive(archive, conflicting=True)

    receipt = _audit(current, baseline, archive)

    assert receipt.outcome is GcRecoveryOutcome.AMBIGUOUS
    assert receipt.recovery_ready is False
    assert receipt.financial_facts.missing_conflicting_archive_count == 1
    assert "archive_payload_conflict" in receipt.blockers


def test_archive_variant_explosion_is_bounded_and_blocks_recovery(tmp_path: Path) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current, include_fact_one=False, include_link=True)
    _database(baseline)
    _archive(archive)
    with sqlite3.connect(archive) as conn:
        conn.executemany(
            "INSERT INTO financial_facts VALUES (1, 'AAA', ?, NULL)",
            ((float(value),) for value in range(100, 200)),
        )

    receipt = _audit(current, baseline, archive)

    assert receipt.financial_facts.archive_variant_overflow_key_count == 1
    assert receipt.recovery_ready is False
    assert "archive_variant_limit_exceeded" in receipt.blockers


def test_matching_but_noncanonical_delete_guard_blocks_recovery(tmp_path: Path) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current)
    _database(baseline)
    _archive(archive)
    for database in (current, baseline):
        with sqlite3.connect(database) as conn:
            conn.execute("DROP TRIGGER trg_financial_facts_observation_delete")
            conn.execute(
                "CREATE TRIGGER trg_financial_facts_observation_delete "
                "BEFORE DELETE ON financial_facts BEGIN "
                "SELECT RAISE(ABORT, 'different guard'); END"
            )

    receipt = _audit(current, baseline, archive)

    assert receipt.delete_trigger_matches_baseline is True
    assert receipt.delete_trigger_matches_canonical is False
    assert receipt.recovery_ready is False
    assert "delete_trigger_differs_from_canonical" in receipt.blockers


def test_retry_admission_binds_exact_self_fk_and_index(tmp_path: Path) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current)
    _database(baseline)
    _archive(archive)

    receipt = _audit(current, baseline, archive)

    assert receipt.facts_depth_retry_fk_ready is True
    assert receipt.facts_depth_retry_index_ready is True
    assert receipt.facts_depth_retry_admission_ready is True


def test_missing_retry_index_blocks_recovery_readiness(tmp_path: Path) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current)
    _database(baseline)
    _archive(archive)
    with sqlite3.connect(current) as conn:
        conn.execute("DROP INDEX ix_0270_financial_facts_supersedes_id")

    receipt = _audit(current, baseline, archive)

    assert receipt.facts_depth_retry_admission_ready is False
    assert receipt.recovery_ready is False
    assert "facts_depth_retry_index_not_ready" in receipt.blockers


def test_missing_retry_self_fk_blocks_recovery_readiness(tmp_path: Path) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current)
    _database(baseline)
    _archive(archive)
    with sqlite3.connect(current) as conn:
        conn.executescript(
            """
            DROP TRIGGER trg_financial_facts_observation_delete;
            DROP INDEX ix_0270_financial_facts_supersedes_id;
            ALTER TABLE financial_facts RENAME TO financial_facts_with_fk;
            CREATE TABLE financial_facts (
                id INTEGER PRIMARY KEY,
                ticker TEXT NOT NULL,
                value REAL NOT NULL,
                supersedes_id INTEGER
            );
            INSERT INTO financial_facts SELECT * FROM financial_facts_with_fk;
            DROP TABLE financial_facts_with_fk;
            CREATE INDEX ix_0270_financial_facts_supersedes_id
                ON financial_facts(supersedes_id);
            """
        )
        conn.execute(TRIGGER_SQL)

    receipt = _audit(current, baseline, archive)

    assert receipt.facts_depth_retry_fk_ready is False
    assert receipt.facts_depth_retry_admission_ready is False
    assert receipt.recovery_ready is False
    assert "facts_depth_retry_self_fk_not_ready" in receipt.blockers


def test_unknown_terminal_status_publishes_typed_blocker(tmp_path: Path) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current)
    _database(baseline)
    _archive(archive)

    receipt = _audit(
        current,
        baseline,
        archive,
        terminal_status="unknown",
    )

    assert receipt.recovery_ready is False
    assert "operation_terminal_status_unknown" in receipt.blockers


def test_failed_terminal_status_publishes_typed_blocker(tmp_path: Path) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current)
    _database(baseline)
    _archive(archive)

    receipt = _audit(
        current,
        baseline,
        archive,
        terminal_status="failed",
    )

    assert receipt.operation_terminal_status == "failed"
    assert receipt.recovery_ready is False
    assert "operation_terminal_status_failed" in receipt.blockers


def test_manifest_must_cover_each_missing_archived_table(tmp_path: Path) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current)
    _database(baseline)
    _archive(archive)
    with sqlite3.connect(current) as conn:
        conn.execute("DELETE FROM metric_computation_attempts")
    with sqlite3.connect(archive) as conn:
        conn.execute(
            "INSERT INTO metric_computation_attempts VALUES (1, 'AAA', 'a' || zeroblob(31))"
        )

    receipt = _audit(current, baseline, archive)

    assert receipt.metric_computation_attempts.missing_exact_in_archive_count == 1
    assert receipt.recovery_ready is False
    assert "archive_manifest_incomplete" in receipt.blockers


def test_absent_empty_optional_attempt_archive_is_typed_zero(tmp_path: Path) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current)
    _database(baseline)
    _archive(archive)
    with sqlite3.connect(archive) as conn:
        conn.execute("DROP TABLE metric_computation_attempts")

    receipt = _audit(current, baseline, archive)

    assert receipt.metric_computation_attempts.archive_row_count == 0
    assert receipt.metric_computation_attempts.missing_from_current_count == 0
    assert receipt.recovery_ready is True


def test_revision_mismatch_blocks_recovery_even_when_target_rows_match(
    tmp_path: Path,
) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current)
    _database(baseline)
    _archive(archive)
    with sqlite3.connect(baseline) as conn:
        conn.execute("UPDATE alembic_version SET version_num = '0259_cutover_readiness_hardening'")

    receipt = _audit(
        current,
        baseline,
        archive,
        expected_baseline_revision="0259_cutover_readiness_hardening",
    )

    assert receipt.outcome is GcRecoveryOutcome.AMBIGUOUS
    assert receipt.recovery_ready is False
    assert "baseline_current_revision_contract_mismatch" in receipt.blockers


def test_artifact_mutation_during_scan_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current)
    _database(baseline)
    _archive(archive)

    import provenance.gc_recovery as recovery

    original = cast(
        Callable[
            [sqlite3.Connection, sqlite3.Connection, sqlite3.Connection],
            object,
        ],
        getattr(recovery, "_audit_tables"),
    )

    def mutate_after_scan(
        current_conn: sqlite3.Connection,
        baseline_conn: sqlite3.Connection,
        archive_conn: sqlite3.Connection,
    ) -> object:
        result = original(current_conn, baseline_conn, archive_conn)
        with baseline.open("ab") as handle:
            handle.write(b"drift")
        return result

    monkeypatch.setattr(recovery, "_audit_tables", mutate_after_scan)

    with pytest.raises(GcRecoveryError, match="changed during recovery audit"):
        _audit(current, baseline, archive)


def test_admission_freshness_is_rechecked_at_publication_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current)
    _database(baseline)
    _archive(archive)

    import provenance.gc_recovery as recovery

    original = cast(
        Callable[[GcRecoveryAdmissionReceipt], None],
        getattr(recovery, "_require_admission_fresh"),
    )
    calls: list[datetime] = []

    def record_freshness(receipt: GcRecoveryAdmissionReceipt) -> None:
        calls.append(datetime.now(UTC))
        original(receipt)

    monkeypatch.setattr(recovery, "_require_admission_fresh", record_freshness)

    receipt = _audit(current, baseline, archive)

    assert receipt.recovery_ready is True
    assert len(calls) == 2


def test_sidecar_appearing_during_scan_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current)
    _database(baseline)
    _archive(archive)

    import provenance.gc_recovery as recovery

    original = cast(
        Callable[
            [sqlite3.Connection, sqlite3.Connection, sqlite3.Connection],
            object,
        ],
        getattr(recovery, "_audit_tables"),
    )

    def create_sidecar_after_scan(
        current_conn: sqlite3.Connection,
        baseline_conn: sqlite3.Connection,
        archive_conn: sqlite3.Connection,
    ) -> object:
        result = original(current_conn, baseline_conn, archive_conn)
        Path(f"{current}-wal").write_bytes(b"writer appeared")
        return result

    monkeypatch.setattr(recovery, "_audit_tables", create_sidecar_after_scan)

    with pytest.raises(GcRecoveryError, match="has sidecars"):
        _audit(current, baseline, archive)


def test_operation_evidence_mutation_during_scan_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current)
    _database(baseline)
    _archive(archive)
    admission, admission_sha = _admission(current, baseline, archive)
    event_log = tmp_path / "gc-events.log"

    import provenance.gc_recovery as recovery

    original = cast(
        Callable[
            [sqlite3.Connection, sqlite3.Connection, sqlite3.Connection],
            object,
        ],
        getattr(recovery, "_audit_tables"),
    )

    def mutate_event_log_after_scan(
        current_conn: sqlite3.Connection,
        baseline_conn: sqlite3.Connection,
        archive_conn: sqlite3.Connection,
    ) -> object:
        result = original(current_conn, baseline_conn, archive_conn)
        event_log.write_text('{"event":"late-write"}\n', encoding="utf-8")
        return result

    monkeypatch.setattr(recovery, "_audit_tables", mutate_event_log_after_scan)

    with pytest.raises(GcRecoveryError, match="admission evidence changed"):
        audit_gc_recovery(
            current,
            baseline_database=baseline,
            archive_database=archive,
            admission_receipt_path=admission,
            expected_admission_receipt_sha256=admission_sha,
            expected_activation_receipt_sha256=_activation_receipt_sha(admission),
            expected_current_revision=REVISION,
            expected_baseline_revision=REVISION,
        )


def test_support_mutation_during_final_database_hash_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current)
    _database(baseline)
    _archive(archive)
    admission, admission_sha = _admission(current, baseline, archive)
    event_log = tmp_path / "gc-events.log"

    import provenance.gc_recovery as recovery

    original = cast(Callable[[Path], object], getattr(recovery, "_snapshot_artifact"))
    calls = 0

    def mutate_support_during_final_hash(path: Path) -> object:
        nonlocal calls
        calls += 1
        if calls == 7:
            event_log.write_text('{"event":"late-spoof"}\n', encoding="utf-8")
        return original(path)

    monkeypatch.setattr(recovery, "_snapshot_artifact", mutate_support_during_final_hash)

    with pytest.raises(GcRecoveryError, match="admission evidence changed"):
        audit_gc_recovery(
            current,
            baseline_database=baseline,
            archive_database=archive,
            admission_receipt_path=admission,
            expected_admission_receipt_sha256=admission_sha,
            expected_activation_receipt_sha256=_activation_receipt_sha(admission),
            expected_current_revision=REVISION,
            expected_baseline_revision=REVISION,
        )


def test_freshness_is_last_check_after_final_evidence_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current)
    _database(baseline)
    _archive(archive)
    admission, admission_sha = _admission(current, baseline, archive)

    import provenance.gc_recovery as recovery

    original_snapshot = cast(
        Callable[[Path], object],
        getattr(recovery, "_snapshot_artifact"),
    )
    original_fresh = cast(
        Callable[[GcRecoveryAdmissionReceipt], None],
        getattr(recovery, "_require_admission_fresh"),
    )
    snapshot_calls = 0
    freshness_calls = 0

    def record_snapshot(path: Path) -> object:
        nonlocal snapshot_calls
        snapshot_calls += 1
        return original_snapshot(path)

    def expire_at_final_check(receipt: GcRecoveryAdmissionReceipt) -> None:
        nonlocal freshness_calls
        freshness_calls += 1
        if freshness_calls == 2:
            assert snapshot_calls == 9
            raise GcRecoveryError("recovery admission expired before publication")
        original_fresh(receipt)

    monkeypatch.setattr(recovery, "_snapshot_artifact", record_snapshot)
    monkeypatch.setattr(recovery, "_require_admission_fresh", expire_at_final_check)

    with pytest.raises(GcRecoveryError, match="expired before publication"):
        audit_gc_recovery(
            current,
            baseline_database=baseline,
            archive_database=archive,
            admission_receipt_path=admission,
            expected_admission_receipt_sha256=admission_sha,
            expected_activation_receipt_sha256=_activation_receipt_sha(admission),
            expected_current_revision=REVISION,
            expected_baseline_revision=REVISION,
        )


def test_reviewed_admission_file_commitment_is_required(tmp_path: Path) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current)
    _database(baseline)
    _archive(archive)
    admission, admission_sha = _admission(current, baseline, archive)
    admission.write_bytes(admission.read_bytes() + b" ")

    with pytest.raises(GcRecoveryError, match="reviewed file commitment"):
        audit_gc_recovery(
            current,
            baseline_database=baseline,
            archive_database=archive,
            admission_receipt_path=admission,
            expected_admission_receipt_sha256=admission_sha,
            expected_activation_receipt_sha256=_activation_receipt_sha(admission),
            expected_current_revision=REVISION,
            expected_baseline_revision=REVISION,
        )


def test_effective_gc_argv_rejects_last_wins_policy_and_database_spoof(
    tmp_path: Path,
) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current)
    _database(baseline)
    _archive(archive)
    admission, _admission_sha = _admission(current, baseline, archive)
    receipt = GcRecoveryAdmissionReceipt.model_validate_json(admission.read_bytes())
    spoofed_argv = (
        *receipt.operation_command_argv,
        "--policies",
        "telemetry",
        "--db-path",
        str(tmp_path / "other.db"),
    )
    terminal_path = Path(receipt.terminal_artifact)
    terminal = RecoveryTerminalEvidence.model_validate_json(terminal_path.read_bytes())
    terminal = terminal.model_copy(update={"command_argv_sha256": _canonical_sha(spoofed_argv)})
    terminal_path.write_bytes((terminal.model_dump_json() + "\n").encode())
    admission_sha = _reseal_admission(
        admission,
        receipt,
        operation_command_argv=spoofed_argv,
        terminal_artifact_sha256=_sha256(terminal_path),
        terminal_artifact_size_bytes=terminal_path.stat().st_size,
    )

    with pytest.raises(GcRecoveryError, match="exactly one --db-path"):
        audit_gc_recovery(
            current,
            baseline_database=baseline,
            archive_database=archive,
            admission_receipt_path=admission,
            expected_admission_receipt_sha256=admission_sha,
            expected_activation_receipt_sha256=_activation_receipt_sha(admission),
            expected_current_revision=REVISION,
            expected_baseline_revision=REVISION,
        )


def test_terminal_report_semantics_must_reproduce_operation(tmp_path: Path) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current)
    _database(baseline)
    _archive(archive)
    admission, _admission_sha = _admission(current, baseline, archive)
    receipt = GcRecoveryAdmissionReceipt.model_validate_json(admission.read_bytes())
    report_path = Path(receipt.report_artifact)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert isinstance(report, dict)
    report["db_path"] = str(tmp_path / "other.db")
    report_path.write_text(json.dumps(report, separators=(",", ":")) + "\n", encoding="utf-8")
    terminal_path = Path(receipt.terminal_artifact)
    terminal = RecoveryTerminalEvidence.model_validate_json(terminal_path.read_bytes())
    terminal = terminal.model_copy(update={"stdout_sha256": _sha256(report_path)})
    terminal_path.write_bytes((terminal.model_dump_json() + "\n").encode())
    admission_sha = _reseal_admission(
        admission,
        receipt,
        report_artifact_sha256=_sha256(report_path),
        report_size_bytes=report_path.stat().st_size,
        terminal_artifact_sha256=_sha256(terminal_path),
        terminal_artifact_size_bytes=terminal_path.stat().st_size,
    )

    with pytest.raises(GcRecoveryError, match="report database differs"):
        audit_gc_recovery(
            current,
            baseline_database=baseline,
            archive_database=archive,
            admission_receipt_path=admission,
            expected_admission_receipt_sha256=admission_sha,
            expected_activation_receipt_sha256=_activation_receipt_sha(admission),
            expected_current_revision=REVISION,
            expected_baseline_revision=REVISION,
        )


def test_terminal_report_deletion_counts_must_equal_exhaustive_delta(tmp_path: Path) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current)
    _database(baseline)
    _archive(archive)
    admission, _admission_sha = _admission(current, baseline, archive)
    receipt = GcRecoveryAdmissionReceipt.model_validate_json(admission.read_bytes())
    report_path = Path(receipt.report_artifact)
    report_raw: object = json.loads(report_path.read_text(encoding="utf-8"))
    assert isinstance(report_raw, dict)
    report = cast(dict[str, object], report_raw)
    policies_raw = report["policies"]
    assert isinstance(policies_raw, list)
    policies = cast(list[object], policies_raw)
    policy_raw = policies[0]
    assert isinstance(policy_raw, dict)
    policy = cast(dict[str, object], policy_raw)
    policy["rows_deleted"] = {"financial_facts": 999}
    report_path.write_text(json.dumps(report, separators=(",", ":")) + "\n", encoding="utf-8")
    event_path = Path(receipt.event_log_artifact)
    event_path.write_text(
        '{"event":"gc_append_only_guard_window"}\n{"event":"gc_batch_commit"}\n',
        encoding="utf-8",
    )
    terminal_path = Path(receipt.terminal_artifact)
    terminal = RecoveryTerminalEvidence.model_validate_json(terminal_path.read_bytes())
    terminal = terminal.model_copy(
        update={
            "stdout_sha256": _sha256(report_path),
            "stderr_sha256": _sha256(event_path),
        }
    )
    _write_sealed_model(terminal_path, terminal)
    admission_sha = _reseal_admission(
        admission,
        receipt,
        report_artifact_sha256=_sha256(report_path),
        report_size_bytes=report_path.stat().st_size,
        event_log_artifact_sha256=_sha256(event_path),
        event_log_size_bytes=event_path.stat().st_size,
        terminal_artifact_sha256=_sha256(terminal_path),
        terminal_artifact_size_bytes=terminal_path.stat().st_size,
    )

    with pytest.raises(GcRecoveryError, match="deletion count differs for financial_facts"):
        audit_gc_recovery(
            current,
            baseline_database=baseline,
            archive_database=archive,
            admission_receipt_path=admission,
            expected_admission_receipt_sha256=admission_sha,
            expected_activation_receipt_sha256=_activation_receipt_sha(admission),
            expected_current_revision=REVISION,
            expected_baseline_revision=REVISION,
        )


def test_raw_process_census_must_reproduce_receipt_counts(tmp_path: Path) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current)
    _database(baseline)
    _archive(archive)
    admission, _admission_sha = _admission(current, baseline, archive)
    receipt = GcRecoveryAdmissionReceipt.model_validate_json(admission.read_bytes())
    census_path = Path(receipt.process_census_artifact)
    census = RecoveryProcessCensus.model_validate_json(census_path.read_bytes())
    changed = census.model_copy(
        update={
            "processes": (
                *census.processes,
                ProcessCensusObservation(
                    pid=3,
                    parent_pid=1,
                    image_name="writer.exe",
                    command_line_status="ok",
                    command_line="writer.exe",
                    working_directory=str(PROJECT_ROOT.resolve()),
                ),
            )
        }
    )
    census_path.write_bytes((changed.model_dump_json() + "\n").encode())
    admission_sha = _reseal_admission(
        admission,
        receipt,
        process_census_artifact_sha256=_sha256(census_path),
        process_census_artifact_size_bytes=census_path.stat().st_size,
    )

    with pytest.raises(GcRecoveryError, match="process census total differs"):
        audit_gc_recovery(
            current,
            baseline_database=baseline,
            archive_database=archive,
            admission_receipt_path=admission,
            expected_admission_receipt_sha256=admission_sha,
            expected_activation_receipt_sha256=_activation_receipt_sha(admission),
            expected_current_revision=REVISION,
            expected_baseline_revision=REVISION,
        )


@pytest.mark.parametrize(
    ("invocation_kind", "expected_evidence"),
    (
        ("script", "derived:db_gc_apply_current_database"),
        ("module", "derived:db_gc_apply_current_database"),
        ("runpy", "derived:ambiguous_db_gc_apply"),
        ("sqlite", "derived:sqlite_cli_current_database"),
    ),
)
def test_raw_process_census_derives_unlabeled_database_writer(
    tmp_path: Path,
    invocation_kind: str,
    expected_evidence: str,
) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current)
    _database(baseline)
    _archive(archive)
    admission, _admission_sha = _admission(current, baseline, archive)
    receipt = GcRecoveryAdmissionReceipt.model_validate_json(admission.read_bytes())
    census_path = Path(receipt.process_census_artifact)
    census = RecoveryProcessCensus.model_validate_json(census_path.read_bytes())
    writer_pid = 3
    common_arguments = (
        "--apply",
        "--db-path",
        str(current.resolve()),
        "--policies",
        "facts-depth",
        "--include-portfolio",
    )
    if invocation_kind == "script":
        writer_argv = ("python", "execution/db_gc.py", *common_arguments)
        image_name = "python.exe"
    elif invocation_kind == "module":
        writer_argv = ("python", "-m", "execution.db_gc", *common_arguments)
        image_name = "python.exe"
    elif invocation_kind == "runpy":
        writer_argv = (
            "python",
            "-c",
            "import runpy; runpy.run_module('execution.db_gc'); --apply",
        )
        image_name = "python.exe"
    else:
        writer_argv = ("sqlite3", str(current.resolve()))
        image_name = "sqlite3.exe"
    writer = ProcessCensusObservation(
        pid=writer_pid,
        parent_pid=1,
        image_name=image_name,
        command_line_status="ok",
        command_line=subprocess.list2cmdline(writer_argv),
        working_directory=str(PROJECT_ROOT.resolve()),
    )
    changed = census.model_copy(update={"processes": (*census.processes, writer)})
    _write_sealed_model(census_path, changed)
    admission_sha = _reseal_admission(
        admission,
        receipt,
        process_census_artifact_sha256=_sha256(census_path),
        process_census_artifact_size_bytes=census_path.stat().st_size,
        process_census_total_count=len(changed.processes),
        database_writer_matches=(f"{writer_pid}:{expected_evidence}",),
    )

    recovery = audit_gc_recovery(
        current,
        baseline_database=baseline,
        archive_database=archive,
        admission_receipt_path=admission,
        expected_admission_receipt_sha256=admission_sha,
        expected_activation_receipt_sha256=_activation_receipt_sha(admission),
        expected_current_revision=REVISION,
        expected_baseline_revision=REVISION,
    )

    assert recovery.recovery_ready is False
    assert "database_writer_still_present" in recovery.blockers


def test_windows_command_line_parser_round_trips_quoted_gc_invocation() -> None:
    argv = (
        r"C:\Program Files\Python\python.exe",
        r"C:\repo with spaces\execution\db_gc.py",
        "--apply",
        "--db-path",
        r"C:\data with spaces\portfolio.db",
        'literal"quote',
        "trailing\\",
    )

    assert _PARSE_WINDOWS_COMMAND_LINE(subprocess.list2cmdline(argv)) == argv


def test_windows_command_line_parser_rejects_unmatched_quote() -> None:
    with pytest.raises(ValueError, match="unmatched quote"):
        _PARSE_WINDOWS_COMMAND_LINE('python "execution/db_gc.py --apply')


@pytest.mark.parametrize(
    "image_name",
    (
        "python.exe",
        "python3.13.exe",
        "dotnet.exe",
        "svchost.exe",
        "custom-maintenance.exe",
        "unknown",
    ),
)
def test_internal_live_process_census_blocks_inaccessible_writer_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    image_name: str,
) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current)
    _database(baseline)
    _archive(archive)

    import provenance.gc_recovery as recovery

    unsealed = LiveProcessCensus(
        schema_version="gc-recovery-live-process-census/v1",
        captured_at=datetime.now(UTC),
        collector="powershell-get-ciminstance-win32-process/v1",
        exit_code=0,
        processes=(
            ProcessCensusObservation(
                pid=3,
                parent_pid=1,
                image_name=image_name,
                command_line_status="access_denied",
                command_line=None,
                working_directory=None,
            ),
        ),
        census_sha256="0" * 64,
    )
    incomplete = unsealed.model_copy(update={"census_sha256": unsealed.computed_census_sha256()})
    monkeypatch.setattr(recovery, "_collect_live_process_census", lambda: incomplete)

    receipt = _audit(current, baseline, archive)

    assert receipt.recovery_ready is False
    assert "live_process_census_incomplete" in receipt.blockers


@pytest.mark.parametrize(
    "image_name",
    ("System Idle Process", "System", "Secure System", "Registry", "Memory Compression"),
)
def test_internal_live_process_census_records_inert_kernel_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    image_name: str,
) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current)
    _database(baseline)
    _archive(archive)

    import provenance.gc_recovery as recovery

    unsealed = LiveProcessCensus(
        schema_version="gc-recovery-live-process-census/v1",
        captured_at=datetime.now(UTC),
        collector="powershell-get-ciminstance-win32-process/v1",
        exit_code=0,
        processes=(
            ProcessCensusObservation(
                pid=4,
                parent_pid=0,
                image_name=image_name,
                command_line_status="access_denied",
                command_line=None,
                working_directory=None,
            ),
        ),
        census_sha256="0" * 64,
    )
    census = unsealed.model_copy(update={"census_sha256": unsealed.computed_census_sha256()})
    monkeypatch.setattr(recovery, "_collect_live_process_census", lambda: census)

    receipt = _audit(current, baseline, archive)

    assert "live_process_census_incomplete" not in receipt.blockers


@pytest.mark.parametrize(
    ("image_name", "expected_blocked"),
    (
        ("System", False),
        ("svchost.exe", True),
        ("python.exe", True),
        ("python3.13.exe", True),
        ("dotnet.exe", True),
        ("custom-maintenance.exe", True),
        ("unknown", True),
    ),
)
def test_admitted_process_census_distinguishes_inaccessible_writer_capability(
    tmp_path: Path,
    image_name: str,
    expected_blocked: bool,
) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current)
    _database(baseline)
    _archive(archive)
    admission, _admission_sha = _admission(current, baseline, archive)
    receipt = GcRecoveryAdmissionReceipt.model_validate_json(admission.read_bytes())
    census_path = Path(receipt.process_census_artifact)
    census = RecoveryProcessCensus.model_validate_json(census_path.read_bytes())
    inaccessible = ProcessCensusObservation(
        pid=4,
        parent_pid=0,
        image_name=image_name,
        command_line_status="access_denied",
        command_line=None,
        working_directory=None,
    )
    changed = census.model_copy(update={"processes": (*census.processes, inaccessible)})
    _write_sealed_model(census_path, changed)
    admission_sha = _reseal_admission(
        admission,
        receipt,
        process_census_artifact_sha256=_sha256(census_path),
        process_census_artifact_size_bytes=census_path.stat().st_size,
        process_census_total_count=len(changed.processes),
        process_command_line_access_denied_count=1,
    )

    recovery = audit_gc_recovery(
        current,
        baseline_database=baseline,
        archive_database=archive,
        admission_receipt_path=admission,
        expected_admission_receipt_sha256=admission_sha,
        expected_activation_receipt_sha256=_activation_receipt_sha(admission),
        expected_current_revision=REVISION,
        expected_baseline_revision=REVISION,
    )

    assert ("process_census_incomplete" in recovery.blockers) is expected_blocked


def test_runtime_authority_requires_available_committed_blob(tmp_path: Path) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current)
    _database(baseline)
    _archive(archive)
    admission, _admission_sha = _admission(current, baseline, archive)
    receipt = GcRecoveryAdmissionReceipt.model_validate_json(admission.read_bytes())
    authority_path = Path(receipt.runtime_authority_artifact)
    authority = RecoveryRuntimeAuthority.model_validate_json(authority_path.read_bytes())
    unavailable_commit = "f" * 40
    changed = authority.model_copy(update={"git_commit": unavailable_commit})
    _write_sealed_model(authority_path, changed)
    admission_sha = _reseal_admission(
        admission,
        receipt,
        runtime_git_commit=unavailable_commit,
        runtime_authority_artifact_sha256=_sha256(authority_path),
        runtime_authority_artifact_size_bytes=authority_path.stat().st_size,
    )

    with pytest.raises(GcRecoveryError, match="not canonical checkout HEAD"):
        audit_gc_recovery(
            current,
            baseline_database=baseline,
            archive_database=archive,
            admission_receipt_path=admission,
            expected_admission_receipt_sha256=admission_sha,
            expected_activation_receipt_sha256=_activation_receipt_sha(admission),
            expected_current_revision=REVISION,
            expected_baseline_revision=REVISION,
        )


def test_runtime_authority_rejects_caller_named_repository(tmp_path: Path) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current)
    _database(baseline)
    _archive(archive)
    admission, _admission_sha = _admission(current, baseline, archive)
    receipt = GcRecoveryAdmissionReceipt.model_validate_json(admission.read_bytes())
    authority_path = Path(receipt.runtime_authority_artifact)
    authority = RecoveryRuntimeAuthority.model_validate_json(authority_path.read_bytes())
    attacker_repository = tmp_path / "attacker-repository"
    attacker_repository.mkdir()
    changed = authority.model_copy(update={"repository": str(attacker_repository.resolve())})
    _write_sealed_model(authority_path, changed)
    admission_sha = _reseal_admission(
        admission,
        receipt,
        runtime_repository=str(attacker_repository.resolve()),
        runtime_authority_artifact_sha256=_sha256(authority_path),
        runtime_authority_artifact_size_bytes=authority_path.stat().st_size,
    )

    with pytest.raises(GcRecoveryError, match="not the canonical checkout"):
        audit_gc_recovery(
            current,
            baseline_database=baseline,
            archive_database=archive,
            admission_receipt_path=admission,
            expected_admission_receipt_sha256=admission_sha,
            expected_activation_receipt_sha256=_activation_receipt_sha(admission),
            expected_current_revision=REVISION,
            expected_baseline_revision=REVISION,
        )


def test_scheduler_tasks_must_match_committed_manifest(tmp_path: Path) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current)
    _database(baseline)
    _archive(archive)
    admission, _admission_sha = _admission(current, baseline, archive)
    receipt = GcRecoveryAdmissionReceipt.model_validate_json(admission.read_bytes())
    quiescence_path = Path(receipt.quiescence_registry_artifact)
    quiescence = RecoveryQuiescenceRegistry.model_validate_json(quiescence_path.read_bytes())
    fabricated_path = r"\earnings-summary\fabricated-task"
    changed = quiescence.model_copy(
        update={
            "tasks": (
                QuiescedTaskObservation(
                    path=fabricated_path,
                    state="Disabled",
                    enabled=False,
                ),
            )
        }
    )
    _write_sealed_model(quiescence_path, changed)
    admission_sha = _reseal_admission(
        admission,
        receipt,
        expected_task_paths=(fabricated_path,),
        disabled_task_paths=(fabricated_path,),
        quiescence_registry_artifact_sha256=_sha256(quiescence_path),
        quiescence_registry_artifact_size_bytes=quiescence_path.stat().st_size,
    )

    with pytest.raises(GcRecoveryError, match="committed canonical task"):
        audit_gc_recovery(
            current,
            baseline_database=baseline,
            archive_database=archive,
            admission_receipt_path=admission,
            expected_admission_receipt_sha256=admission_sha,
            expected_activation_receipt_sha256=_activation_receipt_sha(admission),
            expected_current_revision=REVISION,
            expected_baseline_revision=REVISION,
        )


def test_noop_report_rejects_arbitrary_event_stream(tmp_path: Path) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current)
    _database(baseline)
    _archive(archive)
    admission, _admission_sha = _admission(current, baseline, archive)
    receipt = GcRecoveryAdmissionReceipt.model_validate_json(admission.read_bytes())
    event_path = Path(receipt.event_log_artifact)
    event_path.write_text('{"event":"arbitrary"}\n', encoding="utf-8")
    terminal_path = Path(receipt.terminal_artifact)
    terminal = RecoveryTerminalEvidence.model_validate_json(terminal_path.read_bytes())
    changed_terminal = terminal.model_copy(update={"stderr_sha256": _sha256(event_path)})
    _write_sealed_model(terminal_path, changed_terminal)
    admission_sha = _reseal_admission(
        admission,
        receipt,
        event_log_artifact_sha256=_sha256(event_path),
        event_log_size_bytes=event_path.stat().st_size,
        terminal_artifact_sha256=_sha256(terminal_path),
        terminal_artifact_size_bytes=terminal_path.stat().st_size,
    )

    with pytest.raises(GcRecoveryError, match="no-op facts-depth report"):
        audit_gc_recovery(
            current,
            baseline_database=baseline,
            archive_database=archive,
            admission_receipt_path=admission,
            expected_admission_receipt_sha256=admission_sha,
            expected_activation_receipt_sha256=_activation_receipt_sha(admission),
            expected_current_revision=REVISION,
            expected_baseline_revision=REVISION,
        )


def test_baseline_authority_must_match_admission(tmp_path: Path) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current)
    _database(baseline)
    _archive(archive)
    admission, _admission_sha = _admission(current, baseline, archive)
    receipt = GcRecoveryAdmissionReceipt.model_validate_json(admission.read_bytes())
    authority_path = Path(receipt.baseline_authority_artifact)
    authority = RecoveryBaselineAuthority.model_validate_json(authority_path.read_bytes())
    unsealed = authority.model_copy(
        update={"baseline_revision": "fabricated-revision", "receipt_sha256": "0" * 64}
    )
    changed = unsealed.model_copy(update={"receipt_sha256": unsealed.computed_receipt_sha256()})
    _write_sealed_model(authority_path, changed)
    admission_sha = _reseal_admission(
        admission,
        receipt,
        baseline_authority_artifact_sha256=_sha256(authority_path),
    )

    with pytest.raises(GcRecoveryError, match="baseline authority revision differs"):
        audit_gc_recovery(
            current,
            baseline_database=baseline,
            archive_database=archive,
            admission_receipt_path=admission,
            expected_admission_receipt_sha256=admission_sha,
            expected_activation_receipt_sha256=_activation_receipt_sha(admission),
            expected_current_revision=REVISION,
            expected_baseline_revision=REVISION,
        )


def test_baseline_authority_self_seal_is_required(tmp_path: Path) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current)
    _database(baseline)
    _archive(archive)
    admission, _admission_sha = _admission(current, baseline, archive)
    receipt = GcRecoveryAdmissionReceipt.model_validate_json(admission.read_bytes())
    authority_path = Path(receipt.baseline_authority_artifact)
    authority = RecoveryBaselineAuthority.model_validate_json(authority_path.read_bytes())
    changed = authority.model_copy(update={"baseline_revision": "tampered-revision"})
    _write_sealed_model(authority_path, changed)
    admission_sha = _reseal_admission(
        admission,
        receipt,
        baseline_authority_artifact_sha256=_sha256(authority_path),
    )

    with pytest.raises(GcRecoveryError, match="baseline authority self-seal is invalid"):
        audit_gc_recovery(
            current,
            baseline_database=baseline,
            archive_database=archive,
            admission_receipt_path=admission,
            expected_admission_receipt_sha256=admission_sha,
            expected_activation_receipt_sha256=_activation_receipt_sha(admission),
            expected_current_revision=REVISION,
            expected_baseline_revision=REVISION,
        )


def test_activation_receipt_requires_independent_file_commitment(tmp_path: Path) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current)
    _database(baseline)
    _archive(archive)
    admission, _admission_sha = _admission(current, baseline, archive)
    original_activation_sha = _activation_receipt_sha(admission)
    admission_receipt = GcRecoveryAdmissionReceipt.model_validate_json(admission.read_bytes())
    authority_path = Path(admission_receipt.baseline_authority_artifact)
    authority = RecoveryBaselineAuthority.model_validate_json(authority_path.read_bytes())
    activation_path = Path(authority.activation_receipt_artifact)
    activation = ActivationReceipt.model_validate_json(activation_path.read_bytes())
    unsealed_activation = activation.model_copy(
        update={
            "activation_mechanism": "portable_rename_pair",
            "receipt_sha256": "0" * 64,
        }
    )
    spoofed_activation = unsealed_activation.model_copy(
        update={"receipt_sha256": activation_payload_sha256(unsealed_activation)}
    )
    _write_activation_receipt(activation_path, spoofed_activation)
    unsealed_authority = authority.model_copy(
        update={
            "activation_receipt_artifact_sha256": _sha256(activation_path),
            "activation_receipt_artifact_size_bytes": activation_path.stat().st_size,
            "receipt_sha256": "0" * 64,
        }
    )
    changed_authority = unsealed_authority.model_copy(
        update={"receipt_sha256": unsealed_authority.computed_receipt_sha256()}
    )
    _write_sealed_model(authority_path, changed_authority)
    admission_sha = _reseal_admission(
        admission,
        admission_receipt,
        baseline_authority_artifact_sha256=_sha256(authority_path),
    )

    with pytest.raises(GcRecoveryError, match="independent file commitment"):
        audit_gc_recovery(
            current,
            baseline_database=baseline,
            archive_database=archive,
            admission_receipt_path=admission,
            expected_admission_receipt_sha256=admission_sha,
            expected_activation_receipt_sha256=original_activation_sha,
            expected_current_revision=REVISION,
            expected_baseline_revision=REVISION,
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("repo_root", "canonical checkout"),
        ("live_database", "different live database"),
        ("expected_alembic_head", "admitted current revision"),
        ("candidate_database", "database lineage is inconsistent"),
        ("active_postcheck_database", "database lineage is inconsistent"),
        ("active_sha", "active hash differs from admission"),
        ("quiescence_receipt_sha256", "quiescence commitment is unresolved"),
    ),
)
def test_activation_receipt_semantics_are_cross_bound(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current)
    _database(baseline)
    _archive(archive)
    admission, _admission_sha = _admission(current, baseline, archive)
    admission_receipt = GcRecoveryAdmissionReceipt.model_validate_json(admission.read_bytes())
    authority_path = Path(admission_receipt.baseline_authority_artifact)
    authority = RecoveryBaselineAuthority.model_validate_json(authority_path.read_bytes())
    activation_path = Path(authority.activation_receipt_artifact)
    activation = ActivationReceipt.model_validate_json(activation_path.read_bytes())
    updates: dict[str, object] = {"receipt_sha256": "0" * 64}
    if mutation == "repo_root":
        updates[mutation] = str((tmp_path / "untrusted-repo").resolve())
    elif mutation == "live_database":
        updates[mutation] = str((tmp_path / "other-live.db").resolve())
    elif mutation == "expected_alembic_head":
        updates[mutation] = "unrelated_revision"
    elif mutation == "candidate_database":
        updates[mutation] = str((tmp_path / "other-candidate.db").resolve())
    elif mutation == "active_postcheck_database":
        assert activation.active_postcheck is not None
        updates["active_postcheck"] = activation.active_postcheck.model_copy(
            update={"database": str((tmp_path / "other-active.db").resolve())}
        )
    elif mutation == "active_sha":
        assert activation.active_postcheck is not None
        updates.update(
            {
                "candidate_sha256": "b" * 64,
                "active_sha256_after": "b" * 64,
                "candidate_precheck": activation.candidate_precheck.model_copy(
                    update={"sha256": "b" * 64}
                ),
                "active_postcheck": activation.active_postcheck.model_copy(
                    update={"sha256": "b" * 64}
                ),
            }
        )
    else:
        updates[mutation] = "b" * 64
    unsealed_activation = activation.model_copy(update=updates)
    changed_activation = unsealed_activation.model_copy(
        update={"receipt_sha256": activation_payload_sha256(unsealed_activation)}
    )
    _write_activation_receipt(activation_path, changed_activation)
    unsealed_authority = authority.model_copy(
        update={
            "activation_receipt_artifact_sha256": _sha256(activation_path),
            "activation_receipt_artifact_size_bytes": activation_path.stat().st_size,
            "receipt_sha256": "0" * 64,
        }
    )
    changed_authority = unsealed_authority.model_copy(
        update={"receipt_sha256": unsealed_authority.computed_receipt_sha256()}
    )
    _write_sealed_model(authority_path, changed_authority)
    admission_sha = _reseal_admission(
        admission,
        admission_receipt,
        baseline_authority_artifact_sha256=_sha256(authority_path),
    )

    with pytest.raises(GcRecoveryError, match=message):
        audit_gc_recovery(
            current,
            baseline_database=baseline,
            archive_database=archive,
            admission_receipt_path=admission,
            expected_admission_receipt_sha256=admission_sha,
            expected_activation_receipt_sha256=_sha256(activation_path),
            expected_current_revision=REVISION,
            expected_baseline_revision=REVISION,
        )


def test_activation_quiescence_semantics_are_cross_bound(tmp_path: Path) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current)
    _database(baseline)
    _archive(archive)
    admission, _admission_sha = _admission(current, baseline, archive)
    admission_receipt = GcRecoveryAdmissionReceipt.model_validate_json(admission.read_bytes())
    authority_path = Path(admission_receipt.baseline_authority_artifact)
    authority = RecoveryBaselineAuthority.model_validate_json(authority_path.read_bytes())
    quiescence_path = Path(authority.activation_quiescence_artifact)
    quiescence = CutoverQuiescenceReceipt.model_validate_json(quiescence_path.read_bytes())
    unsealed_quiescence = quiescence.model_copy(
        update={
            "live_database_sha256": "b" * 64,
            "receipt_sha256": "0" * 64,
        }
    )
    changed_quiescence = unsealed_quiescence.model_copy(
        update={"receipt_sha256": quiescence_payload_sha256(unsealed_quiescence)}
    )
    _write_cutover_quiescence(quiescence_path, changed_quiescence)
    activation_path = Path(authority.activation_receipt_artifact)
    activation = ActivationReceipt.model_validate_json(activation_path.read_bytes())
    unsealed_activation = activation.model_copy(
        update={
            "quiescence_receipt_sha256": changed_quiescence.receipt_sha256,
            "receipt_sha256": "0" * 64,
        }
    )
    changed_activation = unsealed_activation.model_copy(
        update={"receipt_sha256": activation_payload_sha256(unsealed_activation)}
    )
    _write_activation_receipt(activation_path, changed_activation)
    unsealed_authority = authority.model_copy(
        update={
            "activation_receipt_artifact_sha256": _sha256(activation_path),
            "activation_receipt_artifact_size_bytes": activation_path.stat().st_size,
            "activation_quiescence_artifact_sha256": _sha256(quiescence_path),
            "activation_quiescence_artifact_size_bytes": quiescence_path.stat().st_size,
            "receipt_sha256": "0" * 64,
        }
    )
    changed_authority = unsealed_authority.model_copy(
        update={"receipt_sha256": unsealed_authority.computed_receipt_sha256()}
    )
    _write_sealed_model(authority_path, changed_authority)
    admission_sha = _reseal_admission(
        admission,
        admission_receipt,
        baseline_authority_artifact_sha256=_sha256(authority_path),
    )

    with pytest.raises(GcRecoveryError, match="live hash differs"):
        audit_gc_recovery(
            current,
            baseline_database=baseline,
            archive_database=archive,
            admission_receipt_path=admission,
            expected_admission_receipt_sha256=admission_sha,
            expected_activation_receipt_sha256=_sha256(activation_path),
            expected_current_revision=REVISION,
            expected_baseline_revision=REVISION,
        )


def test_support_artifact_external_hardlink_is_refused(tmp_path: Path) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current)
    _database(baseline)
    _archive(archive)
    admission, admission_sha = _admission(current, baseline, archive)
    admission_receipt = GcRecoveryAdmissionReceipt.model_validate_json(admission.read_bytes())
    report = Path(admission_receipt.report_artifact)
    try:
        os.link(report, tmp_path / "external-report-alias.json")
    except OSError as exc:
        pytest.skip(f"hardlink creation unavailable: {exc}")

    with pytest.raises(GcRecoveryError, match="external hardlink"):
        audit_gc_recovery(
            current,
            baseline_database=baseline,
            archive_database=archive,
            admission_receipt_path=admission,
            expected_admission_receipt_sha256=admission_sha,
            expected_activation_receipt_sha256=_activation_receipt_sha(admission),
            expected_current_revision=REVISION,
            expected_baseline_revision=REVISION,
        )


def test_fast_replay_refuses_database_drift_after_support_recheck(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current)
    _database(baseline)
    _archive(archive)
    admission, admission_sha = _admission(current, baseline, archive)
    recovery_receipt = audit_gc_recovery(
        current,
        baseline_database=baseline,
        archive_database=archive,
        admission_receipt_path=admission,
        expected_admission_receipt_sha256=admission_sha,
        expected_activation_receipt_sha256=_activation_receipt_sha(admission),
        expected_current_revision=REVISION,
        expected_baseline_revision=REVISION,
    )
    recovery_path = tmp_path / "gc-recovery.json"
    _write_sealed_model(recovery_path, recovery_receipt)

    import provenance.gc_recovery as recovery

    original = cast(Callable[[Path], object], getattr(recovery, "_snapshot_artifact"))
    calls = 0
    mutated = False

    def mutate_before_final_snapshot(path: Path) -> object:
        nonlocal calls, mutated
        calls += 1
        if calls == 4 and not mutated:
            with sqlite3.connect(current) as conn:
                conn.execute("UPDATE financial_facts SET value = value + 1 WHERE id = 2")
            mutated = True
        return original(path)

    monkeypatch.setattr(recovery, "_snapshot_artifact", mutate_before_final_snapshot)

    with pytest.raises(GcRecoveryError, match="changed before replay publication"):
        validate_gc_recovery_replay(
            current,
            baseline_database=baseline,
            archive_database=archive,
            admission_receipt_path=admission,
            expected_admission_receipt_sha256=admission_sha,
            expected_activation_receipt_sha256=_activation_receipt_sha(admission),
            expected_current_revision=REVISION,
            expected_baseline_revision=REVISION,
            recovery_receipt_path=recovery_path,
            expected_recovery_receipt_sha256=_sha256(recovery_path),
        )


def test_replay_requires_independent_recovery_receipt_commitment(tmp_path: Path) -> None:
    current, baseline, archive = _paths(tmp_path)
    output = tmp_path / "gc-recovery.json"
    _database(current)
    _database(baseline)
    _archive(archive)
    admission, admission_sha = _admission(current, baseline, archive)
    receipt = audit_gc_recovery(
        current,
        baseline_database=baseline,
        archive_database=archive,
        admission_receipt_path=admission,
        expected_admission_receipt_sha256=admission_sha,
        expected_activation_receipt_sha256=_activation_receipt_sha(admission),
        expected_current_revision=REVISION,
        expected_baseline_revision=REVISION,
    )
    _write_sealed_model(output, receipt)
    original_file_sha256 = _sha256(output)
    unsealed = receipt.model_copy(
        update={
            "warnings": (*receipt.warnings, "untrusted-replay-claim"),
            "report_sha256": "0" * 64,
        }
    )
    changed = unsealed.model_copy(update={"report_sha256": unsealed.computed_report_sha256()})
    _write_sealed_model(output, changed)

    with pytest.raises(GcRecoveryError, match="independent file commitment"):
        publish_gc_recovery_audit(
            current,
            baseline_database=baseline,
            archive_database=archive,
            admission_receipt_path=admission,
            expected_admission_receipt_sha256=admission_sha,
            expected_activation_receipt_sha256=_activation_receipt_sha(admission),
            expected_current_revision=REVISION,
            expected_baseline_revision=REVISION,
            output=output,
            expected_recovery_receipt_sha256=original_file_sha256,
        )


def test_admission_ttl_and_canonical_listener_are_governed(tmp_path: Path) -> None:
    current, baseline, archive = _paths(tmp_path)
    _database(current)
    _database(baseline)
    _archive(archive)
    admission, _admission_sha = _admission(current, baseline, archive)
    receipt = GcRecoveryAdmissionReceipt.model_validate_json(admission.read_bytes())
    payload = receipt.model_dump()
    payload["valid_until"] = receipt.captured_at + timedelta(hours=1)
    with pytest.raises(ValueError, match="validity exceeds"):
        GcRecoveryAdmissionReceipt.model_validate(payload)

    payload = receipt.model_dump()
    payload["expected_listener_endpoints"] = ("not-the-dashboard:1",)
    payload["inactive_listener_endpoints"] = ("not-the-dashboard:1",)
    with pytest.raises(ValueError, match="canonical dashboard listener"):
        GcRecoveryAdmissionReceipt.model_validate(payload)


@pytest.mark.skipif(os.name != "nt", reason="Windows write-denial semantics")
def test_new_output_is_write_denied_through_final_publication_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current, baseline, archive = _paths(tmp_path)
    output = tmp_path / "gc-recovery.json"
    _database(current)
    _database(baseline)
    _archive(archive)
    admission, admission_sha = _admission(current, baseline, archive)
    attempted = False

    def census_with_output_mutation_attempt() -> LiveProcessCensus:
        nonlocal attempted
        if output.exists() and not attempted:
            attempted = True
            with pytest.raises(OSError):
                output.write_bytes(b"corrupted after publication")
        return _live_census()

    @contextmanager
    def real_output_only_fence(paths: tuple[Path, ...]) -> Generator[str, None, None]:
        if len(paths) == 1 and paths[0].suffix == ".tmp":
            with _REAL_WRITE_DENIAL_FENCE(paths) as mode:
                yield mode
        else:
            yield "windows-deny-write"

    monkeypatch.setattr(recovery_module, "_write_denial_fence", real_output_only_fence)
    monkeypatch.setattr(
        recovery_module,
        "_collect_live_process_census",
        census_with_output_mutation_attempt,
    )

    receipt, published = publish_gc_recovery_audit(
        current,
        baseline_database=baseline,
        archive_database=archive,
        admission_receipt_path=admission,
        expected_admission_receipt_sha256=admission_sha,
        expected_activation_receipt_sha256=_activation_receipt_sha(admission),
        expected_current_revision=REVISION,
        expected_baseline_revision=REVISION,
        output=output,
    )

    assert published is True
    assert attempted is True
    assert GcRecoveryReceipt.model_validate_json(output.read_bytes()) == receipt


def test_runtime_head_is_rechecked_after_output_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current, baseline, archive = _paths(tmp_path)
    output = tmp_path / "gc-recovery.json"
    _database(current)
    _database(baseline)
    _archive(archive)
    admission, admission_sha = _admission(current, baseline, archive)
    original = cast(
        Callable[[str | None], str],
        getattr(recovery_module, "canonical_runtime_git_commit"),
    )

    def fail_after_output_link(expected: str | None = None) -> str:
        if output.exists():
            raise GcRecoveryError("canonical runtime Git HEAD changed before publication")
        return original(expected)

    monkeypatch.setattr(
        recovery_module,
        "canonical_runtime_git_commit",
        fail_after_output_link,
    )

    with pytest.raises(GcRecoveryError, match="HEAD changed"):
        publish_gc_recovery_audit(
            current,
            baseline_database=baseline,
            archive_database=archive,
            admission_receipt_path=admission,
            expected_admission_receipt_sha256=admission_sha,
            expected_activation_receipt_sha256=_activation_receipt_sha(admission),
            expected_current_revision=REVISION,
            expected_baseline_revision=REVISION,
            output=output,
        )

    assert not output.exists()


def test_output_appearance_during_fence_acquisition_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current, baseline, archive = _paths(tmp_path)
    output = tmp_path / "gc-recovery.json"
    _database(current)
    _database(baseline)
    _archive(archive)
    admission, admission_sha = _admission(current, baseline, archive)

    @contextmanager
    def output_appears_before_fence_yields(
        paths: tuple[Path, ...],
    ) -> Generator[str, None, None]:
        assert output not in paths
        output.write_text("{}\n", encoding="utf-8")
        yield "windows-deny-write"

    monkeypatch.setattr(
        recovery_module,
        "_write_denial_fence",
        output_appears_before_fence_yields,
    )

    with pytest.raises(GcRecoveryError, match="existence changed"):
        publish_gc_recovery_audit(
            current,
            baseline_database=baseline,
            archive_database=archive,
            admission_receipt_path=admission,
            expected_admission_receipt_sha256=admission_sha,
            expected_activation_receipt_sha256=_activation_receipt_sha(admission),
            expected_current_revision=REVISION,
            expected_baseline_revision=REVISION,
            output=output,
            expected_recovery_receipt_sha256="a" * 64,
        )


def test_cli_publishes_canonical_receipt_and_exact_replays(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from execution.audit_gc_recovery import main

    current, baseline, archive = _paths(tmp_path)
    output = tmp_path / "gc-recovery.json"
    _database(current)
    _database(baseline)
    _archive(archive)
    admission, admission_sha = _admission(current, baseline, archive)
    arguments = [
        "--database",
        str(current),
        "--baseline-database",
        str(baseline),
        "--archive-database",
        str(archive),
        "--admission-receipt",
        str(admission),
        "--expected-admission-receipt-sha256",
        admission_sha,
        "--expected-activation-receipt-sha256",
        _activation_receipt_sha(admission),
        "--expected-current-revision",
        REVISION,
        "--expected-baseline-revision",
        REVISION,
        "--expected-runtime-git-commit",
        _runtime_commit_and_tasks()[0],
        "--output",
        str(output),
    ]

    assert main(arguments) == 0
    first = output.read_bytes()
    first_output = capsys.readouterr()
    assert '"outcome": "rolled_back_or_noop"' in first_output.out
    assert '"outcome": "published"' in first_output.err
    arguments.extend(["--expected-recovery-receipt-sha256", _sha256(output)])

    import provenance.gc_recovery as recovery

    def census_must_not_repeat(*_args: object) -> object:
        raise AssertionError("exact replay repeated the structural row census")

    monkeypatch.setattr(recovery, "_audit_tables", census_must_not_repeat)

    assert main(arguments) == 0
    second_output = capsys.readouterr()
    assert output.read_bytes() == first
    assert '"outcome": "exact_replay"' in second_output.err


def test_cli_refuses_output_alias_of_database(tmp_path: Path) -> None:
    from execution.audit_gc_recovery import main

    current, baseline, archive = _paths(tmp_path)
    _database(current)
    _database(baseline)
    _archive(archive)
    admission, admission_sha = _admission(current, baseline, archive)

    assert (
        main(
            [
                "--database",
                str(current),
                "--baseline-database",
                str(baseline),
                "--archive-database",
                str(archive),
                "--admission-receipt",
                str(admission),
                "--expected-admission-receipt-sha256",
                admission_sha,
                "--expected-activation-receipt-sha256",
                _activation_receipt_sha(admission),
                "--expected-current-revision",
                REVISION,
                "--expected-baseline-revision",
                REVISION,
                "--expected-runtime-git-commit",
                _runtime_commit_and_tasks()[0],
                "--output",
                str(current),
            ]
        )
        == 2
    )


def test_cli_publishes_blocked_receipt_and_exits_nonzero(tmp_path: Path) -> None:
    from execution.audit_gc_recovery import main

    current, baseline, archive = _paths(tmp_path)
    output = tmp_path / "blocked-gc-recovery.json"
    _database(current)
    _database(baseline)
    _archive(archive)
    with sqlite3.connect(current) as conn:
        conn.execute("DROP INDEX ix_0270_financial_facts_supersedes_id")
    admission, admission_sha = _admission(current, baseline, archive)

    result = main(
        [
            "--database",
            str(current),
            "--baseline-database",
            str(baseline),
            "--archive-database",
            str(archive),
            "--admission-receipt",
            str(admission),
            "--expected-admission-receipt-sha256",
            admission_sha,
            "--expected-activation-receipt-sha256",
            _activation_receipt_sha(admission),
            "--expected-current-revision",
            REVISION,
            "--expected-baseline-revision",
            REVISION,
            "--expected-runtime-git-commit",
            _runtime_commit_and_tasks()[0],
            "--output",
            str(output),
        ]
    )

    assert result == 2
    assert output.exists()
    assert '"recovery_ready":false' in output.read_text(encoding="utf-8")
