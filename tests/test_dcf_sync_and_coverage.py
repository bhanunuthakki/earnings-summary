"""S11 PR2 — durable workbook→assumptions-JSON sync status + DCF coverage.

Four layers:
  * migration 0091 — the two nullable columns land on the real production
    table shape (init_db + alembic chain, the 0076 harness);
  * persist — DcfRunRow sync fields round-trip; a row carrying them against a
    pre-0091 schema raises with the migrate hint (loud, not dropped); rows
    without them keep working on either schema;
  * snapshot/card — dcf_runs sync columns flow into ValuationSnapshot when
    present and stay None on an old schema; the workspace card renders the
    quiet synced row and the loud FAILED row; markdown gains the meta part;
  * coverage panel — per-name classification (briefed/orphan, freshness tones,
    assumptions-JSON state, sync cell, skip reasons) and the dcf/redesign/
    stale-copies callout, from a fabricated repo root (no .xlsx ever opened —
    workbook files are empty placeholders).
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from dcf import persist as persist_mod  # noqa: E402
from pipeline.dcf_coverage_panel import collect_rows, render_dcf_coverage_panel  # noqa: E402
from report.models import SectionStatus, SnapshotSection, ValuationSnapshot  # noqa: E402
from report.renderers.markdown import (  # noqa: E402
    _valuation_card_md,  # pyright: ignore[reportPrivateUsage]  # testing an internal seam
)
from report.renderers.workspace_html import (  # noqa: E402
    _valuation_summary_panel,  # pyright: ignore[reportPrivateUsage]  # testing an internal seam
)
from report.sections import snapshot as snapshot_mod  # noqa: E402

PRIOR_HEAD = "0090_ask_claim_grounding_budget"
NEW_HEAD = "0091_dcf_runs_assumptions_sync"

_LEGACY_DCF_RUNS = """
CREATE TABLE dcf_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT UNIQUE,
    valuation_date TEXT, horizon_years INTEGER,
    wacc REAL, terminal_growth REAL,
    npv REAL, npv_per_share REAL, shares_outstanding REAL,
    currency TEXT, notes TEXT, run_id TEXT,
    live_price REAL, live_price_at TEXT, over_under_pct REAL,
    mos_bar_used REAL, assumption_snapshot_json TEXT,
    revenue_growths_json TEXT, fcf_margin REAL,
    breakdown_json TEXT
);
"""
_SYNCED_DCF_RUNS = _LEGACY_DCF_RUNS.replace(
    "    breakdown_json TEXT\n",
    "    breakdown_json TEXT,\n    assumptions_sync_status TEXT, assumptions_synced_at TEXT\n",
)


def _row(**overrides: object) -> persist_mod.DcfRunRow:
    base: dict[str, object] = {
        "ticker": "TESTCO",
        "valuation_date": date(2026, 6, 12),
        "horizon_years": 10,
        "wacc": 0.09,
        "npv": 1000.0,
        "npv_per_share": 100.0,
        "shares_outstanding": 1e9,
        "currency": "USD",
        "live_price": 80.0,
        "live_price_at": None,
        "mos_bar_used": None,
        "assumption_snapshot_json": "{}",
    }
    base.update(overrides)
    return persist_mod.DcfRunRow(**base)  # pyright: ignore[reportArgumentType]


# --------------------------------------------------------------------------- #
# migration 0091 (real chain — the 0076 harness)
# --------------------------------------------------------------------------- #
def _build_config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture(scope="module")
def prior_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    db = tmp_path_factory.mktemp("sync_cols_tmpl") / "at_0090.db"
    import db as dbmod

    dbmod.set_db_path(str(db))
    dbmod.init_db()
    cfg = _build_config(db)
    command.stamp(cfg, "0000_baseline")
    command.upgrade(cfg, PRIOR_HEAD)
    return db


def _columns(db: Path) -> set[str]:
    conn = sqlite3.connect(str(db))
    try:
        return {str(r[1]) for r in conn.execute("PRAGMA table_info(dcf_runs)")}
    finally:
        conn.close()


def test_migration_adds_nullable_sync_columns(prior_template: Path, tmp_path: Path) -> None:
    db = tmp_path / "sync_cols.db"
    shutil.copy(prior_template, db)
    before = _columns(db)
    assert "assumptions_sync_status" not in before
    command.upgrade(_build_config(db), NEW_HEAD)
    after = _columns(db)
    assert {"assumptions_sync_status", "assumptions_synced_at"} <= after

    # The refresher's write path works against the migrated table.
    conn = sqlite3.connect(str(db))
    try:
        persist_mod.upsert(
            conn,
            _row(
                assumptions_sync_status="synced",
                assumptions_synced_at=datetime(2026, 6, 12, 9, 30),
            ),
        )
        got = conn.execute(
            "SELECT assumptions_sync_status, assumptions_synced_at, over_under_pct"
            " FROM dcf_runs WHERE ticker='TESTCO'"
        ).fetchone()
    finally:
        conn.close()
    assert got[0] == "synced"
    assert got[1].startswith("2026-06-12")
    assert got[2] == pytest.approx(-0.2)  # the 0076 convention is untouched


def test_migration_downgrade_drops_columns(prior_template: Path, tmp_path: Path) -> None:
    db = tmp_path / "sync_cols_down.db"
    shutil.copy(prior_template, db)
    cfg = _build_config(db)
    command.upgrade(cfg, NEW_HEAD)
    command.downgrade(cfg, PRIOR_HEAD)
    assert "assumptions_sync_status" not in _columns(db)


# --------------------------------------------------------------------------- #
# persist — loud on a pre-0091 schema, silent for legacy writers
# --------------------------------------------------------------------------- #
def test_upsert_sync_fields_require_migrated_schema(tmp_path: Path) -> None:
    conn = sqlite3.connect(str(tmp_path / "legacy.db"))
    conn.executescript(_LEGACY_DCF_RUNS)
    try:
        with pytest.raises(sqlite3.OperationalError, match="0091"):
            persist_mod.upsert(conn, _row(assumptions_sync_status="synced"))
        # A legacy row (no sync fields — the bespoke archetype builders) still
        # writes fine against the old schema.
        persist_mod.upsert(conn, _row())
        n = conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0]
    finally:
        conn.close()
    assert n == 1


# --------------------------------------------------------------------------- #
# snapshot -> ValuationSnapshot -> card/markdown
# --------------------------------------------------------------------------- #
def _seed_repo(tmp_path: Path, *, schema: str, sync_cols: bool) -> Path:
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    conn = sqlite3.connect(str(repo / "data" / "portfolio.db"))
    conn.executescript(schema)
    extra_cols = ", assumptions_sync_status, assumptions_synced_at" if sync_cols else ""
    extra_vals = ", 'synced', '2026-06-12T09:30:00'" if sync_cols else ""
    conn.execute(
        "INSERT INTO dcf_runs (ticker, valuation_date, horizon_years, wacc,"
        " terminal_growth, npv, npv_per_share, shares_outstanding, currency,"
        f" live_price, over_under_pct, assumption_snapshot_json{extra_cols}) VALUES"
        " ('TEST', '2026-06-12', 10, 0.0965, 0, 1500000, 610.79, 2.5e9, 'USD',"
        f"  568.43, -0.0694, '{{}}'{extra_vals})"
    )
    conn.commit()
    conn.close()
    return repo


def test_valuation_snapshot_reads_sync_columns(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path, schema=_SYNCED_DCF_RUNS, sync_cols=True)
    v = snapshot_mod._valuation_snapshot(  # pyright: ignore[reportPrivateUsage]
        "TEST", repo, current_price=None, model_link=None, mos_bar=None
    )
    assert v.assumptions_sync_status == "synced"
    assert v.assumptions_synced_at == datetime(2026, 6, 12, 9, 30)


def test_valuation_snapshot_degrades_without_sync_columns(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path, schema=_LEGACY_DCF_RUNS, sync_cols=False)
    v = snapshot_mod._valuation_snapshot(  # pyright: ignore[reportPrivateUsage]
        "TEST", repo, current_price=None, model_link=None, mos_bar=None
    )
    assert v.consolidated_npv_per_share == 610.79  # the read itself still works
    assert v.assumptions_sync_status is None
    assert v.assumptions_synced_at is None


def _render_panel(v: ValuationSnapshot) -> str:
    snap = SnapshotSection(status=SectionStatus.OK, ticker="TEST", valuation=v)
    body = StringIO()
    _valuation_summary_panel(body, snap)
    return body.getvalue()


def test_card_shows_quiet_synced_row_and_loud_failure() -> None:
    ok = _render_panel(
        ValuationSnapshot(
            consolidated_npv_per_share=610.79,
            current_price=568.43,
            assumptions_sync_status="synced",
            assumptions_synced_at=datetime(2026, 6, 12, 9, 30),
        )
    )
    assert "Assumptions JSON" in ok
    assert "synced · 2026-06-12" in ok
    assert "FAILED" not in ok

    bad = _render_panel(
        ValuationSnapshot(
            consolidated_npv_per_share=610.79,
            current_price=568.43,
            assumptions_sync_status="failed: write failed: disk full",
        )
    )
    assert "sync FAILED" in bad
    assert "from-scratch rebuild would revert" in bad

    quiet = _render_panel(ValuationSnapshot(consolidated_npv_per_share=610.79))
    assert "Assumptions JSON" not in quiet  # pre-0091 rows stay silent


def test_markdown_meta_gains_sync_part() -> None:
    out = StringIO()
    _valuation_card_md(
        out,
        ValuationSnapshot(
            consolidated_npv_per_share=610.79,
            current_price=568.43,
            wacc=0.0965,
            assumptions_sync_status="failed: unreadable assumptions JSON",
        ),
    )
    assert "**assumptions sync failed: unreadable assumptions JSON**" in out.getvalue()


# --------------------------------------------------------------------------- #
# coverage panel
# --------------------------------------------------------------------------- #
def _coverage_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A repo root with: 2 briefed names (LIVE fresh+synced, SKIPME bank-skip),
    1 stale briefed name with no workbook and no JSON (NOWB), 1 legacy
    wacc-seeded name (WSEED — maintained by --all-named without being
    briefed), 1 true orphan workbook (ORPH), and a stale dcf/redesign/ copy."""
    repo = tmp_path / "repo"
    (repo / "data" / "dcf_assumptions").mkdir(parents=True)
    (repo / "dcf" / "redesign").mkdir(parents=True)
    (repo / "micro_thesis" / "holdings").mkdir(parents=True)
    db = repo / "data" / "portfolio.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        _SYNCED_DCF_RUNS
        + """
        CREATE TABLE tracked_companies (ticker TEXT, list_type TEXT, name TEXT);
        INSERT INTO tracked_companies VALUES
            ('LIVE', 'portfolio', 'Live Co'),
            ('SKIPME', 'evaluation', 'Skip Bank'),
            ('NOWB', 'evaluation', 'No Workbook Co'),
            ('WATCHY', 'watchlist', 'Watch Co');
        """
    )
    today = date.today()
    conn.execute(
        "INSERT INTO dcf_runs (ticker, valuation_date, live_price_at, horizon_years,"
        " wacc, terminal_growth, npv, npv_per_share, shares_outstanding, currency,"
        " notes, assumption_snapshot_json, assumptions_sync_status,"
        " assumptions_synced_at) VALUES (?, ?, ?, 10, 0.09, 0, 1, 100.0, 1e9,"
        " 'USD', 'workbook=LIVE.xlsx (redesigned)', ?, 'synced', ?)",
        (
            "LIVE",
            today.isoformat(),
            today.isoformat() + "T08:00:00",  # price leg re-priced today (fresh)
            json.dumps({"format": "redesign"}),
            today.isoformat() + "T08:00:00",
        ),
    )
    stale_day = (today - timedelta(days=45)).isoformat()
    conn.execute(
        "INSERT INTO dcf_runs (ticker, valuation_date, horizon_years, wacc,"
        " terminal_growth, npv, npv_per_share, shares_outstanding, currency,"
        " notes, assumption_snapshot_json) VALUES (?, ?, 10, 0.09, 0, 1, 50.0,"
        " 1e9, 'USD', 'workbook=ORPH.xlsx (redesigned)', '{}')",
        ("ORPH", stale_day),
    )
    conn.commit()
    conn.close()

    (repo / "dcf" / "LIVE.xlsx").write_bytes(b"")
    (repo / "dcf" / "ORPH.xlsx").write_bytes(b"")
    (repo / "dcf" / "WSEED.xlsx").write_bytes(b"")
    (repo / "dcf" / "redesign" / "LIVE.xlsx").write_bytes(b"")
    (repo / "micro_thesis" / "holdings" / "WSEED.json").write_text(
        json.dumps({"wacc": 0.085}), encoding="utf-8"
    )
    (repo / "data" / "dcf_assumptions" / "LIVE.json").write_text(
        json.dumps(
            {
                "redesign": {"dcf_applicable": True, "exit_multiple": 12.0},
                "opus_baseline": {"as_of": "2026-06-12", "seeded": True, "values": {}},
            }
        ),
        encoding="utf-8",
    )
    (repo / "data" / "dcf_assumptions" / "SKIPME.json").write_text(
        json.dumps(
            {
                "redesign": {
                    "dcf_applicable": False,
                    "business_model": "bank",
                    "valuation_model": "bank_excess_return",
                }
            }
        ),
        encoding="utf-8",
    )
    return db, repo


def test_collect_rows_classifies_states(tmp_path: Path) -> None:
    db, repo = _coverage_repo(tmp_path)
    rows, stale_copies = collect_rows(db, repo)
    by = {r.ticker: r for r in rows}

    assert stale_copies == ["LIVE"]
    # Briefed first, then watchlist/wacc-seed maintained, then true orphans.
    assert [r.ticker for r in rows] == ["LIVE", "NOWB", "SKIPME", "WATCHY", "WSEED", "ORPH"]

    live = by["LIVE"]
    assert live.in_universe and live.workbook_exists
    assert live.sync_status == "synced"
    assert live.assumptions_state == "opus (seeded)"
    assert live.run_format == "redesign"
    # Both freshness legs read independently: LIVE was valued and re-priced today.
    assert live.last_valued == date.today()
    assert live.last_priced == date.today()
    # ORPH's run carried no live price -> the price leg is None ("never priced"),
    # distinct from its (stale) valuation_date.
    assert by["ORPH"].last_valued is not None and by["ORPH"].last_priced is None

    skip = by["SKIPME"]
    assert skip.model == "bank_excess_return" and skip.model_source == "opus"

    nowb = by["NOWB"]
    assert nowb.in_universe and not nowb.workbook_exists
    assert nowb.last_valued is None and nowb.assumptions_state == "none"

    wseed = by["WSEED"]
    assert wseed.maintained and not wseed.in_universe and wseed.legacy_maintained
    assert "orphan" not in wseed.note

    # Watchlist names get daily refresh_dcf runs — maintained, not briefed.
    watchy = by["WATCHY"]
    assert watchy.maintained and not watchy.in_universe
    assert watchy.list_type == "watchlist" and "orphan" not in watchy.note

    orph = by["ORPH"]
    assert not orph.in_universe
    assert "orphan" in orph.note and "workbook" in orph.note


def test_panel_renders_kpis_table_and_stale_callout(tmp_path: Path) -> None:
    db, repo = _coverage_repo(tmp_path)
    html_out = render_dcf_coverage_panel(db, repo)
    assert "DCF coverage" in html_out
    assert "Maintained" in html_out and "Orphans" in html_out
    assert "LIVE" in html_out and "ORPH" in html_out
    # never-valued briefed name shows loud, orphan badged
    assert "never" in html_out
    assert "orphan" in html_out
    # the dcf/redesign/ reconciliation callout names the superseded copy
    assert "dcf/redesign/" in html_out
    assert "superseded" in html_out
    # tokens only — no raw hex colors in the fragment
    assert "#1F4E78" not in html_out and "#fff" not in html_out.lower()


def test_panel_empty_repo_degrades(tmp_path: Path) -> None:
    repo = tmp_path / "empty"
    (repo / "data").mkdir(parents=True)
    out = render_dcf_coverage_panel(repo / "data" / "portfolio.db", repo)
    assert "No DCF artifacts" in out


def test_price_leg_freshness_is_independent_of_valuation(tmp_path: Path) -> None:
    """The L6 honesty fix: a name valued TODAY but whose price leg is weeks old
    must NOT read as fully fresh. collect_rows exposes the two legs separately
    and the panel tones the stale price loud even beside a fresh fair value —
    the single valuation-date tone used to hide exactly this (over_under_pct
    frozen on an old price between opt-in refresh_dcf runs)."""
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    db = repo / "data" / "portfolio.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        _SYNCED_DCF_RUNS
        + "CREATE TABLE tracked_companies (ticker TEXT, list_type TEXT, name TEXT);"
        " INSERT INTO tracked_companies VALUES ('STALEPX', 'portfolio', 'Stale Price Co');"
    )
    today = date.today()
    stale_price_day = (today - timedelta(days=25)).isoformat() + "T08:00:00"
    conn.execute(
        "INSERT INTO dcf_runs (ticker, valuation_date, live_price_at, horizon_years,"
        " wacc, terminal_growth, npv, npv_per_share, shares_outstanding, currency,"
        " live_price, over_under_pct, notes, assumption_snapshot_json) VALUES"
        " ('STALEPX', ?, ?, 10, 0.09, 0, 1, 100.0, 1e9, 'USD', 90.0, -0.1,"
        " 'workbook=STALEPX.xlsx (redesigned)', '{}')",
        (today.isoformat(), stale_price_day),
    )
    conn.commit()
    conn.close()

    rows, _ = collect_rows(db, repo)
    row = next(r for r in rows if r.ticker == "STALEPX")
    # Fresh fair value, stale price — the two legs are genuinely different dates.
    assert row.last_valued == today
    assert row.last_priced == (today - timedelta(days=25))
    assert row.last_valued != row.last_priced

    html_out = render_dcf_coverage_panel(db, repo)
    assert "Priced" in html_out  # the price-leg column header exists
    # The fair-value cell is fresh (green/ok); the price cell is stale (warn) —
    # both tone classes present in the same row, so a reader sees the split.
    assert "dcv-ok" in html_out
    assert "dcv-warn" in html_out
