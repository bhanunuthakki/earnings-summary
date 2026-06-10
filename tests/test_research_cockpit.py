"""Tests for the Research cockpit (pipeline/research_cockpit.py, master build P1.2).

Build + render are exercised against a REAL alembic-migrated SQLite DB
(init_db + stamp 0000_baseline + upgrade head, the test_tenant_identity
pattern) so the queries agree with the production schema — kpi fact
supersession, alert CHECK constraints, dcf_runs column shapes — not a
hand-rolled approximation. Timestamps are seeded relative to wall-clock now
so staleness/relative-time assertions hold on any run date. The disk caches
(FMP profile, earnings calendar, §Valuation) live in a tmp repo_root.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db as dbmod  # noqa: E402
from pipeline.research_cockpit import (  # noqa: E402
    CockpitRow,
    build_cockpit_rows,
    render_research_cockpit,
)
from report.renderers.numfmt import fmt_date  # noqa: E402

NOW = datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.replace(tzinfo=None).isoformat(timespec="seconds")


@pytest.fixture(scope="module")
def head_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One fully-migrated DB (init_db + alembic head), shared across the module."""
    db = tmp_path_factory.mktemp("cockpit_tmpl") / "head.db"
    dbmod.set_db_path(str(db))
    dbmod.init_db()
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.stamp(cfg, "0000_baseline")
    command.upgrade(cfg, "head")
    return db


@pytest.fixture
def conn(head_template: Path, tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db = tmp_path / "portfolio.db"
    shutil.copy(head_template, db)
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    _seed(c)
    yield c
    c.close()


def _seed(c: sqlite3.Connection) -> None:
    c.execute("INSERT OR IGNORE INTO tenants (id, created_at) VALUES ('bhanu', ?)", (_iso(NOW),))
    c.execute(
        "INSERT INTO tracked_companies (user_id, ticker, name, list_type, instrument_type, "
        "last_built_at) VALUES ('bhanu', 'NU', 'Nu & Co Holdings', 'portfolio', 'equity', ?)",
        (_iso(NOW - timedelta(days=2)),),
    )
    c.execute(
        "INSERT INTO tracked_companies (user_id, ticker, name, list_type, instrument_type, "
        "last_built_at) VALUES ('bhanu', 'AAA', 'Alpha Corp', 'portfolio', 'equity', NULL)"
    )
    c.execute(
        "INSERT INTO tracked_companies (user_id, ticker, name, list_type, instrument_type, "
        "last_built_at) VALUES ('bhanu', 'V', 'Visa', 'evaluation', 'equity', NULL)"
    )
    # Ops freshness: NU pulled an hour ago (fresh); AAA / V never pulled.
    c.execute(
        "INSERT INTO fmp_endpoint_status (ticker, endpoint, period, status, last_pulled) "
        "VALUES ('NU', 'income-statement', 'quarter', 'ok', ?)",
        (_iso(NOW - timedelta(hours=1)),),
    )
    # Thesis: NU latest = warn, with a rule tying a tier-1 KPI name to 'warn'.
    rules = [
        {"kpi_name": "Monthly ARPAC (USD)", "status": "warn", "tier": "business_model"},
        {"kpi_name": "FCF Margin (GAAP)", "status": "ok", "tier": "universal"},
    ]
    c.execute(
        "INSERT INTO thesis_evaluations (ticker, evaluated_at, overall_status, "
        "rule_evaluations_json) VALUES ('NU', ?, 'warn', ?)",
        (_iso(NOW - timedelta(days=1)), json.dumps(rules)),
    )
    c.execute(  # older NU evaluation must be ignored (latest wins)
        "INSERT INTO thesis_evaluations (ticker, evaluated_at, overall_status, "
        "rule_evaluations_json) VALUES ('NU', ?, 'breach', '[]')",
        (_iso(NOW - timedelta(days=9)),),
    )
    c.execute(
        "INSERT INTO thesis_evaluations (ticker, evaluated_at, overall_status, "
        "rule_evaluations_json) VALUES ('AAA', ?, 'ok', '[]')",
        (_iso(NOW - timedelta(days=1)),),
    )
    # Documents: one fetched before NU's last build, one after (-> 1 new doc).
    for i, fetched in enumerate((NOW - timedelta(days=3), NOW - timedelta(days=1))):
        c.execute(
            "INSERT INTO documents (ticker, source_type, doc_type, file_path, sha256, "
            "fetched_at, fetch_status, raw_bytes_size) "
            "VALUES ('NU', 'fmp', 'fmp_statements', ?, ?, ?, 'ok', 10)",
            (f"data/doc{i}.json", f"{i:064d}", _iso(fetched)),
        )
    # Tier-1 KPI definitions + facts. ARPAC moves +10% (usd -> relative move,
    # toned warn via the rule above); ROE moves -1.5pp (percent unit).
    c.execute(
        "INSERT INTO kpi_definitions (ticker, name, unit, primary_source, threshold_tier) "
        "VALUES ('NU', 'Monthly ARPAC (USD)', 'usd', 'ir_pdf', 'tier_1_break')"
    )
    arpac_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    c.execute(
        "INSERT INTO kpi_definitions (ticker, name, unit, primary_source, threshold_tier) "
        "VALUES ('NU', 'ROE (annualized)', 'percent', 'ir_pdf', 'tier_1_break')"
    )
    roe_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    c.execute(  # tier-2 KPI must never become a chip
        "INSERT INTO kpi_definitions (ticker, name, unit, primary_source, threshold_tier) "
        "VALUES ('NU', 'Support tickets', 'count', 'ir_pdf', 'tier_2_monitor')"
    )
    tier2_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    doc_ids = [int(r[0]) for r in c.execute("SELECT id FROM documents ORDER BY id")]
    c.execute(
        "INSERT INTO transcripts (document_id, ticker, period_end, has_qa_section) "
        "VALUES (?, 'NU', '2026-03-31', 1)",
        (doc_ids[0],),
    )

    def fact(def_id: int, period: str, value: float, unit: str, doc: int = 0) -> int:
        c.execute(
            "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, "
            "kpi_definition_id, value, unit, source_doc_id) "
            "VALUES ('NU', ?, 'Q1', ?, ?, ?, ?)",
            (period, def_id, value, unit, doc_ids[doc]),
        )
        return int(c.execute("SELECT last_insert_rowid()").fetchone()[0])

    fact(arpac_id, "2025-12-31 00:00:00", 11.2, "usd")
    stale = fact(arpac_id, "2026-03-31 00:00:00", 99.0, "usd")  # mis-extraction…
    # …superseded by a restatement from a different source doc (the provenance
    # unique index allows same-period rows only across distinct source docs).
    fixed = fact(arpac_id, "2026-03-31 00:00:00", 12.4, "usd", doc=1)
    c.execute("UPDATE kpi_facts SET supersedes_id=? WHERE id=?", (stale, fixed))
    fact(roe_id, "2025-12-31 00:00:00", 28.0, "percent")
    fact(roe_id, "2026-03-31 00:00:00", 26.5, "percent")
    fact(tier2_id, "2025-12-31 00:00:00", 5.0, "count")
    fact(tier2_id, "2026-03-31 00:00:00", 50.0, "count")
    # DCF (one row per ticker — uq_dcf_runs_ticker): the stored over_under_pct
    # is GARBAGE (the bank/holdco writers used a different convention than the
    # documented ratio) — the cockpit must recompute from price + FV.
    c.execute(
        "INSERT INTO dcf_runs (ticker, valuation_date, horizon_years, revenue_growths_json, "
        "fcf_margin, wacc, terminal_growth, npv, npv_per_share, live_price, over_under_pct, "
        "created_at) VALUES ('NU', '2026-06-09', 10, '[]', 0.2, 0.1, 0.025, 1000, 20.0, 10.0, "
        "79.82, ?)",
        (_iso(NOW - timedelta(days=1)),),
    )
    c.execute(  # FV missing -> no gap
        "INSERT INTO dcf_runs (ticker, valuation_date, horizon_years, revenue_growths_json, "
        "fcf_margin, wacc, terminal_growth, npv, npv_per_share, live_price, created_at) "
        "VALUES ('V', '2026-06-09', 10, '[]', 0.2, 0.1, 0.025, 1000, NULL, 300.0, ?)",
        (_iso(NOW - timedelta(days=1)),),
    )
    # Alerts: NU 2 pending + 1 dismissed -> inbox shows 2.
    for n, status in enumerate(("pending", "pending", "dismissed")):
        c.execute(
            "INSERT INTO alerts (user_id, ticker, trigger_kind, fired_at, status, "
            "evidence_json, signature_sha) "
            "VALUES ('bhanu', 'NU', 'kpi_inflection', ?, ?, '{}', ?)",
            (_iso(NOW - timedelta(hours=5)), status, f"sig-{n}"),
        )
    c.commit()


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """A repo tree with NU's disk caches; AAA / V have none (degrade to em-dash)."""
    root = tmp_path / "repo"
    fmp = root / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True)
    (fmp / "NU_profile.json").write_text(
        json.dumps([{"price": 12.29, "changePercentage": 0.82, "companyName": "Nu & Co"}]),
        encoding="utf-8",
    )
    calendar = [
        {"symbol": "NU", "date": (NOW + timedelta(days=64)).date().isoformat()},
        {"symbol": "NU", "date": (NOW + timedelta(days=30)).date().isoformat()},
        {"symbol": "NU", "date": (NOW - timedelta(days=60)).date().isoformat()},
    ]
    (fmp / "NU_earnings_calendar.json").write_text(json.dumps(calendar), encoding="utf-8")
    vb = root / "data" / "valuation_basis"
    vb.mkdir(parents=True)
    (vb / "NU.json").write_text(
        json.dumps({"ticker": "NU", "peg_ratio": 0.52, "peg_growth_pct": 38.1}),
        encoding="utf-8",
    )
    # A workspace build artifact so dashboard_status reports a build for NU.
    research = root / "output" / "research" / "NU"
    research.mkdir(parents=True)
    (research / "2026-06-08_workspace.html").write_text("<html>r</html>", encoding="utf-8")
    return root


@pytest.fixture
def rows(conn: sqlite3.Connection, repo_root: Path) -> dict[str, list[CockpitRow]]:
    return build_cockpit_rows(conn, repo_root)


def _by_ticker(rows_list: list[CockpitRow]) -> dict[str, CockpitRow]:
    return {r.base.ticker: r for r in rows_list}


# --------------------------------------------------------------------------- #
# build_cockpit_rows
# --------------------------------------------------------------------------- #


def test_build_groups_and_attention_sorts(rows: dict[str, list[CockpitRow]]) -> None:
    assert set(rows) == {"portfolio", "evaluation"}
    # NU (warn) outranks AAA (ok) despite alphabetical order.
    assert [r.base.ticker for r in rows["portfolio"]] == ["NU", "AAA"]
    assert [r.base.ticker for r in rows["evaluation"]] == ["V"]


def test_build_thesis_fields(rows: dict[str, list[CockpitRow]]) -> None:
    nu = _by_ticker(rows["portfolio"])["NU"]
    assert nu.base.breach_status == "warn"  # latest evaluation wins
    assert nu.rule_summary == "warn: Monthly ARPAC (USD)"
    assert nu.name == "Nu & Co Holdings"


def test_build_tier1_kpi_deltas(rows: dict[str, list[CockpitRow]]) -> None:
    nu = _by_ticker(rows["portfolio"])["NU"]
    by_name = {d.name: d for d in nu.kpi_deltas}
    assert set(by_name) == {"Monthly ARPAC (USD)", "ROE (annualized)"}  # tier-2 excluded
    arpac = by_name["Monthly ARPAC (USD)"]
    assert arpac.latest_value == 12.4  # superseded mis-extraction (99.0) ignored
    assert arpac.delta_display == "+10.7%"  # relative move for a non-percent unit
    assert arpac.tone == "warn"  # toned via the matching break rule
    roe = by_name["ROE (annualized)"]
    assert roe.delta_display == "-1.5pp"  # pp move for a percent unit
    assert roe.tone == "neutral"  # no rule references ROE


def test_build_kpi_deltas_only_for_portfolio(rows: dict[str, list[CockpitRow]]) -> None:
    assert _by_ticker(rows["evaluation"])["V"].kpi_deltas == []


def test_build_valuation_fields(rows: dict[str, list[CockpitRow]]) -> None:
    nu = _by_ticker(rows["portfolio"])["NU"]
    assert nu.price == 12.29
    assert nu.day_move_pct == 0.82
    assert nu.peg_ratio == 0.52
    # Latest run: price 10 vs FV 20 -> recomputed -50%; the garbage stored
    # over_under_pct (+79.82, wrong writer convention) must be ignored.
    assert nu.fv_gap_pct == pytest.approx(-50.0)
    assert nu.dcf_date == "2026-06-09"
    v = _by_ticker(rows["evaluation"])["V"]
    assert v.fv_gap_pct is None  # FV missing -> no gap
    assert v.price is None  # no profile cache on disk


def test_build_event_fields(rows: dict[str, list[CockpitRow]]) -> None:
    nu = _by_ticker(rows["portfolio"])["NU"]
    assert nu.next_earnings == (NOW + timedelta(days=30)).date().isoformat()  # earliest future
    assert nu.pending_alerts == 2  # dismissed alert not counted
    assert nu.new_docs == 1  # only the doc fetched after last_built_at
    v = _by_ticker(rows["evaluation"])["V"]
    assert (v.next_earnings, v.pending_alerts, v.new_docs) == (None, 0, 0)


def test_build_degrades_on_minimal_schema(tmp_path: Path) -> None:
    """The hand-rolled comments-server test schema (no alerts / dcf_runs /
    documents / kpi tables, no last_built_at column) renders a sparser cockpit
    instead of raising."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(
        """
        CREATE TABLE tracked_companies (
            id INTEGER PRIMARY KEY, user_id TEXT DEFAULT 'bhanu', ticker TEXT,
            name TEXT, list_type TEXT, instrument_type TEXT, archived_at TIMESTAMP,
            fmp_data_saved INTEGER DEFAULT 0, fmp_data_upto TEXT, added_at TIMESTAMP,
            sec_validated INTEGER DEFAULT 0, ir_url TEXT, filing_regime TEXT,
            fiscal_year_end TEXT
        );
        CREATE TABLE transcripts (ticker TEXT, period_end TIMESTAMP, has_qa_section INTEGER,
            call_date TIMESTAMP);
        CREATE TABLE thesis_evaluations (ticker TEXT, evaluated_at TIMESTAMP,
            overall_status TEXT, rule_evaluations_json TEXT);
        CREATE TABLE fmp_endpoint_status (ticker TEXT, endpoint TEXT, period TEXT,
            last_pulled TIMESTAMP);
        """
    )
    c.execute(
        "INSERT INTO tracked_companies (user_id, ticker, name, list_type, instrument_type) "
        "VALUES ('bhanu', 'NU', 'Nu', 'portfolio', 'equity')"
    )
    c.commit()
    rows = build_cockpit_rows(c, tmp_path)
    (nu,) = rows["portfolio"]
    assert nu.base.ticker == "NU"
    assert nu.pending_alerts == 0
    assert nu.fv_gap_pct is None
    assert nu.kpi_deltas == []
    html = render_research_cockpit(rows)
    assert "NU" in html


# --------------------------------------------------------------------------- #
# render_research_cockpit
# --------------------------------------------------------------------------- #


def test_render_badges_chips_and_pills(rows: dict[str, list[CockpitRow]]) -> None:
    html = render_research_cockpit(rows)
    # Verdict badge with tone + rule summary in the hover.
    assert "cockpit-badge b-warn" in html
    assert "warn: Monthly ARPAC (USD)" in html
    # KPI chips with tone + the pp/relative split.
    assert "+10.7%" in html
    assert "-1.5pp" in html
    assert "chip-warn" in html
    # Inbox pills: pending alerts deep-link into the feed; new docs counted.
    assert "/feed?ticker=NU&amp;status=pending" in html or "/feed?ticker=NU&status=pending" in html
    assert "2 alerts" in html
    assert "1 new doc" in html


def test_render_valuation_cells(rows: dict[str, list[CockpitRow]]) -> None:
    html = render_research_cockpit(rows)
    assert "$12.29" in html
    assert "+0.8%" in html  # day move, signed
    assert "-50.0%" in html  # vs-FV gap
    assert "0.5" in html  # PEG
    nu_er = (NOW + timedelta(days=30)).date().isoformat()
    assert fmt_date(nu_er, include_year=False) in html


def test_render_staleness_dots(rows: dict[str, list[CockpitRow]]) -> None:
    html = render_research_cockpit(rows)
    assert "dot-ok" in html  # NU: FMP 1h ago, build 2d ago
    assert "dot-bad" in html  # AAA / V: never pulled, never built
    assert "transcript 2026-03-31 (Q&amp;A)" in html  # detail lives in the hover


def test_render_thin_evaluation_variant(rows: dict[str, list[CockpitRow]]) -> None:
    html = render_research_cockpit(rows)
    assert "cockpit-thin" in html
    # The thin table drops the KPI-moves column; the full table keeps it.
    assert html.count("Tier-1 moves") == 1


def test_render_escapes_company_name(rows: dict[str, list[CockpitRow]]) -> None:
    html = render_research_cockpit(rows)
    assert "Nu &amp; Co Holdings" in html


def test_render_empty_lists() -> None:
    html = render_research_cockpit({"portfolio": [], "evaluation": []})
    assert "No portfolio tickers." in html
    assert "No evaluation tickers." in html
