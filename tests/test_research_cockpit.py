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

import hashlib
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
from pipeline.dashboard_status import DashboardRow  # noqa: E402
from pipeline.research_cockpit import (  # noqa: E402
    CockpitRow,
    _eval_fundamentals,  # pyright: ignore[reportPrivateUsage]  # testing an internal seam
    _price_cell,  # pyright: ignore[reportPrivateUsage]  # testing an internal seam
    _tier1_kpi_deltas,  # pyright: ignore[reportPrivateUsage]  # testing an internal seam
    attractiveness_breakdown,
    attractiveness_tone,
    build_cockpit_rows,
    compute_attractiveness,
    dcf_sanity_flags,
    eval_attractiveness,
    latest_dcf_runs,
    render_research_cockpit,
)
from provenance.evidence_ledger import (  # noqa: E402
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    EvidenceNode,
    ExtractionRun,
    SourceObservation,
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


def _bind_document_evidence(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    document_id: int,
) -> None:
    """Give a legacy test document the evidence required by current head."""

    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    row = conn.execute(
        "SELECT sha256, raw_bytes_size FROM documents WHERE id = ?", (document_id,)
    ).fetchone()
    assert row is not None
    blob_sha = str(row[0])
    config_sha = hashlib.sha256(b"research-cockpit-test").hexdigest()
    output_sha = hashlib.sha256(f"output:{ticker}:{document_id}".encode()).hexdigest()
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ContentBlob(
            sha256=blob_sha,
            byte_size=int(row[1]),
            media_type="application/json",
            storage_uri=f"file:///test/{ticker}-{document_id}.json",
            recorded_at=stamp,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id=f"source:{ticker}:{document_id}",
            idempotency_key=f"source:{ticker}:{document_id}",
            source_kind="vendor_api",
            source_url=f"https://example.test/{ticker}/{document_id}",
            blob_sha256=blob_sha,
            source_published_at=stamp,
            filing_at=None,
            accepted_at=None,
            observed_at=stamp,
            retrieved_at=stamp,
            retrieval_config_sha256=config_sha,
            collector_code_version="test@1",
        )
    )
    ledger.persist(
        DocumentVersion(
            document_version_id=f"document:{ticker}:{document_id}",
            document_key=f"{ticker}:vendor:{document_id}",
            version_sequence=1,
            observation_id=f"source:{ticker}:{document_id}",
            blob_sha256=blob_sha,
            issuer_id=f"issuer:{ticker}",
            ticker=ticker,
            document_type="vendor_statement",
            form_type="vendor_json",
            accession_number=None,
            exhibit_id=None,
            period_start=None,
            period_end=stamp,
            as_of_at=stamp,
            language="en",
            replaces_document_version_id=None,
            legacy_document_id=document_id,
            recorded_at=stamp,
        )
    )
    ledger.persist(
        ExtractionRun(
            extraction_run_id=f"run:{ticker}:{document_id}",
            idempotency_key=f"run:{ticker}:{document_id}",
            document_version_id=f"document:{ticker}:{document_id}",
            input_sha256=blob_sha,
            extractor_name="test-fixture",
            extractor_config_sha256=config_sha,
            extractor_code_version="test@1",
            output_sha256=output_sha,
            started_at=stamp,
            completed_at=stamp,
            outcome="succeeded",
        )
    )
    ledger.persist(
        EvidenceNode(
            node_id=f"node:{ticker}:{document_id}",
            evidence_key=f"node:{ticker}:{document_id}",
            revision=1,
            extraction_run_id=f"run:{ticker}:{document_id}",
            parent_node_id=None,
            supersedes_node_id=None,
            node_kind="document",
            text=f"{ticker} vendor statement",
            locator=None,
            recorded_at=stamp,
        )
    )


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
    for doc_id in doc_ids:
        _bind_document_evidence(c, ticker="NU", document_id=doc_id)
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
    # DCF (one row per ticker — uq_dcf_runs_ticker): stored over_under_pct is
    # NULL. The 0076 substrate CHECK forbids the old inconsistent garbage (the
    # bank/holdco percent-upside writes) from existing at all, so NULL is the
    # untrustworthy shape that remains — the cockpit must still recompute the
    # gap from price + FV rather than read the stored column.
    c.execute(
        "INSERT INTO dcf_runs (ticker, valuation_date, horizon_years, revenue_growths_json, "
        "fcf_margin, wacc, terminal_growth, npv, npv_per_share, live_price, "
        "created_at) VALUES ('NU', '2026-06-09', 10, '[]', 0.2, 0.1, 0.025, 1000, 20.0, 10.0, "
        "?)",
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


def test_tier1_kpi_query_does_not_scan_unrelated_fact_history(
    conn: sqlite3.Connection,
) -> None:
    """A portfolio render must not scan every other ticker's KPI history.

    ``supersedes_id`` is a same-ticker restatement link.  The cockpit only
    needs supersession rows for the requested portfolio tickers; a global
    anti-join made its cost grow with the full 255k-row production ledger.
    SQLite's progress handler gives this regression a deterministic VM-work
    budget without depending on wall-clock timing.
    """
    conn.execute(
        "INSERT INTO kpi_definitions "
        "(ticker, name, unit, primary_source, threshold_tier) "
        "VALUES ('ZZZ', 'Unrelated history', 'count', 'ir_pdf', 'tier_2_monitor')"
    )
    definition_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    source_doc_id = int(conn.execute("SELECT id FROM documents LIMIT 1").fetchone()[0])
    base = datetime(2000, 1, 1)
    conn.executemany(
        "INSERT INTO kpi_facts "
        "(ticker, period_end, fiscal_period_type, kpi_definition_id, value, unit, source_doc_id) "
        "VALUES ('ZZZ', ?, 'Q1', ?, 1, 'count', ?)",
        (
            ((base + timedelta(seconds=i)).isoformat(), definition_id, source_doc_id)
            for i in range(5_000)
        ),
    )

    progress_callbacks = 0

    def count_progress() -> int:
        nonlocal progress_callbacks
        progress_callbacks += 1
        return 0

    conn.set_progress_handler(count_progress, 100)
    try:
        deltas = _tier1_kpi_deltas(conn, {"NU"}, as_of=NOW.date())
    finally:
        conn.set_progress_handler(None, 0)

    assert "NU" in deltas
    assert progress_callbacks < 50


def test_build_kpi_deltas_only_for_portfolio(rows: dict[str, list[CockpitRow]]) -> None:
    assert _by_ticker(rows["evaluation"])["V"].kpi_deltas == []


def test_rule_tone_fuzzy_match() -> None:
    """The evaluator's rule names and the KPI-definition names drift apart in
    casing and unit suffixes on prod (AMZN matched 0 of 3 tier-1 defs, TSM's
    live BREACH rendered a neutral gray chip) — a breached rule must still
    tone its chip: exact match first, then case-insensitive, then token-set
    (subset either way), worst status winning on ambiguity."""
    from pipeline.research_cockpit import _rule_status_for  # pyright: ignore[reportPrivateUsage]

    tones = {
        "Gross Margin (GAAP)": "breach",
        "AWS Operating Margin": "warn",
        "FCF Margin (GAAP)": "ok",
    }
    # Exact, casefold, token-subset rungs.
    assert _rule_status_for("Gross Margin (GAAP)", tones) == ("breach", "Gross Margin (GAAP)")
    assert _rule_status_for("AWS operating margin", tones) == ("warn", "AWS Operating Margin")
    assert _rule_status_for("Gross margin", tones) == ("breach", "Gross Margin (GAAP)")
    # Unrelated names never match — no tone is better than a wrong tone.
    assert _rule_status_for("Members YoY growth", tones) == ("", "")
    # Ambiguous token matches take the WORST status — never hide a breach.
    multi = {"Revenue YoY Growth (USD)": "ok", "Total Revenue YoY Growth (USD)": "breach"}
    status, _rule = _rule_status_for("Revenue YoY growth", multi)
    assert status == "breach"


def test_toned_severity_first_under_cap() -> None:
    """A breached tier-1 KPI must never be pushed out of the capped chip row
    by bigger-but-benign moves (the old pure-magnitude sort dropped it)."""
    from pipeline.research_cockpit import KpiDelta, _toned  # pyright: ignore[reportPrivateUsage]

    def mk(name: str, latest: float) -> KpiDelta:
        return KpiDelta(
            name=name,
            unit="usd",
            latest_value=latest,
            prior_value=10.0,
            latest_period="2026-03-31",
            prior_period="2025-12-31",
            tone="neutral",
        )

    deltas = [
        mk("Neutral A", 20.0),
        mk("Neutral B", 19.0),
        mk("Neutral C", 18.0),
        mk("Breached tiny", 10.1),
    ]
    out = _toned(deltas, {"Breached tiny": "breach"})
    assert len(out) == 3
    assert out[0].name == "Breached tiny"
    assert out[0].tone == "bad"
    assert out[0].tone_why == "break rule 'Breached tiny': breach"
    assert [d.name for d in out[1:]] == ["Neutral A", "Neutral B"]


def test_tier1_future_period_facts_excluded(conn: sqlite3.Connection) -> None:
    """A forward-dated (guidance/forecast) fact must not masquerade as the latest
    actual: the ``as_of`` guard drops period_ends in the future so the move is
    computed from real disclosures only."""
    arpac_id = int(
        conn.execute(
            "SELECT id FROM kpi_definitions WHERE ticker='NU' AND name='Monthly ARPAC (USD)'"
        ).fetchone()[0]
    )
    doc_id = int(conn.execute("SELECT id FROM documents ORDER BY id").fetchone()[0])
    future = (NOW + timedelta(days=400)).date().isoformat()
    conn.execute(
        "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, "
        "value, unit, source_doc_id) VALUES ('NU', ?, 'Q1', ?, 88.0, 'usd', ?)",
        (f"{future} 00:00:00", arpac_id, doc_id),
    )
    conn.commit()
    # No guard → the forward-dated row wins (proves it is present and would skew).
    ungated = {d.name: d for d in _tier1_kpi_deltas(conn, {"NU"})["NU"]}
    assert ungated["Monthly ARPAC (USD)"].latest_value == 88.0
    # With as_of=today → the future row is filtered; latest reverts to the Q1 actual.
    gated = {d.name: d for d in _tier1_kpi_deltas(conn, {"NU"}, as_of=NOW.date())["NU"]}
    assert gated["Monthly ARPAC (USD)"].latest_value == 12.4


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
    # expected_earnings is empty in this fixture -> the FMP-cache file fallback.
    assert nu.next_earnings == (NOW + timedelta(days=30)).date().isoformat()  # earliest future
    assert nu.pending_alerts == 2  # dismissed alert not counted
    assert nu.new_docs == 1  # only the doc fetched after last_built_at
    v = _by_ticker(rows["evaluation"])["V"]
    assert (v.next_earnings, v.pending_alerts, v.new_docs) == (None, 0, 0)


def test_build_next_earnings_prefers_canonical_table(
    conn: sqlite3.Connection, repo_root: Path
) -> None:
    """An expected_earnings row (0082, the canonical calendar) wins over the
    on-disk FMP cache; tickers without a row keep the file fallback."""
    table_date = (NOW + timedelta(days=9)).date().isoformat()  # file says +30d
    conn.execute(
        "INSERT INTO expected_earnings (ticker, expected_date, detected_source) "
        "VALUES ('NU', ?, 'yfinance')",
        (table_date,),
    )
    conn.commit()
    built = build_cockpit_rows(conn, repo_root)
    assert _by_ticker(built["portfolio"])["NU"].next_earnings == table_date
    assert _by_ticker(built["evaluation"])["V"].next_earnings is None


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


def _seed_fact_quarters(
    c: sqlite3.Connection,
    ticker: str,
    periods: list[tuple[str, str, float, float | None, float | None, float | None]],
) -> None:
    """financial_facts rows feeding the metrics/ratios views, one document per
    call: (period_end, fiscal_period_type, revenue, ocf, capex, fcf)."""
    c.execute(
        "INSERT INTO documents (ticker, source_type, doc_type, file_path, sha256, "
        "fetched_at, fetch_status, raw_bytes_size) "
        "VALUES (?, 'fmp', 'fmp_statements', ?, ?, ?, 'ok', 10)",
        (
            ticker,
            f"data/{ticker}_facts.json",
            hashlib.sha256(f"{ticker}:facts".encode()).hexdigest(),
            _iso(NOW),
        ),
    )
    doc_id = int(c.execute("SELECT last_insert_rowid()").fetchone()[0])
    _bind_document_evidence(c, ticker=ticker, document_id=doc_id)
    items = ("revenue", "operating_cash_flow", "capital_expenditure", "free_cash_flow")
    for period_end, fpt, *vals in periods:
        for item, val in zip(items, vals, strict=True):
            if val is None:
                continue
            c.execute(
                "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type, "
                "line_item, value, unit, source_doc_id) VALUES (?, ?, ?, ?, ?, 'actual', ?)",
                (ticker, period_end, fpt, item, val, doc_id),
            )
    c.commit()


def test_eval_fundamentals_ttm_margin_from_quarters(
    conn: sqlite3.Connection, repo_root: Path
) -> None:
    """No TTM facts (the prod shape) -> FCF margin sums the newest four
    quarters; a quarter lacking free_cash_flow derives it from OCF + signed
    capex; the fifth-newest quarter stays out of the window."""
    _seed_fact_quarters(
        conn,
        "V",
        [
            ("2026-03-31 00:00:00", "Q1", 120.0, 30.0, -5.0, 25.0),
            ("2025-12-31 00:00:00", "Q4", 110.0, 25.0, -5.0, None),  # derived: 20
            ("2025-09-30 00:00:00", "Q3", 105.0, 20.0, -5.0, 15.0),
            ("2025-06-30 00:00:00", "Q2", 100.0, 25.0, -5.0, 20.0),
            ("2025-03-31 00:00:00", "Q1", 100.0, None, None, 99.0),  # excluded
        ],
    )
    rev_yoy, margin = _eval_fundamentals(conn)["V"]
    assert rev_yoy == pytest.approx(20.0)
    assert margin == pytest.approx(80.0 / 435.0 * 100.0)
    # …and it lands on the cockpit row end-to-end.
    v = _by_ticker(build_cockpit_rows(conn, repo_root)["evaluation"])["V"]
    assert v.fcf_margin_pct == pytest.approx(80.0 / 435.0 * 100.0)


def test_eval_fundamentals_ttm_guards(conn: sqlite3.Connection) -> None:
    """A hole in the quarter window (endpoints a full year apart) or fewer
    than four FCF-bearing quarters -> no margin rather than a mislabeled one.
    (True half-year reporters take the 2-row fallback instead — see the
    semi-annual tests below.)"""
    _seed_fact_quarters(
        conn,
        "GAPQ",
        [
            ("2026-03-31 00:00:00", "Q1", 120.0, 30.0, -5.0, 25.0),
            ("2025-12-31 00:00:00", "Q4", 110.0, 25.0, -5.0, 20.0),
            ("2025-09-30 00:00:00", "Q3", 105.0, 20.0, -5.0, 15.0),
            # 2025-06-30 missing -> the newest-4 window spans 365 days.
            ("2025-03-31 00:00:00", "Q1", 100.0, 25.0, -5.0, 20.0),
        ],
    )
    _seed_fact_quarters(
        conn,
        "FEWQ",
        [
            ("2026-03-31 00:00:00", "Q1", 120.0, 30.0, -5.0, 25.0),
            ("2025-12-31 00:00:00", "Q4", 110.0, 25.0, -5.0, 20.0),
            ("2025-09-30 00:00:00", "Q3", 105.0, 20.0, -5.0, 15.0),
        ],
    )
    out = _eval_fundamentals(conn)
    assert out["GAPQ"] == (pytest.approx(20.0), None)
    assert out["FEWQ"][1] is None


def test_eval_fundamentals_semi_annual_ttm(conn: sqlite3.Connection) -> None:
    """Half-year reporters (BHP shape: FMP lands semi-annual periods in the
    Q2/Q4 slots, period-ends ~180d apart) fail the 4-row span guard but
    populate from the newest TWO rows; a missing free_cash_flow still derives
    from OCF + signed capex inside the pair."""
    _seed_fact_quarters(
        conn,
        "SEMI",
        [
            ("2025-12-31 00:00:00", "Q2", 120.0, 30.0, -5.0, 25.0),
            ("2025-06-30 00:00:00", "Q4", 110.0, 25.0, -5.0, None),  # derived: 20
            ("2024-12-31 00:00:00", "Q2", 105.0, 20.0, -5.0, 15.0),
            ("2024-06-30 00:00:00", "Q4", 100.0, 25.0, -5.0, 20.0),
        ],
    )
    rev_yoy, margin = _eval_fundamentals(conn)["SEMI"]
    assert rev_yoy == pytest.approx(120.0 / 105.0 * 100.0 - 100.0)
    assert margin == pytest.approx(45.0 / 230.0 * 100.0)


def test_eval_fundamentals_semi_annual_fallback_guards(conn: sqlite3.Connection) -> None:
    """The 2-row fallback demands repeating half-year cadence. A quarterly
    series whose single hole leaves the newest two rows ~180d apart (HOLEQ),
    alternating FCF-less quarters that mimic the spacing but leave a raw row
    between the pair (ALTQ), and a two-row series with no third row to
    corroborate cadence (TWOH) all stay unpopulated."""
    _seed_fact_quarters(
        conn,
        "HOLEQ",
        [
            ("2026-03-31 00:00:00", "Q1", 120.0, 30.0, -5.0, 25.0),
            # 2025-12-31 missing -> newest gap ~182d, but the next is ~92d.
            ("2025-09-30 00:00:00", "Q3", 105.0, 20.0, -5.0, 15.0),
            ("2025-06-30 00:00:00", "Q2", 100.0, 25.0, -5.0, 20.0),
            ("2025-03-31 00:00:00", "Q1", 100.0, 25.0, -5.0, 20.0),
        ],
    )
    _seed_fact_quarters(
        conn,
        "ALTQ",
        [
            ("2026-03-31 00:00:00", "Q1", 120.0, 30.0, -5.0, 25.0),
            ("2025-12-31 00:00:00", "Q4", 110.0, None, None, None),  # revenue only
            ("2025-09-30 00:00:00", "Q3", 105.0, 20.0, -5.0, 15.0),
            ("2025-06-30 00:00:00", "Q2", 100.0, None, None, None),  # revenue only
            ("2025-03-31 00:00:00", "Q1", 100.0, 25.0, -5.0, 20.0),
        ],
    )
    _seed_fact_quarters(
        conn,
        "TWOH",
        [
            ("2025-12-31 00:00:00", "Q2", 120.0, 30.0, -5.0, 25.0),
            ("2025-06-30 00:00:00", "Q4", 110.0, 25.0, -5.0, 20.0),
        ],
    )
    out = _eval_fundamentals(conn)
    assert out["HOLEQ"][1] is None
    assert out["ALTQ"][1] is None
    assert out["TWOH"][1] is None


def test_eval_fundamentals_prefers_ratios_ttm_row(conn: sqlite3.Connection) -> None:
    """A real TTM facts row (margin 25%) outranks the on-the-fly quarterly
    sum (~18.4%) when financial_facts carries one."""
    _seed_fact_quarters(
        conn,
        "TTMQ",
        [
            ("2026-03-31 00:00:00", "TTM", 400.0, None, None, 100.0),
            ("2026-03-31 00:00:00", "Q1", 120.0, 30.0, -5.0, 25.0),
            ("2025-12-31 00:00:00", "Q4", 110.0, 25.0, -5.0, 20.0),
            ("2025-09-30 00:00:00", "Q3", 105.0, 20.0, -5.0, 15.0),
            ("2025-06-30 00:00:00", "Q2", 100.0, 25.0, -5.0, 20.0),
        ],
    )
    assert _eval_fundamentals(conn)["TTMQ"][1] == pytest.approx(25.0)


# --------------------------------------------------------------------------- #
# eval_attractiveness (the next-dollar sort)
# --------------------------------------------------------------------------- #


def _attract(
    dcf: float | None = 0.0,
    growth: float | None = 0.0,
    fcf: float | None = 5.0,
    peg: float | None = 2.0,
) -> tuple[float, str, bool]:
    """Scorer with all-neutral (x1.0) defaults so each test varies one factor."""
    return eval_attractiveness(
        dcf_upside_pct=dcf, rev_yoy_pct=growth, fcf_margin_pct=fcf, peg_ratio=peg
    )


def test_attractiveness_band_edges() -> None:
    """Thresholds are inclusive (>=, best-first); below every band falls to
    the factor floor; PEG bands run lower-better."""
    assert _attract(dcf=50.0)[0] == pytest.approx(1.8)
    assert _attract(dcf=49.9)[0] == pytest.approx(1.5)
    assert _attract(dcf=-10.0)[0] == pytest.approx(1.0)
    assert _attract(dcf=-30.1)[0] == pytest.approx(0.5)
    assert _attract(growth=30.0)[0] == pytest.approx(1.6)
    assert _attract(growth=-10.1)[0] == pytest.approx(0.55)
    assert _attract(fcf=25.0)[0] == pytest.approx(1.3)
    assert _attract(fcf=-1.0)[0] == pytest.approx(0.7)
    assert _attract(peg=1.0)[0] == pytest.approx(1.2)
    assert _attract(peg=3.5)[0] == pytest.approx(0.8)


def test_attractiveness_full_data_why() -> None:
    """Every factor named with its input and multiplier — the math in the why
    string is reproducible by eye (the spec'd chip tooltip)."""
    score, why, partial = eval_attractiveness(
        dcf_upside_pct=32.0, rev_yoy_pct=24.0, fcf_margin_pct=18.0, peg_ratio=1.4
    )
    assert score == pytest.approx(1.5 * 1.4 * 1.15 * 1.0)
    assert not partial
    assert why == (
        "dcf 1.50 (+32.0% upside) x growth 1.40 (+24.0% YoY) "
        f"x fcf 1.15 (18.0% margin) x peg 1.00 (1.4) = {score:.2f}"
    )


def test_attractiveness_missing_factors_sink_not_vanish() -> None:
    """A missing input contributes the x0.85 missing factor (named n/a in the
    why) and flags the row partial; even an all-missing name still scores."""
    score, why, partial = eval_attractiveness(
        dcf_upside_pct=None, rev_yoy_pct=12.0, fcf_margin_pct=None, peg_ratio=None
    )
    assert partial
    assert score == pytest.approx(0.85 * 1.2 * 0.85 * 0.85)
    assert "dcf 0.85 (n/a)" in why
    assert "growth 1.20 (+12.0% YoY)" in why
    floor_score, _, floor_partial = eval_attractiveness(
        dcf_upside_pct=None, rev_yoy_pct=None, fcf_margin_pct=None, peg_ratio=None
    )
    assert floor_partial
    assert floor_score == pytest.approx(0.85**4)


def test_attractiveness_nonpositive_peg_is_missing() -> None:
    """A PEG <= 0 is cache garbage (the §Valuation builder gates on positive
    forward growth), not a cheapness signal."""
    score, why, partial = _attract(peg=-0.5)
    assert partial
    assert "peg 0.85 (n/a)" in why
    assert score == pytest.approx(0.85)


def test_attractiveness_breakdown_factors_and_consistency() -> None:
    """The structured breakdown carries one labeled factor per input — with its
    band multiplier and the formatted input — and its (score, why, partial)
    match the flat eval_attractiveness view exactly."""
    bd = attractiveness_breakdown(
        dcf_upside_pct=32.0, rev_yoy_pct=24.0, fcf_margin_pct=18.0, peg_ratio=None
    )
    by_key = {f.key: f for f in bd.factors}
    assert [f.key for f in bd.factors] == ["dcf", "growth", "fcf", "peg"]
    assert by_key["dcf"].label == "DCF upside"
    assert by_key["dcf"].multiplier == pytest.approx(1.5)
    assert by_key["dcf"].detail == "+32.0% upside"
    assert not by_key["dcf"].missing
    assert by_key["peg"].missing  # None input -> the x0.85 missing factor
    assert by_key["peg"].detail == "n/a"
    assert by_key["peg"].multiplier == pytest.approx(0.85)
    # The flat view is just (score, why, partial) of the same object.
    score, why, partial = eval_attractiveness(
        dcf_upside_pct=32.0, rev_yoy_pct=24.0, fcf_margin_pct=18.0, peg_ratio=None
    )
    assert (bd.score, bd.why, bd.partial) == (score, why, partial)


def test_attractiveness_tone() -> None:
    """The chip + peek share one tone map: hi >= 1.25, lo <= 0.75, else ''."""
    assert attractiveness_tone(1.25) == "hi"
    assert attractiveness_tone(0.75) == "lo"
    assert attractiveness_tone(1.0) == ""


def _seed_eval_name(c: sqlite3.Connection, ticker: str, name: str) -> None:
    c.execute(
        "INSERT INTO tracked_companies (user_id, ticker, name, list_type, instrument_type, "
        "last_built_at) VALUES ('bhanu', ?, ?, 'evaluation', 'equity', NULL)",
        (ticker, name),
    )


def test_build_evaluation_sorted_by_attractiveness(
    conn: sqlite3.Connection, repo_root: Path
) -> None:
    """The evaluation list orders by score descending: a full-data name with
    strong factors leads, a partial name with soft factors sits mid, and the
    fixture's all-missing V sinks to the bottom — present, not dropped. The
    portfolio list keeps its attention sort and never carries a score."""
    _seed_eval_name(conn, "GOODE", "Good Eval Co")
    _seed_eval_name(conn, "MEHE", "Meh Eval Co")
    # GOODE: DCF +50% upside (FV 30 vs the run's own price 20), growth +25%,
    # TTM margin 20%, PEG 0.8 -> 1.8 * 1.4 * 1.15 * 1.2.
    conn.execute(
        "INSERT INTO dcf_runs (ticker, valuation_date, horizon_years, revenue_growths_json, "
        "fcf_margin, wacc, terminal_growth, npv, npv_per_share, live_price, created_at) "
        "VALUES ('GOODE', '2026-06-09', 10, '[]', 0.2, 0.1, 0.025, 1000, 30.0, 20.0, ?)",
        (_iso(NOW - timedelta(days=1)),),
    )
    _seed_fact_quarters(
        conn,
        "GOODE",
        [
            ("2026-03-31 00:00:00", "Q1", 125.0, None, None, 25.0),
            ("2025-12-31 00:00:00", "Q4", 115.0, None, None, 23.0),
            ("2025-09-30 00:00:00", "Q3", 110.0, None, None, 22.0),
            ("2025-06-30 00:00:00", "Q2", 100.0, None, None, 20.0),
            ("2025-03-31 00:00:00", "Q1", 100.0, None, None, None),  # YoY base only
        ],
    )
    (repo_root / "data" / "valuation_basis" / "GOODE.json").write_text(
        json.dumps({"ticker": "GOODE", "peg_ratio": 0.8}), encoding="utf-8"
    )
    # MEHE: no DCF run, growth +5% (x1.0), margin 2% (x0.9), no PEG.
    _seed_fact_quarters(
        conn,
        "MEHE",
        [
            ("2026-03-31 00:00:00", "Q1", 105.0, None, None, 2.1),
            ("2025-12-31 00:00:00", "Q4", 103.0, None, None, 2.06),
            ("2025-09-30 00:00:00", "Q3", 102.0, None, None, 2.04),
            ("2025-06-30 00:00:00", "Q2", 100.0, None, None, 2.0),
            ("2025-03-31 00:00:00", "Q1", 100.0, None, None, None),
        ],
    )
    conn.commit()

    built = build_cockpit_rows(conn, repo_root)
    assert [r.base.ticker for r in built["evaluation"]] == ["GOODE", "MEHE", "V"]
    by = _by_ticker(built["evaluation"])
    goode = by["GOODE"]
    assert goode.attractiveness == pytest.approx(1.8 * 1.4 * 1.15 * 1.2)
    assert not goode.attractiveness_partial
    assert goode.attractiveness_why == (
        "dcf 1.80 (+50.0% upside) x growth 1.40 (+25.0% YoY) "
        "x fcf 1.15 (20.0% margin) x peg 1.20 (0.8) = 3.48"
    )
    mehe = by["MEHE"]
    assert mehe.attractiveness == pytest.approx(0.85 * 1.0 * 0.9 * 0.85)
    assert mehe.attractiveness_partial  # no dcf run, no peg cache
    v = by["V"]  # npv_per_share NULL -> no upside; no fundamentals; no peg
    assert v.attractiveness == pytest.approx(0.85**4)
    assert v.attractiveness_partial
    # Portfolio: attention order (NU warn > AAA ok), scores never computed.
    assert [r.base.ticker for r in built["portfolio"]] == ["NU", "AAA"]
    assert all(r.attractiveness is None for r in built["portfolio"])
    # The strong name renders a hi-tone chip with the full math in its hover
    # (the kit outline-mono chip + ok tone).
    html = render_research_cockpit(built)
    assert "k-chip k-chip-mono k-chip-ok" in html
    assert ">3.48</a>" in html  # the chip is a peek doorway <a>, not a <span>
    assert "x peg 1.20 (0.8) = 3.48" in html
    # …and the chip is a peek doorway: click opens the breakdown, /ticker is
    # the real href, the why stays in the hover title.
    assert "data-peek-url='/api/peek/score?ticker=GOODE'" in html
    assert "href='/ticker/GOODE'" in html


def test_compute_attractiveness_matches_row_and_guards(
    conn: sqlite3.Connection, repo_root: Path
) -> None:
    """The single-ticker breakdown (the Score-peek producer) reads the SAME
    inputs as the cockpit row, so its score and why equal the row's; an
    untracked or empty ticker returns None (the route 404s)."""
    # V (evaluation): NULL npv_per_share -> no upside, no fundamentals, no PEG
    # cache -> every factor missing, score 0.85**4 — exactly the row's value.
    bd = compute_attractiveness(conn, repo_root, "v")  # lowercase -> uppercased
    assert bd is not None
    assert bd.score == pytest.approx(0.85**4)
    assert all(f.missing for f in bd.factors)
    row = _by_ticker(build_cockpit_rows(conn, repo_root)["evaluation"])["V"]
    assert bd.score == pytest.approx(row.attractiveness)
    assert bd.why == row.attractiveness_why
    assert compute_attractiveness(conn, repo_root, "ZZZQ") is None  # untracked
    assert compute_attractiveness(conn, repo_root, "") is None


# --------------------------------------------------------------------------- #
# render_research_cockpit
# --------------------------------------------------------------------------- #


def test_price_cell_rounds_home_quotes_to_whole_dollars() -> None:
    base = DashboardRow("NU", "portfolio", None, None, None, 0, None)
    html = _price_cell(CockpitRow(base=base, price=1234.56))
    assert "$1,235" in html
    assert "$1,234.56" not in html


def test_render_badges_chips_and_pills(rows: dict[str, list[CockpitRow]]) -> None:
    html = render_research_cockpit(rows)
    # Verdict badge with tone + rule summary in the hover (the kit status pill).
    assert "k-pill k-pill-warn" in html
    assert "warn: Monthly ARPAC (USD)" in html
    # KPI chips with tone + the pp/relative split (the kit outline-mono chip).
    assert "+10.7%" in html
    assert "-1.5pp" in html
    assert "k-chip-warn" in html
    # Inbox pills: pending alerts deep-link into the feed; new docs counted.
    assert "/feed?ticker=NU&amp;status=pending" in html or "/feed?ticker=NU&status=pending" in html
    assert "2 alerts" in html
    assert "1 new doc" in html


def test_ticker_cell_uses_ticker_label_with_direct_holding_href(
    rows: dict[str, list[CockpitRow]],
) -> None:
    """.ticker-link collapse (design_language §5): the cockpit's primary
    ticker cell used to be a hand-rolled `<a href='/ticker/<T>'>` — a 302 hop
    (/ticker/<T> redirects to /#holding=<T>) with no hover mini-card. It's
    now ticker_label() in its compact/symbol-only form (the company name
    stays in the <td>'s title, not inline — cockpit column density), a
    direct /#holding=<T> href (skips the redirect), and data-peek-ticker on
    the <td> so the shell's hover card has a target regardless of what's
    nested inside."""
    html = render_research_cockpit(rows)
    assert "<td class='ticker'" in html
    assert "data-peek-ticker='NU'" in html
    assert '<a class="k-tick-sym" href="/#holding=NU">NU</a>' in html
    # Compact form: no inline company-name span (density) — but the full name
    # still rides the <td>'s title (unchanged from before this migration).
    assert "k-tick-name" not in html
    # The old bare-anchor / redirect-hop shape is gone for the primary ticker
    # cell (the score/fit/ΔSR peek chips still legitimately use /ticker/<T> —
    # that assertion lives in test_compute_attractiveness_matches_*).
    assert "<a href='/ticker/NU'>NU</a>" not in html


def test_alert_pill_red_reserved_for_tier1(conn: sqlite3.Connection, repo_root: Path) -> None:
    """Routine pending alerts render an AMBER pill; red is reserved for a
    pending tier-1/decisive alert (owner falsifier breach / registered
    threshold crossing), with the tier-1 count named in the hover — so a red
    pill on Home always means a thesis-decisive alert."""
    html = render_research_cockpit(build_cockpit_rows(conn, repo_root))
    assert "k-pill k-pill-warn cockpit-count" in html  # 2 routine pending alerts
    assert "k-pill k-pill-bad cockpit-count" not in html
    conn.execute(
        "INSERT INTO alerts (user_id, ticker, trigger_kind, fired_at, status, "
        "evidence_json, signature_sha) "
        "VALUES ('bhanu', 'NU', 'decision_condition', ?, 'pending', "
        "'{\"decided_by\": \"owner\"}', 'sig-t1')",
        (_iso(NOW - timedelta(hours=1)),),
    )
    conn.commit()
    html2 = render_research_cockpit(build_cockpit_rows(conn, repo_root))
    assert "k-pill k-pill-bad cockpit-count" in html2
    assert "3 alerts" in html2
    assert "1 tier-1" in html2  # the hover names the decisive subset


def test_render_valuation_cells(rows: dict[str, list[CockpitRow]]) -> None:
    html = render_research_cockpit(rows)
    assert "$12" in html
    assert "$12.29" not in html
    assert "+0.8%" in html  # day move, signed
    assert "-50.0%" in html  # vs-FV gap
    assert "0.5" in html  # PEG
    nu_er = (NOW + timedelta(days=30)).date().isoformat()
    assert fmt_date(nu_er, include_year=False) in html


def _insert_minimal_dcf_run(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    npv_per_share: float | None,
    live_price: float | None,
    created_at: str,
    is_latest: int = 1,
    segment_name: str | None = None,
    sanity_flag: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO dcf_runs (ticker, valuation_date, horizon_years, revenue_growths_json, "
        "fcf_margin, wacc, terminal_growth, npv, npv_per_share, live_price, created_at, "
        "is_latest, segment_name, sanity_flag) VALUES (?, '2026-06-08', 10, '[]', 0.2, 0.1, "
        "0.025, 1000, ?, ?, ?, ?, ?, ?)",
        (ticker, npv_per_share, live_price, created_at, is_latest, segment_name, sanity_flag),
    )
    conn.commit()


def test_latest_dcf_runs_ignores_segment_row_even_when_newer(
    conn: sqlite3.Connection,
) -> None:
    """PART A: a segment row (segment_name set) landed AFTER the consolidated
    row must not win 'the' DCF for a ticker — the shared dcf.latest reader
    fix. Previously ``latest_dcf_runs`` had NO is_latest/segment_name
    predicate at all."""
    _insert_minimal_dcf_run(
        conn,
        ticker="SEG",
        npv_per_share=120.0,
        live_price=100.0,
        created_at=_iso(NOW - timedelta(days=1)),
    )
    _insert_minimal_dcf_run(
        conn,
        ticker="SEG",
        npv_per_share=999.0,
        live_price=100.0,
        created_at=_iso(NOW),  # newer than the consolidated row
        segment_name="Consumer",
    )
    gap, fv, px, _date = latest_dcf_runs(conn)["SEG"]
    assert fv == 120.0
    assert px == 100.0
    assert gap == pytest.approx((100.0 / 120.0 - 1.0) * 100.0)


def test_latest_dcf_runs_nulls_gap_on_sanity_flag(conn: sqlite3.Connection) -> None:
    """The existing sanity_flag behavior (research_cockpit already nulls the
    gap — kept unchanged by this port): values stay visible, gap is None."""
    _insert_minimal_dcf_run(
        conn,
        ticker="OUT",
        npv_per_share=60.0,
        live_price=100.0,
        created_at=_iso(NOW),
        sanity_flag="outlier",
    )
    gap, fv, px, _date = latest_dcf_runs(conn)["OUT"]
    assert gap is None
    assert fv == 60.0
    assert px == 100.0


def test_dcf_sanity_flags_ignores_newer_segment_row(conn: sqlite3.Connection) -> None:
    _insert_minimal_dcf_run(
        conn,
        ticker="FLAGSEG",
        npv_per_share=60.0,
        live_price=100.0,
        created_at=_iso(NOW - timedelta(days=1)),
        sanity_flag="outlier",
    )
    _insert_minimal_dcf_run(
        conn,
        ticker="FLAGSEG",
        npv_per_share=80.0,
        live_price=100.0,
        created_at=_iso(NOW),
        segment_name="Consumer",
    )

    assert "FLAGSEG" in dcf_sanity_flags(conn)


def test_render_staleness_dots(rows: dict[str, list[CockpitRow]]) -> None:
    html = render_research_cockpit(rows)
    assert "k-dot-ok" in html  # NU: FMP 1h ago, build 2d ago
    assert "k-dot-bad" in html  # AAA / V: never pulled, never built
    assert "transcript 2026-03-31 (Q&amp;A)" in html  # detail lives in the hover


def test_render_thin_evaluation_variant(rows: dict[str, list[CockpitRow]]) -> None:
    html = render_research_cockpit(rows)
    assert "cockpit-thin" in html
    # The thin table drops the KPI-moves column; the full table keeps it.
    assert html.count("Tier-1 moves") == 1


def test_render_eval_score_column(rows: dict[str, list[CockpitRow]]) -> None:
    """The Score column exists only in the thin/evaluation table; V (every
    factor missing) renders a low-tone dashed partial chip whose hover names
    all four n/a factors."""
    html = render_research_cockpit(rows)
    # The header is now a living-grid sortable <th> (label + sort indicator span),
    # so the column reads ">Score<span …" rather than ">Score</th>".
    assert html.count(">Score<") == 1
    assert "sortBy('score','num')" in html  # the column is a living-grid sortable header
    # The Fit column sits beside Score (thin table only); with no candidate_fit
    # cache the fixture's V carries no fit, so its cell is the muted em-dash.
    assert html.count(">Fit<") == 1
    assert "k-chip k-chip-mono chip-partial" in html
    assert ">0.52</a>" in html
    assert "dcf 0.85 (n/a) x growth 0.85 (n/a) x fcf 0.85 (n/a) x peg 0.85 (n/a) = 0.52" in html
    assert "/api/peek/fit" not in html  # no cache → no fit chip (only the CSS class is present)


def test_build_attaches_fit_from_cache(conn: sqlite3.Connection, repo_root: Path) -> None:
    """An evaluation row picks up its portfolio-fit scalars from the
    materialized candidate_fit.json; the chip then renders as a peek doorway."""
    (repo_root / "data" / "candidate_fit.json").write_text(
        json.dumps(
            {
                "computed_at": "2026-06-14T04:00:00",
                "fits": {
                    "V": {
                        "fit": 1.15,
                        "why": "sharpe 1.12 (...) x divers 1.03 (...) = 1.15",
                        "partial": False,
                        "obs": 200,
                        "factors": [
                            {
                                "key": "sharpe",
                                "label": "Marginal Sharpe",
                                "multiplier": 1.12,
                                "detail": "SR ...",
                                "missing": False,
                            },
                            {
                                "key": "divers",
                                "label": "Diversification",
                                "multiplier": 1.03,
                                "detail": "corr ...",
                                "missing": False,
                            },
                        ],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    v = _by_ticker(build_cockpit_rows(conn, repo_root)["evaluation"])["V"]
    assert v.fit == pytest.approx(1.15)
    assert not v.fit_partial
    assert "sharpe 1.12" in (v.fit_why or "")
    # Portfolio rows never carry a fit (the cache is evaluation-only).
    assert all(r.fit is None for r in build_cockpit_rows(conn, repo_root)["portfolio"])
    # And it renders the fit chip as a /api/peek/fit doorway (the kit ok chip).
    html = render_research_cockpit(build_cockpit_rows(conn, repo_root))
    assert "k-chip k-chip-mono k-chip-ok" in html  # 1.15 >= 1.10
    assert "data-peek-url='/api/peek/fit?ticker=V'" in html
    assert ">1.15</a>" in html


def _write_fit_cache_v2(
    repo_root: Path,
    *,
    target_source: str = "intent",
    degraded: list[str] | None = None,
) -> None:
    (repo_root / "data").mkdir(parents=True, exist_ok=True)
    (repo_root / "data" / "candidate_fit.json").write_text(
        json.dumps(
            {
                "version": 2,
                "computed_at": "2026-07-10T04:00:00",
                "book": {
                    "sharpe": 0.9,
                    "growth_tilt": 0.25,
                    "risk_free_annual": 0.045,
                    "degraded": degraded or [],
                },
                "target": {
                    "source": target_source,
                    "intent_id": 3 if target_source == "intent" else None,
                    "narrative": "less growth, more intl value",
                },
                "fits": {
                    "V": {
                        "fit": 1.15,
                        "why": "sharpe 1.12 (...) x divers 1.03 (...) = 1.15",
                        "partial": False,
                        "obs": 200,
                        "factors": [
                            {
                                "key": "sharpe",
                                "label": "Marginal Sharpe",
                                "multiplier": 1.12,
                                "detail": "SR ...",
                                "missing": False,
                            }
                        ],
                        "fit_target": 1.29,
                        "target_factors": [
                            {
                                "key": "tgt_tilt",
                                "label": "Target tilt",
                                "multiplier": 1.12,
                                "detail": "closes the tilt gap",
                                "missing": False,
                            }
                        ],
                        "sharpe_delta_bps": 12.4,
                        "corr_trend": "rising",
                        "corr_recent": 0.81,
                        "degraded": degraded or [],
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def test_render_fit_v2_target_chip_and_dsr_column(
    conn: sqlite3.Connection, repo_root: Path
) -> None:
    """With an ACTIVE intent target the Fit chip shows fit-to-target with the
    tgt marker; the ΔSR column renders as a what-if peek doorway and sorts."""
    _write_fit_cache_v2(repo_root, target_source="intent")
    html = render_research_cockpit(build_cockpit_rows(conn, repo_root))
    assert ">1.29<sup>tgt</sup></a>" in html  # fit-to-target shown, marked
    assert html.count(">ΔSR<") == 1
    assert "sortBy('dsr','num')" in html
    assert "data-peek-url='/api/peek/whatif?ticker=V'" in html
    assert ">+12bp</a>" in html
    assert "Fit computed with degraded book context" not in html  # clean book → no banner


def test_render_fit_v2_book_default_is_v1_identical(
    conn: sqlite3.Connection, repo_root: Path
) -> None:
    """Under the book-default target (no saved intent) the chip is the plain
    fit number — no tgt marker, no behavior change vs v1."""
    _write_fit_cache_v2(repo_root, target_source="book_default")
    html = render_research_cockpit(build_cockpit_rows(conn, repo_root))
    assert ">1.29<" not in html  # fit_target hidden under the default
    assert ">1.15</a>" in html
    assert "<sup>tgt</sup>" not in html


def test_render_fit_v2_degraded_is_loud(conn: sqlite3.Connection, repo_root: Path) -> None:
    """A degraded book context renders the warn chip with the ! glyph, the
    reasons in the hover, and the one-line banner above the table."""
    reasons = ["tracker offline and no risk snapshot — book Sharpe unknown"]
    _write_fit_cache_v2(repo_root, target_source="intent", degraded=reasons)
    html = render_research_cockpit(build_cockpit_rows(conn, repo_root))
    assert "cockpit-degraded" in html
    assert "Fit computed with degraded book context" in html
    assert "BOOK CONTEXT DEGRADED" in html
    assert ">! 1.29<sup>tgt</sup></a>" in html


def test_render_escapes_company_name(rows: dict[str, list[CockpitRow]]) -> None:
    html = render_research_cockpit(rows)
    assert "Nu &amp; Co Holdings" in html


def test_render_empty_lists() -> None:
    html = render_research_cockpit({"portfolio": [], "evaluation": []})
    assert "No portfolio tickers." in html
    assert "No evaluation tickers." in html
