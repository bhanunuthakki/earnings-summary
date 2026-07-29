"""Contracts for complete, multi-observation source-inventory provenance."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from provenance.evidence_ledger import ContentBlob, EvidenceLedger, SourceObservation
from provenance.source_coverage import SourceCoverageLedger, SourceInventorySnapshot
from provenance.source_coverage_reconcile import (
    ExpectedDocumentImport,
    ExplicitAbsence,
    InventoryComponentImport,
    SourceCoverageImport,
    reconcile_source_coverage,
)
from provenance.source_inventory_seal import (
    InventoryComponent,
    InventoryManifestLink,
    InventorySeal,
    SourceInventorySealStore,
    component_digest,
)
from search.corpus_builder import (
    CorpusBuildRequest,
    build_grounded_search_corpus,
    load_coverage_expected_document_inventory,
)

ROOT = Path(__file__).resolve().parents[1]
PRIOR = "0219_source_coverage_ledger"
HEAD = "0220_source_inventory_seals"
STAMP = datetime(2026, 7, 27, 5, 0, 0)
A, B, C = "a" * 64, "b" * 64, "c" * 64


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _conn(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "inventory.db"
    config = _config(path)
    command.stamp(config, "0213_decision_draft_provider_id")
    command.upgrade(config, HEAD)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    ledger = EvidenceLedger(conn)
    for digest, observation_id, url in (
        (A, "obs-primary", "https://data.sec.gov/submissions/CIK1.json"),
        (B, "obs-history", "https://data.sec.gov/submissions/CIK1-submissions-001.json"),
    ):
        ledger.persist(
            ContentBlob(
                sha256=digest,
                byte_size=10,
                media_type="application/json",
                storage_uri=url,
                recorded_at=STAMP,
            )
        )
        ledger.persist(
            SourceObservation(
                observation_id=observation_id,
                idempotency_key=observation_id,
                source_kind="sec_submissions",
                source_url=url,
                blob_sha256=digest,
                source_published_at=None,
                filing_at=None,
                accepted_at=None,
                observed_at=STAMP,
                retrieved_at=STAMP,
                retrieval_config_sha256=C,
                collector_code_version="sec-inventory@1",
            )
        )
    SourceCoverageLedger(conn).persist(
        SourceInventorySnapshot(
            snapshot_id="snapshot",
            idempotency_key="snapshot",
            inventory_key="issuer:sec-submissions",
            revision=1,
            issuer_id="issuer",
            ticker="ACME",
            source_kind="sec_submissions",
            source_url="https://data.sec.gov/submissions/CIK1.json",
            source_observation_id="obs-primary",
            outcome="succeeded",
            authoritative=True,
            retrieval_config_sha256=C,
            collector_code_version="sec-inventory@1",
            started_at=STAMP,
            completed_at=STAMP,
            recorded_at=STAMP,
        )
    )
    return conn


def _components() -> tuple[InventoryComponent, ...]:
    return (
        InventoryComponent(
            component_id="component-primary",
            idempotency_key="component-primary",
            snapshot_id="snapshot",
            component_key="primary",
            component_kind="primary",
            source_url="https://data.sec.gov/submissions/CIK1.json",
            source_observation_id="obs-primary",
            outcome="succeeded",
            required=True,
            ordinal=0,
            recorded_at=STAMP,
        ),
        InventoryComponent(
            component_id="component-history",
            idempotency_key="component-history",
            snapshot_id="snapshot",
            component_key="history-001",
            component_kind="historical_page",
            source_url="https://data.sec.gov/submissions/CIK1-submissions-001.json",
            source_observation_id="obs-history",
            outcome="succeeded",
            required=True,
            ordinal=1,
            recorded_at=STAMP,
        ),
    )


def test_components_seal_exact_complete_inventory_and_become_immutable(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    try:
        store = SourceInventorySealStore(conn)
        components = _components()
        for component in components:
            assert store.persist(component).created
        seal = InventorySeal(
            snapshot_id="snapshot",
            expected_component_count=2,
            component_digest_sha256=component_digest(components),
            completion_status="complete",
            sealed_at=STAMP,
        )
        assert store.persist(seal).created
        assert not store.persist(seal).created
        assert conn.execute(
            "SELECT snapshot_id FROM v_source_inventory_sealed_complete"
        ).fetchone() == ("snapshot",)
        with pytest.raises(sqlite3.IntegrityError, match="sealed"):
            conn.execute(
                "INSERT INTO source_inventory_components "
                "(component_id,idempotency_key,snapshot_id,component_key,component_kind,"
                "source_url,source_observation_id,outcome,required,ordinal,recorded_at) "
                "VALUES ('late','late','snapshot','late','other','https://late','obs-primary',"
                "'succeeded',1,2,?)",
                (STAMP,),
            )
    finally:
        conn.close()


def test_incomplete_required_component_cannot_claim_complete_seal(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    try:
        store = SourceInventorySealStore(conn)
        failed = InventoryComponent(
            component_id="component-failed",
            idempotency_key="component-failed",
            snapshot_id="snapshot",
            component_key="history-missing",
            component_kind="historical_page",
            source_url="https://data.sec.gov/submissions/missing.json",
            outcome="failed",
            required=True,
            failure_reason="http_503",
            ordinal=1,
            recorded_at=STAMP,
        )
        store.persist(failed)
        with pytest.raises(ValueError, match="completion status"):
            store.persist(
                InventorySeal(
                    snapshot_id="snapshot",
                    expected_component_count=1,
                    component_digest_sha256=component_digest((failed,)),
                    completion_status="complete",
                    sealed_at=STAMP,
                )
            )
    finally:
        conn.close()


def test_manifest_inventory_link_requires_both_immutable_parents(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    try:
        store = SourceInventorySealStore(conn)
        components = _components()
        for component in components:
            store.persist(component)
        store.persist(
            InventorySeal(
                snapshot_id="snapshot",
                expected_component_count=2,
                component_digest_sha256=component_digest(components),
                completion_status="complete",
                sealed_at=STAMP,
            )
        )
        conn.execute(
            "INSERT INTO search_corpus_manifests "
            "(manifest_id,idempotency_key,corpus_key,revision,selection_config_sha256,"
            "selector_code_version,recorded_at) VALUES "
            "('manifest','manifest','issuer:reporting',1,?,'builder@1',?)",
            (C, STAMP),
        )
        link = InventoryManifestLink(
            manifest_id="manifest", snapshot_id="snapshot", linked_at=STAMP
        )
        assert store.persist(link).created
        assert not store.persist(link).created
    finally:
        conn.close()


def test_migration_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "round-trip.db"
    config = _config(path)
    command.stamp(config, PRIOR)
    command.upgrade(config, HEAD)
    command.downgrade(config, PRIOR)
    conn = sqlite3.connect(path)
    try:
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='source_inventory_components'"
            ).fetchone()
            is None
        )
    finally:
        conn.close()


def test_coverage_reconciliation_atomically_seals_component_inventory(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    try:
        conn.commit()
        request = SourceCoverageImport(
            inventory_key="issuer:sec-reconciled",
            revision=1,
            issuer_id="issuer",
            ticker="ACME",
            source_kind="sec_submissions",
            source_url="https://data.sec.gov/submissions/CIK1.json",
            source_observation_id="obs-primary",
            outcome="succeeded",
            authoritative=True,
            retrieval_config_sha256=C,
            collector_code_version="sec-inventory@1",
            started_at=STAMP,
            completed_at=STAMP,
            recorded_at=STAMP,
            reconciled_at=STAMP,
            components=(
                InventoryComponentImport(
                    component_key="primary",
                    component_kind="primary",
                    source_url="https://data.sec.gov/submissions/CIK1.json",
                    source_observation_id="obs-primary",
                    outcome="succeeded",
                    ordinal=0,
                ),
                InventoryComponentImport(
                    component_key="history-001",
                    component_kind="historical_page",
                    source_url="https://data.sec.gov/submissions/CIK1-submissions-001.json",
                    source_observation_id="obs-history",
                    outcome="succeeded",
                    ordinal=1,
                ),
            ),
            expected_documents=(
                ExpectedDocumentImport(
                    expected_document_key="issuer:accession",
                    source_kind="sec_filing",
                    document_type="filing",
                    form_type="10-K",
                    accession_number="0000000001-26-000001",
                    expectation_basis="authoritative",
                    absence=ExplicitAbsence(
                        coverage_status="available",
                        reason_code="authority_inventory_only",
                        reason_details=(("source", "sec_submissions"),),
                    ),
                ),
            ),
            apply=True,
        )
        result = reconcile_source_coverage(conn, request)
        assert result.records_created == 6
        assert conn.execute(
            "SELECT completion_status FROM source_inventory_snapshot_seals WHERE snapshot_id = ?",
            (result.snapshot_id,),
        ).fetchone() == ("complete",)
        assert conn.execute(
            "SELECT COUNT(*) FROM source_inventory_components WHERE snapshot_id = ?",
            (result.snapshot_id,),
        ).fetchone() == (2,)
        inventory, snapshot_ids = load_coverage_expected_document_inventory(
            conn, ("issuer:sec-reconciled",)
        )
        corpus = build_grounded_search_corpus(
            conn,
            CorpusBuildRequest(
                corpus_key="issuer:reporting",
                revision=1,
                selector_code_version="coverage-selector@1",
                recorded_at=STAMP,
                expected_documents=inventory.expected_documents,
                source_inventory_snapshot_ids=snapshot_ids,
                apply=True,
            ),
        )
        assert corpus.completion_status == "incomplete"
        assert conn.execute(
            "SELECT snapshot_id FROM search_manifest_source_inventories WHERE manifest_id = ?",
            (corpus.manifest_id,),
        ).fetchone() == (result.snapshot_id,)
    finally:
        conn.close()
