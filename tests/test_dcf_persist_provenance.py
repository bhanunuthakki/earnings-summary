"""Focused DCF provenance persistence and idempotency tests."""

from __future__ import annotations

import dataclasses
import os
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

import pytest

from dcf.persist import DcfPromotionBlocked, DcfRunRow, upsert
from dcf.provenance import (
    DcfInputProvenance,
    build_file_provenance,
    build_file_source_record,
)

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


def _bridge_row(row: DcfRunRow, status: str) -> DcfRunRow:
    assert row.provenance is not None
    detail = dict(row.provenance.detail or {})
    detail["equity_bridge_receipt"] = {"status": status}
    return dataclasses.replace(row, provenance=dataclasses.replace(row.provenance, detail=detail))


def test_weaker_bridge_candidate_cannot_replace_verified_current() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    current = _bridge_row(_row(), "verified")
    assert upsert(conn, current) is True
    candidate = _bridge_row(dataclasses.replace(_row(), npv_per_share=101.0), "unverified")

    with pytest.raises(DcfPromotionBlocked) as blocked:
        upsert(conn, candidate)

    assert blocked.value.decision.reason == "candidate_equity_bridge_weaker_than_current"
    assert conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0] == 1
    assert conn.execute("SELECT npv_per_share FROM dcf_runs WHERE is_latest=1").fetchone()[0] == 100.0


def test_outlier_candidate_is_blocked_with_deterministic_evidence() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    candidate = dataclasses.replace(_row(), live_price=200.0)

    with pytest.raises(DcfPromotionBlocked) as blocked:
        upsert(conn, candidate)

    decision = blocked.value.decision
    assert decision.reason == "outlier_requires_explicit_owner_review"
    assert decision.candidate_evidence == {
        "ticker": "META",
        "engine_version": "redesign_fcff_v1",
        "input_sha256": "a" * 64,
        "workbook_sha256": "b" * 64,
        "inputs_as_of": "2026-07-28T12:00:00+00:00",
        "equity_bridge_status": "missing",
        "sanity_flag": "outlier",
    }
    assert conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0] == 0


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


def test_pacific_information_date_accepts_next_utc_day_before_midnight() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    row = _row()
    assert row.provenance is not None
    provenance = dataclasses.replace(
        row.provenance,
        inputs_as_of=datetime(2026, 8, 14, 2, 0, tzinfo=UTC),
    )
    boundary = dataclasses.replace(
        row,
        valuation_date=date(2026, 8, 13),
        provenance=provenance,
    )

    assert upsert(conn, boundary) is True


def test_pacific_information_date_rejects_later_local_day() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    row = _row()
    assert row.provenance is not None
    provenance = dataclasses.replace(
        row.provenance,
        inputs_as_of=datetime(2026, 8, 14, 8, 0, tzinfo=UTC),
    )
    boundary = dataclasses.replace(
        row,
        valuation_date=date(2026, 8, 13),
        provenance=provenance,
    )

    with pytest.raises(ValueError, match="later than the Pacific valuation date"):
        upsert(conn, boundary)


def test_pacific_information_date_rejects_naive_cutoff() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    row = _row()
    assert row.provenance is not None
    provenance = dataclasses.replace(
        row.provenance,
        inputs_as_of=datetime(2026, 8, 13, 17, 0),
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        upsert(conn, dataclasses.replace(row, provenance=provenance))


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


def test_generated_workbook_does_not_fake_the_input_cutoff(tmp_path: Path) -> None:
    source = tmp_path / "assumptions.json"
    workbook = tmp_path / "model.xlsx"
    source.write_text('{"growth":0.1}', encoding="utf-8")
    workbook.write_bytes(b"generated workbook")
    source_time = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
    workbook_time = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    os.utime(source, (source_time.timestamp(), source_time.timestamp()))
    os.utime(workbook, (workbook_time.timestamp(), workbook_time.timestamp()))

    provenance = build_file_provenance(
        ticker="META",
        repo_root=tmp_path,
        workbook_path=workbook,
        engine_version="test@1",
        effective_inputs={"growth": 0.1},
        assumption_snapshot={"growth": 0.1},
        live_price=None,
        live_price_at=None,
        live_price_source=None,
        source_files=((source, "owner_assumptions"),),
    )

    assert provenance.inputs_as_of == source_time
    assert provenance.detail is not None
    sources = provenance.detail["sources"]
    assert isinstance(sources, list)
    typed_sources = cast("list[dict[str, object]]", sources)
    by_role = {item["role"]: item for item in typed_sources}
    assert by_role["owner_assumptions"]["influences_calculation"] is True
    assert by_role["calculation_workbook"]["influences_calculation"] is False


def test_pre_overwrite_workbook_input_receipt_retains_the_exact_old_bytes(
    tmp_path: Path,
) -> None:
    workbook = tmp_path / "BN.xlsx"
    workbook.write_bytes(b"owner-edited yellow cells")
    source_time = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    os.utime(workbook, (source_time.timestamp(), source_time.timestamp()))
    receipt = build_file_source_record(workbook, role="owner_workbook_inputs", repo_root=tmp_path)
    assert receipt is not None

    workbook.write_bytes(b"new generated workbook")
    provenance = build_file_provenance(
        ticker="BN",
        repo_root=tmp_path,
        workbook_path=workbook,
        engine_version="holdco_sotp_v1",
        effective_inputs={"mark": 12.0},
        assumption_snapshot={"mark": 12.0},
        live_price=None,
        live_price_at=None,
        live_price_source=None,
        source_files=(),
        source_records=(receipt,),
    )

    assert provenance.inputs_as_of == source_time
    assert provenance.workbook_sha256 != receipt["sha256"]
    assert provenance.detail is not None
    sources = provenance.detail["sources"]
    assert isinstance(sources, list)
    typed_sources = cast("list[dict[str, object]]", sources)
    owner_input = next(item for item in typed_sources if item["role"] == "owner_workbook_inputs")
    assert owner_input["sha256"] == receipt["sha256"]
