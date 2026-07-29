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
    # Bottoms-up comparable-set resolution (docs/design/
    # comparable_sets_bottoms_up.md §8) — execution/build_comparable_sets.py's
    # per-ticker rule-ladder resolve + freeze. Additive member; no existing
    # StageName usage is exhaustive-matched over the enum (grepped — every
    # caller either does `record_stage(..., StageName.X, ...)` for a specific
    # named stage or reads/compares a stored value), so adding this cannot
    # break an existing match.
    COMPARABLE_SET_RESOLVE = "comparable_set_resolve"


class StageStatus(StrEnum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    OK = "ok"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"
    SKIPPED = "skipped"
    ABANDONED = "abandoned"


class IngestionRun(BaseModel):
    """One full pipeline run (typically one ticker x one period x one directive)."""

    id: int | None = None
    run_id: str
    # ``run_id`` remains the compatibility alias for the concrete attempt.
    # The logical id survives retries and is added by migration 0211.
    pipeline_key: str | None = None
    attempt_id: str | None = None
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
    attempt_id: str | None = None
    ticker: str
    period_end: datetime | None
    stage: StageName
    status: StageStatus
    started_at: datetime
    ended_at: datetime | None
    error_msg: str | None
