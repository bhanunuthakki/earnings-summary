"""Deterministic sealed rendering of the migrated canonical growth slice.

Unmigrated consumers and regime-specific alternate resolution remain unavailable.
Full report acceptance remains HOLD even when the supported slice is reproducible.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from db_paths import configured_db_path
from evals.regime_backtest import SourceRegime, StratumCohort
from models.documents import DocType, SourceType
from provenance.immutable_artifact import read_stable_artifact, require_no_reparse_points
from provenance.source_regime import AdmissionEvidence, SourceDomain, contract_for, contract_sha256
from provenance.source_regime import SourceRegime as CanonicalSourceRegime
from report.offline_artifact import (
    DependencyClass,
    DependencyRecord,
    OfflineArtifactPayload,
    OfflineBoundaryError,
    offline_runtime_guard,
    write_offline_artifact,
)
from report.renderers.offline_document import render_offline_document
from sources.discovery_financials import GrowthFinancials, read_growth_financials
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from ui.controls import prov_drawer

# ---------------------------------------------------------------------------
# Source-bound, read-only growth projection for bounded regime rendering
# (folded in from the former ``pipeline.sealed_growth_projection``).
#
# This consumes the migrated discovery financial slice. It never selects an
# alternate source winner or claims to project unmigrated report consumers.
# NOTE: the provenance ``SourceRegime`` is referred to as
# ``CanonicalSourceRegime`` here — ``SourceRegime`` in this module is the
# renderer's own backtest enum (same collision the split avoided by living in
# a separate module).
# ---------------------------------------------------------------------------

FIXED_COHORT = ("META", "NU", "BN", "RBRK", "FRVO", "WIX")


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def policy_bundle_sha256() -> str:
    return hashlib.sha256(
        canonical_bytes(
            {
                regime.value: contract_sha256(contract_for(regime))
                for regime in CanonicalSourceRegime
            }
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
    regime: CanonicalSourceRegime
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
            for regime in CanonicalSourceRegime:
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


class SectionRenderStatus(StrEnum):
    """Status of an individual rendered section."""

    COMPLETE = "COMPLETE"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"


class RenderedSectionPayload(BaseModel):
    """Immutable rendered section containing content and provenance metadata."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    section_id: str
    section_name: str
    regime: SourceRegime
    status: SectionRenderStatus
    source_lineage: str
    currency: str | None
    fiscal_period: str
    metrics: dict[str, Decimal] = Field(default_factory=dict)
    content_html: str
    content_markdown: str


class SingleRegimeRenderOutput(BaseModel):
    """Immutable output of a single regime render pass for a ticker."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    regime: SourceRegime
    stratum: StratumCohort | None
    as_of_date: date
    currency: str | None
    html_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    markdown_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    sections_json_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    sections_count: int
    two_pass_byte_identical: bool = False
    sections: tuple[RenderedSectionPayload, ...] = ()
    scope: str = "unbound"
    reason_codes: tuple[str, ...] = ()


class ThreeRegimeRenderReceipt(BaseModel):
    """Immutable receipt of deterministic three-regime rendering."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    as_of_date: date
    total_tickers: int
    total_regimes: int
    total_render_outputs: int
    all_two_pass_verified: bool
    status: Literal["PASS", "HOLD", "BLOCK"]
    render_outputs: tuple[SingleRegimeRenderOutput, ...] = ()
    verified_at: datetime | None
    reason_codes: tuple[str, ...] = ()
    input_manifest_sha256: str | None = None
    policy_bundle_sha256: str | None = None
    scope: str = "unbound"


class ThreeRegimeDeterministicRenderer:
    """Render exact admitted growth inputs without claiming full-report completion."""

    def __init__(
        self,
        output_base_dir: Path | None = None,
        *,
        input_manifest: Path | None = None,
        expected_manifest_sha256: str | None = None,
    ) -> None:
        self.output_base_dir = output_base_dir or Path(".tmp/three_regime_renders")
        self.input_manifest = input_manifest
        self.expected_manifest_sha256 = expected_manifest_sha256

    def render_ticker_regime(
        self,
        ticker: str,
        regime: SourceRegime,
        as_of_date: date = date(2026, 4, 30),
    ) -> SingleRegimeRenderOutput:
        """Reject unbound requests rather than invent source lineage and values."""
        raise ValueError(
            "Single render unavailable: use the fixed-cohort route with sealed source inputs "
            "and a verified manifest hash."
        )

    def render_all_regimes_for_cohort(
        self,
        tickers: list[str],
        as_of_date: date = date(2026, 4, 30),
    ) -> ThreeRegimeRenderReceipt:
        """Render the supported sealed slice, or hold an unbound request."""
        if self.input_manifest is not None:
            if self.expected_manifest_sha256 is None:
                raise OfflineBoundaryError("expected manifest hash is required")
            manifest, dependency = load_growth_manifest(
                self.input_manifest, self.expected_manifest_sha256
            )
            if tuple(tickers) != FIXED_COHORT or as_of_date != manifest.as_of:
                raise OfflineBoundaryError("requested cohort/as-of differs from sealed inputs")
            return self._render_bound(manifest, dependency)
        return ThreeRegimeRenderReceipt(
            run_id=f"render_3reg_{uuid4().hex}",
            as_of_date=as_of_date,
            total_tickers=len(tickers),
            total_regimes=0,
            total_render_outputs=0,
            all_two_pass_verified=False,
            status="HOLD",
            render_outputs=(),
            reason_codes=("sealed_regime_rendering_not_implemented",),
            verified_at=datetime.now(UTC),
        )

    def _render_bound(
        self, manifest: GrowthRenderManifest, input_dependency: DependencyRecord
    ) -> ThreeRegimeRenderReceipt:
        output_root = self.output_base_dir.resolve()
        protected = (
            manifest.database.path,
            *(item.path for item in manifest.source_files),
            self.input_manifest,
        )
        for path in protected:
            if path is not None and (
                path.resolve() == output_root or output_root in path.resolve().parents
            ):
                raise OfflineBoundaryError("output root overlaps sealed inputs")
        source_root = Path(__file__).resolve().parents[1]
        code_dependencies = tuple(
            DependencyRecord(
                logical_path="src/" + path.relative_to(source_root).as_posix(),
                dependency_class=DependencyClass.CODE,
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                size_bytes=path.stat().st_size,
            )
            for path in sorted(source_root.rglob("*.py"))
        )
        dependencies = (
            *code_dependencies,
            input_dependency,
            DependencyRecord(
                logical_path="data/sealed_growth_snapshot.db",
                dependency_class=DependencyClass.DATABASE_SNAPSHOT,
                sha256=manifest.database.sha256,
                size_bytes=manifest.database.size_bytes,
            ),
            *(
                DependencyRecord(
                    logical_path=f"sources/{index}",
                    dependency_class=DependencyClass.FILESYSTEM,
                    sha256=item.sha256,
                    size_bytes=item.size_bytes,
                )
                for index, item in enumerate(manifest.source_files)
            ),
        )
        # Both passes reconstruct from the verified input graph, not a copied first output.
        with offline_runtime_guard(output_root) as guard:
            first = tuple(_render_growth(item) for item in project_growth_regimes(manifest))
            second = tuple(_render_growth(item) for item in project_growth_regimes(manifest))
            if first != second:
                raise OfflineBoundaryError("two-pass canonical growth projections differ")
            outputs: list[SingleRegimeRenderOutput] = []
            for output, payload in first:
                receipt = write_offline_artifact(
                    output_dir=output_root / output.regime.value / output.ticker,
                    ticker=output.ticker,
                    as_of=manifest.as_of,
                    payload=payload,
                    dependencies=dependencies,
                )
                outputs.append(
                    output.model_copy(
                        update={
                            "html_sha256": receipt.output_sha256["report.html"],
                            "markdown_sha256": receipt.output_sha256["report.md"],
                            "sections_json_sha256": receipt.output_sha256["sections.json"],
                            "two_pass_byte_identical": True,
                        }
                    )
                )
        if any(
            (
                guard.network_attempts,
                guard.subprocess_attempts,
                guard.llm_attempts,
                guard.denied_writes,
            )
        ):
            raise OfflineBoundaryError("offline runtime observed a forbidden capability attempt")
        return ThreeRegimeRenderReceipt(
            run_id=f"growth_3reg_{input_dependency.sha256}",
            as_of_date=manifest.as_of,
            total_tickers=len(manifest.cohort),
            total_regimes=3,
            total_render_outputs=len(outputs),
            all_two_pass_verified=True,
            status="HOLD",
            render_outputs=tuple(outputs),
            verified_at=None,
            reason_codes=(
                "remaining_consumers_not_migrated",
                "regime_specific_reresolution_not_implemented",
                "full_bha30_appcontainer_attestation_not_executed",
                "source_acquisition_completeness_unverified",
            ),
            input_manifest_sha256=input_dependency.sha256,
            policy_bundle_sha256=manifest.policy_bundle_sha256,
            scope="canonical_discovery_growth_only",
        )


_REGIMES = {
    CanonicalSourceRegime.OFFICIAL_PRIMARY: SourceRegime.REGIME_1_SEC_IR_PRIMARY,
    CanonicalSourceRegime.NORMALIZED_VENDOR_ONLY: SourceRegime.REGIME_0_VENDOR_ONLY,
    CanonicalSourceRegime.COMBINED: SourceRegime.REGIME_2_COMBINED,
}


def _render_growth(
    projection: GrowthRegimeProjection,
) -> tuple[SingleRegimeRenderOutput, OfflineArtifactPayload]:
    """Pure rendering of already admitted values; no repository or database reads."""
    regime = _REGIMES[projection.regime]
    available = projection.status == "available"
    calculation = projection.calculation
    currency = calculation.references[0].currency if available else None
    period = (
        calculation.latest_period_end.isoformat()
        if available and calculation.latest_period_end
        else "unavailable"
    )
    values = {
        name: value
        for name, value in (
            ("revenue_yoy", calculation.revenue_yoy),
            ("revenue_yoy_prior", calculation.revenue_yoy_prior),
            ("gross_margin_ttm", calculation.gross_margin_ttm),
        )
        if available and value is not None
    }
    reasons = projection.reason_codes
    lineage = (
        "; ".join(
            sorted(
                {
                    f"{item.source_type.value}:{item.source_document_id}"
                    for item in projection.source_admissions
                }
            )
        )
        if available
        else "unavailable"
    )
    metadata: dict[str, object] = {
        "regime": projection.regime.value,
        "policy_sha256": projection.contract_sha256,
        "as_of": projection.as_of.isoformat(),
        "status": projection.status,
        "currency": currency,
        "fiscal_period": period,
        "source_lineage": lineage,
        "reason_codes": list(reasons),
        "decision_grade": False,
        "supported_scope": projection.supported_scope,
    }
    labels = {
        "revenue_yoy": "Revenue growth · latest quarter",
        "revenue_yoy_prior": "Revenue growth · prior-year quarter",
        "gross_margin_ttm": "Gross margin · trailing four quarters",
    }
    state = "Available projection · not decision-grade" if available else "Projection unavailable"
    body = (
        '<main class="l1-root"><div class="l1-tabs-wrap"><div class="tab-body">'
        '<header class="k-card-heading"><h1 class="k-section-title">'
        + html.escape(projection.ticker)
        + ' — canonical growth projection</h1><p class="k-note">'
        + html.escape(state)
        + " · As of "
        + projection.as_of.isoformat()
        + '</p><p class="k-note">Scope: migrated discovery growth calculation. '
        + "Other report, valuation and DCF content is unavailable.</p></header>"
    )
    if available:
        body += (
            '<section class="panel"><div class="table-scroll"><table class="tbl">'
            '<thead><tr><th scope="col">Metric</th><th class="num" scope="col">Value</th></tr></thead><tbody>'
            + "".join(
                f'<tr><td>{html.escape(labels[key])}</td><td class="num">{value * 100:.2f}%</td></tr>'
                for key, value in values.items()
            )
            + "</tbody></table></div></section>"
        )
    else:
        body += '<p class="k-note">' + html.escape("; ".join(reasons)) + "</p>"
    body += prov_drawer(
        "Source and policy evidence",
        '<div class="table-scroll" tabindex="0" role="region" aria-label="Source and policy evidence table"><table class="tbl"><tbody>'
        + "".join(
            f'<tr><th scope="row">{html.escape(key.replace("_", " "))}</th>'
            f"<td>{html.escape(str(value))}</td></tr>"
            for key, value in metadata.items()
        )
        + "</tbody></table></div>",
    )
    body += "</div></div></main>"
    markdown = (
        f"# {projection.ticker} — canonical growth projection\n\n"
        + "\n".join(f"- {key}: {value}" for key, value in metadata.items())
        + "\n\n"
        + "\n".join(f"- {key}: {value}" for key, value in values.items())
        + "\n"
    )
    section = RenderedSectionPayload(
        section_id="canonical_growth",
        section_name="Canonical discovery growth",
        regime=regime,
        status=SectionRenderStatus.DEGRADED if available else SectionRenderStatus.UNAVAILABLE,
        source_lineage=lineage,
        currency=currency,
        fiscal_period=period,
        metrics=values,
        content_html=body,
        content_markdown=markdown,
    )
    sections: dict[str, object] = {
        "ticker": projection.ticker,
        "metadata": metadata,
        "sections": [section.model_dump(mode="json")],
        "unavailable_consumers": list(projection.excluded_consumers),
    }
    payload = OfflineArtifactPayload(
        html=render_offline_document(body, title="Canonical growth projection"),
        markdown=markdown,
        sections=sections,
        status={
            "scope": projection.supported_scope,
            "decision_grade": False,
            "status": projection.status,
            "reason_codes": list(reasons),
        },
        numeric_provenance={
            "metadata": metadata,
            "calculation": calculation.model_dump(mode="json") if available else None,
            "source_admissions": [
                item.model_dump(mode="json") for item in projection.source_admissions
            ]
            if available
            else [],
        },
    )
    digest = hashlib.sha256(canonical_bytes(sections)).hexdigest()
    return SingleRegimeRenderOutput(
        ticker=projection.ticker,
        regime=regime,
        stratum=None,
        as_of_date=projection.as_of,
        currency=currency,
        html_sha256=hashlib.sha256(payload.html.encode()).hexdigest(),
        markdown_sha256=hashlib.sha256(payload.markdown.encode()).hexdigest(),
        sections_json_sha256=digest,
        sections_count=1,
        sections=(section,),
        scope=projection.supported_scope,
        reason_codes=reasons,
    ), payload
