"""Immutable report bodies, manifests, and the compact Brief Library index."""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from report.legacy_body import (
    LegacyReaderBody,
    ReaderExtractionReceipt,
    extract_legacy_reader_body,
)
from report.render_clock import render_now

CoverageRole = Literal["portfolio", "evaluation", "unknown"]
ReaderMode = Literal["shared_body", "legacy_standalone"]


class ReportSectionRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    section_id: str
    label: str
    group_id: str


class ReportInteractionManifest(BaseModel):
    """The governed interaction boundary shared by both report wrappers."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal["report_interactions.v1"] = "report_interactions.v1"
    copilot_entry_point: Literal["/api/ask/stream"] = "/api/ask/stream"
    proposal_mode: Literal["explicit_approval"] = "explicit_approval"
    comments_mode: Literal["anchored_receipts"] = "anchored_receipts"


class RenderedReportBody(BaseModel):
    """One complete report body before standalone or Work OS chrome is added."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal["report_body.v1"] = "report_body.v1"
    artifact_id: str
    ticker: str
    report_date: date
    body_html: str
    body_sha256: str
    sections: tuple[ReportSectionRef, ...]
    interaction_manifest: ReportInteractionManifest

    @classmethod
    def from_html(
        cls,
        *,
        ticker: str,
        report_date: date,
        body_html: str,
        sections: tuple[ReportSectionRef, ...],
        interaction_manifest: ReportInteractionManifest,
    ) -> RenderedReportBody:
        normalized_ticker = ticker.strip().upper()
        body_sha256 = hashlib.sha256(body_html.encode("utf-8")).hexdigest()
        return cls(
            artifact_id=(
                f"report_{normalized_ticker}_{report_date.isoformat()}_{body_sha256[:20]}"
            ),
            ticker=normalized_ticker,
            report_date=report_date,
            body_html=body_html,
            body_sha256=body_sha256,
            sections=sections,
            interaction_manifest=interaction_manifest,
        )


def _relative_artifact_path(value: str) -> str:
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("artifact paths must remain relative to the repository")
    return path.as_posix()


class ReportArtifactRef(BaseModel):
    """Stable identity and locations for one persisted research brief."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal["report_artifact.v1"] = "report_artifact.v1"
    artifact_id: str
    ticker: str
    title: str
    artifact_kind: Literal["full_brief"] = "full_brief"
    coverage_role: CoverageRole
    report_date: date
    generated_at: datetime
    reader_mode: ReaderMode
    standalone_path: str
    body_path: str | None = None
    manifest_path: str
    workspace_sha256: str
    body_sha256: str | None = None
    section_ids: tuple[str, ...] = ()
    provenance_ref: int | None = None

    _standalone_relative = field_validator("standalone_path")(_relative_artifact_path)
    _manifest_relative = field_validator("manifest_path")(_relative_artifact_path)

    @field_validator("body_path")
    @classmethod
    def _body_relative(cls, value: str | None) -> str | None:
        return None if value is None else _relative_artifact_path(value)


class ReportArtifactIndex(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal["report_artifact_index.v1"] = "report_artifact_index.v1"
    generated_at: datetime
    items: tuple[ReportArtifactRef, ...] = ()


class ReconcileResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    added: int
    skipped: int


class LegacyBodyMigrationItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    artifact_id: str
    ticker: str
    status: Literal["eligible", "migrated", "rolled_back", "failed"]
    body_sha256: str | None = None
    warnings: tuple[str, ...] = ()
    error: str | None = None


class LegacyBodyMigrationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal["legacy_body_migration.v1"] = "legacy_body_migration.v1"
    apply: bool
    rollback: bool = False
    candidates: int
    eligible: int
    migrated: int
    rolled_back: int = 0
    failed: int
    skipped_shared: int
    items: tuple[LegacyBodyMigrationItem, ...]


def report_artifact_index_path(repo_root: Path) -> Path:
    return repo_root / "output" / "research" / "report_artifacts.v1.json"


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _repo_relative(repo_root: Path, path: Path) -> str:
    """Return a stable logical path while admitting the governed output junction."""

    if ".." in path.parts:
        raise ValueError("report artifacts must remain inside the repository")
    lexical_root = Path(os.path.abspath(repo_root))
    lexical_path = Path(os.path.abspath(path))
    try:
        relative = lexical_path.relative_to(lexical_root)
    except ValueError as exc:
        raise ValueError("report artifacts must remain inside the repository") from exc

    resolved_path = path.resolve()
    resolved_root = repo_root.resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as root_exc:
        if not relative.parts or relative.parts[0] != "output":
            raise ValueError("report artifacts must remain inside the repository") from root_exc
        resolved_output = (lexical_root / "output").resolve()
        try:
            resolved_path.relative_to(resolved_output)
        except ValueError as exc:
            raise ValueError("report artifacts must remain inside the repository") from exc
    return relative.as_posix()


def _read_index(repo_root: Path) -> ReportArtifactIndex:
    path = report_artifact_index_path(repo_root)
    if not path.exists():
        return ReportArtifactIndex(generated_at=render_now())
    return ReportArtifactIndex.model_validate_json(path.read_text(encoding="utf-8"))


def load_report_artifact_index(repo_root: Path) -> ReportArtifactIndex:
    """Read the compact index and exclude entries whose deliverable is gone."""

    index = _read_index(repo_root)
    existing = tuple(item for item in index.items if (repo_root / item.standalone_path).is_file())
    return ReportArtifactIndex(generated_at=index.generated_at, items=existing)


def _write_index(repo_root: Path, items: tuple[ReportArtifactRef, ...]) -> None:
    ordered = tuple(
        sorted(items, key=lambda item: (item.generated_at, item.artifact_id), reverse=True)
    )
    index = ReportArtifactIndex(generated_at=render_now(), items=ordered)
    _atomic_write_text(
        report_artifact_index_path(repo_root),
        index.model_dump_json(indent=2) + "\n",
    )


def _write_manifest(repo_root: Path, ref: ReportArtifactRef) -> None:
    _atomic_write_text(repo_root / ref.manifest_path, ref.model_dump_json(indent=2) + "\n")


def persist_report_artifact(
    *,
    repo_root: Path,
    body: RenderedReportBody,
    standalone_path: Path,
    generated_at: datetime,
    coverage_role: CoverageRole,
    title: str,
    provenance_ref: int | None = None,
) -> ReportArtifactRef:
    """Persist a shared body and atomically upsert its immutable index entry."""

    if not standalone_path.is_file():
        raise FileNotFoundError(standalone_path)
    reader_body = extract_legacy_reader_body(body.body_html, artifact_id=body.artifact_id)
    artifact_dir = repo_root / "output" / "research" / body.ticker / "artifacts" / body.artifact_id
    standalone_snapshot_path = artifact_dir / "standalone.html"
    body_path = artifact_dir / "body.html"
    manifest_path = artifact_dir / "manifest.json"
    if manifest_path.is_file():
        existing = ReportArtifactRef.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        existing_body = repo_root / (existing.body_path or "")
        existing_standalone = repo_root / existing.standalone_path
        if not existing_body.is_file() or not existing_standalone.is_file():
            raise FileNotFoundError("persisted report artifact is incomplete")
        if hashlib.sha256(existing_body.read_bytes()).hexdigest() != existing.body_sha256:
            raise ValueError("persisted report body checksum mismatch")
        if (
            hashlib.sha256(existing_standalone.read_bytes()).hexdigest()
            != existing.workspace_sha256
        ):
            raise ValueError("persisted standalone report checksum mismatch")
        prior = _read_index(repo_root)
        retained = tuple(item for item in prior.items if item.artifact_id != existing.artifact_id)
        _write_index(repo_root, (existing, *retained))
        return existing
    _atomic_write_text(
        standalone_snapshot_path,
        standalone_path.read_text(encoding="utf-8"),
    )
    _atomic_write_text(body_path, reader_body.body_html)
    ref = ReportArtifactRef(
        artifact_id=body.artifact_id,
        ticker=body.ticker,
        title=title,
        coverage_role=coverage_role,
        report_date=body.report_date,
        generated_at=generated_at,
        reader_mode="shared_body",
        standalone_path=_repo_relative(repo_root, standalone_snapshot_path),
        body_path=_repo_relative(repo_root, body_path),
        manifest_path=_repo_relative(repo_root, manifest_path),
        workspace_sha256=hashlib.sha256(standalone_snapshot_path.read_bytes()).hexdigest(),
        body_sha256=reader_body.body_sha256,
        section_ids=tuple(section.section_id for section in body.sections),
        provenance_ref=provenance_ref,
    )
    _write_manifest(repo_root, ref)
    prior = _read_index(repo_root)
    retained = tuple(item for item in prior.items if item.artifact_id != ref.artifact_id)
    _write_index(repo_root, (ref, *retained))
    return ref


def reconcile_legacy_workspace_reports(
    repo_root: Path,
    *,
    coverage_role_for: Callable[[str], CoverageRole] | None = None,
) -> ReconcileResult:
    """Index surviving legacy reports without regenerating their historical facts."""

    def unknown_role(_ticker: str) -> CoverageRole:
        return "unknown"

    role_for: Callable[[str], CoverageRole] = coverage_role_for or unknown_role
    prior = _read_index(repo_root)
    items = list(prior.items)
    known_paths = {item.standalone_path for item in items}
    added = 0
    skipped = 0
    research_root = repo_root / "output" / "research"
    for workspace_path in sorted(research_root.glob("*/*_workspace.html")):
        relative_workspace = _repo_relative(repo_root, workspace_path)
        if relative_workspace in known_paths:
            skipped += 1
            continue
        report_date_text = workspace_path.name.removesuffix("_workspace.html")
        try:
            report_date = date.fromisoformat(report_date_text)
        except ValueError:
            skipped += 1
            continue
        ticker = workspace_path.parent.name.upper()
        workspace_sha256 = hashlib.sha256(workspace_path.read_bytes()).hexdigest()
        artifact_id = f"report_{ticker}_{report_date.isoformat()}_{workspace_sha256[:20]}"
        manifest_path = workspace_path.with_name(f"{report_date.isoformat()}_manifest.json")
        ref = ReportArtifactRef(
            artifact_id=artifact_id,
            ticker=ticker,
            title=f"{ticker} Full Research Brief",
            coverage_role=role_for(ticker),
            report_date=report_date,
            generated_at=datetime.fromtimestamp(workspace_path.stat().st_mtime, tz=UTC),
            reader_mode="legacy_standalone",
            standalone_path=relative_workspace,
            manifest_path=_repo_relative(repo_root, manifest_path),
            workspace_sha256=workspace_sha256,
        )
        _write_manifest(repo_root, ref)
        items.append(ref)
        known_paths.add(relative_workspace)
        added += 1
    _write_index(repo_root, tuple(items))
    return ReconcileResult(added=added, skipped=skipped)


MigrationPlan = tuple[
    ReportArtifactRef,
    ReportArtifactRef,
    LegacyReaderBody,
    ReaderExtractionReceipt,
]


def _migration_artifact_paths(
    repo_root: Path, artifact: ReportArtifactRef
) -> tuple[Path, Path, Path]:
    artifact_dir = (
        repo_root / "output" / "research" / artifact.ticker / "artifacts" / artifact.artifact_id
    )
    return (
        artifact_dir / "body.html",
        artifact_dir / "reader_extraction.v1.json",
        artifact_dir / "reader_manifest.v1.json",
    )


def _activate_migration_plans(
    repo_root: Path,
    prior: ReportArtifactIndex,
    plans: list[MigrationPlan],
) -> None:
    """Stage and verify the complete batch before one atomic index activation."""

    staging_parent = repo_root / ".tmp"
    staging_parent.mkdir(parents=True, exist_ok=True)
    replacements = {migrated.artifact_id: migrated for _, migrated, _, _ in plans}
    updated = tuple(replacements.get(item.artifact_id, item) for item in prior.items)
    created: list[Path] = []
    with tempfile.TemporaryDirectory(
        prefix="report-body-migration-", dir=staging_parent
    ) as staging_name:
        staging_root = Path(staging_name)
        transfers: list[tuple[Path, Path, str]] = []
        for legacy, migrated, extracted, receipt in plans:
            stage_dir = (
                staging_root / hashlib.sha256(legacy.artifact_id.encode("utf-8")).hexdigest()
            )
            stage_body = stage_dir / "body.html"
            stage_receipt = stage_dir / "reader_extraction.v1.json"
            stage_manifest = stage_dir / "reader_manifest.v1.json"
            _atomic_write_text(stage_body, extracted.body_html)
            _atomic_write_text(stage_receipt, receipt.model_dump_json(indent=2) + "\n")
            _atomic_write_text(stage_manifest, migrated.model_dump_json(indent=2) + "\n")
            if hashlib.sha256(stage_body.read_bytes()).hexdigest() != extracted.body_sha256:
                raise ValueError("staged reader body checksum mismatch")
            ReaderExtractionReceipt.model_validate_json(stage_receipt.read_text(encoding="utf-8"))
            ReportArtifactRef.model_validate_json(stage_manifest.read_text(encoding="utf-8"))
            body_path, receipt_path, manifest_path = _migration_artifact_paths(repo_root, legacy)
            transfers.extend(
                (
                    (stage_body, body_path, extracted.body_sha256),
                    (
                        stage_receipt,
                        receipt_path,
                        hashlib.sha256(stage_receipt.read_bytes()).hexdigest(),
                    ),
                    (
                        stage_manifest,
                        manifest_path,
                        hashlib.sha256(stage_manifest.read_bytes()).hexdigest(),
                    ),
                )
            )
        for _staged, final, expected_sha256 in transfers:
            if final.exists() and hashlib.sha256(final.read_bytes()).hexdigest() != expected_sha256:
                raise ValueError(f"derived migration target already differs: {final.name}")
        try:
            for staged, final, expected_sha256 in transfers:
                final.parent.mkdir(parents=True, exist_ok=True)
                if not final.exists():
                    staged.replace(final)
                    created.append(final)
                if hashlib.sha256(final.read_bytes()).hexdigest() != expected_sha256:
                    raise ValueError(f"derived migration target checksum mismatch: {final.name}")
            _write_index(repo_root, updated)
        except (OSError, ValueError):
            for path in reversed(created):
                path.unlink(missing_ok=True)
            raise


def migrate_legacy_report_bodies(
    repo_root: Path,
    *,
    tickers: set[str] | None = None,
    apply: bool = False,
) -> LegacyBodyMigrationResult:
    """Derive inert shared-reader bodies while keeping standalone artifacts immutable."""

    normalized_tickers = None if tickers is None else {value.strip().upper() for value in tickers}
    prior = _read_index(repo_root)
    selected = [
        artifact
        for artifact in prior.items
        if normalized_tickers is None or artifact.ticker in normalized_tickers
    ]
    candidates = [artifact for artifact in selected if artifact.reader_mode == "legacy_standalone"]
    skipped_shared = len(selected) - len(candidates)
    plans: list[MigrationPlan] = []
    results: list[LegacyBodyMigrationItem] = []
    failed = 0
    for artifact in candidates:
        try:
            source_path = repo_root / artifact.standalone_path
            if not source_path.is_file():
                raise FileNotFoundError(source_path)
            source_bytes = source_path.read_bytes()
            source_sha256 = hashlib.sha256(source_bytes).hexdigest()
            if source_sha256 != artifact.workspace_sha256:
                raise ValueError("standalone report checksum mismatch")
            extracted = extract_legacy_reader_body(
                source_bytes.decode("utf-8"), artifact_id=artifact.artifact_id
            )
            body_path, _receipt_path, manifest_path = _migration_artifact_paths(repo_root, artifact)
            body_relative = _repo_relative(repo_root, body_path)
            migrated_ref = artifact.model_copy(
                update={
                    "reader_mode": "shared_body",
                    "body_path": body_relative,
                    "body_sha256": extracted.body_sha256,
                    "manifest_path": _repo_relative(repo_root, manifest_path),
                    "section_ids": extracted.section_ids,
                }
            )
            receipt = ReaderExtractionReceipt(
                artifact_id=artifact.artifact_id,
                source_path=artifact.standalone_path,
                source_sha256=source_sha256,
                legacy_manifest_path=artifact.manifest_path,
                body_path=body_relative,
                body_sha256=extracted.body_sha256,
                text_sha256=extracted.text_sha256,
                section_ids=extracted.section_ids,
                id_map=extracted.id_map,
                source_metrics=extracted.source_metrics,
                preserved_metrics=extracted.preserved_metrics,
                heading_count=extracted.heading_count,
                table_count=extracted.table_count,
                link_count=extracted.link_count,
                source_link_count=extracted.source_link_count,
                warnings=extracted.warnings,
            )
            plans.append((artifact, migrated_ref, extracted, receipt))
            results.append(
                LegacyBodyMigrationItem(
                    artifact_id=artifact.artifact_id,
                    ticker=artifact.ticker,
                    status="eligible",
                    body_sha256=extracted.body_sha256,
                    warnings=extracted.warnings,
                )
            )
        except (OSError, UnicodeError, ValueError) as exc:
            failed += 1
            results.append(
                LegacyBodyMigrationItem(
                    artifact_id=artifact.artifact_id,
                    ticker=artifact.ticker,
                    status="failed",
                    error=str(exc),
                )
            )
    migrated = 0
    if apply and plans and not failed:
        try:
            _activate_migration_plans(repo_root, prior, plans)
        except (OSError, ValueError) as exc:
            failed = len(plans)
            results = [
                item.model_copy(update={"status": "failed", "error": str(exc)})
                if item.status == "eligible"
                else item
                for item in results
            ]
        else:
            migrated = len(plans)
            results = [
                item.model_copy(update={"status": "migrated"})
                if item.status == "eligible"
                else item
                for item in results
            ]
    return LegacyBodyMigrationResult(
        apply=apply,
        candidates=len(candidates),
        eligible=len(plans),
        migrated=migrated,
        failed=failed,
        skipped_shared=skipped_shared,
        items=tuple(results),
    )


def rollback_legacy_report_bodies(
    repo_root: Path,
    *,
    tickers: set[str] | None = None,
) -> LegacyBodyMigrationResult:
    """Atomically reactivate preserved legacy manifests, then remove derived bodies."""

    normalized_tickers = None if tickers is None else {value.strip().upper() for value in tickers}
    prior = _read_index(repo_root)
    candidates = [
        artifact
        for artifact in prior.items
        if artifact.reader_mode == "shared_body"
        and artifact.manifest_path.endswith("/reader_manifest.v1.json")
        and (normalized_tickers is None or artifact.ticker in normalized_tickers)
    ]
    replacements: dict[str, ReportArtifactRef] = {}
    cleanup_paths: dict[str, tuple[Path, Path, Path]] = {}
    results: list[LegacyBodyMigrationItem] = []
    failed = 0
    for artifact in candidates:
        try:
            if artifact.body_path is None:
                raise ValueError("migrated artifact has no body path")
            body_path = repo_root / artifact.body_path
            receipt_path = body_path.with_name("reader_extraction.v1.json")
            receipt = ReaderExtractionReceipt.model_validate_json(
                receipt_path.read_text(encoding="utf-8")
            )
            if receipt.artifact_id != artifact.artifact_id:
                raise ValueError("migration receipt artifact identity mismatch")
            legacy_manifest_path = repo_root / _relative_artifact_path(receipt.legacy_manifest_path)
            legacy_ref = ReportArtifactRef.model_validate_json(
                legacy_manifest_path.read_text(encoding="utf-8")
            )
            if (
                legacy_ref.artifact_id != artifact.artifact_id
                or legacy_ref.reader_mode != "legacy_standalone"
            ):
                raise ValueError("preserved legacy manifest is incompatible")
            source_path = repo_root / legacy_ref.standalone_path
            if hashlib.sha256(source_path.read_bytes()).hexdigest() != legacy_ref.workspace_sha256:
                raise ValueError("preserved standalone report checksum mismatch")
            replacements[artifact.artifact_id] = legacy_ref
            cleanup_paths[artifact.artifact_id] = (
                body_path,
                receipt_path,
                repo_root / artifact.manifest_path,
            )
            results.append(
                LegacyBodyMigrationItem(
                    artifact_id=artifact.artifact_id,
                    ticker=artifact.ticker,
                    status="eligible",
                    body_sha256=artifact.body_sha256,
                )
            )
        except (OSError, ValueError) as exc:
            failed += 1
            results.append(
                LegacyBodyMigrationItem(
                    artifact_id=artifact.artifact_id,
                    ticker=artifact.ticker,
                    status="failed",
                    error=str(exc),
                )
            )
    rolled_back = 0
    if replacements and not failed:
        updated = tuple(replacements.get(item.artifact_id, item) for item in prior.items)
        _write_index(repo_root, updated)
        rolled_back = len(replacements)
        for artifact_id, paths in cleanup_paths.items():
            cleanup_warning = False
            for path in paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    cleanup_warning = True
            results = [
                item.model_copy(
                    update={
                        "status": "rolled_back",
                        "warnings": (("derived_cleanup_incomplete",) if cleanup_warning else ()),
                    }
                )
                if item.artifact_id == artifact_id
                else item
                for item in results
            ]
    return LegacyBodyMigrationResult(
        apply=False,
        rollback=True,
        candidates=len(candidates),
        eligible=len(replacements),
        migrated=0,
        rolled_back=rolled_back,
        failed=failed,
        skipped_shared=len(prior.items) - len(candidates),
        items=tuple(results),
    )


__all__ = [
    "CoverageRole",
    "LegacyBodyMigrationItem",
    "LegacyBodyMigrationResult",
    "ReaderMode",
    "ReconcileResult",
    "RenderedReportBody",
    "ReportArtifactIndex",
    "ReportArtifactRef",
    "ReportInteractionManifest",
    "ReportSectionRef",
    "load_report_artifact_index",
    "migrate_legacy_report_bodies",
    "persist_report_artifact",
    "reconcile_legacy_workspace_reports",
    "report_artifact_index_path",
    "rollback_legacy_report_bodies",
]
