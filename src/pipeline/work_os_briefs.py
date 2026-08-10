"""Narrow Brief Library projection over the compact report artifact index."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from report.artifacts import CoverageRole, ReaderMode, ReportArtifactRef, load_report_artifact_index

BriefStatus = Literal["available", "degraded"]


class BriefLibraryItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    artifact_id: str
    ticker: str
    title: str
    artifact_kind: Literal["full_brief"]
    coverage_role: CoverageRole
    report_date: str
    generated_at: str
    reader_mode: ReaderMode
    status: BriefStatus
    body_url: str | None
    standalone_url: str
    section_count: int
    source_count: int | None = None
    comment_count: int | None = None


class BriefLibraryResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal["brief_library.v1"] = "brief_library.v1"
    inventory_revision: str
    items: tuple[BriefLibraryItem, ...]
    next_cursor: str | None


def _status(repo_root: Path, artifact: ReportArtifactRef) -> BriefStatus:
    if artifact.reader_mode == "legacy_standalone" or artifact.body_path is None:
        return "degraded"
    return "available" if (repo_root / artifact.body_path).is_file() else "degraded"


def _item(repo_root: Path, artifact: ReportArtifactRef) -> BriefLibraryItem:
    status = _status(repo_root, artifact)
    return BriefLibraryItem(
        artifact_id=artifact.artifact_id,
        ticker=artifact.ticker,
        title=artifact.title,
        artifact_kind=artifact.artifact_kind,
        coverage_role=artifact.coverage_role,
        report_date=artifact.report_date.isoformat(),
        generated_at=artifact.generated_at.isoformat().replace("+00:00", "Z"),
        reader_mode=artifact.reader_mode,
        status=status,
        body_url=(
            f"/api/work-os/briefs/{artifact.artifact_id}/body" if status == "available" else None
        ),
        standalone_url=(f"/reports/{artifact.ticker}?artifact_id={artifact.artifact_id}"),
        section_count=len(artifact.section_ids),
    )


def build_brief_library(
    repo_root: Path,
    *,
    ticker: str | None = None,
    coverage_role: CoverageRole | None = None,
    status: BriefStatus | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> BriefLibraryResponse:
    """Return one bounded page without scanning report directories."""

    index = load_report_artifact_index(repo_root)
    filtered = [
        artifact
        for artifact in index.items
        if (ticker is None or artifact.ticker == ticker.upper())
        and (coverage_role is None or artifact.coverage_role == coverage_role)
        and (status is None or _status(repo_root, artifact) == status)
    ]
    start = 0
    if cursor is not None:
        for position, artifact in enumerate(filtered):
            if artifact.artifact_id == cursor:
                start = position + 1
                break
        else:
            raise ValueError("unknown brief-library cursor")
    page = filtered[start : start + limit]
    has_more = start + limit < len(filtered)
    return BriefLibraryResponse(
        inventory_revision=index.generated_at.isoformat().replace("+00:00", "Z"),
        items=tuple(_item(repo_root, artifact) for artifact in page),
        next_cursor=page[-1].artifact_id if has_more and page else None,
    )


def resolve_report_artifact(repo_root: Path, artifact_id: str) -> ReportArtifactRef | None:
    return next(
        (
            artifact
            for artifact in load_report_artifact_index(repo_root).items
            if artifact.artifact_id == artifact_id
        ),
        None,
    )


__all__ = [
    "BriefLibraryItem",
    "BriefLibraryResponse",
    "BriefStatus",
    "build_brief_library",
    "resolve_report_artifact",
]
