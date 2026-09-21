"""Valuation labels cannot turn annual or realized inputs into NTM consensus."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest

from compute.valuation_basis import extract_for_ticker
from report.render_clock import fixed_render_clock


def seed_inputs(root: Path) -> None:
    cache = root / "data/historical/fmp"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "WIX_key_metrics_quarterly.json").write_text(
        json.dumps(
            [
                {"date": "2026-06-30", "marketCap": 500, "peRatio": 10},
                {"date": "2026-03-31", "marketCap": 400, "peRatio": 8},
            ]
        )
    )
    (cache / "WIX_income_statement_quarterly.json").write_text(
        json.dumps(
            [
                {
                    "symbol": "WIX",
                    "date": "2026-06-30",
                    "reportedCurrency": "USD",
                    "netIncome": 10,
                    "eps": 1,
                },
            ]
        )
    )
    (cache / "WIX_analyst_estimates_annual.json").write_text(
        json.dumps(
            [
                {
                    "symbol": "WIX",
                    "date": "2026-12-31",
                    "netIncomeAvg": 25,
                    "epsAvg": 2,
                    "ebitdaAvg": 50,
                },
                {"symbol": "WIX", "date": "2027-12-31", "netIncomeAvg": 30, "epsAvg": 3},
            ]
        )
    )
    thesis = root / "micro_thesis/holdings/WIX.json"
    thesis.parent.mkdir(parents=True, exist_ok=True)
    thesis.write_text(json.dumps({"valuation_multiple_override": "P/E (NTM)", "thesis": "Fixture"}))


def test_uncaptured_annual_estimate_never_presents_current_ntm(tmp_path: Path) -> None:
    seed_inputs(tmp_path)
    with sqlite3.connect(":memory:") as conn, fixed_render_clock(date(2026, 9, 18)):
        result = extract_for_ticker("WIX", tmp_path, conn)
    assert result.multiple_name == "P/E (FY1 estimate)"
    assert result.current_value is None
    assert result.rich_cheap_verdict is None


def capture_estimates(
    conn: sqlite3.Connection,
    root: Path,
    *,
    stamp: str = "2026-09-18T12:00:00+00:00",
    identity: str = "estimate",
) -> None:
    import hashlib

    path = root / "data/historical/fmp/WIX_analyst_estimates_annual.json"
    body = path.read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    conn.execute(
        "INSERT OR IGNORE INTO evidence_content_blobs VALUES (?,?,?,?,?)",
        (digest, len(body), "application/json", path.as_uri(), stamp),
    )
    conn.execute(
        "INSERT INTO evidence_source_observations(observation_id,idempotency_key,source_kind,source_url,blob_sha256,observed_at,retrieved_at,retrieval_config_sha256,collector_code_version) VALUES (?,?,'fmp','https://example.invalid/estimates',?,?,?,?,'fixture')",
        (identity + "-source", identity + "-source", digest, stamp, stamp, "1" * 64),
    )
    conn.execute(
        "INSERT INTO evidence_document_versions(document_version_id,document_key,version_sequence,observation_id,blob_sha256,issuer_id,ticker,document_type,form_type,language,recorded_at) VALUES (?,?,1,?,?,'issuer-1','WIX','fmp_analyst_estimates','annual','en',?)",
        (identity + "-document", identity + "-document", identity + "-source", digest, stamp),
    )
    conn.commit()


def seed_captured_inputs(
    conn: sqlite3.Connection,
    root: Path,
    *,
    stamp: str = "2026-09-18T12:00:00+00:00",
    currency_capture_stamp: str | None = "2026-09-18T12:00:00+00:00",
) -> None:
    from tests.test_canonical_growth_screen import seed_growth_graph
    from tests.test_discovery_financial_inputs import seed_market_context

    seed_growth_graph(conn, root)
    seed_inputs(root)
    seed_market_context(conn, root / "data/historical/fmp")
    conn.execute("UPDATE tracked_companies SET list_type='portfolio' WHERE ticker='WIX'")
    capture_estimates(conn, root, stamp=stamp)
    if currency_capture_stamp is not None:
        capture_currency_source(conn, root, stamp=currency_capture_stamp)


def test_fy1_current_retains_owner_choice_and_rejects_mixed_history_comparison(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    from io import StringIO

    from report.renderers.workspace_sections.valuation import _valuation_tab
    from report.sections.valuation import build

    with (
        sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn,
        fixed_render_clock(date(2026, 9, 18)),
    ):
        seed_captured_inputs(conn, tmp_path)
        result = extract_for_ticker("WIX", tmp_path, conn)
        assert (
            result.requested_multiple == "P/E (NTM)"
            and result.multiple_name == "P/E (FY1 estimate)"
        )
        assert result.current_value == 20 and result.peg_ratio == 0.4
        assert result.current_period_end == "2026-09-18"
        assert result.estimate_target_period_end == "2026-12-31"
        assert result.history and all(
            point.basis == "provider_ltm_proxy_unmigrated" for point in result.history
        )
        assert result.historical_median is None and result.rich_cheap_verdict is None
        assert result.comparison_unavailable_reason == "no_comparable_same_basis_history"
        assert "estimate-document" in json.dumps(result.source_context)
        section = build("WIX", tmp_path, enable_llm=False, conn=conn)
        rendered = StringIO()
        _valuation_tab(rendered, section)
        html = rendered.getvalue()
        assert "P/E (FY1 estimate)" in html and "PEG (FY1)" in html
        assert "Historical comparison unavailable" in html and "Source inputs" in html
        assert "valuation-spark" not in html
        assert "current_owner_context" in html


@pytest.mark.parametrize(
    "change",
    ["balance", "profile", "income", "estimates", "key_metrics", "thesis", "capture", "tier"],
)
def test_cache_invalidates_every_effective_input_identity(
    tmp_path: Path, migrated_db: Callable[..., Path], change: str
) -> None:
    from compute.valuation_basis import load

    with (
        sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn,
        fixed_render_clock(date(2026, 9, 18)),
    ):
        seed_captured_inputs(conn, tmp_path)
        original = extract_for_ticker("WIX", tmp_path, conn)
        assert load(tmp_path, "WIX", conn=conn) == original
        if change == "capture":
            capture_estimates(
                conn, tmp_path, stamp="2026-09-18T18:00:00+00:00", identity="recapture"
            )
        elif change == "tier":
            conn.execute("UPDATE tracked_companies SET list_type='index_member' WHERE ticker='WIX'")
        elif change == "thesis":
            path = tmp_path / "micro_thesis/holdings/WIX.json"
            path.write_text(path.read_text() + " ")
        else:
            suffix = {
                "balance": "balance_sheet_quarterly",
                "profile": "profile",
                "income": "income_statement_quarterly",
                "estimates": "analyst_estimates_annual",
                "key_metrics": "key_metrics_quarterly",
            }[change]
            path = tmp_path / "data/historical/fmp" / f"WIX_{suffix}.json"
            path.write_text(path.read_text() + " " if path.exists() else "[]")
        cached = load(tmp_path, "WIX", conn=conn)
        assert (
            cached is not None
            and cached.skipped_reason == "valuation_cache_rebuild_required_input_identity_changed"
        )


@pytest.mark.parametrize("condition", ["currency", "stale", "future", "ev"])
def test_incomparable_or_unavailable_current_inputs_fail_closed(
    tmp_path: Path, migrated_db: Callable[..., Path], condition: str
) -> None:
    with (
        sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn,
        fixed_render_clock(date(2026, 9, 18)),
    ):
        stamp = (
            "2026-09-10T00:00:00+00:00"
            if condition == "stale"
            else "2026-09-20T00:00:00+00:00"
            if condition == "future"
            else "2026-09-18T12:00:00+00:00"
        )
        seed_captured_inputs(
            conn,
            tmp_path,
            stamp=stamp,
            currency_capture_stamp=None if condition == "currency" else "2026-09-18T12:00:00+00:00",
        )
        if condition == "currency":
            path = tmp_path / "data/historical/fmp/WIX_income_statement_quarterly.json"
            path.write_text(path.read_text().replace("USD", "EUR"))
            capture_currency_source(conn, tmp_path, stamp="2026-09-18T12:00:00+00:00")
        elif condition == "ev":
            path = tmp_path / "micro_thesis/holdings/WIX.json"
            path.write_text(path.read_text().replace("P/E (NTM)", "EV/NTM EBITDA"))
        result = extract_for_ticker("WIX", tmp_path, conn)
        assert result.current_value is None and result.rich_cheap_verdict is None
        assert result.current_unavailable_reason
        if condition == "ev":
            assert (
                result.current_unavailable_reason
                == "enterprise_value_definition_and_capture_unavailable"
            )


def test_legacy_cache_requires_explicit_rebuild_without_picker(tmp_path: Path) -> None:
    from compute.valuation_basis import load

    path = tmp_path / "data/valuation_basis/WIX.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"ticker":"WIX","multiple_name":"P/E (NTM)","current_value":20}')
    cached = load(tmp_path, "WIX")
    assert cached is not None and cached.current_value is None
    assert cached.skipped_reason == "valuation_cache_rebuild_required_unlabelled_legacy_basis"


def test_realized_forward_shadow_and_ltm_proxy_never_form_fy1_band(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    with (
        sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn,
        fixed_render_clock(date(2026, 9, 18)),
    ):
        seed_captured_inputs(conn, tmp_path, currency_capture_stamp=None)
        directory = tmp_path / "data/historical/fmp"
        (directory / "WIX_key_metrics_quarterly.json").write_text(
            json.dumps(
                [
                    {"date": "2026-06-30", "marketCap": 500, "peRatio": 10},
                    {"date": "2025-06-30", "marketCap": 400, "peRatio": 8},
                ]
            )
        )
        (directory / "WIX_income_statement_quarterly.json").write_text(
            json.dumps(
                [
                    {"symbol": "WIX", "date": period, "reportedCurrency": "USD", "netIncome": 10}
                    for period in ["2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30"]
                ]
            )
        )
        capture_currency_source(conn, tmp_path, stamp="2026-09-18T12:00:00+00:00")
        result = extract_for_ticker("WIX", tmp_path, conn)
        assert result.current_value == 20
        assert [(point.basis, point.value) for point in result.history] == [
            ("realized_forward_proxy_unmigrated", 10),
            ("provider_ltm_proxy_unmigrated", 10),
        ]
        assert result.historical_median is None and result.rich_cheap_verdict is None


def test_archive_requires_contemporaneous_currency_and_binds_selected_bytes(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    from sources.valuation_inputs import read_valuation_inputs
    from tests.test_canonical_growth_screen import seed_growth_graph

    with sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn:
        seed_growth_graph(conn, tmp_path)
        seed_inputs(tmp_path)
        conn.execute("UPDATE tracked_companies SET list_type='portfolio' WHERE ticker='WIX'")
        archive = tmp_path / "data/historical/fmp_snapshots/2026-09-18"
        archive.mkdir(parents=True)
        current = tmp_path / "data/historical/fmp"
        (archive / "WIX_analyst_estimates_annual.json").write_bytes(
            (current / "WIX_analyst_estimates_annual.json").read_bytes()
        )
        missing = read_valuation_inputs(tmp_path, "WIX", as_of=date(2026, 9, 18), conn=conn)
        assert not missing.estimates and missing.estimate_unavailable_reason
        (archive / "WIX_income_statement_quarterly.json").write_bytes(
            (current / "WIX_income_statement_quarterly.json").read_bytes()
        )
        present = read_valuation_inputs(tmp_path, "WIX", as_of=date(2026, 9, 18), conn=conn)
        assert present.estimates and present.estimate_unavailable_reason is None
        assert present.estimates[0].observation_date.date() == date(2026, 9, 18)
        assert present.fingerprint != missing.fingerprint
        assert "estimates_archive_daily_snapshot_date" in json.dumps(present.manifest)
        assert present.manifest["estimate_freshness_limit_hours"] == 24
        assert present.manifest["estimate_currency_freshness_limit_hours"] == 336
        assert (
            present.manifest["estimate_currency_freshness_policy"]
            == "pipeline.cadence_policy/statement"
        )
        expired_estimate = read_valuation_inputs(
            tmp_path, "WIX", as_of=date(2026, 9, 20), conn=conn
        )
        assert expired_estimate.estimate_unavailable_reason == "estimate_snapshot_stale"


def test_cache_ignores_file_mtime_when_bytes_and_capture_are_identical(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    import os

    from compute.valuation_basis import load

    with (
        sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn,
        fixed_render_clock(date(2026, 9, 18)),
    ):
        seed_captured_inputs(conn, tmp_path)
        original = extract_for_ticker("WIX", tmp_path, conn)
        path = tmp_path / "data/historical/fmp/WIX_analyst_estimates_annual.json"
        os.utime(path, (1, 1))
        assert load(tmp_path, "WIX", conn=conn) == original


def capture_currency_source(conn: sqlite3.Connection, root: Path, *, stamp: str) -> None:
    import hashlib

    path = root / "data/historical/fmp/WIX_income_statement_quarterly.json"
    body = path.read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    conn.execute(
        "INSERT OR IGNORE INTO evidence_content_blobs VALUES (?,?,?,?,?)",
        (digest, len(body), "application/json", path.as_uri(), stamp),
    )
    conn.execute(
        "INSERT INTO evidence_source_observations(observation_id,idempotency_key,source_kind,source_url,blob_sha256,observed_at,retrieved_at,retrieval_config_sha256,collector_code_version) VALUES ('currency-source','currency-source','fmp','https://example.invalid/income',?,?,?,?,'fixture')",
        (digest, stamp, stamp, "1" * 64),
    )
    conn.execute(
        "INSERT INTO evidence_document_versions(document_version_id,document_key,version_sequence,observation_id,blob_sha256,issuer_id,ticker,document_type,form_type,language,recorded_at) VALUES ('currency-document','currency-document',1,'currency-source',?,'issuer-1','WIX','fmp_income_statement','quarterly','en',?)",
        (digest, stamp),
    )
    conn.commit()


@pytest.mark.parametrize("currency_capture_stamp", [None, "2026-09-20T00:00:00+00:00"])
def test_current_estimate_rejects_uncaptured_or_future_currency_companion(
    tmp_path: Path, migrated_db: Callable[..., Path], currency_capture_stamp: str | None
) -> None:
    with (
        sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn,
        fixed_render_clock(date(2026, 9, 18)),
    ):
        seed_captured_inputs(conn, tmp_path, currency_capture_stamp=None)
        if currency_capture_stamp is not None:
            capture_currency_source(conn, tmp_path, stamp=currency_capture_stamp)
        result = extract_for_ticker("WIX", tmp_path, conn)
        assert result.current_value is None
        assert result.current_unavailable_reason == "estimate_currency_source_capture_unavailable"


def test_stale_currency_companion_cannot_admit_fresh_fy1(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    with (
        sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn,
        fixed_render_clock(date(2026, 9, 18)),
    ):
        seed_captured_inputs(conn, tmp_path, currency_capture_stamp="2020-01-01T00:00:00+00:00")
        result = extract_for_ticker("WIX", tmp_path, conn)
        assert result.current_value is None
        assert result.current_unavailable_reason == "estimate_currency_source_snapshot_stale"
        assert result.peg_ratio is None


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("conflict", [True, False])
def test_same_target_estimate_rows_are_deterministic_or_unavailable(
    tmp_path: Path, migrated_db: Callable[..., Path], reverse: bool, conflict: bool
) -> None:
    with (
        sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn,
        fixed_render_clock(date(2026, 9, 18)),
    ):
        seed_captured_inputs(conn, tmp_path)
        rows = [
            {"symbol": "WIX", "date": "2026-12-31", "netIncomeAvg": 25, "epsAvg": 2},
            {
                "symbol": "WIX",
                "date": "2026-12-31",
                "netIncomeAvg": 50 if conflict else 25,
                "epsAvg": 2,
            },
            {"symbol": "WIX", "date": "2027-12-31", "netIncomeAvg": 30, "epsAvg": 3},
        ]
        path = tmp_path / "data/historical/fmp/WIX_analyst_estimates_annual.json"
        path.write_text(json.dumps(list(reversed(rows)) if reverse else rows))
        capture_estimates(conn, tmp_path, identity="duplicates")
        result = extract_for_ticker("WIX", tmp_path, conn)
        if conflict:
            assert result.current_value is None
            assert result.current_unavailable_reason == "conflicting_estimate_target_observations"
            assert result.peg_ratio is None
        else:
            assert result.current_value == 20.0
            assert result.peg_ratio == pytest.approx(0.4)
            assert result.current_unavailable_reason is None


@pytest.mark.parametrize(
    "target_days,available", [(365, True), (371, True), (385, True), (386, False), (1460, False)]
)
def test_missing_nearer_annual_target_cannot_be_called_fy1(
    tmp_path: Path, migrated_db: Callable[..., Path], target_days: int, available: bool
) -> None:
    from datetime import timedelta

    with (
        sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn,
        fixed_render_clock(date(2026, 9, 18)),
    ):
        seed_captured_inputs(conn, tmp_path)
        target = date(2026, 9, 18) + timedelta(days=target_days)
        path = tmp_path / "data/historical/fmp/WIX_analyst_estimates_annual.json"
        path.write_text(
            json.dumps([{"symbol": "WIX", "date": target.isoformat(), "netIncomeAvg": 25}])
        )
        capture_estimates(conn, tmp_path, identity="horizon")
        result = extract_for_ticker("WIX", tmp_path, conn)
        assert result.estimate_target_period_end == target.isoformat()
        if available:
            assert result.current_value == 20.0
            assert result.current_basis == "fy1_estimate"
        else:
            assert result.current_value is None
            assert result.current_unavailable_reason == "fy1_target_horizon_unavailable"
            assert result.current_basis == "unsupported_annual_estimate_horizon"
            assert result.multiple_name is not None and "FY1" not in result.multiple_name
            assert (
                result.source_context["current_unavailable_reason"]
                == result.current_unavailable_reason
            )


@pytest.mark.parametrize("age_days,available", [(2, True), (15, False)])
def test_currency_companion_uses_statement_cadence_not_estimate_cadence(
    tmp_path: Path, migrated_db: Callable[..., Path], age_days: int, available: bool
) -> None:
    from datetime import UTC, datetime, timedelta

    with (
        sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn,
        fixed_render_clock(date(2026, 9, 18)),
    ):
        captured = datetime(2026, 9, 18, 12, tzinfo=UTC) - timedelta(days=age_days)
        seed_captured_inputs(conn, tmp_path, currency_capture_stamp=captured.isoformat())
        result = extract_for_ticker("WIX", tmp_path, conn)
        assert result.source_context["estimate_freshness_limit_hours"] == 24
        assert result.source_context["estimate_currency_freshness_limit_hours"] == 336
        assert (
            result.source_context["estimate_currency_freshness_policy"]
            == "pipeline.cadence_policy/statement"
        )
        if available:
            assert result.current_value == 20.0 and result.current_unavailable_reason is None
        else:
            assert result.current_value is None
            assert result.current_unavailable_reason == "estimate_currency_source_snapshot_stale"
