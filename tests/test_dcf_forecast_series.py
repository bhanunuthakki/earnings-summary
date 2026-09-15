"""Focused checks for the governed, one-way DCF forecast plane."""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from dcf.forecast_series import (
    ForecastSemanticCoordinate,
    ForecastSeriesPoint,
    load_forecast_overlay,
    load_forecast_overlay_for_metric,
)
from dcf.persist import DcfRunRow, upsert
from dcf.provenance import DcfInputProvenance
from provenance.metric_ontology import (
    CanonicalMetric,
    CanonicalMetricDefinitionRevision,
    MetricOntology,
)
from sqlite_runtime import register_sqlite_integrity_functions

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "execution") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import refresh_dcf  # noqa: E402

_DIMENSIONS_JSON = "[]"
_DIMENSIONS_DIGEST = hashlib.sha256(_DIMENSIONS_JSON.encode()).hexdigest()
_EVIDENCE_JSON = '{"basis":"reviewed test mapping"}'
_EVIDENCE_DIGEST = hashlib.sha256(_EVIDENCE_JSON.encode()).hexdigest()
_POLICY_DIGEST = "a" * 64
_REVIEWER = "test-reviewer"

_SCHEMA = """
CREATE TABLE dcf_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    created_at TEXT DEFAULT '2026-01-01T00:00:00Z',
    segment_name TEXT,
    valuation_date TEXT, horizon_years INTEGER, wacc REAL, terminal_growth REAL,
    npv REAL, npv_per_share REAL, shares_outstanding REAL, currency TEXT,
    notes TEXT, run_id TEXT, live_price REAL, live_price_at TEXT,
    over_under_pct REAL, mos_bar_used REAL, assumption_snapshot_json TEXT,
    revenue_growths_json TEXT, fcf_margin REAL, assumptions_sync_status TEXT,
    assumptions_synced_at TEXT, sanity_flag TEXT, is_latest INTEGER DEFAULT 1,
    superseded_at TEXT, superseded_by_id INTEGER, input_sha256 TEXT,
    workbook_sha256 TEXT, engine_version TEXT, inputs_as_of TEXT, provenance_json TEXT
);
CREATE TABLE dcf_forecast_metric_mapping_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    engine_family TEXT NOT NULL, engine_version TEXT NOT NULL, series_key TEXT NOT NULL,
    viewspec_metric_token TEXT NOT NULL,
    canonical_metric_definition_revision_id TEXT NOT NULL, period_kind TEXT NOT NULL,
    unit_family TEXT NOT NULL, value_scale TEXT NOT NULL, currency TEXT NOT NULL, accounting_basis TEXT NOT NULL,
    consolidation_scope TEXT NOT NULL, dimensions_sha256 TEXT NOT NULL,
    dimensions_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL, reviewer_identity TEXT,
    admission_status TEXT NOT NULL, confidence TEXT NOT NULL, revision INTEGER NOT NULL
);
CREATE TABLE canonical_metric_definition_revisions (
    metric_definition_revision_id TEXT PRIMARY KEY, metric_id TEXT NOT NULL,
    revision INTEGER NOT NULL, lifecycle TEXT NOT NULL, period_kind TEXT NOT NULL,
    unit_family TEXT NOT NULL, accounting_basis TEXT NOT NULL
);
CREATE TABLE dcf_forecast_series_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dcf_run_id INTEGER NOT NULL, mapping_revision_id INTEGER NOT NULL, series_key TEXT NOT NULL,
    period_start TEXT NOT NULL, period_end TEXT NOT NULL, value REAL NOT NULL
);
"""


def test_active_migration_installs_the_forecast_plane(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    db_path = migrated_db(tmp_path / "forecast-head.db")
    conn = sqlite3.connect(db_path)
    objects = {
        (str(row[0]), str(row[1]))
        for row in conn.execute(
            "SELECT type,name FROM sqlite_master "
            "WHERE name LIKE 'dcf_forecast_%' OR name LIKE 'trg_dcf_forecast_%'"
        )
    }
    revision = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    conn.close()

    assert revision == ("0039_add_dcf_forecast_series",)
    assert ("table", "dcf_forecast_metric_mapping_revisions") in objects
    assert ("table", "dcf_forecast_series_points") in objects
    assert ("trigger", "trg_dcf_forecast_point_mapping") in objects
    assert ("trigger", "trg_dcf_forecast_mapping_no_update") in objects
    assert ("trigger", "trg_dcf_forecast_point_no_update") in objects


def _insert_migrated_mapping(
    conn: sqlite3.Connection,
    *,
    series_key: str = "revenue",
    token: str = "fin:revenue",
    dimensions_digest: str = _DIMENSIONS_DIGEST,
    evidence_json: str = _EVIDENCE_JSON,
    evidence_digest: str = _EVIDENCE_DIGEST,
    reviewer: str | None = _REVIEWER,
) -> int:
    """Seed a real canonical definition prerequisite with FK checks enabled."""
    at = datetime(2026, 1, 1, tzinfo=UTC)
    ontology = MetricOntology(conn)
    ontology.persist_metric(
        CanonicalMetric(
            metric_id="test-revenue",
            idempotency_key="test-revenue",
            canonical_name="Test revenue",
            effective_at=at,
            knowledge_at=at,
            recorded_at=at,
        )
    )
    ontology.persist_metric_definition(
        CanonicalMetricDefinitionRevision(
            metric_definition_revision_id="test-definition",
            idempotency_key="test-definition",
            metric_id="test-revenue",
            revision=1,
            lifecycle="active",
            definition_text="Test annual revenue",
            value_kind="numeric",
            period_kind="duration",
            unit_family="currency",
            accounting_basis="us_gaap",
            scope_constraints={},
            effective_at=at,
            knowledge_at=at,
            recorded_at=at,
        )
    )
    cursor = conn.execute(
        """
        INSERT INTO dcf_forecast_metric_mapping_revisions (
            ticker,engine_family,engine_version,series_key,viewspec_metric_token,
            canonical_metric_definition_revision_id,period_kind,unit_family,value_scale,currency,
            accounting_basis,consolidation_scope,dimensions_sha256,dimensions_json,
            admission_status,confidence,policy_name,policy_version,policy_config_sha256,
            evidence_json,evidence_sha256,reviewer_identity,revision,supersedes_mapping_revision_id,
            effective_at,knowledge_at,recorded_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "META",
            "redesign",
            "redesign_fcff_v1",
            series_key,
            token,
            "test-definition",
            "duration",
            "currency",
            "millions",
            "USD",
            "us_gaap",
            "consolidated",
            dimensions_digest,
            _DIMENSIONS_JSON,
            "admitted",
            "high",
            "test",
            "v1",
            _POLICY_DIGEST,
            evidence_json,
            evidence_digest,
            reviewer,
            1,
            None,
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
        ),
    )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def test_migrated_plane_enforces_fk_triggers_and_atomic_point_rollback(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    db_path = migrated_db(tmp_path / "forecast-integrity.db")
    conn = sqlite3.connect(db_path)
    register_sqlite_integrity_functions(conn)
    conn.execute("PRAGMA foreign_keys=ON")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute(
            "INSERT INTO dcf_forecast_metric_mapping_revisions "
            "(ticker,engine_family,engine_version,series_key,viewspec_metric_token,"
            "canonical_metric_definition_revision_id,period_kind,unit_family,value_scale,currency,"
            "accounting_basis,consolidation_scope,dimensions_sha256,dimensions_json,"
            "admission_status,confidence,policy_name,policy_version,policy_config_sha256,"
            "evidence_json,evidence_sha256,reviewer_identity,revision,effective_at,knowledge_at,recorded_at) "
            "VALUES ('META','redesign','redesign_fcff_v1','revenue','fin:revenue',"
            "'missing-definition','duration','currency','millions','USD','us_gaap','consolidated',"
            "?,?,'admitted','high','test','v1',?,?,?,?,1,"
            "'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')",
            (
                _DIMENSIONS_DIGEST,
                _DIMENSIONS_JSON,
                _POLICY_DIGEST,
                _EVIDENCE_JSON,
                _EVIDENCE_DIGEST,
                _REVIEWER,
            ),
        )

    mapping_id = _insert_migrated_mapping(conn)
    with pytest.raises(sqlite3.IntegrityError, match="token/semantic coordinate"):
        _insert_migrated_mapping(conn, series_key="alternate-revenue")
    assert upsert(conn, _persist_row()) is True
    run_id = int(conn.execute("SELECT id FROM dcf_runs WHERE ticker='META'").fetchone()[0])
    with pytest.raises(sqlite3.IntegrityError, match="annual fiscal period"):
        conn.execute(
            "INSERT INTO dcf_forecast_series_points "
            "(dcf_run_id,mapping_revision_id,series_key,period_start,period_end,value) "
            "VALUES (?,?,?,?,?,?)",
            (run_id, mapping_id, "revenue", "2026-01-01", "2026-03-31", 1.0),
        )

    before_runs = conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0]
    bad_row = replace(
        _persist_row(),
        forecast_points=(
            ForecastSeriesPoint(
                mapping_id, "wrong-series", date(2026, 1, 1), date(2026, 12, 31), 1.0
            ),
        ),
    )
    with pytest.raises(sqlite3.IntegrityError, match="current admitted mapping"):
        upsert(conn, bad_row)
    assert conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0] == before_runs
    assert conn.execute("SELECT COUNT(*) FROM dcf_forecast_series_points").fetchone()[0] == 0
    conn.close()


def test_migrated_plane_rejects_unreviewed_or_uncommitted_admitted_mappings(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    db_path = migrated_db(tmp_path / "forecast-admission.db")
    conn = sqlite3.connect(db_path)
    register_sqlite_integrity_functions(conn)
    conn.execute("PRAGMA foreign_keys=ON")

    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _insert_migrated_mapping(conn, series_key="no-review", token="fin:no-review", reviewer=None)
    with pytest.raises(sqlite3.IntegrityError, match="dimensions commitment mismatch"):
        _insert_migrated_mapping(
            conn,
            series_key="bad-dimensions",
            token="fin:bad-dimensions",
            dimensions_digest="b" * 64,
        )
    with pytest.raises(sqlite3.IntegrityError, match="evidence commitment mismatch"):
        _insert_migrated_mapping(
            conn,
            series_key="bad-evidence",
            token="fin:bad-evidence",
            evidence_digest="c" * 64,
        )
    empty_evidence = "{}"
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _insert_migrated_mapping(
            conn,
            series_key="no-basis",
            token="fin:no-basis",
            evidence_json=empty_evidence,
            evidence_digest=hashlib.sha256(empty_evidence.encode()).hexdigest(),
        )
    conn.close()


def _coordinate(*, definition: str = "definition:revenue:1") -> ForecastSemanticCoordinate:
    return ForecastSemanticCoordinate(
        canonical_metric_definition_revision_id=definition,
        period_kind="duration",
        unit_family="currency",
        value_scale="millions",
        currency="USD",
        accounting_basis="us_gaap",
        consolidation_scope="consolidated",
        dimensions_sha256=_DIMENSIONS_DIGEST,
    )


def _mapping(
    conn: sqlite3.Connection,
    *,
    coordinate: ForecastSemanticCoordinate,
    status: str = "admitted",
    confidence: str = "high",
    revision: int = 1,
    series_key: str = "revenue",
    token: str = "fin:revenue",
) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO canonical_metric_definition_revisions "
        "(metric_definition_revision_id,metric_id,revision,lifecycle,period_kind,unit_family,accounting_basis) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            coordinate.canonical_metric_definition_revision_id,
            coordinate.canonical_metric_definition_revision_id,
            1,
            "active",
            coordinate.period_kind,
            coordinate.unit_family,
            coordinate.accounting_basis,
        ),
    )
    cursor = conn.execute(
        """
        INSERT INTO dcf_forecast_metric_mapping_revisions (
            ticker,engine_family,engine_version,series_key,viewspec_metric_token,
            canonical_metric_definition_revision_id,period_kind,unit_family,value_scale,currency,
            accounting_basis,consolidation_scope,dimensions_sha256,dimensions_json,
            evidence_json,evidence_sha256,reviewer_identity,
            admission_status,confidence,revision
        ) VALUES ('META','redesign','redesign_fcff_v1',?,?, ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            series_key,
            token,
            coordinate.canonical_metric_definition_revision_id,
            coordinate.period_kind,
            coordinate.unit_family,
            coordinate.value_scale,
            coordinate.currency,
            coordinate.accounting_basis,
            coordinate.consolidation_scope,
            coordinate.dimensions_sha256,
            _DIMENSIONS_JSON,
            _EVIDENCE_JSON,
            _EVIDENCE_DIGEST,
            _REVIEWER,
            status,
            confidence,
            revision,
        ),
    )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def _current_run(conn: sqlite3.Connection, *, ticker: str = "META", outlier: bool = False) -> int:
    cursor = conn.execute(
        """
        INSERT INTO dcf_runs (ticker,created_at,is_latest,segment_name,sanity_flag,engine_version)
        VALUES (?, '2026-01-01T00:00:00Z', 1, NULL, ?, 'redesign_fcff_v1')
        """,
        (ticker, "outlier" if outlier else None),
    )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def test_read_adapter_returns_only_exact_admitted_current_mapping() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    coordinate = _coordinate()
    run_id = _current_run(conn)
    mapping_id = _mapping(conn, coordinate=coordinate)
    conn.execute(
        "INSERT INTO dcf_forecast_series_points "
        "(dcf_run_id,mapping_revision_id,series_key,period_start,period_end,value) "
        "VALUES (?,?,?,?,?,?)",
        (run_id, mapping_id, "revenue", "2026-01-01", "2026-12-31", 1500.0),
    )

    loaded = load_forecast_overlay(conn, ticker="META", coordinate=coordinate)

    assert loaded.reason is None
    assert loaded.overlay is not None
    assert loaded.overlay.dcf_run_id == run_id
    assert loaded.overlay.mapping_revision_id == mapping_id
    assert loaded.overlay.points[0].value == pytest.approx(1500.0)


def test_read_adapter_fails_closed_when_mapping_commitments_are_corrupted() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    coordinate = _coordinate()
    run_id = _current_run(conn)
    mapping_id = _mapping(conn, coordinate=coordinate)
    conn.execute(
        "INSERT INTO dcf_forecast_series_points "
        "(dcf_run_id,mapping_revision_id,series_key,period_start,period_end,value) "
        "VALUES (?,?,?,?,?,?)",
        (run_id, mapping_id, "revenue", "2026-01-01", "2026-12-31", 1500.0),
    )
    conn.execute(
        "UPDATE dcf_forecast_metric_mapping_revisions SET evidence_sha256=? WHERE id=?",
        ("f" * 64, mapping_id),
    )

    loaded = load_forecast_overlay(conn, ticker="META", coordinate=coordinate)

    assert loaded.overlay is None
    assert loaded.reason == "no_compatible_mapping"


def test_read_adapter_does_not_match_a_renamed_or_different_metric() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    run_id = _current_run(conn)
    mapping_id = _mapping(conn, coordinate=_coordinate())
    conn.execute(
        "INSERT INTO dcf_forecast_series_points VALUES (NULL,?,?,?,?,?,?)",
        (run_id, mapping_id, "revenue", "2026-01-01", "2026-12-31", 1500.0),
    )

    loaded = load_forecast_overlay(
        conn, ticker="META", coordinate=_coordinate(definition="definition:sales:1")
    )

    assert loaded.overlay is None
    assert loaded.reason == "no_compatible_mapping"


def test_viewspec_crosswalk_is_explicit_and_has_no_label_fallback() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    run_id = _current_run(conn)
    mapping_id = _mapping(conn, coordinate=_coordinate())
    conn.execute(
        "INSERT INTO dcf_forecast_series_points VALUES (NULL,?,?,?,?,?,?)",
        (run_id, mapping_id, "revenue", "2026-01-01", "2026-12-31", 1500.0),
    )

    admitted = load_forecast_overlay_for_metric(
        conn,
        ticker="META",
        viewspec_metric_token="fin:revenue",
        coordinate=_coordinate(),
        actual_unit="USD millions",
    )
    absent = load_forecast_overlay_for_metric(
        conn,
        ticker="META",
        viewspec_metric_token="kpi:Revenue growth",
        coordinate=_coordinate(),
        actual_unit="USD millions",
    )

    assert admitted.overlay is not None
    assert absent.overlay is None
    assert absent.reason == "no_compatible_mapping"


def test_viewspec_crosswalk_rejects_incompatible_actual_unit() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    run_id = _current_run(conn)
    mapping_id = _mapping(conn, coordinate=_coordinate())
    conn.execute(
        "INSERT INTO dcf_forecast_series_points VALUES (NULL,?,?,?,?,?,?)",
        (run_id, mapping_id, "revenue", "2026-01-01", "2026-12-31", 1500.0),
    )

    loaded = load_forecast_overlay_for_metric(
        conn,
        ticker="META",
        viewspec_metric_token="fin:revenue",
        coordinate=_coordinate(),
        actual_unit="USD actual",
    )

    assert loaded.overlay is None
    assert loaded.reason == "no_compatible_mapping"


def test_viewspec_crosswalk_rejects_an_engine_mismatch() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    run_id = _current_run(conn)
    mapping_id = _mapping(conn, coordinate=_coordinate())
    conn.execute(
        "INSERT INTO dcf_forecast_series_points VALUES (NULL,?,?,?,?,?,?)",
        (run_id, mapping_id, "revenue", "2026-01-01", "2026-12-31", 1500.0),
    )
    conn.execute("UPDATE dcf_runs SET engine_version='different_engine' WHERE id=?", (run_id,))

    loaded = load_forecast_overlay_for_metric(
        conn,
        ticker="META",
        viewspec_metric_token="fin:revenue",
        coordinate=_coordinate(),
        actual_unit="USD millions",
    )

    assert loaded.overlay is None
    assert loaded.reason == "no_compatible_mapping"


def test_read_adapter_hides_superseded_or_rejected_mapping_and_outlier_run() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    coordinate = _coordinate()
    run_id = _current_run(conn)
    mapping_id = _mapping(conn, coordinate=coordinate)
    conn.execute(
        "INSERT INTO dcf_forecast_series_points VALUES (NULL,?,?,?,?,?,?)",
        (run_id, mapping_id, "revenue", "2026-01-01", "2026-12-31", 1500.0),
    )
    _mapping(conn, coordinate=coordinate, status="retired", confidence="high", revision=2)

    assert load_forecast_overlay(conn, ticker="META", coordinate=coordinate).overlay is None
    conn.execute("UPDATE dcf_runs SET sanity_flag='outlier' WHERE id=?", (run_id,))
    loaded = load_forecast_overlay(conn, ticker="META", coordinate=coordinate)
    assert loaded.overlay is None
    assert loaded.reason == "current_run_rejected"


def test_read_adapter_fails_closed_for_ambiguous_current_semantic_mappings() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    coordinate = _coordinate()
    run_id = _current_run(conn)
    revenue_mapping = _mapping(conn, coordinate=coordinate)
    alternate_mapping = _mapping(
        conn, coordinate=coordinate, series_key="alternate_revenue", token="fin:revenue"
    )
    for mapping_id, series_key in (
        (revenue_mapping, "revenue"),
        (alternate_mapping, "alternate_revenue"),
    ):
        conn.execute(
            "INSERT INTO dcf_forecast_series_points VALUES (NULL,?,?,?,?,?,?)",
            (run_id, mapping_id, series_key, "2026-01-01", "2026-12-31", 1500.0),
        )

    assert load_forecast_overlay(conn, ticker="META", coordinate=coordinate).overlay is None
    assert (
        load_forecast_overlay_for_metric(
            conn,
            ticker="META",
            viewspec_metric_token="fin:revenue",
            coordinate=coordinate,
            actual_unit="USD millions",
        ).overlay
        is None
    )


def test_forecast_point_requires_an_exact_annual_fiscal_period() -> None:
    with pytest.raises(ValueError, match="annual fiscal period"):
        ForecastSeriesPoint(1, "revenue", date(2026, 1, 1), date(2026, 3, 31), 1.0)


def test_refresh_producer_requires_an_admitted_mapping_and_issuer_fiscal_axis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    fmp = tmp_path / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True)
    (fmp / "META_income_statement_quarterly.json").write_text(
        "["
        '{"fiscalYear":2024,"period":"Q4","date":"2024-03-31","reportedCurrency":"USD"},'
        '{"fiscalYear":2025,"period":"Q4","date":"2025-03-31","reportedCurrency":"USD"}'
        "]",
        encoding="utf-8",
    )

    def workbook_base_fiscal_year(_path: Path) -> int:
        return 2025

    monkeypatch.setattr(refresh_dcf, "_workbook_base_fiscal_year", workbook_base_fiscal_year)
    valuation = refresh_dcf.redesign_mod.RedesignValuation(
        1.0,
        1.0,
        1.0,
        1.0,
        [],
        [100.0, 120.0],
        0.1,
        "Exit multiple",
        "EV/EBITDA",
        10.0,
        1.0,
        1.0,
        0.0,
        0.0,
        1.0,
    )

    assert (
        refresh_dcf.redesign_revenue_forecast_points(
            conn,
            repo_root=tmp_path,
            ticker="META",
            workbook_path=tmp_path / "META.xlsx",
            valuation=valuation,
        )
        == ()
    )
    mapping_id = _mapping(conn, coordinate=_coordinate())
    points = refresh_dcf.redesign_revenue_forecast_points(
        conn,
        repo_root=tmp_path,
        ticker="META",
        workbook_path=tmp_path / "META.xlsx",
        valuation=valuation,
    )

    assert [(point.period_start, point.period_end, point.value) for point in points] == [
        (date(2025, 4, 1), date(2026, 3, 31), 100.0),
        (date(2026, 4, 1), date(2027, 3, 31), 120.0),
    ]
    assert {point.mapping_revision_id for point in points} == {mapping_id}


def _persist_row() -> DcfRunRow:
    return DcfRunRow(
        ticker="META",
        valuation_date=date(2026, 1, 1),
        horizon_years=5,
        wacc=0.1,
        npv=1000.0,
        npv_per_share=100.0,
        shares_outstanding=10_000_000.0,
        currency="USD",
        live_price=90.0,
        live_price_at=None,
        mos_bar_used=None,
        assumption_snapshot_json="{}",
        provenance=DcfInputProvenance(
            input_sha256="b" * 64,
            workbook_sha256="c" * 64,
            engine_version="redesign_fcff_v1",
            inputs_as_of=datetime(2026, 1, 1, tzinfo=UTC),
            detail={"equity_bridge_receipt": {"status": "verified"}},
        ),
    )


def test_persistence_inserts_typed_points_with_the_new_dcf_run() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    mapping_id = _mapping(conn, coordinate=_coordinate())
    row = replace(
        _persist_row(),
        forecast_points=(
            ForecastSeriesPoint(
                mapping_revision_id=mapping_id,
                series_key="revenue",
                period_start=date(2026, 1, 1),
                period_end=date(2026, 12, 31),
                value=1500.0,
            ),
        ),
    )

    assert upsert(conn, row) is True
    persisted = conn.execute(
        "SELECT run.ticker,point.series_key,point.period_end,point.value "
        "FROM dcf_forecast_series_points point JOIN dcf_runs run ON run.id=point.dcf_run_id"
    ).fetchone()
    assert persisted == ("META", "revenue", "2026-12-31", 1500.0)


def test_persistence_is_repeat_safe_when_points_are_unchanged() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    mapping_id = _mapping(conn, coordinate=_coordinate())
    row = replace(
        _persist_row(),
        forecast_points=(
            ForecastSeriesPoint(
                mapping_id, "revenue", date(2026, 1, 1), date(2026, 12, 31), 1500.0
            ),
        ),
    )

    assert upsert(conn, row) is True
    assert upsert(conn, row) is False
    assert conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM dcf_forecast_series_points").fetchone()[0] == 1


def test_typed_points_fail_loudly_without_the_forecast_schema() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA.split("CREATE TABLE dcf_forecast_metric_mapping_revisions")[0])
    row = replace(
        _persist_row(),
        forecast_points=(
            ForecastSeriesPoint(1, "revenue", date(2026, 1, 1), date(2026, 12, 31), 1.0),
        ),
    )

    with pytest.raises(sqlite3.OperationalError, match="forecast series tables are missing"):
        upsert(conn, row)
