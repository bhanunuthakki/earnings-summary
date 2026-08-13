from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from pipeline import sec_delta_admission as admission
from pipeline import sec_delta_planner as planner
from pipeline.sec_delta_admission import (
    SecDeltaAdmissionError,
    SecDeltaAdmissionRequest,
    SecDeltaNativeInventoryAuthorization,
    admit_native_inventory_task,
)
from pipeline.sec_delta_planner import (
    EvaluationAuthorization,
    SecDeltaPlannerRequest,
    build_sec_delta_plan,
)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _create_db(path: Path, *, ticker: str = "RBRK", list_type: str = "portfolio") -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        f"""
        CREATE TABLE alembic_version (version_num TEXT PRIMARY KEY);
        INSERT INTO alembic_version VALUES ('{planner.SUPPORTED_ALEMBIC_REVISION}');
        CREATE TABLE tracked_companies (
            ticker TEXT NOT NULL,
            list_type TEXT NOT NULL,
            archived_at TEXT
        );
        INSERT INTO tracked_companies VALUES ('{ticker}', '{list_type}', NULL);
        CREATE TABLE source_inventory_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            inventory_key TEXT NOT NULL,
            revision INTEGER NOT NULL,
            issuer_id TEXT NOT NULL,
            ticker TEXT,
            source_kind TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE source_inventory_snapshot_seals (
            snapshot_id TEXT PRIMARY KEY,
            expected_component_count INTEGER NOT NULL,
            component_digest_sha256 TEXT NOT NULL,
            completion_status TEXT NOT NULL,
            sealed_at TEXT NOT NULL
        );
        CREATE TABLE expected_documents (
            expected_document_id TEXT PRIMARY KEY,
            snapshot_id TEXT NOT NULL,
            source_kind TEXT NOT NULL
        );
        CREATE TABLE source_coverage_assessments (
            assessment_id TEXT PRIMARY KEY,
            expected_document_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            coverage_status TEXT NOT NULL,
            document_version_id TEXT,
            recorded_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()


def _write_plan(
    root: Path,
    db: Path,
    *,
    evaluation_request: EvaluationAuthorization | None = None,
) -> tuple[Path, bytes, str]:
    plan = build_sec_delta_plan(
        SecDeltaPlannerRequest(
            database_path=db,
            as_of=date(2026, 8, 12),
            evaluation_requests=(() if evaluation_request is None else (evaluation_request,)),
        )
    )
    task = next(
        task
        for ticker_plan in plan.ticker_plans
        for task in ticker_plan.tasks
        if task.kind == "NATIVE_INVENTORY_PACKAGES"
    )
    assert task.status == "READY"
    raw = (plan.model_dump_json(exclude_none=False) + "\n").encode("utf-8")
    path = root / "sec-delta-plan.json"
    path.write_bytes(raw)
    return path, raw, task.task_id


def _request(
    plan_path: Path,
    raw_plan: bytes,
    db: Path,
    task_id: str,
) -> SecDeltaAdmissionRequest:
    return SecDeltaAdmissionRequest(
        plan_path=plan_path,
        expected_plan_sha256=_sha256_bytes(raw_plan),
        database_path=db,
        task_id=task_id,
    )


def test_admits_one_ready_portfolio_native_inventory_task(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    _create_db(db)
    plan_path, raw_plan, task_id = _write_plan(tmp_path, db)

    result = admit_native_inventory_task(_request(plan_path, raw_plan, db, task_id))

    assert result.schema_version == "sec_delta_native_inventory_authorization.v1"
    assert result.network_policy == "FORBIDDEN"
    assert result.task_id == task_id
    assert result.ticker == "RBRK"
    assert result.cik == "0001943896"
    assert result.coverage_role == "portfolio"
    assert result.authorization == "AUTOMATIC"
    assert result.authorization_attestation == "NOT_APPLICABLE"
    assert result.owner_request_id is None
    assert result.inventory_key == "sec-cik-0001943896:sec-submissions"
    assert result.next_inventory_revision == 1
    assert result.plan_sha256 == _sha256_bytes(raw_plan)
    assert result.source_policy_version == planner.POLICY_VERSION
    assert result.database_total_changes == 0
    assert len(result.database_storage_identity.entries) == 1
    assert result.computed_authorization_sha256() == result.authorization_sha256

    with pytest.raises(ValidationError, match="frozen"):
        setattr(result, "next_inventory_revision", 2)


def test_admits_owner_requested_evaluation_only_with_bound_attestation(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    _create_db(db, ticker="WIX", list_type="evaluation")
    request = EvaluationAuthorization(ticker="WIX", owner_request_id="owner-req-wix-7")
    plan_path, raw_plan, task_id = _write_plan(tmp_path, db, evaluation_request=request)

    result = admit_native_inventory_task(_request(plan_path, raw_plan, db, task_id))

    assert result.ticker == "WIX"
    assert result.coverage_role == "evaluation"
    assert result.authorization == "OWNER_REQUEST"
    assert result.authorization_attestation == "CALLER_ATTESTED"
    assert result.owner_request_id == "owner-req-wix-7"


def test_rejects_raw_plan_sha_mismatch_before_parsing(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    _create_db(db)
    plan_path, raw_plan, task_id = _write_plan(tmp_path, db)
    request = _request(plan_path, raw_plan, db, task_id).model_copy(
        update={"expected_plan_sha256": "0" * 64}
    )

    with pytest.raises(SecDeltaAdmissionError, match="raw plan SHA-256"):
        admit_native_inventory_task(request)


def test_rejects_plan_whose_self_seal_was_rewritten(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    _create_db(db)
    plan_path, raw_plan, task_id = _write_plan(tmp_path, db)
    payload = json.loads(raw_plan)
    payload["as_of"] = "2026-08-13"
    tampered = json.dumps(payload).encode("utf-8")
    plan_path.write_bytes(tampered)

    with pytest.raises(SecDeltaAdmissionError, match="sealed SEC delta plan"):
        admit_native_inventory_task(_request(plan_path, tampered, db, task_id))


def test_rejects_non_native_blocked_or_ambiguous_task_selection(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    _create_db(db)
    plan_path, raw_plan, task_id = _write_plan(tmp_path, db)
    plan = planner.SecDeltaPlanReceipt.model_validate_json(raw_plan)
    ticker_plan = plan.ticker_plans[0]
    companyfacts = next(task for task in ticker_plan.tasks if task.kind == "COMPANYFACTS_DELTA")
    sections = next(task for task in ticker_plan.tasks if task.kind == "NATIVE_FILING_SECTIONS")

    with pytest.raises(SecDeltaAdmissionError, match="READY NATIVE_INVENTORY_PACKAGES"):
        admit_native_inventory_task(_request(plan_path, raw_plan, db, companyfacts.task_id))
    with pytest.raises(SecDeltaAdmissionError, match="READY NATIVE_INVENTORY_PACKAGES"):
        admit_native_inventory_task(_request(plan_path, raw_plan, db, sections.task_id))

    duplicate_tasks = tuple(
        item.model_copy(update={"ticker": "WIX"}) if item.task_id == task_id else item
        for item in ticker_plan.tasks
    )
    duplicate_plan = ticker_plan.model_copy(update={"ticker": "WIX", "tasks": duplicate_tasks})
    draft = plan.model_copy(
        update={
            "ticker_plans": (*plan.ticker_plans, duplicate_plan),
            "blocked_task_count": plan.blocked_task_count + 1,
            "receipt_sha256": "0" * 64,
        }
    )
    payload = draft.model_dump(mode="json")
    payload["receipt_sha256"] = draft.computed_receipt_sha256()
    ambiguous_raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    plan_path.write_bytes(ambiguous_raw)

    with pytest.raises(SecDeltaAdmissionError, match="exactly one"):
        admit_native_inventory_task(_request(plan_path, ambiguous_raw, db, task_id))


def test_rejects_source_or_reviewed_issuer_policy_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "portfolio.db"
    _create_db(db)
    plan_path, raw_plan, task_id = _write_plan(tmp_path, db)
    request = _request(plan_path, raw_plan, db, task_id)

    monkeypatch.setattr(admission, "_current_source_policy_sha256", lambda: "b" * 64)
    with pytest.raises(SecDeltaAdmissionError, match="source policy"):
        admit_native_inventory_task(request)

    monkeypatch.undo()
    current_policy = admission.issuer_policy("RBRK")

    def drifted_issuer_policy(_ticker: str):
        return current_policy.model_copy(update={"policy_version": "reviewed-v2"})

    monkeypatch.setattr(
        admission,
        "issuer_policy",
        drifted_issuer_policy,
    )
    with pytest.raises(SecDeltaAdmissionError, match="issuer policy"):
        admit_native_inventory_task(request)


def test_rejects_different_database_even_when_roster_and_revision_match(tmp_path: Path) -> None:
    planned_db = tmp_path / "planned.db"
    substitute_db = tmp_path / "substitute.db"
    _create_db(planned_db)
    _create_db(substitute_db)
    plan_path, raw_plan, task_id = _write_plan(tmp_path, planned_db)

    with pytest.raises(SecDeltaAdmissionError, match="plan database path and storage identity"):
        admit_native_inventory_task(_request(plan_path, raw_plan, substitute_db, task_id))


def test_rejects_curated_ticker_to_cik_mapping_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "portfolio.db"
    _create_db(db)
    plan_path, raw_plan, task_id = _write_plan(tmp_path, db)
    monkeypatch.setitem(admission.CIK_MAP, "RBRK", "0000000001")

    with pytest.raises(SecDeltaAdmissionError, match="curated ticker-to-CIK mapping"):
        admit_native_inventory_task(_request(plan_path, raw_plan, db, task_id))


def test_rejects_database_storage_drift_since_planning(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    _create_db(db)
    plan_path, raw_plan, task_id = _write_plan(tmp_path, db)
    request = _request(plan_path, raw_plan, db, task_id)

    conn = sqlite3.connect(db)
    conn.execute("UPDATE tracked_companies SET list_type='evaluation'")
    conn.commit()
    conn.close()
    with pytest.raises(SecDeltaAdmissionError, match="plan database path and storage identity"):
        admit_native_inventory_task(request)


def test_rejects_plan_whose_next_revision_does_not_follow_current_max(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    _create_db(db)
    plan_path, raw_plan, task_id = _write_plan(tmp_path, db)
    plan = planner.SecDeltaPlanReceipt.model_validate_json(raw_plan)
    ticker_plan = plan.ticker_plans[0]
    incorrect_inventory = ticker_plan.inventory.model_copy(update={"next_inventory_revision": 2})
    incorrect_ticker_plan = ticker_plan.model_copy(update={"inventory": incorrect_inventory})
    draft = plan.model_copy(
        update={
            "ticker_plans": (incorrect_ticker_plan,),
            "receipt_sha256": "0" * 64,
        }
    )
    payload = draft.model_dump(mode="json")
    payload["receipt_sha256"] = draft.computed_receipt_sha256()
    incorrect_raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    plan_path.write_bytes(incorrect_raw)

    with pytest.raises(SecDeltaAdmissionError, match="inventory revision"):
        admit_native_inventory_task(_request(plan_path, incorrect_raw, db, task_id))


def test_rejects_active_wal_snapshot_before_opening_or_mutating_shm(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    _create_db(db)
    plan_path, raw_plan, task_id = _write_plan(tmp_path, db)
    connection = sqlite3.connect(db)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        connection.execute("UPDATE tracked_companies SET list_type='portfolio'")
        connection.commit()
        shm = Path(f"{db}-shm")
        assert shm.exists()
        before = (shm.read_bytes(), shm.stat().st_mtime_ns, shm.stat().st_ctime_ns)
        time.sleep(0.01)

        with pytest.raises(SecDeltaAdmissionError, match="closed sidecar-free"):
            admit_native_inventory_task(_request(plan_path, raw_plan, db, task_id))

        assert (shm.read_bytes(), shm.stat().st_mtime_ns, shm.stat().st_ctime_ns) == before
    finally:
        connection.close()


def test_rejects_hardlinked_database_snapshot_before_open(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    alias = tmp_path / "portfolio-alias.db"
    _create_db(db)
    plan_path, raw_plan, task_id = _write_plan(tmp_path, db)
    os.link(db, alias)
    before = admission.database_storage_identity(db)

    with pytest.raises(SecDeltaAdmissionError, match="unaliased immutable database"):
        admit_native_inventory_task(_request(plan_path, raw_plan, db, task_id))

    assert admission.database_storage_identity(db) == before
    assert db.read_bytes() == alias.read_bytes()


def test_authorization_self_seal_rejects_tampering(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    _create_db(db)
    plan_path, raw_plan, task_id = _write_plan(tmp_path, db)
    result = admit_native_inventory_task(_request(plan_path, raw_plan, db, task_id))
    payload = result.model_dump(mode="json")
    payload["ticker"] = "WIX"

    with pytest.raises(ValidationError, match="authorization_sha256"):
        SecDeltaNativeInventoryAuthorization.model_validate_json(json.dumps(payload))
