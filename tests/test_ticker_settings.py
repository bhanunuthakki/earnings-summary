"""Persistent per-ticker budget-bypass flag (PR5): the ticker_settings store +
the GET/POST /api/ticker-settings endpoint + the dashboard toggle.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import comments_server  # noqa: E402

from pipeline.ticker_command_center import _refresh_section  # noqa: E402
from ticker_settings import (  # noqa: E402
    get_auto_pre_earnings_brief,
    get_bypass_budget,
    get_flag,
    set_auto_pre_earnings_brief,
    set_bypass_budget,
)

# Mirrors migration 0067's op.create_table shape + 0260's added flag column.
_CREATE = """
CREATE TABLE ticker_settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL,
    bypass_budget INTEGER NOT NULL DEFAULT 0,
    auto_pre_earnings_brief INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    CONSTRAINT uq_ticker_settings_ticker UNIQUE (ticker));
"""


def _db(tmp_path: Path, *, with_table: bool = True) -> Path:
    d = tmp_path / "data"
    d.mkdir(exist_ok=True)
    p = d / "portfolio.db"
    conn = sqlite3.connect(str(p))
    try:
        if with_table:
            conn.executescript(_CREATE)
        conn.commit()
    finally:
        conn.close()
    return p


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def test_set_get_round_trip(tmp_path: Path) -> None:
    p = _db(tmp_path)
    assert get_bypass_budget("NU", db_path=p) is False  # default when no row
    assert set_bypass_budget("NU", True, db_path=p) is True
    assert get_bypass_budget("NU", db_path=p) is True
    # Upsert (case-insensitive) flips it back.
    assert set_bypass_budget("nu", False, db_path=p) is True
    assert get_bypass_budget("NU", db_path=p) is False


def test_auto_pre_earnings_brief_round_trip(tmp_path: Path) -> None:
    """The 0260 opt-in flag rides the same store: independent of bypass_budget
    (setting one never disturbs the other on the shared row)."""
    p = _db(tmp_path)
    assert get_auto_pre_earnings_brief("WIX", db_path=p) is False
    assert set_auto_pre_earnings_brief("WIX", True, db_path=p) is True
    assert get_auto_pre_earnings_brief("WIX", db_path=p) is True
    assert get_bypass_budget("WIX", db_path=p) is False
    assert set_bypass_budget("WIX", True, db_path=p) is True
    assert get_auto_pre_earnings_brief("WIX", db_path=p) is True  # untouched


def test_unknown_flag_rejected(tmp_path: Path) -> None:
    """The generic helpers interpolate the column name — the closed vocabulary
    is the injection guard, so an unknown flag must raise, never query."""
    p = _db(tmp_path)
    with pytest.raises(ValueError, match="unknown ticker_settings flag"):
        get_flag("NU", "evil; DROP TABLE ticker_settings", db_path=p)


def test_get_and_set_fail_safe_without_table(tmp_path: Path) -> None:
    p = _db(tmp_path, with_table=False)
    assert get_bypass_budget("NU", db_path=p) is False  # missing table → no bypass
    assert set_bypass_budget("NU", True, db_path=p) is False  # can't persist
    # Pre-0260 shape (column missing entirely): same fail-safe contract.
    assert get_auto_pre_earnings_brief("NU", db_path=p) is False


def test_get_fail_safe_no_db(tmp_path: Path) -> None:
    assert get_bypass_budget("NU", db_path=tmp_path / "nope.db") is False


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path):
    _db(tmp_path)
    return comments_server.create_app(tmp_path).test_client()


def test_get_default_false(client) -> None:
    resp = client.get("/api/ticker-settings/NU")
    assert resp.status_code == 200
    assert resp.get_json() == {
        "ticker": "NU",
        "auto_pre_earnings_brief": False,
        "bypass_budget": False,
    }


def test_post_then_get_reflects(client) -> None:
    resp = client.post("/api/ticker-settings/nu", json={"bypass_budget": True})
    assert resp.status_code == 200
    assert resp.get_json()["bypass_budget"] is True
    assert client.get("/api/ticker-settings/NU").get_json()["bypass_budget"] is True


def test_post_auto_brief_flag(client) -> None:
    """The 0260 flag is settable through the same endpoint (the diet panel's
    auto-brief chip POSTs exactly this body) and never disturbs bypass_budget."""
    resp = client.post("/api/ticker-settings/wix", json={"auto_pre_earnings_brief": True})
    assert resp.status_code == 200
    assert resp.get_json() == {"ticker": "WIX", "auto_pre_earnings_brief": True}
    got = client.get("/api/ticker-settings/WIX").get_json()
    assert got["auto_pre_earnings_brief"] is True
    assert got["bypass_budget"] is False


def test_post_missing_field_400(client) -> None:
    assert client.post("/api/ticker-settings/NU", json={}).status_code == 400
    # Unknown keys alone are also a 400 — never silently accepted.
    assert client.post("/api/ticker-settings/NU", json={"nonsense": True}).status_code == 400


# ---------------------------------------------------------------------------
# Dashboard toggle
# ---------------------------------------------------------------------------


def test_refresh_section_renders_persistent_toggle() -> None:
    html = _refresh_section("NU")
    assert "tcc-bypass-toggle" in html  # the checkbox
    assert 'data-ticker="NU"' in html
    assert "/api/ticker-settings/" in html  # GET-on-load + POST-on-change wiring
