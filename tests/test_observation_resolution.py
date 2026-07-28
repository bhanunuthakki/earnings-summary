"""Contracts for canonical reported observations and resolution revisions."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from alembic.config import Config
from pydantic import ValidationError

from alembic import command
from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    EvidenceNode,
    ExtractionRun,
    SourceObservation,
)
from provenance.observation_resolution import (
    ObservationDimension,
    ObservationResolutionLedger,
    ReportedObservation,
    ResolutionRevision,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0213_decision_draft_provider_id"
HEAD = "0215_observation_resolution_ledger"
_STAMP = datetime(2026, 7, 26, 12, 0, 0)
_SHA_A = "a" * 64
_SHA_B = "b" * 64
_SHA_C = "c" * 64


def _config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _migrated_conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "observation-resolution.db"
    cfg = _config(db_path)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, HEAD)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _seed_evidence_node(conn: sqlite3.Connection) -> None:
    ledger = EvidenceLedger(conn)
    assert ledger.persist(
        ContentBlob(
            sha256=_SHA_A,
            byte_size=42,
            media_type="text/plain",
            storage_uri="file:///evidence/acme-q2.txt",
            recorded_at=_STAMP,
        )
    ).created
    assert ledger.persist(
        SourceObservation(
            observation_id="source-observation-acme-q2",
            idempotency_key="sec:acme:2026q2:retrieval-1",
            source_kind="sec_filing",
            source_url="https://www.sec.gov/Archives/acme-q2.htm",
            blob_sha256=_SHA_A,
            source_published_at=_STAMP,
            filing_at=_STAMP,
            accepted_at=_STAMP,
            observed_at=_STAMP,
            retrieved_at=_STAMP,
            retrieval_config_sha256=_SHA_B,
            collector_code_version="collector@2026.07.26",
        )
    ).created
    assert ledger.persist(
        DocumentVersion(
            document_version_id="document-acme-q2-v1",
            document_key="ACME:10-Q:2026-06-30",
            version_sequence=1,
            observation_id="source-observation-acme-q2",
            blob_sha256=_SHA_A,
            issuer_id="0000123456",
            ticker="ACME",
            document_type="10-Q",
            form_type="10-Q",
            accession_number="0000123456-26-000042",
            exhibit_id=None,
            period_start=datetime(2026, 4, 1),
            period_end=datetime(2026, 6, 30),
            as_of_at=datetime(2026, 6, 30),
            language="en",
            replaces_document_version_id=None,
            legacy_document_id=None,
            recorded_at=_STAMP,
        )
    ).created
    assert ledger.persist(
        ExtractionRun(
            extraction_run_id="extract-acme-q2-v1",
            idempotency_key="extract:acme-q2:tables:v1",
            document_version_id="document-acme-q2-v1",
            input_sha256=_SHA_A,
            extractor_name="filing-table-parser",
            extractor_config_sha256=_SHA_B,
            extractor_code_version="parser@2026.07.26",
            output_sha256=_SHA_C,
            started_at=_STAMP,
            completed_at=_STAMP,
            outcome="succeeded",
        )
    ).created
    assert ledger.persist(
        EvidenceNode(
            node_id="node-acme-revenue",
            evidence_key="ACME:revenue:2026Q2",
            revision=1,
            extraction_run_id="extract-acme-q2-v1",
            parent_node_id=None,
            supersedes_node_id=None,
            node_kind="table_cell",
            text="Revenue was $100 million.",
            locator=None,
            recorded_at=_STAMP,
        )
    ).created


def _observation(
    *, observation_id: str = "observation-acme-revenue-reported", value: str = "100.00"
) -> ReportedObservation:
    return ReportedObservation(
        observation_id=observation_id,
        idempotency_key=f"reported:{observation_id}",
        issuer_id="0000123456",
        ticker="ACME",
        concept_key="revenue",
        period_start=datetime(2026, 4, 1),
        period_end=datetime(2026, 6, 30),
        fiscal_period_type="quarter",
        dimensions=(ObservationDimension(key="segment", value="total"),),
        numeric_value=value,
        text_value=None,
        currency="USD",
        unit="currency",
        scale=6,
        observation_status="reported",
        evidence_node_id="node-acme-revenue",
        available_at=datetime(2026, 7, 20, 8, 2),
        recorded_at=_STAMP,
        method="filing_table_parser",
        method_version="2026.07.26",
        confidence=0.98,
        legacy_table=None,
        legacy_row_id=None,
    )


def _resolution(
    *,
    resolution_id: str = "resolution-acme-revenue-r1",
    revision: int = 1,
    selected_observation_id: str = "observation-acme-revenue-reported",
    candidate_observation_ids: tuple[str, ...] = ("observation-acme-revenue-reported",),
    supersedes_resolution_id: str | None = None,
    material_dissent: bool = False,
) -> ResolutionRevision:
    return ResolutionRevision(
        resolution_id=resolution_id,
        idempotency_key=f"resolution:{resolution_id}",
        logical_key="ACME:revenue:2026Q2:total",
        revision=revision,
        candidate_observation_ids=candidate_observation_ids,
        selected_observation_id=selected_observation_id,
        resolver_kind="deterministic_policy",
        policy_version="reported-first@1",
        reason="Reported filing fact is authoritative for the disclosed quarter.",
        knowledge_cutoff=datetime(2026, 7, 21),
        effective_at=datetime(2026, 7, 21),
        material_dissent=material_dissent,
        supersedes_resolution_id=supersedes_resolution_id,
        recorded_at=_STAMP,
    )


def test_models_canonicalize_typed_dimensions_and_require_one_value_kind() -> None:
    observation = _observation()
    assert observation.dimensions_json == '[{"key":"segment","value":"total"}]'
    assert observation.numeric_value == "100"
    with pytest.raises(ValidationError, match="exactly one"):
        ReportedObservation.model_validate(
            _observation().model_dump() | {"text_value": "also a claim"}
        )
    with pytest.raises(ValidationError, match="duplicate dimension"):
        ReportedObservation.model_validate(
            _observation().model_dump()
            | {"dimensions": [{"key": "segment", "value": "a"}, {"key": "segment", "value": "b"}]}
        )


def test_migration_round_trip_creates_only_additive_observation_resolution_objects(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "round-trip.db"
    cfg = _config(db_path)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, HEAD)
    command.downgrade(cfg, "0214_evidence_selection_lifecycle")
    conn = sqlite3.connect(str(db_path))
    try:
        names = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master")}
        assert "reported_observations" not in names
        assert "observation_resolution_revisions" not in names
        assert "v_observation_resolution_current" not in names
        assert "evidence_nodes" in names
    finally:
        conn.close()


def test_persisted_observations_are_idempotent_immutable_and_evidence_bound(tmp_path: Path) -> None:
    conn = _migrated_conn(tmp_path)
    try:
        _seed_evidence_node(conn)
        ledger = ObservationResolutionLedger(conn)
        observation = _observation()
        assert ledger.persist_observation(observation).created
        assert ledger.persist_observation(observation).created is False
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            conn.execute(
                "INSERT INTO reported_observations "
                "(observation_id, idempotency_key, issuer_id, concept_key, period_start, period_end, "
                "fiscal_period_type, dimensions_json, numeric_value, observation_status, evidence_node_id, "
                "available_at, recorded_at, method, method_version, confidence) "
                "VALUES ('orphan', 'orphan', '0000123456', 'revenue', ?, ?, 'quarter', '[]', '1', "
                "'reported', 'missing-node', ?, ?, 'parser', '1', 1)",
                (_STAMP, _STAMP, _STAMP, _STAMP),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE reported_observations SET confidence = 0.5 WHERE observation_id = ?",
                (observation.observation_id,),
            )
    finally:
        conn.close()


def test_resolution_requires_the_complete_candidate_set_and_selected_membership(
    tmp_path: Path,
) -> None:
    conn = _migrated_conn(tmp_path)
    try:
        _seed_evidence_node(conn)
        ledger = ObservationResolutionLedger(conn)
        first = _observation()
        second = _observation(observation_id="observation-acme-revenue-derived", value="99.5")
        third = _observation(observation_id="observation-acme-revenue-late", value="99.0")
        assert ledger.persist_observation(first).created
        assert ledger.persist_observation(second).created
        assert ledger.persist_observation(third).created
        with pytest.raises(ValueError, match="selected observation"):
            ledger.persist_resolution(
                _resolution(
                    candidate_observation_ids=(first.observation_id,),
                    selected_observation_id=second.observation_id,
                )
            )
        with pytest.raises(ValueError, match="duplicate"):
            _resolution(candidate_observation_ids=(first.observation_id, first.observation_id))
        assert ledger.persist_resolution(
            _resolution(candidate_observation_ids=(first.observation_id, second.observation_id))
        ).created
        candidates = conn.execute(
            "SELECT observation_id FROM observation_resolution_candidates WHERE resolution_id = ? ORDER BY observation_id",
            ("resolution-acme-revenue-r1",),
        ).fetchall()
        assert candidates == [(second.observation_id,), (first.observation_id,)]
        with pytest.raises(sqlite3.IntegrityError, match="finalized"):
            conn.execute(
                "INSERT INTO observation_resolution_candidates (resolution_id, observation_id) VALUES (?, ?)",
                ("resolution-acme-revenue-r1", third.observation_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="candidate"):
            conn.execute(
                "INSERT INTO observation_resolution_revisions "
                "(resolution_id, idempotency_key, logical_key, revision, selected_observation_id, resolver_kind, "
                "policy_version, reason, knowledge_cutoff, effective_at, material_dissent, recorded_at) "
                "VALUES ('bad-membership', 'bad-membership', 'ACME:bad', 1, ?, 'policy', '1', 'reason', ?, ?, 0, ?)",
                (first.observation_id, _STAMP, _STAMP, _STAMP),
            )
    finally:
        conn.close()


def test_resolution_revisions_preserve_conflict_dissent_and_project_current_view(
    tmp_path: Path,
) -> None:
    conn = _migrated_conn(tmp_path)
    try:
        _seed_evidence_node(conn)
        ledger = ObservationResolutionLedger(conn)
        first = _observation()
        second = _observation(observation_id="observation-acme-revenue-alternate", value="99.5")
        ledger.persist_observation(first)
        ledger.persist_observation(second)
        assert ledger.persist_resolution(_resolution()).created
        revision_two = _resolution(
            resolution_id="resolution-acme-revenue-r2",
            revision=2,
            selected_observation_id=second.observation_id,
            candidate_observation_ids=(first.observation_id, second.observation_id),
            supersedes_resolution_id="resolution-acme-revenue-r1",
            material_dissent=True,
        )
        assert ledger.persist_resolution(revision_two).created
        current = conn.execute(
            "SELECT resolution_id, revision, material_dissent FROM v_observation_resolution_current"
        ).fetchall()
        assert current == [(revision_two.resolution_id, 2, 1)]
        with pytest.raises(sqlite3.IntegrityError, match="previous revision"):
            ledger.persist_resolution(
                _resolution(
                    resolution_id="resolution-acme-revenue-r4",
                    revision=4,
                    supersedes_resolution_id=revision_two.resolution_id,
                )
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "DELETE FROM observation_resolution_candidates WHERE resolution_id = ?",
                (revision_two.resolution_id,),
            )
    finally:
        conn.close()
