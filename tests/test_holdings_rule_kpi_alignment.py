"""Guard against dead break-rules in the re-encoded holdings.

A break-rule / business-model-rule references a KPI by ``kpi_name``. At
evaluation time ``thesis_evaluator._fetch_kpi_history`` resolves that label
through ``kpi_resolver`` (a parenthetical-insensitive, normalized match) against
the stored ``kpi_definitions``. Those definitions only ever get populated from
two sources:

  * ``compute.fmp_derived_kpis`` — metrics computed from ``financial_facts``
    (the ``_DERIVED_KPI_NAMES`` tuple), and
  * ``compute.kpi_extract_summaries`` — operational KPIs the LLM extracts, which
    reads the ``tier_1/2/3`` KPI *names* from the same holdings file.

So a rule whose ``kpi_name`` normalizes to neither a derived metric nor a tier
KPI in its own file can never resolve: it silently evaluates OK forever (a dead
rule). This test pins that invariant for the seven holdings re-encoded from the
user's thesis blurbs (2026-06-01).

The six pre-existing dormant rules in the populated RBRK/NOW/WIX files reference
metrics that need a dedicated extractor/deriver not built yet (tracked in the
deferred-followups memory); they are pinned in ``NARRATIVE_ALLOWLIST``. Pinning
the exact set makes the test fail if a NEW dead rule is introduced *or* if an
allowlisted one is later wired (shrinking the set) — either way forcing a
deliberate update rather than letting a dead rule slip in unnoticed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from compute.fmp_derived_kpis import (
    KPI_CAPEX_REVENUE_RATIO,
    KPI_FCF_MARGIN_GAAP,
    KPI_GROSS_MARGIN_GAAP,
    KPI_NET_MARGIN_GAAP,
    KPI_OCF_YOY_USD,
    KPI_OPERATING_MARGIN_GAAP,
    KPI_REVENUE_YOY_USD,
    KPI_ROE,
)
from compute.kpi_resolver import normalize_kpi_name

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOLDINGS_DIR = PROJECT_ROOT / "micro_thesis" / "holdings"

# The seven holdings re-encoded from the user's thesis blurbs (2026-06-01).
REENCODED_TICKERS = ("RBRK", "NOW", "WIX", "VEEV", "MELI", "NVO", "BN")

# De-stubbed this pass (plus NVO, already populated) — these must carry zero dead rules.
CLEAN_TICKERS = ("VEEV", "MELI", "NVO", "BN")

_TIER_KEYS = ("tier_1_kpis", "tier_2_kpis", "tier_3_kpis")
_RULE_KEYS = ("break_rules", "business_model_rules")

# Canonical FMP-derived KPI names (see compute.fmp_derived_kpis). Listed via the
# public constants rather than the module-private tuple; if a derived KPI is added
# there, add its constant here too.
_DERIVED_KPI_NAMES = (
    KPI_OPERATING_MARGIN_GAAP,
    KPI_NET_MARGIN_GAAP,
    KPI_GROSS_MARGIN_GAAP,
    KPI_REVENUE_YOY_USD,
    KPI_CAPEX_REVENUE_RATIO,
    KPI_FCF_MARGIN_GAAP,
    KPI_OCF_YOY_USD,
    KPI_ROE,
)
_DERIVED_NORM = {normalize_kpi_name(name) for name in _DERIVED_KPI_NAMES}

# Pre-existing dormant rules in the populated files: their ``kpi_name`` needs a
# dedicated extractor/deriver not built yet (see the deferred-followups memory).
# Pinned so a NEW dead rule fails the test and a future fix forces an update.
NARRATIVE_ALLOWLIST: dict[str, set[str]] = {
    "RBRK": {"Customers >$100K ARR YoY Growth", "SBC / Subscription Revenue"},
    "NOW": {"Now Assist >$1M ACV YoY Growth", "Customers >$5M ACV YoY Growth"},
    "WIX": {"Wix Studio ARR YoY Growth", "Partners Revenue Share"},
}


def _load(ticker: str) -> dict[str, object]:
    raw = json.loads((HOLDINGS_DIR / f"{ticker}.json").read_text(encoding="utf-8"))
    return cast("dict[str, object]", raw)


def _field_values(spec: dict[str, object], keys: tuple[str, ...], field: str) -> list[str]:
    """Collect ``field`` from every dict item under ``keys`` (JSON-boundary safe)."""
    out: list[str] = []
    for key in keys:
        section = spec.get(key)
        if not isinstance(section, list):
            continue
        for raw in cast("list[object]", section):
            if not isinstance(raw, dict):
                continue
            value = cast("dict[str, object]", raw).get(field)
            if isinstance(value, str):
                out.append(value)
    return out


def _producible_norms(spec: dict[str, object]) -> set[str]:
    """Normalized names a rule can resolve to: derived metrics + this file's tier KPIs."""
    norms = set(_DERIVED_NORM)
    for name in _field_values(spec, _TIER_KEYS, "name"):
        norms.add(normalize_kpi_name(name))
    return norms


def _rule_kpi_names(spec: dict[str, object]) -> list[str]:
    return _field_values(spec, _RULE_KEYS, "kpi_name")


def _unresolved(spec: dict[str, object]) -> set[str]:
    producible = _producible_norms(spec)
    return {kn for kn in _rule_kpi_names(spec) if normalize_kpi_name(kn) not in producible}


@pytest.mark.parametrize("ticker", REENCODED_TICKERS)
def test_rule_kpi_names_resolve_or_are_allowlisted(ticker: str) -> None:
    unresolved = _unresolved(_load(ticker))
    allowed = NARRATIVE_ALLOWLIST.get(ticker, set())
    assert unresolved == allowed, (
        f"{ticker}: unresolved rule kpi_names {sorted(unresolved)} != "
        f"allowlist {sorted(allowed)}. A rule whose kpi_name resolves to neither a "
        f"derived metric nor a tier KPI evaluates OK forever (dead rule). Name a "
        f"matching tier_1 KPI / derived metric, or — if it needs a new extractor — "
        f"add it to NARRATIVE_ALLOWLIST and the deferred-followups memory."
    )


def test_destubbed_and_nvo_have_no_dead_rules() -> None:
    for ticker in CLEAN_TICKERS:
        assert not _unresolved(_load(ticker)), (
            f"{ticker} (de-stubbed/clean) must have no dead rules"
        )


def test_meli_fintech_rules_resolve() -> None:
    """The MELI fintech KPIs the user asked to wire must resolve to a tier KPI."""
    producible = _producible_norms(_load("MELI"))
    for kpi_name in (
        "NIMAL (net interest margin after losses)",
        "Credit portfolio 90+ day NPL",
        "ROE",
        "MercadoPago TPV growth (FXN)",
    ):
        assert normalize_kpi_name(kpi_name) in producible, (
            f"MELI rule KPI {kpi_name!r} resolves to no tier KPI / derived metric"
        )


def test_allowlist_is_honest() -> None:
    """Every pinned name must still be a present, unresolved rule kpi_name."""
    for ticker, names in NARRATIVE_ALLOWLIST.items():
        spec = _load(ticker)
        rule_names = set(_rule_kpi_names(spec))
        producible = _producible_norms(spec)
        for name in names:
            assert name in rule_names, (
                f"{ticker}: allowlisted {name!r} is no longer a rule kpi_name — "
                f"remove it from NARRATIVE_ALLOWLIST"
            )
            assert normalize_kpi_name(name) not in producible, (
                f"{ticker}: allowlisted {name!r} now resolves — remove it from NARRATIVE_ALLOWLIST"
            )
