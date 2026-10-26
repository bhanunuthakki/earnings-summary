"""Bounded report file capture primitive (input commitments and replay).

This module provides deterministic, bounded file reads and directory population
selections for report generation. It enforces strict allocation limits before
reading bytes, pins parent directory handles, and verifies replay commitments.
"""

from __future__ import annotations

import codecs
import contextlib
import contextvars
import hashlib
import json
import os
import re
import stat
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping, Sequence
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Literal, NoReturn, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from provenance.immutable_artifact import (
    ImmutableArtifactConflictError,
    read_stable_artifact,
    require_no_reparse_points,
)
from runtime.job_runtime import JobLock, current_lock_claim

CAPTURE_WRITE_SETS: tuple[str, ...] = (
    "capture-tmp-summaries",
    "capture-tmp-saydo",
    "capture-transcripts",
    "capture-news-cache",
    "capture-bear-cache",
    "capture-saydo-filter",
    "capture-qa-topics",
    "capture-peer-selection",
    "capture-fmp-peers",
    "capture-key-metrics",
    "capture-segment-definitions",
)


@contextlib.contextmanager
def hold_capture_write_set(
    repo_root: Path,
    write_set_names: Sequence[str] | str,
    *,
    wait_s: float = 5.0,
) -> Generator[None]:
    """Own one or more report-capture mutable source write sets across processes.

    Synchronous nested callers on the same context reuse already-held write sets
    and acquire only the missing sets in deterministic sorted order.
    """
    root = repo_root.resolve()
    names = [write_set_names] if isinstance(write_set_names, str) else list(write_set_names)
    missing = sorted({ws for ws in names if ws and current_lock_claim(root, ws) is None})
    if not missing:
        yield
        return
    with JobLock(root, "capture-write-set", missing, wait_s=wait_s):
        yield


_TICKER_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._-]*$")
_SUMMARY_RX = re.compile(
    r"^(?P<ticker>[A-Z][A-Z0-9.]*)_Q(?P<q>[1-4])_(?P<y>\d{4})_(?:investor_update_)?summary\.txt$"
)
_TRANSCRIPT_RX = re.compile(
    r"^(?P<ticker>[A-Z][A-Z0-9.]*)_Q(?P<q>[1-4])_(?P<y>\d{4})\.(?:txt|pdf)$"
)
_BRIEF_RX = re.compile(
    r"^(?P<ticker>[A-Z][A-Z0-9.]*)_Q(?P<q>[1-4])_(?P<y>\d{4})_(?P<kind>press_release_summary|presentation_brief)\.txt$"
)
_SAYDO_FILENAME_RX = re.compile(
    r"^SayDo_(?P<t>[A-Z][A-Z0-9.]*)_Q(?P<pq>[1-4])_(?P<py>\d{4})_Q(?P<cq>[1-4])_(?P<cy>\d{4})\.txt$"
)

_SELECTOR_ORDER: tuple[str, ...] = (
    "quarterly_summaries_tmp_v1",
    "transcripts_dual_dir_v1",
    "themes_recent_transcripts_v1",
    "ir_briefs_tmp_v1",
    "saydo_cards_tmp_v1",
    "qa_topics_json_v1",
    "filing_intelligence_json_v1",
    "bear_case_json_v1",
    "news_cache_json_v1",
    "saydo_filter_json_v1",
    "segment_definitions_json_v1",
    "peer_selection_json_v1",
    "peer_fundamentals_json_v1",
    "holdings_json_v1",
    "fmp_price_chart_v1",
    "report_comments_json_v1",
)
_SELECTOR_ALIASES: dict[str, str] = {f"S{i}": n for i, n in enumerate(_SELECTOR_ORDER, start=1)}
_CLOSED_SELECTORS: frozenset[str] = frozenset(_SELECTOR_ALIASES) | frozenset(_SELECTOR_ORDER)

_FIXED_DIR_SELECTORS: dict[str, tuple[str, str]] = {
    "qa_topics_json_v1": ("data/qa_topics", "exact"),
    "filing_intelligence_json_v1": ("data/filing_intelligence", "exact"),
    "bear_case_json_v1": ("data/bear_case", "exact"),
    "news_cache_json_v1": (".tmp/news_cache", "exact"),
    "saydo_filter_json_v1": ("data/saydo_filter", "exact"),
    "segment_definitions_json_v1": ("data/segment_definitions", "exact"),
    "peer_selection_json_v1": ("data/peer_selection", "exact"),
    "holdings_json_v1": ("micro_thesis/holdings", "exact"),
    "report_comments_json_v1": ("data/report_comments/{T}", "json_suffix"),
}


def canonical_digest(value: object) -> str:
    """Compute deterministic SHA256 over canonical JSON serialization."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


class FileCaptureUnavailableError(RuntimeError):
    """Typed capture refusal. Never a partial receipt or a degraded result."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        full_msg = f"report file capture unavailable: {reason}" + (
            f" ({message})" if message else ""
        )
        super().__init__(full_msg)
        self.reason = reason
        self.message = message if message is not None else reason


FileCaptureUnavailable = FileCaptureUnavailableError


class FileCaptureMismatchError(FileCaptureUnavailableError):
    """Replay verification mismatch against a sealed file capture receipt."""

    def __init__(self, message: str, reason: str = "replay_mismatch") -> None:
        super().__init__(reason=reason, message=message)


class FileCaptureBudget(BaseModel):
    """Execution ceilings bounding memory, file counts, and traversal work."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_bytes_per_file: int = Field(default=32 * 1024 * 1024, gt=0)
    max_total_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    max_files: int = Field(default=256, gt=0)
    max_visited_directories: int = Field(default=64, gt=0)
    max_examined_entries: int = Field(default=2_048, gt=0)
    max_selected_members: int = Field(default=256, gt=0)
    max_retained_metadata_bytes: int = Field(default=262_144, gt=0)


class FileReadCommitment(BaseModel):
    """Immutable commitment for one requested source file path."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    relative_path: str
    file_exists: bool
    size_bytes: int
    content_sha256: str | None
    mtime_ns: int | None = None
    encoding: str | None = None
    errors: str | None = None


class DirectorySelectionCommitment(BaseModel):
    """Immutable commitment for one directory selection query."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    selector_id: str
    ticker: str
    directory_exists: bool
    directory_states: Mapping[str, bool]
    selected_relative_paths: tuple[str, ...]
    member_sha256s: Mapping[str, str]
    effective_winners: Mapping[str, str]
    selection_digest: str

    @model_validator(mode="after")
    def _freeze_mappings(self) -> Self:
        for f in ("directory_states", "member_sha256s", "effective_winners"):
            object.__setattr__(self, f, MappingProxyType(dict(getattr(self, f))))
        return self

    @field_serializer("directory_states", "member_sha256s", "effective_winners")
    def _serialize_mappings(self, v: Mapping[str, Any]) -> dict[str, Any]:
        return dict(v)


class FileCaptureReceipt(BaseModel):
    """Cryptographic receipt proving exact source files and selections captured."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    status: Literal["sealed", "invalidated"]
    content_roots: tuple[str, ...]
    files: Mapping[str, FileReadCommitment]
    selections: Mapping[str, DirectorySelectionCommitment]
    total_files_read: int
    total_bytes_read: int
    total_directories_visited: int
    total_examined_entries: int
    receipt_digest: str

    @model_validator(mode="after")
    def _freeze_mappings(self) -> Self:
        for f in ("files", "selections"):
            object.__setattr__(self, f, MappingProxyType(dict(getattr(self, f))))
        return self

    @field_serializer("files", "selections")
    def _serialize_mappings(self, v: Mapping[str, Any]) -> dict[str, Any]:
        return dict(v)


def _brief_sort_key(item: tuple[str, str]) -> tuple[int, int, str]:
    return (
        (int(m.group("y")), int(m.group("q")), m.group("kind"))
        if (m := _BRIEF_RX.match(item[0]))
        else (0, 0, "")
    )


def _saydo_sort_key(item: tuple[str, str]) -> tuple[int, int, int, int]:
    return (
        (int(m.group("cy")), int(m.group("cq")), int(m.group("py")), int(m.group("pq")))
        if (m := _SAYDO_FILENAME_RX.match(item[0]))
        else (0, 0, 0, 0)
    )


_CURRENT_FILE_CAPTURE_SESSION: contextvars.ContextVar[ReportFileCaptureSession | None] = (
    contextvars.ContextVar("_CURRENT_FILE_CAPTURE_SESSION", default=None)
)


def current_file_capture_session() -> ReportFileCaptureSession | None:
    return _CURRENT_FILE_CAPTURE_SESSION.get()


def read_captured_text(
    repo_root: Path,
    path: Path | str,
    encoding: str = "utf-8",
    errors: str = "strict",
) -> str:
    session = current_file_capture_session()
    if session is not None:
        p = Path(path)
        if p.is_absolute():
            try:
                rel = p.resolve().relative_to(session.repo_root.resolve()).as_posix()
            except ValueError:
                rel = p.as_posix()
        else:
            rel = p.as_posix()
        return session.read_text(rel, encoding=encoding, errors=errors)
    p = Path(path)
    abs_path = p if p.is_absolute() else (repo_root / p)
    with open(abs_path, encoding=encoding, errors=errors) as f:
        return f.read()


class ReportFileCaptureSession:
    """Manages bounded file capture, memory caching, and sealed isolation."""

    def __enter__(self) -> Self:
        self._reset_token = _CURRENT_FILE_CAPTURE_SESSION.set(self)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object | None,
    ) -> None:
        if hasattr(self, "_reset_token"):
            _CURRENT_FILE_CAPTURE_SESSION.reset(self._reset_token)

    def __init__(
        self,
        repo_root: Path,
        transcripts_dir: Path | None = None,
        tmp_dir: Path | None = None,
        data_dir: Path | None = None,
        holdings_dir: Path | None = None,
        dcf_dir: Path | None = None,
        content_roots: Sequence[Path] | None = None,
        budget: FileCaptureBudget | None = None,
        scandir_fn: Callable[[str | Path], Iterable[os.DirEntry[str]]] | None = None,
    ) -> None:
        self.repo_root = Path(os.path.abspath(os.fspath(repo_root)))
        roots = content_roots or (
            transcripts_dir or (self.repo_root / "transcripts"),
            tmp_dir or (self.repo_root / ".tmp"),
            data_dir or (self.repo_root / "data"),
            holdings_dir or (self.repo_root / "micro_thesis" / "holdings"),
            dcf_dir or (self.repo_root / "dcf"),
        )
        self.content_roots = tuple(Path(os.path.abspath(os.fspath(r))) for r in roots)
        forbidden = {self.repo_root, self.repo_root / "micro_thesis"}
        if holdings_dir and Path(os.path.abspath(os.fspath(holdings_dir))) in forbidden:
            raise ValueError(f"Overly broad holdings_dir: {holdings_dir}")
        for root in self.content_roots:
            if root in forbidden:
                raise ValueError(f"Overly broad content root: {root}")
        self.budget = budget or FileCaptureBudget()
        self._scandir_fn = scandir_fn

        self._byte_cache: dict[str, bytes] = {}
        self._files: dict[str, FileReadCommitment] = {}
        self._selections: dict[str, DirectorySelectionCommitment] = {}

        self.total_bytes_captured: int = 0
        self.total_files_captured: int = 0
        self.total_directories_visited: int = 0
        self.total_examined_entries: int = 0
        self.total_selected_members: int = 0
        self.total_retained_metadata_bytes: int = 0

        self._sealed: bool = False
        self._invalidated: bool = False
        self._invalidation_reason: str | None = None

    def _fail(self, reason: str, message: str, exc_from: Exception | None = None) -> NoReturn:
        self._invalidated = True
        self._invalidation_reason = f"{reason}: {message}"
        if exc_from is not None:
            raise FileCaptureUnavailableError(reason, message) from exc_from
        raise FileCaptureUnavailableError(reason, message)

    def _check_active(self) -> None:
        if self._invalidated:
            raise FileCaptureUnavailableError("session_invalidated", self._invalidation_reason)

    def _check_ceiling(self, current: int, ceiling: int, name: str) -> None:
        if current > ceiling:
            self._fail("budget_exhausted", f"{name} ceiling exceeded")

    def _normalize_rel_path(self, path: str | Path) -> str:
        posix = PurePosixPath(Path(path).as_posix())
        if posix.is_absolute() or ".." in posix.parts:
            self._fail("forbidden_selector", f"invalid relative path: {path}")
        return posix.as_posix()

    def _resolve_and_verify_path(self, rel_path: str) -> tuple[Path, Path]:
        abs_path = Path(os.path.abspath(os.fspath(self.repo_root / rel_path)))
        matching_root: Path | None = None
        for root in self.content_roots:
            try:
                abs_path.relative_to(root)
                matching_root = root
                break
            except ValueError:
                continue
        if matching_root is None:
            self._fail("artifact_unstable", f"path escapes content roots: {rel_path}")
        try:
            require_no_reparse_points(abs_path)
        except (ImmutableArtifactConflictError, OSError) as exc:
            self._fail("artifact_unstable", str(exc), exc_from=exc)
        return abs_path, matching_root

    def read_bytes(self, relative_path: str | Path) -> bytes:
        """Read source bytes once, cache in memory, and enforce budgets."""
        self._check_active()
        rel_path = self._normalize_rel_path(relative_path)

        if rel_path in self._byte_cache:
            return self._byte_cache[rel_path]
        if rel_path in self._files and not self._files[rel_path].file_exists:
            raise FileNotFoundError(f"file not found: {rel_path}")
        if self._sealed:
            raise FileCaptureUnavailableError(
                "not_captured", f"path was not captured before sealing: {rel_path}"
            )

        abs_path, matching_root = self._resolve_and_verify_path(rel_path)

        try:
            st = os.lstat(abs_path)
        except FileNotFoundError:
            try:
                require_no_reparse_points(abs_path.parent)
            except (ImmutableArtifactConflictError, OSError) as exc:
                self._fail("artifact_unstable", str(exc), exc_from=exc)
            self._check_ceiling(len(self._files) + 1, self.budget.max_files, "max_files")
            commitment = FileReadCommitment(
                relative_path=rel_path,
                file_exists=False,
                size_bytes=0,
                content_sha256=None,
                mtime_ns=None,
            )
            self._files[rel_path] = commitment
            raise FileNotFoundError(f"file not found: {rel_path}") from None
        except OSError as exc:
            self._fail("artifact_unstable", str(exc), exc_from=exc)

        if not stat.S_ISREG(st.st_mode):
            self._fail("artifact_unstable", f"not a regular file: {rel_path}")

        remaining_total_budget = self.budget.max_total_bytes - self.total_bytes_captured
        if remaining_total_budget <= 0:
            self._fail("budget_exhausted", "total byte budget exhausted")

        effective_max_bytes = min(self.budget.max_bytes_per_file, remaining_total_budget)
        if st.st_size > effective_max_bytes:
            self._fail("budget_exhausted", f"file size {st.st_size} exceeds {effective_max_bytes}")

        self._check_ceiling(len(self._files) + 1, self.budget.max_files, "max_files")

        try:
            snapshot, payload = read_stable_artifact(
                abs_path,
                max_bytes=effective_max_bytes,
                allowed_root=matching_root,
            )
        except (ImmutableArtifactConflictError, ValueError) as exc:
            err = str(exc)
            b_err = "exceeds" in err or "must be positive" in err
            self._fail("budget_exhausted" if b_err else "artifact_unstable", err, exc_from=exc)
        except OSError as exc:
            self._fail("artifact_unstable", str(exc), exc_from=exc)

        content_sha256 = hashlib.sha256(payload).hexdigest()
        self.total_bytes_captured += len(payload)
        self.total_files_captured += 1
        self._byte_cache[rel_path] = payload

        commitment = FileReadCommitment(
            relative_path=rel_path,
            file_exists=True,
            size_bytes=len(payload),
            content_sha256=content_sha256,
            mtime_ns=snapshot.modified_time_ns,
        )
        self._files[rel_path] = commitment
        return payload

    def read_text(
        self,
        relative_path: str | Path,
        encoding: str = "utf-8",
        errors: str = "strict",
    ) -> str:
        """Read source text through captured bytes and record encoding."""
        payload = self.read_bytes(relative_path)
        rel_path = self._normalize_rel_path(relative_path)
        comm = self._files.get(rel_path)
        if self._sealed:
            if comm is None or comm.encoding != encoding or comm.errors != errors:
                raise FileCaptureUnavailableError(
                    "not_captured",
                    f"text decoding options ({encoding}, {errors}) were not captured before sealing: {rel_path}",
                )
        elif (
            comm is not None
            and comm.encoding is not None
            and (comm.encoding != encoding or comm.errors != errors)
        ):
            self._fail("decode_failed", "conflicting encoding parameters for path")

        try:
            codecs.lookup_error(errors)
            text = payload.decode(encoding=encoding, errors=errors)
        except (UnicodeDecodeError, LookupError) as exc:
            self._fail("decode_failed", str(exc), exc_from=exc)

        if not self._sealed and comm is not None and comm.encoding is None:
            self._files[rel_path] = comm.model_copy(update={"encoding": encoding, "errors": errors})
        return text

    def read_json(self, relative_path: str | Path, encoding: str = "utf-8") -> Any:
        """Read and parse JSON at existing reader seam; fail closed on syntax errors."""
        text = self.read_text(relative_path, encoding=encoding)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            self._fail("json_failed", str(exc), exc_from=exc)

    def _scan_single_dir(
        self,
        rel_dir: str,
        match_fn: Callable[[str], bool],
    ) -> tuple[bool, list[tuple[str, str]]]:
        self._check_active()
        abs_dir, _ = self._resolve_and_verify_path(rel_dir)
        try:
            lstat_before = os.lstat(abs_dir)
        except FileNotFoundError:
            return False, []
        except OSError as exc:
            self._fail("artifact_unstable", str(exc), exc_from=exc)

        if getattr(abs_dir, "is_junction", lambda: False)() or stat.S_ISLNK(lstat_before.st_mode):
            self._fail("artifact_unstable", f"directory is symlink/junction: {rel_dir}")
        if not stat.S_ISDIR(lstat_before.st_mode):
            return False, []

        self.total_directories_visited += 1
        self._check_ceiling(
            self.total_directories_visited,
            self.budget.max_visited_directories,
            "max_visited_directories",
        )

        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_DIRECTORY", 0)
        try:
            dir_fd = os.open(abs_dir, flags)
        except OSError as exc:
            self._fail("artifact_unstable", str(exc), exc_from=exc)

        matched: list[tuple[str, str]] = []
        try:
            dev_ino = (lstat_before.st_dev, lstat_before.st_ino)
            try:
                fstat_before = os.fstat(dir_fd)
            except OSError as exc:
                self._fail("artifact_unstable", str(exc), exc_from=exc)

            if (fstat_before.st_dev, fstat_before.st_ino) != dev_ino or not stat.S_ISDIR(
                fstat_before.st_mode
            ):
                self._fail("artifact_unstable", f"directory identity mismatch on open: {rel_dir}")

            def _iterate_entries() -> Iterator[os.DirEntry[str]]:
                if self._scandir_fn is None:
                    with os.scandir(dir_fd) as it:
                        yield from it
                else:
                    yield from self._scandir_fn(abs_dir)

            try:
                for entry in _iterate_entries():
                    if entry.name in ("job_locks", ".DS_Store"):
                        continue
                    self.total_examined_entries += 1
                    self._check_ceiling(
                        self.total_examined_entries,
                        self.budget.max_examined_entries,
                        "max_examined_entries",
                    )

                    self.total_retained_metadata_bytes += (
                        len(entry.path.encode("utf-8")) + len(entry.name.encode("utf-8")) + 64
                    )
                    self._check_ceiling(
                        self.total_retained_metadata_bytes,
                        self.budget.max_retained_metadata_bytes,
                        "max_retained_metadata_bytes",
                    )

                    if entry.is_symlink() or getattr(entry, "is_junction", lambda: False)():
                        self._fail("artifact_unstable", f"entry is symlink/junction: {entry.name}")

                    if entry.is_file(follow_symlinks=False) and match_fn(entry.name):
                        self.total_selected_members += 1
                        self._check_ceiling(
                            self.total_selected_members,
                            self.budget.max_selected_members,
                            "max_selected_members",
                        )
                        matched.append((entry.name, f"{rel_dir}/{entry.name}"))
            except OSError as exc:
                self._fail("artifact_unstable", str(exc), exc_from=exc)

            try:
                fstat_after, lstat_after = os.fstat(dir_fd), os.lstat(abs_dir)
            except OSError as exc:
                self._fail("artifact_unstable", str(exc), exc_from=exc)

            if (fstat_after.st_dev, fstat_after.st_ino) != dev_ino or (
                lstat_after.st_dev,
                lstat_after.st_ino,
            ) != dev_ino:
                self._fail("artifact_unstable", f"directory identity changed: {rel_dir}")
        finally:
            with contextlib.suppress(OSError):
                os.close(dir_fd)

        return True, matched

    def _ingest_and_record(
        self,
        selector_id: str,
        ticker: str,
        directory_exists: bool,
        directory_states: dict[str, bool],
        effective_winners: dict[str, str],
        matched_paths: list[str],
    ) -> DirectorySelectionCommitment:
        member_sha256s: dict[str, str] = {}
        for rel_path in matched_paths:
            self.read_bytes(rel_path)
            if (comm := self._files.get(rel_path)) and comm.content_sha256 is not None:
                member_sha256s[rel_path] = comm.content_sha256

        selected_relative_paths = tuple(sorted(member_sha256s.keys()))
        payload = {
            "directory_exists": directory_exists,
            "directory_states": directory_states,
            "effective_winners": effective_winners,
            "member_sha256s": member_sha256s,
            "selected_relative_paths": list(selected_relative_paths),
            "selector_id": selector_id,
            "ticker": ticker.upper(),
        }
        commitment = DirectorySelectionCommitment(
            selector_id=selector_id,
            ticker=ticker.upper(),
            directory_exists=directory_exists,
            directory_states=directory_states,
            selected_relative_paths=selected_relative_paths,
            member_sha256s=member_sha256s,
            effective_winners=effective_winners,
            selection_digest=canonical_digest(payload),
        )
        self._selections[f"{selector_id}:{ticker.upper()}"] = commitment
        return commitment

    def record_directory_selection(
        self, selector_id: str, ticker: str
    ) -> DirectorySelectionCommitment:
        """Record bounded directory enumeration and compute winner selection."""
        self._check_active()
        clean_ticker = ticker.strip()
        if ".." in clean_ticker or not _TICKER_PATTERN.match(clean_ticker):
            self._fail("forbidden_selector", f"invalid ticker: {ticker!r}")
        if selector_id not in _CLOSED_SELECTORS:
            self._fail("forbidden_selector", f"unknown selector_id: {selector_id!r}")

        canonical_id = _SELECTOR_ALIASES.get(selector_id, selector_id)
        selection_key = f"{canonical_id}:{clean_ticker.upper()}"

        if selection_key in self._selections:
            return self._selections[selection_key]
        if self._sealed:
            raise FileCaptureUnavailableError(
                "not_captured", f"selection was not captured before sealing: {selection_key}"
            )

        effective_winners: dict[str, str] = {}
        upper_ticker = clean_ticker.upper()
        dir_states: dict[str, bool] = {}
        matched_paths: list[str] = []
        matched: list[tuple[str, str]] = []
        dir_exists: bool = False

        if canonical_id == "quarterly_summaries_tmp_v1":
            dir_exists, matched = self._scan_single_dir(
                ".tmp",
                lambda n: bool(
                    (m := _SUMMARY_RX.match(n)) and m.group("ticker").upper() == upper_ticker
                ),
            )
            dir_states = {".tmp": dir_exists}
            for name, rel_path in matched:
                if m := _SUMMARY_RX.match(name):
                    effective_winners[f"Q{m.group('q')}_{m.group('y')}"] = rel_path

        elif canonical_id == "transcripts_dual_dir_v1":
            p_exists, p_matched = self._scan_single_dir(
                "transcripts/processed",
                lambda n: bool(
                    (m := _TRANSCRIPT_RX.match(n)) and m.group("ticker").upper() == upper_ticker
                ),
            )
            r_exists, r_matched = self._scan_single_dir(
                "transcripts/raw",
                lambda n: bool(
                    (m := _TRANSCRIPT_RX.match(n)) and m.group("ticker").upper() == upper_ticker
                ),
            )
            dir_exists = p_exists or r_exists
            dir_states = {"transcripts/processed": p_exists, "transcripts/raw": r_exists}
            for name, rel_path in p_matched + r_matched:
                if (m := _TRANSCRIPT_RX.match(name)) and (
                    k := f"Q{m.group('q')}_{m.group('y')}"
                ) not in effective_winners:
                    effective_winners[k] = rel_path
            matched_paths = [p for _, p in p_matched + r_matched]

        elif canonical_id == "themes_recent_transcripts_v1":
            s2 = self.record_directory_selection("transcripts_dual_dir_v1", clean_ticker)
            dir_exists = s2.directory_exists
            dir_states = dict(s2.directory_states)

            def _sort_slot(k: str) -> tuple[int, int]:
                return int(k.split("_")[1]), int(k.split("_")[0][1:])

            recent = sorted(s2.effective_winners.keys(), key=_sort_slot)[-4:]
            effective_winners = {k: s2.effective_winners[k] for k in recent}
            matched_paths = list(effective_winners.values())

        elif canonical_id == "ir_briefs_tmp_v1":
            dir_exists, matched = self._scan_single_dir(
                ".tmp",
                lambda n: bool(
                    (m := _BRIEF_RX.match(n)) and m.group("ticker").upper() == upper_ticker
                ),
            )
            dir_states = {".tmp": dir_exists}
            for name, rel_path in sorted(matched, key=_brief_sort_key):
                if m := _BRIEF_RX.match(name):
                    effective_winners[f"{m.group('y')}_Q{m.group('q')}_{m.group('kind')}"] = (
                        rel_path
                    )

        elif canonical_id == "saydo_cards_tmp_v1":
            dir_exists, matched = self._scan_single_dir(
                ".tmp",
                lambda n: bool(
                    (m := _SAYDO_FILENAME_RX.match(n)) and m.group("t").upper() == upper_ticker
                ),
            )
            dir_states = {".tmp": dir_exists}
            for name, rel_path in sorted(matched, key=_saydo_sort_key):
                if m := _SAYDO_FILENAME_RX.match(name):
                    effective_winners[
                        f"Q{m.group('cq')}_{m.group('cy')}_Q{m.group('pq')}_{m.group('py')}"
                    ] = rel_path

        elif canonical_id == "fmp_price_chart_v1":
            fmp_dir = "data/historical/fmp"
            goog = upper_ticker == "GOOG"

            def _price_match(n: str) -> bool:
                return ("price_chart" in n and n.endswith(".json")) and (
                    n.startswith(f"{upper_ticker}_") or (goog and n.startswith("GOOGL_"))
                )

            def _price_sort_key(item: tuple[str, str]) -> tuple[int, str]:
                name, _rel = item
                is_primary = 0 if name.startswith(f"{upper_ticker}_") else 1
                return (is_primary, name)

            dir_exists, matched = self._scan_single_dir(fmp_dir, _price_match)
            dir_states = {fmp_dir: dir_exists}
            for idx, (_name, rel_path) in enumerate(sorted(matched, key=_price_sort_key)):
                effective_winners[f"slot_{idx:03d}"] = rel_path

        elif canonical_id == "peer_fundamentals_json_v1":
            s12 = self.record_directory_selection("peer_selection_json_v1", clean_ticker)
            s12_peers: set[str] = set()

            def _add_ticker(val: object) -> None:
                if (
                    isinstance(val, str)
                    and ".." not in val
                    and _TICKER_PATTERN.match(clean := val.strip().upper())
                ):
                    s12_peers.add(clean)

            if (s12_file := f"{upper_ticker}.json") in s12.effective_winners:
                try:
                    s12_raw: object = self.read_json(s12.effective_winners[s12_file])
                    if isinstance(s12_raw, dict):
                        s12_dict = cast(dict[str, list[dict[str, object]]], s12_raw)
                        suggs = s12_dict.get("suggestions")
                        if isinstance(suggs, list):
                            for e in suggs:
                                if type(e) is dict:
                                    _add_ticker(e.get("ticker"))
                except (FileNotFoundError, json.JSONDecodeError):
                    pass

            fmp_dir = "data/historical/fmp"
            allowed_names: set[str] = {
                f"{upper_ticker}_profile.json",
                f"{upper_ticker}_peers.json",
            }
            suffixes = (
                "_profile.json",
                "_income_statement_quarterly.json",
                "_key_metrics_ttm.json",
                "_financial_ratios_ttm.json",
            )
            for p in sorted(s12_peers):
                allowed_names.update(f"{p}{s}" for s in suffixes)

            dir_exists, matched = self._scan_single_dir(fmp_dir, lambda n: n in allowed_names)
            dir_states = {fmp_dir: dir_exists}
            for name, rel_path in sorted(matched, key=lambda x: x[0]):
                effective_winners[name] = rel_path

        else:
            tmpl, mode = _FIXED_DIR_SELECTORS.get(canonical_id, ("data", "exact"))
            target_dir = tmpl.replace("{T}", upper_ticker)
            dir_exists, matched = self._scan_single_dir(
                target_dir,
                (lambda n: n.endswith(".json"))
                if mode == "json_suffix"
                else (lambda n: n == f"{upper_ticker}.json"),
            )
            dir_states = {target_dir: dir_exists}
            for name, rel_path in sorted(matched, key=lambda x: x[0]):
                effective_winners[name] = rel_path

        return self._ingest_and_record(
            canonical_id,
            clean_ticker,
            dir_exists,
            dir_states,
            effective_winners,
            matched_paths or [p for _, p in matched],
        )

    def has_cached_bytes(self, relative_path: str | Path) -> bool:
        """Check if raw bytes are cached in memory for the path."""
        return self._normalize_rel_path(relative_path) in self._byte_cache

    def get_file_commitment(self, relative_path: str | Path) -> FileReadCommitment | None:
        """Retrieve commitment for captured file path."""
        return self._files.get(self._normalize_rel_path(relative_path))

    def read_mtime_ns(self, relative_path: str | Path) -> int | None:
        """Retrieve mtime_ns for captured file; read and cache bytes if needed."""
        self.read_bytes(relative_path)
        return self._files[self._normalize_rel_path(relative_path)].mtime_ns

    def get_directory_selection(
        self, selector_id: str, ticker: str
    ) -> DirectorySelectionCommitment | None:
        """Retrieve commitment for captured directory selection."""
        canonical_id = _SELECTOR_ALIASES.get(selector_id, selector_id)
        return self._selections.get(f"{canonical_id}:{ticker.strip().upper()}")

    def seal(self) -> FileCaptureReceipt:
        """Seal session into immutable receipt; reject future uncached operations."""
        self._check_active()
        self._sealed = True

        roots_list = sorted(root.as_posix() for root in self.content_roots)
        receipt_payload = {
            "content_roots": roots_list,
            "files": {k: v.model_dump() for k, v in sorted(self._files.items())},
            "selections": {k: v.model_dump() for k, v in sorted(self._selections.items())},
            "status": "sealed",
            "total_bytes_read": self.total_bytes_captured,
            "total_directories_visited": self.total_directories_visited,
            "total_examined_entries": self.total_examined_entries,
            "total_files_read": self.total_files_captured,
        }
        return FileCaptureReceipt(
            status="sealed",
            content_roots=tuple(roots_list),
            files=dict(self._files),
            selections=dict(self._selections),
            total_files_read=self.total_files_captured,
            total_bytes_read=self.total_bytes_captured,
            total_directories_visited=self.total_directories_visited,
            total_examined_entries=self.total_examined_entries,
            receipt_digest=canonical_digest(receipt_payload),
        )


def replay_file_capture(
    receipt: FileCaptureReceipt,
    repo_root: Path,
    *,
    transcripts_dir: Path | None = None,
    tmp_dir: Path | None = None,
    data_dir: Path | None = None,
    holdings_dir: Path | None = None,
    content_roots: Sequence[Path] | None = None,
    budget: FileCaptureBudget | None = None,
    scandir_fn: Callable[[str | Path], Iterable[os.DirEntry[str]]] | None = None,
) -> FileCaptureReceipt:
    """Replay sealed capture receipt against disk and verify complete agreement."""
    if receipt.status != "sealed":
        raise FileCaptureMismatchError("cannot replay unsealed receipt")

    session = ReportFileCaptureSession(
        repo_root=repo_root,
        transcripts_dir=transcripts_dir,
        tmp_dir=tmp_dir,
        data_dir=data_dir,
        holdings_dir=holdings_dir,
        content_roots=content_roots,
        budget=budget,
        scandir_fn=scandir_fn,
    )

    try:
        attrs = (
            "directory_exists",
            "directory_states",
            "selected_relative_paths",
            "member_sha256s",
            "effective_winners",
            "selection_digest",
        )
        for sel_key, exp_sel in receipt.selections.items():
            act_sel = session.record_directory_selection(exp_sel.selector_id, exp_sel.ticker)
            for attr in attrs:
                if getattr(act_sel, attr) != getattr(exp_sel, attr):
                    raise FileCaptureMismatchError(f"selection {sel_key} {attr} mismatch")

        for rel_path, exp_file in receipt.files.items():
            if exp_file.file_exists:
                payload = session.read_bytes(rel_path)
                if exp_file.encoding is not None:
                    session.read_text(
                        rel_path, encoding=exp_file.encoding, errors=exp_file.errors or "strict"
                    )
                if hashlib.sha256(payload).hexdigest() != exp_file.content_sha256:
                    raise FileCaptureMismatchError(f"file {rel_path} content sha mismatch")
                if len(payload) != exp_file.size_bytes:
                    raise FileCaptureMismatchError(f"file {rel_path} size mismatch")
                act_comm = session.get_file_commitment(rel_path)
                if act_comm is None or act_comm.mtime_ns != exp_file.mtime_ns:
                    raise FileCaptureMismatchError(f"file {rel_path} mtime_ns mismatch")
            else:
                try:
                    session.read_bytes(rel_path)
                    raise FileCaptureMismatchError(f"file {rel_path} was missing but now exists")
                except FileNotFoundError:
                    pass
                act_comm = session.get_file_commitment(rel_path)
                if act_comm is None or act_comm.file_exists:
                    raise FileCaptureMismatchError(f"file {rel_path} missing commitment mismatch")

        actual_receipt = session.seal()
    except (FileNotFoundError, FileCaptureUnavailableError) as exc:
        raise FileCaptureMismatchError(str(exc)) from exc

    if actual_receipt.receipt_digest != receipt.receipt_digest:
        raise FileCaptureMismatchError("receipt digest mismatch")

    return actual_receipt
