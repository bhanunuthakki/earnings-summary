"""KPI polarity table + adverse-direction derivation (auto-seed correctness core).

This is correctness mechanism #2 for the auto KPI seeder (see
``scratch/plans/auto_kpi_seeding_plan.md`` §5). The KPI-inflection trigger
escalates a thesis-breaker only when the registered ``threshold_direction``
points the *adverse* way — i.e. it fires when a load-bearing KPI deteriorates,
not when it improves. Get the direction wrong and a breaker fires on good news
and stays silent on the real break (catastrophic, per the plan).

The robust judgment to ask a model for is *"is higher or lower better for this
KPI?"* (its polarity), NOT *"which comparator should fire?"*. Code derives the
comparator deterministically from polarity via :func:`adverse_direction`, so the
LLM never sets the alert direction directly. For well-known metrics the
deterministic :data:`KNOWN_KPI_POLARITY` table is authoritative and overrides
the model entirely.

Pure module — no LLM, no DB, no CLI dependency — so it is importable, strict-
typed, and unit-tested in ``tests/test_kpi_polarity.py``. The trigger could
later import these helpers to validate registry rows at scan time.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal


class Polarity(StrEnum):
    """Whether a higher or lower value of a KPI is good for the thesis.

    String values match the JSON the auto-seed LLM emits in its ``polarity``
    field, so an LLM response value round-trips straight into this enum via
    ``Polarity(raw)``.
    """

    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"


# Substring-keyed, lowercased match against the KPI name. Authoritative for the
# known KPIs the portfolio actually tracks; the auto-seed gate prefers this
# table over the LLM's self-reported polarity and routes any table-vs-LLM
# conflict to human review (plan §5). Keys are lowercase substrings — a KPI
# name matches when any key is contained in its lowercased form, so
# "Net Interest Margin (consolidated)" matches "net interest margin".
KNOWN_KPI_POLARITY: dict[str, Polarity] = {
    # higher-is-better
    "net interest margin": Polarity.HIGHER_IS_BETTER,
    "nim": Polarity.HIGHER_IS_BETTER,
    "net dollar retention": Polarity.HIGHER_IS_BETTER,
    "ndr": Polarity.HIGHER_IS_BETTER,
    "net revenue retention": Polarity.HIGHER_IS_BETTER,
    "gross margin": Polarity.HIGHER_IS_BETTER,
    "operating margin": Polarity.HIGHER_IS_BETTER,
    "free cash flow": Polarity.HIGHER_IS_BETTER,
    "fcf margin": Polarity.HIGHER_IS_BETTER,
    "gmv": Polarity.HIGHER_IS_BETTER,
    "arr": Polarity.HIGHER_IS_BETTER,
    "take rate": Polarity.HIGHER_IS_BETTER,
    "roe": Polarity.HIGHER_IS_BETTER,
    "return on equity": Polarity.HIGHER_IS_BETTER,
    # lower-is-better
    "churn": Polarity.LOWER_IS_BETTER,
    "npl": Polarity.LOWER_IS_BETTER,
    "non-performing": Polarity.LOWER_IS_BETTER,
    "cost of risk": Polarity.LOWER_IS_BETTER,
    "cac payback": Polarity.LOWER_IS_BETTER,
    "leverage": Polarity.LOWER_IS_BETTER,
    "net charge-off": Polarity.LOWER_IS_BETTER,
    "default rate": Polarity.LOWER_IS_BETTER,
}


def infer_polarity_from_table(kpi_name: str) -> Polarity | None:
    """Polarity of ``kpi_name`` from :data:`KNOWN_KPI_POLARITY`, or ``None``.

    Lowercases the name and returns the polarity of the first matching
    substring key (insertion order — higher-is-better entries are listed
    first). Returns ``None`` when no key matches, signalling the caller to
    fall back to the LLM's polarity (gated on confidence, per plan §5).
    """
    lowered = kpi_name.lower()
    for substring, polarity in KNOWN_KPI_POLARITY.items():
        if substring in lowered:
            return polarity
    return None


def adverse_direction(polarity: Polarity) -> Literal["above", "below"]:
    """Map a KPI's polarity to the comparator that fires on deterioration.

    THE #1 rule (plan §5): adverse = the OPPOSITE of good.

      * ``HIGHER_IS_BETTER`` deteriorates when it falls  -> ``"below"``
        (e.g. NIM, NDR, FCF margin fire when they drop under a floor).
      * ``LOWER_IS_BETTER`` deteriorates when it rises   -> ``"above"``
        (e.g. churn, NPL, CAC payback fire when they exceed a ceiling).

    The returned strings match the registry's ``threshold_direction``
    vocabulary consumed by ``triggers.kpi_inflection._crosses_threshold``.
    """
    if polarity is Polarity.HIGHER_IS_BETTER:
        return "below"
    return "above"
