"""Tests for the self-calibrating credibility engine (Close-the-Loops L10).

Covers the measurement loop end-to-end on a synthetic schema:

* ``credibility.observations`` — forward capture grading a superseded fact,
  the immaterial-vs-material outcome rule, the historical backfill (restatement
  + resolved-disagreement), and idempotence.
* ``credibility.calibration`` — reliability buckets, per-tier rows, the Brier
  score, the minimum-n gate, and the empty / absent-table degrade paths.
* ``pipeline.credibility_panel`` — the Provenance-console fragment renders for
  absent / empty / populated ledgers.
* The four insert call sites actually capture (integration through
  ``compute._common.insert_financial_facts``).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from credibility.calibration import build_reliability_table
from credibility.observations import (
    FINANCIAL_FACTS,
    backfill_disagreement_observations,
    backfill_restatement_observations,
    record_restatement_observation,
    restatement_outcome,
)
from models.documents import SourceQualityTier
from models.facts import Currency, FinancialFact, FiscalPeriodType, LegacyEscapeHatch, Unit
from pipeline.credibility_panel import render_credibility_panel

# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


def _make_schema(db_path: Path) -> None:
    """documents + financial_facts + kpi_facts + kpi_definitions +
    validation_issues + confidence_observations (the 0106 shape)."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                source_type TEXT NOT NULL,
                doc_type TEXT NOT NULL,
                period_end TEXT,
                file_path TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                source_quality_tier TEXT NOT NULL DEFAULT 'fmp_normalized'
            );
            CREATE TABLE financial_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                period_end TEXT NOT NULL,
                fiscal_period_type TEXT NOT NULL,
                line_item TEXT NOT NULL,
                value NUMERIC(24,6) NOT NULL,
                currency TEXT,
                unit TEXT NOT NULL,
                source_doc_id INTEGER NOT NULL,
                confidence FLOAT NOT NULL DEFAULT 1.0,
                extracted_by TEXT,
                supersedes_id INTEGER
            );
            CREATE UNIQUE INDEX uq_ff_provenance
              ON financial_facts (ticker, period_end, fiscal_period_type, line_item, source_doc_id);
            CREATE TABLE kpi_definitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                name TEXT NOT NULL,
                unit TEXT NOT NULL,
                primary_source TEXT NOT NULL,
                UNIQUE(ticker, name)
            );
            CREATE TABLE kpi_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                period_end TEXT NOT NULL,
                fiscal_period_type TEXT NOT NULL,
                kpi_definition_id INTEGER NOT NULL,
                value NUMERIC(24,6) NOT NULL,
                unit TEXT NOT NULL,
                source_doc_id INTEGER NOT NULL,
                confidence FLOAT NOT NULL DEFAULT 1.0,
                extracted_by TEXT,
                supersedes_id INTEGER
            );
            CREATE UNIQUE INDEX uq_kf_provenance
              ON kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, source_doc_id);
            CREATE TABLE validation_issues (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT,
                rule TEXT NOT NULL,
                severity TEXT NOT NULL,
                raw_value TEXT,
                resolved_at TEXT
            );
            CREATE TABLE confidence_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL DEFAULT 'bhanu',
                fact_table TEXT NOT NULL,
                fact_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                predicted_confidence FLOAT NOT NULL,
                source_tier TEXT,
                line_item TEXT,
                ticker TEXT,
                period_end TEXT,
                old_value FLOAT,
                new_value FLOAT,
                delta_pct FLOAT,
                outcome INTEGER NOT NULL,
                observed_at TEXT NOT NULL,
                CONSTRAINT uq_confidence_obs UNIQUE (fact_table, fact_id, kind)
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def _doc(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    period_end: str,
    fetched_at: str,
    tier: str,
    source_type: str = "fmp",
) -> int:
    cur = conn.execute(
        "INSERT INTO documents (ticker, source_type, doc_type, period_end, file_path, "
        "fetched_at, source_quality_tier) VALUES (?, ?, '10k', ?, '/x.json', ?, ?)",
        (ticker, source_type, period_end, fetched_at, tier),
    )
    return int(cur.lastrowid or 0)


def _ff(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    period_end: str,
    line_item: str,
    value: str,
    doc_id: int,
    confidence: float,
) -> int:
    cur = conn.execute(
        "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type, line_item, "
        "value, unit, source_doc_id, confidence, extracted_by) "
        "VALUES (?, ?, 'Q1', ?, ?, 'actual', ?, ?, 'fmp')",
        (ticker, period_end, line_item, value, doc_id, confidence),
    )
    return int(cur.lastrowid or 0)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "portfolio.db"
    _make_schema(p)
    return p


# ---------------------------------------------------------------------------
# Outcome rule (pure)
# ---------------------------------------------------------------------------


def test_restatement_outcome_rule() -> None:
    assert restatement_outcome(100.0, 100.0) == 1  # exact hold
    assert restatement_outcome(100.0, 100.4) == 1  # within 0.5% → held
    assert restatement_outcome(100.0, 90.0) == 0  # 10% restatement → wrong
    assert restatement_outcome(0.0, 0.0) == 1  # both zero → held
    assert restatement_outcome(0.0, 5.0) == 0  # appeared from nothing → wrong
    assert restatement_outcome(100.0, None) == 1  # no replacement value → assume held


# ---------------------------------------------------------------------------
# Forward capture
# ---------------------------------------------------------------------------


def test_record_restatement_grades_old_fact(db: Path) -> None:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        doc1 = _doc(
            conn,
            ticker="AAPL",
            period_end="2023-03-31",
            fetched_at="2023-04-15",
            tier="fmp_normalized",
        )
        old_id = _ff(
            conn,
            ticker="AAPL",
            period_end="2023-03-31",
            line_item="revenue",
            value="100",
            doc_id=doc1,
            confidence=0.94,
        )
        conn.commit()

        obs_id = record_restatement_observation(
            conn, fact_table=FINANCIAL_FACTS, superseded_id=old_id, new_value=Decimal("90")
        )
        conn.commit()
        assert obs_id is not None
        row = conn.execute(
            "SELECT * FROM confidence_observations WHERE id = ?", (obs_id,)
        ).fetchone()
        assert row["kind"] == "restatement"
        assert row["fact_id"] == old_id
        assert row["predicted_confidence"] == pytest.approx(0.94)
        assert row["source_tier"] == "fmp_normalized"
        assert row["old_value"] == pytest.approx(100.0)
        assert row["new_value"] == pytest.approx(90.0)
        assert row["delta_pct"] == pytest.approx(10.0)
        assert row["outcome"] == 0
        assert row["line_item"] == "revenue"
    finally:
        conn.close()


def test_record_restatement_immaterial_holds(db: Path) -> None:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        doc1 = _doc(
            conn,
            ticker="AAPL",
            period_end="2023-03-31",
            fetched_at="2023-04-15",
            tier="sec_official",
        )
        old_id = _ff(
            conn,
            ticker="AAPL",
            period_end="2023-03-31",
            line_item="revenue",
            value="100",
            doc_id=doc1,
            confidence=0.98,
        )
        conn.commit()
        obs_id = record_restatement_observation(
            conn, fact_table=FINANCIAL_FACTS, superseded_id=old_id, new_value=Decimal("100.2")
        )
        conn.commit()
        assert obs_id is not None
        outcome = conn.execute(
            "SELECT outcome FROM confidence_observations WHERE id = ?", (obs_id,)
        ).fetchone()[0]
        assert outcome == 1  # 0.2% move → confirmed
    finally:
        conn.close()


def test_record_restatement_missing_table_is_noop(tmp_path: Path) -> None:
    """No confidence_observations table → returns None, never raises."""
    p = tmp_path / "bare.db"
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(
            "CREATE TABLE financial_facts (id INTEGER PRIMARY KEY, ticker TEXT, "
            "period_end TEXT, line_item TEXT, value NUMERIC, confidence FLOAT, source_doc_id INT);"
        )
        conn.execute(
            "INSERT INTO financial_facts (id, ticker, period_end, line_item, value, confidence, "
            "source_doc_id) VALUES (1, 'X', '2023-03-31', 'revenue', 100, 0.9, 1)"
        )
        conn.commit()
        assert (
            record_restatement_observation(
                conn, fact_table=FINANCIAL_FACTS, superseded_id=1, new_value=Decimal("90")
            )
            is None
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------


def test_backfill_restatement_from_chains_idempotent(db: Path) -> None:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        doc1 = _doc(
            conn,
            ticker="NU",
            period_end="2023-03-31",
            fetched_at="2023-04-15",
            tier="fmp_normalized",
        )
        doc2 = _doc(
            conn, ticker="NU", period_end="2023-12-31", fetched_at="2024-02-15", tier="sec_official"
        )
        old_id = _ff(
            conn,
            ticker="NU",
            period_end="2023-03-31",
            line_item="revenue",
            value="100",
            doc_id=doc1,
            confidence=0.94,
        )
        # the superseding row links back via supersedes_id
        conn.execute(
            "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type, line_item, "
            "value, unit, source_doc_id, confidence, extracted_by, supersedes_id) "
            "VALUES ('NU', '2023-03-31', 'Q1', 'revenue', '80', 'actual', ?, 0.98, 'sec_xbrl', ?)",
            (doc2, old_id),
        )
        conn.commit()

        n = backfill_restatement_observations(conn)
        conn.commit()
        assert n == 1
        row = conn.execute("SELECT * FROM confidence_observations").fetchone()
        assert row["fact_id"] == old_id
        assert row["outcome"] == 0  # 100 -> 80 is material
        assert row["new_value"] == pytest.approx(80.0)

        # Re-run inserts nothing (UNIQUE).
        assert backfill_restatement_observations(conn) == 0
    finally:
        conn.close()


def test_backfill_disagreement_grades_lower_tier(db: Path) -> None:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        sec_doc = _doc(
            conn,
            ticker="META",
            period_end="2023-03-31",
            fetched_at="2023-04-15",
            tier="sec_official",
            source_type="sec_xbrl",
        )
        fmp_doc = _doc(
            conn,
            ticker="META",
            period_end="2023-03-31",
            fetched_at="2023-04-10",
            tier="fmp_normalized",
            source_type="fmp",
        )
        _ff(
            conn,
            ticker="META",
            period_end="2023-03-31",
            line_item="revenue",
            value="101",
            doc_id=sec_doc,
            confidence=0.98,
        )
        fmp_id = _ff(
            conn,
            ticker="META",
            period_end="2023-03-31",
            line_item="revenue",
            value="100",
            doc_id=fmp_doc,
            confidence=0.79,
        )
        conn.execute(
            "INSERT INTO validation_issues (ticker, rule, severity, raw_value, resolved_at) "
            "VALUES ('META', 'source_disagreement', 'warn', "
            "'revenue @ 2023-03-31: fmp=100 vs sec_xbrl=101 (0.99%)', '2023-05-01 00:00:00')"
        )
        conn.commit()

        n = backfill_disagreement_observations(conn)
        conn.commit()
        assert n == 1  # only the lower-tier FMP fact is graded
        row = conn.execute("SELECT * FROM confidence_observations").fetchone()
        assert row["kind"] == "disagreement"
        assert row["fact_id"] == fmp_id
        assert row["source_tier"] == "fmp_normalized"
        assert row["outcome"] == 0  # disagreed with the SEC authority
    finally:
        conn.close()


def test_backfill_disagreement_skips_unresolved(db: Path) -> None:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        sec_doc = _doc(
            conn,
            ticker="META",
            period_end="2023-03-31",
            fetched_at="2023-04-15",
            tier="sec_official",
            source_type="sec_xbrl",
        )
        fmp_doc = _doc(
            conn,
            ticker="META",
            period_end="2023-03-31",
            fetched_at="2023-04-10",
            tier="fmp_normalized",
            source_type="fmp",
        )
        _ff(
            conn,
            ticker="META",
            period_end="2023-03-31",
            line_item="revenue",
            value="101",
            doc_id=sec_doc,
            confidence=0.98,
        )
        _ff(
            conn,
            ticker="META",
            period_end="2023-03-31",
            line_item="revenue",
            value="100",
            doc_id=fmp_doc,
            confidence=0.79,
        )
        conn.execute(
            "INSERT INTO validation_issues (ticker, rule, severity, raw_value, resolved_at) "
            "VALUES ('META', 'source_disagreement', 'warn', "
            "'revenue @ 2023-03-31: fmp=100 vs sec_xbrl=101 (0.99%)', NULL)"  # unresolved
        )
        conn.commit()
        assert backfill_disagreement_observations(conn) == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Reliability table + Brier
# ---------------------------------------------------------------------------


def _seed_obs(conn: sqlite3.Connection, predicted: float, outcome: int, tier: str, n: int) -> None:
    for _ in range(n):
        conn.execute(
            "INSERT INTO confidence_observations (fact_table, fact_id, kind, predicted_confidence, "
            "source_tier, outcome, observed_at) VALUES ('financial_facts', "
            "(SELECT COALESCE(MAX(fact_id), 0) + 1 FROM confidence_observations), "
            "'restatement', ?, ?, ?, '2024-01-01')",
            (predicted, tier, outcome),
        )


def test_reliability_table_buckets_and_brier(db: Path) -> None:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        # 0.92 band: 8 held, 2 wrong → observed 80% vs predicted 92% (overconfident)
        _seed_obs(conn, 0.92, 1, "fmp_normalized", 8)
        _seed_obs(conn, 0.92, 0, "fmp_normalized", 2)
        # 0.98 band: 10 held → observed 100% vs 98%
        _seed_obs(conn, 0.98, 1, "sec_official", 10)
        conn.commit()

        table = build_reliability_table(conn)
        assert table is not None
        assert table.total_n == 20
        assert table.restatement_n == 20
        # Brier = mean((p-o)^2): 8*(.08^2)+2*(.92^2)+10*(.02^2) all /20
        expected_brier = (8 * 0.08**2 + 2 * 0.92**2 + 10 * 0.02**2) / 20
        assert table.brier == pytest.approx(round(expected_brier, 4))

        bands = {b.label: b for b in table.buckets}
        assert "90%-100%" in bands
        nine = bands["90%-100%"]
        assert nine.n == 20
        assert nine.observed_rate == pytest.approx(0.9)  # 18 of 20 held

        tiers = {t.tier: t for t in table.tiers}
        assert tiers["fmp_normalized"].observed_rate == pytest.approx(0.8)
        assert tiers["fmp_normalized"].gap > 0  # overconfident
        assert tiers["sec_official"].observed_rate == pytest.approx(1.0)
    finally:
        conn.close()


def test_reliability_min_n_gate(db: Path) -> None:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        _seed_obs(conn, 0.92, 1, "fmp_normalized", 3)  # sparse
        conn.commit()
        table = build_reliability_table(conn)
        assert table is not None
        assert table.total_n == 3
        assert not table.has_confident_cell  # n=3 < MIN_CONFIDENT_N
        assert all(not b.confident for b in table.buckets)

        _seed_obs(conn, 0.92, 1, "fmp_normalized", 9)  # now 12 total
        conn.commit()
        table2 = build_reliability_table(conn)
        assert table2 is not None
        assert table2.has_confident_cell
    finally:
        conn.close()


def test_reliability_none_when_table_absent(tmp_path: Path) -> None:
    p = tmp_path / "bare.db"
    conn = sqlite3.connect(str(p))
    try:
        assert build_reliability_table(conn) is None
    finally:
        conn.close()


def test_reliability_empty_table(db: Path) -> None:
    conn = sqlite3.connect(str(db))
    try:
        table = build_reliability_table(conn)
        assert table is not None
        assert table.total_n == 0
        assert table.brier is None
        assert not table.has_confident_cell
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------


def test_panel_absent_ledger(tmp_path: Path) -> None:
    p = tmp_path / "bare.db"
    sqlite3.connect(str(p)).close()
    html = render_credibility_panel(p)
    assert "Credibility" in html
    assert "No confidence-observation ledger" in html


def test_panel_empty_then_populated(db: Path) -> None:
    empty = render_credibility_panel(db)
    assert "No graded observations yet" in empty

    conn = sqlite3.connect(str(db))
    try:
        _seed_obs(conn, 0.92, 1, "fmp_normalized", 8)
        _seed_obs(conn, 0.92, 0, "fmp_normalized", 4)
        conn.commit()
    finally:
        conn.close()
    html = render_credibility_panel(db)
    assert "Brier score" in html
    assert "Reliability by confidence band" in html
    assert "Reliability by source tier" in html
    assert "fmp_normalized" in html


# ---------------------------------------------------------------------------
# Integration: the real call site captures
# ---------------------------------------------------------------------------


def test_insert_financial_facts_captures_supersede(db: Path) -> None:
    from compute._common import insert_financial_facts

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        doc1 = _doc(
            conn,
            ticker="GOOG",
            period_end="2023-03-31",
            fetched_at="2023-04-15",
            tier="fmp_normalized",
        )
        doc2 = _doc(
            conn,
            ticker="GOOG",
            period_end="2023-12-31",
            fetched_at="2024-02-15",
            tier="sec_official",
            source_type="sec_xbrl",
        )
        conn.commit()

        pe = datetime(2023, 3, 31)
        fact1 = FinancialFact(
            ticker="GOOG",
            period_end=pe,
            fiscal_period_type=FiscalPeriodType.Q1,
            line_item="revenue",
            value=Decimal("100"),
            currency=Currency.USD,
            unit=Unit.ACTUAL,
            source_doc_id=doc1,
            locator=LegacyEscapeHatch(
                reason="test fixture value -- provenance not under test here"
            ),
        )
        assert (
            insert_financial_facts(
                conn, [fact1], extracted_by="fmp", tier=SourceQualityTier.FMP_NORMALIZED
            )
            == 1
        )
        conn.commit()
        # No observation yet — first write for the logical key.
        assert conn.execute("SELECT COUNT(*) FROM confidence_observations").fetchone()[0] == 0

        fact2 = FinancialFact(
            ticker="GOOG",
            period_end=pe,
            fiscal_period_type=FiscalPeriodType.Q1,
            line_item="revenue",
            value=Decimal("88"),
            currency=Currency.USD,
            unit=Unit.ACTUAL,
            source_doc_id=doc2,
            locator=LegacyEscapeHatch(
                reason="test fixture value -- provenance not under test here"
            ),
        )
        assert (
            insert_financial_facts(
                conn, [fact2], extracted_by="sec_xbrl", tier=SourceQualityTier.SEC_OFFICIAL
            )
            == 1
        )
        conn.commit()

        rows = conn.execute("SELECT * FROM confidence_observations").fetchall()
        assert len(rows) == 1
        obs = rows[0]
        assert obs["kind"] == "restatement"
        assert obs["old_value"] == pytest.approx(100.0)
        assert obs["new_value"] == pytest.approx(88.0)
        assert obs["outcome"] == 0
        assert obs["source_tier"] == "fmp_normalized"
        assert obs["predicted_confidence"] == pytest.approx(0.94)  # scored fmp prior
    finally:
        conn.close()
