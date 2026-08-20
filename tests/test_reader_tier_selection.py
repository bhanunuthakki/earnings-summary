"""Guard: every fact reader materializes the (source_quality_tier, id) winner.

The EDGAR backfill left ~162k (ticker, period_end, fiscal_period_type,
line_item) keys with BOTH an FMP and a SEC row. The canonical contract
(timeseries.loaders.load_financial_series) resolves the duplicate by tier — SEC
beats FMP — with id only as the within-tier tiebreak. Four readers used to
bypass it (source_doc_id DESC / insertion order); this test seeds the worst case
(the FMP row has the HIGHER id, so an id-only pick would wrongly choose it) and
asserts each reader surfaces the SEC value + provenance instead.

It also exercises the sample-based audit (pipeline.reader_tier_audit) and the
source-disagreement reconciler end-to-end on the same fixture.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

# (period_end, fiscal_period_type, line_item, value, source_doc_id, source_type,
#  source_quality_tier)
_FactSeed = tuple[str, str, str, int, int, str, str]

_DDL = """
CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    source_type TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    file_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    fetched_at TIMESTAMP NOT NULL,
    fetch_status TEXT NOT NULL,
    raw_bytes_size INTEGER NOT NULL DEFAULT 0,
    source_url TEXT,
    source_quality_tier TEXT NOT NULL DEFAULT 'fmp_normalized',
    accession_number TEXT,
    filing_date TEXT
);
CREATE TABLE financial_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    period_end TIMESTAMP NOT NULL,
    fiscal_period_type TEXT NOT NULL,
    line_item TEXT NOT NULL,
    value TEXT NOT NULL,
    currency TEXT DEFAULT 'USD',
    unit TEXT NOT NULL DEFAULT 'actual',
    source_doc_id INTEGER NOT NULL,
    locator TEXT,
    confidence REAL DEFAULT 1.0
);
CREATE TABLE kpi_definitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, name TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT 'actual'
);
CREATE TABLE kpi_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL,
    period_end TIMESTAMP NOT NULL, fiscal_period_type TEXT NOT NULL,
    kpi_definition_id INTEGER NOT NULL, value TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT 'actual', source_doc_id INTEGER NOT NULL
);
CREATE TABLE validation_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
    source_doc_id INTEGER, ticker TEXT, severity TEXT NOT NULL, rule TEXT NOT NULL,
    raw_value TEXT, expected TEXT, raised_at TIMESTAMP NOT NULL,
    resolved_at TIMESTAMP, resolved_by TEXT, resolution_note TEXT
);
"""

# A quarterly key and an annual key, each duplicated FMP-vs-SEC. In BOTH cases
# the FMP row is inserted LAST (higher id), so a source-agnostic id/source_doc_id
# pick would choose FMP. The SEC value is the one every reader must surface.
_SEC_Q_REVENUE = 100_000
_FMP_Q_REVENUE = 90_000
_SEC_FY_REVENUE = 400_000
_FMP_FY_REVENUE = 360_000

_SEEDS: list[_FactSeed] = [
    # doc 1 = SEC (lower id), doc 2 = FMP (higher id) — for the quarterly key
    ("2026-03-31", "Q1", "revenue", _SEC_Q_REVENUE, 1, "sec_xbrl", "sec_official"),
    ("2026-03-31", "Q1", "revenue", _FMP_Q_REVENUE, 2, "fmp", "fmp_normalized"),
    # annual key (period_end also has a Q4 dual-write to prove FY/Q4 don't mix)
    ("2025-12-31", "FY", "revenue", _SEC_FY_REVENUE, 1, "sec_xbrl", "sec_official"),
    ("2025-12-31", "FY", "revenue", _FMP_FY_REVENUE, 2, "fmp", "fmp_normalized"),
    # the SEC FY/Q4 dual-write: same period_end, Q4 label, DIFFERENT value —
    # the annual axis must NOT pull this, the quarterly axis must NOT pull FY.
    ("2025-12-31", "Q4", "revenue", 111_111, 1, "sec_xbrl", "sec_official"),
]


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[Path]:
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(db))
    conn.executescript(_DDL)
    # Two documents: id 1 = SEC (lower), id 2 = FMP (higher).
    conn.execute(
        "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, sha256, "
        "fetched_at, fetch_status, source_quality_tier) VALUES "
        "(1, 'TST', 'sec_xbrl', 'sec_companyfacts', 'sec.json', 'a', '2026-04-01', 'ok', "
        "'sec_official')"
    )
    conn.execute(
        "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, sha256, "
        "fetched_at, fetch_status, source_quality_tier) VALUES "
        "(2, 'TST', 'fmp', 'fmp_income_statement', "
        "'data/historical/fmp/TST_income_statement_quarterly.json', 'b', '2026-04-02', 'ok', "
        "'fmp_normalized')"
    )
    for pe, fpt, li, val, doc, _st, _tier in _SEEDS:
        conn.execute(
            "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type, line_item, "
            "value, source_doc_id) VALUES ('TST', ?, ?, ?, ?, ?)",
            (pe, fpt, li, str(val), doc),
        )
    conn.commit()
    conn.close()
    yield db.parent.parent  # repo_root


def _conn(repo: Path) -> sqlite3.Connection:
    c = sqlite3.connect(str(repo / "data" / "portfolio.db"))
    c.row_factory = sqlite3.Row
    return c


# ---------------------------------------------------------------------------
# The canonical contract — the reference every reader must match.
# ---------------------------------------------------------------------------


def test_canonical_loader_picks_sec_despite_lower_id(repo: Path) -> None:
    from timeseries.loaders import load_financial_series

    db = repo / "data" / "portfolio.db"
    q = {
        str(o.period_end)[:10]: o.value for o in load_financial_series("TST", "revenue", db_path=db)
    }
    assert q["2026-03-31"] == _SEC_Q_REVENUE  # SEC, not the higher-id FMP row
    a = {
        str(o.period_end)[:10]: o.value
        for o in load_financial_series("TST", "revenue", db_path=db, period_types=("FY", "annual"))
    }
    assert a["2025-12-31"] == _SEC_FY_REVENUE


# ---------------------------------------------------------------------------
# Reader 1 — cockpit_fundamentals
# ---------------------------------------------------------------------------


def test_cockpit_fundamentals_uses_tier_winner(repo: Path) -> None:
    from cockpit_fundamentals import compute_from_db

    conn = _conn(repo)
    try:
        # rev_yoy needs a prior-year quarter; seed one SEC row a year earlier so
        # the YoY is computable and anchored on the SEC value.
        conn.execute(
            "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type, line_item, "
            "value, source_doc_id) VALUES ('TST', '2025-03-31', 'Q1', 'revenue', '80000', 1)"
        )
        conn.commit()
        out = compute_from_db(conn)
        rev_yoy, _margin = out["TST"]
        # SEC latest = 100000 vs prior 80000 → +25%. An FMP pick (90000) → +12.5%.
        assert rev_yoy == pytest.approx(25.0)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Reader 3 — report/sections/financials
# ---------------------------------------------------------------------------


def test_financials_quarterly_uses_tier_winner(repo: Path) -> None:
    import report.sections.financials as fin_section

    conn = _conn(repo)
    try:
        rows = fin_section._load_quarterly(conn, "TST")  # pyright: ignore[reportPrivateUsage]
        q = {str(r["period_end"])[:10]: r["revenue"] for r in rows}
        assert float(cast("float", q["2026-03-31"])) == _SEC_Q_REVENUE
    finally:
        conn.close()


def test_financials_annual_uses_tier_winner_and_ignores_q4_dualwrite(repo: Path) -> None:
    import report.sections.financials as fin_section

    conn = _conn(repo)
    try:
        rows = fin_section._load_annual(conn, "TST")  # pyright: ignore[reportPrivateUsage]
        a = {str(r["period_end"])[:10]: r["revenue"] for r in rows}
        # FY axis picks the SEC FY row, NOT the FMP FY row and NOT the Q4 dual-write.
        assert float(cast("float", a["2025-12-31"])) == _SEC_FY_REVENUE
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Reader 4 — ask.grounding
# ---------------------------------------------------------------------------


def test_grounding_fin_item_surfaces_sec_provenance(repo: Path) -> None:
    import ask.grounding as grounding

    conn = _conn(repo)
    try:
        item = grounding._fact_ref_fin_item(  # pyright: ignore[reportPrivateUsage]
            conn, "TST", "revenue", "Q1"
        )
        assert item is not None
        # The winning row's document must be the SEC doc (id 1), not the FMP doc.
        assert item["doc_id"] == 1
        src = conn.execute(
            "SELECT source_type FROM documents WHERE id = ?", (item["doc_id"],)
        ).fetchone()
        assert src["source_type"] == "sec_xbrl"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Reader 2 — fmp_derived_kpis (per-cell tier-winner)
# ---------------------------------------------------------------------------


def test_fmp_derived_fetch_is_deterministic_within_fmp_scope(repo: Path) -> None:
    """``_fetch_quarterly_facts`` is doc_type-scoped to the FMP statement docs, so
    it never sees SEC companyfacts rows. Within that scope the tier-aware order
    makes a same-tier collision (two FMP docs reporting one cell) resolve to the
    HIGHER-id (later-ingested) row deterministically — the prior period_end-only
    order left it arbitrary. Seed two FMP docs for one Q1 cell set; the higher-id
    doc's values must win every cell and anchor the provenance."""
    import compute.fmp_derived_kpis as fmp_derived

    conn = _conn(repo)
    try:
        # A SECOND FMP document (id 3, higher than doc 2) — a re-fetch that
        # restated the quarter. All four required line items under BOTH FMP docs.
        conn.execute(
            "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, sha256, "
            "fetched_at, fetch_status, source_quality_tier) VALUES "
            "(3, 'TST', 'fmp', 'fmp_income_statement', "
            "'data/historical/fmp/TST_income_statement_quarterly.json', 'c', '2026-05-01', 'ok', "
            "'fmp_normalized')"
        )
        # doc 2 = older FMP (lower id), doc 3 = newer FMP (higher id, should win).
        for li, old_v, new_v in [
            ("revenue", 90_000, 95_000),
            ("operating_income", 18000, 19000),
            ("net_income", 13000, 14000),
            ("gross_profit", 36000, 37000),
        ]:
            conn.execute(
                "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type, line_item, "
                "value, source_doc_id) VALUES ('TST', '2026-03-31', 'Q1', ?, ?, 2)",
                (li, str(old_v)),
            )
            conn.execute(
                "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type, line_item, "
                "value, source_doc_id) VALUES ('TST', '2026-03-31', 'Q1', ?, ?, 3)",
                (li, str(new_v)),
            )
        conn.commit()
        facts, _degradations = fmp_derived._fetch_quarterly_facts(  # pyright: ignore[reportPrivateUsage]
            conn, "TST"
        )
        q1 = [f for f in facts if str(f.period_end)[:10] == "2026-03-31"]
        assert len(q1) == 1
        f = q1[0]
        # The higher-id FMP doc (3) wins every cell + anchors provenance.
        assert int(f.revenue) == 95_000
        assert int(f.operating_income) == 19000
        assert int(f.net_income) == 14000
        assert int(f.gross_profit) == 37000
        assert f.source_doc_id == 3
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The audit + reconciler over the same fixture
# ---------------------------------------------------------------------------


def test_audit_reports_no_mismatch_when_readers_agree(repo: Path) -> None:
    from pipeline.reader_tier_audit import audit_readers

    conn = _conn(repo)
    try:
        res = audit_readers(
            conn, run_id="t", db_path=repo / "data" / "portfolio.db", limit=50, dry_run=True
        )
        assert res.keys_examined >= 1
        assert res.mismatches == 0
        assert res.issues_written == 0
    finally:
        conn.close()


def test_reconciler_auto_resolves_small_delta_in_sec_favor(repo: Path) -> None:
    from pipeline.reader_tier_audit import (
        open_source_disagreement_count,
        reconcile_source_disagreements,
    )

    conn = _conn(repo)
    try:
        # One near-agreement (0.50%) with SEC involved → auto-resolve; one large
        # (10%) → left open; one non-SEC pair at 0.10% → left open (SEC not named).
        rows = [
            ("revenue @ 2025-06-30: sec_xbrl=1000 vs fmp=1005 (0.50%)", None),
            ("net_income @ 2025-06-30: sec_xbrl=100 vs fmp=110 (10.00%)", None),
            ("revenue @ 2025-06-30: ir_doc=1000 vs fmp=1001 (0.10%)", None),
        ]
        for raw, _ in rows:
            conn.execute(
                "INSERT INTO validation_issues (run_id, ticker, severity, rule, raw_value, "
                "expected, raised_at) VALUES ('r', 'TST', 'warn', 'source_disagreement', ?, "
                "'agreement within 0.5%', '2026-06-01')",
                (raw,),
            )
        conn.commit()

        res = reconcile_source_disagreements(conn, threshold_pct=1.0)
        assert res.examined == 3
        assert res.auto_resolved == 1
        assert res.left_open == 2
        assert open_source_disagreement_count(conn) == 2

        resolved = conn.execute(
            "SELECT resolution_note FROM validation_issues WHERE resolved_at IS NOT NULL"
        ).fetchall()
        assert len(resolved) == 1
        assert "SEC's favor" in resolved[0]["resolution_note"]
    finally:
        conn.close()
