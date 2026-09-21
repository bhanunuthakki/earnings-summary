"""Canonical financial-source coverage for the cockpit fundamentals cache."""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast

import pytest

from cockpit_fundamentals import (
    compute_from_db,
    compute_snapshot,
    materialize_fundamentals,
    read_materialized_fundamentals,
)
from sources.canonical_financial_series import (
    CanonicalFinancialReadError,
    CanonicalFinancialSeriesReader,
    FinancialCadence,
    SeriesContinuity,
)
from tests import test_source_fact_repository as foundation
from tests.test_report_canonical_financials import STAMP, seed_table


@pytest.fixture(scope="module")
def head_template(
    tmp_path_factory: pytest.TempPathFactory,
    migrated_db: Callable[..., Path],
) -> Path:
    path = tmp_path_factory.mktemp("fundamentals-head") / "head.db"
    return migrated_db(path)


@pytest.fixture
def conn(head_template: Path, tmp_path: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "portfolio.db"
    shutil.copy(head_template, path)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    yield connection
    connection.close()


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "data").mkdir(parents=True)
    return root


def _publish(
    conn: sqlite3.Connection,
    rows: list[tuple[str, str, str, str, str, str]],
    *,
    currencies: dict[int, str] | None = None,
) -> None:
    foundation.seed_foundation(conn)
    seed_table(conn, rows, currencies=currencies)


def _track_synth(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT OR IGNORE INTO tenants (id, created_at) VALUES ('bhanu', '2026-01-01')")
    conn.execute(
        "INSERT OR IGNORE INTO tracked_companies "
        "(user_id,ticker,name,list_type,instrument_type) "
        "VALUES ('bhanu','SYNTH','Synthetic','evaluation','equity')"
    )
    conn.commit()


def _quarter_rows(
    *, missing_direct_index: int | None = None
) -> list[tuple[str, str, str, str, str, str]]:
    periods = [
        ("2025-01-01", "2025-03-31", "Q1", "100", None, None, None),
        ("2025-04-01", "2025-06-30", "Q2", "100", "25", "-5", "20"),
        ("2025-07-01", "2025-09-30", "Q3", "105", "20", "-5", "15"),
        ("2025-10-01", "2025-12-31", "Q4", "110", "25", "-5", "20"),
        ("2026-01-01", "2026-03-31", "Q1", "120", "30", "-5", "25"),
    ]
    rows: list[tuple[str, str, str, str, str, str]] = []
    for period_index, (start, end, fiscal, revenue, operating, capex, direct) in enumerate(periods):
        rows.append(("revenue", start, end, fiscal, revenue, "USD"))
        if operating is not None:
            rows.append(("operating_cash_flow", start, end, fiscal, operating, "USD"))
        if capex is not None:
            rows.append(("capital_expenditure", start, end, fiscal, capex, "USD"))
        if direct is not None and period_index != missing_direct_index:
            rows.append(("free_cash_flow", start, end, fiscal, direct, "USD"))
    return rows


def _semiannual_rows() -> list[tuple[str, str, str, str, str, str]]:
    rows: list[tuple[str, str, str, str, str, str]] = []
    periods = [
        ("2024-01-01", "2024-06-30", "Q2", "100", "25", "-5", "20"),
        ("2024-07-01", "2024-12-31", "Q4", "105", "20", "-5", "15"),
        ("2025-01-01", "2025-06-30", "Q2", "110", "25", "-5", None),
        ("2025-07-01", "2025-12-31", "Q4", "120", "30", "-5", "25"),
    ]
    for start, end, fiscal, revenue, operating, capex, direct in periods:
        for concept, value in (
            ("revenue", revenue),
            ("operating_cash_flow", operating),
            ("capital_expenditure", capex),
            ("free_cash_flow", direct),
        ):
            if value is not None:
                rows.append((concept, start, end, fiscal, value, "USD"))
    return rows


def test_compute_from_db_preserves_quarterly_oracles_and_derivation(
    conn: sqlite3.Connection,
) -> None:
    _publish(conn, _quarter_rows(missing_direct_index=3))
    revenue_yoy, margin = compute_from_db(conn, cutoff=STAMP)["SYNTH"]
    assert revenue_yoy == pytest.approx(20.0)
    assert margin == pytest.approx(80.0 / 435.0 * 100.0)
    snapshot = compute_snapshot(conn, cutoff=STAMP)
    result = snapshot.fundamentals["SYNTH"].fcf_margin
    assert result.status == "available"
    assert result.calculation_kind == "calculated"
    periods = cast(list[dict[str, object]], result.lineage["periods"])
    assert any(period["kind"] == "calculated" for period in periods)
    assert all("revenue_observation_id" in period for period in periods)


def test_semiannual_and_reported_ttm_cadences_are_explicit(conn: sqlite3.Connection) -> None:
    rows = _semiannual_rows()
    rows.extend(
        [
            ("revenue", "2025-04-01", "2026-03-31", "TTM", "400", "USD"),
            ("free_cash_flow", "2025-04-01", "2026-03-31", "TTM", "100", "USD"),
        ]
    )
    _publish(conn, rows)
    snapshot = compute_snapshot(conn, cutoff=STAMP)
    result = snapshot.fundamentals["SYNTH"]
    assert result.revenue_yoy.value_pct == pytest.approx(120.0 / 105.0 * 100.0 - 100.0)
    assert result.fcf_margin.value_pct == pytest.approx(25.0)
    assert result.fcf_margin.lineage["formula"] == (
        "reported_ttm_free_cash_flow/reported_ttm_revenue*100"
    )
    reader = CanonicalFinancialSeriesReader(conn, "SYNTH", cutoff=STAMP)
    quarter = reader.read(
        "revenue", cadence=FinancialCadence.QUARTERLY, continuity=SeriesContinuity.WINDOWED
    )
    semiannual = reader.read(
        "revenue", cadence=FinancialCadence.SEMIANNUAL, continuity=SeriesContinuity.WINDOWED
    )
    ttm = reader.read(
        "revenue", cadence=FinancialCadence.REPORTED_TTM, continuity=SeriesContinuity.WINDOWED
    )
    assert quarter.status == "unavailable"
    assert semiannual.status == "available" and len(semiannual.observations) == 4
    assert ttm.status == "available" and len(ttm.observations) == 1


def test_rejected_direct_fcf_does_not_fall_back_to_derived(
    conn: sqlite3.Connection,
) -> None:
    rows = _quarter_rows()
    direct_indexes = [index for index, row in enumerate(rows) if row[0] == "free_cash_flow"]
    _publish(conn, rows, currencies={direct_indexes[-1]: "EUR"})
    result = compute_snapshot(conn, cutoff=STAMP).fundamentals["SYNTH"].fcf_margin
    assert result.status == "unavailable"
    assert result.reason_code == "direct_free_cash_flow_candidate_rejected"
    assert result.value_pct is None
    rejected = cast(tuple[str, ...], result.lineage["rejected_direct_candidate_observation_ids"])
    assert len(rejected) == 1


def test_malformed_direct_fcf_candidate_blocks_derived_fallback(
    conn: sqlite3.Connection,
) -> None:
    rows = _quarter_rows(missing_direct_index=2)
    rows.append(("free_cash_flow", "2025-08-01", "2025-09-30", "Q3", "15", "USD"))
    _publish(conn, rows)
    result = compute_snapshot(conn, cutoff=STAMP).fundamentals["SYNTH"].fcf_margin
    assert result.status == "unavailable"
    assert result.reason_code == "direct_free_cash_flow_candidate_rejected"
    rejected = cast(tuple[str, ...], result.lineage["rejected_direct_candidate_observation_ids"])
    assert len(rejected) == 1


def test_irrelevant_annual_row_does_not_poison_quarterly_window(
    conn: sqlite3.Connection,
) -> None:
    rows = _quarter_rows()
    rows.append(("revenue", "2025-01-01", "2025-12-31", "FY", "415", "USD"))
    _publish(conn, rows)
    result = compute_snapshot(conn, cutoff=STAMP).fundamentals["SYNTH"]
    assert result.revenue_yoy.value_pct == pytest.approx(20.0)
    assert result.fcf_margin.value_pct == pytest.approx(80.0 / 435.0 * 100.0)


def test_empty_canonical_universe_is_a_complete_snapshot(conn: sqlite3.Connection) -> None:
    snapshot = compute_snapshot(conn, cutoff=STAMP)
    assert snapshot.status == "complete"
    assert snapshot.ticker_universe == ()
    assert snapshot.fundamentals == {}
    assert compute_from_db(conn, cutoff=STAMP) == {}


def test_empty_canonical_universe_materializes_complete_receipt(
    conn: sqlite3.Connection, repo_root: Path
) -> None:
    assert materialize_fundamentals(conn, repo_root) == 0
    payload = json.loads(
        (repo_root / "data" / "cockpit_fundamentals.json").read_text(encoding="utf-8")
    )
    assert payload["snapshot"]["status"] == "complete"
    assert payload["snapshot"]["ticker_universe"] == []
    assert payload["snapshot"]["fundamentals"] == {}


def test_rejected_supported_label_candidate_remains_in_universe(
    conn: sqlite3.Connection,
) -> None:
    _publish(
        conn,
        [("revenue", "2026-03-01", "2026-03-31", "Q1", "120", "USD")],
    )
    snapshot = compute_snapshot(conn, cutoff=STAMP)
    assert snapshot.ticker_universe == ("SYNTH",)
    result = snapshot.fundamentals["SYNTH"]
    assert result.status == "unavailable"
    assert result.revenue_yoy.reason_code == "unsupported_financial_cadence_or_duration"


def test_materialized_cache_requires_complete_canonical_manifest(
    conn: sqlite3.Connection, repo_root: Path
) -> None:
    _publish(conn, _quarter_rows())
    _track_synth(conn)
    assert materialize_fundamentals(conn, repo_root) == 1
    cache = repo_root / "data" / "cockpit_fundamentals.json"
    payload = json.loads(cache.read_text(encoding="utf-8"))
    assert payload["cache_schema"] == "cockpit-fundamentals-canonical/v2"
    snapshot = payload["snapshot"]
    assert snapshot["source_authority"] == "canonical_fact_resolution"
    assert snapshot["status"] == "complete"
    assert snapshot["ticker_universe"] == ["SYNTH"]
    manifests = snapshot["fundamentals"]["SYNTH"]["fcf_margin"]["source_manifests"]
    assert manifests
    selected = next(
        observation for manifest in manifests.values() for observation in manifest["observations"]
    )
    assert selected["observation_id"]
    assert selected["canonical_resolution_revision_id"]
    assert selected["document_version_id"]
    assert read_materialized_fundamentals(repo_root)["SYNTH"][0] == pytest.approx(20.0)


def test_legacy_or_incomplete_cache_cannot_claim_canonical(repo_root: Path) -> None:
    cache = repo_root / "data" / "cockpit_fundamentals.json"
    cache.write_text(json.dumps({"fundamentals": {"OLD": [99.0, 99.0]}}), encoding="utf-8")
    assert read_materialized_fundamentals(repo_root) == {}


def test_cache_rejects_manifest_or_envelope_cutoff_tampering(
    conn: sqlite3.Connection, repo_root: Path
) -> None:
    _publish(conn, _quarter_rows())
    materialize_fundamentals(conn, repo_root)
    cache = repo_root / "data" / "cockpit_fundamentals.json"
    payload = json.loads(cache.read_text(encoding="utf-8"))
    manifests = payload["snapshot"]["fundamentals"]["SYNTH"]["fcf_margin"]["source_manifests"]
    next(iter(manifests.values()))["cutoff"] = "2001-01-01T00:00:00Z"
    cache.write_text(json.dumps(payload), encoding="utf-8")
    assert read_materialized_fundamentals(repo_root) == {}

    materialize_fundamentals(conn, repo_root)
    payload = json.loads(cache.read_text(encoding="utf-8"))
    payload["computed_at"] = "2001-01-01T00:00:00"
    cache.write_text(json.dumps(payload), encoding="utf-8")
    assert read_materialized_fundamentals(repo_root) == {}
    cache.write_text("not-json", encoding="utf-8")
    assert read_materialized_fundamentals(repo_root) == {}


def test_global_failure_preserves_previous_cache_bytes(
    conn: sqlite3.Connection,
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _publish(conn, _quarter_rows())
    materialize_fundamentals(conn, repo_root)
    cache = repo_root / "data" / "cockpit_fundamentals.json"
    before = cache.read_bytes()

    def fail(*_args: object, **_kwargs: object) -> tuple[str, ...]:
        raise CanonicalFinancialReadError("synthetic failure")

    monkeypatch.setattr("cockpit_fundamentals.discover_canonical_financial_tickers", fail)
    with pytest.raises(CanonicalFinancialReadError, match="synthetic failure"):
        materialize_fundamentals(conn, repo_root)
    assert cache.read_bytes() == before


def test_build_cockpit_rows_uses_cache_and_canonical_fallback(
    conn: sqlite3.Connection, repo_root: Path
) -> None:
    from pipeline.research_cockpit import build_cockpit_rows

    _publish(conn, _quarter_rows(missing_direct_index=3))
    _track_synth(conn)
    without_cache = build_cockpit_rows(conn, repo_root)
    row = {item.base.ticker: item for item in without_cache["evaluation"]}["SYNTH"]
    assert row.fcf_margin_pct == pytest.approx(80.0 / 435.0 * 100.0)
    materialize_fundamentals(conn, repo_root)
    with_cache = build_cockpit_rows(conn, repo_root)
    row = {item.base.ticker: item for item in with_cache["evaluation"]}["SYNTH"]
    assert row.rev_yoy_pct == pytest.approx(20.0)
