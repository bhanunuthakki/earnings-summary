"""Normalize selected retained foreign documents through governed publishers.

This is a source-plane operation, not semantic approval. Native packages use the
qualified offline XBRL processor; interim documents require an existing governed
extraction. The manifest supplies assertions, never facts or admission authority.
Each publisher owns its existing atomic transaction and replay contract.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from filings.inline_xbrl_processor import (
    ProcessorPackageMember,
    load_approved_processor_bundle_manifest,
)
from pipeline.source_policy import (
    ArtifactKind,
    CollectionSource,
    authorize_collection_target_in_connection,
)
from provenance.immutable_artifact import (
    ImmutableArtifactSnapshot,
    assert_artifact_unchanged,
    read_stable_artifact,
)
from provenance.population_identity import (
    PopulationIdentityRequest,
    PopulationIdentityResult,
    populate_recorded_subject_bindings,
)
from provenance.population_source_facts import (
    SourceFactDocumentScope,
    SourceFactPopulationBatchError,
    SourceFactPopulationRequest,
    SourceFactPopulationResult,
    populate_source_fact_plane,
)
from provenance.sec_filing_xbrl_ingest import (
    FilingXbrlIngestRequest,
    file_uri_path,
    ingest_sec_filing_xbrl,
)
from provenance.sec_native_capture import load_captured_sec_filing_package
from provenance.source_fact_publication import verify_source_fact_publication
from sources.foreign_filers import ForeignFilingForm


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ForeignPeriodAssertion(_Frozen):
    """An expected reported period, never a calendar-derived fiscal quarter."""

    start: date | None = None
    end: date
    fiscal_period: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.start is not None and self.start > self.end:
            raise ValueError("reported period start exceeds end")
        return self


class ForeignDocumentInput(_Frozen):
    ticker: str = Field(pattern=r"^[A-Z0-9][A-Z0-9.-]{0,15}$")
    issuer_id: str = Field(min_length=1)
    document_version_id: str = Field(min_length=1)
    document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    form: ForeignFilingForm
    currencies: tuple[str | None, ...] = Field(min_length=1)
    units: tuple[str, ...] = Field(min_length=1)
    periods: tuple[ForeignPeriodAssertion, ...] = Field(min_length=1)
    inventory_key: str | None = None
    accession_number: str | None = None
    expected_cik: str | None = Field(default=None, pattern=r"^\d{10}$")

    @property
    def native(self) -> bool:
        return self.form not in {
            ForeignFilingForm.ISSUER_IR_SPREADSHEET,
            ForeignFilingForm.ISSUER_STATEMENT_CACHE,
        }

    @model_validator(mode="after")
    def _contract(self) -> Self:
        if any(
            value is not None and (len(value) != 3 or not value.isalpha() or value != value.upper())
            for value in self.currencies
        ):
            raise ValueError("currencies must be explicit uppercase codes")
        if any(not value or value.lower() == "unknown" for value in self.units):
            raise ValueError("units must be explicit")
        if self.native and not all((self.inventory_key, self.accession_number, self.expected_cik)):
            raise ValueError("native forms require inventory, accession and CIK")
        if not self.native and any((self.inventory_key, self.accession_number, self.expected_cik)):
            raise ValueError("interim inputs cannot assert a native package")
        return self


class ForeignProcessorRuntime(_Frozen):
    bundle_manifest: Path
    runtime_root: Path
    bundle_python: Path
    sandbox_launcher: Path
    offline_artifacts: tuple[ProcessorPackageMember, ...] = ()


class ForeignNormalizationManifest(_Frozen):
    schema_version: Literal["foreign-normalization-input/v1"] = "foreign-normalization-input/v1"
    data_cutoff_at: datetime
    recorded_at: datetime
    documents: tuple[ForeignDocumentInput, ...] = Field(min_length=1, max_length=100)
    processor: ForeignProcessorRuntime | None = None

    @model_validator(mode="after")
    def _contract(self) -> Self:
        if self.data_cutoff_at.tzinfo is None or self.recorded_at.tzinfo is None:
            raise ValueError("normalization clocks require timezones")
        if self.recorded_at < self.data_cutoff_at:
            raise ValueError("recorded time precedes cutoff")
        ids = [item.document_version_id for item in self.documents]
        if len(ids) != len(set(ids)):
            raise ValueError("document selectors must be unique")
        if any(item.native for item in self.documents) and self.processor is None:
            raise ValueError("native forms require a qualified processor runtime")
        return self


class ForeignDocumentResult(_Frozen):
    ticker: str
    document_version_id: str
    document_sha256: str
    status: Literal["planned", "normalized", "partial", "rejected", "failed"]
    observations: int = 0
    publications: tuple[str, ...] = ()
    findings: tuple[str, ...] = ()


class ForeignRunReceipt(_Frozen):
    status: Literal["DRY_RUN", "PARTIAL", "HOLD"]
    mode: Literal["dry_run", "apply"]
    input_manifest_sha256: str
    receipts: tuple[ForeignDocumentResult, ...]
    total_tickers_evaluated: int
    decision_grade: Literal[False] = False
    reason_codes: tuple[str, ...]
    publication_checkpoint: dict[str, JsonValue] | None = None
    subject_identity: PopulationIdentityResult | None = None
    source_population_plan: SourceFactPopulationResult | None = None


def _day(value: object) -> date | None:
    return None if value is None else date.fromisoformat(str(value)[:10])


def _validate_semantics(
    rows: list[sqlite3.Row],
    item: ForeignDocumentInput,
) -> None:
    if not rows:
        raise ValueError("missing_nonzero_governed_extraction")
    expected = {(period.start, period.end, period.fiscal_period) for period in item.periods}
    observed: set[tuple[date | None, date | None, str | None]] = set()
    for row in rows:
        if row["currency"] not in item.currencies:
            raise ValueError("source_currency_mismatch")
        if row["unit"] not in item.units:
            raise ValueError("source_unit_mismatch")
        period = (_day(row["period_start"]), _day(row["period_end"]), str(row["fiscal_period"]))
        if period not in expected:
            raise ValueError("source_period_mismatch")
        observed.add(period)
    if observed != expected:
        raise ValueError("missing_expected_period")


def _preflight(
    conn: sqlite3.Connection,
    manifest: ForeignNormalizationManifest,
    item: ForeignDocumentInput,
) -> tuple[ImmutableArtifactSnapshot, int]:
    authorization = authorize_collection_target_in_connection(
        conn,
        item.ticker,
        requested=False,
        source=CollectionSource.SEC if item.native else CollectionSource.IR,
        artifact_kind=ArtifactKind.FILING_PACKAGE if item.native else ArtifactKind.IR_DOCUMENT,
        require_corporate_instrument=True,
    )
    if not authorization.allowed:
        raise ValueError("stored_identity_denied:" + authorization.status.value)
    row = conn.execute(
        "SELECT version.*,blob.storage_uri,blob.byte_size,source.blob_sha256 AS source_sha "
        "FROM evidence_document_versions version "
        "JOIN evidence_content_blobs blob ON blob.sha256=version.blob_sha256 "
        "JOIN evidence_source_observations source ON source.observation_id=version.observation_id "
        "WHERE version.document_version_id=? AND datetime(version.recorded_at)<=datetime(?) "
        "AND datetime(source.retrieved_at)<=datetime(?)",
        (
            item.document_version_id,
            manifest.recorded_at.isoformat(),
            manifest.data_cutoff_at.isoformat(),
        ),
    ).fetchone()
    if row is None:
        raise ValueError("missing_captured_document_at_cutoff")
    if (row["ticker"], row["issuer_id"], row["blob_sha256"], row["source_sha"]) != (
        item.ticker,
        item.issuer_id,
        item.document_sha256,
        item.document_sha256,
    ):
        raise ValueError("captured_source_identity_mismatch")
    snapshot, _payload = read_stable_artifact(file_uri_path(str(row["storage_uri"])))
    if snapshot.file_sha256 != item.document_sha256 or snapshot.size_bytes != row["byte_size"]:
        raise ValueError("captured_source_bytes_mismatch")
    retained_count = 0
    if item.native:
        if row["form_type"] != item.form.value or row["accession_number"] != item.accession_number:
            raise ValueError("native_form_or_accession_mismatch")
        assert item.inventory_key is not None and item.accession_number is not None
        package = load_captured_sec_filing_package(
            conn,
            inventory_key=item.inventory_key,
            accession_number=item.accession_number,
        )
        primary = next(member for member in package if member.document_type == "filing")
        if (
            primary.document_version_id != item.document_version_id
            or primary.blob_sha256 != item.document_sha256
        ):
            raise ValueError("native_package_primary_mismatch")
    else:
        allowed = (
            {"ir_historical_spreadsheet"}
            if item.form == ForeignFilingForm.ISSUER_IR_SPREADSHEET
            else {
                "fmp_income_statement",
                "fmp_balance_sheet",
                "fmp_cashflow",
                "fmp_as_reported_financial",
                "fmp_as_reported_income",
                "fmp_as_reported_balance",
                "fmp_as_reported_cashflow",
            }
        )
        if row["document_type"] not in allowed:
            raise ValueError("unsupported_interim_document_type")
        legacy = conn.execute(
            "SELECT ticker,sha256 FROM documents WHERE id=?",
            (row["legacy_document_id"],),
        ).fetchone()
        if legacy is None or tuple(legacy) != (item.ticker, item.document_sha256):
            raise ValueError("legacy_document_identity_mismatch")
        runs = conn.execute(
            "SELECT outcome,input_sha256 FROM evidence_extraction_runs WHERE document_version_id=?",
            (item.document_version_id,),
        ).fetchall()
        if not runs or any(
            run["outcome"] != "succeeded" or run["input_sha256"] != item.document_sha256
            for run in runs
        ):
            raise ValueError("incomplete_extraction_run")
        rows = conn.execute(
            "SELECT observation.currency,observation.unit,observation.period_start,"
            "observation.period_end,observation.fiscal_period_type AS fiscal_period, "
            "run.input_sha256,run.outcome,run.completed_at "
            "FROM reported_observations observation "
            "JOIN evidence_nodes node ON node.node_id=observation.evidence_node_id "
            "JOIN evidence_extraction_runs run ON run.extraction_run_id=node.extraction_run_id "
            "WHERE run.document_version_id=?",
            (item.document_version_id,),
        ).fetchall()
        for observation in rows:
            if observation["input_sha256"] != item.document_sha256:
                raise ValueError("extraction_source_hash_mismatch")
            completed = observation["completed_at"]
            if observation["outcome"] != "succeeded" or completed is None:
                raise ValueError("incomplete_extraction_run")
            completed_at = datetime.fromisoformat(str(completed).replace("Z", "+00:00"))
            if completed_at.tzinfo is None:
                completed_at = completed_at.replace(tzinfo=manifest.data_cutoff_at.tzinfo)
            if completed_at > manifest.data_cutoff_at:
                raise ValueError("extraction_after_cutoff")
        _validate_semantics(rows, item)
        retained_count = len(rows)
    return snapshot, retained_count


def _published(
    conn: sqlite3.Connection, item: ForeignDocumentInput, cutoff: datetime
) -> tuple[int, tuple[str, ...]]:
    rows = conn.execute(
        "SELECT observation.observation_id,cell.currency,cell.unit_key AS unit,"
        "cell.period_start,cell.period_end,cell.fiscal_period "
        "FROM fact_observations_v2 observation JOIN fact_cells_v2 cell "
        "ON cell.fact_cell_id=observation.fact_cell_id WHERE observation.document_version_id=?",
        (item.document_version_id,),
    ).fetchall()
    publications = tuple(
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT member.publication_id FROM source_fact_publication_members member "
            "JOIN fact_observations_v2 observation ON observation.observation_id=member.record_id "
            "WHERE member.record_kind='fact_observation' AND observation.document_version_id=? "
            "ORDER BY member.publication_id",
            (item.document_version_id,),
        )
    )
    for publication_id in publications:
        verify_source_fact_publication(conn, publication_id=publication_id, cutoff=cutoff)
    return len(rows), publications


def normalize_foreign_sources(
    conn: sqlite3.Connection,
    manifest: ForeignNormalizationManifest,
    *,
    input_manifest_sha256: str,
    apply: bool = False,
) -> ForeignRunReceipt:
    """Preflight all inputs; publish selected sources without inventing semantics."""
    if conn.in_transaction:
        raise ValueError("normalization_requires_unowned_transaction_boundary")
    original_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    results: list[ForeignDocumentResult] = []
    snapshots: list[ImmutableArtifactSnapshot] = []
    retained_count = 0
    retained_by_document: dict[str, int] = {}
    plan: SourceFactPopulationResult | None = None
    batch_failure: SourceFactPopulationBatchError | None = None
    try:
        for item in manifest.documents:
            try:
                snapshot, count = _preflight(conn, manifest, item)
                snapshots.append(snapshot)
                retained_count += count
                retained_by_document[item.document_version_id] = count
            except (ValueError, OSError, RuntimeError) as exc:
                results.append(
                    ForeignDocumentResult(
                        ticker=item.ticker,
                        document_version_id=item.document_version_id,
                        document_sha256=item.document_sha256,
                        status="rejected",
                        findings=(str(exc),),
                    )
                )
        if results:
            return _receipt(
                manifest, input_manifest_sha256, apply, results, "source_preflight_failed"
            )
        identity_request = PopulationIdentityRequest(
            document_version_ids=tuple(item.document_version_id for item in manifest.documents),
            knowledge_cutoff=manifest.data_cutoff_at,
            operation_recorded_at=manifest.recorded_at,
        )
        identity = populate_recorded_subject_bindings(conn, identity_request)
        if identity.unresolved_count or identity.conflict_count:
            return _receipt(
                manifest,
                input_manifest_sha256,
                apply,
                [],
                "canonical_subject_identity_unavailable",
                identity=identity,
            )
        if apply:
            # Identity closure is a separately committed prerequisite stage.
            # Its receipt remains visible even if later source publication fails.
            conn.execute("BEGIN IMMEDIATE")
            try:
                identity = populate_recorded_subject_bindings(
                    conn, identity_request.model_copy(update={"apply": True})
                )
                if identity.unresolved_count or identity.conflict_count:
                    raise ValueError("canonical_subject_identity_changed")
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        scopes = tuple(
            SourceFactDocumentScope(ticker=item.ticker, document_sha256=item.document_sha256)
            for item in manifest.documents
            if not item.native
        )
        bridge_request = (
            SourceFactPopulationRequest(
                document_scopes=scopes,
                data_cutoff_at=manifest.data_cutoff_at,
                operation_recorded_at=manifest.recorded_at,
            )
            if scopes
            else None
        )
        if bridge_request is not None:
            plan = populate_source_fact_plane(conn, bridge_request)
            if plan.eligible_count == 0 or plan.expected_count != retained_count:
                return _receipt(
                    manifest,
                    input_manifest_sha256,
                    apply,
                    [],
                    "incomplete_or_unadmitted_interim_extraction",
                    identity=identity,
                    population_plan=plan,
                )
        for snapshot in snapshots:
            assert_artifact_unchanged(snapshot)
        if apply and bridge_request is not None:
            assert plan is not None
            try:
                populate_source_fact_plane(
                    conn,
                    bridge_request.model_copy(
                        update={
                            "apply": True,
                            "input_commitment_sha256": plan.input_commitment_sha256,
                            "planned_output_commitment_sha256": plan.planned_output_commitment_sha256,
                        }
                    ),
                )
            except SourceFactPopulationBatchError as exc:
                batch_failure = exc
            except ValueError:
                return _receipt(
                    manifest,
                    input_manifest_sha256,
                    apply,
                    [],
                    "source_population_plan_changed",
                    identity=identity,
                    population_plan=plan,
                )
        for item in manifest.documents:
            try:
                if batch_failure is not None:
                    raise ValueError("interim_publication_batch_failed")
                if item.native:
                    runtime = manifest.processor
                    assert runtime is not None
                    assert (
                        item.inventory_key is not None
                        and item.accession_number is not None
                        and item.expected_cik is not None
                    )
                    result = ingest_sec_filing_xbrl(
                        conn,
                        FilingXbrlIngestRequest(
                            inventory_key=item.inventory_key,
                            accession_number=item.accession_number,
                            expected_cik=item.expected_cik,
                            runtime_root=runtime.runtime_root,
                            bundle_python=runtime.bundle_python,
                            sandbox_launcher=runtime.sandbox_launcher,
                            recorded_at=manifest.recorded_at,
                            offline_artifacts=runtime.offline_artifacts,
                            apply=apply,
                        ),
                        approved_bundle=load_approved_processor_bundle_manifest(
                            runtime.bundle_manifest
                        ),
                    )
                    if (
                        result.normalized_count == 0
                        or result.rejected_count
                        or result.quarantined_count
                    ):
                        raise ValueError("incomplete_native_extraction")
                if item.native:
                    native_rows = conn.execute(
                        "SELECT cell.currency,cell.unit_key AS unit,cell.period_start,cell.period_end,cell.fiscal_period "
                        "FROM fact_observations_v2 observation JOIN fact_cells_v2 cell ON cell.fact_cell_id=observation.fact_cell_id "
                        "WHERE observation.document_version_id=?",
                        (item.document_version_id,),
                    ).fetchall()
                    _validate_semantics(native_rows, item)
                count, publications = (
                    _published(conn, item, manifest.recorded_at) if apply else (0, ())
                )
                status: Literal["planned", "normalized", "partial"] = (
                    "planned"
                    if not apply
                    else "normalized"
                    if count
                    and publications
                    and (item.native or count == retained_by_document[item.document_version_id])
                    else "partial"
                )
                results.append(
                    ForeignDocumentResult(
                        ticker=item.ticker,
                        document_version_id=item.document_version_id,
                        document_sha256=item.document_sha256,
                        status=status,
                        observations=count,
                        publications=publications,
                        findings=("canonical_semantic_resolution_not_performed",),
                    )
                )
            except (ValueError, OSError, RuntimeError) as exc:
                count, publications = (
                    _published(conn, item, manifest.recorded_at) if apply else (0, ())
                )
                results.append(
                    ForeignDocumentResult(
                        ticker=item.ticker,
                        document_version_id=item.document_version_id,
                        document_sha256=item.document_sha256,
                        status="partial" if count else "failed",
                        observations=count,
                        publications=publications,
                        findings=(str(exc),),
                    )
                )
        receipt = _receipt(
            manifest,
            input_manifest_sha256,
            apply,
            results,
            "interim_publication_batch_failed"
            if batch_failure
            else "canonical_semantic_resolution_not_performed",
            identity=identity,
            population_plan=plan,
        )
        return (
            receipt.model_copy(
                update={"publication_checkpoint": batch_failure.checkpoint_payload()}
            )
            if batch_failure
            else receipt
        )
    finally:
        conn.row_factory = original_factory


def _receipt(
    manifest: ForeignNormalizationManifest,
    digest: str,
    apply: bool,
    results: list[ForeignDocumentResult],
    reason: str,
    *,
    identity: PopulationIdentityResult | None = None,
    population_plan: SourceFactPopulationResult | None = None,
) -> ForeignRunReceipt:
    planned = len(results) == len(manifest.documents) and all(
        item.status == "planned" for item in results
    )
    return ForeignRunReceipt(
        status="DRY_RUN"
        if planned and not apply
        else "PARTIAL"
        if any(item.observations for item in results)
        or (identity is not None and identity.created_count > 0)
        else "HOLD",
        mode="apply" if apply else "dry_run",
        input_manifest_sha256=digest,
        receipts=tuple(results),
        total_tickers_evaluated=len({item.ticker for item in manifest.documents}),
        reason_codes=(reason,)
        + (
            ("source_observations_excluded",)
            if population_plan and population_plan.excluded_count
            else ()
        ),
        subject_identity=identity,
        source_population_plan=population_plan,
    )
