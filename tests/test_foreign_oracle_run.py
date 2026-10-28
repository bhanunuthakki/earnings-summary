"""Source-bound offline diagnostic receipts; no fixture claims production completion."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from execution.backfill_foreign_oracle import main
from provenance.fact_read_model import (
    FactReadModel,
    ObservationUnavailableError,
    ProvenanceBundle,
)
from sources.foreign_normalization_run import normalize_foreign_sources
from sources.foreign_oracle_run import (
    ForeignOracleManifest,
    OracleSourceSelection,
    compare_bound_observations,
    compare_foreign_sources,
)
from tests.test_canary_corpus import seed_canary
from tests.test_foreign_normalization_run import STAMP, seed_foreign_fixture


def test_cli_without_admitted_population_cannot_certify_backfill(tmp_path: Path) -> None:
    receipt_path = tmp_path / "receipt.json"
    result = subprocess.run(
        [
            sys.executable,
            "execution/backfill_foreign_oracle.py",
            "--output-receipt",
            str(receipt_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    receipt = json.loads(receipt_path.read_text())
    assert receipt["status"] == "HOLD"
    assert receipt["receipts"] == []
    assert receipt["total_tickers_evaluated"] == 0
    assert receipt["total_exact_matches"] == 0
    assert receipt["reason_codes"] == ["source_bound_input_manifest_and_database_required"]


def test_empty_source_population_is_not_a_comparison_manifest() -> None:
    with pytest.raises(ValidationError, match="sources"):
        ForeignOracleManifest(cutoff_at=STAMP, sources=(), canary_document_ids=tuple(range(12)))


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


def test_observations_use_ordered_strict_bundle_batch(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sources import foreign_oracle_run

    with sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn:
        manifest = seed_source_oracle(conn, tmp_path)
        document_version_id = manifest.sources[0].document_version_id
        read_observation_ids = cast(
            "Callable[[sqlite3.Connection, str, datetime], tuple[str, ...]]",
            getattr(foreign_oracle_run, "_observation_ids"),
        )
        read_observations = cast(
            "Callable[[sqlite3.Connection, str, datetime], tuple[ProvenanceBundle, ...]]",
            getattr(foreign_oracle_run, "_observations"),
        )
        retained_ids = read_observation_ids(conn, document_version_id, manifest.cutoff_at)
        assert retained_ids
        calls: list[tuple[tuple[str, ...], datetime]] = []
        original_batch = FactReadModel.required_provenance_bundles

        def reject_scalar(*_args: object, **_kwargs: object) -> ProvenanceBundle:
            raise AssertionError("foreign oracle observations must use the strict batch API")

        def record_batch(
            reader: FactReadModel,
            observation_ids: tuple[str, ...],
            *,
            cutoff: datetime,
        ) -> tuple[ProvenanceBundle, ...]:
            calls.append((observation_ids, cutoff))
            return original_batch(reader, observation_ids, cutoff=cutoff)

        requested_ids = (retained_ids[0], retained_ids[0])

        def provide_ids(
            _conn: sqlite3.Connection, _document_version_id: str, _cutoff: datetime
        ) -> tuple[str, ...]:
            return requested_ids

        monkeypatch.setattr(foreign_oracle_run, "_observation_ids", provide_ids)
        monkeypatch.setattr(FactReadModel, "provenance_bundle", reject_scalar)
        monkeypatch.setattr(FactReadModel, "required_provenance_bundles", record_batch)
        before = conn.total_changes
        bundles = read_observations(conn, document_version_id, manifest.cutoff_at)
        assert tuple(bundle.observation.observation_id for bundle in bundles) == requested_ids
        assert calls == [(requested_ids, manifest.cutoff_at)]

        requested_ids = (retained_ids[0], "not-a-retained-observation")
        with pytest.raises(ObservationUnavailableError) as caught:
            read_observations(conn, document_version_id, manifest.cutoff_at)
        assert caught.value.observation_id == requested_ids[1]
        assert calls[-1] == (requested_ids, manifest.cutoff_at)
        assert conn.total_changes == before


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
        manifest = seed_source_oracle(conn, tmp_path)
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
        zero_source = source.model_copy(
            update={
                "observation": source.observation.model_copy(update={"decimal_value": Decimal(0)})
            }
        )
        zero_oracle = oracle.model_copy(
            update={
                "observation": oracle.observation.model_copy(update={"decimal_value": Decimal(0)})
            }
        )
        zero_comparison = compare_bound_observations(zero_source, zero_oracle)
        assert zero_comparison["classification"] == "EXACT_MATCH"
        assert "status" not in zero_comparison
        receipt = compare_foreign_sources(conn, manifest, repo_root=tmp_path)
        assert receipt["status"] == "PARTIAL"
        assert receipt["decision_grade"] is False
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
