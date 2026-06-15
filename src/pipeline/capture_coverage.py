"""Capture-run coverage log — make the silent-drop blind spot measurable.

The capture-every-number program's whole premise is that numbers were being
*silently dropped* (the allowlist path ignored everything off the watchlist; the
deterministic walker defers rate/percent/per-share rows). A capture pass is only
trustworthy if every number it SAW is accounted for — captured, or deferred /
dropped for a NAMED reason. This module persists that accounting to
``data/capture_coverage_log.json`` so coverage is queryable across runs rather
than scrolling past in a log line.

One :class:`CaptureCoverageRecord` is appended per (stage, ticker, source) run:
  - ``seen``               — atomic numbers the pass examined
  - ``captured``           — landed in kpi_facts
  - ``dropped_validation`` — handed to persist_manifest but dropped by its
                             PLAUSIBLE_RANGE / UNIT_MISMATCH guards
  - ``skipped``            — ``{reason: count}`` for everything deferred before
                             persist (per-row guards, off-cycle columns, …)

Invariant the producers maintain: ``captured + dropped_validation +
sum(cell-level skipped) == seen`` (section-level ``section_*`` skips defer whole
sections before their cells are counted, so they sit outside the cell tally).

Read it back with :func:`load_coverage` / roll it up with :func:`summarize`
(the ``execution/capture_coverage_report.py`` CLI prints that summary).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

_LOG_REL = Path("data") / "capture_coverage_log.json"


class CaptureCoverageRecord(BaseModel):
    """One capture run's coverage accounting."""

    stage: str  # "xbrl_capture" (Stage A) | "enumerate_capture" (Stage B)
    ticker: str
    source: str  # e.g. "10k", "ir", "earnings"
    seen: int
    captured: int
    dropped_validation: int = 0
    skipped: dict[str, int] = Field(default_factory=dict[str, int])
    fiscal_year: int | None = None
    captured_at: str = ""  # ISO-Z; stamped by record_coverage when blank


def _log_path(repo_root: Path) -> Path:
    return repo_root / _LOG_REL


def load_coverage(repo_root: Path) -> list[CaptureCoverageRecord]:
    """Load all coverage records. Returns [] when the log is absent or unreadable
    (a corrupt log must not crash a capture run or a report)."""
    path = _log_path(repo_root)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning({"event": "coverage_log_read_failed", "path": str(path), "error": str(exc)})
        return []
    if not isinstance(raw, list):
        return []
    out: list[CaptureCoverageRecord] = []
    for item in cast("list[object]", raw):
        if not isinstance(item, dict):
            continue
        try:
            out.append(CaptureCoverageRecord.model_validate(item))
        except ValueError:
            continue
    return out


def record_coverage(repo_root: Path, record: CaptureCoverageRecord) -> None:
    """Append ``record`` to the coverage log (stamping ``captured_at`` when blank).

    Best-effort: a write failure is logged, never raised — coverage telemetry must
    not break the capture run that produced it.
    """
    if not record.captured_at:
        record = record.model_copy(
            update={
                "captured_at": datetime.now(UTC)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z")
            }
        )
    try:
        existing = load_coverage(repo_root)
        existing.append(record)
        path = _log_path(repo_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps([r.model_dump() for r in existing], indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning({"event": "coverage_log_write_failed", "error": str(exc)})


def summarize(records: list[CaptureCoverageRecord]) -> dict[str, object]:
    """Roll records up into headline coverage totals + per-stage + top skip reasons.

    ``capture_rate`` is captured / seen (0.0 when nothing was seen). The skip
    histogram surfaces WHERE the long tail is being deferred — the actionable
    signal for whether Stage A's guards are too aggressive or Stage B is needed.
    """
    seen = sum(r.seen for r in records)
    captured = sum(r.captured for r in records)
    dropped = sum(r.dropped_validation for r in records)
    skip_hist: dict[str, int] = {}
    by_stage: dict[str, dict[str, int]] = {}
    for r in records:
        for reason, n in r.skipped.items():
            skip_hist[reason] = skip_hist.get(reason, 0) + n
        bucket = by_stage.setdefault(r.stage, {"seen": 0, "captured": 0, "dropped_validation": 0})
        bucket["seen"] += r.seen
        bucket["captured"] += r.captured
        bucket["dropped_validation"] += r.dropped_validation
    return {
        "runs": len(records),
        "seen": seen,
        "captured": captured,
        "dropped_validation": dropped,
        "capture_rate": round(captured / seen, 4) if seen else 0.0,
        "by_stage": by_stage,
        "skipped": dict(sorted(skip_hist.items(), key=lambda kv: (-kv[1], kv[0]))),
    }
