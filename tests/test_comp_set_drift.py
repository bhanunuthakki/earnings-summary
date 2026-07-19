"""Phase 2 unit tests: pool-wide industry/sector slices
(compute.comparable_sets), FMP snapshot ingest + drift check
(compute.comp_set_drift) — docs/design/comparable_sets_bottoms_up.md §6/§7.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from compute.comp_set_drift import (  # noqa: E402
    DRIFT_ALERT_THRESHOLD,
    SnapshotEntry,
    compute_drift,
    latest_scope_date,
    load_fmp_pe_snapshot,
    upsert_fmp_snapshot_rows,
)
from compute.comp_set_metrics import MetricRow, upsert_metric_rows  # noqa: E402
from compute.comparable_sets import (  # noqa: E402
    PoolProfile,
    pool_scope_slices,
    scope_metric_class,
)

AS_OF = date(2026, 7, 18)


def _metrics_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE comp_set_metrics_daily (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "scope_type TEXT, scope_key TEXT, as_of_date TEXT, metric TEXT, stat_type TEXT, "
        "value REAL, n_members INTEGER, n_valid INTEGER, coverage_pct REAL, "
        "method_version INTEGER, method_flags TEXT, computed_at TEXT, locator TEXT, "
        "UNIQUE(scope_type, scope_key, as_of_date, metric, stat_type, method_version))"
    )
    return conn


def _profile(
    ticker: str,
    *,
    sector: str = "Technology",
    industry: str = "Software - Application",
    exchange: str | None = "NASDAQ",
    active: bool = True,
) -> PoolProfile:
    return PoolProfile(
        ticker=ticker,
        list_type="index_member",
        sector=sector,
        industry=industry,
        market_cap=1e10,
        exchange=exchange,
        is_actively_trading=active,
        is_etf=False,
    )


def _seed_metric_row(
    conn: sqlite3.Connection,
    *,
    scope_type: str,
    scope_key: str,
    as_of: date,
    value: float | None,
    metric: str = "pe_ttm",
    stat_type: str = "median",
    n_members: int = 10,
    n_valid: int = 9,
    method_version: int = 1,
    method_flags: dict[str, object] | None = None,
) -> None:
    row = MetricRow(
        metric=metric,
        stat_type=stat_type,
        value=value,
        n_members=n_members,
        n_valid=n_valid,
        coverage_pct=n_valid / n_members if n_members else 0.0,
        method_flags=method_flags if method_flags is not None else {},
    )
    upsert_metric_rows(conn, scope_type, scope_key, as_of, method_version, [row])


# ---------------------------------------------------------------------------
# §6 pool-wide slices
# ---------------------------------------------------------------------------


def test_pool_scope_slices_groups_by_industry_and_sector() -> None:
    pool = {
        "A": _profile("A"),
        "B": _profile("B"),
        "C": _profile("C", sector="Healthcare", industry="Biotechnology"),
    }
    slices = pool_scope_slices(pool)
    assert slices[("industry", "Software - Application")] == ["A", "B"]
    assert slices[("industry", "Biotechnology")] == ["C"]
    assert slices[("sector", "Technology")] == ["A", "B"]
    assert slices[("sector", "Healthcare")] == ["C"]


def test_pool_scope_slices_applies_us_listed_and_active_guards() -> None:
    pool = {
        "OK": _profile("OK"),
        "FOREIGN": _profile("FOREIGN", exchange="LSE"),
        "NOEXCH": _profile("NOEXCH", exchange=None),
        "HALTED": _profile("HALTED", active=False),
    }
    slices = pool_scope_slices(pool)
    assert slices[("industry", "Software - Application")] == ["OK"]
    assert slices[("sector", "Technology")] == ["OK"]


def test_scope_metric_class_industry_uses_keyword_classifier() -> None:
    assert scope_metric_class("industry", "Banks - Regional") == "financial"
    assert scope_metric_class("industry", "Financial - Credit Services") == "financial"
    assert scope_metric_class("industry", "Software - Application") == "operating"
    assert scope_metric_class("industry", "REIT - Office") == "reit"


def test_scope_metric_class_sector_uses_explicit_map() -> None:
    assert scope_metric_class("sector", "Financial Services") == "financial"
    assert scope_metric_class("sector", "Real Estate") == "reit"
    assert scope_metric_class("sector", "Technology") == "operating"


# ---------------------------------------------------------------------------
# §6 note — fmp_snapshot ingest
# ---------------------------------------------------------------------------


def _write_snapshot(tmp_path: Path, kind: str, rows: list[dict[str, object]]) -> None:
    d = tmp_path / "data" / "historical" / "sector_industry"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{kind}_pe_snapshot.json").write_text(json.dumps(rows), encoding="utf-8")


def test_load_fmp_pe_snapshot_parses_and_groups(tmp_path: Path) -> None:
    _write_snapshot(
        tmp_path,
        "industry",
        [
            {"date": "2026-05-19", "industry": "Semiconductors", "exchange": "NASDAQ", "pe": 30.0},
            # Multi-exchange rows for one key combine as their median.
            {"date": "2026-05-19", "industry": "Biotechnology", "exchange": "NASDAQ", "pe": 20.0},
            {"date": "2026-05-19", "industry": "Biotechnology", "exchange": "NYSE", "pe": 30.0},
            # Malformed rows skipped, never fabricated.
            {"date": "2026-05-19", "industry": "", "pe": 10.0},
            {"date": "2026-05-19", "industry": "NoPE"},
            {"date": "not-a-date", "industry": "BadDate", "pe": 10.0},
        ],
    )
    entries = load_fmp_pe_snapshot(tmp_path, "industry")
    by_key = {e.scope_key: e for e in entries}
    assert set(by_key) == {"Semiconductors", "Biotechnology"}
    assert by_key["Semiconductors"].pe == 30.0
    assert by_key["Biotechnology"].pe == 25.0  # median of 20/30
    assert by_key["Biotechnology"].exchanges == ("NASDAQ", "NYSE")
    assert by_key["Semiconductors"].as_of == date(2026, 5, 19)


def test_load_fmp_pe_snapshot_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_fmp_pe_snapshot(tmp_path, "sector") == []


def test_upsert_fmp_snapshot_rows_idempotent_with_vendor_locator(tmp_path: Path) -> None:
    conn = _metrics_conn()
    entries = [
        SnapshotEntry(
            kind="industry",
            scope_key="Semiconductors",
            as_of=date(2026, 5, 19),
            pe=30.0,
            exchanges=("NASDAQ",),
        )
    ]
    n1 = upsert_fmp_snapshot_rows(conn, entries, 1)
    n2 = upsert_fmp_snapshot_rows(conn, entries, 1)
    assert n1 == n2 == 1
    rows = conn.execute(
        "SELECT scope_type, stat_type, value, method_flags, locator FROM comp_set_metrics_daily"
    ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["scope_type"] == "fmp_snapshot"
    assert row["stat_type"] == "median"  # FMP publishes no cap-weighted variant
    assert row["value"] == 30.0
    flags = json.loads(row["method_flags"])
    assert flags["snapshot_kind"] == "industry"
    locator = json.loads(row["locator"])
    assert locator["kind"] == "vendor_field"
    assert locator["vendor_field"]["endpoint"] == "industry-pe-snapshot"
    assert locator["vendor_field"]["field"] == "pe"


# ---------------------------------------------------------------------------
# §7 drift computation
# ---------------------------------------------------------------------------


def _seed_snapshot_row(
    conn: sqlite3.Connection, scope_key: str, as_of: date, pe: float, kind: str = "industry"
) -> None:
    upsert_fmp_snapshot_rows(
        conn,
        [SnapshotEntry(kind=kind, scope_key=scope_key, as_of=as_of, pe=pe, exchanges=())],
        1,
    )


def test_drift_math_and_threshold_band(tmp_path: Path) -> None:
    conn = _metrics_conn()
    # Exactly at threshold: (30 - 24) / 24 = 0.25 -> NOT an alert (strictly beyond).
    _seed_metric_row(conn, scope_type="industry", scope_key="AtBand", as_of=AS_OF, value=30.0)
    _seed_snapshot_row(conn, "AtBand", date(2026, 5, 19), 24.0)
    # Beyond threshold: (31.2 - 24) / 24 = 0.30 -> alert.
    _seed_metric_row(conn, scope_type="industry", scope_key="Beyond", as_of=AS_OF, value=31.2)
    _seed_snapshot_row(conn, "Beyond", date(2026, 5, 19), 24.0)

    report = compute_drift(conn, "industry", AS_OF, 1)
    by_key = {r.scope_key: r for r in report.results}
    assert abs(by_key["AtBand"].drift_pct - 0.25) < 1e-9
    assert by_key["AtBand"].alert is False
    assert abs(by_key["Beyond"].drift_pct - 0.30) < 1e-9
    assert by_key["Beyond"].alert is True
    assert by_key["Beyond"].snapshot_age_days == (AS_OF - date(2026, 5, 19)).days
    assert DRIFT_ALERT_THRESHOLD == 0.25


def test_drift_uses_nearest_prior_snapshot(tmp_path: Path) -> None:
    conn = _metrics_conn()
    _seed_metric_row(conn, scope_type="industry", scope_key="X", as_of=AS_OF, value=30.0)
    _seed_snapshot_row(conn, "X", date(2026, 3, 1), 10.0)
    _seed_snapshot_row(conn, "X", date(2026, 6, 1), 25.0)  # nearest prior — used
    _seed_snapshot_row(conn, "X", date(2026, 8, 1), 99.0)  # future — never used
    report = compute_drift(conn, "industry", AS_OF, 1)
    assert len(report.results) == 1
    assert report.results[0].fmp_median == 25.0
    assert report.results[0].fmp_as_of == date(2026, 6, 1)


def test_drift_compares_median_to_median_only(tmp_path: Path) -> None:
    """An aggregate bottoms-up row must never enter the drift comparison
    (comparing different constructions would manufacture fake drift)."""
    conn = _metrics_conn()
    _seed_metric_row(
        conn,
        scope_type="industry",
        scope_key="X",
        as_of=AS_OF,
        value=999.0,
        stat_type="aggregate",
    )
    _seed_snapshot_row(conn, "X", date(2026, 6, 1), 25.0)
    report = compute_drift(conn, "industry", AS_OF, 1)
    assert report.results == []


def test_drift_skips_null_median_and_missing_snapshot(tmp_path: Path) -> None:
    conn = _metrics_conn()
    _seed_metric_row(conn, scope_type="industry", scope_key="NullMed", as_of=AS_OF, value=None)
    _seed_metric_row(conn, scope_type="industry", scope_key="NoSnap", as_of=AS_OF, value=30.0)
    report = compute_drift(conn, "industry", AS_OF, 1)
    assert report.results == []
    reasons = {s["scope_key"]: s["reason"] for s in report.skipped}
    assert reasons["NullMed"] == "bottoms_up_median_null"
    assert reasons["NoSnap"] == "no_fmp_snapshot_row"


def test_drift_uses_latest_bottoms_up_date_on_or_before(tmp_path: Path) -> None:
    """The drift run may fire on a day track_comp_metrics hasn't — it
    compares against the latest scope rows on/before the check date."""
    conn = _metrics_conn()
    earlier = date(2026, 7, 16)
    _seed_metric_row(conn, scope_type="industry", scope_key="X", as_of=earlier, value=30.0)
    _seed_snapshot_row(conn, "X", date(2026, 6, 1), 25.0)
    assert latest_scope_date(conn, "industry", AS_OF, 1) == earlier
    report = compute_drift(conn, "industry", AS_OF, 1)
    assert report.as_of_date == earlier
    assert len(report.results) == 1


def test_drift_snapshot_kind_disambiguates_industry_from_sector(tmp_path: Path) -> None:
    """A sector-kind snapshot row whose key collides with an industry key
    must not be used for the industry drift comparison."""
    conn = _metrics_conn()
    _seed_metric_row(conn, scope_type="industry", scope_key="Same", as_of=AS_OF, value=30.0)
    _seed_snapshot_row(conn, "Same", date(2026, 6, 1), 25.0, kind="sector")
    report = compute_drift(conn, "industry", AS_OF, 1)
    assert report.results == []
    assert report.skipped[0]["reason"] == "no_fmp_snapshot_row"
