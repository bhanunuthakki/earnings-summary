"""Tests for the Opus-driven DCF assumption normalizer.

The LLM call is mocked — these cover the parse, cache, and apply-to-workbook
plumbing (no model is called), plus the contract that an applied grid round-trips
back out through `read_inputs_from_sheet`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import openpyxl
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import llm.cli  # noqa: E402
from dcf import forecast, llm_assumptions  # noqa: E402

_HORIZON = 5


def _make_workbook(tmp_path: Path) -> Path:
    """A workbook with just a Forecast sheet holding a flat-default INPUTS grid."""
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Forecast"
    flat = [0.10] * _HORIZON
    inputs = forecast.ForecastInputs(
        base_revenue_M=1000.0,
        diluted_shares_M=100.0,
        terminal_multiple=15.0,
        forecast_years=_HORIZON,
        revenue_growth_pct=flat,
        gross_margin_pct=[0.50] * _HORIZON,
        rnd_pct=flat,
        sga_pct=[0.20] * _HORIZON,
        sbc_pct=[0.05] * _HORIZON,
        da_pct=[0.04] * _HORIZON,
        capex_to_da=[1.2] * _HORIZON,
        dso_days=[45.0] * _HORIZON,
        dpo_days=[30.0] * _HORIZON,
        deferred_rev_pct=[0.0] * _HORIZON,
        tax_rate_pct=[0.25] * _HORIZON,
        net_share_change_pct=[0.0] * _HORIZON,
    )
    forecast.write_inputs_section(ws, inputs)
    path = tmp_path / "TEST.xlsx"
    wb.save(str(path))
    return path


def _sample_response() -> str:
    # Revenue growth fades; gross margin expands; capex/D&A normalizes to ~1.0.
    drivers = {
        "revenue_growth": {"values": [0.18, 0.14, 0.10, 0.07, 0.04], "note": "fade to GDP+"},
        "gross_margin": {"values": [0.52, 0.54, 0.56, 0.58, 0.60], "note": "mix lift"},
        "rnd_pct": {"values": [0.14, 0.13, 0.12, 0.11, 0.10], "note": "op leverage"},
        "sga_pct": {"values": [0.19, 0.18, 0.17, 0.16, 0.15], "note": "op leverage"},
        "sbc_pct": {"values": [0.06, 0.05, 0.05, 0.04, 0.04], "note": "moderates"},
        "da_pct": {"values": [0.05, 0.05, 0.05, 0.05, 0.05], "note": "stable"},
        "capex_to_da": {"values": [1.8, 1.5, 1.3, 1.1, 1.0], "note": "buildout normalizes"},
        "dso_days": {"values": [40, 40, 40, 40, 40], "note": "structural"},
        "dpo_days": {"values": [35, 35, 35, 35, 35], "note": "structural"},
        "deferred_rev_pct": {"values": [0.08, 0.08, 0.08, 0.08, 0.08], "note": "SaaS float"},
        "tax_rate": {"values": [0.22, 0.22, 0.22, 0.22, 0.22], "note": "statutory"},
        "net_share_change": {"values": [0.015, 0.01, 0.01, 0.005, 0.0], "note": "dilution fades"},
    }
    return (
        "```json\n"
        + json.dumps({"narrative": "A sensible normalization.", "drivers": drivers})
        + "\n```"
    )


def test_parse_response_full() -> None:
    a = llm_assumptions.parse_response(_sample_response(), "TEST", _HORIZON)
    assert a.ticker == "TEST"
    assert len(a.drivers) == 12
    assert a.drivers["revenue_growth"] == [0.18, 0.14, 0.10, 0.07, 0.04]
    assert a.drivers["capex_to_da"][-1] == 1.0
    assert a.narrative == "A sensible normalization."
    assert a.notes["gross_margin"] == "mix lift"


def test_parse_response_drops_incomplete_driver() -> None:
    obj = {
        "narrative": "x",
        "drivers": {
            "revenue_growth": {"values": [0.1, 0.1, 0.1, 0.1, 0.1]},
            "gross_margin": {"values": [0.5, 0.5]},  # too short — dropped
        },
    }
    a = llm_assumptions.parse_response(json.dumps(obj), "TEST", _HORIZON)
    assert "revenue_growth" in a.drivers
    assert "gross_margin" not in a.drivers


def test_parse_response_no_json_raises() -> None:
    with pytest.raises(llm_assumptions.LlmAssumptionError):
        llm_assumptions.parse_response("sorry, no data", "TEST", _HORIZON)


def test_generate_caches_and_applies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wb = _make_workbook(tmp_path)

    def _fake_call_llm(prompt: str, **_kw: object) -> str:
        assert "DCF" in prompt and "best practice" in prompt.lower()
        return _sample_response()

    monkeypatch.setattr(llm.cli, "call_llm", _fake_call_llm)

    a = llm_assumptions.generate_assumptions("TEST", wb, tmp_path)
    assert len(a.drivers) == 12
    # Cached to disk for review + re-runs.
    assert llm_assumptions.cache_path(tmp_path, "TEST").exists()

    # A second call with no force uses the cache (call_llm would assert-fail if hit
    # with a different prompt, but here we just confirm it returns the same grid).
    a2 = llm_assumptions.generate_assumptions("TEST", wb, tmp_path)
    assert a2.drivers["revenue_growth"] == a.drivers["revenue_growth"]

    # Apply writes the grid into the Forecast INPUTS; it round-trips back out.
    applied = llm_assumptions.apply_to_workbook(wb, a)
    assert applied == 12
    book = openpyxl.load_workbook(str(wb), data_only=True)
    back = forecast.read_inputs_from_sheet(book["Forecast"])
    assert back.revenue_growth_pct == pytest.approx([0.18, 0.14, 0.10, 0.07, 0.04])
    assert back.gross_margin_pct[-1] == pytest.approx(0.60)
    assert back.capex_to_da[-1] == pytest.approx(1.0)
    # Scalars are preserved (only the per-year grid changed).
    assert back.base_revenue_M == pytest.approx(1000.0)
    assert back.diluted_shares_M == pytest.approx(100.0)
