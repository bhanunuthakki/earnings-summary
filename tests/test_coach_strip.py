"""B9: the Home "Coach" strip — brief and Telegram show the SAME governed
items (today's sent pings + the digest queue), read-only, never raising."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from pipeline.coach_strip import render_coach_strip

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0130_owner_decision_extension"

# Pinned clock (mid-day, far from UTC midnight): a real-clock seed 1h old can
# cross the date boundary right after 00:00 UTC and vanish from "sent today" —
# the repo's known full-suite midnight-flake class.
_NOW = datetime(2026, 7, 10, 12, 0, 0)
_KEY_SEQ = iter(range(10_000))


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
    command.upgrade(cfg, "0131_coach_pings")
    return db


def _seed_ping(db_path: Path, *, class_: str, body: str, status: str, age_hours: int = 1) -> None:
    stamp = (_NOW - timedelta(hours=age_hours)).isoformat()
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO coach_pings (class_, key, ticker, body, status, source_ref, "
            "created_at, updated_at) VALUES (?, ?, NULL, ?, ?, NULL, ?, ?)",
            (class_, f"k:{next(_KEY_SEQ)}", body, status, stamp, stamp),
        )
        conn.commit()
    finally:
        conn.close()


def test_strip_renders_sent_today_and_digest_queue(db_path: Path) -> None:
    _seed_ping(db_path, class_="calibration_finding", body="33% hit-rate on high", status="sent")
    _seed_ping(db_path, class_="tenet_challenge", body="violated 3x since May", status="digest")
    html = render_coach_strip(db_path, now=_NOW)
    assert "Coach" in html
    assert "33% hit-rate on high" in html
    assert "violated 3x since May" in html
    assert "(queued)" in html  # the digest row is labeled, the sent row is not
    assert html.count("(queued)") == 1
    assert "calibration" in html and "tenet" in html  # short class labels


def test_strip_excludes_dismissed_and_old_sent(db_path: Path) -> None:
    _seed_ping(db_path, class_="post_mortem", body="old ping", status="sent", age_hours=50)
    _seed_ping(db_path, class_="intent_followup", body="dismissed one", status="dismissed")
    assert render_coach_strip(db_path, now=_NOW) == ""


def test_strip_empty_db_renders_nothing(db_path: Path) -> None:
    assert render_coach_strip(db_path, now=_NOW) == ""


def test_strip_never_raises_on_missing_table(tmp_path: Path) -> None:
    bare = tmp_path / "bare.db"
    sqlite3.connect(str(bare)).close()  # a DB with no coach_pings at all
    assert render_coach_strip(bare, now=_NOW) == ""


def test_strip_caps_rows(db_path: Path) -> None:
    for i in range(6):
        _seed_ping(db_path, class_="profile_drift", body=f"digest item {i}", status="digest")
    html = render_coach_strip(db_path, now=_NOW)
    assert html.count("cc-ol-line") == 4  # _MAX_ROWS


def test_strip_renders_routed_to_brief_items(db_path: Path) -> None:
    """P2.2: calibration_finding/capacity_breach/life_event_checkpoint/
    profile_drift moments land status='routed_to_brief' (governor.
    BRIEF_ROUTED_CLASSES) instead of sent/digest — the strip must still show
    them (labeled distinctly), or they'd silently vanish from Home."""
    _seed_ping(db_path, class_="profile_drift", body="drifted fact", status="routed_to_brief")
    html = render_coach_strip(db_path, now=_NOW)
    assert "drifted fact" in html
    assert "(in weekly brief)" in html
    assert "(queued)" not in html
