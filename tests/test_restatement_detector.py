"""Tests for src/pipeline/restatement_detector.py + the tier-aware /
time-travel paths in src/timeseries/loaders.py.

Golden-data tests on a synthetic schema (documents + financial_facts +
kpi_facts + kpi_definitions + segment_facts) so behavior is deterministic
and independent of live portfolio.db state. Mirrors the fixture shape
used in tests/test_timeseries.py.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from pipeline.restatement_detector import (
    find_incumbent,
    insert_kpi_with_restatement_detection,
    insert_with_restatement_detection,
    is_later_filing,
    latest_in_chain,
)
from timeseries import load_financial_series
from timeseries.loaders import load_kpi_series

# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


def _make_schema(db_path: Path) -> None:
    """Build the post-0053 shape: documents with source_quality_tier,
    financial_facts + kpi_facts with confidence/extracted_by/supersedes_id."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                source_type TEXT NOT NULL,
                doc_type TEXT NOT NULL,
                period_start TEXT,
                period_end TEXT,
                file_path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                fetch_status TEXT NOT NULL,
                http_code INTEGER,
                raw_bytes_size INTEGER NOT NULL,
                source_url TEXT,
                parent_document_id INTEGER,
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
            CREATE UNIQUE INDEX uq_financial_facts_provenance
              ON financial_facts (ticker, period_end, fiscal_period_type, line_item, source_doc_id);
            CREATE INDEX idx_financial_facts_restatement_lookup
              ON financial_facts (ticker, period_end, fiscal_period_type, line_item);

            CREATE TABLE kpi_definitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                name TEXT NOT NULL,
                unit TEXT NOT NULL,
                primary_source TEXT NOT NULL,
                fallback_source TEXT,
                ir_url TEXT,
                threshold_tier TEXT,
                threshold_low FLOAT,
                threshold_high FLOAT,
                notes TEXT,
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
            CREATE UNIQUE INDEX uq_kpi_facts_provenance
              ON kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, source_doc_id);
            """
        )
        conn.commit()
    finally:
        conn.close()


def _insert_document(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    source_type: str,
    doc_type: str,
    period_end: str,
    fetched_at: str,
    sha256: str,
    tier: str = "fmp_normalized",
) -> int:
    """Insert one documents row; returns id."""
    cur = conn.execute(
        "INSERT INTO documents "
        "(ticker, source_type, doc_type, period_end, file_path, sha256, "
        " fetched_at, fetch_status, raw_bytes_size, source_quality_tier) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ticker.upper(),
            source_type,
            doc_type,
            period_end,
            f"data/{sha256}.json",
            sha256,
            fetched_at,
            "ok",
            1024,
            tier,
        ),
    )
    return int(cur.lastrowid) if cur.lastrowid is not None else 0


@pytest.fixture()
def fixture_db(tmp_path: Path) -> Path:
    p = tmp_path / "portfolio.db"
    _make_schema(p)
    return p


# ---------------------------------------------------------------------------
# is_later_filing
# ---------------------------------------------------------------------------


def test_is_later_filing_compares_fiscal_year(fixture_db: Path) -> None:
    """A FY 10-K (period_end 2023-12-31) is later than a Q1 10-Q
    (period_end 2023-03-31) because its year > 2023... wait, both are 2023.
    Within the same year, fetched_at is the tiebreaker."""
    conn = sqlite3.connect(str(fixture_db))
    conn.row_factory = sqlite3.Row
    try:
        q1_doc = _insert_document(
            conn,
            ticker="AMZN",
            source_type="fmp",
            doc_type="fmp_income_statement",
            period_end="2023-03-31 00:00:00",
            fetched_at="2023-05-01 12:00:00",
            sha256="a" * 64,
        )
        fy_doc = _insert_document(
            conn,
            ticker="AMZN",
            source_type="fmp",
            doc_type="fmp_income_statement",
            period_end="2023-12-31 00:00:00",
            fetched_at="2024-02-15 12:00:00",
            sha256="b" * 64,
        )
        # Cross-year: FY 2024 should be later than Q1 2023.
        future_fy_doc = _insert_document(
            conn,
            ticker="AMZN",
            source_type="fmp",
            doc_type="fmp_income_statement",
            period_end="2024-12-31 00:00:00",
            fetched_at="2025-02-15 12:00:00",
            sha256="c" * 64,
        )
        conn.commit()

        assert (
            is_later_filing(
                conn,
                new_source_doc_id=future_fy_doc,
                incumbent_source_doc_id=q1_doc,
            )
            is True
        )
        assert (
            is_later_filing(conn, new_source_doc_id=q1_doc, incumbent_source_doc_id=fy_doc) is False
        )
        # Same-year fallback to fetched_at: FY 2023 fetched after Q1 2023.
        assert (
            is_later_filing(conn, new_source_doc_id=fy_doc, incumbent_source_doc_id=q1_doc) is True
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# insert_with_restatement_detection
# ---------------------------------------------------------------------------


def test_financial_fact_sqlite_invariant_failure_aborts_batch(
    fixture_db: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    conn = sqlite3.connect(str(fixture_db))
    conn.row_factory = sqlite3.Row
    attempted: list[str] = []
    try:
        doc = _insert_document(
            conn,
            ticker="V",
            source_type="fmp",
            doc_type="fmp_income_statement",
            period_end="2026-06-30 00:00:00",
            fetched_at="2026-07-25 12:00:00",
            sha256="a" * 64,
        )
        conn.execute(
            "CREATE TRIGGER reject_unanchored_fact AFTER INSERT ON financial_facts "
            "BEGIN SELECT RAISE(ABORT, "
            "'fact write requires an evidence-backed source document'); END"
        )
        conn.commit()

        with pytest.raises(
            sqlite3.IntegrityError,
            match="fact write requires an evidence-backed source document",
        ):
            for line_item in ("revenue", "gross_profit"):
                attempted.append(line_item)
                insert_with_restatement_detection(
                    conn,
                    ticker="V",
                    period_end=datetime.fromisoformat("2026-06-30"),
                    fiscal_period_type="Q2",
                    line_item=line_item,
                    value=Decimal("100"),
                    currency="USD",
                    unit="millions",
                    source_doc_id=doc,
                    extracted_by="fmp",
                )

        assert attempted == ["revenue"]
        assert conn.execute("SELECT COUNT(*) FROM financial_facts").fetchone()[0] == 0
        failure_events = [
            record
            for record in caplog.records
            if "restatement_insert_failed" in record.getMessage()
        ]
        assert len(failure_events) == 1
    finally:
        conn.close()


def test_restatement_chain_two_rows_with_supersedes_link(fixture_db: Path) -> None:
    """The brief's example: AMZN Q1 2023 revenue from FMP first (Q-filing),
    then from FMP again (FY-filing restated value). Both rows survive;
    the FY row's supersedes_id links to the Q row's id."""
    conn = sqlite3.connect(str(fixture_db))
    conn.row_factory = sqlite3.Row
    try:
        q_doc = _insert_document(
            conn,
            ticker="AMZN",
            source_type="fmp",
            doc_type="fmp_income_statement",
            period_end="2023-03-31 00:00:00",
            fetched_at="2023-05-01 12:00:00",
            sha256="a" * 64,
        )
        fy_doc = _insert_document(
            conn,
            ticker="AMZN",
            source_type="fmp",
            doc_type="fmp_income_statement",
            period_end="2023-12-31 00:00:00",
            fetched_at="2024-02-15 12:00:00",
            sha256="b" * 64,
        )
        conn.commit()

        # First write: Q-filing value for Q1.
        q_row_id, superseded_q = insert_with_restatement_detection(
            conn,
            ticker="AMZN",
            period_end=datetime.fromisoformat("2023-03-31"),
            fiscal_period_type="Q1",
            line_item="revenue",
            value=Decimal("127358"),
            currency="USD",
            unit="millions",
            source_doc_id=q_doc,
            extracted_by="fmp",
        )
        conn.commit()
        assert q_row_id is not None
        assert superseded_q is None  # first row for the logical key

        # Restatement: FY-filing for Q1 with adjusted value.
        fy_row_id, superseded_fy = insert_with_restatement_detection(
            conn,
            ticker="AMZN",
            period_end=datetime.fromisoformat("2023-03-31"),
            fiscal_period_type="Q1",
            line_item="revenue",
            value=Decimal("127500"),
            currency="USD",
            unit="millions",
            source_doc_id=fy_doc,
            extracted_by="fmp",
        )
        conn.commit()
        assert fy_row_id is not None
        assert superseded_fy == q_row_id

        # Both rows survive.
        rows = conn.execute(
            "SELECT id, value, supersedes_id FROM financial_facts "
            "WHERE ticker = ? AND line_item = ? "
            "ORDER BY id ASC",
            ("AMZN", "revenue"),
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["supersedes_id"] is None
        assert int(rows[1]["supersedes_id"]) == q_row_id
        assert float(rows[1]["value"]) == 127500.0
    finally:
        conn.close()


def test_restatement_chain_loader_returns_newer_by_default(fixture_db: Path) -> None:
    """After the restatement chain is built, load_financial_series should
    return the restated (newer) value. The as_of_date=2023-04-30 view
    should return the original Q1 value because the restating FY 10-K
    was fetched in 2024-02."""
    conn = sqlite3.connect(str(fixture_db))
    try:
        q_doc = _insert_document(
            conn,
            ticker="AMZN",
            source_type="fmp",
            doc_type="fmp_income_statement",
            period_end="2023-03-31 00:00:00",
            fetched_at="2023-05-01 12:00:00",
            sha256="a" * 64,
        )
        fy_doc = _insert_document(
            conn,
            ticker="AMZN",
            source_type="fmp",
            doc_type="fmp_income_statement",
            period_end="2023-12-31 00:00:00",
            fetched_at="2024-02-15 12:00:00",
            sha256="b" * 64,
        )
        conn.commit()
        insert_with_restatement_detection(
            conn,
            ticker="AMZN",
            period_end=datetime.fromisoformat("2023-03-31"),
            fiscal_period_type="Q1",
            line_item="revenue",
            value=Decimal("127358"),
            currency="USD",
            unit="millions",
            source_doc_id=q_doc,
        )
        insert_with_restatement_detection(
            conn,
            ticker="AMZN",
            period_end=datetime.fromisoformat("2023-03-31"),
            fiscal_period_type="Q1",
            line_item="revenue",
            value=Decimal("127500"),
            currency="USD",
            unit="millions",
            source_doc_id=fy_doc,
        )
        conn.commit()
    finally:
        conn.close()

    # Default (no as_of_date): the restated FY-sourced value wins.
    obs = load_financial_series(ticker="AMZN", line_item="revenue", db_path=fixture_db)
    assert len(obs) == 1
    assert obs[0].value == pytest.approx(127500.0)

    # Time-travel: as of 2023-05-01, only the Q-filing existed (fy_doc was
    # fetched 2024-02-15). Loader should return the original 127358.
    # 2023-04-30 cutoff: q_doc fetched 2023-05-01 12:00:00 — strictly later
    # than the 23:59:59 cutoff, so even the Q row should be filtered out.
    obs_pre_q = load_financial_series(
        ticker="AMZN",
        line_item="revenue",
        db_path=fixture_db,
        as_of_date="2023-04-30",
    )
    assert obs_pre_q == []

    obs_post_q = load_financial_series(
        ticker="AMZN",
        line_item="revenue",
        db_path=fixture_db,
        as_of_date="2023-06-01",
    )
    assert len(obs_post_q) == 1
    assert obs_post_q[0].value == pytest.approx(127358.0)


# ---------------------------------------------------------------------------
# Tier-aware dedup
# ---------------------------------------------------------------------------


def test_loader_prefers_sec_official_over_fmp_normalized(fixture_db: Path) -> None:
    """Two documents target the same logical period: an FMP row (id 1)
    and a later SEC XBRL row (id 2). The tier-aware loader must pick the
    SEC value regardless of id ordering — but here SEC is also higher id,
    so this case verifies the SEC value wins."""
    conn = sqlite3.connect(str(fixture_db))
    try:
        fmp_doc = _insert_document(
            conn,
            ticker="AMZN",
            source_type="fmp",
            doc_type="fmp_income_statement",
            period_end="2023-03-31 00:00:00",
            fetched_at="2023-05-01 12:00:00",
            sha256="a" * 64,
            tier="fmp_normalized",
        )
        sec_doc = _insert_document(
            conn,
            ticker="AMZN",
            source_type="sec_xbrl",
            doc_type="sec_10q",
            period_end="2023-03-31 00:00:00",
            fetched_at="2023-05-02 12:00:00",
            sha256="b" * 64,
            tier="sec_official",
        )
        for doc, val in [(fmp_doc, 127358), (sec_doc, 127360)]:
            conn.execute(
                "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type, "
                "line_item, value, currency, unit, source_doc_id, confidence) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "AMZN",
                    "2023-03-31 00:00:00",
                    "Q1",
                    "revenue",
                    str(val),
                    "USD",
                    "millions",
                    doc,
                    1.0,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    obs = load_financial_series(ticker="AMZN", line_item="revenue", db_path=fixture_db)
    assert len(obs) == 1
    assert obs[0].value == pytest.approx(127360.0)


def test_loader_tier_beats_higher_id(fixture_db: Path) -> None:
    """Critical case: SEC row inserted FIRST (id=1), FMP row inserted
    SECOND (id=2). Old behavior (max(id)) would pick FMP. Tier-aware
    must still pick SEC because sec_official > fmp_normalized."""
    conn = sqlite3.connect(str(fixture_db))
    try:
        sec_doc = _insert_document(
            conn,
            ticker="AMZN",
            source_type="sec_xbrl",
            doc_type="sec_10q",
            period_end="2023-03-31 00:00:00",
            fetched_at="2023-05-01 12:00:00",
            sha256="a" * 64,
            tier="sec_official",
        )
        fmp_doc = _insert_document(
            conn,
            ticker="AMZN",
            source_type="fmp",
            doc_type="fmp_income_statement",
            period_end="2023-03-31 00:00:00",
            fetched_at="2023-05-02 12:00:00",
            sha256="b" * 64,
            tier="fmp_normalized",
        )
        # SEC row first → lower id; FMP row second → higher id.
        for doc, val in [(sec_doc, 127360), (fmp_doc, 127358)]:
            conn.execute(
                "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type, "
                "line_item, value, currency, unit, source_doc_id, confidence) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "AMZN",
                    "2023-03-31 00:00:00",
                    "Q1",
                    "revenue",
                    str(val),
                    "USD",
                    "millions",
                    doc,
                    1.0,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    obs = load_financial_series(ticker="AMZN", line_item="revenue", db_path=fixture_db)
    assert len(obs) == 1
    # SEC value wins despite lower id — that is the whole point of the
    # tier-aware ordering.
    assert obs[0].value == pytest.approx(127360.0)


def test_loader_within_same_tier_higher_id_wins(fixture_db: Path) -> None:
    """When both rows share a tier (two FMP docs), max(id) is the tiebreaker.
    The brief's expected behavior — newest ingestion within a tier wins."""
    conn = sqlite3.connect(str(fixture_db))
    try:
        old_doc = _insert_document(
            conn,
            ticker="GOOG",
            source_type="fmp",
            doc_type="fmp_income_statement",
            period_end="2023-03-31 00:00:00",
            fetched_at="2023-05-01 12:00:00",
            sha256="a" * 64,
            tier="fmp_normalized",
        )
        new_doc = _insert_document(
            conn,
            ticker="GOOG",
            source_type="fmp",
            doc_type="fmp_income_statement",
            period_end="2023-03-31 00:00:00",
            fetched_at="2023-06-01 12:00:00",
            sha256="b" * 64,
            tier="fmp_normalized",
        )
        for doc, val in [(old_doc, 80000), (new_doc, 80540)]:
            conn.execute(
                "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type, "
                "line_item, value, currency, unit, source_doc_id, confidence) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "GOOG",
                    "2023-03-31 00:00:00",
                    "Q1",
                    "revenue",
                    str(val),
                    "USD",
                    "millions",
                    doc,
                    1.0,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    obs = load_financial_series(ticker="GOOG", line_item="revenue", db_path=fixture_db)
    assert len(obs) == 1
    assert obs[0].value == pytest.approx(80540.0)


# ---------------------------------------------------------------------------
# find_incumbent + latest_in_chain
# ---------------------------------------------------------------------------


def test_find_incumbent_returns_head_of_chain(fixture_db: Path) -> None:
    """When A <- B <- C exists, find_incumbent returns C (the head)."""
    conn = sqlite3.connect(str(fixture_db))
    try:
        # Three docs, period_end years 2023 / 2024 / 2025 — strictly
        # increasing so each is a "later filing" than the previous.
        docs: list[int] = []
        for year, sha in zip([2023, 2024, 2025], ["a", "b", "c"], strict=True):
            d = _insert_document(
                conn,
                ticker="META",
                source_type="fmp",
                doc_type="fmp_income_statement",
                period_end=f"{year}-12-31 00:00:00",
                fetched_at=f"{year + 1}-02-15 12:00:00",
                sha256=sha * 64,
            )
            docs.append(d)
        conn.commit()

        ids: list[int] = []
        for doc, val in zip(docs, [100, 101, 102], strict=True):
            new_id, _ = insert_with_restatement_detection(
                conn,
                ticker="META",
                period_end=datetime.fromisoformat("2023-03-31"),
                fiscal_period_type="Q1",
                line_item="revenue",
                value=Decimal(str(val)),
                currency="USD",
                unit="millions",
                source_doc_id=doc,
            )
            assert new_id is not None
            ids.append(new_id)
        conn.commit()

        # Chain: ids[0] <- ids[1] <- ids[2]. The head is ids[2].
        assert (
            find_incumbent(
                conn,
                ticker="META",
                period_end=datetime.fromisoformat("2023-03-31"),
                fiscal_period_type="Q1",
                line_item="revenue",
            )
            == ids[2]
        )

        # latest_in_chain from the root should walk forward to ids[2].
        assert latest_in_chain(conn, ids[0]) == ids[2]
        assert latest_in_chain(conn, ids[1]) == ids[2]
        assert latest_in_chain(conn, ids[2]) == ids[2]
    finally:
        conn.close()


def test_same_document_replay_is_noop(fixture_db: Path) -> None:
    """Re-running an extractor against the same source_doc_id yields a UNIQUE
    conflict on (ticker, period_end, fiscal_period_type, line_item,
    source_doc_id) — no row written, no chain link."""
    conn = sqlite3.connect(str(fixture_db))
    try:
        doc = _insert_document(
            conn,
            ticker="META",
            source_type="fmp",
            doc_type="fmp_income_statement",
            period_end="2023-03-31 00:00:00",
            fetched_at="2023-05-01 12:00:00",
            sha256="a" * 64,
        )
        conn.commit()

        first_id, _ = insert_with_restatement_detection(
            conn,
            ticker="META",
            period_end=datetime.fromisoformat("2023-03-31"),
            fiscal_period_type="Q1",
            line_item="revenue",
            value=Decimal("100"),
            currency="USD",
            unit="millions",
            source_doc_id=doc,
        )
        replay_id, replay_supersedes = insert_with_restatement_detection(
            conn,
            ticker="META",
            period_end=datetime.fromisoformat("2023-03-31"),
            fiscal_period_type="Q1",
            line_item="revenue",
            value=Decimal("100"),
            currency="USD",
            unit="millions",
            source_doc_id=doc,
        )
        conn.commit()

        assert first_id is not None
        assert replay_id is None
        assert replay_supersedes is None
        # Exactly one row exists for the logical key.
        rows = conn.execute(
            "SELECT COUNT(*) FROM financial_facts WHERE ticker = ? AND line_item = ?",
            ("META", "revenue"),
        ).fetchone()
        assert rows[0] == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Same-document correction (heal a stale value for the SAME source_doc_id)
# ---------------------------------------------------------------------------


def _seed_ff_row(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    period_end: str,
    fiscal_period_type: str,
    line_item: str,
    value: str,
    source_doc_id: int,
    extracted_by: str,
    currency: str = "USD",
    unit: str = "actual",
) -> int:
    """Insert one financial_facts row directly (controls extracted_by so the
    same-extractor safety rail can be exercised). Returns id."""
    cur = conn.execute(
        "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type, "
        "line_item, value, currency, unit, source_doc_id, extracted_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ticker.upper(),
            period_end,
            fiscal_period_type,
            line_item,
            value,
            currency,
            unit,
            source_doc_id,
            extracted_by,
        ),
    )
    return int(cur.lastrowid) if cur.lastrowid is not None else 0


def test_same_document_correction_heals_stale_value(fixture_db: Path) -> None:
    """The MELI revenue case: the SAME 10-Q accession is re-extracted and now
    yields the aggregate `us-gaap:Revenues` (8,845M) instead of the narrow
    concept first pulled (6,065M). INSERT OR IGNORE would freeze the stale
    value; the same-document correction UPDATEs the incumbent in place. No new
    row is written and no chain link is created."""
    conn = sqlite3.connect(str(fixture_db))
    conn.row_factory = sqlite3.Row
    try:
        doc = _insert_document(
            conn,
            ticker="MELI",
            source_type="sec_xbrl",
            doc_type="sec_10q",
            period_end="2026-03-31 00:00:00",
            fetched_at="2026-05-01 12:00:00",
            sha256="a" * 64,
            tier="sec_official",
        )
        conn.commit()

        first_id, _ = insert_with_restatement_detection(
            conn,
            ticker="MELI",
            period_end=datetime.fromisoformat("2026-03-31"),
            fiscal_period_type="Q1",
            line_item="revenue",
            value=Decimal("6065000000"),
            currency="USD",
            unit="actual",
            source_doc_id=doc,
            extracted_by="sec_xbrl",
        )
        conn.commit()
        assert first_id is not None

        # Re-extraction of the SAME accession, corrected value, SAME extractor.
        heal_id, heal_supersedes = insert_with_restatement_detection(
            conn,
            ticker="MELI",
            period_end=datetime.fromisoformat("2026-03-31"),
            fiscal_period_type="Q1",
            line_item="revenue",
            value=Decimal("8845000000"),
            currency="USD",
            unit="actual",
            source_doc_id=doc,
            extracted_by="sec_xbrl",
        )
        conn.commit()

        # No new row, no chain link — but the incumbent is healed in place.
        assert heal_id is None
        assert heal_supersedes is None
        rows = conn.execute(
            "SELECT id, value FROM financial_facts WHERE ticker=? AND line_item=?",
            ("MELI", "revenue"),
        ).fetchall()
        assert len(rows) == 1
        assert int(rows[0]["id"]) == first_id
        assert Decimal(str(rows[0]["value"])) == Decimal("8845000000")
    finally:
        conn.close()


def test_same_document_identical_replay_leaves_value_untouched(fixture_db: Path) -> None:
    """An identical same-document replay stays a true no-op — the correction
    path must not touch a row whose value/currency/unit are unchanged."""
    conn = sqlite3.connect(str(fixture_db))
    conn.row_factory = sqlite3.Row
    try:
        doc = _insert_document(
            conn,
            ticker="NU",
            source_type="sec_xbrl",
            doc_type="sec_10q",
            period_end="2026-03-31 00:00:00",
            fetched_at="2026-05-01 12:00:00",
            sha256="a" * 64,
            tier="sec_official",
        )
        conn.commit()
        first_id, _ = insert_with_restatement_detection(
            conn,
            ticker="NU",
            period_end=datetime.fromisoformat("2026-03-31"),
            fiscal_period_type="Q1",
            line_item="net_income",
            value=Decimal("557000000"),
            currency="USD",
            unit="actual",
            source_doc_id=doc,
            extracted_by="sec_xbrl",
        )
        conn.commit()
        replay_id, replay_supersedes = insert_with_restatement_detection(
            conn,
            ticker="NU",
            period_end=datetime.fromisoformat("2026-03-31"),
            fiscal_period_type="Q1",
            line_item="net_income",
            value=Decimal("557000000"),
            currency="USD",
            unit="actual",
            source_doc_id=doc,
            extracted_by="sec_xbrl",
        )
        conn.commit()
        assert replay_id is None
        assert replay_supersedes is None
        rows = conn.execute(
            "SELECT id, value FROM financial_facts WHERE ticker=? AND line_item=?",
            ("NU", "net_income"),
        ).fetchall()
        assert len(rows) == 1
        assert int(rows[0]["id"]) == first_id
        assert Decimal(str(rows[0]["value"])) == Decimal("557000000")
    finally:
        conn.close()


def test_same_document_correction_respects_extractor_safety_rail(fixture_db: Path) -> None:
    """A row sharing the doc id but authored by a DIFFERENT extractor (a manual
    override) is never clobbered. The re-extraction is dropped, the manual
    value survives."""
    conn = sqlite3.connect(str(fixture_db))
    conn.row_factory = sqlite3.Row
    try:
        doc = _insert_document(
            conn,
            ticker="NVO",
            source_type="sec_xbrl",
            doc_type="sec_10q",
            period_end="2026-03-31 00:00:00",
            fetched_at="2026-05-01 12:00:00",
            sha256="a" * 64,
            tier="sec_official",
        )
        conn.commit()
        # Incumbent authored by a manual override, sharing the doc id.
        manual_id = _seed_ff_row(
            conn,
            ticker="NVO",
            period_end="2026-03-31 00:00:00",
            fiscal_period_type="Q1",
            line_item="revenue",
            value="71000000000",
            source_doc_id=doc,
            extracted_by="manual",
        )
        conn.commit()

        # sec_xbrl re-extraction of the same doc with a different value.
        heal_id, heal_supersedes = insert_with_restatement_detection(
            conn,
            ticker="NVO",
            period_end=datetime.fromisoformat("2026-03-31"),
            fiscal_period_type="Q1",
            line_item="revenue",
            value=Decimal("69000000000"),
            currency="USD",
            unit="actual",
            source_doc_id=doc,
            extracted_by="sec_xbrl",
        )
        conn.commit()

        assert heal_id is None
        assert heal_supersedes is None
        rows = conn.execute(
            "SELECT id, value, extracted_by FROM financial_facts WHERE ticker=? AND line_item=?",
            ("NVO", "revenue"),
        ).fetchall()
        assert len(rows) == 1
        assert int(rows[0]["id"]) == manual_id
        # Manual value preserved — the safety rail blocked the overwrite.
        assert Decimal(str(rows[0]["value"])) == Decimal("71000000000")
        assert rows[0]["extracted_by"] == "manual"
    finally:
        conn.close()


def test_same_document_correction_heals_unit_change(fixture_db: Path) -> None:
    """The correction also fires when only the unit/currency changed (value
    equal) — e.g. a mis-stamped unit corrected on re-extraction."""
    conn = sqlite3.connect(str(fixture_db))
    conn.row_factory = sqlite3.Row
    try:
        doc = _insert_document(
            conn,
            ticker="VEEV",
            source_type="sec_xbrl",
            doc_type="sec_10q",
            period_end="2026-01-31 00:00:00",
            fetched_at="2026-03-01 12:00:00",
            sha256="a" * 64,
            tier="sec_official",
        )
        conn.commit()
        first_id = _seed_ff_row(
            conn,
            ticker="VEEV",
            period_end="2026-01-31 00:00:00",
            fiscal_period_type="Q4",
            line_item="revenue",
            value="700000000",
            source_doc_id=doc,
            extracted_by="sec_xbrl",
            unit="millions",
        )
        conn.commit()
        heal_id, _ = insert_with_restatement_detection(
            conn,
            ticker="VEEV",
            period_end=datetime.fromisoformat("2026-01-31"),
            fiscal_period_type="Q4",
            line_item="revenue",
            value=Decimal("700000000"),
            currency="USD",
            unit="actual",
            source_doc_id=doc,
            extracted_by="sec_xbrl",
        )
        conn.commit()
        assert heal_id is None
        row = conn.execute(
            "SELECT unit FROM financial_facts WHERE id=?",
            (first_id,),
        ).fetchone()
        assert row["unit"] == "actual"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# insert_kpi_with_restatement_detection — kpi_facts side (post-0059)
# ---------------------------------------------------------------------------


def _seed_kpi_definition(conn: sqlite3.Connection, *, ticker: str, name: str) -> int:
    """Insert one kpi_definitions row; returns id."""
    cur = conn.execute(
        "INSERT INTO kpi_definitions (ticker, name, unit, primary_source) "
        "VALUES (?, ?, 'percent', 'ir_doc')",
        (ticker.upper(), name),
    )
    return int(cur.lastrowid) if cur.lastrowid is not None else 0


def test_kpi_restatement_chain_two_rows_with_supersedes_link(
    fixture_db: Path,
) -> None:
    """kpi_facts twin of test_restatement_chain_two_rows_with_supersedes_link.
    A KPI value first lands from a Q-period IR doc; the FY IR doc later
    restates it. Both rows survive under uq_kpi_facts_provenance, and the
    FY row's supersedes_id links back to the Q row's id."""
    conn = sqlite3.connect(str(fixture_db))
    conn.row_factory = sqlite3.Row
    try:
        q_doc = _insert_document(
            conn,
            ticker="MELI",
            source_type="ir_doc",
            doc_type="ir_press_release",
            period_end="2023-03-31 00:00:00",
            fetched_at="2023-05-01 12:00:00",
            sha256="a" * 64,
        )
        fy_doc = _insert_document(
            conn,
            ticker="MELI",
            source_type="ir_doc",
            doc_type="ir_press_release",
            period_end="2023-12-31 00:00:00",
            fetched_at="2024-02-15 12:00:00",
            sha256="b" * 64,
        )
        kpi_def_id = _seed_kpi_definition(conn, ticker="MELI", name="Revenue Growth (FXN)")
        conn.commit()

        # First write: Q-filing value.
        q_row_id, superseded_q = insert_kpi_with_restatement_detection(
            conn,
            ticker="MELI",
            period_end=datetime.fromisoformat("2023-03-31"),
            fiscal_period_type="Q1",
            kpi_definition_id=kpi_def_id,
            value=Decimal("90"),
            unit="percent",
            source_doc_id=q_doc,
            extracted_by="llm:claude-haiku-4-5-20251001",
        )
        conn.commit()
        assert q_row_id is not None
        assert superseded_q is None

        # Restatement: FY-filing for Q1 with adjusted value.
        fy_row_id, superseded_fy = insert_kpi_with_restatement_detection(
            conn,
            ticker="MELI",
            period_end=datetime.fromisoformat("2023-03-31"),
            fiscal_period_type="Q1",
            kpi_definition_id=kpi_def_id,
            value=Decimal("92"),
            unit="percent",
            source_doc_id=fy_doc,
            extracted_by="llm:claude-haiku-4-5-20251001",
        )
        conn.commit()
        assert fy_row_id is not None
        assert superseded_fy == q_row_id

        rows = conn.execute(
            "SELECT id, value, supersedes_id, extracted_by FROM kpi_facts "
            "WHERE ticker = ? AND kpi_definition_id = ? "
            "ORDER BY id ASC",
            ("MELI", kpi_def_id),
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["supersedes_id"] is None
        assert int(rows[1]["supersedes_id"]) == q_row_id
        assert float(rows[1]["value"]) == 92.0
        assert rows[1]["extracted_by"] == "llm:claude-haiku-4-5-20251001"
    finally:
        conn.close()


def test_kpi_same_document_replay_is_noop(fixture_db: Path) -> None:
    """Re-running an extractor against the same source_doc_id yields a
    UNIQUE conflict on (ticker, period_end, fiscal_period_type,
    kpi_definition_id, source_doc_id) — no row written, no chain link."""
    conn = sqlite3.connect(str(fixture_db))
    conn.row_factory = sqlite3.Row
    try:
        doc = _insert_document(
            conn,
            ticker="RBRK",
            source_type="ir_doc",
            doc_type="ir_press_release",
            period_end="2026-01-31 00:00:00",
            fetched_at="2026-02-25 12:00:00",
            sha256="a" * 64,
        )
        kpi_def_id = _seed_kpi_definition(conn, ticker="RBRK", name="Revenue YoY Growth (USD)")
        conn.commit()

        first_id, _ = insert_kpi_with_restatement_detection(
            conn,
            ticker="RBRK",
            period_end=datetime.fromisoformat("2026-01-31"),
            fiscal_period_type="Q4",
            kpi_definition_id=kpi_def_id,
            value=Decimal("46.33"),
            unit="percent",
            source_doc_id=doc,
        )
        replay_id, replay_supersedes = insert_kpi_with_restatement_detection(
            conn,
            ticker="RBRK",
            period_end=datetime.fromisoformat("2026-01-31"),
            fiscal_period_type="Q4",
            kpi_definition_id=kpi_def_id,
            value=Decimal("46.33"),
            unit="percent",
            source_doc_id=doc,
        )
        conn.commit()

        assert first_id is not None
        assert replay_id is None
        assert replay_supersedes is None
        rows = conn.execute(
            "SELECT COUNT(*) FROM kpi_facts WHERE ticker = ?",
            ("RBRK",),
        ).fetchone()
        assert rows[0] == 1
    finally:
        conn.close()


def test_kpi_loader_returns_restated_value_by_default(fixture_db: Path) -> None:
    """After the restatement chain is built, load_kpi_series should return
    the restated (newer-tier/newer-id) value. Mirrors the financial-facts
    loader behavior for kpi_facts."""
    conn = sqlite3.connect(str(fixture_db))
    try:
        q_doc = _insert_document(
            conn,
            ticker="MELI",
            source_type="ir_doc",
            doc_type="ir_press_release",
            period_end="2023-03-31 00:00:00",
            fetched_at="2023-05-01 12:00:00",
            sha256="a" * 64,
            tier="llm_extracted",
        )
        fy_doc = _insert_document(
            conn,
            ticker="MELI",
            source_type="ir_doc",
            doc_type="ir_press_release",
            period_end="2023-12-31 00:00:00",
            fetched_at="2024-02-15 12:00:00",
            sha256="b" * 64,
            tier="llm_extracted",
        )
        kpi_def_id = _seed_kpi_definition(conn, ticker="MELI", name="Revenue Growth (FXN)")
        conn.commit()
        insert_kpi_with_restatement_detection(
            conn,
            ticker="MELI",
            period_end=datetime.fromisoformat("2023-03-31"),
            fiscal_period_type="Q1",
            kpi_definition_id=kpi_def_id,
            value=Decimal("90"),
            unit="percent",
            source_doc_id=q_doc,
        )
        insert_kpi_with_restatement_detection(
            conn,
            ticker="MELI",
            period_end=datetime.fromisoformat("2023-03-31"),
            fiscal_period_type="Q1",
            kpi_definition_id=kpi_def_id,
            value=Decimal("92"),
            unit="percent",
            source_doc_id=fy_doc,
        )
        conn.commit()
    finally:
        conn.close()

    obs = load_kpi_series(ticker="MELI", kpi_name="Revenue Growth (FXN)", db_path=fixture_db)
    assert len(obs) == 1
    assert obs[0].value == pytest.approx(92.0)


def test_kpi_loader_prefers_sec_official_over_llm_extracted(
    fixture_db: Path,
) -> None:
    """When the same logical KPI key has both an IR-doc-derived row (tier
    llm_extracted) and an FMP-derived row (tier fmp_normalized), the
    higher-tier FMP row wins regardless of insertion order. Validates
    that the relaxed constraint + tier-aware loader composes correctly
    for KPIs."""
    conn = sqlite3.connect(str(fixture_db))
    try:
        # IR doc lands FIRST (lower id, lower tier).
        ir_doc = _insert_document(
            conn,
            ticker="MELI",
            source_type="ir_doc",
            doc_type="ir_press_release",
            period_end="2023-03-31 00:00:00",
            fetched_at="2023-05-01 12:00:00",
            sha256="a" * 64,
            tier="llm_extracted",
        )
        # FMP doc lands SECOND (higher id, higher tier).
        fmp_doc = _insert_document(
            conn,
            ticker="MELI",
            source_type="fmp",
            doc_type="fmp_income_statement",
            period_end="2023-03-31 00:00:00",
            fetched_at="2023-05-02 12:00:00",
            sha256="b" * 64,
            tier="fmp_normalized",
        )
        kpi_def_id = _seed_kpi_definition(conn, ticker="MELI", name="Operating Margin (GAAP)")
        for doc, val in [(ir_doc, Decimal("13.0")), (fmp_doc, Decimal("13.5"))]:
            insert_kpi_with_restatement_detection(
                conn,
                ticker="MELI",
                period_end=datetime.fromisoformat("2023-03-31"),
                fiscal_period_type="Q1",
                kpi_definition_id=kpi_def_id,
                value=val,
                unit="percent",
                source_doc_id=doc,
            )
        conn.commit()
    finally:
        conn.close()

    obs = load_kpi_series(
        ticker="MELI",
        kpi_name="Operating Margin (GAAP)",
        db_path=fixture_db,
    )
    assert len(obs) == 1
    # FMP (fmp_normalized) beats IR doc (llm_extracted) regardless of id order.
    assert obs[0].value == pytest.approx(13.5)
