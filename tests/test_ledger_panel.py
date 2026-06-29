"""The Ledger flat musings panel — rendering over the analyst_notes spine."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from capture import ingest
from capture.matcher import build_roster_index
from pipeline.ledger_panel import render_ledger_list, render_ledger_panel

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0059_kpi_facts_restatement"


def _cfg(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "ledger.db"
    cfg = _cfg(db)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, "head")
    return db


def test_empty_panel_shows_capture_box(db_path: Path) -> None:
    html = render_ledger_panel(db_path)
    assert "No musings yet" in html
    assert "ledger-cap" in html
    assert ">Capture<" in html
    assert "/api/capture/text" in html  # the capture box POST target


def test_panel_lists_musings_newest_first(db_path: Path) -> None:
    roster = build_roster_index(symbols=["NU"], phrases={"nubank": "NU"})
    ingest.ingest_capture(
        channel="tray", text="Nubank NPL formation worries me", roster=roster, db_path=db_path
    )
    ingest.ingest_capture(
        channel="telegram", text="the macro tape feels off today", roster=roster, db_path=db_path
    )
    html = render_ledger_list(db_path)
    assert "NPL formation worries me" in html
    assert "macro tape feels off" in html
    assert "ledger-musing" in html
    assert "unattributed" in html  # the macro musing has no roster name


def test_needs_ticker_chip_renders(db_path: Path) -> None:
    roster = build_roster_index(symbols=["NU", "MELI"], phrases={})
    ingest.ingest_capture(
        channel="tray", text="NU and MELI both look compelling", roster=roster, db_path=db_path
    )
    html = render_ledger_list(db_path)
    assert "needs ticker" in html
