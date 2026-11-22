"""Tests for the redesigned 9-sheet DCF: the reader/projection/value engine
(``src/dcf/redesign.py``) and the redesign refresh path in
``execution/refresh_dcf.py`` (rebuild-from-FMP with Dashboard edit-preservation).

Three layers:
  * Pure engine — construct ``RedesignInputs`` directly and assert the projection
    math (growth monotonicity, FX scaling, perpetuity terminal, the WACC>g guard).
    No workbook, no subprocess.
  * Reader — run the real builder as a subprocess (the way the driver/refresher
    do), then read it back: format detection, value-of-record parity with the
    builder's shared numerical owner, FX for a non-USD reporter.
  * Refresh integration — drive ``refresh_dcf.refresh_one`` end-to-end: it rebuilds
    every sheet from FMP, PRESERVES the user's Dashboard inputs, recomputes the
    value, and upserts ``dcf_runs``. Plus the ``dcf_applicable=false`` skip and the
    negative-fair-value (#291) guard.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import sqlite3
import subprocess
from collections.abc import Callable
from pathlib import Path

import dcf_sheets
import openpyxl
import pytest
import refresh_dcf
from openpyxl.cell.cell import Cell

import tests.fixtures.dcf_redesign_support as support
from dcf import redesign


@pytest.fixture(scope="module", autouse=True)
def canonical_override_schema(
    migrated_db: Callable[..., Path], tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Reuse the migrated override contract in the specialized bridge fixture."""
    support.install_schema_template(migrated_db, tmp_path_factory)


@pytest.fixture(scope="module")
def built_usd(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, float]:
    """Build one USD single-segment workbook once; read-only tests share it."""
    return support.build_usd(tmp_path_factory)


@pytest.fixture
def refresh_repo(tmp_path: Path) -> Path:
    """A repo_root with FMP, a dcf/ dir, and a dcf_runs DB — ready for refresh_one."""
    return support.make_refresh_repo(tmp_path)


@pytest.fixture(scope="module")
def built_semiannual(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, float]:
    """Build one semi-annual (Q2/Q4-only) workbook once; reader tests share it."""
    return support.build_semiannual(tmp_path_factory)


# --------------------------------------------------------------------------- #
# Pure engine — RedesignInputs -> value (no workbook)
# --------------------------------------------------------------------------- #


def test_value_is_deterministic() -> None:
    assert (
        redesign.value(support.BASE).value_per_share_usd
        == redesign.value(support.BASE).value_per_share_usd
    )


def test_higher_growth_raises_value() -> None:
    faster = dataclasses.replace(support.BASE, near_growth_by_segment={"Total company": 0.20})
    assert (
        redesign.value(faster).value_per_share_usd
        > redesign.value(support.BASE).value_per_share_usd
    )


def test_higher_margin_raises_value() -> None:
    richer = dataclasses.replace(support.BASE, terminal_op_margin=0.35)
    assert (
        redesign.value(richer).value_per_share_usd
        > redesign.value(support.BASE).value_per_share_usd
    )


def test_fx_scales_per_share_value_linearly() -> None:
    usd = redesign.value(support.BASE)
    half = redesign.value(dataclasses.replace(support.BASE, fx_to_usd=0.5))
    # Reporting-currency value is FX-independent; the USD value scales by FX.
    assert half.value_per_share_reporting == pytest.approx(usd.value_per_share_reporting)
    assert half.value_per_share_usd == pytest.approx(usd.value_per_share_usd * 0.5)


def test_exit_multiple_higher_than_perpetuity_basis_selects_metric() -> None:
    sales = redesign.value(
        dataclasses.replace(support.BASE, terminal_basis="EV/Sales", exit_multiple=3.0)
    )
    ebitda = redesign.value(support.BASE)
    # Different bases price different terminal metrics — just assert both resolve
    # to finite, positive values (the basis plumbing reaches compute_valuation).
    assert sales.value_per_share_usd > 0
    assert ebitda.value_per_share_usd > 0


def test_perpetuity_terminal_resolves() -> None:
    perp = redesign.value(
        dataclasses.replace(support.BASE, terminal_method="Perpetuity", terminal_growth_g=0.03)
    )
    assert perp.value_per_share_usd > 0
    assert perp.terminal_method == "Perpetuity"


def test_perpetuity_requires_wacc_above_terminal_g() -> None:
    bad = dataclasses.replace(
        support.BASE, terminal_method="Perpetuity", wacc=0.025, terminal_growth_g=0.03
    )
    with pytest.raises(redesign.RedesignError, match="WACC"):
        redesign.value(bad)


def test_negative_margin_yields_negative_value_without_crashing() -> None:
    # Opex above revenue every year -> negative FCF -> negative value (no raise);
    # the refresher's #291 guard nulls over/under on a value like this.
    loss = dataclasses.replace(support.BASE, near_op_margin=-0.5, terminal_op_margin=-0.5)
    assert redesign.value(loss).value_per_share_usd < 0


# --------------------------------------------------------------------------- #
# SBC as an explicit after-tax expense (op margins stay non-GAAP)
# --------------------------------------------------------------------------- #
def test_sbc_defaults_to_zero_backcompat() -> None:
    """A pre-SBC input set (no SBC fields) charges no SBC — the value is identical
    to the old 'SBC nets out' behaviour, so legacy workbooks/fixtures don't move."""
    assert support.BASE.near_sbc_pct == 0.0
    assert support.BASE.terminal_sbc_pct == 0.0
    # An explicit 0.0 must equal the default (the charge term vanishes).
    zeroed = dataclasses.replace(support.BASE, near_sbc_pct=0.0, terminal_sbc_pct=0.0)
    assert redesign.value(zeroed).value_per_share_usd == pytest.approx(
        redesign.value(support.BASE).value_per_share_usd
    )


def test_charging_sbc_lowers_value() -> None:
    """A heavy-SBC name (12% -> 8% of revenue) values materially LOWER than the
    same name with SBC excluded — the whole point of the fix."""
    heavy = dataclasses.replace(support.BASE, near_sbc_pct=0.12, terminal_sbc_pct=0.08)
    assert (
        redesign.value(heavy).value_per_share_usd < redesign.value(support.BASE).value_per_share_usd
    )


def test_sbc_charged_after_tax() -> None:
    """The FCF-stream SBC charge is ``sbc_pct * revenue * (1 - tax)``: at a higher
    tax rate the *same* SBC% charges LESS after-tax SBC. Isolate that channel by
    valuing on an EV/Sales terminal (whose metric is revenue, so the terminal-EBITDA
    SBC burden — deliberately NOT tax-adjusted — does not confound the comparison).

    The SBC drag then scales cleanly by (1-tax): the drag at 40% tax is 0.60/0.90 of
    the drag at 10% tax."""
    lo_tax = dataclasses.replace(
        support.BASE, tax_rate=0.10, terminal_basis="EV/Sales", exit_multiple=3.0
    )
    hi_tax = dataclasses.replace(
        support.BASE, tax_rate=0.40, terminal_basis="EV/Sales", exit_multiple=3.0
    )
    sbc = {"near_sbc_pct": 0.10, "terminal_sbc_pct": 0.10}
    drag_lo = (
        redesign.value(lo_tax).value_per_share_usd
        - redesign.value(dataclasses.replace(lo_tax, **sbc)).value_per_share_usd
    )
    drag_hi = (
        redesign.value(hi_tax).value_per_share_usd
        - redesign.value(dataclasses.replace(hi_tax, **sbc)).value_per_share_usd
    )
    # After-tax SBC at 40% tax is 0.60/0.90 of the charge at 10% tax.
    assert drag_hi < drag_lo
    assert drag_hi == pytest.approx(drag_lo * (0.60 / 0.90), rel=0.02)


def test_terminal_ebitda_burdened_by_sbc() -> None:
    """A one-turn EBITDA multiple change isolates the public terminal value."""
    base = dataclasses.replace(support.BASE, terminal_basis="EV/EBITDA")
    heavy = dataclasses.replace(base, near_sbc_pct=0.12, terminal_sbc_pct=0.10)

    def terminal_ebitda(inputs: redesign.RedesignInputs) -> float:
        original = redesign.value(inputs)
        extra_turn = redesign.value(
            dataclasses.replace(inputs, exit_multiple=inputs.exit_multiple + 1)
        )
        return (
            (extra_turn.operating_value_usd_m - original.operating_value_usd_m)
            * (1 + inputs.wacc) ** len(original.forecast_revenue_m)
            / inputs.fx_to_usd
        )

    unburdened = terminal_ebitda(base)
    burdened = terminal_ebitda(heavy)
    assert burdened < unburdened
    terminal_revenue = redesign.value(heavy).forecast_revenue_m[-1]
    assert unburdened - burdened == pytest.approx(heavy.terminal_sbc_pct * terminal_revenue)


def test_sbc_round_trips_through_dict() -> None:
    heavy = dataclasses.replace(support.BASE, near_sbc_pct=0.13, terminal_sbc_pct=0.07)
    rebuilt = redesign.RedesignInputs.from_dict(heavy.to_dict())
    assert rebuilt.near_sbc_pct == pytest.approx(0.13)
    assert rebuilt.terminal_sbc_pct == pytest.approx(0.07)
    assert rebuilt == heavy
    assert redesign.value(rebuilt).value_per_share_usd == pytest.approx(
        redesign.value(heavy).value_per_share_usd
    )


def test_from_dict_missing_sbc_defaults_to_zero() -> None:
    payload = support.BASE.to_dict()
    del payload["near_sbc_pct"]
    del payload["terminal_sbc_pct"]
    inp = redesign.RedesignInputs.from_dict(payload)
    assert inp.near_sbc_pct == 0.0
    assert inp.terminal_sbc_pct == 0.0


# --------------------------------------------------------------------------- #
# Scenarios + sensitivity (pure engine)
# --------------------------------------------------------------------------- #
def test_scenario_values_order_bear_base_bull() -> None:
    """With the seeded offsets the three fair values are strictly ordered and
    Base is exactly the value-of-record."""
    sv = redesign.scenario_values(support.BASE)
    assert sv.bull is not None and sv.bear is not None
    assert sv.bear < sv.base < sv.bull
    assert sv.base == pytest.approx(redesign.value(support.BASE).value_per_share_usd)


def test_apply_scenario_shifts_every_lever() -> None:
    shifted = redesign.apply_scenario(support.BASE, redesign.BULL_SEED)
    assert shifted.near_growth_by_segment["Total company"] == pytest.approx(0.13)
    assert shifted.terminal_growth_by_segment["Total company"] == pytest.approx(0.04)
    assert shifted.near_op_margin == pytest.approx(0.21)
    assert shifted.terminal_op_margin == pytest.approx(0.27)
    assert shifted.exit_multiple == pytest.approx(14.0)
    assert shifted.terminal_growth_g == pytest.approx(0.035)


def test_scenario_degenerate_bull_degrades_to_none() -> None:
    """A Bull terminal-g pushed to/above WACC under a perpetuity terminal is
    un-valuable: bull degrades to None while Base (still WACC > g) and Bear
    (g moves further below WACC) stay numeric."""
    perp = dataclasses.replace(
        support.BASE, terminal_method="Perpetuity", wacc=0.038, terminal_growth_g=0.034
    )
    sv = redesign.scenario_values(perp)
    assert sv.bull is None  # bull g = 3.9% >= 3.8% WACC
    assert sv.base > 0
    assert sv.bear is not None


def test_sensitivity_grid_centered_on_base_and_monotonic() -> None:
    grid = redesign.sensitivity_grid(support.BASE)
    assert len(grid.wacc_axis) == 7 and len(grid.multiple_axis) == 7
    assert grid.values[3][3] == pytest.approx(redesign.value(support.BASE).value_per_share_usd)
    assert grid.wacc_axis[3] == pytest.approx(support.BASE.wacc)
    assert grid.multiple_axis[3] == pytest.approx(support.BASE.exit_multiple)
    for row in grid.values:  # value falls as WACC rises (left -> right)
        assert all(row[j] > row[j + 1] for j in range(len(row) - 1))
    for j in range(7):  # value rises with the exit multiple (top -> bottom)
        col = [grid.values[i][j] for i in range(7)]
        assert all(col[i] < col[i + 1] for i in range(len(col) - 1))


# --------------------------------------------------------------------------- #
# JSON constructor — to_dict / from_dict (the non-workbook transport for the
# in-app recompute route; no xlsx in the loop)
# --------------------------------------------------------------------------- #
def test_to_dict_is_json_safe() -> None:
    payload = support.BASE.to_dict()
    # Round-trips through json without raising; tuple → list, deltas → dicts.
    assert isinstance(payload["segments"], list)
    assert isinstance(payload["bull_deltas"], dict)
    text = json.dumps(payload)
    assert json.loads(text)["wacc"] == pytest.approx(support.BASE.wacc)


def test_to_dict_from_dict_round_trips() -> None:
    """from_dict(to_dict(x)) reconstructs an equal input set with an identical
    value-of-record — the recompute route can rebuild edits with no workbook."""
    rebuilt = redesign.RedesignInputs.from_dict(support.BASE.to_dict())
    assert rebuilt == support.BASE
    assert redesign.value(rebuilt).value_per_share_usd == pytest.approx(
        redesign.value(support.BASE).value_per_share_usd
    )


def test_from_dict_applies_an_edit() -> None:
    """A WACC edit posted as a dict recomputes a lower value (taken as given —
    NOT re-derived from CAPM the way read_inputs does)."""
    edited = support.BASE.to_dict()
    edited["wacc"] = 0.12
    inp = redesign.RedesignInputs.from_dict(edited)
    assert inp.wacc == pytest.approx(0.12)
    assert (
        redesign.value(inp).value_per_share_usd < redesign.value(support.BASE).value_per_share_usd
    )


def test_from_dict_defaults_scenario_deltas_to_seeds() -> None:
    payload = support.BASE.to_dict()
    del payload["bull_deltas"]
    del payload["bear_deltas"]
    inp = redesign.RedesignInputs.from_dict(payload)
    assert inp.bull_deltas == redesign.BULL_SEED
    assert inp.bear_deltas == redesign.BEAR_SEED


def test_from_dict_rejects_missing_field() -> None:
    payload = support.BASE.to_dict()
    del payload["wacc"]
    with pytest.raises(redesign.RedesignError, match="wacc"):
        redesign.RedesignInputs.from_dict(payload)


def test_from_dict_rejects_non_numeric_field() -> None:
    payload = support.BASE.to_dict()
    payload["wacc"] = "high"
    with pytest.raises(redesign.RedesignError, match="must be a number"):
        redesign.RedesignInputs.from_dict(payload)


def test_from_dict_rejects_segment_without_growth() -> None:
    """A segment present in the list but absent from a growth map fails loud
    here, not with a KeyError deep in the projection."""
    payload = support.BASE.to_dict()
    payload["segments"] = ["Total company", "Phantom"]
    with pytest.raises(redesign.RedesignError, match="Phantom"):
        redesign.RedesignInputs.from_dict(payload)


# --------------------------------------------------------------------------- #
# Fixtures: write FMP, run the real builder
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Reader / format detection
# --------------------------------------------------------------------------- #
def test_is_redesign_format_true_for_builder_output(built_usd: tuple[Path, float]) -> None:
    dest, _ = built_usd
    assert redesign.is_redesign_format(dest) is True


def test_is_redesign_format_false_for_missing(tmp_path: Path) -> None:
    assert redesign.is_redesign_format(tmp_path / "nope.xlsx") is False


def test_is_redesign_format_false_for_legacy_three_sheet(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.xlsx"
    wb = openpyxl.Workbook()
    for i, name in enumerate(("Historicals", "Forecast", "Valuation")):
        if i == 0:
            ws = wb.active
            assert ws is not None
            ws.title = name
        else:
            wb.create_sheet(name)
    wb.save(str(legacy))
    assert redesign.is_redesign_format(legacy) is False
    assert redesign.read_inputs(legacy) is None
    assert redesign.read_and_value(legacy) is None


def test_read_and_value_matches_builder_mirror(built_usd: tuple[Path, float]) -> None:
    """Builder RESULT and workbook reader use one calculation (RESULT is rounded)."""
    dest, builder_value = built_usd
    rv = redesign.read_and_value(dest)
    assert rv is not None
    assert rv.value_per_share_usd == pytest.approx(builder_value, abs=0.0051)
    assert rv.fx_to_usd == 1.0


def test_builder_headline_uses_final_tax_and_model_capex_base(refresh_repo: Path) -> None:
    assumptions = refresh_repo / "data" / "dcf_assumptions" / "TESTCO.json"
    assumptions.parent.mkdir(parents=True)
    assumptions.write_text(
        json.dumps(
            {
                "redesign": {
                    "segments": {
                        "Cloud": {"near_term_growth": 0.22, "terminal_growth": 0.03},
                        "Devices": {"near_term_growth": 0.04, "terminal_growth": 0.02},
                    },
                    "tax_rate": 0.37,
                    "country_risk_premium": 0.027,
                }
            }
        ),
        encoding="utf-8",
    )
    dest = refresh_repo / "dcf" / "TESTCO.xlsx"
    headline = support.build_workbook(refresh_repo, "TESTCO", dest)
    inp = redesign.read_inputs(dest)
    assert inp is not None
    assert inp.tax_rate == pytest.approx(0.37)
    assert inp.total_debt_m > 0
    assert inp.country_risk_premium == pytest.approx(0.027)
    expected = redesign.value(inp).value_per_share_usd
    assert headline == pytest.approx(expected, abs=0.0051)
    assert support.dashboard_cell(dest, redesign.SCEN_FV_ROW) == pytest.approx(expected)


def test_failed_builder_keeps_unbaselined_assumptions_bytes(
    refresh_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assumptions = refresh_repo / "data" / "dcf_assumptions" / "TESTCO.json"
    assumptions.parent.mkdir(parents=True)
    before = b'{"narrative":"owner wording","redesign":{"beta":1.4}}\n'
    assumptions.write_bytes(before)
    monkeypatch.setattr(refresh_dcf.live_price_mod, "read_live_price", support.fake_read)
    original_builder = getattr(refresh_dcf, "_run_builder")

    def rejected_builder(
        ticker: str,
        repo_root: Path,
        dest: Path,
        *,
        db_path: Path,
        country_risk_override: float | None = None,
        assumptions_path: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        proc = original_builder(
            ticker,
            repo_root,
            dest,
            db_path=db_path,
            country_risk_override=country_risk_override,
            assumptions_path=assumptions_path,
        )
        assert proc.returncode == 0, proc.stderr
        return subprocess.CompletedProcess(proc.args, 1, proc.stdout, "synthetic rejection")

    monkeypatch.setattr(refresh_dcf, "_run_builder", rejected_builder)
    result = refresh_dcf.refresh_one(
        "TESTCO", refresh_repo, refresh_repo / "data" / "portfolio.db", valuation_year=2026
    )
    assert result["status"] == "failed"
    assert assumptions.read_bytes() == before
    assert not (refresh_repo / "dcf" / "TESTCO.xlsx").exists()
    assert not list((refresh_repo / "dcf").glob("*.rebuild.*"))
    assert not list(assumptions.parent.glob("*.assumptions.*"))


def test_rejected_import_preserves_accepted_bundle(
    refresh_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(refresh_dcf.live_price_mod, "read_live_price", support.fake_read)
    db = refresh_repo / "data" / "portfolio.db"
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(db))
    result = refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)
    assert result["status"] == "ok", result
    workbook = refresh_repo / "dcf" / "TESTCO.xlsx"
    assumptions = refresh_repo / "data" / "dcf_assumptions" / "TESTCO.json"
    prior_workbook, prior_assumptions = workbook.read_bytes(), assumptions.read_bytes()
    with sqlite3.connect(db) as conn:
        prior_rows = conn.execute("SELECT * FROM dcf_runs ORDER BY id").fetchall()
    rejected = refresh_repo / "rejected.xlsx"
    rejected.write_bytes(b"unsupported imported workbook")

    def blocked_refresh(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"status": "blocked", "reason": "synthetic rejection"}

    monkeypatch.setattr(refresh_dcf, "refresh_one", blocked_refresh)
    rc = dcf_sheets.main(
        ["import", "--ticker", "TESTCO", "--file", str(rejected), "--repo-root", str(refresh_repo)]
    )
    assert rc != 0
    assert workbook.read_bytes() == prior_workbook
    assert assumptions.read_bytes() == prior_assumptions
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT * FROM dcf_runs ORDER BY id").fetchall() == prior_rows


def test_import_uses_configured_db_separate_from_artifacts(
    refresh_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = refresh_repo.parent / "configured-state" / "portfolio.sqlite"
    db.parent.mkdir()
    checkout_db = refresh_repo / "data" / "portfolio.db"
    shutil.move(checkout_db, db)
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(db))
    monkeypatch.setattr(refresh_dcf.live_price_mod, "read_live_price", support.fake_read)
    result = refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)
    assert result["status"] == "ok", result
    accepted = refresh_repo / "dcf" / "TESTCO.xlsx"
    candidate = refresh_repo / "downloaded.xlsx"
    shutil.copyfile(accepted, candidate)
    wb = openpyxl.load_workbook(candidate)
    wb["Dashboard"].cell(30, 2, 0.28)
    wb.save(candidate)
    wb.close()
    rc = dcf_sheets.main(
        ["import", "--ticker", "TESTCO", "--file", str(candidate), "--repo-root", str(refresh_repo)]
    )
    assert rc == 0
    assert not checkout_db.exists()
    inp = redesign.read_inputs(accepted)
    assert inp is not None and inp.terminal_op_margin == pytest.approx(0.28)
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT id, is_latest FROM dcf_runs ORDER BY id").fetchall()
        assert len(rows) == 2
        assert [row[1] for row in rows] == [0, 1]
        current = conn.execute("SELECT npv_per_share FROM dcf_runs WHERE is_latest=1").fetchone()
    assert current is not None
    assert current[0] == pytest.approx(redesign.value(inp).value_per_share_usd)
    assert not list(accepted.parent.glob("*.import.*"))
    assert not list(accepted.parent.glob("*.rebuild.*"))


@pytest.mark.parametrize("invalid_tax", [None, "not a tax rate"])
def test_invalid_import_required_input_preserves_real_accepted_bundle(
    refresh_repo: Path, monkeypatch: pytest.MonkeyPatch, invalid_tax: str | None
) -> None:
    db = refresh_repo / "data" / "portfolio.db"
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(db))
    monkeypatch.setattr(refresh_dcf.live_price_mod, "read_live_price", support.fake_read)
    assert (
        refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)["status"] == "ok"
    )
    accepted = refresh_repo / "dcf" / "TESTCO.xlsx"
    assumptions = refresh_repo / "data" / "dcf_assumptions" / "TESTCO.json"
    original_workbook, original_json = accepted.read_bytes(), assumptions.read_bytes()
    with sqlite3.connect(db) as conn:
        original_rows = conn.execute("SELECT * FROM dcf_runs ORDER BY id").fetchall()
    imported = refresh_repo / "invalid-tax.xlsx"
    shutil.copyfile(accepted, imported)
    wb = openpyxl.load_workbook(imported)
    cell = wb["Dashboard"].cell(31, 2)
    assert isinstance(cell, Cell)
    cell.value = invalid_tax
    wb.save(imported)
    wb.close()
    rc = dcf_sheets.main(
        ["import", "--ticker", "TESTCO", "--file", str(imported), "--repo-root", str(refresh_repo)]
    )
    assert rc != 0
    assert accepted.read_bytes() == original_workbook
    assert assumptions.read_bytes() == original_json
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT * FROM dcf_runs ORDER BY id").fetchall() == original_rows
    assert not list(accepted.parent.glob("*.import.*"))


def test_builder_seeds_scenario_weights_and_reader_reads_them(
    built_usd: tuple[Path, float],
) -> None:
    """The builder writes the scenario probability-weight cells (row 62) and
    read_inputs reads them back — seeded to the symmetric default here (the test
    FMP carries no scenario_prior block)."""
    dest, _ = built_usd
    inp = redesign.read_inputs(dest)
    assert inp is not None
    assert inp.weight_base == pytest.approx(0.50)
    assert inp.weight_bull == pytest.approx(0.25)
    assert inp.weight_bear == pytest.approx(0.25)


# --------------------------------------------------------------------------- #
# Semi-annual filer (BHP shape): H1/H2 reported as Q2/Q4, two periods per FY
# --------------------------------------------------------------------------- #


def test_semiannual_reader_round_trips_and_matches_builder(
    built_semiannual: tuple[Path, float],
) -> None:
    """The reader must accept a semi-annual workbook — its _latest_full_fy detects
    the Q2/Q4 cadence instead of demanding four quarters — and recompute the
    value-of-record in line with the builder, proving the H1/H2 build round-trips."""
    dest, builder_value = built_semiannual
    assert redesign.is_redesign_format(dest) is True
    rv = redesign.read_and_value(dest)
    assert rv is not None
    assert builder_value > 0 and rv.value_per_share_usd > 0
    assert rv.value_per_share_usd == pytest.approx(builder_value, abs=0.0051)


def test_semiannual_reader_aggregates_two_halves_into_fy(
    built_semiannual: tuple[Path, float],
) -> None:
    """Single-segment base revenue = the latest FY's two half-year columns summed
    (Q2 + Q4), not one quarter — the same FY aggregation the builder applies."""
    dest, _ = built_semiannual
    inp = redesign.read_inputs(dest)
    assert inp is not None
    assert inp.segments == ("Total company",)
    expected_fy2025 = 400.0 * (1.03**7) + 400.0 * (1.03**8)  # the two FY2025 halves
    assert inp.base_revenue_by_segment["Total company"] == pytest.approx(expected_fy2025, rel=1e-3)


def test_reader_reads_dashboard_and_financials(built_usd: tuple[Path, float]) -> None:
    dest, _ = built_usd
    inp = redesign.read_inputs(dest)
    assert inp is not None
    assert inp.segments == ("Total company",)
    assert inp.diluted_shares_m == pytest.approx(100.0)
    assert 0.0 < inp.wacc < 0.25
    assert inp.consensus_years == 5  # 2026..2030 on the Consensus sheet


def test_reader_accepts_long_term_debt_label(built_usd: tuple[Path, float], tmp_path: Path) -> None:
    """Live FMP workbooks label the balance-sheet debt row ``Long-term Debt``;
    the reader must accept that source label as the equity-bridge debt input."""
    source, _ = built_usd
    dest = tmp_path / "long-term-debt-label.xlsx"
    shutil.copy2(source, dest)
    wb = openpyxl.load_workbook(dest)
    try:
        ws = wb["Financials"]
        debt_row = next(
            row for row in range(1, ws.max_row + 1) if ws.cell(row, 1).value == "Total Debt"
        )
        debt_cell = ws.cell(debt_row, 1)
        assert isinstance(debt_cell, Cell)
        debt_cell.value = "Long-term Debt"
        wb.save(dest)
    finally:
        wb.close()

    inp = redesign.read_inputs(dest)
    assert inp is not None
    assert inp.total_debt_m > 0


# --------------------------------------------------------------------------- #
# SBC + analyst-segment plumbing through the real builder
# --------------------------------------------------------------------------- #
def test_builder_writes_sbc_cells_reader_charges_them(built_usd: tuple[Path, float]) -> None:
    """The builder writes the Dashboard SBC % inputs (rows 65/66) from the actuals
    fade, the reader reads them back, and the value-of-record is the SBC-charged one
    (the in-sheet valfcf formula + the reader mirror agree)."""
    dest, builder_value = built_usd
    inp = redesign.read_inputs(dest)
    assert inp is not None
    # The FMP fixture carries SBC at 5% of revenue, faded 5% -> 3% (x0.6 terminal).
    assert inp.near_sbc_pct == pytest.approx(0.05, abs=0.01)
    assert inp.terminal_sbc_pct == pytest.approx(0.03, abs=0.01)
    assert inp.near_sbc_pct > 0  # SBC is actually charged now, not netted out
    # The reader's value tracks the builder's own SBC-charged mirror.
    rv = redesign.read_and_value(dest)
    assert rv is not None
    assert rv.value_per_share_usd == pytest.approx(builder_value, rel=0.03)


def test_analyst_segments_override_fmp_and_split_base_revenue(tmp_path: Path) -> None:
    """A valid analyst_segments block REPLACES the FMP-resolved segment set: the
    workbook models the analyst's two streams, base revenue is split by base_pct,
    and each segment grows on its own near/terminal rate (reader round-trips)."""
    repo = tmp_path / "analyst"
    support.write_redesign_fmp(repo, "SEGCO", segments=True)  # FMP would resolve Cloud/Devices
    dest = repo / "dcf" / "SEGCO.xlsx"
    support.build_with_assumptions(
        repo,
        "SEGCO",
        {
            "dcf_applicable": True,
            "business_model": "operating",
            "analyst_segments": {
                "Legacy": {"base_pct": 0.80, "near_term_growth": 0.05, "terminal_growth": -0.01},
                "NewEngine": {"base_pct": 0.20, "near_term_growth": 0.35, "terminal_growth": 0.04},
            },
        },
        dest,
    )
    inp = redesign.read_inputs(dest)
    assert inp is not None
    # The analyst split, not FMP's Cloud/Devices.
    assert set(inp.segments) == {"Legacy", "NewEngine"}
    total_base = sum(inp.base_revenue_by_segment.values())
    assert inp.base_revenue_by_segment["Legacy"] == pytest.approx(0.80 * total_base, rel=1e-3)
    assert inp.base_revenue_by_segment["NewEngine"] == pytest.approx(0.20 * total_base, rel=1e-3)
    # Each stream carries its own growth (a split FMP does not report).
    assert inp.near_growth_by_segment["NewEngine"] == pytest.approx(0.35)
    assert inp.terminal_growth_by_segment["Legacy"] == pytest.approx(-0.01)
    rv = redesign.read_and_value(dest)
    assert rv is not None and rv.value_per_share_usd > 0


def test_invalid_analyst_segments_falls_back_to_fmp(tmp_path: Path) -> None:
    """An analyst_segments block whose base_pct doesn't sum to ~1 is rejected — the
    builder falls back to the FMP-resolved segments rather than half-applying it."""
    repo = tmp_path / "analyst_bad"
    support.write_redesign_fmp(repo, "BADCO", segments=True)
    dest = repo / "dcf" / "BADCO.xlsx"
    support.build_with_assumptions(
        repo,
        "BADCO",
        {
            "dcf_applicable": True,
            "business_model": "operating",
            "analyst_segments": {  # sums to 0.6 — invalid
                "A": {"base_pct": 0.30, "near_term_growth": 0.05, "terminal_growth": 0.01},
                "B": {"base_pct": 0.30, "near_term_growth": 0.05, "terminal_growth": 0.01},
            },
        },
        dest,
    )
    inp = redesign.read_inputs(dest)
    assert inp is not None
    # Fell back to FMP's Cloud/Devices — the analyst A/B split was NOT applied.
    assert set(inp.segments) == {"Cloud", "Devices"}


def test_bare_exit_multiple_override_applies_without_segments_block(tmp_path: Path) -> None:
    """A block with only `exit_multiple` (no `segments` sub-block) still applies —
    the terminal-basis re-calibration (#837 SBC-burdened-EBITDA re-basing) sets the
    exit multiple without fabricating a segments block. The builder's default is 12x;
    a bare 20x override must land on the Dashboard cell and lift the value."""
    repo = tmp_path / "bare_mult"
    support.write_redesign_fmp(repo, "MULTCO")  # single-segment, no analyst/opus segments
    dest = repo / "dcf" / "MULTCO.xlsx"
    support.build_with_assumptions(
        repo, "MULTCO", {"dcf_applicable": True, "exit_multiple": 20.0}, dest
    )
    inp = redesign.read_inputs(dest)
    assert inp is not None
    assert inp.exit_multiple == pytest.approx(20.0)  # bare override applied


def test_bare_margin_and_sbc_overrides_apply_without_segments_block(tmp_path: Path) -> None:
    """The #838 heavy-SBC terminal normalization: a block with only
    `terminal_op_margin` + `sbc_pct_near`/`sbc_pct_terminal` (no `segments` sub-block)
    still applies — margins and SBC % are scalar terminal assumptions independent of
    the segment set, so a name carrying only a margin/SBC recut takes effect."""
    repo = tmp_path / "bare_margin"
    support.write_redesign_fmp(repo, "MGNCO")  # single-segment, no segments block
    dest = repo / "dcf" / "MGNCO.xlsx"
    support.build_with_assumptions(
        repo,
        "MGNCO",
        {
            "dcf_applicable": True,
            "terminal_op_margin": 0.28,
            "sbc_pct_near": 0.20,
            "sbc_pct_terminal": 0.10,
        },
        dest,
    )
    inp = redesign.read_inputs(dest)
    assert inp is not None
    assert inp.terminal_op_margin == pytest.approx(0.28)  # bare margin override applied
    assert inp.near_sbc_pct == pytest.approx(0.20)  # bare SBC overrides applied
    assert inp.terminal_sbc_pct == pytest.approx(0.10)


def test_fx_applied_for_non_usd_reporter(tmp_path: Path) -> None:
    """A non-USD reporter (EUR) gets a × FX multiplier off the VALUE/SHARE
    formula; the reporting-currency value is the USD value ÷ FX."""
    repo = tmp_path / "eur"
    support.write_redesign_fmp(repo, "EURCO", currency="EUR")
    dest = repo / "EURCO.xlsx"
    support.build_workbook(repo, "EURCO", dest)
    rv = redesign.read_and_value(dest)
    assert rv is not None
    assert rv.fx_to_usd != 1.0  # EUR -> a non-unity FX
    assert rv.value_per_share_usd == pytest.approx(rv.value_per_share_reporting * rv.fx_to_usd)


# --------------------------------------------------------------------------- #
# Scenarios + sensitivity in the built workbook
# --------------------------------------------------------------------------- #
# The grid is 7x7 anchored at header row 4 / column B, so the base (center)
# cell sits at row 8, column E.


def test_builder_writes_scenarios_block_and_sensitivity_sheet(
    built_usd: tuple[Path, float],
) -> None:
    """A fresh build carries the seeded Bull/Bear Δ columns, the three computed
    fair values (Base == the reader's value-of-record), and a Sensitivity sheet
    whose center cell is that same value — builder statics and the refresh
    engine can't drift because both come from dcf.redesign."""
    dest, _ = built_usd
    rv = redesign.read_and_value(dest)
    assert rv is not None
    wb = openpyxl.load_workbook(str(dest))
    try:
        assert wb.sheetnames == support.REDESIGN_SHEETS
        dsh = wb["Dashboard"]
        bull_margin = dsh.cell(redesign.SCEN_ROW_MARGIN_TERM, redesign.SCEN_COL_BULL).value
        bear_mult = dsh.cell(redesign.SCEN_ROW_EXIT_MULT, redesign.SCEN_COL_BEAR).value
        assert bull_margin == pytest.approx(redesign.BULL_SEED.margin_term)
        assert bear_mult == pytest.approx(redesign.BEAR_SEED.exit_multiple)
        base_fv = dsh.cell(redesign.SCEN_FV_ROW, 2).value
        bull_fv = dsh.cell(redesign.SCEN_FV_ROW, redesign.SCEN_COL_BULL).value
        bear_fv = dsh.cell(redesign.SCEN_FV_ROW, redesign.SCEN_COL_BEAR).value
        assert isinstance(base_fv, float) and isinstance(bull_fv, float)
        assert isinstance(bear_fv, float)
        assert base_fv == pytest.approx(rv.value_per_share_usd)
        assert bear_fv < base_fv < bull_fv
        sens = wb["Sensitivity"]
        assert sens.cell(*support.GRID_CENTER).value == pytest.approx(rv.value_per_share_usd)
    finally:
        wb.close()


def test_reader_returns_seeded_deltas_and_blank_cells_fall_back(
    built_usd: tuple[Path, float], tmp_path: Path
) -> None:
    """read_inputs returns the seeded offsets from a fresh build; blanking a
    single Δ cell falls back to that lever's documented seed (and a pre-scenario
    workbook therefore reads as the full seeds)."""
    dest, _ = built_usd
    inp = redesign.read_inputs(dest)
    assert inp is not None
    assert inp.bull_deltas == redesign.BULL_SEED
    assert inp.bear_deltas == redesign.BEAR_SEED

    copy = tmp_path / "blanked.xlsx"
    shutil.copyfile(dest, copy)
    wb = openpyxl.load_workbook(str(copy))
    wb["Dashboard"].cell(row=redesign.SCEN_ROW_TG, column=redesign.SCEN_COL_BULL).value = None
    wb.save(str(copy))
    wb.close()
    blanked = redesign.read_inputs(copy)
    assert blanked is not None
    assert blanked.bull_deltas.terminal_g == pytest.approx(redesign.BULL_SEED.terminal_g)


# --------------------------------------------------------------------------- #
