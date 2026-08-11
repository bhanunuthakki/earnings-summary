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
    root = repo_root.resolve()
    resolved = path.resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("report artifacts must remain inside the repository") from exc


def _read_index(repo_root: Path) -> ReportArtifactIndex:
    path = report_artifact_index_path(repo_root)
    if not path.exists():
        return ReportArtifactIndex(generated_at=datetime.now(UTC))
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
    index = ReportArtifactIndex(generated_at=datetime.now(UTC), items=ordered)
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
    _atomic_write_text(body_path, body.body_html)
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
        body_sha256=body.body_sha256,
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


__all__ = [
    "CoverageRole",
    "ReaderMode",
    "ReconcileResult",
    "RenderedReportBody",
    "ReportArtifactIndex",
    "ReportArtifactRef",
    "ReportInteractionManifest",
    "ReportSectionRef",
    "load_report_artifact_index",
    "persist_report_artifact",
    "reconcile_legacy_workspace_reports",
    "report_artifact_index_path",
]
