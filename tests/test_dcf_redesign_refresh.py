"""Refresh and apply-edits integration tests for the redesigned 9-sheet DCF.

Drive ``refresh_dcf.refresh_one`` and ``apply_edits`` end-to-end: rebuild every
sheet from FMP, preserve the user's Dashboard inputs, recompute the value, and
upsert ``dcf_runs``. Engine, builder and reader tests stay in
``tests/test_dcf_redesign.py``; shared helpers live in
``tests/fixtures/dcf_redesign_support.py``.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import dcf_sheets
import openpyxl
import pytest
import refresh_dcf

import tests.fixtures.dcf_redesign_support as support
from dcf import redesign
from dcf.input_evidence import InputEvidenceError
from sources.dcf_statements import DcfStatementInputs


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


# --------------------------------------------------------------------------- #
# Edit-preservation refresh integration
# --------------------------------------------------------------------------- #


def test_refresh_redesign_seeds_then_persists(
    refresh_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing workbook builds fresh (redesign format) and persists dcf_runs."""
    monkeypatch.setattr(refresh_dcf.live_price_mod, "read_live_price", support.fake_read)
    db = refresh_repo / "data" / "portfolio.db"
    res = refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)
    assert res["status"] == "ok", res
    assert res["format"] == "redesign"
    dest = refresh_repo / "dcf" / "TESTCO.xlsx"
    assert dest.exists()
    wb = openpyxl.load_workbook(str(dest))
    assert wb.sheetnames == support.REDESIGN_SHEETS
    wb.close()
    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT npv_per_share, live_price, input_sha256, workbook_sha256, "
        "engine_version, inputs_as_of, provenance_json "
        "FROM dcf_runs WHERE ticker='TESTCO' AND is_latest=1"
    ).fetchone()
    conn.close()
    assert row is not None and row[0] is not None
    assert row[1] == pytest.approx(50.0)
    assert isinstance(row[2], str) and len(row[2]) == 64
    assert isinstance(row[3], str) and len(row[3]) == 64
    assert row[4] == refresh_dcf.DCF_ENGINE_VERSION
    assert row[5]
    provenance_detail = json.loads(row[6])
    assert provenance_detail["ticker"] == "TESTCO"
    assert {source["role"] for source in provenance_detail["sources"]} >= {
        "income_statement",
        "balance_sheet",
        "cash_flow",
        "calculation_workbook",
    }


def test_sync_assumptions_json_mirrors_numbers_keeps_prose(tmp_path: Path) -> None:
    """_sync_assumptions_json writes edited numeric inputs into the redesign block
    while preserving the Opus narrative/reasoning and the model flags."""
    adir = tmp_path / "data" / "dcf_assumptions"
    adir.mkdir(parents=True)
    (adir / "TESTCO.json").write_text(
        json.dumps(
            {
                "ticker": "TESTCO",
                "narrative": "TOP NARRATIVE keep",
                "redesign": {
                    "dcf_applicable": True,
                    "business_model": "operating",
                    "segments": {
                        "Total company": {"near_term_growth": 0.10, "terminal_growth": 0.03}
                    },
                    "near_term_op_margin": 0.20,
                    "terminal_op_margin": 0.25,
                    "tax_rate": 0.24,
                    "exit_basis": "EV/EBITDA",
                    "terminal_method": "Exit multiple",
                    "exit_multiple": 12.0,
                    "terminal_growth_g": 0.03,
                    "terminal_capex_da": 1.05,
                    "narrative": "REDESIGN NARRATIVE keep",
                    "reasoning": "REASONING keep",
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    edited = dataclasses.replace(
        support.BASE,
        near_growth_by_segment={"Total company": 0.06},
        terminal_growth_by_segment={"Total company": 0.02},
        near_op_margin=0.05,
        terminal_op_margin=0.15,
        exit_multiple=9.0,
        terminal_growth_g=0.02,
        beta=1.45,
        equity_risk_premium=0.055,
    )
    assert refresh_dcf.sync_assumptions_json(tmp_path, "TESTCO", edited).status == "synced"
    out = json.loads((adir / "TESTCO.json").read_text(encoding="utf-8"))
    rd = out["redesign"]
    assert rd["segments"]["Total company"] == {"near_term_growth": 0.06, "terminal_growth": 0.02}
    assert rd["near_term_op_margin"] == 0.05
    assert rd["terminal_op_margin"] == 0.15
    assert rd["exit_multiple"] == 9.0
    assert rd["terminal_growth_g"] == 0.02
    # WACC drivers: beta + cost_of_debt mirror per name; risk_free_rate +
    # equity_risk_premium are GLOBAL now (global_dcf_assumptions, migration 0112)
    # and deliberately NOT re-pinned into the block (PR6).
    assert rd["beta"] == 1.45
    assert rd["cost_of_debt"] == 0.045
    assert "risk_free_rate" not in rd
    assert "equity_risk_premium" not in rd
    # Opus prose + model flags untouched
    assert rd["narrative"] == "REDESIGN NARRATIVE keep"
    assert rd["reasoning"] == "REASONING keep"
    assert rd["dcf_applicable"] is True
    assert rd["business_model"] == "operating"
    assert out["narrative"] == "TOP NARRATIVE keep"


def test_sync_creates_assumptions_json_when_absent(tmp_path: Path) -> None:
    """No assumptions file (the silent-no-op hole: 43 of ~91 workbooks) -> one
    is CREATED from the workbook inputs, marked set_by='sync', so a
    from-scratch build reproduces the user's values instead of reverting them."""
    res = refresh_dcf.sync_assumptions_json(tmp_path, "MISSING", support.BASE)
    assert res.status == "created"
    created = json.loads(
        (tmp_path / "data" / "dcf_assumptions" / "MISSING.json").read_text(encoding="utf-8")
    )
    rd = created["redesign"]
    assert rd["set_by"] == "sync"
    assert rd["exit_multiple"] == 12.0
    assert rd["segments"]["Total company"]["near_term_growth"] == 0.10
    # no fabricated Opus provenance for a workbook-derived block
    assert "opus_baseline" not in created

    # A file with no redesign block gets one (other keys preserved).
    adir = tmp_path / "data" / "dcf_assumptions"
    (adir / "NOBLOCK.json").write_text(json.dumps({"ticker": "NOBLOCK"}), encoding="utf-8")
    res = refresh_dcf.sync_assumptions_json(tmp_path, "NOBLOCK", support.BASE)
    assert res.status == "created"
    out = json.loads((adir / "NOBLOCK.json").read_text(encoding="utf-8"))
    assert out["ticker"] == "NOBLOCK"
    assert out["redesign"]["set_by"] == "sync"


def test_sync_fails_loud_on_unreadable_json(tmp_path: Path) -> None:
    """Corruption is 'failed' + detail — never a silent False."""
    adir = tmp_path / "data" / "dcf_assumptions"
    adir.mkdir(parents=True)
    (adir / "BAD.json").write_text("{not json", encoding="utf-8")
    res = refresh_dcf.sync_assumptions_json(tmp_path, "BAD", support.BASE)
    assert res.status == "failed"
    assert res.detail is not None and "unreadable" in res.detail
    (adir / "NOTOBJ.json").write_text('"just a string"', encoding="utf-8")
    assert refresh_dcf.sync_assumptions_json(tmp_path, "NOTOBJ", support.BASE).status == "failed"


def test_refresh_stages_missing_assumptions_without_live_path(tmp_path: Path) -> None:
    assumptions = tmp_path / "data" / "dcf_assumptions" / "MISSING.json"
    staged = refresh_dcf.stage_assumptions(assumptions)
    assert staged != assumptions
    assert staged.name.startswith("MISSING.assumptions.")
    assert staged.suffix == ".json"
    assert refresh_dcf.stage_assumptions(assumptions) != staged
    assert not assumptions.exists()
    assert not staged.exists()


def test_refresh_redesign_syncs_assumptions_json(
    refresh_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a Dashboard edit flows back into data/dcf_assumptions on refresh,
    so a from-scratch rebuild would reproduce it (single source of truth)."""
    monkeypatch.setattr(refresh_dcf.live_price_mod, "read_live_price", support.fake_read)
    db = refresh_repo / "data" / "portfolio.db"
    dest = refresh_repo / "dcf" / "TESTCO.xlsx"
    adir = refresh_repo / "data" / "dcf_assumptions"
    adir.mkdir(parents=True)
    (adir / "TESTCO.json").write_text(
        json.dumps({"redesign": {"exit_multiple": 12.0, "narrative": "keep me"}}, indent=2),
        encoding="utf-8",
    )
    # Seed a workbook, then simulate the user editing the exit multiple (B45) and refresh.
    refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)
    wb = openpyxl.load_workbook(str(dest))
    dsh = wb["Dashboard"]
    dsh.cell(row=45, column=2, value=8.0)  # exit multiple -> 8x
    dsh.cell(row=40, column=2, value=1.55)  # beta -> 1.55
    wb.save(str(dest))
    wb.close()
    res = refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)
    assert res["status"] == "blocked", res
    assert res["reason"] == "outlier_requires_explicit_owner_review"
    rd = json.loads((adir / "TESTCO.json").read_text(encoding="utf-8"))["redesign"]
    assert rd["exit_multiple"] == pytest.approx(12.0)
    assert rd["narrative"] == "keep me"
    # The candidate was rejected before replacement, so the prior run's sync
    # outcome remains durable and the assumptions mirror stays unchanged.
    conn = sqlite3.connect(str(db))
    srow = conn.execute(
        "SELECT assumptions_sync_status, assumptions_synced_at FROM dcf_runs WHERE ticker='TESTCO' AND is_latest=1"
    ).fetchone()
    conn.close()
    assert srow is not None and srow[0] == "synced" and srow[1]


def test_refresh_provenance_end_to_end(refresh_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Full provenance round-trip through the real builder + refresher:

    1. first refresh seeds the immutable opus_baseline and writes the
       Assumptions sheet with the Opus-sourced exit multiple;
    2. the user edits the exit multiple in the workbook; the next refresh
       classifies it user-edited, records the override (Opus value + date) in
       the ledger, annotates the yellow cell — while sync rewrites the
       redesign block to 8x and the baseline provably stays at 11x.
    """
    monkeypatch.setattr(refresh_dcf.live_price_mod, "read_live_price", support.fake_read)
    db = refresh_repo / "data" / "portfolio.db"
    dest = refresh_repo / "dcf" / "TESTCO.xlsx"
    adir = refresh_repo / "data" / "dcf_assumptions"
    adir.mkdir(parents=True)
    (adir / "TESTCO.json").write_text(
        json.dumps(
            {
                "redesign": {
                    "dcf_applicable": True,
                    "business_model": "operating",
                    "segments": {
                        "Cloud": {"near_term_growth": 0.12, "terminal_growth": 0.04},
                        "Devices": {"near_term_growth": 0.05, "terminal_growth": 0.02},
                    },
                    "near_term_op_margin": 0.18,
                    "terminal_op_margin": 0.22,
                    "tax_rate": 0.22,
                    "terminal_capex_da": 1.05,
                    "terminal_method": "Exit multiple",
                    "exit_basis": "EV/EBITDA",
                    "exit_multiple": 11.0,
                    "terminal_growth_g": 0.035,
                    "narrative": "STORY PROSE",
                    "reasoning": "JUDGMENT PROSE",
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    res1 = refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)
    assert res1["status"] == "ok", res1
    prov1 = res1["assumption_provenance"]
    assert isinstance(prov1, dict) and prov1["status"] == "ok"
    data = json.loads((adir / "TESTCO.json").read_text(encoding="utf-8"))
    assert data["opus_baseline"]["values"]["exit_multiple"] == 11.0
    assert data["opus_baseline"]["seeded"] is True
    wb = openpyxl.load_workbook(str(dest))
    try:
        assert "Assumptions" in wb.sheetnames
        text = "\n".join(
            str(c.value)
            for row in wb["Assumptions"].iter_rows()
            for c in row
            if c.value is not None
        )
        assert "STORY PROSE" in text and "JUDGMENT PROSE" in text
        dsh = wb["Dashboard"]
        assert dsh.cell(row=45, column=2).value == pytest.approx(11.0)  # Opus applied
        dsh.cell(row=45, column=2, value=8.0)  # the user's edit
        wb.save(str(dest))
    finally:
        wb.close()

    res2 = refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)
    assert res2["status"] == "ok", res2
    provenance = cast("dict[str, object]", res2["assumption_provenance"])
    sources = cast("dict[str, int]", provenance["sources"])
    assert sources.get("user-edited", 0) >= 1

    data = json.loads((adir / "TESTCO.json").read_text(encoding="utf-8"))
    # sync mirrored the edit into the redesign block (from-scratch builds
    # reproduce it) while the baseline kept the original Opus value.
    assert data["redesign"]["exit_multiple"] == pytest.approx(8.0)
    assert data["opus_baseline"]["values"]["exit_multiple"] == 11.0
    assert data["assumption_overrides"]["exit_multiple"]["opus_value"] == 11.0

    wb = openpyxl.load_workbook(str(dest))
    try:
        comment = wb["Dashboard"]["B45"].comment
        assert comment is not None
        assert "overridden from unattributed baseline 11.0x" in comment.text
        text = "\n".join(
            str(c.value)
            for row in wb["Assumptions"].iter_rows()
            for c in row
            if c.value is not None
        )
        assert "overridden from unattributed baseline 11.0x" in text
    finally:
        wb.close()


def test_builder_reads_wacc_override_from_block(
    refresh_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A beta/ERP override in the redesign block flows into the built workbook's WACC
    cells — so a from-scratch rebuild reproduces the user's WACC drivers (the other
    half of the round-trip from test_refresh_redesign_syncs_assumptions_json)."""
    monkeypatch.setattr(refresh_dcf.live_price_mod, "read_live_price", support.fake_read)
    db = refresh_repo / "data" / "portfolio.db"
    dest = refresh_repo / "dcf" / "TESTCO.xlsx"
    adir = refresh_repo / "data" / "dcf_assumptions"
    adir.mkdir(parents=True)
    (adir / "TESTCO.json").write_text(
        json.dumps({"redesign": {"beta": 1.62, "equity_risk_premium": 0.055}}, indent=2),
        encoding="utf-8",
    )
    res = refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)
    assert res["status"] == "ok", res
    # The builder wrote the override into the Dashboard WACC cells (B40 beta, B39 ERP).
    assert support.dashboard_cell(dest, 40) == pytest.approx(1.62)
    assert support.dashboard_cell(dest, 39) == pytest.approx(0.055)


@pytest.mark.parametrize("operation", ["refresh", "save"])
def test_receipt_failure_preserves_workbook_and_current_run(
    refresh_repo: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    monkeypatch.setattr(refresh_dcf.live_price_mod, "read_live_price", support.fake_read)
    db = refresh_repo / "data" / "portfolio.db"
    dest = refresh_repo / "dcf" / "TESTCO.xlsx"
    initial = refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)
    assert initial["status"] == "ok", initial
    before = dest.read_bytes()
    with sqlite3.connect(db) as conn:
        count = conn.execute("SELECT count(*) FROM dcf_runs").fetchone()[0]

    def unavailable(*args: object, **kwargs: object) -> None:
        raise InputEvidenceError("synthetic_receipt_unavailable")

    monkeypatch.setattr(refresh_dcf, "redesign_run_evidence", unavailable)
    if operation == "refresh":
        result = refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)
    else:
        inputs = redesign.read_inputs(dest)
        assert inputs is not None
        result = refresh_dcf.apply_edits("TESTCO", refresh_repo, db, inputs)
    assert result["status"] == "blocked", result
    assert result["reason"] == "synthetic_receipt_unavailable"
    assert dest.read_bytes() == before
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM dcf_runs").fetchone()[0] == count


def test_refresh_current_assumptions_uses_file_instead_of_saved_workbook(
    refresh_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(refresh_dcf.live_price_mod, "read_live_price", support.fake_read)
    db = refresh_repo / "data" / "portfolio.db"
    dest = refresh_repo / "dcf" / "TESTCO.xlsx"
    assumptions = refresh_repo / "data" / "dcf_assumptions" / "TESTCO.json"
    assumptions.parent.mkdir(parents=True)
    assumptions.write_text(json.dumps({"redesign": {"exit_multiple": 12.0}}))
    first = refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)
    assert first["status"] == "ok", first
    wb = openpyxl.load_workbook(dest)
    wb["Dashboard"].cell(45, 2, 8.0)
    wb.save(dest)
    wb.close()
    result = refresh_dcf.refresh_one(
        "TESTCO", refresh_repo, db, valuation_year=2026, current_assumptions=True
    )
    assert result["status"] == "ok", result
    assert support.dashboard_cell(dest, 45) == 12.0


def test_refresh_redesign_preserves_dashboard_edit_and_updates_actuals(
    refresh_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core edit-preservation contract: edit a yellow Dashboard cell, change
    the underlying FMP actuals, refresh. The edit MUST survive, the actuals MUST
    update, and the dropdowns/charts MUST still be intact."""
    monkeypatch.setattr(refresh_dcf.live_price_mod, "read_live_price", support.fake_read)
    db = refresh_repo / "data" / "portfolio.db"
    dest = refresh_repo / "dcf" / "TESTCO.xlsx"

    # Seed a workbook, then simulate a user editing the terminal margin (B30) and
    # the Cloud segment near-term growth (a segment row).
    refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)
    wb = openpyxl.load_workbook(str(dest))
    dsh = wb["Dashboard"]
    dsh.cell(row=30, column=2, value=0.33)  # terminal op margin -> 33%
    for r in range(20, 28):  # Cloud segment near-term growth -> 50%
        if dsh.cell(row=r, column=1).value == "Cloud":
            dsh.cell(row=r, column=2, value=0.50)
    wb.save(str(dest))
    wb.close()

    # Mutate the latest-quarter FMP revenue so the rebuilt Financials must change.
    inc_path = (
        refresh_repo / "data" / "historical" / "fmp" / "TESTCO_income_statement_quarterly.json"
    )
    inc = json.loads(inc_path.read_text(encoding="utf-8"))
    newest = max(inc, key=lambda r: (int(r["fiscalYear"]), str(r["period"])))
    newest["revenue"] = 9_999 * 1e6
    inc_path.write_text(json.dumps(inc), encoding="utf-8")
    # Scale that quarter's product segmentation to match the new revenue. Otherwise a 6x
    # income jump against flat segments would (correctly) trip the partial-coverage guard
    # (src/dcf/segment_coverage.py) and fall back to a whole-company build — which has no
    # "Cloud" segment row, defeating the per-segment edit-preservation this test asserts.
    seg_path = (
        refresh_repo / "data" / "historical" / "fmp" / "TESTCO_product_segments_quarterly.json"
    )
    pseg = json.loads(seg_path.read_text(encoding="utf-8"))
    for rec in pseg:
        if int(rec["fiscalYear"]) == int(newest["fiscalYear"]) and str(rec["period"]) == str(
            newest["period"]
        ):
            rec["data"] = {"Cloud": 9_999 * 0.6 * 1e6, "Devices": 9_999 * 0.4 * 1e6}
    seg_path.write_text(json.dumps(pseg), encoding="utf-8")

    before_blocked_refresh = dest.read_bytes()
    res = refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)
    assert res["status"] == "blocked", res
    assert res["reason"] == "outlier_requires_explicit_owner_review"
    assert dest.read_bytes() == before_blocked_refresh


def test_refresh_preserves_scenario_edits_and_recomputes_outputs(
    refresh_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scenario half of the edit-preservation contract: edit Bull/Bear Δ
    cells, refresh. The edits MUST survive the rebuild, the computed fair-value
    row + Sensitivity grid MUST be rewritten from the edited inputs (not the
    builder's defaults), dcf_runs MUST carry the scenario range in
    assumption_snapshot_json, and the deltas MUST mirror to dcf_assumptions."""
    monkeypatch.setattr(refresh_dcf.live_price_mod, "read_live_price", support.fake_read)
    db = refresh_repo / "data" / "portfolio.db"
    dest = refresh_repo / "dcf" / "TESTCO.xlsx"
    adir = refresh_repo / "data" / "dcf_assumptions"
    adir.mkdir(parents=True)
    (adir / "TESTCO.json").write_text(
        json.dumps({"redesign": {"exit_multiple": 12.0, "narrative": "keep me"}}, indent=2),
        encoding="utf-8",
    )
    refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)

    # The user edits both scenario columns (a richer Bull margin, a harsher
    # Bear multiple) and we note the seeded computed Bull value beforehand.
    wb = openpyxl.load_workbook(str(dest))
    dsh = wb["Dashboard"]
    pre_bull = dsh.cell(redesign.SCEN_FV_ROW, redesign.SCEN_COL_BULL).value
    pre_bear = dsh.cell(redesign.SCEN_FV_ROW, redesign.SCEN_COL_BEAR).value
    assert isinstance(pre_bull, float) and isinstance(pre_bear, float)
    dsh.cell(row=redesign.SCEN_ROW_MARGIN_TERM, column=redesign.SCEN_COL_BULL, value=0.10)
    dsh.cell(row=redesign.SCEN_ROW_EXIT_MULT, column=redesign.SCEN_COL_BEAR, value=-4.0)
    wb.save(str(dest))
    wb.close()

    res = refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)
    assert res["status"] == "ok", res

    wb2 = openpyxl.load_workbook(str(dest))
    try:
        dsh2 = wb2["Dashboard"]
        # The Δ edits survived the rebuild.
        bull_margin = dsh2.cell(redesign.SCEN_ROW_MARGIN_TERM, redesign.SCEN_COL_BULL).value
        bear_mult = dsh2.cell(redesign.SCEN_ROW_EXIT_MULT, redesign.SCEN_COL_BEAR).value
        assert bull_margin == pytest.approx(0.10)
        assert bear_mult == pytest.approx(-4.0)
        # The computed row was rewritten from the EDITED deltas: a richer Bull
        # margin lifts Bull, a harsher Bear multiple cuts Bear.
        post_bull = dsh2.cell(redesign.SCEN_FV_ROW, redesign.SCEN_COL_BULL).value
        post_bear = dsh2.cell(redesign.SCEN_FV_ROW, redesign.SCEN_COL_BEAR).value
        assert isinstance(post_bull, float) and isinstance(post_bear, float)
        assert post_bull > pre_bull
        assert post_bear < pre_bear
        # The Sensitivity grid tracks the value-of-record (rewritten post-inject).
        rv = redesign.read_and_value(dest)
        assert rv is not None
        sens = wb2["Sensitivity"]
        assert sens.cell(*support.GRID_CENTER).value == pytest.approx(rv.value_per_share_usd)
    finally:
        wb2.close()

    # dcf_runs carries the scenario range; BASE stays npv_per_share.
    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT npv_per_share, assumption_snapshot_json FROM dcf_runs WHERE ticker='TESTCO' AND is_latest=1"
    ).fetchone()
    history = conn.execute(
        "SELECT is_latest,assumption_snapshot_json FROM dcf_runs WHERE ticker='TESTCO' ORDER BY id"
    ).fetchall()
    conn.close()
    assert [version[0] for version in history] == [0, 1]
    assert json.loads(history[0][1])["scenarios"]["bull"][
        "fair_value_per_share_usd"
    ] == pytest.approx(pre_bull)
    assert row is not None
    snap = json.loads(row[1])
    sc = snap["scenarios"]
    assert sc["base"]["fair_value_per_share_usd"] == pytest.approx(float(row[0]))
    assert sc["bull"]["fair_value_per_share_usd"] == pytest.approx(post_bull)
    assert sc["bear"]["fair_value_per_share_usd"] == pytest.approx(post_bear)
    assert sc["bull"]["deltas"]["margin_term"] == pytest.approx(0.10)
    assert sc["bear"]["deltas"]["exit_multiple"] == pytest.approx(-4.0)

    # The deltas mirrored back to the assumptions JSON (from-scratch parity).
    rd = json.loads((adir / "TESTCO.json").read_text(encoding="utf-8"))["redesign"]
    assert rd["scenario_bull"]["margin_term"] == pytest.approx(0.10)
    assert rd["scenario_bear"]["exit_multiple"] == pytest.approx(-4.0)
    assert rd["narrative"] == "keep me"


def test_builder_reads_scenario_override_from_block(
    refresh_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """scenario_bull/scenario_bear overrides in the redesign block flow into the
    built workbook's Δ columns (the other half of the sync round-trip) — levers
    absent from the override keep their documented seeds."""
    monkeypatch.setattr(refresh_dcf.live_price_mod, "read_live_price", support.fake_read)
    db = refresh_repo / "data" / "portfolio.db"
    dest = refresh_repo / "dcf" / "TESTCO.xlsx"
    adir = refresh_repo / "data" / "dcf_assumptions"
    adir.mkdir(parents=True)
    (adir / "TESTCO.json").write_text(
        json.dumps(
            {
                "redesign": {
                    "scenario_bull": {"margin_term": 0.07},
                    "scenario_bear": {"growth_near": -0.08},
                }
            }
        ),
        encoding="utf-8",
    )
    res = refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)
    assert res["status"] == "ok", res
    assert support.dashboard_cell(
        dest, redesign.SCEN_ROW_MARGIN_TERM, redesign.SCEN_COL_BULL
    ) == pytest.approx(0.07)
    assert support.dashboard_cell(
        dest, redesign.SCEN_ROW_GROWTH_NEAR, redesign.SCEN_COL_BEAR
    ) == pytest.approx(-0.08)
    # un-overridden levers keep the seeds
    assert support.dashboard_cell(
        dest, redesign.SCEN_ROW_EXIT_MULT, redesign.SCEN_COL_BULL
    ) == pytest.approx(redesign.BULL_SEED.exit_multiple)


def test_refresh_persists_seeded_per_name_prior_and_skews_reward(
    refresh_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A per-name ``scenario_prior`` block already on file (the producer's LLM
    output) flows through end-to-end: the built workbook's weight cells (row 62)
    seed from it, the ``dcf_runs`` snapshot carries both ``scenario_prior`` and
    ``priced_in`` blocks, and ``dcf.scenario_reward`` skews its expected return
    toward the per-name weights (not the symmetric 25/50/25 global)."""
    from dcf import scenario_reward as scenario_reward_mod

    monkeypatch.setattr(refresh_dcf.live_price_mod, "read_live_price", support.fake_read)
    db = refresh_repo / "data" / "portfolio.db"
    dest = refresh_repo / "dcf" / "TESTCO.xlsx"
    adir = refresh_repo / "data" / "dcf_assumptions"
    adir.mkdir(parents=True)
    (adir / "TESTCO.json").write_text(
        json.dumps(
            {
                "redesign": {
                    "scenario_prior": {
                        "bull_weight": 0.15,
                        "base_weight": 0.50,
                        "bear_weight": 0.35,
                        "rationale": "fragile, execution-heavy thesis",
                        "set_by": "llm",
                        "as_of": "2026-07-01",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    res = refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)
    assert res["status"] == "ok", res

    # The built workbook's row-62 weight cells seeded from the per-name prior.
    assert support.dashboard_cell(
        dest, redesign.SCEN_WEIGHTS_ROW, redesign.SCEN_COL_WEIGHT_BASE
    ) == pytest.approx(0.50)
    assert support.dashboard_cell(
        dest, redesign.SCEN_WEIGHTS_ROW, redesign.SCEN_COL_BULL
    ) == pytest.approx(0.15)
    assert support.dashboard_cell(
        dest, redesign.SCEN_WEIGHTS_ROW, redesign.SCEN_COL_BEAR
    ) == pytest.approx(0.35)

    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT npv_per_share, live_price, assumption_snapshot_json "
        "FROM dcf_runs WHERE ticker='TESTCO' AND is_latest=1"
    ).fetchone()
    conn.close()
    assert row is not None
    npv_per_share, live_price, snapshot_raw = row
    snap = json.loads(snapshot_raw)

    # Both blocks land on the run.
    assert "priced_in" in snap
    sp = snap["scenario_prior"]
    assert sp["weights"] == {
        "bull": pytest.approx(0.15),
        "base": pytest.approx(0.50),
        "bear": pytest.approx(0.35),
    }
    assert sp["rationale"] == "fragile, execution-heavy thesis"
    assert sp["set_by"] == "llm"

    # scenario_reward reads the per-name weights (not the global 25/50/25) and
    # the skew reflects the bear-heavy tilt.
    reward = scenario_reward_mod.scenario_reward(
        price=live_price, base_fv=npv_per_share, snapshot_json=snapshot_raw
    )
    assert reward is not None
    assert reward.weights_source == "per_name"
    assert reward.probabilities["bear"] == pytest.approx(0.35)
    assert reward.probabilities["bull"] == pytest.approx(0.15)
    # A bear-heavy prior pulls the expected return below the symmetric-prior one.
    global_reward = scenario_reward_mod.ScenarioReward(
        expected_return=sum(
            scenario_reward_mod.SCENARIO_PROBABILITIES[s]
            * (
                (
                    scenario_reward_mod.parse_scenario_fair_values(snapshot_raw)
                    | {"base": npv_per_share}
                )[s]
                / live_price
                - 1.0
            )
            for s in ("bull", "base", "bear")
        ),
        base_return=npv_per_share / live_price - 1.0,
        bull_return=None,
        bear_return=None,
        has_scenarios=True,
        probabilities=scenario_reward_mod.SCENARIO_PROBABILITIES,
        detail="",
    )
    assert reward.expected_return < global_reward.expected_return


def test_owner_workbook_edit_off_default_creates_per_name_owner_prior(
    refresh_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner edits the row-62 weight cells directly in the workbook (no
    per-name ``scenario_prior`` on file yet, no LLM run) — the sync MUST create a
    per-name block with ``set_by='owner'`` rather than silently dropping the
    edit, and that block MUST survive the next refresh (owner edits always
    win)."""
    monkeypatch.setattr(refresh_dcf.live_price_mod, "read_live_price", support.fake_read)
    db = refresh_repo / "data" / "portfolio.db"
    dest = refresh_repo / "dcf" / "TESTCO.xlsx"
    adir = refresh_repo / "data" / "dcf_assumptions"

    # First refresh: no assumptions JSON at all yet — a from-scratch build seeds
    # row 62 at the symmetric global default (no prior on file).
    refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)
    assert not (adir / "TESTCO.json").exists() or "scenario_prior" not in json.loads(
        (adir / "TESTCO.json").read_text(encoding="utf-8")
    ).get("redesign", {})

    # The owner opens the workbook and edits the weight cells directly, off the
    # 25/50/25 default — a locked-in decision, not an LLM proposal.
    wb = openpyxl.load_workbook(str(dest))
    dsh = wb["Dashboard"]
    dsh.cell(row=redesign.SCEN_WEIGHTS_ROW, column=redesign.SCEN_COL_WEIGHT_BASE, value=0.50)
    dsh.cell(row=redesign.SCEN_WEIGHTS_ROW, column=redesign.SCEN_COL_BULL, value=0.40)
    dsh.cell(row=redesign.SCEN_WEIGHTS_ROW, column=redesign.SCEN_COL_BEAR, value=0.10)
    wb.save(str(dest))
    wb.close()

    res = refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)
    assert res["status"] == "ok", res

    # A per-name owner block now exists — the edit was NOT silently lost.
    rd = json.loads((adir / "TESTCO.json").read_text(encoding="utf-8"))["redesign"]
    sp = rd["scenario_prior"]
    assert sp["set_by"] == "owner"
    assert sp["bull_weight"] == pytest.approx(0.40)
    assert sp["base_weight"] == pytest.approx(0.50)
    assert sp["bear_weight"] == pytest.approx(0.10)

    # The workbook weight cells themselves carried the edit through this refresh.
    assert support.dashboard_cell(
        dest, redesign.SCEN_WEIGHTS_ROW, redesign.SCEN_COL_BULL
    ) == pytest.approx(0.40)
    assert support.dashboard_cell(
        dest, redesign.SCEN_WEIGHTS_ROW, redesign.SCEN_COL_BEAR
    ) == pytest.approx(0.10)

    # A THIRD refresh (no further owner edit) must not revert the owner's block —
    # it survives the next rebuild, seeded straight back from the JSON.
    res2 = refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)
    assert res2["status"] == "ok", res2
    rd2 = json.loads((adir / "TESTCO.json").read_text(encoding="utf-8"))["redesign"]
    sp2 = rd2["scenario_prior"]
    assert sp2["set_by"] == "owner"
    assert sp2["bull_weight"] == pytest.approx(0.40)
    assert sp2["bear_weight"] == pytest.approx(0.10)
    assert support.dashboard_cell(
        dest, redesign.SCEN_WEIGHTS_ROW, redesign.SCEN_COL_BULL
    ) == pytest.approx(0.40)


def test_apply_edits_identical_save_reuses_existing_revision(
    refresh_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An identical effective Save returns the exact committed revision and
    leaves both the workbook bytes and dcf_runs untouched."""
    import hashlib

    monkeypatch.setattr(refresh_dcf.live_price_mod, "read_live_price", support.fake_read)
    db = refresh_repo / "data" / "portfolio.db"
    dest = refresh_repo / "dcf" / "TESTCO.xlsx"
    refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)

    before_inputs = redesign.read_inputs(dest)
    assert before_inputs is not None
    before_bytes = dest.read_bytes()
    before_mtime = dest.stat().st_mtime_ns
    with sqlite3.connect(str(db)) as conn:
        seeded = conn.execute(
            "SELECT id, input_sha256, workbook_sha256, engine_version, "
            "assumption_snapshot_json, provenance_json FROM dcf_runs "
            "WHERE ticker='TESTCO' AND is_latest=1"
        ).fetchone()
        total_before = conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0]
    assert seeded is not None

    result = refresh_dcf.apply_edits(
        "TESTCO",
        refresh_repo,
        db,
        redesign.RedesignInputs.from_dict(before_inputs.to_dict()),
    )

    assert result["status"] == "ok", result
    assert result.get("reused_existing_revision") is True
    assert result.get("dcf_run_persisted") is False
    assert result["inputs"] == before_inputs.to_dict()
    assert result["model_revision"] == {
        "row_id": seeded[0],
        "ticker": "TESTCO",
        "input_sha256": seeded[1],
        "workbook_sha256": seeded[2],
        "engine_version": seeded[3],
        "provenance_sha256": hashlib.sha256(str(seeded[5]).encode("utf-8")).hexdigest(),
        "assumption_snapshot_sha256": hashlib.sha256(str(seeded[4]).encode("utf-8")).hexdigest(),
    }
    assert dest.read_bytes() == before_bytes
    assert dest.stat().st_mtime_ns == before_mtime
    with sqlite3.connect(str(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0] == total_before


def test_apply_edits_changed_evidence_does_not_reuse_revision(
    refresh_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changed source evidence invalidates reuse even when the posted inputs are
    identical, so the normal staged save produces a new revision."""
    monkeypatch.setattr(refresh_dcf.live_price_mod, "read_live_price", support.fake_read)
    db = refresh_repo / "data" / "portfolio.db"
    dest = refresh_repo / "dcf" / "TESTCO.xlsx"
    refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)

    base_inp = redesign.read_inputs(dest)
    assert base_inp is not None
    with sqlite3.connect(str(db)) as conn:
        seeded_id = conn.execute(
            "SELECT id FROM dcf_runs WHERE ticker='TESTCO' AND is_latest=1"
        ).fetchone()[0]
        total_before = conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0]
    assumptions = refresh_repo / "data" / "dcf_assumptions" / "TESTCO.json"
    payload = json.loads(assumptions.read_text(encoding="utf-8"))
    payload["evidence_marker"] = "changed"
    assumptions.write_text(json.dumps(payload), encoding="utf-8")

    result = refresh_dcf.apply_edits("TESTCO", refresh_repo, db, base_inp)

    assert result["status"] == "ok", result
    assert result.get("reused_existing_revision") is not True
    revision = result["model_revision"]
    assert isinstance(revision, dict)
    assert revision["row_id"] != seeded_id
    with sqlite3.connect(str(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0] == total_before + 1


def test_apply_edits_corrupt_persisted_npv_declines_reuse(
    refresh_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupted persisted valuation output must not be reported as an
    identical reuse: the owner equivalence declines and the normal staged save
    writes a fresh, correct revision."""
    monkeypatch.setattr(refresh_dcf.live_price_mod, "read_live_price", support.fake_read)
    db = refresh_repo / "data" / "portfolio.db"
    dest = refresh_repo / "dcf" / "TESTCO.xlsx"
    refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)

    base_inp = redesign.read_inputs(dest)
    assert base_inp is not None
    with sqlite3.connect(str(db)) as conn:
        seeded = conn.execute(
            "SELECT id, npv FROM dcf_runs WHERE ticker='TESTCO' AND is_latest=1"
        ).fetchone()
        total_before = conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0]
        conn.execute(
            "UPDATE dcf_runs SET npv=? WHERE ticker='TESTCO' AND is_latest=1",
            (float(seeded[1]) + 1000.0,),
        )

    result = refresh_dcf.apply_edits("TESTCO", refresh_repo, db, base_inp)

    assert result["status"] == "ok", result
    assert result.get("reused_existing_revision") is not True
    revision = result["model_revision"]
    assert isinstance(revision, dict)
    assert revision["row_id"] != seeded[0]
    with sqlite3.connect(str(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0] == total_before + 1
        repaired = conn.execute(
            "SELECT npv FROM dcf_runs WHERE ticker='TESTCO' AND is_latest=1"
        ).fetchone()[0]
    assert repaired == pytest.approx(float(seeded[1]))


def test_apply_edits_malformed_stored_snapshot_declines_reuse(
    refresh_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed persisted assumption snapshot is caught at the reuse boundary
    (declined, never raised) and the normal save writes a valid revision."""
    monkeypatch.setattr(refresh_dcf.live_price_mod, "read_live_price", support.fake_read)
    db = refresh_repo / "data" / "portfolio.db"
    dest = refresh_repo / "dcf" / "TESTCO.xlsx"
    refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)

    base_inp = redesign.read_inputs(dest)
    assert base_inp is not None
    with sqlite3.connect(str(db)) as conn:
        seeded_id = conn.execute(
            "SELECT id FROM dcf_runs WHERE ticker='TESTCO' AND is_latest=1"
        ).fetchone()[0]
        total_before = conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0]
        conn.execute(
            "UPDATE dcf_runs SET assumption_snapshot_json=? WHERE ticker='TESTCO' AND is_latest=1",
            ("{not valid json",),
        )

    result = refresh_dcf.apply_edits("TESTCO", refresh_repo, db, base_inp)

    assert result["status"] == "ok", result
    assert result.get("reused_existing_revision") is not True
    revision = result["model_revision"]
    assert isinstance(revision, dict)
    assert revision["row_id"] != seeded_id
    with sqlite3.connect(str(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0] == total_before + 1
        snapshot = conn.execute(
            "SELECT assumption_snapshot_json FROM dcf_runs WHERE ticker='TESTCO' AND is_latest=1"
        ).fetchone()[0]
    assert json.loads(snapshot)


def test_apply_edits_changed_forecast_population_declines_reuse(
    refresh_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the reuse probe computes a different forecast-point population than
    the committed run, reuse is refused and the normal save persists the real
    population."""
    from datetime import date

    from dcf.forecast_series import ForecastSeriesPoint

    monkeypatch.setattr(refresh_dcf.live_price_mod, "read_live_price", support.fake_read)
    db = refresh_repo / "data" / "portfolio.db"
    dest = refresh_repo / "dcf" / "TESTCO.xlsx"
    refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)

    base_inp = redesign.read_inputs(dest)
    assert base_inp is not None
    with sqlite3.connect(str(db)) as conn:
        seeded_id = conn.execute(
            "SELECT id FROM dcf_runs WHERE ticker='TESTCO' AND is_latest=1"
        ).fetchone()[0]
        total_before = conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0]

    real_points = refresh_dcf.redesign_revenue_forecast_points
    probe = ForecastSeriesPoint(
        mapping_revision_id=1,
        series_key="synthetic_reuse_probe",
        period_start=date(2025, 1, 1),
        period_end=date(2025, 12, 31),
        value=12345.0,
    )
    calls: list[object] = []

    def _probe_then_real(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        if len(calls) == 1:
            return (probe,)
        return real_points(*args, **kwargs)

    monkeypatch.setattr(refresh_dcf, "redesign_revenue_forecast_points", _probe_then_real)

    result = refresh_dcf.apply_edits("TESTCO", refresh_repo, db, base_inp)

    assert result["status"] == "ok", result
    assert result.get("reused_existing_revision") is not True
    revision = result["model_revision"]
    assert isinstance(revision, dict)
    assert revision["row_id"] != seeded_id
    assert len(calls) >= 2
    with sqlite3.connect(str(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0] == total_before + 1


def test_apply_edits_source_read_failure_declines_reuse(
    refresh_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure while reading evidence at the reuse boundary declines to the
    normal staged save, which still succeeds."""
    monkeypatch.setattr(refresh_dcf.live_price_mod, "read_live_price", support.fake_read)
    db = refresh_repo / "data" / "portfolio.db"
    dest = refresh_repo / "dcf" / "TESTCO.xlsx"
    refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)

    base_inp = redesign.read_inputs(dest)
    assert base_inp is not None

    real_build = refresh_dcf.build_dcf_provenance
    calls: list[object] = []

    def _fail_then_build(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        if len(calls) == 1:
            raise OSError("simulated unreadable evidence")
        return real_build(*args, **kwargs)

    monkeypatch.setattr(refresh_dcf, "build_dcf_provenance", _fail_then_build)

    result = refresh_dcf.apply_edits("TESTCO", refresh_repo, db, base_inp)

    assert result["status"] == "ok", result
    assert result.get("reused_existing_revision") is not True
    assert result.get("dcf_run_persisted") is True
    assert len(calls) >= 2


def test_apply_edits_missing_country_context_declines_reuse(
    refresh_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unavailable country-risk context declines reuse; the normal staged
    save retains its own specific guard and refuses the write loudly."""
    monkeypatch.setattr(refresh_dcf.live_price_mod, "read_live_price", support.fake_read)
    db = refresh_repo / "data" / "portfolio.db"
    dest = refresh_repo / "dcf" / "TESTCO.xlsx"
    refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)

    base_inp = redesign.read_inputs(dest)
    assert base_inp is not None
    with sqlite3.connect(str(db)) as conn:
        total_before = conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0]

    def _no_prior_context(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(refresh_dcf, "prior_country_risk_context", _no_prior_context)

    result = refresh_dcf.apply_edits("TESTCO", refresh_repo, db, base_inp)

    assert result.get("reused_existing_revision") is not True
    assert result["status"] == "failed"
    assert result["reason"] == "country_risk_context_unavailable"
    with sqlite3.connect(str(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0] == total_before


def test_apply_edits_persists_without_rebuild_and_records_override(
    refresh_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """apply_edits (the in-app save) writes edited assumptions onto the LIVE
    workbook, recomputes + re-persists dcf_runs, and records the change in the
    override ledger WITHOUT overwriting the Opus baseline — no FMP rebuild, the
    market quote carried forward from the prior run."""
    monkeypatch.setattr(refresh_dcf.live_price_mod, "read_live_price", support.fake_read)
    db = refresh_repo / "data" / "portfolio.db"
    dest = refresh_repo / "dcf" / "TESTCO.xlsx"
    # Seed the workbook + dcf_runs + assumptions JSON via a normal refresh.
    refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)

    base_inp = redesign.read_inputs(dest)
    assert base_inp is not None
    m0 = base_inp.exit_multiple
    conn = sqlite3.connect(str(db))
    seeded = conn.execute(
        "SELECT npv_per_share, provenance_json FROM dcf_runs WHERE ticker='TESTCO' AND is_latest=1"
    ).fetchone()
    assert seeded is not None
    npv0 = seeded[0]
    seeded_provenance = json.loads(str(seeded[1]))
    primary_overlay = json.loads(json.dumps(seeded_provenance["primary_fact_overlay"]))
    for key in (
        "schema_version",
        "scope",
        "historical_overlay_locator",
        "historical_overlay_sha256",
        "selected_observation_ids",
    ):
        primary_overlay.pop(key, None)
    for statement in primary_overlay["statements"].values():
        statement.pop("model_input_observation_ids", None)
    primary_overlay["statements"]["income"] = {
        "status": "ok",
        "applied": [
            {
                "fact_id": 42,
                "source_url": "https://www.sec.gov/example",
                "as_of": "2026-08-20T12:00:00+00:00",
            }
        ],
    }
    prior_country_context = {
        "schema_version": "dcf_country_risk_context.v1",
        "ticker": "TESTCO",
        "premium": base_inp.country_risk_premium,
        "authority": "systematic_geo",
        "source_record": {
            "role": "geographic_revenue",
            "path": "data/historical/fmp/TESTCO_geo_segments_annual.json",
            "sha256": "a" * 64,
            "bytes": 123,
            "observed_at": "2026-08-20T12:00:00+00:00",
            "influences_calculation": True,
            "selection": "annual_latest_fiscal_year",
        },
    }
    conn.execute(
        "UPDATE dcf_runs SET provenance_json=? WHERE ticker='TESTCO' AND is_latest=1",
        (
            json.dumps(
                {
                    **seeded_provenance,
                    "primary_fact_overlay": primary_overlay,
                    "country_risk_context": prior_country_context,
                }
            ),
        ),
    )
    conn.commit()
    conn.close()

    payload = base_inp.to_dict()
    payload["exit_multiple"] = m0 + 3.0  # a clean ledger-tracked scalar
    payload["country_risk_premium"] = base_inp.country_risk_premium + 0.01
    res = refresh_dcf.apply_edits(
        "TESTCO", refresh_repo, db, redesign.RedesignInputs.from_dict(payload)
    )
    assert res["status"] == "ok", res
    revision = res["model_revision"]
    assert isinstance(revision, dict)
    with sqlite3.connect(db) as saved_conn:
        saved_row = saved_conn.execute(
            "SELECT id, input_sha256, workbook_sha256, engine_version FROM dcf_runs "
            "WHERE ticker='TESTCO' AND is_latest=1"
        ).fetchone()
    assert saved_row == (
        revision["row_id"],
        revision["input_sha256"],
        revision["workbook_sha256"],
        revision["engine_version"],
    )

    # The edit landed on the live workbook (no rebuild) and lifts the value.
    edited = redesign.read_inputs(dest)
    assert edited is not None and edited.exit_multiple == pytest.approx(m0 + 3.0)
    assert cast("float", res["fair_value_per_share"]) > float(npv0)

    # dcf_runs re-persisted; the prior market quote was carried forward.
    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT npv_per_share, live_price, provenance_json FROM dcf_runs WHERE ticker='TESTCO' AND is_latest=1"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[1] == pytest.approx(50.0)
    assert res["fair_value_per_share"] == pytest.approx(float(row[0]))
    persisted_provenance = json.loads(str(row[2]))
    assert persisted_provenance["primary_fact_overlay"] == primary_overlay
    persisted_country = persisted_provenance["country_risk_context"]
    assert persisted_country == {
        "schema_version": "dcf_country_risk_context.v1",
        "ticker": "TESTCO",
        "premium": pytest.approx(base_inp.country_risk_premium + 0.01),
        "authority": "owner_override",
        "source_record": None,
    }
    assert all(
        source.get("role") != "geographic_revenue" for source in persisted_provenance["sources"]
    )

    # The override ledger records the edit; the Opus baseline is NOT overwritten.
    adata = json.loads(
        (refresh_repo / "data" / "dcf_assumptions" / "TESTCO.json").read_text(encoding="utf-8")
    )
    assert adata["opus_baseline"]["values"]["exit_multiple"] == pytest.approx(m0)
    assert adata["assumption_overrides"]["exit_multiple"]["opus_value"] == pytest.approx(m0)
    assert adata["redesign"]["exit_multiple"] == pytest.approx(m0 + 3.0)


@pytest.mark.parametrize(
    ("is_latest_column", "segment_column", "created_at_column"),
    (
        (False, False, False),
        (False, False, True),
        (False, True, False),
        (False, True, True),
        (True, False, False),
        (True, False, True),
        (True, True, False),
        (True, True, True),
    ),
)
def test_prior_primary_overlay_supports_only_explicit_static_schema_variants(
    tmp_path: Path,
    is_latest_column: bool,
    segment_column: bool,
    created_at_column: bool,
) -> None:
    db = tmp_path / "portfolio.db"
    optional_columns: list[str] = []
    optional_values: list[object] = []
    if is_latest_column:
        optional_columns.append("is_latest INTEGER")
        optional_values.append(1)
    if segment_column:
        optional_columns.append("segment_name TEXT")
        optional_values.append("")
    if created_at_column:
        optional_columns.append("created_at TEXT")
        optional_values.append("2026-08-20T12:00:00+00:00")
    schema_suffix = ", " + ", ".join(optional_columns) if optional_columns else ""
    insert_columns = ["id", "ticker", "provenance_json"] + [
        column.split()[0] for column in optional_columns
    ]
    placeholders = ", ".join("?" for _ in insert_columns)
    overlay: dict[str, object] = {"status": "ok", "statements": {}}
    bridge_context: dict[str, object] = {
        "schema_version": "dcf_equity_bridge_context.v2",
        "ticker": "TESTCO",
        "period_end": "2026-06-30",
        "fiscal_period_type": "Q2",
        "reporting_currency": "USD",
        "cash_m": 200.0,
        "total_debt_m": 100.0,
        "diluted_shares_m": 10.0,
        "cash_basis": "reported_aggregate",
        "total_debt_basis": "reported_aggregate",
        "debt_scope": "interest_bearing_debt_only",
        "debt_calculation": ("debt_and_capital_lease_obligations - finance_lease_liability"),
        "debt_operations": [
            {"field": "totalDebt", "sign": 1},
            {"field": "financeLeaseLiability", "sign": -1},
        ],
        "debt_component_lineage": [],
    }
    country_context: dict[str, object] = {
        "schema_version": "dcf_country_risk_context.v1",
        "ticker": "TESTCO",
        "premium": 0.0,
        "authority": "owner_override",
        "source_record": None,
    }
    payload = json.dumps(
        {
            "ticker": "TESTCO",
            "primary_fact_overlay": overlay,
            "equity_bridge_receipt": {
                "schema_version": "dcf_equity_bridge_receipt.v3",
                "bridge_context": bridge_context,
            },
            "country_risk_context": country_context,
        }
    )

    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE dcf_runs "
            f"(id INTEGER PRIMARY KEY, ticker TEXT, provenance_json TEXT{schema_suffix})"
        )
        conn.execute(
            f"INSERT INTO dcf_runs ({', '.join(insert_columns)}) VALUES ({placeholders})",
            (1, "TESTCO", payload, *optional_values),
        )

    assert refresh_dcf.prior_primary_fact_overlay(db, "testco") == overlay
    assert refresh_dcf.prior_equity_bridge_context(db, "testco") == bridge_context
    assert refresh_dcf.prior_country_risk_context(db, "testco") == country_context


def test_save_cleanup_failure_preserves_committed_truth_and_blocks_next_write(
    refresh_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(refresh_dcf.live_price_mod, "read_live_price", support.fake_read)
    db = refresh_repo / "data" / "portfolio.db"
    accepted = refresh_repo / "dcf" / "TESTCO.xlsx"
    assert (
        refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)["status"] == "ok"
    )
    old_bytes = accepted.read_bytes()
    inp = redesign.read_inputs(accepted)
    assert inp is not None
    edited = dataclasses.replace(inp, beta=inp.beta + 0.3)
    original_unlink = Path.unlink

    def fail_backup_unlink(path: Path, missing_ok: bool = False) -> None:
        if ".rollback." in path.name:
            raise OSError("synthetic cleanup failure")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_backup_unlink)
    result = refresh_dcf.apply_edits("TESTCO", refresh_repo, db, edited)
    assert result["status"] == "committed_cleanup_failed" and result["recovery_required"] is True
    revision = result["model_revision"]
    assert isinstance(revision, dict)
    with sqlite3.connect(db) as saved_conn:
        assert saved_conn.execute("SELECT id FROM dcf_runs WHERE is_latest=1").fetchone() == (
            revision["row_id"],
        )
    committed = redesign.read_inputs(accepted)
    assert committed is not None and result["inputs"] == committed.to_dict()
    assert accepted.read_bytes() != old_bytes
    backups = list(accepted.parent.glob("TESTCO.rollback.*.xlsx"))
    assert len(backups) == 1 and backups[0].read_bytes() == old_bytes
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT is_latest FROM dcf_runs ORDER BY id").fetchall()
    assert rows == [(0,), (1,)]
    blocked = refresh_dcf.apply_edits("TESTCO", refresh_repo, db, edited)
    assert blocked["reason"] == "dcf_recovery_required"
    after_block = redesign.read_inputs(accepted)
    assert after_block is not None and result["inputs"] == after_block.to_dict()


def test_apply_edits_no_workbook_fails_soft(refresh_repo: Path) -> None:
    """No redesigned workbook to edit → a soft failure, not a crash (the route
    maps this to 409)."""
    db = refresh_repo / "data" / "portfolio.db"
    res = refresh_dcf.apply_edits(
        "TESTCO", refresh_repo, db, redesign.RedesignInputs.from_dict(support.BASE.to_dict())
    )
    assert res["status"] == "failed"
    assert "no redesigned workbook" in str(res["reason"])


def test_refresh_skips_dcf_not_applicable(
    refresh_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-bank financial (asset-manager/insurer) Opus flagged dcf_applicable=
    false skips before any build — only credit banks route to the bank model."""
    assumptions = refresh_repo / "data" / "dcf_assumptions"
    assumptions.mkdir(parents=True, exist_ok=True)
    (assumptions / "TESTCO.json").write_text(
        json.dumps({"redesign": {"dcf_applicable": False, "business_model": "asset_manager"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(refresh_dcf.live_price_mod, "read_live_price", support.fake_read)
    db = refresh_repo / "data" / "portfolio.db"
    res = refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)
    assert res["status"] == "skipped"
    assert "asset_manager" in str(res["reason"])
    assert not (refresh_repo / "dcf" / "TESTCO.xlsx").exists()  # never built


def test_refresh_bank_dispatches_to_bank_model(
    refresh_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A credit bank (business_model=bank) is routed to the equity-side bank
    model (`_refresh_bank`), NOT skipped — it returns format='bank'."""
    assumptions = refresh_repo / "data" / "dcf_assumptions"
    assumptions.mkdir(parents=True, exist_ok=True)
    (assumptions / "TESTCO.json").write_text(
        json.dumps({"redesign": {"dcf_applicable": False, "business_model": "bank"}}),
        encoding="utf-8",
    )
    db = refresh_repo / "data" / "portfolio.db"
    res = refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)
    assert res["status"] != "skipped"  # dispatched to the bank builder
    assert res["format"] == "bank"  # (status is 'failed' here — no TESTCO FMP fixture data)


def test_valuation_model_dispatches_to_holdco(refresh_repo: Path) -> None:
    """An explicit valuation_model='holdco_sotp' routes to the SOTP builder."""
    assumptions = refresh_repo / "data" / "dcf_assumptions"
    assumptions.mkdir(parents=True, exist_ok=True)
    (assumptions / "TESTCO.json").write_text(
        json.dumps({"redesign": {"dcf_applicable": False, "valuation_model": "holdco_sotp"}}),
        encoding="utf-8",
    )
    db = refresh_repo / "data" / "portfolio.db"
    res = refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)
    assert res["status"] != "skipped"
    assert res["format"] == "holdco_sotp"


def test_holdco_refresh_threads_live_owner_inputs_and_atomic_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = tmp_path / "dcf" / "BN.xlsx"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"owner workbook")
    captured_env: dict[str, str] = {}

    def fake_run(*_args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        env = cast("dict[str, str]", kwargs["env"])
        captured_env.update(env)
        Path(env["DCF_PROMOTE_DEST"]).write_bytes(b"promoted workbook")
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="RESULT\tBN\tdcf_runs=ok\t-> staged\n",
            stderr="",
        )

    monkeypatch.setattr(refresh_dcf.subprocess, "run", fake_run)

    holdings = tmp_path / "micro_thesis" / "holdings"
    holdings.mkdir(parents=True)
    (holdings / "BN.json").write_text(json.dumps({"valuation_model": "holdco_sotp"}))
    result = refresh_dcf.refresh_one(
        "BN", tmp_path, tmp_path / "unused.sqlite", valuation_year=2026
    )

    assert result["status"] == "ok"
    assert captured_env["DCF_OWNER_INPUTS_DEST"] == str(live)
    assert captured_env["DCF_PROMOTE_DEST"] == str(live)
    staged = Path(captured_env["DCF_DEST"])
    assert staged.parent == live.parent
    assert staged.name.startswith("BN.rebuild.")
    assert staged.suffix == ".xlsx"
    assert staged != live
    assert live.read_bytes() == b"promoted workbook"


def test_specialized_refresh_rejects_skip_without_replacing_live_workbook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = tmp_path / "dcf" / "NU.xlsx"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"current workbook")

    def fake_run(*_args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        env = cast("dict[str, str]", kwargs["env"])
        Path(env["DCF_DEST"]).write_bytes(b"unpersisted candidate")
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="RESULT\tNU\tdcf_runs=skip\t-> staged\n",
            stderr="",
        )

    monkeypatch.setattr(refresh_dcf.subprocess, "run", fake_run)

    holdings = tmp_path / "micro_thesis" / "holdings"
    holdings.mkdir(parents=True)
    (holdings / "NU.json").write_text(json.dumps({"valuation_model": "platform_dcf"}))
    result = refresh_dcf.refresh_one(
        "NU", tmp_path, tmp_path / "unused.sqlite", valuation_year=2026
    )

    assert result["status"] == "failed"
    assert live.read_bytes() == b"current workbook"
    assert not (tmp_path / "dcf" / "NU.rebuild.xlsx").exists()


def test_valuation_model_dispatches_to_fintech_sotp(refresh_repo: Path) -> None:
    """An explicit valuation_model='fintech_sotp' routes to the fintech SOTP builder."""
    assumptions = refresh_repo / "data" / "dcf_assumptions"
    assumptions.mkdir(parents=True, exist_ok=True)
    (assumptions / "TESTCO.json").write_text(
        json.dumps({"redesign": {"dcf_applicable": False, "valuation_model": "fintech_sotp"}}),
        encoding="utf-8",
    )
    db = refresh_repo / "data" / "portfolio.db"
    res = refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)
    assert res["status"] != "skipped"
    assert res["format"] == "fintech_sotp"


def test_valuation_model_dispatches_to_platform_dcf(refresh_repo: Path) -> None:
    """An explicit valuation_model='platform_dcf' routes to the customer-driven platform DCF."""
    assumptions = refresh_repo / "data" / "dcf_assumptions"
    assumptions.mkdir(parents=True, exist_ok=True)
    (assumptions / "TESTCO.json").write_text(
        json.dumps({"redesign": {"dcf_applicable": False, "valuation_model": "platform_dcf"}}),
        encoding="utf-8",
    )
    db = refresh_repo / "data" / "portfolio.db"
    res = refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)
    assert res["status"] != "skipped"
    assert res["format"] == "platform_dcf"


def test_valuation_model_holdings_override_wins(refresh_repo: Path) -> None:
    """The holdings valuation_model override beats the dcf_assumptions one."""
    (refresh_repo / "data" / "dcf_assumptions").mkdir(parents=True, exist_ok=True)
    (refresh_repo / "data" / "dcf_assumptions" / "TESTCO.json").write_text(
        json.dumps({"redesign": {"valuation_model": "holdco_sotp"}}), encoding="utf-8"
    )
    holdings = refresh_repo / "micro_thesis" / "holdings"
    holdings.mkdir(parents=True, exist_ok=True)
    (holdings / "TESTCO.json").write_text(
        json.dumps({"ticker": "TESTCO", "valuation_model": "bank_excess_return"}), encoding="utf-8"
    )
    db = refresh_repo / "data" / "portfolio.db"
    res = refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)
    assert res["format"] == "bank"  # holdings override won over the dcf_assumptions holdco


def test_valuation_model_new_skips_with_suggestion(refresh_repo: Path) -> None:
    """valuation_model='new' has no template → skip, surfacing the Opus suggestion."""
    (refresh_repo / "data" / "dcf_assumptions").mkdir(parents=True, exist_ok=True)
    (refresh_repo / "data" / "dcf_assumptions" / "TESTCO.json").write_text(
        json.dumps(
            {
                "redesign": {
                    "valuation_model": "new",
                    "valuation_model_suggestion": "insurance embedded value — book + PV(in-force)",
                }
            }
        ),
        encoding="utf-8",
    )
    db = refresh_repo / "data" / "portfolio.db"
    res = refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)
    assert res["status"] == "skipped"
    assert res["valuation_model"] == "new"
    assert "embedded value" in str(res["reason"])  # the suggestion is surfaced


def test_refresh_redesign_negative_fair_value_nulls_over_under(
    refresh_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user forcing deeply negative margins yields a negative fair value;
    refresh_one persists it with over_under None (the #291 guard) rather than
    crashing on the over/under calc."""
    monkeypatch.setattr(refresh_dcf.live_price_mod, "read_live_price", support.fake_read)
    db = refresh_repo / "data" / "portfolio.db"
    dest = refresh_repo / "dcf" / "TESTCO.xlsx"
    refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)
    wb = openpyxl.load_workbook(str(dest))
    dsh = wb["Dashboard"]
    dsh.cell(row=29, column=2, value=-1.0)  # near op margin -100%
    dsh.cell(row=30, column=2, value=-1.0)  # terminal op margin -100%
    wb.save(str(dest))
    wb.close()

    res = refresh_dcf.refresh_one("TESTCO", refresh_repo, db, valuation_year=2026)
    assert res["status"] == "ok"
    fv = res["fair_value_per_share"]
    assert isinstance(fv, float) and fv < 0
    assert res["over_under_pct"] is None


def test_gsheets_reingest_carries_dashboard_edit_to_dcf_runs(
    refresh_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Google-Sheets re-ingest path (dcf_sheets import --file -> refresh_one)
    carries a Dashboard edit on the pulled workbook through to the persisted
    dcf_runs value — the user edits in Sheets, pulls down, re-ingests."""
    monkeypatch.setattr(refresh_dcf.live_price_mod, "read_live_price", support.fake_read)
    repo = refresh_repo
    # A redesign workbook standing in for the pulled Sheet; bump terminal margin.
    downloaded = repo / "downloaded_TESTCO.xlsx"
    support.build_workbook(repo, "TESTCO", downloaded)
    base = redesign.read_and_value(downloaded)
    assert base is not None
    wb = openpyxl.load_workbook(str(downloaded))
    wb["Dashboard"].cell(row=30, column=2, value=0.40)  # terminal op margin up
    wb.save(str(downloaded))
    wb.close()
    edited = redesign.read_and_value(downloaded)
    assert edited is not None and edited.value_per_share_usd > base.value_per_share_usd

    rc = dcf_sheets.main(
        [
            "import",
            "--ticker",
            "TESTCO",
            "--file",
            str(downloaded),
            "--repo-root",
            str(repo),
            "--valuation-year",
            "2026",
        ]
    )
    assert rc == 0
    assert (repo / "dcf" / "TESTCO.xlsx").exists()  # placed at the canonical path

    conn = sqlite3.connect(str(repo / "data" / "portfolio.db"))
    row = conn.execute(
        "SELECT npv_per_share FROM dcf_runs WHERE ticker='TESTCO' AND is_latest=1"
    ).fetchone()
    conn.close()
    assert row is not None and row[0] is not None
    # The persisted value reflects the edited (higher) terminal margin.
    assert float(row[0]) == pytest.approx(edited.value_per_share_usd, rel=0.05)
    assert float(row[0]) > base.value_per_share_usd


@pytest.mark.parametrize("cash_equivalents_only", [False, True])
def test_builder_reader_preserve_exact_cash_field_and_governed_debt_scope(
    tmp_path: Path, cash_equivalents_only: bool
) -> None:
    repo = tmp_path / "fixture"
    support.write_redesign_fmp(repo, "TEST")
    balance_path = repo / "data" / "historical" / "fmp" / "TEST_balance_sheet_quarterly.json"
    balances = cast("list[dict[str, object]]", json.loads(balance_path.read_text()))
    for record in balances:
        if cash_equivalents_only:
            record["cashAndCashEquivalents"] = record.pop("cashAndShortTermInvestments")
    balance_path.write_text(json.dumps(balances))
    # The same released debt-perimeter oracle now receives immutable canonical
    # observations, not mutable financial_facts overlays.
    for record in balances:
        record["totalDebt"] = 130_000_000
        record["financeLeaseLiability"] = 30_000_000
    fmp = repo / "data" / "historical" / "fmp"
    income = cast(
        "list[dict[str, object]]",
        json.loads((fmp / "TEST_income_statement_quarterly.json").read_text()),
    )
    cashflow = cast(
        "list[dict[str, object]]", json.loads((fmp / "TEST_cash_flow_quarterly.json").read_text())
    )
    support.write_statement_facts(repo, "TEST", income, balances, cashflow, currency="USD")
    dest = tmp_path / "TEST.xlsx"
    builder_value = support.build_workbook(repo, "TEST", dest)
    inputs = redesign.read_inputs(dest)
    assert inputs is not None
    assert inputs.total_debt_m == pytest.approx(100.0)
    assert redesign.value(inputs).value_per_share_usd == pytest.approx(builder_value, rel=0.03)
    workbook = openpyxl.load_workbook(dest, read_only=True)
    try:
        labels = [row[0].value for row in workbook["Financials"].iter_rows(min_col=1, max_col=1)]
        expected_cash_label = (
            "Cash & Equivalents" if cash_equivalents_only else "Cash & ST Investments"
        )
        assert expected_cash_label in labels
    finally:
        workbook.close()


def test_generic_builder_rejects_raw_statements_without_canonical_bindings(tmp_path: Path) -> None:
    support.write_redesign_fmp(tmp_path, "TESTCO", canonical_bindings=False)
    destination = tmp_path / "rejected.xlsx"
    result = subprocess.run(
        [sys.executable, str(support.BUILDER)],
        env={
            **os.environ,
            "DCF_TICKER": "TESTCO",
            "DCF_REPO_ROOT": str(tmp_path),
            "DCF_DEST": str(destination),
            "EARNINGS_SUMMARY_DB_PATH": str(tmp_path / "data" / "portfolio.db"),
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode != 0
    assert not destination.exists()


def test_workbook_retains_complete_canonical_statement_manifest(
    built_usd: tuple[Path, float],
) -> None:
    path, _ = built_usd
    wb = openpyxl.load_workbook(path)
    try:
        evidence = wb["Statement Evidence"]
        assert evidence.sheet_state == "hidden"
        payload = "".join(
            str(evidence.cell(row, 2).value) for row in range(1, evidence.max_row + 1)
        )
        manifest = DcfStatementInputs.model_validate_json(payload)
        manifest.require_complete_actuals()
        assert manifest.ticker == "TESTCO"
        assert len(manifest.cells) > 100
        assert all(
            cell.provenance and cell.definition_revision_id and cell.resolution_revision_id
            for cell in manifest.cells
        )
    finally:
        wb.close()
