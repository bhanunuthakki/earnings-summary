"""Passive evidence for the native Windows scratch-backup producer.

The observer reads only the producer's bounded latest-success log and filesystem
metadata for the archive named by that log. It does not open or hash the archive,
inspect a database, run a backup, or claim that a restore has been verified.
"""

from __future__ import annotations

import os
import re
import stat
from datetime import UTC, date, datetime, timedelta
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

LOG_LIMIT = 128 * 1024
FUTURE_TOLERANCE = timedelta(minutes=5)
ARCHIVE_NAME = re.compile(r"^scratch_(\d{4}-\d{2}-\d{2})\.(tar\.zst|tar\.gz)$")
LINE_TIME = r"\d{2}:\d{2}:\d{2}  "
LIVE_START = re.compile(rf"(?m)^{LINE_TIME}=== backup_scratch \(LIVE\) ===\r?$")
PUBLISHED = re.compile(rf"(?m)^{LINE_TIME}published: (?P<path>[^\r\n]+)\r?$")
UPLOAD_START = re.compile(rf"(?m)^{LINE_TIME}--- headless Drive API upload ---\r?$")
SUMMARY = re.compile(
    rf"(?m)^{LINE_TIME}SUMMARY \(LIVE\): files=\d+, dbs=\d+, "
    r"secrets-excluded=\d+, archive=.+, ratio=.+, duration=.+min\r?$"
)
DONE = re.compile(rf"(?m)^{LINE_TIME}=== done ===\r?$")
FATAL = re.compile(rf"(?m)^{LINE_TIME}FATAL:")

BackupFinding = Literal[
    "backup_evidence_unavailable",
    "backup_evidence_invalid",
    "backup_completion_stale",
]


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _pure_local_path(value: str) -> PureWindowsPath | PurePosixPath:
    if not value or len(value) > 1024 or "\x00" in value or value.startswith(("\\\\", "//")):
        raise ValueError("backup source must be an absolute local path")
    if re.match(r"^[A-Za-z]:[\\/]", value):
        path: PureWindowsPath | PurePosixPath = PureWindowsPath(value)
        if not path.is_absolute() or re.fullmatch(r"[A-Za-z]:", path.drive) is None:
            raise ValueError("backup source must be an absolute local path")
    else:
        path = PurePosixPath(value)
        if not path.is_absolute():
            raise ValueError("backup source must be an absolute local path")
    if ".." in path.parts:
        raise ValueError("backup source cannot traverse parents")
    return path


def _path_identity(value: str) -> tuple[str, ...]:
    path = _pure_local_path(value)
    if isinstance(path, PureWindowsPath):
        return ("windows", *(part.casefold() for part in path.parts))
    return ("posix", *path.parts)


class BackupConfig(Frozen):
    completion_log_path: str
    archive_directory: str
    max_age_seconds: int = Field(strict=True, ge=86400, le=31 * 86400)
    minimum_archive_size_bytes: int = Field(strict=True, ge=1, le=2**63 - 1)

    @field_validator("completion_log_path", "archive_directory")
    @classmethod
    def absolute_local_path(cls, value: str) -> str:
        _pure_local_path(value)
        return value

    @model_validator(mode="after")
    def distinct_sources(self) -> BackupConfig:
        if _path_identity(self.completion_log_path) == _path_identity(self.archive_directory):
            raise ValueError("backup log and archive directory must be distinct")
        return self


class BackupObservation(Frozen):
    schema_version: Literal["backup_observation.v1"] = "backup_observation.v1"
    observed_at: datetime
    state: Literal["healthy", "unavailable", "invalid", "stale"]
    completed_at: datetime | None = None
    archive_name: str | None = Field(default=None, pattern=ARCHIVE_NAME.pattern)
    archive_modified_at: datetime | None = None
    archive_size_bytes: int | None = Field(default=None, strict=True, ge=1)
    restore_verification: Literal["not_observed"] = "not_observed"
    findings: tuple[BackupFinding, ...] = Field(default=(), max_length=1)

    @field_validator("observed_at", "completed_at", "archive_modified_at")
    @classmethod
    def aware_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return value

    @model_validator(mode="after")
    def coherent(self) -> BackupObservation:
        evidence = (
            self.completed_at,
            self.archive_name,
            self.archive_modified_at,
            self.archive_size_bytes,
        )
        expected: dict[str, tuple[BackupFinding, ...]] = {
            "healthy": (),
            "unavailable": ("backup_evidence_unavailable",),
            "invalid": ("backup_evidence_invalid",),
            "stale": ("backup_completion_stale",),
        }
        if self.findings != expected[self.state]:
            raise ValueError("backup state and findings disagree")
        if self.state in {"healthy", "stale"} and any(value is None for value in evidence):
            raise ValueError("valid backup evidence requires complete archive metadata")
        if self.state in {"unavailable", "invalid"} and any(
            value is not None for value in evidence
        ):
            raise ValueError("failed backup evidence cannot expose partial source metadata")
        return self


def _same_direct_path(path: Path) -> bool:
    return os.path.normcase(os.path.normpath(str(path.resolve(strict=True)))) == os.path.normcase(
        os.path.normpath(str(path.absolute()))
    )


def _direct_file_stat(path: Path) -> os.stat_result:
    details = path.lstat()
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise ValueError("backup source is not a direct regular file")
    if not _same_direct_path(path):
        raise ValueError("backup source is indirect")
    return details


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _bounded_read(path: Path) -> tuple[bytes, os.stat_result]:
    details = _direct_file_stat(path)
    if details.st_size > LOG_LIMIT:
        raise ValueError("backup log is oversized")
    with path.open("rb") as stream:
        data = stream.read(LOG_LIMIT + 1)
        opened = os.fstat(stream.fileno())
    if len(data) > LOG_LIMIT:
        raise ValueError("backup log is oversized")
    after = _direct_file_stat(path)
    if _stat_identity(details) != _stat_identity(opened) or _stat_identity(
        opened
    ) != _stat_identity(after):
        raise ValueError("backup log changed while observed")
    return data, opened


def _single(pattern: re.Pattern[str], text: str) -> re.Match[str]:
    matches = tuple(pattern.finditer(text))
    if len(matches) != 1:
        raise ValueError("backup completion marker missing or duplicated")
    return matches[0]


def _archive_from_log(config: BackupConfig, text: str) -> tuple[str, Path]:
    start = _single(LIVE_START, text)
    published = _single(PUBLISHED, text)
    upload = _single(UPLOAD_START, text)
    summary = _single(SUMMARY, text)
    done = _single(DONE, text)
    if FATAL.search(text) or not (
        start.start() < published.start() < upload.start() < summary.start() < done.start()
    ):
        raise ValueError("backup completion sequence is invalid")
    if text[done.end() :].strip():
        raise ValueError("backup completion marker is not final")

    published_path = published.group("path")
    pure = _pure_local_path(published_path)
    name = pure.name
    if ARCHIVE_NAME.fullmatch(name) is None:
        raise ValueError("backup archive name is invalid")
    if _path_identity(str(pure.parent)) != _path_identity(config.archive_directory):
        raise ValueError("published archive is outside configured directory")
    return name, Path(config.archive_directory) / name


def _archive_date(name: str) -> date:
    match = ARCHIVE_NAME.fullmatch(name)
    if match is None:
        raise ValueError("backup archive name is invalid")
    return date.fromisoformat(match.group(1))


def _failed(now: datetime, state: Literal["unavailable", "invalid"]) -> BackupObservation:
    finding: BackupFinding = (
        "backup_evidence_unavailable" if state == "unavailable" else "backup_evidence_invalid"
    )
    return BackupObservation(observed_at=now, state=state, findings=(finding,))


def observe_backup(config: BackupConfig, now: datetime) -> BackupObservation:
    """Observe the latest completed backup without causing backup or restore side effects."""

    if now.tzinfo is None:
        raise ValueError("observation timestamp must be timezone-aware")
    try:
        log_path = Path(config.completion_log_path)
        raw_log, log_details = _bounded_read(log_path)
        text = raw_log.decode("utf-8-sig", errors="strict")
        archive_name, archive_path = _archive_from_log(config, text)
        archive_details = _direct_file_stat(archive_path)
        if archive_details.st_size < config.minimum_archive_size_bytes:
            raise ValueError("backup archive is below its configured size floor")

        completed_at = datetime.fromtimestamp(log_details.st_mtime, UTC)
        archive_modified_at = datetime.fromtimestamp(archive_details.st_mtime, UTC)
        archive_date = _archive_date(archive_name)
        if completed_at > now + FUTURE_TOLERANCE:
            raise ValueError("backup completion timestamp is in the future")
        if archive_modified_at > completed_at + FUTURE_TOLERANCE:
            raise ValueError("archive timestamp follows backup completion")
        if archive_date > now.date():
            raise ValueError("backup archive date is in the future")
        max_age = timedelta(seconds=config.max_age_seconds)
        if now - completed_at > max_age or now - archive_modified_at > max_age:
            return BackupObservation(
                observed_at=now,
                state="stale",
                completed_at=completed_at,
                archive_name=archive_name,
                archive_modified_at=archive_modified_at,
                archive_size_bytes=archive_details.st_size,
                findings=("backup_completion_stale",),
            )
        if any(
            abs((archive_date - timestamp.date()).days) > 1
            for timestamp in (archive_modified_at, completed_at)
        ):
            raise ValueError("backup archive date is implausible")
        return BackupObservation(
            observed_at=now,
            state="healthy",
            completed_at=completed_at,
            archive_name=archive_name,
            archive_modified_at=archive_modified_at,
            archive_size_bytes=archive_details.st_size,
        )
    except (FileNotFoundError, PermissionError, OSError):
        return _failed(now, "unavailable")
    except (UnicodeError, ValueError):
        return _failed(now, "invalid")
