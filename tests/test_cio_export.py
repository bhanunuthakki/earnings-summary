"""Tests for the Personal-CIO xlsx export (src/dashboard/cio_export.py).

The CIO content (alerts → queued actions → thesis ledger) is surfaced as HTML
but previously had no spreadsheet export. The exporter reads the substrate via
the stores and writes a three-sheet workbook; it must produce a valid file even
when the substrate is empty.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from alembic.config import Config
from openpyxl import load_workbook

from alembic import command
from alerts import compute_signature_sha, fire_alert, queue_action
from dashboard.cio_export import export_cio_workbook
from user_state.ledger import append_entry

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_PRIOR_HEAD = "0059_kpi_facts_restatement"
_FIRED_AT = datetime(2026, 6, 1, 12, 0)  # naive-UTC, per the repo convention


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "portfolio.db"
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.stamp(cfg, _PRIOR_HEAD)
    command.upgrade(cfg, "head")
    return db


def test_export_empty_substrate_is_header_only(tmp_path: Path, db_path: Path) -> None:
    out = export_cio_workbook(tmp_path / "cio.xlsx", db_path=db_path)
    wb = load_workbook(out)
    assert wb.sheetnames == ["Alerts", "Queued Actions", "Thesis Ledger"]
    # Each sheet has only its header row when the substrate is empty.
    assert wb["Alerts"].max_row == 1
    assert wb["Queued Actions"].max_row == 1
    assert wb["Thesis Ledger"].max_row == 1
    assert [c.value for c in wb["Alerts"][1]][:3] == ["id", "fired_at", "ticker"]


def test_export_populated_substrate(tmp_path: Path, db_path: Path) -> None:
    alert = fire_alert(
        ticker="NU",
        trigger_kind="earnings_tone",
        fired_at=_FIRED_AT,
        evidence_json=json.dumps({"summary": "NIM contracted 100bps QoQ"}),
        signature_sha=compute_signature_sha("earnings_tone", "NU", {"k": "v"}),
        db_path=db_path,
    )
    queue_action(
        alert_id=alert.id,
        action_kind="thesis_update",
        payload={"note": "watch NIM"},
        db_path=db_path,
    )
    append_entry(
        ticker="NU",
        entry_kind="thesis_update",
        body="Thesis: NIM under pressure",
        source_alert_id=alert.id,
        db_path=db_path,
    )

    wb = load_workbook(export_cio_workbook(tmp_path / "cio.xlsx", db_path=db_path))

    alerts_row = [c.value for c in wb["Alerts"][2]]
    assert "NU" in alerts_row
    assert "earnings_tone" in alerts_row
    assert any("NIM contracted" in str(c) for c in alerts_row)  # summary flattened in

    actions_row = [c.value for c in wb["Queued Actions"][2]]
    assert "thesis_update" in actions_row
    assert any("watch NIM" in str(c) for c in actions_row)  # payload serialized in

    ledger_row = [c.value for c in wb["Thesis Ledger"][2]]
    assert any("NIM under pressure" in str(c) for c in ledger_row)
