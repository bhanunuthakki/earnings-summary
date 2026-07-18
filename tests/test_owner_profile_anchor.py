"""The owner-profile anchor injection (tenet-2 Phase 2) — load_owner_profile_anchor
+ the 6th compose_anchor_block slot.

Copies the Worldview anchor's test shape exactly: only AFFIRMED facts ride in,
dated for cache stability, capped, degrade-safe, and spotlight-wrapped inside
compose_anchor_block. Unlike Worldview there is no env-flag gate — "inert by
default" comes from the data (Phase 1 only ever stages `proposed` facts).
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from llm.anchors import (  # noqa: E402
    OWNER_PROFILE_ANCHOR_CHAR_CAP,
    compose_anchor_block,
    load_owner_profile_anchor,
)
from owner_profile.store import affirm_fact, append_fact  # noqa: E402

_DDL = """
CREATE TABLE owner_profile_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'bhanu',
    category TEXT NOT NULL CHECK (category IN ('capacity','appetite','behavioral')),
    key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    narrative TEXT NOT NULL,
    provenance TEXT NOT NULL CHECK (
        provenance IN ('wealthplan_import','cio_context_import','owner','derived')
    ),
    status TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed','affirmed','rejected')),
    affirmed_at TEXT,
    review_horizon_days INTEGER,
    source_detail TEXT,
    created_at TEXT NOT NULL,
    is_latest INTEGER NOT NULL DEFAULT 1,
    superseded_at TEXT,
    superseded_by_id INTEGER
);
CREATE UNIQUE INDEX ux_owner_profile_facts_latest
    ON owner_profile_facts(user_id, category, key) WHERE is_latest = 1;
"""


@pytest.fixture()
def repo_root(tmp_path: Path) -> Iterator[Path]:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    conn = sqlite3.connect(str(data_dir / "portfolio.db"))
    conn.executescript(_DDL)
    conn.commit()
    conn.close()
    yield tmp_path


def _db(repo_root: Path) -> Path:
    return repo_root / "data" / "portfolio.db"


def _stage_and_affirm(
    repo_root: Path, *, category: str = "capacity", key: str = "cash_buffer_months"
) -> None:
    conn = sqlite3.connect(str(_db(repo_root)))
    try:
        fact_id = append_fact(
            conn,
            category=category,
            key=key,
            value={"months": 6.0},
            narrative="Target cash buffer: 6 months of spend.",
            provenance="wealthplan_import",
            status="proposed",
        )
        affirm_fact(conn, fact_id)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# load_owner_profile_anchor
# ---------------------------------------------------------------------------


def test_anchor_empty_when_no_facts_at_all(repo_root: Path) -> None:
    assert load_owner_profile_anchor(repo_root) == ""


def test_anchor_empty_when_only_proposed(repo_root: Path) -> None:
    """The common case today: 25 capacity facts staged as `proposed`, zero
    affirmed — zero prompt bloat until the owner ratifies via the packet walk."""
    conn = sqlite3.connect(str(_db(repo_root)))
    try:
        append_fact(
            conn,
            category="capacity",
            key="cash_buffer_months",
            value={"months": 6.0},
            narrative="Target cash buffer: 6 months of spend.",
            provenance="wealthplan_import",
            status="proposed",
        )
        conn.commit()
    finally:
        conn.close()
    assert load_owner_profile_anchor(repo_root) == ""


def test_anchor_renders_affirmed_facts(repo_root: Path) -> None:
    _stage_and_affirm(repo_root)
    anchor = load_owner_profile_anchor(repo_root)
    assert "OWNER PROFILE ANCHOR" in anchor
    assert "Soft priors" in anchor and "NOT rules" in anchor
    assert "6 months of spend" in anchor
    assert "(as of 20" in anchor  # dated for cache stability


def test_anchor_only_affirmed_not_proposed_or_rejected(repo_root: Path) -> None:
    conn = sqlite3.connect(str(_db(repo_root)))
    try:
        affirmed_id = append_fact(
            conn,
            category="capacity",
            key="cash_buffer_months",
            value={"months": 6.0},
            narrative="an affirmed fact",
            provenance="wealthplan_import",
            status="proposed",
        )
        affirm_fact(conn, affirmed_id)
        append_fact(
            conn,
            category="capacity",
            key="home_city",
            value={"city": "Austin"},
            narrative="a still-proposed fact",
            provenance="wealthplan_import",
            status="proposed",
        )
        rejected_id = append_fact(
            conn,
            category="appetite",
            key="next_dollar.blend_weights",
            value={"ret": 0.5, "div": 0.3, "macro": 0.2},
            narrative="a rejected fact",
            provenance="derived",
            status="proposed",
        )
        from owner_profile.store import reject_fact

        reject_fact(conn, rejected_id)
        conn.commit()
    finally:
        conn.close()
    anchor = load_owner_profile_anchor(repo_root)
    assert "an affirmed fact" in anchor
    assert "a still-proposed fact" not in anchor
    assert "a rejected fact" not in anchor


def test_anchor_missing_db_degrades(tmp_path: Path) -> None:
    assert load_owner_profile_anchor(tmp_path) == ""  # no data/portfolio.db, never raises


def test_anchor_is_deterministic(repo_root: Path) -> None:
    _stage_and_affirm(repo_root, key="cash_buffer_months")
    _stage_and_affirm(repo_root, category="capacity", key="home_city")
    assert load_owner_profile_anchor(repo_root) == load_owner_profile_anchor(repo_root)


def test_anchor_respects_char_cap(repo_root: Path) -> None:
    conn = sqlite3.connect(str(_db(repo_root)))
    try:
        for i in range(60):
            fact_id = append_fact(
                conn,
                category="capacity",
                key=f"life_event.baby_{i}",
                value={
                    "kind": "baby",
                    "label": f"belief number {i} " * 8,
                    "date": "2031-01-01",
                },
                narrative=f"belief number {i} " * 8,
                provenance="wealthplan_import",
                status="proposed",
            )
            affirm_fact(conn, fact_id)
        conn.commit()
    finally:
        conn.close()
    anchor = load_owner_profile_anchor(repo_root)
    assert len(anchor) <= OWNER_PROFILE_ANCHOR_CHAR_CAP + len("\n[...truncated]")


# ---------------------------------------------------------------------------
# compose_anchor_block — the 6th slot
# ---------------------------------------------------------------------------


def test_compose_includes_owner_profile_slot() -> None:
    out = compose_anchor_block("THESIS_A", "BEAR_A", "", "", "", "OWNER_PROFILE_A")
    assert "THESIS_A" in out and "OWNER_PROFILE_A" in out


def test_compose_omits_empty_owner_profile() -> None:
    out = compose_anchor_block("THESIS_A", "BEAR_A")
    assert "THESIS_A" in out
    assert "OWNER_PROFILE" not in out  # empty slot contributes nothing


def test_compose_back_compat_five_args() -> None:
    out = compose_anchor_block("T", "B", "IR", "PRIORS", "WORLDVIEW")
    assert all(s in out for s in ("T", "B", "IR", "PRIORS", "WORLDVIEW"))


def test_compose_spotlights_owner_profile_block() -> None:
    out = compose_anchor_block("", "", "", "", "", "OWNER_PROFILE_FACTS")
    assert "OWNER_PROFILE_FACTS" in out
    assert out.strip() != "OWNER_PROFILE_FACTS"  # wrapped, not bare
