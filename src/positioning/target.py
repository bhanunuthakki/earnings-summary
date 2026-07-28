"""Resolve the target book — the single seam Fit v2 and the panel consume.

``resolve_target_context`` answers "what book is the owner steering toward?"
in one deterministic read:

  * an authoritative intent exists → its expressed targets, with every
    UNEXPRESSED dimension falling back to the current book reading
    (per-dimension, not all-or-nothing);
  * no intent (or a pre-0145 substrate, or an undecodable row) →
    ``source="book_default"``: every target equals the current book.

The book-default guarantee is load-bearing: targets equal to current
readings mean zero gaps, so target-relative fit factors score neutral and
the fit numbers are bit-identical to the pre-positioning behavior. Tests pin
this equivalence.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from identity import DEFAULT_USER_ID
from positioning.store import latest_intent
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

if TYPE_CHECKING:
    from allocation.candidate_fit import BookContext


@dataclass(frozen=True, slots=True)
class TargetContext:
    """The resolved target book the fit factors compare candidates against."""

    growth_tilt: float | None
    growth_tilt_band: float
    #: Current-book sector weights overlaid with the intent's explicit
    #: targets — a sector with no expressed target 'targets' its current
    #: weight (zero gap by construction).
    sector_weights: dict[str, float] = field(default_factory=dict[str, float])
    #: Sectors the owner explicitly set, with their ± bands. Only these can
    #: produce a target gap; everything else is book-default.
    sector_bands: dict[str, float] = field(default_factory=dict[str, float])
    sleeves: dict[str, float] = field(default_factory=dict[str, float])  # explicit sleeves only
    vol_posture: Literal["reduce", "hold", "increase"] | None = None
    target_vol_ann: float | None = None
    sharpe_floor: float | None = None
    max_position_weight: float | None = None
    source: Literal["intent", "book_default"] = "book_default"
    intent_id: int | None = None
    intent_created_at: str | None = None
    narrative: str | None = None


def book_default_target(book: BookContext) -> TargetContext:
    """The no-intent target: the current book is the target — zero gaps."""
    return TargetContext(
        growth_tilt=book.growth_tilt,
        growth_tilt_band=0.15,
        sector_weights=dict(book.sector_weights),
        source="book_default",
    )


def resolve_target_context(
    db_path: Path, book: BookContext, *, user_id: str = DEFAULT_USER_ID
) -> TargetContext:
    """The active target book. Never raises — any read trouble resolves to
    the book default (today's behavior)."""
    if not Path(db_path).exists():
        return book_default_target(book)
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return book_default_target(book)
    try:
        intent = latest_intent(conn, user_id=user_id)
    except sqlite3.Error:
        return book_default_target(book)
    finally:
        conn.close()
    if intent is None:
        return book_default_target(book)

    profile = intent.profile
    sector_weights = dict(book.sector_weights)
    sector_bands: dict[str, float] = {}
    for st in profile.sector_targets:
        sector_weights[st.sector] = st.target_weight
        sector_bands[st.sector] = st.band
    return TargetContext(
        growth_tilt=(
            profile.target_growth_tilt
            if profile.target_growth_tilt is not None
            else book.growth_tilt
        ),
        growth_tilt_band=profile.growth_tilt_band,
        sector_weights=sector_weights,
        sector_bands=sector_bands,
        sleeves=dict(profile.sleeves),
        vol_posture=profile.vol_posture,
        target_vol_ann=profile.target_vol_ann,
        sharpe_floor=profile.sharpe_floor,
        max_position_weight=profile.max_position_weight,
        source="intent",
        intent_id=intent.id,
        intent_created_at=intent.created_at,
        narrative=intent.narrative,
    )
