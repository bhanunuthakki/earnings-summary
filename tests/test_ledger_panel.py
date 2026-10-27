"""The Ledger flat musings panel — rendering over the analyst_notes spine."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from capture import ingest
from capture.matcher import build_roster_index
from pipeline.ledger_panel import render_ledger_list, render_ledger_panel

PRIOR_HEAD = "0059_kpi_facts_restatement"


@pytest.fixture
def db_path(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    return migrated_db(tmp_path / "ledger.db", stamp=PRIOR_HEAD)


def test_empty_panel_shows_capture_box(db_path: Path) -> None:
    html = render_ledger_panel(db_path)
    assert "No notes yet" in html
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


def test_capture_box_keeps_logging_and_has_no_coaching_controls(db_path: Path) -> None:
    from pipeline.ledger_panel import LEDGER_CAPTURE_JS

    html = render_ledger_panel(db_path)
    assert 'id="ledger-cap-btn"' in html and 'id="ledger-cap-text"' in html
    assert "ledger-cap-coach" not in html
    assert "pledge_challenge" not in html
    assert LEDGER_CAPTURE_JS.count("/api/capture/text") == 1
    assert "annotated_decision_id" not in LEDGER_CAPTURE_JS
    assert "e.metaKey||e.ctrlKey" in LEDGER_CAPTURE_JS
    assert "pollAnswer" not in LEDGER_CAPTURE_JS
