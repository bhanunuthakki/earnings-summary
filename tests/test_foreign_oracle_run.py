"""Source-bound offline diagnostic receipts; no fixture claims production completion."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest

from execution.backfill_foreign_oracle import main
from provenance.fact_read_model import FactReadModel
from sources.foreign_normalization_run import normalize_foreign_sources
from sources.foreign_oracle_run import (
    ForeignOracleManifest,
    OracleSourceSelection,
    compare_bound_observations,
    compare_foreign_sources,
)
from tests.test_canary_corpus import seed_canary
from tests.test_foreign_normalization_run import STAMP, seed_foreign_fixture


def seed_source_oracle(conn: sqlite3.Connection, root: Path) -> ForeignOracleManifest:
    source = seed_foreign_fixture(conn, root)
    normalize_foreign_sources(conn, source, input_manifest_sha256="4" * 64, apply=True)
    canary = seed_canary(conn, root)
    selected = source.documents[0]
    return ForeignOracleManifest(
        cutoff_at=STAMP,
        sources=(
            OracleSourceSelection(
                ticker=selected.ticker,
                document_version_id=selected.document_version_id,
                document_sha256=selected.document_sha256,
            ),
        ),
        canary_document_ids=canary,
    )


def test_cli_reads_real_sealed_graph_and_reports_missing_comparison_inputs(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = migrated_db(tmp_path / "fixture.db")
    with sqlite3.connect(database) as conn:
        manifest = seed_source_oracle(conn, tmp_path)
        before = conn.total_changes
        first = compare_foreign_sources(conn, manifest, repo_root=tmp_path)
        second = compare_foreign_sources(conn, manifest, repo_root=tmp_path)
        assert first == second
        assert conn.total_changes == before
    before_hash = hashlib.sha256(database.read_bytes()).hexdigest()
    input_path = tmp_path / "input.json"
    input_path.write_text(manifest.model_dump_json())
    output = tmp_path / "receipt.json"
    assert (
        main(
            [
                "--db",
                str(database),
                "--input-manifest",
                str(input_path),
                "--repo-root",
                str(tmp_path),
                "--output-receipt",
                str(output),
                "--json",
            ]
        )
        == 1
    )
    receipt = json.loads(output.read_text())
    assert receipt["status"] == "PARTIAL"
    assert receipt["receipts"][0]["observations"] == 1
    assert (
        receipt["receipts"][0]["comparisons"][0]["reason"] == "canonical_metric_binding_unavailable"
    )
    assert receipt["quick_check"] == ["ok"]
    assert receipt["foreign_key_violations"] == 0
    assert receipt["decision_grade"] is False
    assert receipt["publication_performed"] is False
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before_hash
    assert "PARTIAL" in capsys.readouterr().out


def test_changed_source_bytes_and_unpublished_v2_inputs_fail_closed(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    with sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn:
        manifest = seed_source_oracle(conn, tmp_path)
        source = tmp_path / "statement.json"
        source.write_bytes(b"changed bytes")
        with pytest.raises(ValueError, match="source bytes mismatch"):
            compare_foreign_sources(conn, manifest, repo_root=tmp_path)


def test_independent_comparison_never_invents_conversion_or_uses_same_bytes(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    with sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn:
        seed_source_oracle(conn, tmp_path)
        observation_id = str(
            conn.execute("SELECT observation_id FROM fact_observations_v2").fetchone()[0]
        )
        source = FactReadModel(conn).provenance_bundle(observation_id, cutoff=STAMP)
        with pytest.raises(ValueError, match="own independent oracle"):
            compare_bound_observations(source, source)
        assert source.evidence is not None
        # Arithmetic-only synthetic alternate graph. Positive CLI above retains
        # missing semantic/oracle inputs rather than promoting these stand-ins.
        oracle = source.model_copy(
            update={
                "evidence": source.evidence.model_copy(
                    update={"document_version_id": "synthetic-other", "input_sha256": "a" * 64}
                )
            }
        )
        assert compare_bound_observations(source, oracle)["classification"] == "EXACT_MATCH"
        changed = oracle.model_copy(
            update={
                "observation": oracle.observation.model_copy(
                    update={"decimal_value": Decimal("99")}
                )
            }
        )
        assert (
            compare_bound_observations(source, changed)["classification"] == "MATERIAL_DISAGREEMENT"
        )
        foreign_currency = oracle.model_copy(
            update={"observation": oracle.observation.model_copy(update={"currency": "DKK"})}
        )
        assert (
            compare_bound_observations(source, foreign_currency)["classification"]
            == "TAXONOMY_MAPPING_DIVERGENCE"
        )


def test_sealed_ontology_enables_binding_lookup_without_inventing_oracle_facts(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    from provenance.population_metric_ontology import (
        MetricOntologyPopulationRequest,
        populate_metric_ontology,
    )

    with sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn:
        manifest = seed_source_oracle(conn, tmp_path)
        populated = populate_metric_ontology(
            conn,
            MetricOntologyPopulationRequest(
                knowledge_cutoff=STAMP, operation_recorded_at=STAMP, apply=True
            ),
        )
        assert populated is not None
        snapshot = str(
            conn.execute("SELECT ontology_snapshot_id FROM ontology_snapshot_headers").fetchone()[0]
        )
        selected = manifest.model_copy(update={"ontology_snapshot_id": snapshot})
        receipt = compare_foreign_sources(conn, selected, repo_root=tmp_path)
        assert receipt["status"] == "PARTIAL"
        assert receipt["total_exact_matches"] == 0
        assert "independent_oracle_missing_or_ambiguous" in json.dumps(receipt)
