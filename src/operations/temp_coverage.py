"""Read-only inventory of temporary growth, including paths held by cleanup."""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, Field

from src.operations.artifact_retention import RetirementDecision


class CoverageGroup(BaseModel):
    root: str
    scope: str
    disposition: str
    age_bucket: Literal["0-14d", "15-30d", "31-60d", "61d+"]
    files: int = Field(default=0, ge=0)
    bytes: int = Field(default=0, ge=0)


class TempCoverage(BaseModel):
    status: Literal["complete", "incomplete"] = "complete"
    byte_basis: Literal["logical_file_size"] = "logical_file_size"
    age_basis: Literal["last_file_modification"] = "last_file_modification"
    files: int = Field(default=0, ge=0)
    bytes: int = Field(default=0, ge=0)
    unreadable: int = Field(default=0, ge=0)
    unsafe_paths: int = Field(default=0, ge=0)
    groups: list[CoverageGroup] = Field(default_factory=lambda: list[CoverageGroup]())


def _linked(path: Path) -> bool:
    metadata = path.lstat()
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & 0x400
    )


def inventory_temp_coverage(
    roots: list[tuple[Path, str]],
    *,
    now: datetime,
    decisions: list[RetirementDecision],
    run_statuses: dict[Path, str],
) -> TempCoverage:
    """Count every file, without reading its contents or treating age as permission.

    ``recovery`` roots are inventory only. Manifest and checkpoint states explain
    holds; only the retention engine owns deletion eligibility.
    """
    result = TempCoverage()
    groups: dict[tuple[str, str, str, str], CoverageGroup] = {}
    by_path = {os.path.normcase(item.path): item.reason for item in decisions}
    seen_roots: list[Path] = []
    for root, purpose in roots:
        if root in seen_roots or any(parent in root.parents for parent in seen_roots):
            continue
        seen_roots.append(root)
        if not root.exists() and not root.is_symlink():
            continue
        try:
            if any(_linked(parent) for parent in (root, *root.parents)):
                result.unsafe_paths += 1
                continue
        except OSError:
            result.unreadable += 1
            continue
        # No excluded directory is invisible here: environments and registered
        # scopes contribute bytes even when the deletion pass preserves them.
        stack: list[tuple[Path, str | None]] = [(root, None)]
        while stack:
            current, checkpoint = stack.pop()
            state = current / "state.json"
            try:
                if state.is_file() and not _linked(state):
                    if state.stat().st_size <= 65536:
                        raw: object = json.loads(state.read_text(encoding="utf-8"))
                        status = (
                            cast("dict[str, object]", raw).get("status")
                            if isinstance(raw, dict)
                            else None
                        )
                        if isinstance(status, str):
                            normalized = status.strip().casefold()
                            new_checkpoint = (
                                "failed"
                                if normalized in {"failed", "error", "cancelled", "aborted"}
                                else "unclassified"
                                if normalized
                                in {"complete", "completed", "done", "success", "succeeded"}
                                else "active"
                            )
                            if checkpoint not in {"active", "failed"}:
                                checkpoint = new_checkpoint
                        else:
                            checkpoint = "unclassified"
                    else:
                        checkpoint = "unclassified"
            except (OSError, UnicodeError, ValueError):
                result.unreadable += 1
                checkpoint = "unclassified"
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        path = Path(entry.path)
                        try:
                            metadata = entry.stat(follow_symlinks=False)
                            if (
                                stat.S_ISLNK(metadata.st_mode)
                                or getattr(metadata, "st_file_attributes", 0) & 0x400
                            ):
                                result.unsafe_paths += 1
                                continue
                            if stat.S_ISDIR(metadata.st_mode):
                                stack.append((path, checkpoint))
                                continue
                            if not stat.S_ISREG(metadata.st_mode):
                                continue
                            relative = path.relative_to(root)
                            scope = relative.parts[0] if len(relative.parts) > 1 else "(root files)"
                            reason = by_path.get(os.path.normcase(str(path)))
                            disposition = reason or checkpoint or purpose
                            for run_root, run_status in run_statuses.items():
                                if run_root in path.parents:
                                    # Undeclared files added after sealing remain
                                    # visible as unknown, not implicitly disposable.
                                    disposition = reason or (
                                        run_status
                                        if run_status in {"active", "failed"}
                                        else "unclassified"
                                    )
                                    if path.name == ".earnings-temp-run.json":
                                        disposition = "lifecycle_receipt"
                                    break
                            days = (now.timestamp() - metadata.st_mtime) / 86400
                            bucket: Literal["0-14d", "15-30d", "31-60d", "61d+"] = (
                                "0-14d"
                                if days <= 14
                                else "15-30d"
                                if days <= 30
                                else "31-60d"
                                if days <= 60
                                else "61d+"
                            )
                            key = (str(root), scope, disposition, bucket)
                            group = groups.setdefault(
                                key,
                                CoverageGroup(
                                    root=str(root),
                                    scope=scope,
                                    disposition=disposition,
                                    age_bucket=bucket,
                                ),
                            )
                            group.files += 1
                            group.bytes += metadata.st_size
                            result.files += 1
                            result.bytes += metadata.st_size
                        except OSError:
                            result.unreadable += 1
            except OSError:
                result.unreadable += 1
    result.groups = sorted(groups.values(), key=lambda item: (-item.bytes, item.root, item.scope))
    if (
        result.unreadable
        or result.unsafe_paths
        or any(group.disposition == "unclassified" for group in result.groups)
    ):
        result.status = "incomplete"
    return result
