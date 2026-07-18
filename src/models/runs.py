"""Pipeline run accounting — every script execution writes rows here."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel


class StageName(StrEnum):
    INGEST = "ingest"
    TRANSCRIBE = "transcribe"
    PARSE = "parse"
    VALIDATE = "validate"
    PERSIST = "persist"
    COMPUTE = "compute"
    SYNTHESIZE = "synthesize"
    PUBLISH = "publish"
    # Bottoms-up comparable-set resolution/freeze (docs/design/
    # comparable_sets_bottoms_up.md section 8) -- distinct from COMPUTE because
    # it's a resolve-and-freeze step (membership), not a numeric derivation;
    # the per-date aggregate math in track_comp_metrics.py uses COMPUTE.
    COMPARABLE_SET_RESOLVE = "comparable_set_resolve"


class StageStatus(StrEnum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    OK = "ok"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"
    SKIPPED = "skipped"


class IngestionRun(BaseModel):
    """One full pipeline run (typically one ticker x one period x one directive)."""

    id: int | None = None
    run_id: str
    started_at: datetime
    ended_at: datetime | None
    directive: str
    ticker_scope: list[str]
    status: StageStatus
    error_summary: str | None


class StageTransition(BaseModel):
    """A single stage's status within a run; resumption point on failure."""

    id: int | None = None
    run_id: str
    ticker: str
    period_end: datetime | None
    stage: StageName
    status: StageStatus
    started_at: datetime
    ended_at: datetime | None
    error_msg: str | None
