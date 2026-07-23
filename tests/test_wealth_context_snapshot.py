"""P0-F wealth context snapshots (PRD §7.6): aggregates-only balance-sheet
timeseries.

Seams under test:

  * migration 0187 — table + unique input_sha at head;
  * ``build_wealth_context_snapshot`` — source composition (tracker live
    total supersedes wealthplan investment buckets; either source alone
    degrades with a warning; both down returns None);
  * ``WealthContextSnapshot.validate_plausible`` — §7.6.3;
  * the store — idempotent append (identical observation dedupes), latest/
    history reads, pre-0187 degrade.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from integrations.wealth_context import (
    WealthContextSnapshot,
    build_wealth_context_snapshot,
    load_wealthplan_starting,
)
from wealth_context_store import append_snapshot, read_history, read_latest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

_WP = (
    {"cash": 40_000.0, "taxable": 90_000.0, "roth": 30_000.0, "illiquid": 15_000.0},
    250_000.0,
    "2026-06-30",
)
_TRACKER_BUCKETS = {"taxable": 95_000.0, "tax_free": 33_000.0, "unknown": 2_000.0}


def _build_config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture(scope="module")
def head_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    db = tmp_path_factory.mktemp("wealth_ctx_tmpl") / "at_head.db"
    import db as dbmod

    saved = (dbmod.DB_PATH, dbmod.DATA_DIR, dbmod.FMP_DIR)
    dbmod.set_db_path(str(db))
    dbmod.init_db()
    cfg = _build_config(db)
    command.stamp(cfg, "0000_baseline")
    command.upgrade(cfg, "head")
    dbmod.DB_PATH, dbmod.DATA_DIR, dbmod.FMP_DIR = saved
    return db


@pytest.fixture
def head_db(head_template: Path, tmp_path: Path) -> Path:
    db = tmp_path / "wealth_ctx.db"
    shutil.copy(head_template, db)
    return db


def _snap(tmp_path: Path, **overrides: object) -> WealthContextSnapshot:
    """Both sources present; cash_need_root at an empty dir so the band leg
    degrades deterministically (no dependency on a sibling wealthplan)."""
    kwargs: dict[str, object] = {
        "tracker_total": 130_000.0,
        "tracker_by_tax_treatment": _TRACKER_BUCKETS,
        "tracker_as_of": "2026-07-23",
        "wealthplan": _WP,
        "cash_need_root": tmp_path / "no_wealthplan",
    }
    kwargs.update(overrides)
    snap = build_wealth_context_snapshot(**kwargs)  # type: ignore[arg-type]
    assert snap is not None
    return snap


# --------------------------------------------------------------------------- #
# Migration shape
# --------------------------------------------------------------------------- #
def test_migration_creates_table_with_unique_sha(head_db: Path) -> None:
    conn = sqlite3.connect(str(head_db))
    try:
        cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(wealth_context_snapshot_history)").fetchall()
        }
        assert {"as_of", "currency", "net_worth_total", "snapshot_json", "input_sha"} <= cols
        # input_sha uniqueness is enforced (constraint name is SQLite-internal).
        row = (
            "bhanu",
            "t",
            "2026-07-23",
            None,
            None,
            "USD",
            1.0,
            1.0,
            1.0,
            None,
            "{}",
            "sha_x",
            "t",
        )
        ins = (
            "INSERT INTO wealth_context_snapshot_history "
            "(user_id, captured_at, as_of, tracker_as_of, wealthplan_as_of, currency, "
            " net_worth_total, liquid_total, investable_total, home_equity, "
            " snapshot_json, input_sha, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
        )
        conn.execute(ins, row)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(ins, row)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Composition
# --------------------------------------------------------------------------- #
def test_tracker_supersedes_stale_investment_buckets(tmp_path: Path) -> None:
    snap = _snap(tmp_path)
    # Live tracker total (130k) + wealthplan cash (40k) = liquid.
    assert snap.liquid_total == pytest.approx(170_000.0)
    # Net worth adds wealthplan illiquid (15k) and home equity (250k).
    assert snap.net_worth_total == pytest.approx(435_000.0)
    # Allocation carries tracker vocabulary for investments + wealthplan cash/illiquid.
    assert snap.allocation_by_bucket["taxable"] == pytest.approx(95_000.0)
    assert snap.allocation_by_bucket["cash"] == pytest.approx(40_000.0)
    assert snap.allocation_by_bucket["illiquid"] == pytest.approx(15_000.0)
    assert snap.sources["investable_total"] == "tracker"
    assert snap.sources["net_worth_total"] == "tracker+wealthplan"
    # The stale wealthplan taxable (90k) must NOT leak into allocation.
    assert "roth" not in snap.allocation_by_bucket
    assert snap.as_of == "2026-07-23"  # max of source as-ofs


def test_tracker_down_falls_back_to_wealthplan_buckets(tmp_path: Path) -> None:
    snap = _snap(tmp_path, tracker_total=None, tracker_by_tax_treatment=None, tracker_as_of=None)
    assert snap.sources["investable_total"] == "wealthplan"
    assert snap.liquid_total == pytest.approx(40_000.0 + 90_000.0 + 30_000.0)
    assert any("tracker unavailable" in w for w in snap.warnings)
    assert snap.as_of == "2026-06-30"  # wealthplan's typed balance as-of


def test_wealthplan_down_yields_investment_only_snapshot(tmp_path: Path) -> None:
    snap = _snap(tmp_path, wealthplan=None)
    assert snap.net_worth_total == pytest.approx(130_000.0)
    assert snap.home_equity is None
    assert any("wealthplan" in w for w in snap.warnings)


def test_both_sources_down_returns_none(tmp_path: Path) -> None:
    assert (
        build_wealth_context_snapshot(
            tracker_total=None,
            tracker_by_tax_treatment=None,
            tracker_as_of=None,
            wealthplan=None,
            cash_need_root=tmp_path / "no_wealthplan",
        )
        is None
    )


def test_load_wealthplan_starting_degrades_on_missing_checkout(tmp_path: Path) -> None:
    assert load_wealthplan_starting(tmp_path / "nothing_here") is None


# --------------------------------------------------------------------------- #
# Validation + idempotency hash
# --------------------------------------------------------------------------- #
def test_validate_plausible_accepts_good_and_rejects_bad(tmp_path: Path) -> None:
    snap = _snap(tmp_path)
    assert snap.validate_plausible() == []
    bad = snap.model_copy(update={"net_worth_total": -5.0})
    assert any("negative" in r for r in bad.validate_plausible())
    empty = snap.model_copy(
        update={"net_worth_total": None, "liquid_total": None, "investable_total": None}
    )
    assert any("core totals" in r for r in empty.validate_plausible())


def test_input_sha_ignores_warnings(tmp_path: Path) -> None:
    a = _snap(tmp_path)
    b = a.model_copy(update={"warnings": ("something transient",)})
    assert a.input_sha() == b.input_sha()
    c = a.model_copy(update={"net_worth_total": 999.0})
    assert a.input_sha() != c.input_sha()


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #
def test_append_dedupes_identical_observation(head_db: Path, tmp_path: Path) -> None:
    snap = _snap(tmp_path)
    assert append_snapshot(snap, db_path=head_db) == (True, False)
    assert append_snapshot(snap, db_path=head_db) == (False, True)  # idempotent re-run
    assert len(read_history(db_path=head_db)) == 1
    moved = snap.model_copy(update={"as_of": "2026-07-24", "net_worth_total": 436_000.0})
    assert append_snapshot(moved, db_path=head_db) == (True, False)
    latest = read_latest(db_path=head_db)
    assert latest is not None and latest.as_of == "2026-07-24"
    assert latest.snapshot is not None
    assert latest.snapshot.allocation_by_bucket["cash"] == pytest.approx(40_000.0)


def test_read_history_since_floor_and_pre_0187_degrade(head_db: Path, tmp_path: Path) -> None:
    append_snapshot(_snap(tmp_path), db_path=head_db)
    assert read_history(since="2099-01-01", db_path=head_db) == []
    assert read_history(since="2000-01-01", db_path=head_db) != []
    conn = sqlite3.connect(str(head_db))
    conn.execute("DROP TABLE wealth_context_snapshot_history")
    conn.commit()
    conn.close()
    assert read_history(db_path=head_db) == []
    assert read_latest(db_path=head_db) is None
    assert append_snapshot(_snap(tmp_path), db_path=head_db) == (False, False)
