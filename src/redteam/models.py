"""Shared types for the Red Team engine.

``RedTeamLLMItem`` is the structured-output schema at the LLM/JSON boundary
(validated by pydantic per the repo's Layer-3 discipline). ``RedTeamItemRow``
is the persisted-row shape read back from ``red_team_items`` — a plain
dataclass, matching the repo's established DB-row convention (e.g.
``user_state.thesis_status.ThesisStatusRow``, ``portfolio_correlation.CorrelationRead``)
rather than re-validating already-typed SQLite output through pydantic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["high", "med", "low"]
Kind = Literal["per_name", "cross_book"]
Status = Literal["open", "refuted", "accepted", "deferred", "closed"]

LENS_NAMES: tuple[str, ...] = (
    "shared_factor",
    "fx_translation",
    "competitive_encroachment",
    "model_vs_market",
    "behavioral_consistency",
)

CROSS_BOOK_LENS_NAMES: tuple[str, ...] = (
    "factor_block",
    "style_drift",
    "human_capital",
)


class RedTeamLLMItem(BaseModel):
    """The LLM's structured attack — one falsifiable claim, one question, one
    proposed concrete rule/scenario change. Validated at the JSON boundary
    immediately after ``call_llm_structured`` parses the envelope."""

    attack_md: str = Field(min_length=1)
    question_md: str = Field(min_length=1)
    proposed_change_md: str = Field(min_length=1)
    severity: Severity


@dataclass(slots=True, frozen=True)
class RedTeamItemRow:
    """One persisted ``red_team_items`` row."""

    id: int
    run_key: str
    ticker: str | None
    lens: str
    kind: Kind
    attack_md: str
    question_md: str
    proposed_change_md: str
    severity: Severity
    status: Status
    defer_count: int
    response_md: str | None
    responded_at: datetime | None
    created_at: datetime
