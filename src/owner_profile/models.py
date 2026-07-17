"""Typed value shapes for ``owner_profile_facts`` (0159).

The table is deliberately heterogeneous — a ``capacity`` fact and a
``behavioral`` fact carry unrelated payload shapes, unlike ``positioning_intents``
(one canonical ``PositioningProfile``). Instead of one giant optional-everything
model, each fact *kind* gets its own small Pydantic model; a producer (the
importer, a ratification route) validates its payload against the matching
model before calling ``store.append_fact`` with the JSON-safe dict
(``model_dump(mode="json")``). ``store.py`` itself stays generic — it persists
and reads back a ``dict[str, object]``, never re-validating against a specific
schema (there is no single schema to check against on read).

Money fields are DELIBERATELY ABSENT from every model below — decision 1
(§7 of docs/design/tenet2_advisory_program.md): the wealthplan import is
derived summaries only (buffers, dates, bucket totals, fractions), never comp,
bonus, equity-comp, or expense-line amounts.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator

FactCategory = Literal["capacity", "appetite", "behavioral"]
FactStatus = Literal["proposed", "affirmed", "rejected"]
FactProvenance = Literal["wealthplan_import", "cio_context_import", "owner", "derived"]

CATEGORIES: frozenset[str] = frozenset({"capacity", "appetite", "behavioral"})
STATUSES: frozenset[str] = frozenset({"proposed", "affirmed", "rejected"})
PROVENANCES: frozenset[str] = frozenset(
    {"wealthplan_import", "cio_context_import", "owner", "derived"}
)

# Tax buckets carried over verbatim from wealthplan's TaxBucket vocabulary
# (src/wealthplan/models.py) — kept as a closed set here too so a schema
# drift in the sibling repo is a loud validation error, not a silent gap.
TAX_BUCKET_KEYS: frozenset[str] = frozenset(
    {"pretax", "roth", "hsa", "taxable", "cash", "illiquid"}
)


class TaxBucketBalances(BaseModel):
    """Household investable balances by tax treatment, as of a snapshot date.
    Totals only — never a comp/contribution figure."""

    balances: dict[str, float] = Field(default_factory=dict[str, float])
    as_of: date

    @field_validator("balances")
    @classmethod
    def _closed_bucket_vocab(cls, v: dict[str, float]) -> dict[str, float]:
        unknown = set(v) - TAX_BUCKET_KEYS
        if unknown:
            raise ValueError(f"unknown tax buckets {sorted(unknown)}")
        for key, amount in v.items():
            if amount < 0:
                raise ValueError(f"bucket {key!r} balance {amount} is negative")
        return v


class EquityFraction(BaseModel):
    """Fraction of the investable book currently in equity (glide input)."""

    equity_fraction: float = Field(ge=0.0, le=1.0)


class CashBufferMonths(BaseModel):
    """Target cash reserve, in months of spend (wealthplan's cash_buffer_months)."""

    months: float = Field(ge=0.0, le=120.0)


class GlidePosture(BaseModel):
    """The equity-weight glide path: accumulation -> retirement posture over a
    de-risking window (wealthplan's GlidePath, minus the cash_floor knob which
    is a policy fraction, not household-specific)."""

    equity_accumulation: float = Field(ge=0.0, le=1.0)
    equity_retirement: float = Field(ge=0.0, le=1.0)
    derisk_years: float = Field(gt=0.0, le=100.0)


class HorizonAges(BaseModel):
    """Retirement-planning horizon ages (wealthplan's RetirementSettings)."""

    target_retirement_age: int = Field(ge=18, le=110)
    horizon_age: int = Field(ge=18, le=120)


class HomeCity(BaseModel):
    """Current home city (wealthplan's Household.home_city)."""

    city: str = Field(min_length=1)


# Dated life-event kinds the importer recognizes, mirroring wealthplan's
# LifeEvent discriminated union (type + label + date ONLY — no amounts).
LifeEventKind = Literal[
    "baby", "buy_house", "move", "work_break", "startup", "exit_payout", "career_change"
]


class LifeEventFact(BaseModel):
    """One dated life event: what it is, when, nothing about its dollar size."""

    kind: LifeEventKind
    label: str = Field(min_length=1)
    date: date
    end_date: date | None = None  # work_break / startup windows only
    person: str | None = None  # 'person_a' | 'person_b', when the event names one


class ParentCareWindow(BaseModel):
    """Parent-care support window, keyed by the owner's age (wealthplan's
    ParentCareEvent uses ages, not calendar dates) — no annual amount."""

    label: str = Field(min_length=1)
    start_age: int = Field(ge=0, le=120)
    end_age: int = Field(ge=0, le=120)


class HumanCapitalBucket(BaseModel):
    """One human-capital correlation bucket + its aggregate cap, imported from
    the tracker's CIO_CONTEXT.local.md prose table."""

    cap_pct: float = Field(ge=0.0, le=100.0)
    members: list[str] = Field(default_factory=list[str])

    @field_validator("members")
    @classmethod
    def _tickers_upper(cls, v: list[str]) -> list[str]:
        return [t.strip().upper() for t in v if t.strip()]


__all__ = [
    "CATEGORIES",
    "PROVENANCES",
    "STATUSES",
    "TAX_BUCKET_KEYS",
    "CashBufferMonths",
    "EquityFraction",
    "FactCategory",
    "FactProvenance",
    "FactStatus",
    "GlidePosture",
    "HomeCity",
    "HorizonAges",
    "HumanCapitalBucket",
    "LifeEventFact",
    "LifeEventKind",
    "ParentCareWindow",
    "TaxBucketBalances",
]
