"""Tests for the macro scenario registry and the series registry."""

from __future__ import annotations

from macro_scenarios import SCENARIOS, all_scenario_ids, get
from macro_series import (
    REGISTRY,
    all_series_ids,
    by_category,
)
from macro_series import (
    get as get_series,
)

EXPECTED_SCENARIO_IDS = {
    "fed_cuts_50bps",
    "fed_hikes_25bps",
    "usd_brl_up_15pct",
    "oil_to_50",
    "copper_doubles",
    "recession_2026",
}

EXPECTED_SERIES_IDS = {
    "fed_funds",
    "us_10y",
    "vix",
    "usd_brl",
    "usd_inr",
    "usd_eur",
    "usd_cad",
    "usd_twd",
    "brent",
    "copper",
    "gold",
    "sox",
}


def test_all_six_scenarios_present() -> None:
    assert set(SCENARIOS.keys()) == EXPECTED_SCENARIO_IDS
    assert set(all_scenario_ids()) == EXPECTED_SCENARIO_IDS


def test_scenarios_have_well_formed_shocks() -> None:
    for sid, scen in SCENARIOS.items():
        assert scen.id == sid, f"{sid}: id mismatch"
        assert scen.title, f"{sid}: missing title"
        assert scen.shocks, f"{sid}: must have at least one shock"
        for shock in scen.shocks:
            assert shock.series_id in REGISTRY, (
                f"{sid} shocks {shock.series_id} which is not in the series registry"
            )
            assert shock.unit in ("bps", "pct", "absolute"), f"{sid}: invalid unit {shock.unit}"
            assert shock.direction in ("up", "down"), f"{sid}: invalid direction {shock.direction}"
            assert shock.magnitude >= 0  # magnitude is non-negative; direction carries sign


def test_get_returns_scenario_or_none() -> None:
    assert get("fed_cuts_50bps") is SCENARIOS["fed_cuts_50bps"]
    assert get("does_not_exist") is None


def test_series_registry_has_all_twelve() -> None:
    assert set(REGISTRY.keys()) == EXPECTED_SERIES_IDS
    assert set(all_series_ids()) == EXPECTED_SERIES_IDS


def test_series_categories_partition() -> None:
    """Every series belongs to exactly one of rates/fx/commodity/index, and
    counts match the expected layout from the spec."""
    by_cat = {
        "rates": {s.series_id for s in by_category("rates")},
        "fx": {s.series_id for s in by_category("fx")},
        "commodity": {s.series_id for s in by_category("commodity")},
        "index": {s.series_id for s in by_category("index")},
    }
    assert by_cat["rates"] == {"fed_funds", "us_10y"}
    assert by_cat["fx"] == {"usd_brl", "usd_inr", "usd_eur", "usd_cad", "usd_twd"}
    assert by_cat["commodity"] == {"brent", "copper", "gold"}
    assert by_cat["index"] == {"vix", "sox"}
    # Disjoint partition
    union: set[str] = set()
    for cat_set in by_cat.values():
        assert union.isdisjoint(cat_set), "category sets must be disjoint"
        union |= cat_set
    assert union == EXPECTED_SERIES_IDS


def test_series_providers_well_formed() -> None:
    """Each series must declare at least one provider; each provider must
    name an FMP endpoint kind and a path. Sanity-only — we don't ground-
    truth against FMP itself."""
    for sid, spec in REGISTRY.items():
        assert spec.providers, f"{sid}: no providers configured"
        assert get_series(sid) is spec
        for prov in spec.providers:
            assert prov.kind == "yfinance" or prov.kind.startswith("fmp_"), (
                f"{sid} provider kind {prov.kind!r} should be yfinance or fmp_*"
            )
            assert prov.path, f"{sid} provider has empty path"
            assert prov.date_key
            assert prov.value_key


def test_recession_scenario_covers_multiple_dimensions() -> None:
    """Smoke that recession_2026 — the composite scenario — touches more than
    just rates. This is a sanity tripwire if the scenario gets accidentally
    simplified later."""
    scen = SCENARIOS["recession_2026"]
    series_ids_in_scenario = {shock.series_id for shock in scen.shocks}
    categories_touched = {REGISTRY[sid].category for sid in series_ids_in_scenario}
    # Should hit at least 3 of the 4 macro categories.
    assert len(categories_touched) >= 3, (
        f"recession_2026 only touches {categories_touched} — should span more dimensions"
    )
