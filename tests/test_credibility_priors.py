"""Tests for the measured confidence prior (Close-the-Loops L10 PR2).

Covers ``credibility.priors`` (shrinkage, hierarchical backoff, the min-n gate)
and its wiring into ``pipeline.confidence`` (the ``tier_base_override`` on
``score_confidence`` and the ``priors`` path through ``apply_confidence_scores``
— the one consumer wired as proof).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from credibility.priors import SHRINKAGE_PSEUDOCOUNT, build_measured_priors
from pipeline.confidence import apply_confidence_scores, score_confidence


def _make_schema(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT, source_type TEXT, doc_type TEXT, period_end TEXT,
                file_path TEXT, fetched_at TEXT,
                source_quality_tier TEXT NOT NULL DEFAULT 'fmp_normalized'
            );
            CREATE TABLE financial_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL, period_end TEXT NOT NULL, fiscal_period_type TEXT NOT NULL,
                line_item TEXT NOT NULL, value NUMERIC NOT NULL, currency TEXT, unit TEXT NOT NULL,
                source_doc_id INTEGER NOT NULL, confidence FLOAT NOT NULL DEFAULT 1.0,
                extracted_by TEXT, supersedes_id INTEGER
            );
            CREATE TABLE confidence_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL DEFAULT 'bhanu',
                fact_table TEXT NOT NULL, fact_id INTEGER NOT NULL, kind TEXT NOT NULL,
                predicted_confidence FLOAT NOT NULL, source_tier TEXT, line_item TEXT,
                ticker TEXT, period_end TEXT, old_value FLOAT, new_value FLOAT, delta_pct FLOAT,
                outcome INTEGER NOT NULL, observed_at TEXT NOT NULL,
                CONSTRAINT uq UNIQUE (fact_table, fact_id, kind)
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


_FACT_SEQ = [0]


def _seed_obs(
    conn: sqlite3.Connection,
    *,
    tier: str,
    ticker: str | None,
    line_item: str | None,
    held: int,
    wrong: int,
) -> None:
    for i in range(held + wrong):
        _FACT_SEQ[0] += 1
        conn.execute(
            "INSERT INTO confidence_observations (fact_table, fact_id, kind, predicted_confidence, "
            "source_tier, ticker, line_item, outcome, observed_at) "
            "VALUES ('financial_facts', ?, 'restatement', 0.9, ?, ?, ?, ?, '2024-01-01')",
            (_FACT_SEQ[0], tier, ticker, line_item, 1 if i < held else 0),
        )


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "portfolio.db"
    _make_schema(p)
    _FACT_SEQ[0] = 0
    return p


# ---------------------------------------------------------------------------
# score_confidence override
# ---------------------------------------------------------------------------


def test_score_confidence_uses_override() -> None:
    # sec_official + deterministic = 0.98 + 0.02 = 1.0 without override.
    assert score_confidence(tier="sec_official", extracted_by="sec_xbrl") == pytest.approx(1.0)
    # With a measured base of 0.80, the method delta still applies: 0.80 + 0.02.
    assert score_confidence(
        tier="sec_official", extracted_by="sec_xbrl", tier_base_override=0.80
    ) == pytest.approx(0.82)


# ---------------------------------------------------------------------------
# MeasuredPriors
# ---------------------------------------------------------------------------


def test_empty_priors_return_fallback(db: Path) -> None:
    conn = sqlite3.connect(str(db))
    try:
        priors = build_measured_priors(conn)
        assert priors.tier_base("fmp_normalized", fallback=0.92) == pytest.approx(0.92)
    finally:
        conn.close()


def test_below_min_n_keeps_constant(db: Path) -> None:
    conn = sqlite3.connect(str(db))
    try:
        _seed_obs(conn, tier="fmp_normalized", ticker=None, line_item=None, held=3, wrong=2)  # n=5
        conn.commit()
        priors = build_measured_priors(conn)
        # n=5 < MIN_CONFIDENT_N → the gate refuses, fallback returned unchanged.
        assert priors.tier_base("fmp_normalized", fallback=0.92) == pytest.approx(0.92)
        assert priors.measured_cell("fmp_normalized") is None
    finally:
        conn.close()


def test_above_min_n_shrinks_toward_constant(db: Path) -> None:
    conn = sqlite3.connect(str(db))
    try:
        # 20 obs at the tier level, 14 held → observed 0.70.
        _seed_obs(conn, tier="sec_official", ticker=None, line_item=None, held=14, wrong=6)
        conn.commit()
        priors = build_measured_priors(conn)
        # shrink = (held + PSEUDO*const) / (n + PSEUDO) = (14 + 10*0.98)/30.
        expected = (14 + SHRINKAGE_PSEUDOCOUNT * 0.98) / (20 + SHRINKAGE_PSEUDOCOUNT)
        got = priors.tier_base("sec_official", fallback=0.98)
        assert got == pytest.approx(round(expected, 4))
        assert 0.70 < got < 0.98  # between observed and the constant
        cell = priors.measured_cell("sec_official")
        assert cell is not None and cell[0] == 20 and cell[1] == pytest.approx(0.70)
    finally:
        conn.close()


def test_backoff_prefers_most_specific_confident_cell(db: Path) -> None:
    conn = sqlite3.connect(str(db))
    try:
        # Tier-wide cell: mostly held (high rate). Specific (NU, revenue): mostly wrong.
        _seed_obs(conn, tier="fmp_normalized", ticker="OTHER", line_item="x", held=15, wrong=0)
        _seed_obs(conn, tier="fmp_normalized", ticker="NU", line_item="revenue", held=2, wrong=10)
        conn.commit()
        priors = build_measured_priors(conn)
        specific = priors.tier_base(
            "fmp_normalized", fallback=0.92, ticker="NU", line_item="revenue"
        )
        broad = priors.tier_base("fmp_normalized", fallback=0.92, ticker="ZZ", line_item="other")
        # The specific cell (n=12, rate≈0.17) pulls the base well below the broad one.
        assert specific < broad
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# apply_confidence_scores consumer
# ---------------------------------------------------------------------------


def _seed_fact(db: Path) -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, fetched_at, "
            "source_quality_tier) VALUES (1, 'TST', 'sec_xbrl', 'sec_10k', '/x', '2024-01-01', "
            "'sec_official')"
        )
        conn.execute(
            "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type, line_item, "
            "value, unit, source_doc_id, confidence, extracted_by) "
            "VALUES ('TST', '2023-03-31', 'Q1', 'revenue', '100', 'actual', 1, 1.0, 'sec_xbrl')"
        )
        # 20 graded obs for (sec_official, TST, revenue), 14 held → confident, rate 0.70.
        _seed_obs(conn, tier="sec_official", ticker="TST", line_item="revenue", held=14, wrong=6)
        conn.commit()
    finally:
        conn.close()


def test_apply_with_measured_priors_shifts_confidence(db: Path) -> None:
    _seed_fact(db)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        priors = build_measured_priors(conn)
        # Constants-only: sec_official(0.98)+det(0.02)=1.0 == stored → no update.
        baseline = apply_confidence_scores(conn, table="financial_facts", apply=False)
        assert baseline.updated == 0
        # Measured prior pulls the base down → the row would be rescored.
        measured = apply_confidence_scores(conn, table="financial_facts", apply=True, priors=priors)
        assert measured.updated == 1
        new_conf = conn.execute("SELECT confidence FROM financial_facts").fetchone()[0]
        expected_base = (14 + SHRINKAGE_PSEUDOCOUNT * 0.98) / (20 + SHRINKAGE_PSEUDOCOUNT)
        assert new_conf == pytest.approx(round(round(expected_base, 4) + 0.02, 4))
    finally:
        conn.close()


def test_apply_below_min_n_leaves_confidence_unchanged(db: Path) -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, fetched_at, "
            "source_quality_tier) VALUES (1, 'TST', 'sec_xbrl', 'sec_10k', '/x', '2024-01-01', "
            "'sec_official')"
        )
        conn.execute(
            "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type, line_item, "
            "value, unit, source_doc_id, confidence, extracted_by) "
            "VALUES ('TST', '2023-03-31', 'Q1', 'revenue', '100', 'actual', 1, 1.0, 'sec_xbrl')"
        )
        _seed_obs(conn, tier="sec_official", ticker="TST", line_item="revenue", held=3, wrong=2)
        conn.commit()
        priors = build_measured_priors(conn)
        # n=5 < floor → measured prior falls back to the constant → no change.
        out = apply_confidence_scores(conn, table="financial_facts", apply=False, priors=priors)
        assert out.updated == 0
    finally:
        conn.close()
