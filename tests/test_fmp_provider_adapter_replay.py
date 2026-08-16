"""Offline replay coverage for the sealed FMP provider-adapter corpus slice."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sources.fmp_replay import replay_fmp_adapter_corpus, validate_report_output_path

OBSERVED_AT = datetime(2026, 8, 15, tzinfo=UTC)


def _write(corpus: Path, name: str, payload: object) -> None:
    (corpus / name).write_text(json.dumps(payload), encoding="utf-8")


def _complete_corpus(corpus: Path) -> None:
    _write(
        corpus,
        "WIX_form_10k_2024.json",
        {"symbol": "WIX", "period": "FY", "year": 2024, "business": "text"},
    )
    _write(
        corpus,
        "WIX_analyst_estimates_quarterly.json",
        [{"symbol": "WIX", "date": "2026-09-30", "quarter": 3, "revenueAvg": 5}],
    )
    _write(
        corpus,
        "WIX_income_statement_annual.json",
        [{"symbol": "WIX", "reportedCurrency": "USD"}],
    )
    _write(
        corpus,
        "WIX_geo_segments_annual.json",
        [
            {
                "symbol": "WIX",
                "date": "2025-12-31",
                "fiscalYear": 2025,
                "period": "FY",
                "reportedCurrency": "USD",
                "data": {"North America": 4},
            }
        ],
    )
    _write(
        corpus,
        "WIX_product_segments_annual.json",
        [
            {
                "symbol": "WIX",
                "date": "2025-12-31",
                "fiscalYear": 2025,
                "period": "FY",
                "reportedCurrency": "USD",
                "data": {"Subscriptions": 5},
            }
        ],
    )
    _write(
        corpus,
        "WIX_price_chart_10y_div_adj.json",
        [{"date": "2026-08-14", "open": 1, "high": 2, "low": 1, "close": 2, "volume": 3}],
    )
    _write(corpus, "WIX_profile.json", [{"symbol": "WIX", "currency": "USD"}])


def test_replay_is_offline_deterministic_and_seals_companion_packets(tmp_path: Path) -> None:
    _complete_corpus(tmp_path)

    first = replay_fmp_adapter_corpus(tmp_path, observed_at=OBSERVED_AT)
    second = replay_fmp_adapter_corpus(
        tmp_path,
        observed_at=OBSERVED_AT,
        expected_manifest_sha256=first.corpus_manifest_sha256,
    )

    assert first == second
    assert first.selected_files == 5
    assert first.manifest_files == 7
    assert first.succeeded_files == 5
    assert first.failed_files == 0
    assert first.emitted_records == {"filing": 1, "estimate": 1, "segment": 2, "price": 1}
    assert first.corpus_manifest_sha256 != hashlib.sha256(b"WIX").hexdigest()


def test_replay_records_missing_companion_without_silent_admission(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "MELI_analyst_estimates_annual.json",
        [{"symbol": "MELI", "date": "2026-12-31", "revenueAvg": 5}],
    )

    report = replay_fmp_adapter_corpus(tmp_path, observed_at=OBSERVED_AT)

    assert report.selected_files == 1
    assert report.succeeded_files == 0
    assert report.failed_files == 1
    assert report.failures[0].relative_path == "MELI_analyst_estimates_annual.json"
    assert "currency companion" in report.failures[0].message


def test_replay_rejects_manifest_drift(tmp_path: Path) -> None:
    _complete_corpus(tmp_path)

    with pytest.raises(ValueError, match="manifest SHA-256"):
        replay_fmp_adapter_corpus(
            tmp_path,
            observed_at=OBSERVED_AT,
            expected_manifest_sha256="a" * 64,
        )


def test_replay_report_cannot_overwrite_corpus_or_existing_file(tmp_path: Path) -> None:
    _complete_corpus(tmp_path)
    report_root = tmp_path.parent / "reports"
    safe_report = report_root / "replay.json"
    safe_report.parent.mkdir()
    safe_report.write_text("old", encoding="utf-8")
    external_report = tmp_path.parent / "portfolio.db"

    with pytest.raises(ValueError, match="outside the corpus"):
        validate_report_output_path(tmp_path, report_root, tmp_path / "WIX_profile.json")
    with pytest.raises(ValueError, match="stay under the report root"):
        validate_report_output_path(tmp_path, report_root, external_report)
    with pytest.raises(ValueError, match="already exists"):
        validate_report_output_path(tmp_path, report_root, safe_report)
    assert (
        validate_report_output_path(tmp_path, report_root, safe_report, overwrite=True)
        == safe_report
    )


def test_replay_reports_close_only_price_packet_as_a_non_admission(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "QQQ_price_chart_10y_div_adj.json",
        [{"date": "2026-08-14", "adjClose": 1, "close": 1, "volume": 3}],
    )
    _write(tmp_path, "QQQ_profile.json", [{"symbol": "QQQ", "currency": "USD"}])

    report = replay_fmp_adapter_corpus(tmp_path, observed_at=OBSERVED_AT)

    assert report.succeeded_files == 0
    assert report.failed_files == 1
    assert report.failures[0].family == "price"
    assert report.failures[0].message == "FMP price open is required"
