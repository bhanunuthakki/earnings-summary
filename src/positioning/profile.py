"""PositioningProfile — the encoded quantitative form of the owner's positioning.

The Pydantic boundary model between the semantic layer (a coach conversation
or the manual form) and the deterministic fit math. Everything in here is a
*target the owner actually expressed* — encoders must leave unexpressed
fields at None rather than inventing defaults, so the resolver
(``positioning/target.py``) can fall back to the current book per-dimension.

Conventions:
- ``target_growth_tilt`` speaks the book's factor vocabulary: QQQ-beta minus
  SPY-beta, the same growth-tilt number ``portfolio_risk.factor_exposure_rollup``
  reports for the held book.
- Sector labels MUST come from the tracker positioning ``by_sector`` taxonomy
  (the closed vocabulary the coach prompt carries) — the fit math compares
  them against ``BookContext.sector_weights`` keys verbatim.
- Weights/fractions are decimals in [0, 1]; ratios/tilts are unit-free.
- ``life_circumstances`` is carried verbatim, never interpreted by code — it
  exists so future coach turns can re-ground on what the owner said.

``profile_json`` in ``positioning_intents`` is only ever written via
``model_dump_json()`` and read via ``model_validate_json()``; a row that no
longer validates is a loud skip (see ``positioning/store.py``), never a
silent guess.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

#: Closed sleeve vocabulary — coarse, cross-sector exposure buckets the owner
#: can set targets for. Kept deliberately small; a sleeve the fit math can't
#: measure is a sleeve the encoder must not emit.
SLEEVE_KEYS: frozenset[str] = frozenset({"intl", "small_cap", "em", "cash"})


class SectorTarget(BaseModel):
    """One sector's target weight (tracker taxonomy label)."""

    sector: str = Field(min_length=1)
    target_weight: float = Field(ge=0.0, le=1.0)  # fraction of the book
    band: float = Field(default=0.05, ge=0.0, le=0.5)  # ± tolerance before it's a gap


class PositioningProfile(BaseModel):
    """The owner's encoded positioning targets. Every field optional —
    unexpressed dimensions default to the current book at resolve time."""

    target_growth_tilt: float | None = Field(default=None, ge=-2.0, le=2.0)
    growth_tilt_band: float = Field(default=0.15, ge=0.0, le=1.0)
    vol_posture: Literal["reduce", "hold", "increase"] | None = None
    target_vol_ann: float | None = Field(default=None, gt=0.0, lt=1.0)  # fraction
    sharpe_floor: float | None = Field(default=None, ge=-1.0, le=5.0)
    sector_targets: list[SectorTarget] = Field(default_factory=list[SectorTarget])
    sleeves: dict[str, float] = Field(default_factory=dict[str, float])  # key -> target fraction
    max_position_weight: float | None = Field(default=None, gt=0.0, le=1.0)
    horizon_years: float | None = Field(default=None, gt=0.0, le=60.0)
    life_circumstances: list[str] = Field(default_factory=list[str])  # verbatim owner notes

    @field_validator("sleeves")
    @classmethod
    def _sleeves_closed_vocab(cls, v: dict[str, float]) -> dict[str, float]:
        unknown = set(v) - SLEEVE_KEYS
        if unknown:
            raise ValueError(
                f"unknown sleeve keys {sorted(unknown)}; allowed: {sorted(SLEEVE_KEYS)}"
            )
        for key, frac in v.items():
            if not 0.0 <= frac <= 1.0:
                raise ValueError(f"sleeve {key!r} target {frac} outside [0, 1]")
        return v

    @field_validator("life_circumstances")
    @classmethod
    def _notes_trimmed(cls, v: list[str]) -> list[str]:
        return [s.strip() for s in v if s.strip()]

    @model_validator(mode="after")
    def _sector_targets_sane(self) -> PositioningProfile:
        seen: set[str] = set()
        total = 0.0
        for st in self.sector_targets:
            label = st.sector.strip()
            if label.lower() in seen:
                raise ValueError(f"duplicate sector target {label!r}")
            seen.add(label.lower())
            total += st.target_weight
        if total > 1.0 + 1e-9:
            raise ValueError(f"sector targets sum to {total:.2f} > 1.0")
        return self

    def is_empty(self) -> bool:
        """True when no quantitative dimension was expressed at all — the
        approval flow rejects these (an empty profile would silently behave
        like 'no intent' while looking like one)."""
        return (
            self.target_growth_tilt is None
            and self.vol_posture is None
            and self.target_vol_ann is None
            and self.sharpe_floor is None
            and not self.sector_targets
            and not self.sleeves
            and self.max_position_weight is None
        )
