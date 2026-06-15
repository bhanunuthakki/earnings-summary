"""Global DCF assumptions store + resolver (migration 0112).

Pins the precedence the whole feature rests on — **per-ticker override > global
default > hardcoded fallback > seed** — plus the seeded values, the upsert
round-trip, range validation, and the best-effort degrade-to-seed when the DB /
table is missing (a build must never fail because the global store is absent).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dcf import global_assumptions as ga

# Mirrors migration 0112's schema + seed.
_CREATE = """
CREATE TABLE global_dcf_assumptions (
    field TEXT PRIMARY KEY,
    value REAL NOT NULL,
    updated_at TEXT NOT NULL);
"""
_SEED = [
    ("risk_free_rate", 0.043),
    ("equity_risk_premium", 0.045),
    ("tax_rate", 0.24),
]
_NOW = datetime(2026, 6, 15, tzinfo=UTC).replace(tzinfo=None)


def _seeded_db(tmp_path: Path) -> Path:
    path = tmp_path / "t.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_CREATE)
        conn.executemany(
            "INSERT INTO global_dcf_assumptions (field, value, updated_at) VALUES (?, ?, ?)",
            [(f, v, _NOW.isoformat()) for f, v in _SEED],
        )
        conn.commit()
    finally:
        conn.close()
    return path


def _empty_db(tmp_path: Path) -> Path:
    """DB with the table but no rows — exercises the seed-default overlay."""
    path = tmp_path / "empty.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_CREATE)
        conn.commit()
    finally:
        conn.close()
    return path


# --------------------------------------------------------------------------- reads


def test_get_all_returns_seeded_values(tmp_path: Path) -> None:
    db = _seeded_db(tmp_path)
    got = ga.get_all(db_path=db)
    assert got == {"risk_free_rate": 0.043, "equity_risk_premium": 0.045, "tax_rate": 0.24}


def test_load_returns_typed_object(tmp_path: Path) -> None:
    g = ga.load(db_path=_seeded_db(tmp_path))
    assert g.risk_free_rate == 0.043
    assert g.equity_risk_premium == 0.045
    assert g.tax_rate == 0.24


def test_get_returns_none_for_unset_field(tmp_path: Path) -> None:
    # Empty table → no stored row → get() is None even for a known field.
    assert ga.get("risk_free_rate", db_path=_empty_db(tmp_path)) is None


def test_get_all_overlays_seed_when_rows_absent(tmp_path: Path) -> None:
    # Effective map always carries every field, defaulting to the seed.
    assert ga.get_all(db_path=_empty_db(tmp_path)) == dict(ga.SEED_DEFAULTS)


# --------------------------------------------------------------------------- resolve precedence


def test_resolve_ticker_value_wins(tmp_path: Path) -> None:
    db = _seeded_db(tmp_path)
    # Per-ticker override beats the global default (0.043) AND the fallback.
    assert ga.resolve("risk_free_rate", ticker_value=0.05, fallback=0.043, db_path=db) == 0.05


def test_resolve_falls_to_global_when_no_ticker_value(tmp_path: Path) -> None:
    db = _seeded_db(tmp_path)
    # Change the global so it differs from the seed/fallback, then prove it wins
    # over the supplied fallback when there's no ticker override.
    ga.set_value("risk_free_rate", 0.052, db_path=db, now=_NOW)
    assert ga.resolve("risk_free_rate", ticker_value=None, fallback=0.043, db_path=db) == 0.052


def test_resolve_falls_to_fallback_when_no_global(tmp_path: Path) -> None:
    db = _empty_db(tmp_path)  # table present, no rows → no stored global
    assert ga.resolve("tax_rate", ticker_value=None, fallback=0.30, db_path=db) == 0.30


def test_resolve_falls_to_seed_when_no_db_no_fallback(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.db"
    assert ga.resolve("tax_rate", db_path=missing) == ga.SEED_DEFAULTS["tax_rate"]


def test_resolve_unknown_field_without_fallback_raises(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        ga.resolve("not_a_field", db_path=_empty_db(tmp_path))


# --------------------------------------------------------------------------- writes


def test_set_value_round_trip(tmp_path: Path) -> None:
    db = _seeded_db(tmp_path)
    assert ga.set_value("equity_risk_premium", 0.05, db_path=db, now=_NOW) is True
    assert ga.get("equity_risk_premium", db_path=db) == 0.05


def test_set_value_creates_row_when_absent(tmp_path: Path) -> None:
    db = _empty_db(tmp_path)
    assert ga.set_value("risk_free_rate", 0.041, db_path=db, now=_NOW) is True
    assert ga.get("risk_free_rate", db_path=db) == 0.041


def test_set_value_rejects_unknown_field(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ga.set_value("terminal_growth", 0.03, db_path=_seeded_db(tmp_path), now=_NOW)


@pytest.mark.parametrize("bad", [-0.01, 1.5, 4.3])
def test_set_value_rejects_out_of_range(tmp_path: Path, bad: float) -> None:
    with pytest.raises(ValueError):
        ga.set_value("risk_free_rate", bad, db_path=_seeded_db(tmp_path), now=_NOW)


def test_set_value_returns_false_when_db_missing(tmp_path: Path) -> None:
    # Validation still runs (raises on bad input) but a missing DB → no write.
    assert ga.set_value("tax_rate", 0.25, db_path=tmp_path / "nope.db") is False


# --------------------------------------------------------------------------- degrade-to-seed


def test_load_degrades_to_seed_without_db(tmp_path: Path) -> None:
    g = ga.load(db_path=tmp_path / "missing.db")
    assert g.as_dict() == dict(ga.SEED_DEFAULTS)
