"""Workstream B2 (program review 2026-07-19): unified anchors + the macro pack.

The review's gap G3/G4/G7: the open-ended Ask portfolio scope — exactly the
surface meant for macro/allocation/method questions — loaded none of the
owner's tenets, themes, or profile facts; distilled themes had ZERO prompt
consumers anywhere; and 6,986 macro_series rows + 57 sensitivities had no
conversational doorway. Four seams:

  * load_themes_anchor — current themes only, rides the LEDGER_WORLDVIEW_ANCHOR
    gate, degrade-to-"" contract, dated lines (cache stability);
  * _portfolio_system_context — carries worldview + themes + owner-profile
    blocks (spotlight-wrapped) when the loaders return content, byte-identical
    to before when they return "";
  * the deterministic `macro` ask pack — series levels, r²-floored betas
    (weak fits reported as unknown, never as exposure), and `macro:` stances;
  * scope_key_for — `macro:` is a recognized sibling namespace that round-trips
    verbatim instead of being re-slugged into `tenet:`.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import cast

import pytest

from ask import context as ask_context
from ask.packs import load_packs
from llm.anchors import load_themes_anchor, load_worldview_anchor
from macro_store import fetch_sensitivities, persist_rate_sensitivity
from synthesis.insights import record_insight
from synthesis.tenets import record_tenet, scope_key_for

# Keep the exact internal prompt contract typed without suppressing private-use diagnostics.
_portfolio_system_context = cast(
    Callable[[Path, dict[str, list[str]]], str],
    getattr(ask_context, "_portfolio_system_context"),
)

PRIOR_HEAD = "0059_kpi_facts_restatement"


@pytest.fixture
def repo_root(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    migrated_db(tmp_path / "data" / "portfolio.db", stamp=PRIOR_HEAD)
    return tmp_path


def _db(repo_root: Path) -> Path:
    return repo_root / "data" / "portfolio.db"


@pytest.fixture(autouse=True)
def _flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEDGER_WORLDVIEW_ANCHOR", raising=False)


def _record_theme(repo_root: Path, slug: str, body: str) -> None:
    record_insight(
        scope_key=f"theme:{slug}",
        kind="theme",
        body_md=body,
        source_note_ids=[],
        watermark_id=None,
        db_path=_db(repo_root),
    )


# ---------------------------------------------------------------- themes anchor


def test_themes_anchor_empty_when_flag_off(repo_root: Path) -> None:
    _record_theme(repo_root, "ai-compute-buildout", "AI compute demand outruns supply.")
    assert load_themes_anchor(repo_root) == ""


def test_themes_anchor_renders_current_themes_when_on(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEDGER_WORLDVIEW_ANCHOR", "1")
    _record_theme(repo_root, "ai-compute-buildout", "AI compute demand outruns supply.")
    anchor = load_themes_anchor(repo_root)
    assert "THEMES ANCHOR" in anchor
    assert "AI compute demand outruns supply" in anchor
    assert "theme:ai-compute-buildout" in anchor
    assert "(since 20" in anchor  # dated for cache stability


def test_themes_anchor_empty_without_themes_or_db(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEDGER_WORLDVIEW_ANCHOR", "1")
    assert load_themes_anchor(repo_root) == ""  # table present, zero themes
    bare = tmp_path / "empty_repo"
    bare.mkdir()
    assert load_themes_anchor(bare) == ""  # no DB at all — degrade, never raise


# ------------------------------------------------- portfolio scope owner memory


def test_portfolio_context_carries_owner_memory_when_on(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEDGER_WORLDVIEW_ANCHOR", "1")
    record_tenet(body_md="I sell my winners too early.", db_path=_db(repo_root))
    _record_theme(repo_root, "glp1-obesity", "GLP-1 reshapes consumer staples demand.")
    ctx = _portfolio_system_context(repo_root, {"portfolio": ["NU"]})
    assert "sell my winners too early" in ctx
    assert "GLP-1 reshapes consumer staples" in ctx
    # Sanity: the worldview loader is what fed it (same content, same gate).
    assert load_worldview_anchor(repo_root) != ""


def test_portfolio_context_unchanged_when_anchors_empty(repo_root: Path) -> None:
    # Flag off ⇒ every loader returns "" ⇒ the prompt renders exactly as before.
    ctx = _portfolio_system_context(repo_root, {"portfolio": ["NU"]})
    assert "WORLDVIEW ANCHOR" not in ctx
    assert "THEMES ANCHOR" not in ctx
    assert "OWNER PROFILE ANCHOR" not in ctx


# -------------------------------------------------------------------- macro pack


_MACRO_DDL = (
    "CREATE TABLE IF NOT EXISTS macro_series ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, series_id TEXT NOT NULL, "
    "rate_date TEXT NOT NULL, value REAL NOT NULL, source TEXT, created_at TEXT)",
    "CREATE TABLE IF NOT EXISTS macro_sensitivities ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, "
    "series_id TEXT NOT NULL, beta REAL NOT NULL, r_squared REAL, "
    "lookback_window_days INTEGER, computed_at TEXT)",
)


def _ensure_macro_tables(repo_root: Path) -> None:
    """The macro tables live outside the alembic chain (init_db-owned); the
    fixture DB stops at the migration graph, so create the minimal shape here —
    prod columns per PRAGMA, nothing more."""
    conn = sqlite3.connect(str(_db(repo_root)))
    try:
        for ddl in _MACRO_DDL:
            conn.execute(ddl)
        conn.commit()
    finally:
        conn.close()


def _seed_macro(repo_root: Path) -> None:
    _ensure_macro_tables(repo_root)
    conn = sqlite3.connect(str(_db(repo_root)))
    try:
        conn.execute(
            "INSERT INTO macro_series (series_id, rate_date, value, source, created_at) "
            "VALUES ('us_10y', '2026-07-18', 4.2, 'test', '2026-07-18T00:00:00')"
        )
        conn.execute(
            "INSERT INTO macro_sensitivities "
            "(ticker, series_id, beta, r_squared, lookback_window_days, computed_at) "
            "VALUES ('MELI', 'usd_cad', -2.3, 0.02, 365, '2026-07-18T00:00:00')"
        )
        conn.commit()
    finally:
        conn.close()
    rates = [
        (date.today() - timedelta(days=7 * (30 - i)), 4 + (i % 3) * 0.1 + i * 0.01)
        for i in range(31)
    ]
    prices = [(day, 100 * math.exp(-1.4 * (level - 4))) for day, level in rates]
    assert (
        persist_rate_sensitivity(
            ticker="NU",
            series_id="us_10y",
            ticker_prices=prices,
            series_points=rates,
            db_path=_db(repo_root),
        )
        is not None
    )


def test_macro_pack_levels_betas_and_stances(repo_root: Path) -> None:
    _seed_macro(repo_root)
    record_insight(
        scope_key="macro:rates-duration",
        kind="stance",
        body_md="I expect rates to stay above 4% through 2027; avoid duration bets.",
        source_note_ids=[],
        watermark_id=None,
        db_path=_db(repo_root),
    )
    items = load_packs(["macro"], db_path=_db(repo_root), focus_tickers=[])
    assert len(items) == 1
    text = str(items[0]["text"])
    assert "us_10y=4.2" in text
    assert "NU~us_10y" in text  # admitted v2 rate fit clears the r² floor
    estimate = fetch_sensitivities(ticker="NU", db_path=_db(repo_root))[0]
    assert estimate.input_sha is not None and estimate.input_sha in text
    assert "log return per +100 bps; v2_rate_diff" in text
    assert f"source {date.today().isoformat()}" in text
    assert "MELI" not in text  # r²=0.02 is noise, not exposure
    assert "macro:rates-duration" in text
    assert "rates to stay above 4%" in text


def test_macro_pack_reports_unknown_when_all_betas_weak(repo_root: Path) -> None:
    _ensure_macro_tables(repo_root)
    conn = sqlite3.connect(str(_db(repo_root)))
    try:
        conn.execute(
            "INSERT INTO macro_sensitivities "
            "(ticker, series_id, beta, r_squared, lookback_window_days, computed_at) "
            "VALUES ('MELI', 'usd_cad', -2.3, 0.02, 365, '2026-07-18T00:00:00')"
        )
        conn.commit()
    finally:
        conn.close()
    items = load_packs(["macro"], db_path=_db(repo_root), focus_tickers=[])
    text = str(items[0]["text"])
    assert "sensitivity unavailable" in text
    assert "not zero" in text
    assert "β=" not in text


# ------------------------------------------------------------- macro: namespace


def test_scope_key_for_macro_namespace_round_trips() -> None:
    assert scope_key_for("body", "macro:rates-duration") == "macro:rates-duration"
    # tenet: keys and bare topics keep the existing behavior.
    assert scope_key_for("body", "tenet:x-y") == "tenet:x-y"
    assert scope_key_for("body", "Some Topic").startswith("tenet:")
