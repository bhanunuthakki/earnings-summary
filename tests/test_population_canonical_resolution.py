# pyright: reportPrivateUsage=false
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from alembic.config import Config

import provenance.population_canonical_resolution as population
from alembic import command
from provenance.canonical_fact_resolution import (
    ResolutionSnapshotScope,
    VerifiedResolutionSnapshot,
)
from provenance.filing_xbrl_extraction_ledger import FilingXbrlExtractionLedger
from provenance.metric_ontology import (
    CanonicalMetricCell,
    MetricOntology,
    OntologySnapshot,
    PeriodKind,
)
from provenance.population_canonical_resolution import (
    CanonicalResolutionPopulationRequest,
    CanonicalResolutionPrewriteManifest,
    CanonicalResolutionScopeManifest,
    populate_canonical_resolution,
    verify_canonical_projection,
    verify_canonical_resolution,
)
from provenance.population_completeness import PopulationTemporalScope
from tests.test_canonical_fact_resolution import (
    NOW,
    ROOT,
    _bind_every_published_cell,
    _resolution_database,
)
from tests.test_filing_xbrl_extraction_ledger import _entry, _output


def _seal_ontology(conn: sqlite3.Connection) -> str:
    snapshot_id = "ontology:population"
    MetricOntology(conn).seal_snapshot(
        OntologySnapshot(
            ontology_snapshot_id=snapshot_id,
            idempotency_key=snapshot_id,
            cutoff_at=NOW,
            recorded_at=NOW,
        )
    )
    return snapshot_id


def _ready_database(tmp_path: Path) -> sqlite3.Connection:
    conn = _resolution_database(
        tmp_path,
        _output((_entry(0, numeric_value=Decimal("100")),)),
    )
    FilingXbrlExtractionLedger(conn).publish(_output((_entry(0, numeric_value=Decimal("100")),)))
    _bind_every_published_cell(conn)
    _seal_ontology(conn)
    path = Path(str(conn.execute("PRAGMA database_list").fetchone()[2]))
    conn.commit()
    conn.close()
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(config, "head")
    upgraded = sqlite3.connect(path)
    upgraded.execute("PRAGMA foreign_keys = ON")
    return upgraded


def test_full_population_uses_system_clock_and_closes_exact_sets(
    tmp_path: Path,
) -> None:
    conn = _ready_database(tmp_path)
    recorded_at = NOW + timedelta(hours=2)
    try:
        result = populate_canonical_resolution(
            conn,
            CanonicalResolutionPopulationRequest(
                cutoff_at=NOW,
                operation_recorded_at=recorded_at,
                apply=True,
            ),
        )

        assert result.state == "complete"
        assert result.expected_cell_count == 1
        assert result.resolved_cell_count == 1
        assert result.resolution_snapshot_count == 1
        assert result.projection_count == 1
        assert result.projection_entry_count == 1
        assert result.checkpoint.safe_to_seal is True
        assert len(result.input_commitment_sha256) == 64
        assert len(result.output_commitment_sha256) == 64
        assert {
            str(row[0])
            for row in conn.execute(
                "SELECT canonical_metric_cell_id FROM canonical_fact_projection_entries"
            )
        } == {"canonical:revenue"}
    finally:
        conn.close()


def test_dual_clock_verifiers_ignore_artifacts_recorded_after_observation(
    tmp_path: Path,
) -> None:
    conn = _ready_database(tmp_path)
    recorded_at = NOW + timedelta(hours=2)
    try:
        populate_canonical_resolution(
            conn,
            CanonicalResolutionPopulationRequest(
                cutoff_at=NOW,
                operation_recorded_at=recorded_at,
                apply=True,
            ),
        )

        before_write = PopulationTemporalScope(
            knowledge_cutoff=NOW,
            observed_through=NOW,
        )
        at_write = PopulationTemporalScope(
            knowledge_cutoff=NOW,
            observed_through=recorded_at,
        )

        assert verify_canonical_resolution(conn, before_write).failed_count == 1
        assert verify_canonical_projection(conn, before_write).failed_count == 1
        assert verify_canonical_resolution(conn, at_write).materialized_count == 1
        assert verify_canonical_projection(conn, at_write).materialized_count == 1
    finally:
        conn.close()


def test_terminal_verifiers_reject_o1_artifacts_after_o2_late_input(
    tmp_path: Path,
) -> None:
    conn = _ready_database(tmp_path)
    observed_o1 = NOW + timedelta(hours=1)
    observed_o2 = NOW + timedelta(hours=2)
    try:
        populate_canonical_resolution(
            conn,
            CanonicalResolutionPopulationRequest(
                cutoff_at=NOW,
                operation_recorded_at=observed_o1,
                apply=True,
            ),
        )
        original = conn.execute(
            "SELECT metric_id,reporting_entity_id,period_kind,period_start,"
            "period_end,unit_family,accounting_basis,consolidation_scope "
            "FROM canonical_metric_cells WHERE canonical_metric_cell_id=?",
            ("canonical:revenue",),
        ).fetchone()
        assert original is not None
        MetricOntology(conn).persist_canonical_metric_cell(
            CanonicalMetricCell(
                canonical_metric_cell_id="canonical:late-o2-cell",
                idempotency_key="canonical:late-o2-cell",
                metric_id=str(original[0]),
                reporting_entity_id=str(original[1]),
                period_kind=cast(PeriodKind, str(original[2])),
                period_start=None
                if original[3] is None
                else population.datetime.fromisoformat(str(original[3])),
                period_end=population.datetime.fromisoformat(str(original[4])) + timedelta(days=1),
                unit_family=str(original[5]),
                accounting_basis=str(original[6]),
                consolidation_scope=str(original[7]),
                effective_at=NOW,
                knowledge_at=NOW,
                recorded_at=observed_o2,
            )
        )
        scope_o2 = PopulationTemporalScope(
            knowledge_cutoff=NOW,
            observed_through=observed_o2,
        )

        resolution = verify_canonical_resolution(conn, scope_o2)
        projection = verify_canonical_projection(conn, scope_o2)

        assert resolution.expected_count == 1
        assert resolution.materialized_count == 0
        assert resolution.failed_count == 1
        assert projection.expected_count == 1
        assert projection.materialized_count == 0
        assert projection.failed_count == 1
    finally:
        conn.close()


def test_bounded_all_stops_before_sealing_and_exposes_checkpoint(
    tmp_path: Path,
) -> None:
    conn = _ready_database(tmp_path)
    try:
        preview = populate_canonical_resolution(
            conn,
            CanonicalResolutionPopulationRequest(
                cutoff_at=NOW,
                operation_recorded_at=NOW + timedelta(hours=1),
                max_cells=1,
            ),
        )
        result = populate_canonical_resolution(
            conn,
            CanonicalResolutionPopulationRequest(
                cutoff_at=NOW,
                operation_recorded_at=NOW + timedelta(hours=1),
                apply=True,
                max_cells=1,
                input_commitment_sha256=preview.input_commitment_sha256,
                plan_commitment_sha256=preview.plan_commitment_sha256,
            ),
        )

        assert result.state == "partial"
        assert result.processed_cell_count == 1
        assert result.resolution_snapshot_count == 0
        assert result.projection_count == 0
        assert result.checkpoint.bounded is True
        assert result.checkpoint.safe_to_seal is False
        assert result.checkpoint.last_canonical_metric_cell_id == "canonical:revenue"
    finally:
        conn.close()


def test_prewrite_manifest_streams_candidate_commitment_after_subject_revision_supersession(
    tmp_path: Path,
) -> None:
    conn = _ready_database(tmp_path)
    conn.row_factory = sqlite3.Row
    try:
        original = conn.execute(
            "SELECT * FROM recorded_subject_binding_revisions ORDER BY revision LIMIT 1"
        ).fetchone()
        assert original is not None
        replacement = list(original)
        replacement[0] = f"{original[0]}:revision-2"
        replacement[1] = f"{original[1]}:revision-2"
        replacement[3] = 2
        replacement[9] = "subject_binding_reaffirmed"
        replacement[10] = "{}"
        replacement[15] = str(original[0])
        conn.execute(
            "INSERT INTO recorded_subject_binding_revisions "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            replacement,
        )

        manifest = population._prewrite_manifest(
            conn,
            NOW,
            NOW + timedelta(hours=1),
        )

        assert manifest.candidate_input_count == 1
        assert len(manifest.candidate_input_set_sha256) == 64
        assert not hasattr(manifest, "candidate_inputs")
        assert not hasattr(manifest, "canonical_metric_cell_ids")
    finally:
        conn.close()


def test_apply_rejects_a_dry_run_commitment_mismatch(tmp_path: Path) -> None:
    conn = _ready_database(tmp_path)
    try:
        with pytest.raises(ValueError, match="input commitment"):
            populate_canonical_resolution(
                conn,
                CanonicalResolutionPopulationRequest(
                    cutoff_at=NOW,
                    operation_recorded_at=NOW + timedelta(hours=1),
                    apply=True,
                    input_commitment_sha256="0" * 64,
                    plan_commitment_sha256="1" * 64,
                ),
            )
    finally:
        conn.close()


def test_stale_ontology_snapshot_is_rejected_before_resolution_write(
    tmp_path: Path,
) -> None:
    conn = _ready_database(tmp_path)
    try:
        original = conn.execute(
            "SELECT metric_id,reporting_entity_id,period_kind,period_start,"
            "period_end,unit_family,accounting_basis,consolidation_scope "
            "FROM canonical_metric_cells WHERE canonical_metric_cell_id=?",
            ("canonical:revenue",),
        ).fetchone()
        assert original is not None
        MetricOntology(conn).persist_canonical_metric_cell(
            CanonicalMetricCell(
                canonical_metric_cell_id="canonical:stale-snapshot-cell",
                idempotency_key="canonical:stale-snapshot-cell",
                metric_id=str(original[0]),
                reporting_entity_id=str(original[1]),
                period_kind=cast(PeriodKind, str(original[2])),
                period_start=None
                if original[3] is None
                else population.datetime.fromisoformat(str(original[3])),
                period_end=population.datetime.fromisoformat(str(original[4])) + timedelta(days=1),
                unit_family=str(original[5]),
                accounting_basis=str(original[6]),
                consolidation_scope=str(original[7]),
                effective_at=NOW,
                knowledge_at=NOW,
                recorded_at=NOW,
            )
        )

        with pytest.raises(ValueError, match="ontology snapshot is stale"):
            populate_canonical_resolution(
                conn,
                CanonicalResolutionPopulationRequest(
                    cutoff_at=NOW,
                    operation_recorded_at=NOW + timedelta(hours=1),
                    apply=True,
                ),
            )
        assert (
            conn.execute("SELECT COUNT(*) FROM canonical_fact_resolution_revisions").fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_equal_cardinality_swapped_issuer_cells_fail_exact_scope_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE canonical_fact_resolution_snapshot_members "
        "(resolution_snapshot_id TEXT,canonical_metric_cell_id TEXT)"
    )
    conn.execute(
        "CREATE TABLE reporting_entities "
        "(reporting_entity_id TEXT PRIMARY KEY,issuer_id TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE canonical_metric_cells "
        "(canonical_metric_cell_id TEXT PRIMARY KEY,reporting_entity_id TEXT NOT NULL,"
        "knowledge_at TEXT NOT NULL,recorded_at TEXT NOT NULL)"
    )
    conn.executemany(
        "INSERT INTO reporting_entities VALUES (?,?)",
        [("entity-a", "issuer-a"), ("entity-b", "issuer-b")],
    )
    conn.executemany(
        "INSERT INTO canonical_metric_cells VALUES (?,?,?,?)",
        [
            ("cell-a", "entity-a", NOW.isoformat(), NOW.isoformat()),
            ("cell-b", "entity-b", NOW.isoformat(), NOW.isoformat()),
        ],
    )
    manifest = CanonicalResolutionPrewriteManifest(
        cutoff_at=NOW,
        recorded_at=NOW,
        policy_name="policy",
        policy_version="1",
        policy_config_sha256="a" * 64,
        canonical_cell_count=2,
        canonical_cell_set_sha256="a" * 64,
        candidate_input_count=0,
        candidate_input_set_sha256="b" * 64,
        ontology_snapshot_id="ontology",
        ontology_snapshot_member_set_sha256="b" * 64,
        ontology_member_count=1,
        ontology_member_set_sha256="c" * 64,
        issuer_scopes=(
            CanonicalResolutionScopeManifest(
                issuer_id="issuer-a",
                reporting_entity_ids=("entity-a",),
                canonical_cell_count=1,
                canonical_cell_set_sha256="d" * 64,
            ),
            CanonicalResolutionScopeManifest(
                issuer_id="issuer-b",
                reporting_entity_ids=("entity-b",),
                canonical_cell_count=1,
                canonical_cell_set_sha256="e" * 64,
            ),
        ),
    )
    for issuer_id, wrong_cell in (("issuer-a", "cell-b"), ("issuer-b", "cell-a")):
        conn.execute(
            "INSERT INTO canonical_fact_resolution_snapshot_members VALUES (?,?)",
            (population._snapshot_id(issuer_id, NOW), wrong_cell),
        )

    class _FakeEngine:
        def __init__(self, _: sqlite3.Connection) -> None:
            pass

        def verify_snapshot(
            self,
            snapshot_id: str,
            cutoff_at: population.datetime,
            *,
            observed_through: population.datetime | None = None,
        ) -> VerifiedResolutionSnapshot:
            issuer_id = (
                "issuer-a"
                if snapshot_id.endswith(
                    population._digest(
                        "issuer-a", population._db_time(NOW), population._POLICY.config_sha256
                    )
                )
                else "issuer-b"
            )
            entity_id = "entity-a" if issuer_id == "issuer-a" else "entity-b"
            return VerifiedResolutionSnapshot(
                resolution_snapshot_id=snapshot_id,
                scope=ResolutionSnapshotScope(
                    issuer_id=issuer_id,
                    reporting_entity_ids=(entity_id,),
                ),
                cutoff_at=cutoff_at,
                recorded_at=NOW,
                member_count=1,
                member_set_sha256="c" * 64,
                scope_member_set_sha256="d" * 64,
                scope_sha256="e" * 64,
                snapshot_commitment_sha256="f" * 64,
            )

    monkeypatch.setattr(population, "CanonicalFactResolutionEngine", _FakeEngine)
    with pytest.raises(ValueError, match="cell scope is not exact"):
        population._verify_snapshot_sets(conn, NOW, NOW, manifest)


def test_ontology_manifest_hash_is_canonical() -> None:
    payload = json.dumps([], separators=(",", ":"))
    assert hashlib.sha256(payload.encode()).hexdigest() == population._sha(payload)
