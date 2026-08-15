"""Tests for source-regime cost attribution, canary corpus verification, and telemetry."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from execution.attribute_source_cost import verify_canary_corpus
from sources.telemetry import (
    SourceCostTelemetryAccumulator,
    SourceRegime,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "data" / "fmp_canary_manifest.json"
FMP_DIR = REPO_ROOT / "data" / "historical" / "fmp"


def test_fmp_canary_corpus_verification() -> None:
    if not MANIFEST_PATH.exists() or not FMP_DIR.exists():
        pytest.skip("Manifest or FMP historical directory missing")

    passed, errors, verified_count, total_bytes = verify_canary_corpus(MANIFEST_PATH, FMP_DIR)
    assert passed, f"Canary verification failed: {errors}"
    assert verified_count >= 100
    assert total_bytes > 0


def test_fmp_canary_manifest_fiscal_awareness() -> None:
    manifest_data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    files = manifest_data["files"]

    wix_files = [f for f in files if f["ticker"] == "WIX"]
    rbrk_files = [f for f in files if f["ticker"] == "RBRK"]

    assert len(wix_files) > 0
    assert len(rbrk_files) > 0

    # WIX should specify calendar Q1 2026 (2026-03-31)
    assert all(f["max_period_date"] == "2026-03-31" for f in wix_files)
    # RBRK should specify fiscal Q1 FY2027 (2026-04-30)
    assert all(f["max_period_date"] == "2026-04-30" for f in rbrk_files)


def test_canary_verification_failure_on_corrupt_file(tmp_path: Path) -> None:
    test_manifest = tmp_path / "test_manifest.json"
    dummy_file = tmp_path / "DUMMY_test.json"
    dummy_file.write_bytes(b"actual content")

    manifest_payload = {
        "version": 1,
        "files": [
            {
                "filename": "DUMMY_test.json",
                "ticker": "DUMMY",
                "size_bytes": 14,
                "sha256": "0" * 64,  # mismatched hash
                "max_period_date": "2026-03-31",
                "fiscal_period_description": "Test",
            }
        ],
    }
    test_manifest.write_text(json.dumps(manifest_payload), encoding="utf-8")

    passed, errors, _verified_count, _total_bytes = verify_canary_corpus(test_manifest, tmp_path)
    assert not passed
    assert len(errors) == 1
    assert "SHA-256 mismatch" in errors[0]


def test_telemetry_accumulator_and_redaction() -> None:
    accum = SourceCostTelemetryAccumulator(run_id="test_run_123")

    # Record event with secret in endpoint URL and notes
    accum.record(
        regime=SourceRegime.VENDOR_ONLY,
        provider="fmp",
        ticker="WIX",
        endpoint="https://api.fmp.com/v3/profile/WIX?apikey=SUPER_SECRET_12345",
        bytes_transferred=5000,
        latency_ms=150,
        provider_cost_usd=Decimal("0.01"),
        notes="Fetched with token=BEARER_SECRET_TOKEN_XYZ",
    )

    accum.record(
        regime=SourceRegime.SEC_PRIMARY,
        provider="sec",
        ticker="WIX",
        endpoint="https://data.sec.gov/api/companyfacts",
        bytes_transferred=20000,
        latency_ms=300,
        operator_time_seconds=Decimal("2.0"),
    )

    summary = accum.summarize()
    assert summary.events_count == 2
    assert summary.run_id == "test_run_123"
    assert summary.total_cost_usd == Decimal("0.01")

    # Verify secret redaction
    events = accum.events
    assert "SUPER_SECRET_12345" not in events[0].endpoint
    assert "BEARER_SECRET_TOKEN_XYZ" not in (events[0].notes or "")

    vendor_summary = summary.regimes[SourceRegime.VENDOR_ONLY]
    assert vendor_summary.total_calls == 1
    assert vendor_summary.total_bytes == 5000
    assert vendor_summary.total_provider_cost_usd == Decimal("0.01")
