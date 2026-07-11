"""Materialized cache for the naked-position gate (Monthly Red Team Phase 1,
guard 7) — ``data/dashboard/position_guard.json``.

``position_guard.build_position_guard`` is cheap (a handful of read-only
SQLite queries + small JSON file reads), but the directive's contract is that
the nightly pipeline computes it ONCE and every render reads the cache — the
same discipline ``portfolio_weights`` and ``factor_proxies`` already apply, so
a render is never the thing paying for (or blocking on) a DB read. Pydantic
validates the payload at both write and read boundaries (this repo's Layer 3
rule: every execution-script output is a typed, validated shape, never a bare
dict), and the write is atomic (temp file + ``os.replace``) so a reader never
observes a partially-written cache.

Last-good semantics match ``portfolio_weights.materialize_weights``: the
writer always has a fresh report to write (``build_position_guard`` degrades
to an empty report rather than raising), so there is no "skip on failure"
branch here — the nightly stage script is what decides whether a build
exception should leave the last-good file untouched (see
``execution/refresh_position_guard.py``).
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from position_guard import PositionGuardCheck, PositionGuardReport, PositionGuardRow

__all__ = [
    "PositionGuardCacheModel",
    "PositionGuardCheckModel",
    "PositionGuardRowModel",
    "cache_path",
    "read_position_guard_cache",
    "to_cache_model",
    "write_position_guard_cache",
]

_CACHE_REL: tuple[str, ...] = ("data", "dashboard", "position_guard.json")


def cache_path(repo_root: Path) -> Path:
    return repo_root.joinpath(*_CACHE_REL)


class PositionGuardCheckModel(BaseModel):
    passed: bool
    reason: str


class PositionGuardRowModel(BaseModel):
    ticker: str
    weight_pct: float
    passed: bool
    failed_checks: list[str]
    downside_trigger: PositionGuardCheckModel
    realistic_bear: PositionGuardCheckModel
    thesis_fresh: PositionGuardCheckModel


class PositionGuardCacheModel(BaseModel):
    """The persisted payload. ``computed_at`` is a naive-UTC ISO stamp (project
    convention). ``rows`` preserves ``build_position_guard``'s violations-first
    ordering exactly, so a reader never needs to re-sort."""

    computed_at: str
    rows: list[PositionGuardRowModel]

    @property
    def violations(self) -> list[PositionGuardRowModel]:
        return [r for r in self.rows if not r.passed]


def _check_model(c: PositionGuardCheck) -> PositionGuardCheckModel:
    return PositionGuardCheckModel(passed=c.passed, reason=c.reason)


def _row_model(r: PositionGuardRow) -> PositionGuardRowModel:
    return PositionGuardRowModel(
        ticker=r.ticker,
        weight_pct=r.weight_pct,
        passed=r.passed,
        failed_checks=list(r.failed_checks),
        downside_trigger=_check_model(r.downside_trigger),
        realistic_bear=_check_model(r.realistic_bear),
        thesis_fresh=_check_model(r.thesis_fresh),
    )


def to_cache_model(
    report: PositionGuardReport, *, computed_at: datetime | None = None
) -> PositionGuardCacheModel:
    stamp = computed_at or datetime.now(UTC).replace(tzinfo=None)
    return PositionGuardCacheModel(
        computed_at=stamp.isoformat(), rows=[_row_model(r) for r in report.rows]
    )


def write_position_guard_cache(
    repo_root: Path, report: PositionGuardReport, *, computed_at: datetime | None = None
) -> Path:
    """Validate + persist atomically (temp file + ``os.replace``), mirroring
    ``portfolio_weights.materialize_weights``'s write idiom. Always writes —
    an empty ``report.rows`` (no weighted holdings) is a legitimate cache
    state, not a skip condition; the caller decides whether a build failure
    should skip the write entirely (last-good file left untouched)."""
    model = to_cache_model(report, computed_at=computed_at)
    path = cache_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix="position_guard.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(model.model_dump_json())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return path


def read_position_guard_cache(repo_root: Path) -> PositionGuardCacheModel | None:
    """The materialized report, or ``None`` when the cache is absent,
    unreadable, or fails schema validation (a stale pre-migration shape) —
    the render path degrades to the empty state rather than crashing, the
    same tolerance every other materialized-cache reader in this repo has."""
    path = cache_path(repo_root)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        payload: object = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return PositionGuardCacheModel.model_validate(payload)
    except Exception:  # pydantic.ValidationError + any other schema drift
        return None
