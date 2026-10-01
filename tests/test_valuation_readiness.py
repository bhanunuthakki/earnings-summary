"""Readiness must not infer issuer evidence from quotes or file modification times."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dcf.provenance import build_file_provenance
from dcf.readiness import load_valuation_readiness

NOW = datetime(2026, 10, 1, 20, tzinfo=UTC)


def _db(provenance: dict[str, object] | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE dcf_runs (
        id INTEGER, ticker TEXT, created_at TEXT, valuation_date TEXT, engine_version TEXT,
        input_sha256 TEXT, workbook_sha256 TEXT, inputs_as_of TEXT, live_price REAL,
        live_price_at TEXT, npv_per_share REAL, over_under_pct REAL, sanity_flag TEXT,
        assumption_snapshot_json TEXT, provenance_json TEXT, is_latest INTEGER, segment_name TEXT
    )""")
    conn.execute(
        "INSERT INTO dcf_runs VALUES (1,'META',?,'2026-06-01','redesign_fcff_v1',?,?,?,?,?,?,?,?,?,?,1,NULL)",
        (
            "2026-06-01T20:00:00+00:00",
            "a" * 64,
            "b" * 64,
            NOW.isoformat(),
            700,
            NOW.isoformat(),
            600,
            0.1,
            None,
            "{}",
            json.dumps(provenance or {}),
        ),
    )
    conn.commit()
    return conn


def test_recent_quote_does_not_make_old_or_unproven_financial_inputs_ready() -> None:
    conn = _db({"sources": [{"role": "income_statement", "observed_at": NOW.isoformat()}]})
    receipt = load_valuation_readiness(conn, "meta", as_of=NOW)
    assert not receipt.ready
    assert receipt.market_status == "current"
    assert receipt.valuation_date == "2026-06-01"
    assert "financial_input_completeness_unverified" in receipt.reason_codes
    assert receipt.financial_period_end is None
    assert receipt.assumption_reviewed_at is None
    assert conn.row_factory is None
    assert not conn.in_transaction


def test_missing_model_and_failed_schema_are_distinct() -> None:
    conn = _db()
    assert load_valuation_readiness(conn, "UNKNOWN", as_of=NOW).status == "missing"
    conn.execute("DROP TABLE dcf_runs")
    assert load_valuation_readiness(conn, "META", as_of=NOW).status == "failed"


def test_future_market_timestamp_is_not_current() -> None:
    conn = _db()
    conn.execute("UPDATE dcf_runs SET live_price_at='2026-10-02T20:00:00+00:00'")
    receipt = load_valuation_readiness(conn, "META", as_of=NOW)
    assert receipt.market_status == "invalid"
    assert "market_timestamp_after_cutoff" in receipt.reason_codes


def test_file_clock_is_explicitly_not_financial_period_or_admission(tmp_path: Path) -> None:
    source = tmp_path / "income.json"
    source.write_text('{"period_end":"2024-12-31"}')
    modified = datetime(2025, 1, 1, tzinfo=UTC)
    os.utime(source, (modified.timestamp(), modified.timestamp()))
    proof = build_file_provenance(
        ticker="META",
        repo_root=tmp_path,
        workbook_path=tmp_path / "none.xlsx",
        engine_version="example",
        effective_inputs={},
        assumption_snapshot={},
        live_price=700,
        live_price_at=NOW,
        live_price_source="test",
        source_files=[(source, "income_statement")],
    )
    assert proof.inputs_as_of == NOW  # historical cutoff semantics retained
    assert proof.detail is not None
    clocks = proof.detail["input_clocks"]
    assert isinstance(clocks, dict)
    assert clocks["non_market_latest_observed_at"] == modified.isoformat()
    assert clocks["market_observed_at"] == NOW.isoformat()
    assert clocks["financial_period_end"] is None
    assert clocks["financial_input_completeness"] == "unverified"
    sources = proof.detail["sources"]
    assert isinstance(sources, list)
    assert sources[0]["clock_kind"] == "file_modified"


def test_naive_cutoff_rejected() -> None:
    with pytest.raises(ValueError, match="timezone"):
        load_valuation_readiness(_db(), "META", as_of=NOW.replace(tzinfo=None))


def test_unavailable_referenced_observation_is_reported_not_replaced() -> None:
    conn = _db(
        {
            "primary_fact_overlay": {
                "statements": {
                    "income": {
                        "applied": [
                            {
                                "reported_observation_id": "missing-used-observation",
                                "period_end": "2025-12-31",
                                "line_item": "revenue",
                            }
                        ]
                    }
                }
            }
        }
    )
    result = load_valuation_readiness(conn, "META", as_of=NOW)
    assert not result.ready
    assert result.financial_inputs[0].observation_id == "missing-used-observation"
    assert result.financial_inputs[0].status == "failed"  # absent fact schema, not no observation
    assert result.financial_period_end is None  # unverified manifest date is not admitted evidence


def test_only_run_referenced_canonical_observations_are_admitted(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    from sources.discovery_financials import read_financial_history
    from tests.test_canonical_growth_screen import seed_growth_graph

    db = migrated_db(tmp_path / "readiness.db")
    with sqlite3.connect(db) as conn:
        seed_growth_graph(conn, tmp_path)
        history = read_financial_history(conn, "WIX", as_of=NOW.date())
        assert history.references
        used = min(history.references, key=lambda item: item.period_end)
        newer = max(history.references, key=lambda item: item.period_end)
        assert used.period_end < newer.period_end
        proof = {
            "primary_fact_overlay": {
                "statements": {
                    "income": {
                        "applied": [
                            {
                                "reported_observation_id": used.observation_id,
                                "period_end": used.period_end.isoformat(),
                            }
                        ]
                    }
                }
            }
        }
        conn.execute(
            """INSERT INTO dcf_runs (
            ticker, created_at, valuation_date, engine_version, input_sha256,
            workbook_sha256, inputs_as_of, live_price, live_price_at, npv_per_share,
            assumption_snapshot_json, provenance_json, is_latest, horizon_years,
            revenue_growths_json, fcf_margin, wacc, terminal_growth, npv
        ) VALUES ('WIX',?,'2026-09-30','redesign_fcff_v1',?,?,?,100,?,120,'{}',?,1,10,'[]',0.2,0.1,0.03,120)""",
            (
                "2026-09-30T20:00:00+00:00",
                "a" * 64,
                "b" * 64,
                NOW.isoformat(),
                NOW.isoformat(),
                json.dumps(proof),
            ),
        )
        conn.commit()
        result = load_valuation_readiness(conn, "WIX", as_of=NOW)
        assert result.financial_inputs[0].status == "admitted", result.financial_inputs[0]
        assert result.financial_period_end == used.period_end.isoformat()
        assert result.financial_period_end != newer.period_end.isoformat()
        assert not result.ready  # admission of one used input proves no full population
        assert result.financial_inputs[0].document_version_id
        conn.execute("UPDATE dcf_runs SET ticker='META'")
        wrong_issuer = load_valuation_readiness(conn, "META", as_of=NOW)
        assert wrong_issuer.financial_inputs[0].status == "semantic_gap"
        assert wrong_issuer.financial_inputs[0].reason_code == (
            "run_input_outside_ticker_reported_financial_slice"
        )
        assert wrong_issuer.financial_period_end is None
        conn.execute("UPDATE dcf_runs SET ticker='WIX'")
        before_capture = load_valuation_readiness(
            conn, "WIX", as_of=datetime(2026, 9, 18, 11, tzinfo=UTC)
        )
        assert before_capture.financial_inputs[0].status != "admitted"
        assert before_capture.financial_period_end is None
        conn.execute("UPDATE dcf_runs SET provenance_json='{}'")
        unrelated_only = load_valuation_readiness(conn, "WIX", as_of=NOW)
        assert unrelated_only.financial_period_end is None
        assert "financial_input_lineage_missing" in unrelated_only.reason_codes
        assert not unrelated_only.ready
        assert conn.in_transaction  # caller transaction is preserved


def test_closed_connection_is_failed_not_missing() -> None:
    conn = _db()
    conn.close()
    result = load_valuation_readiness(conn, "META", as_of=NOW)
    assert result.status == "failed"
