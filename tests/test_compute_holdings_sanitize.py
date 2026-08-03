"""Tests for src/compute/holdings_sanitize.py — scalar hygiene for the
micro_thesis holdings JSON (scalars plain, prose fields keep markdown)."""

from __future__ import annotations

import copy

from compute.holdings_sanitize import sanitize_holdings_scalars


def _dirty_payload() -> dict[str, object]:
    return {
        "ticker": "NU",
        "thesis": "The bet is **local deposit funding** compounding.\n\n## Killers\n...",
        "chart_priorities": [
            "**Priority #1 — Mexico momentum**",
            "Risk-adj. NIM",
        ],
        "competitive_watchlist": ["**MELI**", "Inter&Co"],
        "peer_exclude": ["`PAGS`"],
        "thesis_breakers_qualitative": [
            "**Regulatory cap on interchange**",
            {"free": "**shape**"},
        ],
        "tier_1_kpis": [
            {
                "name": "**Risk-adj. NIM**",
                "break_condition": "sustains below **8%** for 2Q",
                "status": "ok",
            }
        ],
        "break_rules": [
            {
                "rule_id": "roe_floor",
                "kpi_name": "__ROE__",
                "comparator": "lt",
                "threshold": 25,
                "narrative": "ROE below 25% breaks the **compounding** leg.",
            }
        ],
    }


def test_scalar_fields_are_stripped_in_place() -> None:
    payload = _dirty_payload()
    changed = sanitize_holdings_scalars(payload)
    assert payload["chart_priorities"] == [
        "Priority #1 — Mexico momentum",
        "Risk-adj. NIM",
    ]
    assert payload["competitive_watchlist"] == ["MELI", "Inter&Co"]
    assert payload["peer_exclude"] == ["PAGS"]
    breakers = payload["thesis_breakers_qualitative"]
    assert isinstance(breakers, list)
    assert breakers[0] == "Regulatory cap on interchange"
    tier1 = payload["tier_1_kpis"]
    assert isinstance(tier1, list) and isinstance(tier1[0], dict)
    assert tier1[0]["name"] == "Risk-adj. NIM"
    rules = payload["break_rules"]
    assert isinstance(rules, list) and isinstance(rules[0], dict)
    assert rules[0]["kpi_name"] == "ROE"
    assert set(changed) == {
        "chart_priorities[0]",
        "competitive_watchlist[0]",
        "peer_exclude[0]",
        "thesis_breakers_qualitative[0]",
        "tier_1_kpis[0].name",
        "break_rules[0].kpi_name",
    }


def test_prose_fields_and_free_shapes_are_untouched() -> None:
    payload = _dirty_payload()
    sanitize_holdings_scalars(payload)
    # thesis prose keeps its markdown (render_prose owns that boundary).
    assert payload["thesis"] == _dirty_payload()["thesis"]
    tier1 = payload["tier_1_kpis"]
    assert isinstance(tier1, list) and isinstance(tier1[0], dict)
    assert tier1[0]["break_condition"] == "sustains below **8%** for 2Q"
    rules = payload["break_rules"]
    assert isinstance(rules, list) and isinstance(rules[0], dict)
    assert rules[0]["narrative"] == "ROE below 25% breaks the **compounding** leg."
    # Dict entries in the free-shape breaker list are left alone.
    breakers = payload["thesis_breakers_qualitative"]
    assert isinstance(breakers, list)
    assert breakers[1] == {"free": "**shape**"}


def test_clean_payload_is_a_no_op() -> None:
    payload = _dirty_payload()
    sanitize_holdings_scalars(payload)
    snapshot = copy.deepcopy(payload)
    assert sanitize_holdings_scalars(payload) == []
    assert payload == snapshot


def test_missing_and_odd_shaped_fields_are_tolerated() -> None:
    assert sanitize_holdings_scalars({}) == []
    payload: dict[str, object] = {
        "chart_priorities": "not-a-list",
        "tier_1_kpis": [None, "not-a-dict"],
        "break_rules": {"not": "a list"},
    }
    assert sanitize_holdings_scalars(payload) == []
