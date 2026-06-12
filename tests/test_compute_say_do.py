"""Tests for src/compute/say_do.py — Commitment persistence + outcome matching."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal

import pytest

from compute.say_do import (
    CommitmentExtractionManifest,
    CommitmentInput,
    CommitmentOutcome,
    _classify_outcome,
    fetch_pending_commitments,
    match_pending,
    persist_commitment,
    persist_manifest,
)
from compute.thesis_evaluator import Comparator
from models.facts import Unit


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE transcripts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            period_end TIMESTAMP
        );
        CREATE TABLE transcript_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transcript_id INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            text TEXT NOT NULL
        );
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
            period_end TIMESTAMP NOT NULL,
            fiscal_period_type TEXT NOT NULL,
            kpi_definition_id INTEGER NOT NULL,
            value NUMERIC(24, 6) NOT NULL,
            unit TEXT NOT NULL,
            source_doc_id INTEGER NOT NULL
        );
        CREATE TABLE management_commitments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            period_made TIMESTAMP NOT NULL,
            transcript_segment_id INTEGER NOT NULL,
            period_target TIMESTAMP NOT NULL,
            kpi_name TEXT NOT NULL,
            comparator TEXT NOT NULL,
            target_value NUMERIC(24, 6) NOT NULL,
            unit TEXT NOT NULL,
            narrative TEXT NOT NULL,
            realized_value NUMERIC(24, 6),
            realized_doc_id INTEGER,
            outcome TEXT,
            evaluated_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    conn.commit()


def _seed_segment(conn: sqlite3.Connection, ticker: str = "X") -> int:
    cur = conn.execute(
        "INSERT INTO transcripts (document_id, ticker, period_end) VALUES (1, ?, ?)",
        (ticker, datetime(2024, 9, 30)),
    )
    transcript_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
    cur = conn.execute(
        "INSERT INTO transcript_segments (transcript_id, seq, text) VALUES (?, 0, 'mgmt said X')",
        (transcript_id,),
    )
    conn.commit()
    return int(cur.lastrowid) if cur.lastrowid is not None else 0


def _seed_kpi_fact(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    kpi_name: str,
    value: Decimal,
    period_end: datetime,
) -> None:
    conn.execute(
        "INSERT INTO kpi_definitions (ticker, name, unit, primary_source) VALUES (?, ?, 'percent', 'fmp')",
        (ticker, kpi_name),
    )
    kpi_id = conn.execute(
        "SELECT id FROM kpi_definitions WHERE ticker=? AND name=?", (ticker, kpi_name)
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO kpi_facts "
        "(ticker, period_end, fiscal_period_type, kpi_definition_id, value, unit, source_doc_id) "
        "VALUES (?, ?, 'Q4', ?, ?, 'percent', 1)",
        (ticker, period_end, kpi_id, str(value)),
    )
    conn.commit()


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _create_schema(c)
    return c


def test_classify_outcome_ge_hit() -> None:
    """target=10 ge, realized=11 -> HIT (1/10 over = 10%, but 10% threshold for BEAT means >=10% so it's BEAT)."""
    # Boundary: exactly 10% over -> BEAT
    assert (
        _classify_outcome(comparator=Comparator.GE, target=Decimal("10"), realized=Decimal("11"))
        == CommitmentOutcome.BEAT
    )
    # 5% over -> HIT
    assert (
        _classify_outcome(comparator=Comparator.GE, target=Decimal("10"), realized=Decimal("10.5"))
        == CommitmentOutcome.HIT
    )


def test_classify_outcome_ge_miss() -> None:
    assert (
        _classify_outcome(comparator=Comparator.GE, target=Decimal("10"), realized=Decimal("9"))
        == CommitmentOutcome.MISS
    )


def test_classify_outcome_lt_inverted() -> None:
    """LT/LE: realized below target by >=10% => BEAT, below but <10% => HIT, above => MISS."""
    assert (
        _classify_outcome(comparator=Comparator.LT, target=Decimal("10"), realized=Decimal("11"))
        == CommitmentOutcome.MISS
    )
    assert (
        _classify_outcome(comparator=Comparator.LT, target=Decimal("10"), realized=Decimal("9.5"))
        == CommitmentOutcome.HIT
    )
    assert (
        _classify_outcome(comparator=Comparator.LT, target=Decimal("10"), realized=Decimal("8"))
        == CommitmentOutcome.BEAT
    )


def test_classify_outcome_eq_tolerance() -> None:
    """EQ: |realized - target| <= 5% of target -> HIT, else MISS."""
    assert (
        _classify_outcome(comparator=Comparator.EQ, target=Decimal("100"), realized=Decimal("103"))
        == CommitmentOutcome.HIT
    )
    assert (
        _classify_outcome(comparator=Comparator.EQ, target=Decimal("100"), realized=Decimal("110"))
        == CommitmentOutcome.MISS
    )


def test_persist_commitment_writes_row(conn: sqlite3.Connection) -> None:
    seg = _seed_segment(conn, "MELI")
    commitment = CommitmentInput(
        ticker="MELI",
        period_made=datetime(2024, 9, 30),
        transcript_segment_id=seg,
        period_target=datetime(2024, 12, 31),
        kpi_name="Operating Margin",
        comparator=Comparator.GE,
        target_value=Decimal("13"),
        unit=Unit.PERCENT,
        narrative="we expect mid-teens margins by Q4",
    )
    cid = persist_commitment(conn, commitment=commitment)
    conn.commit()
    assert cid > 0
    row = conn.execute("SELECT * FROM management_commitments WHERE id = ?", (cid,)).fetchone()
    assert dict(row)["kpi_name"] == "Operating Margin"
    assert dict(row)["outcome"] is None  # not yet matched


def test_persist_commitment_rejects_unknown_segment(conn: sqlite3.Connection) -> None:
    """Unknown transcript_segment_id raises rather than silently dropping."""
    commitment = CommitmentInput(
        ticker="X",
        period_made=datetime(2024, 9, 30),
        transcript_segment_id=99999,
        period_target=datetime(2024, 12, 31),
        kpi_name="x",
        comparator=Comparator.GE,
        target_value=Decimal("1"),
        unit=Unit.PERCENT,
        narrative="x",
    )
    with pytest.raises(ValueError, match="does not exist"):
        persist_commitment(conn, commitment=commitment)


def test_persist_manifest_handles_multiple(conn: sqlite3.Connection) -> None:
    seg = _seed_segment(conn, "MELI")
    manifest = CommitmentExtractionManifest(
        commitments=[
            CommitmentInput(
                ticker="MELI",
                period_made=datetime(2024, 9, 30),
                transcript_segment_id=seg,
                period_target=datetime(2024, 12, 31),
                kpi_name="A",
                comparator=Comparator.GE,
                target_value=Decimal("10"),
                unit=Unit.PERCENT,
                narrative="a",
            ),
            CommitmentInput(
                ticker="MELI",
                period_made=datetime(2024, 9, 30),
                transcript_segment_id=seg,
                period_target=datetime(2024, 12, 31),
                kpi_name="B",
                comparator=Comparator.LT,
                target_value=Decimal("5"),
                unit=Unit.PERCENT,
                narrative="b",
            ),
        ]
    )
    ids = persist_manifest(conn, manifest)
    assert len(ids) == 2


def test_match_pending_marks_no_data_when_kpi_missing(conn: sqlite3.Connection) -> None:
    """If kpi_facts has no row for the period, outcome=NO_DATA."""
    seg = _seed_segment(conn, "X")
    persist_commitment(
        conn,
        commitment=CommitmentInput(
            ticker="X",
            period_made=datetime(2024, 9, 30),
            transcript_segment_id=seg,
            period_target=datetime(2024, 12, 31),
            kpi_name="Missing KPI",
            comparator=Comparator.GE,
            target_value=Decimal("10"),
            unit=Unit.PERCENT,
            narrative="x",
        ),
    )
    conn.commit()
    results = match_pending(conn, ticker="X")
    assert len(results) == 1
    assert results[0].outcome == CommitmentOutcome.NO_DATA


def test_match_pending_full_cycle(conn: sqlite3.Connection) -> None:
    """End-to-end: persist -> match -> outcome written back -> no longer pending."""
    # Create saydo_historical_metrics table in-memory for testing
    conn.execute(
        """
        CREATE TABLE saydo_historical_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            period_made TIMESTAMP NOT NULL,
            period_target TIMESTAMP NOT NULL,
            kpi_name TEXT NOT NULL,
            comparator TEXT NOT NULL,
            target_value NUMERIC(24, 6) NOT NULL,
            realized_value NUMERIC(24, 6),
            outcome TEXT NOT NULL,
            guidance_narrative TEXT NOT NULL,
            realized_narrative TEXT,
            evaluated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    seg = _seed_segment(conn, "MELI")
    _seed_kpi_fact(
        conn,
        ticker="MELI",
        kpi_name="Operating Margin",
        value=Decimal("13.5"),
        period_end=datetime(2024, 12, 31),
    )
    persist_commitment(
        conn,
        commitment=CommitmentInput(
            ticker="MELI",
            period_made=datetime(2024, 9, 30),
            transcript_segment_id=seg,
            period_target=datetime(2024, 12, 31),
            kpi_name="Operating Margin",
            comparator=Comparator.GE,
            target_value=Decimal("13"),
            unit=Unit.PERCENT,
            narrative="we expect mid-teens margins by Q4",
        ),
    )
    conn.commit()

    results = match_pending(conn, ticker="MELI")
    assert len(results) == 1
    assert results[0].outcome == CommitmentOutcome.HIT
    assert results[0].realized_value == Decimal("13.5")

    # Verify historical ledger persistence
    hist_row = conn.execute("SELECT * FROM saydo_historical_metrics WHERE ticker='MELI'").fetchone()
    assert hist_row is not None
    assert dict(hist_row)["kpi_name"] == "Operating Margin"
    assert Decimal(str(dict(hist_row)["realized_value"])) == Decimal("13.5")
    assert dict(hist_row)["outcome"] == "hit"

    # Re-running should be idempotent — outcome already set, nothing pending
    pending = fetch_pending_commitments(conn, ticker="MELI")
    assert pending == []
