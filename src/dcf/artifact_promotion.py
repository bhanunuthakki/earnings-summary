"""Compensating file promotion coordinated with a DCF database transaction."""

from __future__ import annotations

import contextlib
import json
import os
import sys
from collections.abc import Callable, Generator
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast
from uuid import uuid4

from db_paths import db_path_context, require_db_path
from runtime.job_runtime import (
    JobAlreadyRunningError,
    JobLock,
    current_lock_claim,
    inherited_lock_is_valid,
)
from ticker_validation import safe_ticker


@dataclass(slots=True)
class DcfArtifactClaim:
    child_environment: dict[str, str]
    candidates: list[Path] = field(default_factory=list[Path])


_ACTIVE_CLAIMS: ContextVar[dict[tuple[Path, str], DcfArtifactClaim] | None] = ContextVar(
    "dcf_artifact_claims", default=None
)
_ACTIVE_CANDIDATES: ContextVar[list[Path] | None] = ContextVar("dcf_candidates", default=None)


@contextlib.contextmanager
def hold_dcf_artifacts(
    repo_root: Path, ticker: str, *, owner: str, wait_s: float = 0
) -> Generator[DcfArtifactClaim]:
    """Own one ticker's mutable artifacts, including validated builder children.

    Lock identity follows the resolved artifact directory, independently of the
    code checkout and configured database location. Nested synchronous callers
    reuse only their exact live claim; unrelated threads/processes must contend.
    """
    root = (repo_root / "dcf").resolve()
    write_set = f"dcf-{safe_ticker(ticker)}"
    key = (root, write_set)
    claims = _ACTIVE_CLAIMS.get() or {}
    existing = claims.get(key)
    if existing is not None and current_lock_claim(root, write_set) is not None:
        yield existing
        return
    if inherited_lock_is_valid(root, write_set):
        claim = DcfArtifactClaim(
            {"EARNINGS_SUMMARY_JOB_LOCK_PROOF": os.environ["EARNINGS_SUMMARY_JOB_LOCK_PROOF"]}
        )
        lock_context = contextlib.nullcontext(None)
    else:
        claim = DcfArtifactClaim({})
        lock_context = JobLock(root, owner, [write_set], wait_s=wait_s)
    with lock_context as held:
        unresolved = [
            backup
            for path in (
                repo_root / "dcf" / f"{safe_ticker(ticker)}.xlsx",
                repo_root / "data" / "dcf_assumptions" / f"{safe_ticker(ticker)}.json",
            )
            for backup in path.parent.glob(f"{path.stem}.rollback.*{path.suffix}")
        ]
        if unresolved:
            raise DcfRecoveryError(
                "dcf_recovery_required; retained=" + ",".join(map(str, unresolved))
            )
        if held is not None:
            inherited: object = {}
            with contextlib.suppress(ValueError):
                inherited = json.loads(os.environ.get("EARNINGS_SUMMARY_JOB_LOCK_PROOF", "{}"))
            proof = cast("dict[str, object]", inherited) if isinstance(inherited, dict) else {}
            proof.update(json.loads(held.inheritance_proof()))
            claim.child_environment = {"EARNINGS_SUMMARY_JOB_LOCK_PROOF": json.dumps(proof)}
        claim_token = _ACTIVE_CLAIMS.set({**claims, key: claim})
        candidate_token = _ACTIVE_CANDIDATES.set(claim.candidates)
        try:
            yield claim
        finally:
            cleanup_failures: list[Path] = []
            try:
                for candidate in claim.candidates:
                    try:
                        candidate.unlink(missing_ok=True)
                    except OSError:
                        cleanup_failures.append(candidate)
            finally:
                _ACTIVE_CANDIDATES.reset(candidate_token)
                _ACTIVE_CLAIMS.reset(claim_token)
            if cleanup_failures:
                raise DcfRecoveryError(
                    "dcf_stage_cleanup_failed; retained=" + ",".join(map(str, cleanup_failures))
                ) from None


def dcf_child_environment(repo_root: Path, ticker: str) -> dict[str, str]:
    key = ((repo_root / "dcf").resolve(), f"dcf-{safe_ticker(ticker)}")
    claim = (_ACTIVE_CLAIMS.get() or {}).get(key)
    return dict(claim.child_environment) if claim is not None else {}


def run_dcf_entrypoint(
    repo_root: Path,
    ticker: str,
    operation: Callable[[], int],
    *,
    owner: str,
    require_database: bool = True,
) -> int:
    """Apply the same writer and recovery contract to direct model CLI mains."""
    try:
        with hold_dcf_artifacts(repo_root, ticker, owner=owner, wait_s=0):
            if not require_database:
                return operation()
            try:
                configured = os.environ.get("EARNINGS_SUMMARY_DB_PATH", "").strip()
                if not configured:
                    raise RuntimeError("configured_database_required")
                database = require_db_path(configured)
            except (FileNotFoundError, RuntimeError) as exc:
                print(f"BLOCKED\t{ticker}\tdcf_database_unavailable\t{exc}", file=sys.stderr)
                return 2
            with db_path_context(database):
                return operation()
    except JobAlreadyRunningError:
        print(f"BLOCKED\t{ticker}\tdcf_writer_busy", file=sys.stderr)
        return 75
    except DcfCommittedCleanupError as exc:
        print(f"COMMITTED\t{ticker}\tdcf_committed_cleanup_failed\t{exc}", file=sys.stderr)
        return 3
    except DcfRecoveryError:
        print(f"BLOCKED\t{ticker}\tdcf_recovery_required", file=sys.stderr)
        return 2


def unique_staged_path(path: Path, purpose: str) -> Path:
    """Allocate an attempt-owned sibling; never remove another attempt's bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f"{path.stem}.{purpose}.{uuid4().hex}{path.suffix}")
    candidates = _ACTIVE_CANDIDATES.get()
    if candidates is not None:
        candidates.append(staged)
    return staged


class DcfRecoveryError(RuntimeError):
    """File compensation failed; retained rollback bytes require recovery."""


class DcfCommittedCleanupError(DcfRecoveryError):
    """The SQL/file write committed; retained backup cleanup failed afterward."""


class ArtifactPromotion(Protocol):
    """A reversible artifact swap owned by the DCF persistence transaction."""

    def apply(self) -> None: ...

    def rollback(self) -> None: ...

    def finalize(self) -> None: ...


class StagedFilePromotion:
    """Swap a staged workbook into its live path with a rollback copy.

    ``apply`` is invoked only after the candidate DCF row has passed its
    promotion gate and its SQL statements have succeeded. ``rollback`` restores
    both paths if the database commit fails; ``finalize`` removes the old bytes
    only after the database commit succeeds.
    """

    def __init__(self, staged_path: Path, live_path: Path) -> None:
        self.staged_path = staged_path
        self.live_path = live_path
        self.backup_path = live_path.with_name(
            f"{live_path.stem}.rollback.{uuid4().hex}{live_path.suffix}"
        )
        self._had_live = False
        self._applied = False

    def apply(self) -> None:
        if self._applied:
            raise RuntimeError("artifact promotion was already applied")
        if not self.staged_path.is_file():
            raise FileNotFoundError(f"staged DCF workbook is missing: {self.staged_path}")
        if self.backup_path.exists():
            raise FileExistsError(f"DCF rollback path already exists: {self.backup_path}")
        self._had_live = self.live_path.is_file()
        if self._had_live:
            os.replace(self.live_path, self.backup_path)
        try:
            os.replace(self.staged_path, self.live_path)
        except Exception:
            if self._had_live and self.backup_path.is_file():
                try:
                    os.replace(self.backup_path, self.live_path)
                except OSError:
                    raise DcfRecoveryError(
                        f"dcf_recovery_failed; retained={self.backup_path}"
                    ) from None
            raise
        self._applied = True

    def rollback(self) -> None:
        if not self._applied:
            return
        if self.live_path.is_file():
            os.replace(self.live_path, self.staged_path)
        if self._had_live and self.backup_path.is_file():
            os.replace(self.backup_path, self.live_path)
        self._applied = False

    def finalize(self) -> None:
        if not self._applied:
            return
        try:
            self.backup_path.unlink(missing_ok=True)
        except OSError:
            raise DcfCommittedCleanupError(
                f"dcf_committed_cleanup_failed; retained={self.backup_path}"
            ) from None
        self._applied = False


class StagedArtifactBundle(contextlib.AbstractContextManager["StagedArtifactBundle"]):
    """Compensate all file swaps if any swap or the owning SQL commit fails.

    The caller enters this only after admission and inside the writer transaction.
    Existing row idempotency can return without an artifact callback, so this
    context also preserves publication when the stored row is unchanged.
    """

    def __init__(self, files: list[tuple[Path, Path]]) -> None:
        self.promotions = [StagedFilePromotion(staged, live) for staged, live in files]

    def __enter__(self) -> StagedArtifactBundle:
        try:
            for promotion in self.promotions:
                promotion.apply()
        except Exception:
            self.rollback()
            raise
        return self

    def rollback(self) -> None:
        failures: list[Path] = []
        for promotion in reversed(self.promotions):
            try:
                promotion.rollback()
            except OSError:
                failures.append(promotion.backup_path)
        if failures:
            raise DcfRecoveryError(
                "dcf_recovery_failed; retained=" + ",".join(map(str, failures))
            ) from None

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if exc_type is not None:
            self.rollback()
        else:
            failures: list[str] = []
            for promotion in self.promotions:
                try:
                    promotion.finalize()
                except DcfCommittedCleanupError as exc:
                    failures.append(str(exc))
            if failures:
                raise DcfCommittedCleanupError("; ".join(failures)) from None


def live_path_from_env(staged_path: Path) -> Path:
    """Resolve the durable workbook locator supplied by a refresh wrapper."""
    raw = os.environ.get("DCF_PROMOTE_DEST")
    if raw is None or not raw.strip():
        return staged_path
    return Path(raw)


def promotion_from_env(staged_path: Path) -> StagedFilePromotion | None:
    """Resolve the live workbook target supplied by a refresh wrapper."""
    live_path = live_path_from_env(staged_path)
    if live_path == staged_path:
        return None
    return StagedFilePromotion(staged_path, live_path)
