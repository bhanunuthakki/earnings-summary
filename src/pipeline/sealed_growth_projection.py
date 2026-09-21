"""Source-bound, read-only growth projection for bounded regime rendering.

This consumes the migrated discovery financial slice. It never selects an
alternate source winner or claims to project unmigrated report consumers.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from db_paths import configured_db_path
from models.documents import DocType, SourceType
from provenance.immutable_artifact import read_stable_artifact, require_no_reparse_points
from provenance.source_regime import (
    AdmissionEvidence,
    SourceDomain,
    SourceRegime,
    contract_for,
    contract_sha256,
)
from report.offline_artifact import DependencyClass, DependencyRecord, OfflineBoundaryError
from sources.discovery_financials import GrowthFinancials, read_growth_financials
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

FIXED_COHORT = ("META", "NU", "BN", "RBRK", "FRVO", "WIX")


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def policy_bundle_sha256() -> str:
    return hashlib.sha256(
        canonical_bytes(
            {regime.value: contract_sha256(contract_for(regime)) for regime in SourceRegime}
        )
    ).hexdigest()


class SealedFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    path: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)


class SealedSourceFile(SealedFile):
    document_version_id: str = Field(min_length=1)


class GrowthRenderManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["canonical-growth-regime-input/v1"] = "canonical-growth-regime-input/v1"
    state_kind: Literal["sealed_disposable_snapshot"]
    database: SealedFile
    source_files: tuple[SealedSourceFile, ...]
    as_of: date
    policy_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cohort: tuple[str, ...] = FIXED_COHORT

    @model_validator(mode="after")
    def validate_scope(self) -> GrowthRenderManifest:
        if self.cohort != FIXED_COHORT:
            raise ValueError("render cohort must exactly match the approved six-ticker cohort")
        if len({item.document_version_id for item in self.source_files}) != len(self.source_files):
            raise ValueError("source document identities must be unique")
        if self.policy_bundle_sha256 != policy_bundle_sha256():
            raise ValueError("source-regime policy hash mismatch")
        if not self.database.path.is_absolute() or any(
            not item.path.is_absolute() for item in self.source_files
        ):
            raise ValueError("sealed inputs require explicit absolute paths")
        return self


class GrowthRegimeProjection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    ticker: str
    regime: SourceRegime
    as_of: date
    contract_sha256: str
    status: Literal["available", "unavailable", "degraded"]
    reason_codes: tuple[str, ...]
    calculation: GrowthFinancials
    source_admissions: tuple[AdmissionEvidence, ...] = ()
    supported_scope: Literal["canonical_discovery_growth_only"] = "canonical_discovery_growth_only"
    decision_grade: Literal[False] = False
    excluded_consumers: tuple[str, ...] = (
        "report_other_panels",
        "dcf",
        "valuation",
        "grading",
        "owner_state",
        "price",
        "discovery_other_screens",
    )


def verify_sealed_file(file: SealedFile) -> None:
    """Stream large snapshots and bind the hash to one stable regular file identity."""
    require_no_reparse_points(file.path)
    before = file.path.stat()
    if not file.path.is_file() or before.st_nlink != 1:
        raise OfflineBoundaryError("sealed input must be one regular single-link file")
    digest = hashlib.sha256()
    with file.path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    after = file.path.stat()

    def identity(value: os.stat_result) -> tuple[int, ...]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_nlink,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )

    if (
        identity(before) != identity(after)
        or before.st_size != file.size_bytes
        or digest.hexdigest() != file.sha256
    ):
        raise OfflineBoundaryError("sealed input changed or does not match its manifest")


def load_growth_manifest(
    path: Path, expected_sha256: str
) -> tuple[GrowthRenderManifest, DependencyRecord]:
    snapshot, payload = read_stable_artifact(path)
    if snapshot.file_sha256 != expected_sha256:
        raise OfflineBoundaryError("input manifest hash mismatch")
    manifest = GrowthRenderManifest.model_validate_json(payload)
    return manifest, DependencyRecord(
        logical_path="config/growth_regime_input.json",
        dependency_class=DependencyClass.CONFIG,
        sha256=snapshot.file_sha256,
        size_bytes=len(payload),
    )


def _clock(value: object) -> datetime:
    result = datetime.fromisoformat(str(value))
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)


def project_growth_regimes(manifest: GrowthRenderManifest) -> tuple[GrowthRegimeProjection, ...]:
    """Use only a verified closed snapshot and explicitly inventoried source bytes."""
    manifest = GrowthRenderManifest.model_validate_json(manifest.model_dump_json())
    database = manifest.database.path
    if database.resolve() == configured_db_path(Path(__file__).resolve().parents[2]):
        raise OfflineBoundaryError(
            "configured application database is not a disposable render snapshot"
        )
    if database.name.lower() == "portfolio.db" and database.parent.name.lower() == "data":
        raise OfflineBoundaryError("implicit checkout database is not a sealed snapshot")
    if any(Path(str(database) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise OfflineBoundaryError("sealed snapshot must have no SQLite sidecars")
    files: tuple[SealedFile, ...] = (manifest.database, *manifest.source_files)
    for item in files:
        verify_sealed_file(item)
    sources = {item.document_version_id: item for item in manifest.source_files}
    result: list[GrowthRegimeProjection] = []
    conn = connect_sqlite(database, role=SQLiteConnectionRole.QUIESCED_IMMUTABLE_READ_ONLY)
    try:
        if (
            conn.execute("PRAGMA quick_check").fetchone()[0] != "ok"
            or conn.execute("PRAGMA foreign_key_check").fetchone() is not None
        ):
            raise OfflineBoundaryError("sealed snapshot integrity or foreign-key check failed")
        for ticker in manifest.cohort:
            calculation = read_growth_financials(conn, ticker, as_of=manifest.as_of)
            admissions: list[AdmissionEvidence] = []
            problems: set[str] = set()
            for reference in calculation.references:
                row = conn.execute(
                    "SELECT observation.source_kind,version.document_type,observation.source_published_at,observation.retrieved_at,version.blob_sha256,version.ticker FROM evidence_document_versions version JOIN evidence_source_observations observation ON observation.observation_id=version.observation_id WHERE version.document_version_id=?",
                    (reference.document_version_id,),
                ).fetchone()
                source = sources.get(reference.document_version_id)
                if row is None or source is None or row[4] != source.sha256 or row[5] != ticker:
                    raise OfflineBoundaryError(
                        "canonical fact source is absent or mismatched in sealed input manifest"
                    )
                try:
                    admission = AdmissionEvidence(
                        source_type=SourceType(str(row[0])),
                        document_type=DocType(str(row[1])),
                        source_document_id=reference.document_version_id,
                        observation_or_projection_version=reference.observation_id,
                        currency=reference.currency,
                        fiscal_period=f"{reference.period_start.isoformat()}/{reference.period_end.isoformat()}",
                        published_at=_clock(row[2]) if row[2] is not None else None,
                        ingested_at=_clock(row[3]),
                        sealed_at=None,
                        transformation_lineage=None,
                    )
                except ValueError:
                    problems.add("source_admission_metadata_unavailable_or_invalid")
                else:
                    admissions.append(admission)
            cutoff = datetime.combine(manifest.as_of, datetime.max.time(), tzinfo=UTC)
            for regime in SourceRegime:
                reasons = set(calculation.reason_codes) | problems
                for admission in admissions:
                    try:
                        contract_for(regime).admits(
                            domain=SourceDomain.REPORTED_FACT, evidence=admission, cutoff=cutoff
                        )
                    except ValueError:
                        reasons.add("selected_canonical_source_not_admitted_by_regime")
                if len(admissions) != len(calculation.references):
                    reasons.add("source_admission_incomplete")
                available = calculation.status == "available" and not reasons
                result.append(
                    GrowthRegimeProjection(
                        ticker=ticker,
                        regime=regime,
                        as_of=manifest.as_of,
                        contract_sha256=contract_sha256(contract_for(regime)),
                        status="available" if available else "unavailable",
                        reason_codes=tuple(sorted(reasons)),
                        calculation=calculation,
                        source_admissions=tuple(admissions),
                    )
                )
    finally:
        conn.close()
    for item in files:
        verify_sealed_file(item)
    if any(Path(str(database) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise OfflineBoundaryError("sealed snapshot sidecars appeared during projection")
    return tuple(result)
