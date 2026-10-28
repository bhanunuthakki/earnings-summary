"""Typed value shapes for retained owner process settings and rules.

The profile store remains generic. It persists JSON objects and preserves old
versions. This module validates the retained next-dollar settings and explicit
behavioral rules at their write boundaries.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

FactCategory = Literal["capacity", "appetite", "behavioral"]
FactStatus = Literal["proposed", "affirmed", "rejected"]
FactProvenance = Literal["wealthplan_import", "cio_context_import", "owner", "derived"]

CATEGORIES: frozenset[str] = frozenset({"capacity", "appetite", "behavioral"})
STATUSES: frozenset[str] = frozenset({"proposed", "affirmed", "rejected"})
PROVENANCES: frozenset[str] = frozenset(
    {"wealthplan_import", "cio_context_import", "owner", "derived"}
)


class NextDollarBlendWeights(BaseModel):
    """Owner-authored next-dollar factor blend (appetite tier) — owner decision
    5 (§7 of tenet2_advisory_program.md): "Profile-driven weights from the
    appetite tier, plus a cash-aware mode. Hardcoded 50/30/20 becomes the
    labeled no-profile fallback only." Keys mirror ``allocation.model``'s
    factor ids exactly (``ret``/``div``/``macro``); FRACTIONS, not percentages,
    and must sum to ~1.0 (a per-holding renormalization still happens downstream
    when a factor is hidden model-wide for lack of data — that logic is
    unaffected by which set of weights fed it)."""

    ret: float = Field(ge=0.0, le=1.0)
    div: float = Field(ge=0.0, le=1.0)
    macro: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _sums_to_one(self) -> NextDollarBlendWeights:
        total = self.ret + self.div + self.macro
        if abs(total - 1.0) > 0.01:
            raise ValueError(f"blend weights must sum to ~1.0, got {total:.4f}")
        return self


class BehavioralRule(BaseModel):
    """Stored owner rule with cited decision IDs and recorded outcome counts.

    Existing affirmed owner facts retain this value shape after automatic
    behavioral-rule generation is retired.
    """

    rule_text: str = Field(min_length=1)
    citations: list[int] = Field(default_factory=list[int])
    wrong: int = Field(ge=0)
    total: int = Field(ge=0)

    @model_validator(mode="after")
    def _tallies_are_consistent(self) -> BehavioralRule:
        if self.wrong > self.total:
            raise ValueError(f"wrong ({self.wrong}) cannot exceed total ({self.total})")
        if self.total != len(set(self.citations)):
            raise ValueError(
                f"total ({self.total}) must equal the distinct citation count "
                f"({len(set(self.citations))})"
            )
        return self


__all__ = [
    "CATEGORIES",
    "PROVENANCES",
    "STATUSES",
    "BehavioralRule",
    "FactCategory",
    "FactProvenance",
    "FactStatus",
    "NextDollarBlendWeights",
]
