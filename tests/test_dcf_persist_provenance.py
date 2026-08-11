"""Focused DCF provenance persistence and idempotency tests."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

import pytest

from dcf.persist import DcfRunRow, upsert
from dcf.provenance import build_effective_provenance

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


def _row(repo_root: Path, *, snapshot: str = '{"wacc":0.09}') -> DcfRunRow:
    workbook = repo_root / "dcf" / "META.xlsx"
    workbook.parent.mkdir(exist_ok=True)
    if not workbook.exists():
        workbook.write_bytes(b"stable-workbook")
    income = repo_root / "data" / "META_income_statement.json"
    income.parent.mkdir(exist_ok=True)
    if not income.exists():
        income.write_text('{"revenue":1000}', encoding="utf-8")
    provenance = build_effective_provenance(
        ticker="META",
        repo_root=repo_root,
        workbook_path=workbook,
        assumption_snapshot_json=snapshot,
        engine_version="redesign_fcff_v1",
        source_paths=(("income_statement", income),),
        additional_inputs={
            "market_price": {
                "price": 90.0,
                "observed_at": "2026-07-28T12:00:00+00:00",
                "source": "fmp_quote",
            }
        },
    )
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
        assumption_snapshot_json=snapshot,
        notes="workbook=META.xlsx (redesigned)",
        assumptions_sync_status="synced",
        assumptions_synced_at=datetime(2026, 7, 28, 12, 1),
        provenance=provenance,
    )


def test_exact_provenance_retry_is_a_noop(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    row = _row(tmp_path)

    assert upsert(conn, row, repo_root=tmp_path) is True
    assert upsert(conn, row, repo_root=tmp_path) is False

    assert conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0] == 1
    current = conn.execute(
        "SELECT input_sha256, workbook_sha256, engine_version, provenance_json "
        "FROM dcf_runs WHERE is_latest=1"
    ).fetchone()
    assert current is not None
    provenance = row.provenance
    assert provenance is not None
    assert current[0] == provenance.input_sha256
    assert current[1] == provenance.workbook_sha256
    assert current[2] == "redesign_fcff_v1"
    assert json.loads(str(current[3])) == provenance.detail
    inputs = conn.execute(
        "SELECT role, locator, sha256 FROM dcf_run_inputs ORDER BY role"
    ).fetchall()
    assert {input_row[0] for input_row in inputs} == {
        "calculation_workbook",
        "effective_assumptions",
        "income_statement",
        "market_price",
    }


def test_reused_hash_cannot_hide_a_changed_calculation(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    row = _row(tmp_path)
    assert upsert(conn, row, repo_root=tmp_path) is True

    changed = dataclasses.replace(row, npv=1_200.0, npv_per_share=120.0)
    assert upsert(conn, changed, repo_root=tmp_path) is True

    versions = conn.execute(
        "SELECT npv_per_share, is_latest, superseded_by_id FROM dcf_runs ORDER BY id"
    ).fetchall()
    assert versions[0][0:2] == (100.0, 0)
    assert versions[0][2] == 2
    assert versions[1] == (120.0, 1, None)


def test_tampered_input_hash_is_rejected_before_write(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    row = _row(tmp_path)
    assert row.provenance is not None
    tampered = dataclasses.replace(
        row,
        provenance=dataclasses.replace(row.provenance, input_sha256="e" * 64),
    )

    with pytest.raises(ValueError, match="canonical commitments"):
        upsert(conn, tampered, repo_root=tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0] == 0


def test_current_schema_rejects_latest_run_without_provenance(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)

    with pytest.raises(ValueError, match="latest DCF requires input provenance"):
        upsert(conn, dataclasses.replace(_row(tmp_path), provenance=None), repo_root=tmp_path)

    assert conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0] == 0


def test_partial_governance_schema_without_input_ledger_fails_closed(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA.split("CREATE TABLE dcf_run_inputs", maxsplit=1)[0])

    with pytest.raises(sqlite3.OperationalError, match="governance schema is incomplete"):
        upsert(conn, _row(tmp_path), repo_root=tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0] == 0


def test_current_schema_rejects_missing_workbook_hash(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    row = _row(tmp_path)
    provenance = row.provenance
    assert provenance is not None

    with pytest.raises(ValueError, match="workbook SHA-256"):
        upsert(
            conn,
            dataclasses.replace(
                row, provenance=dataclasses.replace(provenance, workbook_sha256=None)
            ),
            repo_root=tmp_path,
        )


def test_current_schema_rejects_empty_input_ledger(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    row = _row(tmp_path)
    provenance = row.provenance
    assert provenance is not None

    with pytest.raises(ValueError, match="input ledger"):
        upsert(
            conn,
            dataclasses.replace(row, provenance=dataclasses.replace(provenance, detail={})),
            repo_root=tmp_path,
        )


def test_invalid_source_rolls_back_with_the_parent_transaction(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    row = _row(tmp_path)
    provenance = row.provenance
    assert provenance is not None and provenance.detail is not None
    detail = dict(provenance.detail)
    raw_sources = cast("list[object]", detail["sources"])
    sources = [
        dict(cast("dict[str, object]", source))
        for source in raw_sources
        if isinstance(source, dict)
    ]
    sources[0]["sha256"] = "not-a-sha"
    detail["sources"] = sources
    invalid = dataclasses.replace(
        row,
        provenance=dataclasses.replace(provenance, detail=detail),
    )

    with pytest.raises(ValueError, match="invalid SHA-256"):
        upsert(conn, invalid, repo_root=tmp_path)
    conn.rollback()

    assert conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM dcf_run_inputs").fetchone()[0] == 0


def test_duplicate_ledger_identity_is_rejected_before_supersede(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    current = _row(tmp_path)
    assert upsert(conn, current, repo_root=tmp_path) is True
    provenance = current.provenance
    assert provenance is not None and provenance.detail is not None
    detail = dict(provenance.detail)
    raw_sources = cast("list[object]", detail["sources"])
    sources = [
        dict(cast("dict[str, object]", source))
        for source in raw_sources
        if isinstance(source, dict)
    ]
    sources.append(dict(sources[0]))
    detail["sources"] = sources
    duplicate_inputs = dataclasses.replace(
        current,
        npv=1_100.0,
        provenance=dataclasses.replace(provenance, detail=detail),
    )

    with pytest.raises(ValueError, match="duplicate source identity"):
        upsert(conn, duplicate_inputs, repo_root=tmp_path)

    assert conn.execute("SELECT COUNT(*) FROM dcf_runs WHERE is_latest=1").fetchone()[0] == 1
    assert conn.execute("SELECT npv FROM dcf_runs WHERE is_latest=1").fetchone()[0] == 1_000.0
