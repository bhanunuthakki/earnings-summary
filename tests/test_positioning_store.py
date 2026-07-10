"""Positioning intent store (alembic 0145 + src/positioning/).

- PositioningProfile validators: bounds, closed sleeve vocabulary, sector-sum,
  duplicate sectors, is_empty.
- Store: append → supersede chain (is_latest flips, superseded_at stamped,
  superseded_by_id back-links), latest/list reads, undecodable-row loud skip,
  missing-table degrade, empty-profile refusal.
- Target resolver: book-default equivalence (the load-bearing guarantee) and
  per-dimension fallback when an intent is active.
- Migration 0145 round-trip incl. the one-latest-per-user partial index.
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from allocation.candidate_fit import BookContext  # noqa: E402
from positioning.profile import PositioningProfile, SectorTarget  # noqa: E402
from positioning.store import append_intent, latest_intent, list_intents  # noqa: E402
from positioning.target import book_default_target, resolve_target_context  # noqa: E402

_DDL = """
CREATE TABLE positioning_intents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'bhanu',
    narrative TEXT NOT NULL,
    profile_json TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('coach','manual')),
    coach_session_id TEXT,
    created_at TEXT NOT NULL,
    is_latest INTEGER NOT NULL DEFAULT 1,
    superseded_at TEXT,
    superseded_by_id INTEGER
);
CREATE UNIQUE INDEX ux_positioning_intents_latest
    ON positioning_intents(user_id) WHERE is_latest = 1;
"""


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(str(tmp_path / "p.db"))
    c.row_factory = sqlite3.Row
    c.executescript(_DDL)
    c.commit()
    yield c
    c.close()


def _profile(**overrides: object) -> PositioningProfile:
    base: dict[str, object] = {"target_growth_tilt": -0.2}
    base.update(overrides)
    return PositioningProfile.model_validate(base)


# ---------------------------------------------------------------------------
# Profile validators
# ---------------------------------------------------------------------------


def test_profile_bounds() -> None:
    with pytest.raises(ValidationError):
        PositioningProfile(target_growth_tilt=3.0)  # tilt outside ±2
    with pytest.raises(ValidationError):
        PositioningProfile(target_vol_ann=1.5)  # vol is a fraction
    with pytest.raises(ValidationError):
        PositioningProfile(sleeves={"crypto": 0.1})  # closed vocabulary
    with pytest.raises(ValidationError):
        PositioningProfile(sleeves={"intl": 1.2})  # sleeve fraction bound
    with pytest.raises(ValidationError):
        PositioningProfile(
            sector_targets=[
                SectorTarget(sector="Technology", target_weight=0.7),
                SectorTarget(sector="Healthcare", target_weight=0.6),
            ]
        )  # sums over 1
    with pytest.raises(ValidationError):
        PositioningProfile(
            sector_targets=[
                SectorTarget(sector="Technology", target_weight=0.2),
                SectorTarget(sector="technology", target_weight=0.1),
            ]
        )  # duplicate (case-insensitive)


def test_profile_is_empty() -> None:
    assert PositioningProfile().is_empty()
    assert PositioningProfile(life_circumstances=["kid on the way"]).is_empty()
    assert not _profile().is_empty()
    assert not PositioningProfile(sleeves={"intl": 0.2}).is_empty()


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def test_append_supersede_chain(conn: sqlite3.Connection) -> None:
    id1 = append_intent(
        conn, narrative="tilt away from growth", profile=_profile(), source="manual"
    )
    id2 = append_intent(
        conn,
        narrative="add intl small-cap value",
        profile=_profile(sleeves={"intl": 0.15, "small_cap": 0.1}),
        source="coach",
        coach_session_id="sess-42",
    )
    latest = latest_intent(conn)
    assert latest is not None and latest.id == id2
    assert latest.source == "coach"
    assert latest.coach_session_id == "sess-42"
    assert latest.profile.sleeves == {"intl": 0.15, "small_cap": 0.1}

    history = list_intents(conn)
    assert [r.id for r in history] == [id2, id1]
    old = history[1]
    assert old.is_latest is False
    assert old.superseded_at is not None
    assert old.superseded_by_id == id2

    # Exactly one is_latest row — the partial unique index invariant.
    n = conn.execute("SELECT COUNT(*) FROM positioning_intents WHERE is_latest = 1").fetchone()[0]
    assert n == 1


def test_append_refuses_empty_profile(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="empty positioning profile"):
        append_intent(conn, narrative="x", profile=PositioningProfile(), source="manual")
    with pytest.raises(ValueError, match="source"):
        append_intent(conn, narrative="x", profile=_profile(), source="llm")


def test_undecodable_row_is_loud_skip(conn: sqlite3.Connection) -> None:
    append_intent(conn, narrative="ok", profile=_profile(), source="manual")
    conn.execute('UPDATE positioning_intents SET profile_json = \'{"sleeves": {"x": 9}}\'')
    assert latest_intent(conn) is None  # undecodable latest → treated as no intent
    assert list_intents(conn) == []


def test_missing_table_degrades(tmp_path: Path) -> None:
    bare = sqlite3.connect(str(tmp_path / "bare.db"))
    try:
        assert latest_intent(bare) is None
        assert list_intents(bare) == []
    finally:
        bare.close()


# ---------------------------------------------------------------------------
# Target resolver
# ---------------------------------------------------------------------------


_BOOK = BookContext(
    weights={"NU": 0.5, "MELI": 0.5},
    sharpe=0.9,
    risk_free_annual=0.045,
    growth_tilt=0.25,
    sector_weights={"Technology": 0.45, "Financial Services": 0.30},
)


def test_book_default_equivalence(tmp_path: Path) -> None:
    """No intent saved → every target equals the current book reading (the
    zero-gap guarantee Fit v2's behavior-preservation depends on)."""
    target = resolve_target_context(tmp_path / "missing.db", _BOOK)
    assert target.source == "book_default"
    assert target.growth_tilt == _BOOK.growth_tilt
    assert target.sector_weights == _BOOK.sector_weights
    assert target.sector_bands == {}
    assert target.sleeves == {}
    assert target.intent_id is None
    assert target == book_default_target(_BOOK)


def test_resolver_overlays_expressed_dimensions_only(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    append_intent(
        conn,
        narrative="less growth crowding, build intl value",
        profile=PositioningProfile(
            target_growth_tilt=-0.1,
            sector_targets=[SectorTarget(sector="Technology", target_weight=0.30, band=0.04)],
            sleeves={"intl": 0.20},
        ),
        source="coach",
    )
    conn.commit()
    db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])

    target = resolve_target_context(db_path, _BOOK)
    assert target.source == "intent"
    assert target.growth_tilt == pytest.approx(-0.1)  # expressed → intent wins
    assert target.sector_weights["Technology"] == pytest.approx(0.30)  # expressed
    assert target.sector_weights["Financial Services"] == pytest.approx(0.30)  # book default
    assert target.sector_bands == {"Technology": 0.04}
    assert target.sleeves == {"intl": 0.20}
    assert target.vol_posture is None  # unexpressed stays None
    assert target.narrative == "less growth crowding, build intl value"


# ---------------------------------------------------------------------------
# Migration 0145 round-trip
# ---------------------------------------------------------------------------


def test_migration_0145_roundtrip(tmp_path: Path) -> None:
    from alembic.config import Config

    from alembic import command

    db = tmp_path / "m.db"
    sqlite3.connect(str(db)).close()
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.stamp(cfg, "0144_etf_published_data")
    command.upgrade(cfg, "0145_positioning_intents")

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        # The store works against the migrated schema end-to-end.
        id1 = append_intent(conn, narrative="v1", profile=_profile(), source="manual")
        id2 = append_intent(conn, narrative="v2", profile=_profile(), source="manual")
        assert id2 > id1
        latest = latest_intent(conn)
        assert latest is not None and latest.id == id2
        # CHECK constraint enforced by the migrated table.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO positioning_intents "
                "(user_id, narrative, profile_json, source, created_at) "
                "VALUES ('bhanu', 'x', '{}', 'robot', '2026-07-10T00:00:00')"
            )
        # Partial unique index: a second is_latest=1 row for the same user is
        # rejected at the SQL layer, not just by convention.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO positioning_intents "
                "(user_id, narrative, profile_json, source, created_at, is_latest) "
                "VALUES ('bhanu', 'x', '{}', 'manual', '2026-07-10T00:00:00', 1)"
            )
    finally:
        conn.close()

    command.downgrade(cfg, "0144_etf_published_data")
    conn = sqlite3.connect(str(db))
    try:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "positioning_intents" not in tables
    finally:
        conn.close()
