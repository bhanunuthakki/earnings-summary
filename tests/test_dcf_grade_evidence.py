"""Fail-closed evidence projection for the latest persisted DCF run."""

from __future__ import annotations

import json
import sqlite3

import pytest

from dcf.grade_evidence import load_dcf_grade_evidence


def _schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE dcf_runs (
            id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            created_at TEXT,
            valuation_date TEXT,
            engine_version TEXT,
            input_sha256 TEXT,
            workbook_sha256 TEXT,
            inputs_as_of TEXT,
            live_price REAL,
            live_price_at TEXT,
            npv_per_share REAL,
            over_under_pct REAL,
            sanity_flag TEXT,
            assumption_snapshot_json TEXT,
            provenance_json TEXT,
            is_latest INTEGER,
            segment_name TEXT
        )
        """
    )


def test_generic_receipts_are_projected_without_regrading_them() -> None:
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    observed_at = "2026-08-26T03:32:46+00:00"
    snapshot: dict[str, object] = {
        "format": "redesign",
        "scenarios": {"bull": {}, "base": {}, "bear": {}},
        "priced_in": {"method": "reverse"},
    }
    provenance = {
        "sources": [{"role": "income_statement"}, {"role": "calculation_workbook"}],
        "market_price": {"price": 570.05, "observed_at": observed_at, "source": "yfinance"},
        "primary_fact_overlay": {"status": "ok"},
        "equity_bridge_receipt": {"status": "verified"},
        "country_risk_context": {"authority": "systematic_default_zero"},
    }
    conn.execute(
        """
        INSERT INTO dcf_runs VALUES (
            1, 'META', '2026-08-26T03:33:00', '2026-08-26', 'redesign_fcff_v1',
            ?, ?, '2026-08-26T03:32:46+00:00', 570.05, ?, 427.77, 0.3326,
            NULL, ?, ?, 1, NULL
        )
        """,
        ("a" * 64, "b" * 64, observed_at, json.dumps(snapshot), json.dumps(provenance)),
    )

    evidence = load_dcf_grade_evidence(conn, "meta")

    assert evidence.status == "available"
    assert evidence.checks is not None
    assert evidence.checks.input_hash_valid is True
    assert evidence.checks.workbook_hash_valid is True
    assert evidence.checks.scenario_receipt_present is True
    assert evidence.checks.reverse_receipt_present is True
    assert evidence.checks.primary_fact_overlay_status == "ok"
    assert evidence.checks.equity_bridge_status == "verified"
    assert evidence.checks.market_price_consistent is True


def test_specialized_receipts_mark_fcff_only_checks_not_applicable() -> None:
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    snapshot: dict[str, object] = {
        "model": "platform_dcf",
        "scenarios": {"bull": {}, "base": {}, "bear": {}},
        "reverse_valuation": {"archetype": "platform_dcf"},
    }
    provenance = {
        "sources": [{"role": "owner_assumptions"}],
        "market_price": {"price": 14.39, "observed_at": None, "source": "assumption_seed"},
    }
    conn.execute(
        "INSERT INTO dcf_runs VALUES (1,'NU','2026-08-26','2026-08-26',"
        "'nu_platform_fcfe_v1',?,?,NULL,14.39,NULL,22.10,-0.35,NULL,?,?,1,NULL)",
        ("a" * 64, "b" * 64, json.dumps(snapshot), json.dumps(provenance)),
    )

    evidence = load_dcf_grade_evidence(conn, "NU")

    assert evidence.checks is not None
    assert evidence.checks.primary_fact_overlay_status == "not_applicable"
    assert evidence.checks.equity_bridge_status == "not_applicable"
    assert evidence.checks.scenario_receipt_present is True
    assert evidence.checks.reverse_receipt_present is True


def test_missing_provenance_columns_fail_closed() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE dcf_runs (id INTEGER PRIMARY KEY, ticker TEXT)")

    evidence = load_dcf_grade_evidence(conn, "META")

    assert evidence.status == "invalid"
    assert "provenance_json" in evidence.missing_columns


def test_malformed_required_json_fails_closed_at_top_level() -> None:
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    conn.execute(
        "INSERT INTO dcf_runs VALUES (1,'META','2026-08-26','2026-08-26',"
        "'redesign_fcff_v1',?,?,NULL,10.0,NULL,12.0,-0.2,NULL,?,?,1,NULL)",
        ("a" * 64, "b" * 64, "{bad", "{bad"),
    )

    evidence = load_dcf_grade_evidence(conn, "META")

    assert evidence.status == "invalid"
    assert evidence.invalid_reason == "assumption_snapshot_invalid,provenance_invalid"
    assert evidence.checks is None


def test_missing_required_json_fails_closed_at_top_level() -> None:
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    conn.execute(
        "INSERT INTO dcf_runs VALUES (1,'META','2026-08-26','2026-08-26',"
        "'redesign_fcff_v1',?,?,NULL,10.0,NULL,12.0,-0.2,NULL,NULL,NULL,1,NULL)",
        ("a" * 64, "b" * 64),
    )

    evidence = load_dcf_grade_evidence(conn, "META")

    assert evidence.status == "invalid"
    assert evidence.invalid_reason == "assumption_snapshot_missing,provenance_missing"


def test_blob_and_invalid_clock_row_drift_fail_closed() -> None:
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    snapshot: dict[str, object] = {"scenarios": {}, "priced_in": {}}
    provenance: dict[str, object] = {"sources": []}
    conn.execute(
        "INSERT INTO dcf_runs VALUES (1,'META',?,'not-a-date','redesign_fcff_v1',"
        "?,?,NULL,10.0,NULL,12.0,-0.2,NULL,?,?,1,NULL)",
        (
            sqlite3.Binary(b"bad"),
            "a" * 64,
            "b" * 64,
            json.dumps(snapshot),
            json.dumps(provenance),
        ),
    )

    evidence = load_dcf_grade_evidence(conn, "META")

    assert evidence.status == "invalid"
    assert evidence.invalid_reason == "row_decode_failed"


def test_query_failure_returns_typed_invalid_evidence() -> None:
    conn = sqlite3.connect(":memory:")
    _schema(conn)

    def deny_dcf_reads(
        action: int,
        arg1: str | None,
        _arg2: str | None,
        _database: str | None,
        _trigger: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_READ and arg1 == "dcf_runs":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(deny_dcf_reads)

    evidence = load_dcf_grade_evidence(conn, "META")

    assert evidence.status == "invalid"
    assert evidence.invalid_reason == "row_query_failed"


@pytest.mark.parametrize("invalid_number", (float("inf"), float("-inf")))
def test_nonfinite_financial_numbers_fail_closed(invalid_number: float) -> None:
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    snapshot: dict[str, object] = {"scenarios": {}, "priced_in": {}}
    provenance: dict[str, object] = {"sources": []}
    conn.execute(
        "INSERT INTO dcf_runs VALUES (1,'META','2026-08-26','2026-08-26',"
        "'redesign_fcff_v1',?,?,NULL,?,NULL,12.0,-0.2,NULL,?,?,1,NULL)",
        (
            "a" * 64,
            "b" * 64,
            invalid_number,
            json.dumps(snapshot),
            json.dumps(provenance),
        ),
    )

    evidence = load_dcf_grade_evidence(conn, "META")

    assert evidence.status == "invalid"
    assert evidence.invalid_reason == "row_decode_failed"


def test_nonfinite_nested_json_evidence_fails_closed() -> None:
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    conn.execute(
        "INSERT INTO dcf_runs VALUES (1,'META','2026-08-26','2026-08-26',"
        "'redesign_fcff_v1',?,?,NULL,10.0,NULL,12.0,-0.2,NULL,?,?,1,NULL)",
        ("a" * 64, "b" * 64, '{"scenario":1e309}', '{"sources":[]}'),
    )

    evidence = load_dcf_grade_evidence(conn, "META")

    assert evidence.status == "invalid"
    assert evidence.invalid_reason == "assumption_snapshot_invalid"
