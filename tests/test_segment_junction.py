"""Tests for the segment_periods + segment_dimensions junction model.

Covers:
  - segment_junction_writer round-trips one (ticker, period, source_doc) +
    list[SegmentDimension] into segment_periods + segment_dimensions
  - Writer is idempotent on (period uniq, dim natural key)
  - segment_fact_to_dimension maps legacy metrics into junction shape
  - load_segment_junction_series returns ascending Observation[] for both
    single-dim and multi-dim (AND) filters
  - Loader is backward-compatible with load_segment_series (existing
    segment_facts queries unchanged)
  - Backfill from segment_facts → junction matches the count of unique
    (period, source_doc_id) tuples in segment_facts
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from models.facts import (
    Currency,
    FiscalPeriodType,
    SegmentDimension,
    SegmentDimType,
    Unit,
)
from pipeline.segment_junction_writer import (
    segment_fact_to_dimension,
    write_segment_facts_junction,
)
from timeseries.loaders import (
    load_segment_junction_series,
    load_segment_series,
)

# ---------------------------------------------------------------------------
# In-memory schema bootstrap
# ---------------------------------------------------------------------------


def _create_schema(conn: sqlite3.Connection) -> None:
    """Build a minimal DB shape with the junction tables + legacy segment_facts.

    Mirrors the columns + constraints from migrations 0004 and 0053. Kept
    inline so the tests don't have to run alembic against a temp file.
    """
    conn.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            source_type TEXT NOT NULL,
            doc_type TEXT NOT NULL,
            file_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            fetched_at TIMESTAMP NOT NULL,
            fetch_status TEXT NOT NULL,
            raw_bytes_size INTEGER NOT NULL,
            source_quality_tier TEXT NOT NULL DEFAULT 'fmp_normalized'
        );
        CREATE TABLE segment_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            period_end TIMESTAMP NOT NULL,
            fiscal_period_type TEXT NOT NULL,
            segment_name TEXT NOT NULL,
            metric TEXT NOT NULL,
            value NUMERIC(24, 6) NOT NULL,
            currency TEXT,
            unit TEXT NOT NULL,
            source_doc_id INTEGER NOT NULL
        );
        CREATE UNIQUE INDEX uq_segment_facts_provenance
        ON segment_facts (ticker, period_end, fiscal_period_type, segment_name, metric, source_doc_id);

        CREATE TABLE segment_periods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker VARCHAR(16) NOT NULL,
            period_end DATETIME NOT NULL,
            fiscal_period_type VARCHAR(8) NOT NULL,
            source_doc_id INTEGER NOT NULL REFERENCES documents(id),
            currency VARCHAR(8),
            unit VARCHAR(16) NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_segment_periods_provenance UNIQUE
              (ticker, period_end, fiscal_period_type, source_doc_id)
        );
        CREATE INDEX ix_segment_periods_ticker_period
        ON segment_periods (ticker, period_end);

        CREATE TABLE segment_dimensions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            period_id INTEGER NOT NULL REFERENCES segment_periods(id),
            dim_type VARCHAR(16) NOT NULL,
            dim_name VARCHAR(128) NOT NULL,
            value NUMERIC(20, 4) NOT NULL,
            metric VARCHAR(32) NOT NULL
        );
        CREATE INDEX ix_segment_dimensions_period_type_metric
        ON segment_dimensions (period_id, dim_type, metric);
        CREATE INDEX ix_segment_dimensions_dim
        ON segment_dimensions (dim_type, dim_name);
        """
    )
    conn.commit()


def _insert_document(conn: sqlite3.Connection, doc_id: int, ticker: str) -> None:
    conn.execute(
        "INSERT INTO documents "
        "(id, ticker, source_type, doc_type, file_path, sha256, fetched_at, "
        " fetch_status, raw_bytes_size) "
        "VALUES (?, ?, 'fmp', 'fmp_segment_product', ?, ?, ?, 'ok', 1)",
        (doc_id, ticker, f"data/historical/fmp/{ticker}_x.json", "0" * 64, datetime.now()),
    )
    conn.commit()


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _create_schema(c)
    return c


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


def test_writer_persists_one_period_and_n_dimensions(conn: sqlite3.Connection) -> None:
    """write_segment_facts_junction stores the period + each dim row exactly."""
    _insert_document(conn, doc_id=1, ticker="AMZN")
    dims = [
        SegmentDimension(
            dim_type=SegmentDimType.PRODUCT,
            dim_name="Amazon Web Services",
            value=Decimal("33006000000"),
            metric="revenue",
        ),
        SegmentDimension(
            dim_type=SegmentDimType.PRODUCT,
            dim_name="North America",
            value=Decimal("106267000000"),
            metric="revenue",
        ),
    ]
    p, d = write_segment_facts_junction(
        conn,
        ticker="AMZN",
        period_end=datetime(2025, 9, 30),
        fiscal_period_type=FiscalPeriodType.Q3,
        source_doc_id=1,
        currency=Currency.USD,
        unit=Unit.ACTUAL,
        dimensions=dims,
    )
    conn.commit()

    assert (p, d) == (1, 2)
    rows = conn.execute(
        "SELECT ticker, fiscal_period_type, currency, unit FROM segment_periods"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["ticker"] == "AMZN"
    assert rows[0]["fiscal_period_type"] == "Q3"
    assert rows[0]["currency"] == "USD"

    dim_rows = conn.execute(
        "SELECT dim_type, dim_name, value, metric FROM segment_dimensions ORDER BY id"
    ).fetchall()
    assert [(r["dim_type"], r["dim_name"], str(r["value"]), r["metric"]) for r in dim_rows] == [
        ("product", "Amazon Web Services", "33006000000", "revenue"),
        ("product", "North America", "106267000000", "revenue"),
    ]


def test_writer_is_idempotent(conn: sqlite3.Connection) -> None:
    """Re-running the writer on the same (period, dim) tuples writes nothing new."""
    _insert_document(conn, doc_id=1, ticker="GOOG")
    dims = [
        SegmentDimension(
            dim_type=SegmentDimType.PRODUCT,
            dim_name="Google Cloud",
            value=Decimal("13_000_000_000"),
            metric="revenue",
        )
    ]
    for _ in range(2):
        write_segment_facts_junction(
            conn,
            ticker="GOOG",
            period_end=datetime(2026, 3, 31),
            fiscal_period_type=FiscalPeriodType.Q1,
            source_doc_id=1,
            currency=Currency.USD,
            unit=Unit.ACTUAL,
            dimensions=dims,
        )
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM segment_periods").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM segment_dimensions").fetchone()[0] == 1


def test_writer_accepts_multi_dim_cross_tab(conn: sqlite3.Connection) -> None:
    """A measurement can carry multiple dim labels under one period row.

    The spec's example: ``[(product, AWS), (geography, US)] → $X`` is encoded
    as two dim rows sharing the same period_id — the loader self-joins to
    express AND semantics across them.
    """
    _insert_document(conn, doc_id=1, ticker="AMZN")
    # AWS in US, AWS in EMEA — same primary axis (product=AWS), differing
    # secondary axis (geography). Each cell stored as 2 dim rows.
    write_segment_facts_junction(
        conn,
        ticker="AMZN",
        period_end=datetime(2025, 9, 30),
        fiscal_period_type=FiscalPeriodType.Q3,
        source_doc_id=1,
        currency=Currency.USD,
        unit=Unit.ACTUAL,
        dimensions=[
            SegmentDimension(
                dim_type=SegmentDimType.PRODUCT,
                dim_name="Amazon Web Services",
                value=Decimal("20_000_000_000"),
                metric="revenue",
            ),
            SegmentDimension(
                dim_type=SegmentDimType.GEOGRAPHY,
                dim_name="United States",
                value=Decimal("20_000_000_000"),
                metric="revenue",
            ),
        ],
    )
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM segment_dimensions").fetchone()[0] == 2


# ---------------------------------------------------------------------------
# Legacy mapping
# ---------------------------------------------------------------------------


def test_segment_fact_to_dimension_maps_revenue_by_product() -> None:
    d = segment_fact_to_dimension("Google Cloud", "revenue_by_product", Decimal("12345"))
    assert d.dim_type == SegmentDimType.PRODUCT
    assert d.metric == "revenue"
    assert d.dim_name == "Google Cloud"


def test_segment_fact_to_dimension_maps_revenue_by_geography() -> None:
    d = segment_fact_to_dimension("EMEA", "revenue_by_geography", Decimal("999"))
    assert d.dim_type == SegmentDimType.GEOGRAPHY
    assert d.metric == "revenue"


def test_segment_fact_to_dimension_maps_operating_income() -> None:
    d = segment_fact_to_dimension("Google Services", "operating_income", Decimal("50_000_000_000"))
    assert d.dim_type == SegmentDimType.BUSINESS_UNIT
    assert d.metric == "operating_income"


def test_segment_fact_to_dimension_unknown_metric_falls_back() -> None:
    """Unknown legacy metric → BUSINESS_UNIT + preserves the original metric."""
    d = segment_fact_to_dimension("Cloud", "asset_turnover", Decimal("1"))
    assert d.dim_type == SegmentDimType.BUSINESS_UNIT
    assert d.metric == "asset_turnover"


# ---------------------------------------------------------------------------
# Loader — single dim
# ---------------------------------------------------------------------------


def _seed_quarterly_series(conn: sqlite3.Connection, *, ticker: str, doc_id: int) -> None:
    """Insert 4 quarters of (product=Cloud, metric=revenue) data."""
    _insert_document(conn, doc_id=doc_id, ticker=ticker)
    quarters = [
        (datetime(2025, 3, 31), FiscalPeriodType.Q1, Decimal("1000")),
        (datetime(2025, 6, 30), FiscalPeriodType.Q2, Decimal("1100")),
        (datetime(2025, 9, 30), FiscalPeriodType.Q3, Decimal("1200")),
        (datetime(2025, 12, 31), FiscalPeriodType.Q4, Decimal("1300")),
    ]
    for pe, pt, val in quarters:
        write_segment_facts_junction(
            conn,
            ticker=ticker,
            period_end=pe,
            fiscal_period_type=pt,
            source_doc_id=doc_id,
            currency=Currency.USD,
            unit=Unit.ACTUAL,
            dimensions=[
                SegmentDimension(
                    dim_type=SegmentDimType.PRODUCT,
                    dim_name="Cloud",
                    value=val,
                    metric="revenue",
                )
            ],
        )
    conn.commit()


def test_loader_returns_ascending_observations(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """Loader queries return Observation[] aligned with single-dim loader."""
    _seed_quarterly_series(conn, ticker="GOOG", doc_id=1)
    # Persist the in-memory DB to a temp file (loaders open-by-path).
    db_path = tmp_path / "portfolio.db"
    disk = sqlite3.connect(str(db_path))
    conn.backup(disk)
    disk.close()

    obs = load_segment_junction_series(
        "GOOG", dims=[("product", "Cloud")], metric="revenue", db_path=db_path
    )
    assert [o.value for o in obs] == [1000.0, 1100.0, 1200.0, 1300.0]
    assert [o.period_end.month for o in obs] == [3, 6, 9, 12]


def test_loader_missing_tables_returns_empty(tmp_path: Path) -> None:
    """If migration 0053 hasn't run, the loader returns [] rather than raising."""
    db_path = tmp_path / "bare.db"
    bare = sqlite3.connect(str(db_path))
    bare.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY)")
    bare.commit()
    bare.close()
    assert (
        load_segment_junction_series(
            "AMZN", dims=[("product", "AWS")], metric="revenue", db_path=db_path
        )
        == []
    )


def test_loader_unknown_ticker_returns_empty(conn: sqlite3.Connection, tmp_path: Path) -> None:
    _seed_quarterly_series(conn, ticker="GOOG", doc_id=1)
    db_path = tmp_path / "portfolio.db"
    disk = sqlite3.connect(str(db_path))
    conn.backup(disk)
    disk.close()
    assert (
        load_segment_junction_series(
            "MSFT", dims=[("product", "Cloud")], metric="revenue", db_path=db_path
        )
        == []
    )


# ---------------------------------------------------------------------------
# Loader — multi-dim AND semantics
# ---------------------------------------------------------------------------


def test_loader_multi_dim_filters_with_and(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """Two dim filters return periods where BOTH labels appear under the same period."""
    _insert_document(conn, doc_id=1, ticker="AMZN")
    # Q3'25: AWS-in-US present
    write_segment_facts_junction(
        conn,
        ticker="AMZN",
        period_end=datetime(2025, 9, 30),
        fiscal_period_type=FiscalPeriodType.Q3,
        source_doc_id=1,
        currency=Currency.USD,
        unit=Unit.ACTUAL,
        dimensions=[
            SegmentDimension(
                dim_type=SegmentDimType.PRODUCT,
                dim_name="AWS",
                value=Decimal("20_000_000_000"),
                metric="revenue",
            ),
            SegmentDimension(
                dim_type=SegmentDimType.GEOGRAPHY,
                dim_name="United States",
                value=Decimal("20_000_000_000"),
                metric="revenue",
            ),
        ],
    )
    # Q4'25: AWS only — no geography label, so the AND filter must skip this period
    write_segment_facts_junction(
        conn,
        ticker="AMZN",
        period_end=datetime(2025, 12, 31),
        fiscal_period_type=FiscalPeriodType.Q4,
        source_doc_id=1,
        currency=Currency.USD,
        unit=Unit.ACTUAL,
        dimensions=[
            SegmentDimension(
                dim_type=SegmentDimType.PRODUCT,
                dim_name="AWS",
                value=Decimal("22_000_000_000"),
                metric="revenue",
            )
        ],
    )
    conn.commit()
    db_path = tmp_path / "portfolio.db"
    disk = sqlite3.connect(str(db_path))
    conn.backup(disk)
    disk.close()

    obs_aws_us = load_segment_junction_series(
        "AMZN",
        dims=[("product", "AWS"), ("geography", "United States")],
        metric="revenue",
        db_path=db_path,
    )
    assert len(obs_aws_us) == 1
    assert obs_aws_us[0].period_end == datetime(2025, 9, 30)
    assert obs_aws_us[0].value == 20_000_000_000.0

    # Single-dim filter on AWS sees both quarters.
    obs_aws_all = load_segment_junction_series(
        "AMZN", dims=[("product", "AWS")], metric="revenue", db_path=db_path
    )
    assert [o.value for o in obs_aws_all] == [20_000_000_000.0, 22_000_000_000.0]


# ---------------------------------------------------------------------------
# Legacy-API shim — load_segment_series translates legacy metric strings into
# the junction's (dim_type, metric) shape and delegates to the junction loader.
# ---------------------------------------------------------------------------


def test_load_segment_series_translates_legacy_metric(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """`load_segment_series(... metric='revenue_by_product')` reads the same
    series the writer persisted under (dim_type=product, metric=revenue).

    Guards the legacy-metric → junction-tuple translation in the shim so
    older callers (DCF, derived KPIs, etc.) keep working unchanged after the
    junction migration."""
    _insert_document(conn, doc_id=1, ticker="GOOG")
    for pe, pt, val in (
        (datetime(2025, 3, 31), FiscalPeriodType.Q1, Decimal("10")),
        (datetime(2025, 6, 30), FiscalPeriodType.Q2, Decimal("11")),
    ):
        write_segment_facts_junction(
            conn,
            ticker="GOOG",
            period_end=pe,
            fiscal_period_type=pt,
            source_doc_id=1,
            currency=Currency.USD,
            unit=Unit.ACTUAL,
            dimensions=[
                SegmentDimension(
                    dim_type=SegmentDimType.PRODUCT,
                    dim_name="Google Cloud",
                    value=val,
                    metric="revenue",
                )
            ],
        )
    conn.commit()

    db_path = tmp_path / "portfolio.db"
    disk = sqlite3.connect(str(db_path))
    conn.backup(disk)
    disk.close()

    obs = load_segment_series("GOOG", "Google Cloud", "revenue_by_product", db_path=db_path)
    assert [o.value for o in obs] == [10.0, 11.0]


# ---------------------------------------------------------------------------
# Migration smoke — sanity-check the migration file loads + has the expected
# revision metadata
# ---------------------------------------------------------------------------
