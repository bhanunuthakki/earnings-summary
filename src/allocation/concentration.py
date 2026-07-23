"""Concentration-zone policy (PRD ``docs/design/personal_investment_partner_prd.md``
§7.2, P0.2). Pure — no I/O, no DB, no network.

Replaces the old blanket "single name >= 8% -> concentration flag" rule with
five soft zones on POST-TRADE single-name weight (percent of book, 0-100):

    < 10%          ordinary             weight alone creates no concern
    10% to < 12%   meaningful           show risk contribution when adding; no trim implication
    12% to < 15%   concentrated         triggers an explicit hold-versus-trim assessment
    15% to < 20%   highly_concentrated  stronger trim/diversification discussion; high evidence hurdle for adds
    >= 20%         exceptional          full hold/trim/diversify comparison

Ratified rules this module and its callers must never violate:

  1. A zone never FORCES a trim — it only changes what gets discussed/shown.
  2. A trim recommendation needs at least one ADDITIONAL reason beyond zone
     (thesis impairment, poor forward risk/reward, correlation/capacity
     pressure, an affirmed limit, or a superior alternative).
  3. Appreciation-driven drift into a zone gets more tolerance than an
     intentional add — see :func:`classify_entry_method`.
  4. Zone status alone must NOT bypass the behavioral guard's winner
     protection (``advisor.position_review.apply_behavioral_guard`` enforces
     this: reaching a zone through price appreciation, with no intentional
     add, is still not "sizing justified").
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

__all__ = [
    "ENTRY_APPRECIATION",
    "ENTRY_INTENTIONAL",
    "TRIM_ASSESSMENT_THRESHOLD_PCT",
    "ZONE_BOUNDS",
    "Zone",
    "ZoneAssessment",
    "classify_entry_method",
    "classify_zone",
    "zone_at_least",
]

Zone = Literal["ordinary", "meaningful", "concentrated", "highly_concentrated", "exceptional"]

# Ordered (lo_pct, hi_pct_or_None, zone) — lo is inclusive, hi is exclusive;
# None means unbounded above. Order is the canonical zone ranking, walked by
# classify_zone and used to build the zone_at_least ordering.
ZONE_BOUNDS: tuple[tuple[float, float | None, Zone], ...] = (
    (0.0, 10.0, "ordinary"),
    (10.0, 12.0, "meaningful"),
    (12.0, 15.0, "concentrated"),
    (15.0, 20.0, "highly_concentrated"),
    (20.0, None, "exceptional"),
)

# The weight (%) at/above which a hold-versus-trim assessment is required —
# the zone table's "concentrated" floor. This REPLACES the old flat 8%
# single-name concentration bar; ``concentration_flag`` on PreAnalysis now
# means "at/above this threshold", not the old blanket 8%.
TRIM_ASSESSMENT_THRESHOLD_PCT = 12.0

_ZONE_ORDER: tuple[Zone, ...] = tuple(zone for _, _, zone in ZONE_BOUNDS)
# Keyed by plain `str` (not the Literal `Zone`) so `.get()` accepts an
# arbitrary caller-supplied string (e.g. `zone_at_least`'s params) without a
# type-ignore at the lookup site.
_ZONE_RANK: dict[str, int] = {zone: i for i, zone in enumerate(_ZONE_ORDER)}

_HURDLE_BY_ZONE: dict[Zone, Literal["normal", "elevated", "high", "exceptional"]] = {
    "ordinary": "normal",
    "meaningful": "normal",
    "concentrated": "elevated",
    "highly_concentrated": "high",
    "exceptional": "exceptional",
}

_TREATMENT_BY_ZONE: dict[Zone, str] = {
    "ordinary": "weight alone creates no concern",
    "meaningful": "show risk contribution when adding; no trim implication",
    "concentrated": "triggers an explicit hold-versus-trim assessment",
    "highly_concentrated": (
        "stronger trim/diversification discussion; high evidence hurdle for adds"
    ),
    "exceptional": "full hold/trim/diversify comparison",
}

# classify_entry_method's two possible readings of how a name reached its
# current weight — threaded into the guard so a zone reached purely by price
# appreciation earns more tolerance than one reached by an intentional add.
ENTRY_APPRECIATION = "appreciation_drift"
ENTRY_INTENTIONAL = "intentional_add"


@dataclass(frozen=True)
class ZoneAssessment:
    """The zone read for one weight, plus what it implies. Never opinionated
    beyond the PRD §7.2 table itself — no verdict, no trim size."""

    zone: Zone
    weight_pct: float
    # True at/above TRIM_ASSESSMENT_THRESHOLD_PCT (zone "concentrated" or
    # higher) — the signal that an explicit hold-vs-trim assessment is due.
    # This is NOT permission to trim (rule 1); it only says the conversation
    # must happen.
    trim_assessment_required: bool
    add_evidence_hurdle: Literal["normal", "elevated", "high", "exceptional"]
    treatment: str  # one-line human text, verbatim from the PRD §7.2 table


def classify_zone(weight_pct: float | None) -> ZoneAssessment | None:
    """Classify a post-trade single-name weight (percent, 0-100) into its
    PRD §7.2 zone. ``None`` in (no weight known) -> ``None`` out."""
    if weight_pct is None:
        return None
    for lo, hi, zone in ZONE_BOUNDS:
        if weight_pct >= lo and (hi is None or weight_pct < hi):
            return ZoneAssessment(
                zone=zone,
                weight_pct=weight_pct,
                trim_assessment_required=weight_pct >= TRIM_ASSESSMENT_THRESHOLD_PCT,
                add_evidence_hurdle=_HURDLE_BY_ZONE[zone],
                treatment=_TREATMENT_BY_ZONE[zone],
            )
    # weight_pct < 0 never occurs for a real position, but degrade to the
    # lowest zone rather than returning None for an out-of-table value.
    return ZoneAssessment(
        zone="ordinary",
        weight_pct=weight_pct,
        trim_assessment_required=False,
        add_evidence_hurdle="normal",
        treatment=_TREATMENT_BY_ZONE["ordinary"],
    )


def zone_at_least(zone: str, floor: str) -> bool:
    """True when ``zone`` is at/above ``floor`` in the PRD §7.2 ranking
    (ordinary < meaningful < concentrated < highly_concentrated <
    exceptional). Unknown zone names never compare true (fails closed)."""
    z_rank = _ZONE_RANK.get(zone)
    f_rank = _ZONE_RANK.get(floor)
    if z_rank is None or f_rank is None:
        return False
    return z_rank >= f_rank


def classify_entry_method(buy_dates: Sequence[str], *, as_of: date, window_days: int = 180) -> str:
    """How the position reached its current size: an :data:`ENTRY_INTENTIONAL`
    add (a BUY fill within ``window_days`` of ``as_of``) or
    :data:`ENTRY_APPRECIATION` drift (every buy is older than the window, or
    there are none on file). Pure date math over ISO date strings — any
    unparseable entry is skipped rather than raising, so a malformed upstream
    date degrades to "no evidence of a recent buy" instead of crashing.
    """
    floor = as_of - timedelta(days=window_days)
    for raw in buy_dates:
        try:
            d = date.fromisoformat(raw[:10])
        except ValueError:
            continue
        if floor <= d <= as_of:
            return ENTRY_INTENTIONAL
    return ENTRY_APPRECIATION
