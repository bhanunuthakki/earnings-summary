# pyright: reportPrivateUsage=false
"""S12 PR2 — the valuation card names the archetype that produced the number.

Covers the three layers (the S6 scenario-range test shape):
  * snapshot._model_label — "model" tag → label, format="redesign" → FCFF DCF,
    unknown tags pass through raw, legacy/malformed rows stay None;
  * snapshot._valuation_snapshot — the label flows off a fixture dcf_runs row
    into ValuationSnapshot.valuation_model_label;
  * the renderers — workspace panel sub slot shows the label (generic "DCF"
    for unlabeled rows); markdown meta line gains "Model: …".
"""

from __future__ import annotations

import json
import sqlite3
import sys
from io import StringIO
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from report.models import (  # noqa: E402
    SectionStatus,
    SnapshotSection,
    ValuationSnapshot,
)
from report.renderers.markdown import _valuation_card_md  # noqa: E402
from report.renderers.workspace_html import _valuation_summary_panel  # noqa: E402
from report.sections import snapshot as snapshot_mod  # noqa: E402

# --------------------------------------------------------------------------- #
# _model_label parsing
# --------------------------------------------------------------------------- #


def test_model_tags_map_to_card_labels() -> None:
    lab = snapshot_mod._model_label
    assert lab(json.dumps({"model": "holdco_sotp"})) == "SOTP / NAV"
    assert lab(json.dumps({"model": "holdco_sotp_brk"})) == "SOTP / NAV"
    assert lab(json.dumps({"model": "bank_excess_return"})) == "Excess return"
    assert lab(json.dumps({"model": "platform_dcf"})) == "Platform DCF"
    assert lab(json.dumps({"model": "fintech_sotp"})) == "Fintech SOTP"


def test_redesign_format_labels_fcff() -> None:
    assert snapshot_mod._model_label(json.dumps({"format": "redesign"})) == "FCFF DCF"


def test_unknown_model_tag_passes_through_raw() -> None:
    """A new archetype labels itself without a code change here."""
    assert snapshot_mod._model_label(json.dumps({"model": "weird_new_model"})) == "weird_new_model"


def test_legacy_and_malformed_rows_stay_unlabeled() -> None:
    lab = snapshot_mod._model_label
    assert lab(None) is None
    assert lab("") is None
    assert lab("{not json") is None
    assert lab(json.dumps({"wacc": 0.09})) is None  # pre-tagging row
    assert lab(json.dumps({"model": ""})) is None


# --------------------------------------------------------------------------- #
# dcf_runs row -> ValuationSnapshot (fixture DB)
# --------------------------------------------------------------------------- #


def _seed_repo(tmp_path: Path, snapshot_json: str | None) -> Path:
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    conn = sqlite3.connect(str(repo / "data" / "portfolio.db"))
    conn.executescript(
        """
        CREATE TABLE dcf_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT UNIQUE,
            valuation_date TEXT, horizon_years INTEGER,
            wacc REAL, terminal_growth REAL,
            npv REAL, npv_per_share REAL, shares_outstanding REAL,
            currency TEXT, notes TEXT, run_id TEXT,
            live_price REAL, live_price_at TEXT, over_under_pct REAL,
            mos_bar_used REAL, assumption_snapshot_json TEXT,
            revenue_growths_json TEXT, fcf_margin REAL,
            breakdown_json TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO dcf_runs (ticker, valuation_date, horizon_years, wacc, terminal_growth,"
        " npv, npv_per_share, shares_outstanding, currency, live_price, over_under_pct,"
        " mos_bar_used, assumption_snapshot_json) VALUES"
        " ('TEST', '2026-06-12', 0, 0.10, 0, 115568, 48.74, 2371000000.0, 'USD',"
        "  44.61, -0.0848, 0.20, ?)",
        (snapshot_json,),
    )
    conn.commit()
    conn.close()
    return repo


def test_valuation_snapshot_carries_model_label(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path, json.dumps({"model": "holdco_sotp"}))
    v = snapshot_mod._valuation_snapshot(
        "TEST", repo, current_price=None, model_link=None, mos_bar=None
    )
    assert v.valuation_model_label == "SOTP / NAV"


def test_valuation_snapshot_legacy_row_stays_unlabeled(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path, None)
    v = snapshot_mod._valuation_snapshot(
        "TEST", repo, current_price=None, model_link=None, mos_bar=None
    )
    assert v.valuation_model_label is None


# --------------------------------------------------------------------------- #
# renderers
# --------------------------------------------------------------------------- #


def _render_panel(v: ValuationSnapshot) -> str:
    snap = SnapshotSection(status=SectionStatus.OK, ticker="TEST", valuation=v)
    body = StringIO()
    _valuation_summary_panel(body, snap)
    return body.getvalue()


def test_panel_sub_names_the_archetype() -> None:
    html_out = _render_panel(
        ValuationSnapshot(
            consolidated_npv_per_share=48.74,
            current_price=44.61,
            valuation_model_label="SOTP / NAV",
        )
    )
    assert "SOTP / NAV" in html_out


def test_panel_sub_falls_back_to_generic_dcf() -> None:
    html_out = _render_panel(
        ValuationSnapshot(consolidated_npv_per_share=48.74, current_price=44.61)
    )
    assert ">DCF<" in html_out or "DCF" in html_out
    assert "SOTP" not in html_out


def test_markdown_meta_line_gains_model() -> None:
    out = StringIO()
    _valuation_card_md(
        out,
        ValuationSnapshot(
            consolidated_npv_per_share=48.74,
            current_price=44.61,
            wacc=0.10,
            valuation_model_label="SOTP / NAV",
        ),
    )
    assert "Model: SOTP / NAV" in out.getvalue()


def test_markdown_meta_line_unchanged_without_label() -> None:
    out = StringIO()
    _valuation_card_md(
        out,
        ValuationSnapshot(consolidated_npv_per_share=48.74, current_price=44.61, wacc=0.10),
    )
    assert "Model:" not in out.getvalue()
