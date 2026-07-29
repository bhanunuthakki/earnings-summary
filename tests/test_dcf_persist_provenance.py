"""Focused DCF provenance persistence and idempotency tests."""

from __future__ import annotations

import dataclasses
import sqlite3
from datetime import UTC, date, datetime

import pytest

from dcf.persist import DcfRunRow, upsert
from dcf.provenance import DcfInputProvenance

_SCHEMA = """
CREATE TABLE dcf_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    segment_name TEXT,
    valuation_date TEXT,
    horizon_years INTEGER,
    wacc REAL,
    terminal_growth REAL,
    npv REAL,
    npv_per_share REAL,
    shares_outstanding REAL,
    currency TEXT,
    notes TEXT,
    run_id TEXT,
    live_price REAL,
    live_price_at TEXT,
    over_under_pct REAL,
    mos_bar_used REAL,
    assumption_snapshot_json TEXT,
    revenue_growths_json TEXT,
    fcf_margin REAL,
    assumptions_sync_status TEXT,
    assumptions_synced_at TEXT,
    sanity_flag TEXT,
    is_latest INTEGER NOT NULL DEFAULT 1,
    superseded_at TEXT,
    superseded_by_id INTEGER,
    input_sha256 TEXT,
    workbook_sha256 TEXT,
    engine_version TEXT,
    inputs_as_of TEXT,
    provenance_json TEXT
);
CREATE TABLE dcf_run_inputs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dcf_run_id INTEGER NOT NULL REFERENCES dcf_runs(id) ON DELETE RESTRICT,
    role TEXT NOT NULL,
    locator TEXT NOT NULL,
    sha256 TEXT,
    byte_size INTEGER,
    observed_at TEXT,
    detail_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(dcf_run_id, role, locator)
);
"""


def _row() -> DcfRunRow:
    return DcfRunRow(
        ticker="META",
        valuation_date=date(2026, 7, 28),
        horizon_years=10,
        wacc=0.09,
        npv=1_000.0,
        npv_per_share=100.0,
        shares_outstanding=10_000_000.0,
        currency="USD",
        live_price=90.0,
        live_price_at=datetime(2026, 7, 28, 12, 0, tzinfo=UTC),
        mos_bar_used=0.2,
        assumption_snapshot_json='{"wacc":0.09}',
        notes="workbook=META.xlsx (redesigned)",
        assumptions_sync_status="synced",
        assumptions_synced_at=datetime(2026, 7, 28, 12, 1),
        provenance=DcfInputProvenance(
            input_sha256="a" * 64,
            workbook_sha256="b" * 64,
            engine_version="redesign_fcff_v1",
            inputs_as_of=datetime(2026, 7, 28, 12, 0, tzinfo=UTC),
            detail={
                "sources": [
                    {
                        "role": "income_statement",
                        "path": "data/META_income_statement.json",
                        "sha256": "c" * 64,
                        "bytes": 123,
                        "observed_at": "2026-07-28T11:59:00+00:00",
                    }
                ],
                "market_price": {
                    "price": 90.0,
                    "observed_at": "2026-07-28T12:00:00+00:00",
                    "source": "fmp_quote",
                },
            },
        ),
    )


def test_exact_provenance_retry_is_a_noop() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    row = _row()

    assert upsert(conn, row) is True
    assert upsert(conn, row) is False

    count = conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0]
    assert count == 1
    current = conn.execute(
        "SELECT input_sha256, workbook_sha256, engine_version, provenance_json "
        "FROM dcf_runs WHERE is_latest=1"
    ).fetchone()
    assert current == (
        "a" * 64,
        "b" * 64,
        "redesign_fcff_v1",
        '{"market_price":{"observed_at":"2026-07-28T12:00:00+00:00",'
        '"price":90.0,"source":"fmp_quote"},"sources":[{"bytes":123,'
        '"observed_at":"2026-07-28T11:59:00+00:00",'
        '"path":"data/META_income_statement.json","role":"income_statement",'
        '"sha256":"' + ("c" * 64) + '"}]}',
    )
    inputs = conn.execute(
        "SELECT role, locator, sha256, byte_size, observed_at FROM dcf_run_inputs ORDER BY role"
    ).fetchall()
    assert inputs == [
        (
            "income_statement",
            "data/META_income_statement.json",
            "c" * 64,
            123,
            "2026-07-28T11:59:00+00:00",
        ),
        (
            "market_price",
            "fmp_quote",
            None,
            None,
            "2026-07-28T12:00:00+00:00",
        ),
    ]


def test_reused_hash_cannot_hide_a_changed_calculation() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    row = _row()
    assert upsert(conn, row) is True

    changed = dataclasses.replace(row, npv=1_200.0, npv_per_share=120.0)
    assert upsert(conn, changed) is True

    versions = conn.execute(
        "SELECT npv_per_share, is_latest, superseded_by_id FROM dcf_runs ORDER BY id"
    ).fetchall()
    assert versions[0][0:2] == (100.0, 0)
    assert versions[0][2] == 2
    assert versions[1] == (120.0, 1, None)
    assert conn.execute("SELECT COUNT(*) FROM dcf_run_inputs").fetchone()[0] == 4


def test_provenance_none_keeps_existing_callers_working() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)

    assert upsert(conn, dataclasses.replace(_row(), provenance=None)) is True

    assert conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM dcf_run_inputs").fetchone()[0] == 0


def test_invalid_source_rolls_back_with_the_parent_transaction() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    provenance = _row().provenance
    assert provenance is not None
    invalid = dataclasses.replace(
        _row(),
        provenance=dataclasses.replace(
            provenance,
            detail={
                "sources": [
                    {
                        "role": "income_statement",
                        "path": "data/source.json",
                        "sha256": "not-a-sha",
                    }
                ]
            },
        ),
    )

    with pytest.raises(ValueError, match="invalid SHA-256"):
        upsert(conn, invalid)
    conn.rollback()

    assert conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM dcf_run_inputs").fetchone()[0] == 0


def test_input_ledger_constraint_failure_preserves_the_current_run() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    current = _row()
    assert upsert(conn, current) is True
    provenance = current.provenance
    assert provenance is not None
    source = {
        "role": "income_statement",
        "path": "data/duplicate.json",
        "sha256": "d" * 64,
    }
    duplicate_inputs = dataclasses.replace(
        current,
        npv=1_100.0,
        provenance=dataclasses.replace(
            provenance,
            input_sha256="e" * 64,
            detail={"sources": [source, source]},
        ),
    )

    with pytest.raises(sqlite3.IntegrityError):
        upsert(conn, duplicate_inputs)

    assert conn.execute("SELECT COUNT(*) FROM dcf_runs WHERE is_latest=1").fetchone()[0] == 1
    assert conn.execute("SELECT npv FROM dcf_runs WHERE is_latest=1").fetchone()[0] == 1_000.0
    assert conn.execute("SELECT COUNT(*) FROM dcf_run_inputs").fetchone()[0] == 2
