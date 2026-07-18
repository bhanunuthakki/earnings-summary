"""Owner-edited per-ticker comparable-set overrides (docs/design/comparable_sets_bottoms_up.md §3.1 Step D).

Same philosophy as ``ir_pipeline.ir_url_overrides.IR_URL_OVERRIDES``: a plain,
version-controlled Python dict, edited by hand — "adding a name = adding one
dict entry." **Never auto-written by code or an LLM.** This is the ONE place
holdco / idiosyncratic comp-set judgment calls live; no generic "detect a
holdco" heuristic is attempted in ``compute.comparable_sets`` (a wrong
auto-classification would silently poison an aggregate, which is worse than
requiring one manual entry per known special case).

``force_include`` / ``force_exclude`` splice directly into the frozen
membership list before it's written (always wins over the rule-ladder
output). ``method_flags`` propagate onto the ``comparable_sets.method_flags``
JSON blob so the metrics layer can skip a metric class for that subject
without a second lookup table.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ComparableSetOverride:
    """One ticker's manual comp-set directive. Every field is optional —
    an entry can pin includes/excludes only, flags only, or both."""

    force_include: tuple[str, ...] = ()
    force_exclude: tuple[str, ...] = ()
    method_flags: dict[str, object] = field(default_factory=dict[str, object])
    note: str = ""


# ticker -> override directive. Any key present here short-circuits or amends
# the rule-ladder output for that ticker. Owner-edited only, per the
# LLM-governance rule (proposals are ratified into this file by a human, same
# pattern as sector_benchmark_map.py).
COMPARABLE_SET_OVERRIDES: dict[str, ComparableSetOverride] = {
    "BN": ComparableSetOverride(
        force_include=("BAM", "BX", "KKR", "APO"),
        force_exclude=(),
        method_flags={
            "whole_co_pe_not_meaningful": True,
            "whole_co_ev_ebitda_not_meaningful": True,
        },
        note=(
            "Holdco — consolidated multiples are noise from minority-interest and "
            "look-through accounting; whole-co PE/EV-EBITDA excluded from aggregates, "
            "see directives/holdco_sotp_model.md for the SOTP alternative."
        ),
    ),
    # NU, RBRK, ... added as the owner reviews Step A/B/C output per ticker.
}


def get_override(ticker: str) -> ComparableSetOverride | None:
    """Return the override for ``ticker`` (case-insensitive), or ``None``."""
    return COMPARABLE_SET_OVERRIDES.get(ticker.upper())
