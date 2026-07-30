# pyright: reportPrivateUsage=false
"""Full-universe population receipt meta-gate."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from alembic.config import Config
from pydantic import JsonValue

from alembic import command
from provenance.population_completeness import (
    _CUTOVER_WRITE_AUTHORITY,
    REQUIRED_CUTOVER_AUDIT_GATES,
    REQUIRED_POPULATION_PLANES,
    PopulationAuditReceipt,
    PopulationCompletenessLedger,
    PopulationParityReceipt,
    PopulationPlaneName,
    PopulationPlaneReceipt,
    PopulationRun,
    PopulationTemporalScope,
    canonical_json,
    digest_text,
    population_run_identity,
    stream_population_artifact_set,
)

ROOT = Path(__file__).resolve().parents[1]
STAMP = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
SHA = hashlib.sha256(b"population-test").hexdigest()
SCOPE = PopulationTemporalScope(knowledge_cutoff=STAMP, observed_through=STAMP)
RUN_ID = population_run_identity(SHA, SHA, SCOPE)


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _database(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "population-receipt.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE financial_facts (
            id INTEGER PRIMARY KEY,
            source_doc_id INTEGER NOT NULL
        );
        CREATE TABLE kpi_facts (
            id INTEGER PRIMARY KEY,
            source_doc_id INTEGER NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()
    config = _config(path)
    command.stamp(config, "0213_decision_draft_provider_id")
    command.upgrade(config, "0256_population_cutover_receipts")
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _run() -> PopulationRun:
    return PopulationRun(
        population_run_id=RUN_ID,
        idempotency_key=RUN_ID,
        policy_name="full_universe_population",
        policy_version="1",
        policy_config_sha256=SHA,
        source_snapshot_sha256=SHA,
        temporal_scope=SCOPE,
        verified_at=STAMP,
    )


def _plane(
    name: str,
    *,
    failed: int = 0,
    output_sha256: str = SHA,
) -> PopulationPlaneReceipt:
    result: dict[str, JsonValue] = {"failed_count": failed, "plane_name": name}
    if name == "retrieval_runtime":
        result["governance"] = {
            "evaluation_receipt_id": "evaluation-1",
            "evaluation_evaluated_at": STAMP.isoformat(),
            "promotion_id": "promotion-1",
            "promotion_recorded_at": STAMP.isoformat(),
            "projection_seal_ids": ["projection-1"],
            "projection_sealed_at": {"projection-1": STAMP.isoformat()},
            "runtime_registered_at": STAMP.isoformat(),
            "runtime_registration_id": "runtime-1",
        }
    return PopulationPlaneReceipt(
        population_run_id=RUN_ID,
        plane_name=cast(PopulationPlaneName, name),
        expected_count=1,
        materialized_count=1 - failed,
        excluded_count=0,
        failed_count=failed,
        input_commitment_sha256=SHA,
        output_commitment_sha256=output_sha256,
        status="blocked" if failed else "complete",
        details=cast(
            dict[str, JsonValue],
            {
                "artifact_sets": [
                    {
                        "row_count": 1,
                        "rows_sha256": SHA,
                        "selection_policy_id": f"{name}.v1",
                        "table": f"{name}_artifacts",
                    }
                ],
                "exclusion_counts": {},
                "result": result,
                "temporal_scope": SCOPE.model_dump(mode="json"),
                "verifier": {
                    "code_sha256": SHA,
                    "name": f"{name}-verifier",
                    "result_sha256": digest_text(canonical_json(result)),
                    "version": "1",
                },
            },
        ),
        temporal_scope=SCOPE,
        verified_at=STAMP,
    )


def _parity(*, absent: int = 0) -> PopulationParityReceipt:
    return PopulationParityReceipt(
        population_run_id=RUN_ID,
        eligible_legacy_count=1,
        canonical_count=1 - absent,
        matched_count=1 - absent,
        mismatched_count=0,
        absent_count=absent,
        extra_count=0,
        status="blocked" if absent else "complete",
        report={"absent_count": absent},
        temporal_scope=SCOPE,
        verified_at=STAMP,
    )


def _audit() -> PopulationAuditReceipt:
    coverage: list[dict[str, JsonValue]] = [
        {
            "eligible_count": 1,
            "failed_count": 0,
            "gate": gate,
            "verified_count": 1,
        }
        for gate in REQUIRED_CUTOVER_AUDIT_GATES
    ]
    gate_evidence: list[dict[str, JsonValue]] = []
    for gate in sorted(REQUIRED_CUTOVER_AUDIT_GATES):
        tables: list[dict[str, JsonValue]] = [
            {"row_count": 1, "rows_sha256": SHA, "table": f"{gate}_table"}
        ]
        gate_evidence.append(
            {
                "gate": gate,
                "gate_evidence_sha256": digest_text(
                    canonical_json({"gate": gate, "tables": tables})
                ),
                "tables": cast(JsonValue, tables),
            }
        )
    watermark_material: dict[str, JsonValue] = {
        "knowledge_cutoff": STAMP.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "observed_through": STAMP.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "gates": cast(
            JsonValue,
            [
                {
                    "gate": item["gate"],
                    "gate_evidence_sha256": item["gate_evidence_sha256"],
                }
                for item in gate_evidence
            ],
        ),
    }
    return PopulationAuditReceipt(
        population_run_id=RUN_ID,
        verifier_name="population-cutover-readiness-auditor",
        verifier_version="2",
        verifier_code_sha256=SHA,
        verifier_config_sha256=SHA,
        temporal_scope=SCOPE,
        verified_at=STAMP,
        required_gate_count=13,
        eligible_count=13,
        verified_count=13,
        failed_count=0,
        evidence={
            "coverage": cast(JsonValue, coverage),
            "findings": cast(JsonValue, []),
            "gate_evidence": cast(JsonValue, gate_evidence),
            "has_blockers": False,
            "schema_version": "data-cutover-readiness-audit/v1",
            "tables_present": cast(JsonValue, []),
            "watermark_material": watermark_material,
            "watermark_sha256": digest_text(canonical_json(watermark_material)),
        },
    )


def test_seal_requires_all_seven_nonempty_planes_and_exact_parity(
    tmp_path: Path,
) -> None:
    conn = _database(tmp_path)
    try:
        ledger = PopulationCompletenessLedger(conn)
        assert not hasattr(ledger, "record_verified_cutover")
        receipt = ledger._record_verified_cutover(
            run=_run(),
            planes=tuple(_plane(name) for name in REQUIRED_POPULATION_PLANES),
            parity=_parity(),
            audit=_audit(),
            sealed_at=STAMP,
            authority=_CUTOVER_WRITE_AUTHORITY,
        )
        conn.commit()

        assert receipt.required_plane_count == 7
        assert receipt.complete_plane_count == 7
        assert receipt.audit_receipt_sha256 == _audit().receipt_sha256
        assert ledger.verify(RUN_ID) == receipt
        assert ledger._record_run(_run()) is False
        assert ledger._record_plane(_plane(REQUIRED_POPULATION_PLANES[0])) is False
    finally:
        conn.close()


def test_0256_downgrade_refuses_to_destroy_sealed_population_history(
    tmp_path: Path,
) -> None:
    conn = _database(tmp_path)
    path = Path(str(conn.execute("PRAGMA database_list").fetchone()[2]))
    try:
        PopulationCompletenessLedger(conn)._record_verified_cutover(
            run=_run(),
            planes=tuple(_plane(name) for name in REQUIRED_POPULATION_PLANES),
            parity=_parity(),
            audit=_audit(),
            sealed_at=STAMP,
            authority=_CUTOVER_WRITE_AUTHORITY,
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(
        RuntimeError,
        match="0256 downgrade would destroy sealed population evidence",
    ):
        command.downgrade(
            _config(path),
            "0255_scoped_canonical_resolution_snapshots",
        )
    reopened = sqlite3.connect(path)
    try:
        assert reopened.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0256_population_cutover_receipts",
        )
        assert reopened.execute("SELECT COUNT(*) FROM population_cutover_receipts").fetchone() == (
            1,
        )
    finally:
        reopened.close()


def test_seal_fails_closed_for_missing_or_blocked_work(tmp_path: Path) -> None:
    conn = _database(tmp_path)
    try:
        ledger = PopulationCompletenessLedger(conn)
        with pytest.raises(RuntimeError, match="direct population sealing is disabled"):
            ledger.seal(RUN_ID, sealed_at=STAMP)
        with pytest.raises(ValueError, match="exactly the seven"), conn:
            ledger._record_verified_cutover(
                run=_run(),
                planes=tuple(_plane(name) for name in REQUIRED_POPULATION_PLANES[:-1]),
                parity=_parity(),
                audit=_audit(),
                sealed_at=STAMP,
                authority=_CUTOVER_WRITE_AUTHORITY,
            )
    finally:
        conn.close()


def test_empty_green_receipt_and_append_only_mutation_are_rejected(
    tmp_path: Path,
) -> None:
    conn = _database(tmp_path)
    try:
        ledger = PopulationCompletenessLedger(conn)
        ledger._record_run(_run())
        with pytest.raises(ValueError, match="greater than 0"):
            PopulationPlaneReceipt(
                population_run_id=RUN_ID,
                plane_name="identity_scope",
                expected_count=0,
                materialized_count=0,
                excluded_count=0,
                failed_count=0,
                input_commitment_sha256=SHA,
                output_commitment_sha256=SHA,
                status="complete",
                details={
                    "artifact_sets": [
                        {
                            "row_count": 0,
                            "rows_sha256": SHA,
                            "selection_policy_id": "identity.v1",
                            "table": "identity_artifacts",
                        }
                    ],
                    "exclusion_counts": {},
                    "result": {},
                    "temporal_scope": SCOPE.model_dump(mode="json"),
                    "verifier": {
                        "code_sha256": SHA,
                        "name": "identity-verifier",
                        "result_sha256": digest_text(canonical_json({})),
                        "version": "1",
                    },
                },
                temporal_scope=SCOPE,
                verified_at=STAMP,
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE population_run_headers SET policy_version='2' WHERE population_run_id=?",
                (RUN_ID,),
            )
    finally:
        conn.close()


def test_zero_materialized_or_unapproved_exclusions_cannot_be_green() -> None:
    with pytest.raises(ValueError, match="status"):
        PopulationPlaneReceipt(
            population_run_id=RUN_ID,
            plane_name="identity_scope",
            expected_count=1,
            materialized_count=0,
            excluded_count=1,
            failed_count=0,
            input_commitment_sha256=SHA,
            output_commitment_sha256=SHA,
            status="complete",
            details={
                "artifact_sets": [
                    {
                        "row_count": 0,
                        "rows_sha256": SHA,
                        "selection_policy_id": "identity.v1",
                        "table": "identity_artifacts",
                    }
                ],
                "exclusion_counts": {},
                "result": {},
                "temporal_scope": SCOPE.model_dump(mode="json"),
                "verifier": {
                    "code_sha256": SHA,
                    "name": "identity-verifier",
                    "result_sha256": digest_text(canonical_json({})),
                    "version": "1",
                },
            },
            temporal_scope=SCOPE,
            verified_at=STAMP,
        )


def test_plane_result_commitment_and_internal_write_authority_are_enforced(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="result commitment"):
        PopulationPlaneReceipt.model_validate(
            {
                **_plane("identity_scope").model_dump(),
                "details": {
                    "artifact_sets": _plane("identity_scope").details["artifact_sets"],
                    "exclusion_counts": {},
                    "result": {"changed": True},
                    "temporal_scope": SCOPE.model_dump(mode="json"),
                    "verifier": {
                        "code_sha256": SHA,
                        "name": "identity-verifier",
                        "result_sha256": SHA,
                        "version": "1",
                    },
                },
            }
        )

    conn = _database(tmp_path)
    try:
        ledger = PopulationCompletenessLedger(conn)
        with pytest.raises(RuntimeError, match="internal verifier authority"):
            ledger._record_verified_cutover(
                run=_run(),
                planes=tuple(_plane(name) for name in REQUIRED_POPULATION_PLANES),
                parity=_parity(),
                audit=_audit(),
                sealed_at=STAMP,
                authority=object(),
            )
        assert conn.execute("SELECT COUNT(*) FROM population_run_headers").fetchone() == (0,)
    finally:
        conn.close()


def test_existing_cutover_replay_recomputes_every_semantic_commitment(
    tmp_path: Path,
) -> None:
    conn = _database(tmp_path)
    try:
        ledger = PopulationCompletenessLedger(conn)
        planes = tuple(_plane(name) for name in REQUIRED_POPULATION_PLANES)
        ledger._record_verified_cutover(
            run=_run(),
            planes=planes,
            parity=_parity(),
            audit=_audit(),
            sealed_at=STAMP,
            authority=_CUTOVER_WRITE_AUTHORITY,
        )
        changed = tuple(
            _plane(name, output_sha256=("b" * 64 if name == "research_snapshot" else SHA))
            for name in REQUIRED_POPULATION_PLANES
        )

        with pytest.raises(ValueError, match="research_snapshot plane commitment"):
            ledger._verify_fresh_cutover(
                run=_run(),
                planes=changed,
                parity=_parity(),
                audit=_audit(),
                authority=_CUTOVER_WRITE_AUTHORITY,
            )
    finally:
        conn.close()


def test_audit_requires_exact_13_gate_evidence_and_full_watermark() -> None:
    valid = _audit()
    missing_gate_evidence = dict(valid.evidence)
    coverage = list(cast(list[dict[str, object]], missing_gate_evidence["coverage"]))
    missing_gate_evidence["coverage"] = cast(JsonValue, coverage[:-1])
    with pytest.raises(ValueError, match="exactly 13 gates"):
        PopulationAuditReceipt.model_validate(
            {
                **valid.model_dump(exclude={"evidence"}),
                "evidence": missing_gate_evidence,
            }
        )

    tampered = dict(valid.evidence)
    gate_evidence = list(cast(list[dict[str, object]], tampered["gate_evidence"]))
    first = dict(gate_evidence[0])
    tables = list(cast(list[dict[str, object]], first["tables"]))
    tables[0] = {**tables[0], "rows_sha256": "b" * 64}
    first["tables"] = tables
    gate_evidence[0] = first
    tampered["gate_evidence"] = cast(JsonValue, gate_evidence)
    with pytest.raises(ValueError, match="gate evidence commitment"):
        PopulationAuditReceipt.model_validate(
            {
                **valid.model_dump(exclude={"evidence"}),
                "evidence": tampered,
            }
        )


def test_database_triggers_reject_missing_verifier_result_and_audit_gates(
    tmp_path: Path,
) -> None:
    conn = _database(tmp_path)
    try:
        ledger = PopulationCompletenessLedger(conn)
        ledger._record_run(_run())
        invalid_details = canonical_json(
            {
                "exclusion_counts": {},
                "verifier": {
                    "code_sha256": SHA,
                    "name": "identity-verifier",
                    "result_sha256": SHA,
                    "version": "1",
                },
            }
        )
        with pytest.raises(sqlite3.IntegrityError, match="plane receipt mismatch"):
            conn.execute(
                "INSERT INTO population_plane_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    RUN_ID,
                    "identity_scope",
                    1,
                    1,
                    0,
                    0,
                    SHA,
                    SHA,
                    "complete",
                    invalid_details,
                    digest_text(invalid_details),
                    STAMP,
                    STAMP,
                    STAMP,
                ),
            )

        audit = _audit()
        invalid_evidence = dict(audit.evidence)
        invalid_evidence["coverage"] = cast(
            JsonValue,
            cast(list[JsonValue], invalid_evidence["coverage"])[:-1],
        )
        evidence_json = canonical_json(invalid_evidence)
        with pytest.raises(sqlite3.IntegrityError, match="audit receipt mismatch"):
            conn.execute(
                "INSERT INTO population_cutover_audit_receipts VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    RUN_ID,
                    audit.verifier_name,
                    audit.verifier_version,
                    audit.verifier_code_sha256,
                    audit.verifier_config_sha256,
                    audit.cutoff_at,
                    audit.temporal_scope.observed_through,
                    audit.audited_at,
                    audit.required_gate_count,
                    audit.eligible_count,
                    audit.verified_count,
                    audit.failed_count,
                    evidence_json,
                    digest_text(evidence_json),
                    audit.canonical_receipt_json,
                    audit.receipt_sha256,
                ),
            )

        negative_evidence = dict(audit.evidence)
        negative_coverage = [
            dict(row) for row in cast(list[dict[str, JsonValue]], negative_evidence["coverage"])
        ]
        negative_coverage[0]["eligible_count"] = -1
        negative_coverage[0]["verified_count"] = -1
        negative_coverage[1]["eligible_count"] = 3
        negative_coverage[1]["verified_count"] = 3
        negative_evidence["coverage"] = cast(JsonValue, negative_coverage)
        negative_evidence_json = canonical_json(negative_evidence)
        negative_evidence_sha = digest_text(negative_evidence_json)
        negative_receipt = cast(
            dict[str, JsonValue],
            json.loads(audit.canonical_receipt_json),
        )
        negative_receipt["evidence_sha256"] = negative_evidence_sha
        negative_receipt_json = canonical_json(negative_receipt)
        with pytest.raises(sqlite3.IntegrityError, match="audit receipt mismatch"):
            conn.execute(
                "INSERT INTO population_cutover_audit_receipts VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    RUN_ID,
                    audit.verifier_name,
                    audit.verifier_version,
                    audit.verifier_code_sha256,
                    audit.verifier_config_sha256,
                    audit.cutoff_at,
                    audit.temporal_scope.observed_through,
                    audit.audited_at,
                    audit.required_gate_count,
                    audit.eligible_count,
                    audit.verified_count,
                    audit.failed_count,
                    negative_evidence_json,
                    negative_evidence_sha,
                    negative_receipt_json,
                    digest_text(negative_receipt_json),
                ),
            )
    finally:
        conn.close()


def test_streamed_audit_watermark_binds_full_row_state_not_only_counts() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE evidence_rows ("
        "evidence_id TEXT PRIMARY KEY,payload_sha256 TEXT NOT NULL,"
        "seal_sha256 TEXT NOT NULL,knowledge_at TEXT NOT NULL,recorded_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO evidence_rows VALUES (?,?,?,?,?)",
        ("evidence-1", SHA, SHA, STAMP.isoformat(), STAMP.isoformat()),
    )
    query = (
        "SELECT evidence_id artifact_id,fact_sha256(payload_sha256) payload_sha256,"
        "seal_sha256,"
        "knowledge_at,recorded_at FROM evidence_rows "
        "WHERE datetime(knowledge_at)<=datetime(?) AND datetime(recorded_at)<=datetime(?) "
        "ORDER BY evidence_id"
    )
    first = stream_population_artifact_set(
        conn,
        table="evidence_rows",
        query=query,
        params=(STAMP.isoformat(), STAMP.isoformat()),
        selection_policy_id="evidence-rows.K-knowledge.O-recorded.v1",
        fetch_size=1,
    )
    conn.execute(
        "UPDATE evidence_rows SET payload_sha256=? WHERE evidence_id=?",
        ("b" * 64, "evidence-1"),
    )
    second = stream_population_artifact_set(
        conn,
        table="evidence_rows",
        query=query,
        params=(STAMP.isoformat(), STAMP.isoformat()),
        selection_policy_id="evidence-rows.K-knowledge.O-recorded.v1",
        fetch_size=1,
    )

    assert first.row_count == second.row_count == 1
    assert first.rows_sha256 != second.rows_sha256


def test_clockless_child_watermark_uses_exact_governed_parent_cutoff() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE canonical_fact_resolution_snapshot_seals (
            resolution_snapshot_id TEXT PRIMARY KEY,
            cutoff_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE canonical_fact_resolution_snapshot_members (
            resolution_snapshot_id TEXT NOT NULL,
            member_ordinal INTEGER NOT NULL,
            member_sha256 TEXT NOT NULL,
            seal_sha256 TEXT NOT NULL
        );
        """
    )
    future = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
    conn.executemany(
        "INSERT INTO canonical_fact_resolution_snapshot_seals VALUES (?,?,?)",
        (
            ("snapshot-cutoff", STAMP.isoformat(), STAMP.isoformat()),
            ("snapshot-future", future.isoformat(), future.isoformat()),
        ),
    )
    conn.executemany(
        "INSERT INTO canonical_fact_resolution_snapshot_members VALUES (?,?,?,?)",
        (
            ("snapshot-cutoff", 0, "a" * 64, SHA),
            ("snapshot-future", 0, "b" * 64, SHA),
        ),
    )
    query = (
        "SELECT member.resolution_snapshot_id||':'||member.member_ordinal artifact_id,"
        "member.member_sha256 payload_sha256,member.seal_sha256 seal_sha256,"
        "parent.cutoff_at knowledge_at,parent.recorded_at recorded_at "
        "FROM canonical_fact_resolution_snapshot_members member "
        "JOIN canonical_fact_resolution_snapshot_seals parent USING (resolution_snapshot_id) "
        "WHERE datetime(parent.cutoff_at)<=datetime(?) "
        "AND datetime(parent.recorded_at)<=datetime(?) "
        "ORDER BY member.resolution_snapshot_id,member.member_ordinal"
    )
    baseline = stream_population_artifact_set(
        conn,
        table="canonical_fact_resolution_snapshot_members",
        query=query,
        params=(STAMP.isoformat(), STAMP.isoformat()),
        selection_policy_id="resolution-members.K-parent.O-parent.v1",
        fetch_size=1,
    )
    conn.execute(
        "UPDATE canonical_fact_resolution_snapshot_members "
        "SET member_sha256=? WHERE resolution_snapshot_id=?",
        ("c" * 64, "snapshot-future"),
    )
    future_changed = stream_population_artifact_set(
        conn,
        table="canonical_fact_resolution_snapshot_members",
        query=query,
        params=(STAMP.isoformat(), STAMP.isoformat()),
        selection_policy_id="resolution-members.K-parent.O-parent.v1",
        fetch_size=1,
    )
    conn.execute(
        "UPDATE canonical_fact_resolution_snapshot_members "
        "SET member_sha256=? WHERE resolution_snapshot_id=?",
        ("d" * 64, "snapshot-cutoff"),
    )
    cutoff_changed = stream_population_artifact_set(
        conn,
        table="canonical_fact_resolution_snapshot_members",
        query=query,
        params=(STAMP.isoformat(), STAMP.isoformat()),
        selection_policy_id="resolution-members.K-parent.O-parent.v1",
        fetch_size=1,
    )

    assert baseline.row_count == future_changed.row_count == 1
    assert baseline.rows_sha256 == future_changed.rows_sha256
    assert cutoff_changed.rows_sha256 != baseline.rows_sha256


def test_audit_metadata_contract_rejects_inconsistent_schema_findings_and_tables() -> None:
    valid = _audit()
    invalid_schema = json.loads(canonical_json(valid.evidence))
    invalid_schema["schema_version"] = "invented/v99"
    with pytest.raises(ValueError, match="schema version"):
        PopulationAuditReceipt.model_validate(
            {**valid.model_dump(exclude={"evidence"}), "evidence": invalid_schema}
        )

    invalid_tables = json.loads(canonical_json(valid.evidence))
    invalid_tables["tables_present"] = ["z_table", "a_table", "a_table"]
    with pytest.raises(ValueError, match="sorted and unique"):
        PopulationAuditReceipt.model_validate(
            {**valid.model_dump(exclude={"evidence"}), "evidence": invalid_tables}
        )

    invalid_findings = json.loads(canonical_json(valid.evidence))
    invalid_findings["findings"] = [
        {
            "code": "BLOCKER",
            "count": 1,
            "query_context": "SELECT 1",
            "remediation": "hard-stop",
            "samples": [],
            "severity": "blocker",
        }
    ]
    with pytest.raises(ValueError, match="blocking findings"):
        PopulationAuditReceipt.model_validate(
            {**valid.model_dump(exclude={"evidence"}), "evidence": invalid_findings}
        )


def test_audit_trigger_rejects_self_hashed_inconsistent_receipt_metadata(
    tmp_path: Path,
) -> None:
    conn = _database(tmp_path)
    try:
        ledger = PopulationCompletenessLedger(conn)
        ledger._record_run(_run())
        audit = _audit()
        receipt_payload = json.loads(audit.canonical_receipt_json)
        receipt_payload["verifier_name"] = "fabricated-verifier"
        receipt_json = canonical_json(receipt_payload)
        with pytest.raises(sqlite3.IntegrityError, match="audit receipt mismatch"):
            conn.execute(
                "INSERT INTO population_cutover_audit_receipts VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    RUN_ID,
                    audit.verifier_name,
                    audit.verifier_version,
                    audit.verifier_code_sha256,
                    audit.verifier_config_sha256,
                    audit.cutoff_at,
                    audit.temporal_scope.observed_through,
                    audit.audited_at,
                    audit.required_gate_count,
                    audit.eligible_count,
                    audit.verified_count,
                    audit.failed_count,
                    audit.canonical_evidence_json,
                    audit.evidence_sha256,
                    receipt_json,
                    digest_text(receipt_json),
                ),
            )
    finally:
        conn.close()
