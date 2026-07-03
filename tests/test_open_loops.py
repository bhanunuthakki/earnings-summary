"""The open-loops ritual-debt band — queue counts, doorways, ritual-clear state,
and the never-raises guarantee on thin/pre-migration DBs."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from capture import ingest  # noqa: E402
from capture.matcher import build_roster_index  # noqa: E402
from pipeline.command_center_shell import render_overview_panel  # noqa: E402
from pipeline.open_loops import render_open_loops_band  # noqa: E402

PRIOR_HEAD = "0059_kpi_facts_restatement"

# ``decisions`` predates the 0059 stamp (db.init_db() territory), and 0130's
# extension is _has_table-guarded, so the stamp+upgrade fixture never creates
# it — hand-build the modern shape the band queries (the test_governor.py
# pattern, post-upgrade so no migration conflicts).
_DECISIONS_DDL = """
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16),
    recommendation_kind VARCHAR(32) NOT NULL,
    conviction VARCHAR(16),
    outcome_label VARCHAR(16) NOT NULL DEFAULT 'pending',
    decided_by VARCHAR(16) NOT NULL DEFAULT 'advisor',
    scope VARCHAR(16) NOT NULL DEFAULT 'ticker',
    falsifier TEXT,
    size_usd FLOAT,
    user_notes TEXT,
    made_at DATETIME NOT NULL,
    created_at DATETIME NOT NULL
);
"""


def _cfg(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "loops.db"
    cfg = _cfg(db)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, "head")
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(_DECISIONS_DDL)
        conn.commit()
    finally:
        conn.close()
    return db


def test_ritual_clear_on_empty_db(db_path: Path) -> None:
    html = render_open_loops_band(db_path)
    assert "Ritual clear" in html
    assert "cc-open-loops" in html


def test_reconcile_line_counts_and_doors(db_path: Path) -> None:
    roster = build_roster_index(symbols=["NU"], phrases={})
    result = ingest.ingest_capture(
        channel="tray", text="seed thought", roster=roster, db_path=db_path
    )
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE analyst_notes SET source_ref = 'seed:decision:1' WHERE id = ?",
            (result.note_id,),
        )
        conn.commit()
    finally:
        conn.close()
    html = render_open_loops_band(db_path)
    assert "Reconcile" in html
    assert 'href="/#musings"' in html
    assert "Ritual clear" not in html


def test_decision_stub_debt_line(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, decided_by, "
            "made_at, created_at) VALUES ('NU', 'add', 'owner', '2026-06-01', '2026-06-01')"
        )
        conn.commit()
    finally:
        conn.close()
    html = render_open_loops_band(db_path)
    assert "Decisions missing conviction/falsifier" in html
    assert 'href="/#decisions_record"' in html
    assert "oldest" in html  # the aged stub carries its debt age


def test_digest_ping_line(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO coach_pings (class_, key, ticker, body, status, source_ref, "
            "created_at, updated_at) VALUES ('falsifier_breach', 'alert:1', 'NU', 'b', "
            "'digest', 'decision:1', '2026-07-01T08:00:00', '2026-07-01T08:00:00')"
        )
        conn.commit()
    finally:
        conn.close()
    html = render_open_loops_band(db_path)
    assert "Coach digest" in html


def test_proposed_tenets_flag_gated(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from synthesis.tenets import record_tenet

    record_tenet(body_md="Let winners run.", status="proposed", db_path=db_path)
    monkeypatch.delenv("LEDGER_WORLDVIEW", raising=False)
    assert "Tenets proposed" not in render_open_loops_band(db_path)
    monkeypatch.setenv("LEDGER_WORLDVIEW", "1")
    assert "Tenets proposed" in render_open_loops_band(db_path)


def test_never_raises_without_schema(tmp_path: Path) -> None:
    bare = tmp_path / "bare.db"
    sqlite3.connect(str(bare)).close()  # a DB with no tables at all
    html = render_open_loops_band(bare)
    assert "Ritual clear" in html


def test_overview_panel_prepends_band() -> None:
    html = render_overview_panel(
        {}, None, open_loops_html='<div class="cc-open-loops">BAND</div>'
    )
    assert "BAND" in html
    assert html.index("BAND") < html.index("cc-cockpit-live")
    # Without the band the panel is byte-identical to the pre-band contract.
    assert "cc-open-loops" not in render_overview_panel({}, None)
