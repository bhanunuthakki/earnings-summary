"""execution/resolve_manager_ciks.py — the verifying CIK resolver + the 0099
CIK-backfill migration.

Pins the safety property that the live probe proved necessary: a CIK is
auto-applied ONLY for a single recent, strong-name 13F-HR filer; an old entity
that stopped filing (stale) or several near-duplicate sleeves (ambiguous) are
reported, never guessed. Network is injected — no live SEC calls.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from discovery.sources import SourceRow, get_source

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import resolve_manager_ciks as rmc  # noqa: E402

_AS_OF = date(2026, 6, 13)


def _source(key: str, name: str) -> SourceRow:
    return SourceRow(
        source_key=key,
        signal_class="investor_13f",
        display_name=name,
        base_weight=0.8,
        tier="crossover",
        style_tags=(),
        cik=None,
        active=True,
        last_calibrated_at=None,
        created_at="seed",
        updated_at="seed",
    )


def _fetch(*, search_ciks: list[str], subs: dict[str, tuple[str, str | None]]) -> rmc.Fetch:
    """A fake SEC client: the company search returns ``search_ciks``; each
    CIK's submissions returns (conformed_name, latest_13f_date)."""

    def fetch(url: str) -> str | None:
        if "browse-edgar" in url:
            # EDGAR's real atom feed tags the CIK in lowercase inside
            # <company-info> — mirror that here so the fixture pins reality, not
            # the old uppercase-<CIK> bug that matched nothing.
            return "".join(f"<company-info><cik>{c}</cik></company-info>" for c in search_ciks)
        if "submissions" in url:
            for cik, (name, latest) in subs.items():
                if cik in url:
                    recent: dict[str, list[str]] = {"form": [], "filingDate": []}
                    if latest is not None:
                        recent = {"form": ["13F-HR"], "filingDate": [latest]}
                    return json.dumps({"name": name, "filings": {"recent": recent}})
            return None
        return None

    return fetch


# ----------------------------------------------------------------------------
# name scoring
# ----------------------------------------------------------------------------


def test_name_score_ignores_generic_tokens() -> None:
    assert rmc.name_score("Coatue", "COATUE MANAGEMENT LLC") == pytest.approx(1.0)
    assert rmc.name_score("Tiger Global", "TIGER GLOBAL MANAGEMENT LLC") == pytest.approx(1.0)
    # A different sleeve dilutes the score (distinctive token mismatch).
    assert rmc.name_score("Sands Capital", "Sands Capital Alternatives, LLC") == pytest.approx(0.5)
    assert rmc.name_score("Coatue", "Acme Holdings") == 0.0


# ----------------------------------------------------------------------------
# the four resolution paths
# ----------------------------------------------------------------------------


def test_resolve_single_recent_strong_match() -> None:
    fetch = _fetch(
        search_ciks=["1135730"],
        subs={"0001135730": ("COATUE MANAGEMENT LLC", "2026-05-15")},
    )
    res = rmc.resolve_one(_source("coatue", "Coatue"), fetch=fetch, as_of=_AS_OF)
    assert res.status == "resolved"
    assert res.cik == "0001135730"


def test_resolve_stale_entity_is_rejected() -> None:
    # Strong name, but the entity's last 13F-HR is ancient (the Viking trap).
    fetch = _fetch(
        search_ciks=["1101785"],
        subs={"0001101785": ("Akre Capital Management LLC", "2014-02-14")},
    )
    res = rmc.resolve_one(_source("akre", "Akre Capital"), fetch=fetch, as_of=_AS_OF)
    assert res.status == "stale"
    assert res.cik is None


def test_resolve_ambiguous_multiple_recent_matches() -> None:
    # Two recent, equally-strong sleeves → reported, not guessed.
    fetch = _fetch(
        search_ciks=["111", "222"],
        subs={
            "0000000111": ("Foo Growth Management LLC", "2026-05-15"),
            "0000000222": ("Foo Growth Advisers LP", "2026-05-12"),
        },
    )
    res = rmc.resolve_one(_source("foo", "Foo Growth"), fetch=fetch, as_of=_AS_OF)
    assert res.status == "ambiguous"
    assert res.cik is None
    assert "111" in res.detail and "222" in res.detail


def test_resolve_not_found() -> None:
    fetch = _fetch(search_ciks=[], subs={})
    res = rmc.resolve_one(_source("mystery", "Mystery Partners"), fetch=fetch, as_of=_AS_OF)
    assert res.status == "not_found"


# ----------------------------------------------------------------------------
# roster apply
# ----------------------------------------------------------------------------

_DDL = """
CREATE TABLE discovery_sources (source_key TEXT PRIMARY KEY, signal_class TEXT NOT NULL,
  display_name TEXT NOT NULL, base_weight FLOAT NOT NULL DEFAULT 1.0, tier TEXT NOT NULL
  DEFAULT 'crossover', style_tags TEXT, cik TEXT, active INTEGER NOT NULL DEFAULT 1,
  last_calibrated_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
"""


@pytest.fixture
def sources_db(tmp_path: Path) -> Path:
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.executescript(_DDL)
    conn.executemany(
        "INSERT INTO discovery_sources (source_key, signal_class, display_name, cik,"
        " created_at, updated_at) VALUES (?, 'investor_13f', ?, ?, 'seed', 'seed')",
        [("coatue", "Coatue", None), ("appaloosa", "Appaloosa", "0001656456")],
    )
    conn.commit()
    conn.close()
    return db


def test_resolve_roster_apply_writes_only_confident(sources_db: Path) -> None:
    fetch = _fetch(
        search_ciks=["1135730"],
        subs={"0001135730": ("COATUE MANAGEMENT LLC", "2026-05-15")},
    )
    # Only the unresolved coatue is touched (appaloosa already has a CIK).
    results = rmc.resolve_roster(apply=True, fetch=fetch, db_path=sources_db, as_of=_AS_OF)
    assert [r.source_key for r in results] == ["coatue"]
    assert get_source("coatue", db_path=sources_db).cik == "0001135730"  # type: ignore[union-attr]

    # Dry-run never writes.
    conn = sqlite3.connect(sources_db)
    conn.execute("UPDATE discovery_sources SET cik = NULL WHERE source_key = 'coatue'")
    conn.commit()
    conn.close()
    rmc.resolve_roster(apply=False, fetch=fetch, db_path=sources_db, as_of=_AS_OF)
    assert get_source("coatue", db_path=sources_db).cik is None  # type: ignore[union-attr]


# ----------------------------------------------------------------------------
# migration 0099 (CIK backfill)
# ----------------------------------------------------------------------------


def test_migration_backfills_ciks(tmp_path: Path) -> None:
    db = tmp_path / "mig.db"
    conn = sqlite3.connect(db)
    conn.executescript(_DDL)
    # The 7 verified funds (cik NULL) + one already-set (must not be clobbered).
    conn.executemany(
        "INSERT INTO discovery_sources (source_key, signal_class, display_name, cik,"
        " created_at, updated_at) VALUES (?, 'investor_13f', ?, ?, 'seed', 'seed')",
        [
            ("altimeter", "Altimeter", None),
            ("coatue", "Coatue", None),
            ("whale_rock", "Whale Rock", "9999999999"),  # owner-set; preserve
        ],
    )
    conn.commit()
    conn.close()
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.stamp(cfg, "0098_discovery_sources")
    command.upgrade(cfg, "0099_discovery_sources_ciks")
    conn = sqlite3.connect(db)
    ciks = dict(conn.execute("SELECT source_key, cik FROM discovery_sources").fetchall())
    conn.close()
    assert ciks["altimeter"] == "0001541617"  # backfilled
    assert ciks["coatue"] == "0001135730"
    assert ciks["whale_rock"] == "9999999999"  # NOT clobbered (cik IS NULL gate)
