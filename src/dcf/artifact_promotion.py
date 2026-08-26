"""Compensating file promotion coordinated with a DCF database transaction."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Protocol


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
            f"{live_path.stem}.rollback.{os.getpid()}{live_path.suffix}"
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
                os.replace(self.backup_path, self.live_path)
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
        with contextlib.suppress(OSError):
            self.backup_path.unlink()
        self._applied = False


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
