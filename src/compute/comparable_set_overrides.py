"""Curated per-ticker comparable-set overrides (docs/design/comparable_sets_bottoms_up.md
section 3.1 Step D).

Same pattern as ``src/ir_pipeline/ir_url_overrides.py``: a plain, version-controlled
Python dict, hand-edited by the owner, never auto-written by code or an LLM. This is
the ONE place holdco/idiosyncratic judgment calls live for comparable-set membership
-- no generic "detect a holdco" heuristic is attempted (too fragile; a wrong
auto-classification silently poisons an aggregate).

``force_include``/``force_exclude`` splice directly into the rule-ladder output
(``compute.comparable_sets.resolve_comparable_set``) before it's frozen:
``force_exclude`` always wins over anything Step A/B/C resolved (the owner's veto),
``force_include`` adds a member with ``membership_reason='pinned_override'``.
``method_flags`` propagate verbatim onto the frozen ``comparable_sets.method_flags``
JSON so the aggregate math (``compute.comp_set_metrics``) can skip a metric class for
that subject without a second lookup table.

Adding a name = adding one dict entry.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ComparableSetOverride:
    """One ticker's manual comparable-set correction.

    ``force_include`` / ``force_exclude`` are ticker lists (uppercased by the
    resolver, not here, so this module stays a pure data file). ``method_flags``
    is a small flat dict of booleans propagated onto the frozen set's
    ``method_flags`` JSON blob -- e.g. ``{"whole_co_pe_not_meaningful": True}``
    tells the aggregate math to skip PE/EV-EBITDA for this subject's own set
    without a second lookup table.
    """

    force_include: list[str] = field(default_factory=list[str])
    force_exclude: list[str] = field(default_factory=list[str])
    method_flags: dict[str, bool] = field(default_factory=dict[str, bool])
    note: str = ""


# ticker -> override directive. Owner-edited only, per the LLM-governance rule
# (proposals from Step C are ratified into the frozen set automatically unless
# vetoed here; pinned membership itself is never LLM-proposed).
COMPARABLE_SET_OVERRIDES: dict[str, ComparableSetOverride] = {
    "BN": ComparableSetOverride(
        force_include=["BAM", "BX", "KKR", "APO"],
        force_exclude=[],
        method_flags={
            "whole_co_pe_not_meaningful": True,
            "whole_co_ev_ebitda_not_meaningful": True,
        },
        note=(
            "Holdco -- consolidated multiples are noise from minority-interest and "
            "look-through accounting; whole-co PE/EV-EBITDA excluded from aggregates, "
            "see directives/holdco_sotp_model.md for the SOTP alternative."
        ),
    ),
    # NU, RBRK, ... added as the owner reviews Step A/B/C output per ticker.
}


def get_override(ticker: str) -> ComparableSetOverride | None:
    """The override for ``ticker``, or ``None`` if it's never been pinned."""
    return COMPARABLE_SET_OVERRIDES.get(ticker.upper())
