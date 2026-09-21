"""Read-only foreign-source comparison through sealed v2 observations and ontology.

Inputs select retained identities, never values or admission. This diagnostic does
not publish, bless a population, or replace the canonical resolution producer.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from provenance.fact_read_model import FactReadModel, ProvenanceBundle
from provenance.immutable_artifact import read_stable_artifact
from provenance.metric_ontology import MetricOntology
from provenance.sec_filing_xbrl_ingest import file_uri_path
from sources.canary_corpus import seal_statement_corpus
from sources.foreign_filers import FOREIGN_FILER_ROSTER


class OracleSourceSelection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    ticker: str = Field(pattern=r"^[A-Z0-9][A-Z0-9.-]{0,15}$")
    document_version_id: str = Field(min_length=1)
    document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ForeignOracleManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["foreign-oracle-input/v1"] = "foreign-oracle-input/v1"
    cutoff_at: datetime
    sources: tuple[OracleSourceSelection, ...] = Field(min_length=1, max_length=100)
    canary_document_ids: tuple[int, ...] = Field(min_length=12, max_length=12)
    ontology_snapshot_id: str | None = None

    @model_validator(mode="after")
    def _scope(self) -> Self:
        if self.cutoff_at.tzinfo is None:
            raise ValueError("oracle cutoff requires timezone")
        identities = [item.document_version_id for item in self.sources]
        if len(identities) != len(set(identities)):
            raise ValueError("source selections must be unique")
        if len(set(self.canary_document_ids)) != 12:
            raise ValueError("select twelve distinct canary documents")
        return self


def _canonical_coordinate(
    conn: sqlite3.Connection, bundle: ProvenanceBundle, cutoff: datetime
) -> str | None:
    binding = MetricOntology(conn).binding_as_known(bundle.observation.observation_id, cutoff)
    if binding is None or binding.binding_status != "bound":
        return None
    return binding.canonical_metric_cell_id


def _source_bytes(conn: sqlite3.Connection, selected: OracleSourceSelection) -> None:
    row = conn.execute(
        "SELECT document.ticker,document.blob_sha256,blob.storage_uri,blob.byte_size FROM evidence_document_versions document JOIN evidence_content_blobs blob ON blob.sha256=document.blob_sha256 WHERE document.document_version_id=?",
        (selected.document_version_id,),
    ).fetchone()
    if row is None or row[0] != selected.ticker or row[1] != selected.document_sha256:
        raise ValueError("selected source identity mismatch")
    snapshot, _ = read_stable_artifact(file_uri_path(str(row[2])))
    if (snapshot.file_sha256, snapshot.size_bytes) != (selected.document_sha256, int(row[3])):
        raise ValueError("selected source bytes mismatch")


def _observation_ids(
    conn: sqlite3.Connection, document_version_id: str, cutoff: datetime
) -> tuple[str, ...]:
    """Inventory identities only; this does not admit their provenance or values."""
    return tuple(
        str(row[0])
        for row in conn.execute(
            "SELECT observation_id FROM fact_observations_v2 "
            "WHERE document_version_id=? AND observation_kind='reported' "
            "AND knowledge_at<=? AND recorded_at<=? ORDER BY observation_id",
            (document_version_id, cutoff, cutoff),
        )
    )


def _observations(
    conn: sqlite3.Connection, document_version_id: str, cutoff: datetime
) -> tuple[ProvenanceBundle, ...]:
    reader = FactReadModel(conn)
    return tuple(
        reader.provenance_bundle(observation_id, cutoff=cutoff)
        for observation_id in _observation_ids(conn, document_version_id, cutoff)
    )


def _reference(bundle: ProvenanceBundle) -> dict[str, object]:
    observation = bundle.observation
    return {
        "observation_id": observation.observation_id,
        "observation_payload_sha256": bundle.observation_payload_sha256,
        "document_version_id": bundle.evidence.document_version_id if bundle.evidence else None,
        "source_locator": bundle.evidence.source_locator.root if bundle.evidence else None,
        "value": str(observation.decimal_value) if observation.decimal_value is not None else None,
        "currency": observation.currency,
        "unit": observation.unit_key,
        "period_start": observation.period_start.isoformat() if observation.period_start else None,
        "period_end": observation.period_end.isoformat(),
        "fiscal_year": bundle.cell.fiscal_year,
        "fiscal_period": bundle.cell.fiscal_period,
        "revision_kind": observation.revision_kind,
        "knowledge_at": observation.knowledge_at.isoformat(),
    }


def compare_bound_observations(
    source: ProvenanceBundle, oracle: ProvenanceBundle
) -> dict[str, object]:
    """Compare already admitted, canonically aligned inputs without guessing transforms."""
    if source.evidence is None or oracle.evidence is None:
        raise ValueError("oracle requires reported source evidence")
    if (
        source.evidence.document_version_id == oracle.evidence.document_version_id
        or source.evidence.input_sha256 == oracle.evidence.input_sha256
    ):
        raise ValueError("a source cannot be its own independent oracle")
    left, right = source.observation, oracle.observation

    def coordinates(item: ProvenanceBundle) -> tuple[object, ...]:
        return (
            item.cell.reporting_entity_id,
            item.cell.scope_security_id,
            item.observation.period_kind,
            item.observation.period_start,
            item.observation.period_end,
            item.observation.currency,
            item.observation.unit_key,
            item.cell.accounting_basis,
            item.cell.consolidation_scope,
            item.cell.dimensions,
        )

    if coordinates(source) != coordinates(oracle):
        classification, reason = "TAXONOMY_MAPPING_DIVERGENCE", "source_coordinates_not_comparable"
    elif left.decimal_value is None or right.decimal_value is None:
        classification, reason = "MISSING_EXTRACTION", "numeric_value_unavailable"
    elif left.decimal_value == right.decimal_value:
        classification, reason = "EXACT_MATCH", "same_coordinates_and_exact_decimal_value"
    elif left.revision_kind != right.revision_kind and any(
        item in {"restatement", "amendment", "correction"}
        for item in (left.revision_kind, right.revision_kind)
    ):
        classification, reason = "SOURCE_TIMING_RESTATED", "explicit_source_revision_kinds_differ"
    else:
        classification, reason = (
            "MATERIAL_DISAGREEMENT",
            "unequal_values_without_proven_normalization_or_restatement",
        )
    return {
        "classification": classification,
        "reason": reason,
        "source": _reference(source),
        "oracle": _reference(oracle),
    }


def compare_foreign_sources(
    conn: sqlite3.Connection, manifest: ForeignOracleManifest, *, repo_root: Path
) -> dict[str, object]:
    """Read selected source graph and cached oracle; every unavailable step stays visible."""
    canary = seal_statement_corpus(
        conn,
        document_ids=manifest.canary_document_ids,
        repo_root=repo_root,
        cutoff_at=manifest.cutoff_at,
    )
    ontology = MetricOntology(conn)
    semantic_ready = manifest.ontology_snapshot_id is not None
    if manifest.ontology_snapshot_id is not None:
        ontology.verify_snapshot(manifest.ontology_snapshot_id)
        row = conn.execute(
            "SELECT cutoff_at FROM ontology_snapshot_headers WHERE ontology_snapshot_id=?",
            (manifest.ontology_snapshot_id,),
        ).fetchone()
        if row is None:
            raise ValueError("ontology snapshot header unavailable")
        snapshot_cutoff = datetime.fromisoformat(str(row[0]))
        if snapshot_cutoff.tzinfo is None:
            snapshot_cutoff = snapshot_cutoff.replace(tzinfo=UTC)
        if snapshot_cutoff != manifest.cutoff_at:
            raise ValueError("ontology snapshot must bind the exact comparison cutoff")
    oracle_by_coordinate: dict[str, list[ProvenanceBundle]] = {}
    if semantic_ready:
        for item in canary.files:
            if item.evidence_document_version_id is None:
                continue
            for bundle in _observations(
                conn, item.evidence_document_version_id, manifest.cutoff_at
            ):
                key = (
                    _canonical_coordinate(conn, bundle, manifest.cutoff_at)
                    if semantic_ready
                    else None
                )
                if key:
                    oracle_by_coordinate.setdefault(key, []).append(bundle)
    receipts: list[dict[str, object]] = []
    exact = 0
    for selected in manifest.sources:
        _source_bytes(conn, selected)
        # Without a sealed semantic snapshot, no value can be compared. Keep
        # candidate identities/counts explicit without repeatedly verifying entire
        # publication graphs for facts whose admission cannot affect this outcome.
        observation_ids = _observation_ids(conn, selected.document_version_id, manifest.cutoff_at)
        bundles = (
            _observations(conn, selected.document_version_id, manifest.cutoff_at)
            if semantic_ready
            else ()
        )
        comparisons: list[dict[str, object]] = [
            {
                "classification": "MISSING_EXTRACTION",
                "reason": "canonical_metric_binding_unavailable",
                "source": {
                    "observation_id": observation_id,
                    "document_version_id": selected.document_version_id,
                },
                "oracle": None,
            }
            for observation_id in observation_ids
            if not semantic_ready
        ]
        for bundle in bundles:
            coordinate = _canonical_coordinate(conn, bundle, manifest.cutoff_at)
            candidates = [
                item
                for item in oracle_by_coordinate.get(coordinate or "", [])
                if item.evidence
                and item.evidence.document_version_id != selected.document_version_id
                and item.evidence.input_sha256 != selected.document_sha256
            ]
            if coordinate is None or len(candidates) != 1:
                comparisons.append(
                    {
                        "classification": "MISSING_EXTRACTION",
                        "reason": "canonical_metric_binding_unavailable"
                        if coordinate is None
                        else "independent_oracle_missing_or_ambiguous",
                        "source": _reference(bundle),
                        "oracle": None,
                    }
                )
            else:
                comparisons.append(compare_bound_observations(bundle, candidates[0]))
        exact += sum(item["classification"] == "EXACT_MATCH" for item in comparisons)
        profile = FOREIGN_FILER_ROSTER.get(selected.ticker)
        receipts.append(
            {
                "ticker": selected.ticker,
                "document_version_id": selected.document_version_id,
                "document_sha256": selected.document_sha256,
                "observations": len(observation_ids),
                "observation_admission": "verified"
                if semantic_ready
                else "not_evaluated_missing_ontology",
                "comparisons": comparisons,
                "reporting_cadence": profile.cadence.value if profile else "unavailable",
                "quarterly_requirement": "not_applicable"
                if profile and profile.cadence.value == "semiannual"
                else "source_inventory_required",
                "completeness": "unverified",
                "reason_codes": []
                if observation_ids
                else ["no_reported_v2_observation_candidates"],
            }
        )
    quick = [str(row[0]) for row in conn.execute("PRAGMA quick_check")]
    violations = len(conn.execute("PRAGMA foreign_key_check").fetchall())
    roster = [
        profile.model_dump(mode="json") for _, profile in sorted(FOREIGN_FILER_ROSTER.items())
    ]
    payload: dict[str, object] = {
        "schema_version": "foreign-oracle-receipt/v1",
        "status": "PARTIAL",
        "cutoff_at": manifest.cutoff_at.isoformat(),
        "total_tickers_evaluated": len({item.ticker for item in manifest.sources}),
        "total_exact_matches": exact,
        "canary_sha256": canary.corpus_sha256,
        "governed_roster": roster,
        "roster_sha256": hashlib.sha256(
            json.dumps(roster, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "expected_period_population": "source_inventory_evidence_required",
        "asml_image_negative": "unverified_unless_selected_source_evidence_proves_it",
        "receipts": receipts,
        "quick_check": quick,
        "foreign_key_violations": violations,
        "publication_performed": False,
        "decision_grade": False,
        "entitlement_proven": False,
        "reason_codes": [
            "population_completeness_not_proven",
            "canonical_resolution_projection_replay_not_proven",
        ],
    }
    if not semantic_ready:
        payload["reason_codes"] = [
            "population_completeness_not_proven",
            "canonical_resolution_projection_replay_not_proven",
            "ontology_snapshot_unavailable",
        ]
    if quick != ["ok"] or violations:
        payload["status"] = "HOLD"
        payload["reason_codes"] = ["database_integrity_failed"]
    payload["receipt_sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    return payload
