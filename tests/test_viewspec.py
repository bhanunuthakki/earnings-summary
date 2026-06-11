"""P5.1 ViewSpec engine: the spec model, the provenance-rich loaders, the
execution engine (transforms + calendar-bucket alignment + chips), the
fragment renderer, the saved-views store, and the metric catalog.

The fixture DB is raw DDL mirroring the alembic schema (the loader-test
convention from test_source_chips.py) — financial_facts/kpi_facts/segments
plus documents with source_quality_tier, and saved_views as 0079 creates
it (json_valid CHECK + (user_id, name) uniqueness).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from timeseries.loaders import (
    load_financial_series_with_provenance,
    load_kpi_series_with_provenance,
)
from user_state.saved_views import delete_view, get_view, list_views, save_view
from viewspec.engine import ViewCell, ViewResult, ViewRow, execute_view, metric_catalog
from viewspec.render import render_view_fragment
from viewspec.spec import MetricRef, ViewSpec, ViewSpecError

_DDL = """
CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    source_type TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    file_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    fetched_at TIMESTAMP NOT NULL,
    fetch_status TEXT NOT NULL,
    raw_bytes_size INTEGER NOT NULL DEFAULT 0,
    source_url TEXT,
    source_quality_tier TEXT NOT NULL DEFAULT 'fmp_normalized',
    accession_number TEXT,
    filing_date TEXT
);
CREATE TABLE financial_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    period_end TIMESTAMP NOT NULL,
    fiscal_period_type TEXT NOT NULL,
    line_item TEXT NOT NULL,
    value TEXT NOT NULL,
    currency TEXT,
    unit TEXT NOT NULL DEFAULT 'actual',
    source_doc_id INTEGER NOT NULL,
    locator TEXT
);
CREATE TABLE kpi_definitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    name TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT 'actual'
);
CREATE TABLE kpi_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    period_end TIMESTAMP NOT NULL,
    fiscal_period_type TEXT NOT NULL,
    kpi_definition_id INTEGER NOT NULL,
    value TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT 'actual',
    source_doc_id INTEGER NOT NULL,
    locator TEXT
);
CREATE TABLE segment_periods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    period_end TIMESTAMP NOT NULL,
    fiscal_period_type TEXT NOT NULL,
    source_doc_id INTEGER NOT NULL,
    currency TEXT,
    unit TEXT NOT NULL DEFAULT 'millions'
);
CREATE TABLE segment_dimensions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_id INTEGER NOT NULL,
    dim_type TEXT NOT NULL,
    dim_name TEXT NOT NULL,
    value NUMERIC NOT NULL,
    metric TEXT NOT NULL,
    unit TEXT
);
CREATE TABLE saved_views (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'bhanu',
    name TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CONSTRAINT ck_saved_views_name_nonempty CHECK (length(trim(name)) > 0),
    CONSTRAINT ck_saved_views_spec_json CHECK (json_valid(spec_json)),
    CONSTRAINT uq_saved_views_user_name UNIQUE (user_id, name)
);
"""

# TST quarterly revenue: 2024 Q1-Q4 then 2025 Q1-Q4 (calendar period ends).
_TST_REVENUE = [
    ("2024-03-31 00:00:00", "Q1", 100.0),
    ("2024-06-30 00:00:00", "Q2", 110.0),
    ("2024-09-30 00:00:00", "Q3", 120.0),
    ("2024-12-31 00:00:00", "Q4", 130.0),
    ("2025-03-31 00:00:00", "Q1", 120.0),
    ("2025-06-30 00:00:00", "Q2", 132.0),
    ("2025-09-30 00:00:00", "Q3", 150.0),
]


def _seed(db: Path) -> None:
    conn = sqlite3.connect(db)
    conn.executescript(_DDL)
    conn.execute(
        "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, sha256,"
        " fetched_at, fetch_status, source_url, source_quality_tier) VALUES"
        " (1, 'TST', 'fmp', 'fmp_income_statement', 'f.json', 'a', '2026-01-05 10:00:00',"
        " 'ok', 'https://fmp.example/f.json', 'fmp_normalized')"
    )
    conn.execute(
        "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, sha256,"
        " fetched_at, fetch_status, source_url, source_quality_tier, accession_number,"
        " filing_date) VALUES"
        " (2, 'TST', 'sec_xbrl', 'sec_10q', 's.json', 'b', '2026-02-01 10:00:00',"
        " 'ok', 'https://sec.example/s', 'sec_official', '0001-01-000001', '2026-01-30')"
    )
    fin = (
        "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type, line_item,"
        " value, unit, source_doc_id, locator) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
    )
    for pe, fpt, v in _TST_REVENUE:
        conn.execute(fin, ("TST", pe, fpt, "revenue", v, "actual", 1, None))
    # 2025 Q4 exists from BOTH documents: the FMP row first (value 159, higher
    # would win on plain max-id) then the SEC row — tier-aware picks SEC's 160.
    conn.execute(fin, ("TST", "2025-12-31 00:00:00", "Q4", "revenue", 159.0, "actual", 1, None))
    conn.execute(
        fin,
        (
            "TST",
            "2025-12-31 00:00:00",
            "Q4",
            "revenue",
            160.0,
            "actual",
            2,
            '{"json_path":"facts.Revenues[7]"}',
        ),
    )
    # Annual aggregates for the annual-cadence path.
    conn.execute(fin, ("TST", "2024-12-31 00:00:00", "FY", "revenue", 460.0, "actual", 1, None))
    conn.execute(fin, ("TST", "2025-12-31 00:00:00", "FY", "revenue", 562.0, "actual", 1, None))
    # net_income for the margin transform (2025 quarters only).
    for pe, fpt, v in [
        ("2025-03-31 00:00:00", "Q1", 12.0),
        ("2025-06-30 00:00:00", "Q2", 13.2),
        ("2025-09-30 00:00:00", "Q3", 18.0),
        ("2025-12-31 00:00:00", "Q4", 24.0),
    ]:
        conn.execute(fin, ("TST", pe, fpt, "net_income", v, "actual", 1, None))
    # A second ticker with a shorter history (alignment + ragged columns).
    for pe, fpt, v in [
        ("2025-03-31 00:00:00", "Q1", 200.0),
        ("2025-06-30 00:00:00", "Q2", 210.0),
        ("2025-09-30 00:00:00", "Q3", 220.0),
        ("2025-12-31 00:00:00", "Q4", 230.0),
    ]:
        conn.execute(fin, ("ALT", pe, fpt, "revenue", v, "actual", 1, None))
    # One KPI with locator-equipped facts.
    conn.execute(
        "INSERT INTO kpi_definitions (id, ticker, name, unit) VALUES (1, 'TST', 'ROE', 'percent')"
    )
    kpi = (
        "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id,"
        " value, unit, source_doc_id, locator) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
    )
    conn.execute(kpi, ("TST", "2025-09-30 00:00:00", "Q3", 1, 11.0, "percent", 1, None))
    conn.execute(
        kpi,
        ("TST", "2025-12-31 00:00:00", "Q4", 1, 12.5, "percent", 2, '{"section":"MD&A"}'),
    )
    # One segment slice across two periods.
    conn.execute(
        "INSERT INTO segment_periods (id, ticker, period_end, fiscal_period_type,"
        " source_doc_id, unit) VALUES (1, 'TST', '2025-09-30 00:00:00', 'Q3', 1, 'millions')"
    )
    conn.execute(
        "INSERT INTO segment_periods (id, ticker, period_end, fiscal_period_type,"
        " source_doc_id, unit) VALUES (2, 'TST', '2025-12-31 00:00:00', 'Q4', 1, 'millions')"
    )
    conn.execute(
        "INSERT INTO segment_dimensions (period_id, dim_type, dim_name, value, metric)"
        " VALUES (1, 'product', 'Cloud', 50, 'revenue')"
    )
    conn.execute(
        "INSERT INTO segment_dimensions (period_id, dim_type, dim_name, value, metric)"
        " VALUES (2, 'product', 'Cloud', 64, 'revenue')"
    )
    conn.commit()
    conn.close()


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "viewspec.db"
    _seed(path)
    return path


# ----------------------------------------------------------------------------
# spec model
# ----------------------------------------------------------------------------


def test_spec_round_trip() -> None:
    spec = ViewSpec.from_dict(
        {
            "tickers": ["nu", "MELI", "nu"],
            "metrics": [
                {"domain": "fin", "key": "revenue"},
                "kpi:ROE",
                "seg:product:AWS:revenue",
            ],
            "transform": "yoy",
            "cadence": "quarterly",
            "periods": 8,
            "cagr_years": 2,
        }
    )
    assert spec.tickers == ("NU", "MELI")  # uppercased + deduped, order kept
    assert spec.metrics[0] == MetricRef(domain="fin", key="revenue")
    assert spec.metrics[1].token() == "kpi:ROE"
    assert spec.metrics[2] == MetricRef(
        domain="seg", key="revenue", dim_type="product", dim_name="AWS"
    )
    assert ViewSpec.from_dict(spec.to_dict()) == spec


def test_spec_validation_aggregates_errors() -> None:
    with pytest.raises(ViewSpecError) as exc:
        ViewSpec.from_dict({"tickers": [], "metrics": [], "transform": "nope", "periods": 0})
    msg = str(exc.value)
    assert "tickers" in msg
    assert "metrics" in msg
    assert "transform" in msg
    assert "periods" in msg


def test_spec_limits_and_bad_tokens() -> None:
    with pytest.raises(ViewSpecError, match="at most"):
        ViewSpec.from_dict({"tickers": [f"T{i}" for i in range(17)], "metrics": ["fin:revenue"]})
    with pytest.raises(ViewSpecError, match="unparseable"):
        MetricRef.parse_token("bogus")
    with pytest.raises(ViewSpecError, match="dim_type"):
        MetricRef.from_dict({"domain": "seg", "key": "revenue"})
    assert MetricRef.parse_token("seg:product:AWS:revenue").label == "AWS revenue"


# ----------------------------------------------------------------------------
# provenance-rich loaders
# ----------------------------------------------------------------------------


def test_financial_sourced_loader_tier_pick(db: Path) -> None:
    obs = load_financial_series_with_provenance("TST", "revenue", db_path=db)
    assert [o.value for o in obs][-2:] == [150.0, 160.0]  # SEC 160 beats FMP 159
    last = obs[-1]
    assert last.unit == "actual"
    assert last.provenance["source"] == "sec_official"
    assert last.provenance["source_doc_id"] == 2
    assert last.provenance["accession_number"] == "0001-01-000001"
    assert last.provenance["locator"] == '{"json_path":"facts.Revenues[7]"}'
    prior = obs[-2]
    assert prior.provenance["source"] == "fmp_normalized"


def test_kpi_sourced_loader(db: Path) -> None:
    obs = load_kpi_series_with_provenance("TST", "ROE", db_path=db)
    assert [o.value for o in obs] == [11.0, 12.5]
    assert obs[-1].unit == "percent"
    assert obs[-1].provenance["locator"] == '{"section":"MD&A"}'
    assert obs[-1].provenance["source"] == "sec_official"
    assert load_kpi_series_with_provenance("TST", "missing", db_path=db) == []


def test_sourced_loader_requires_documents(tmp_path: Path) -> None:
    bare = tmp_path / "bare.db"
    conn = sqlite3.connect(bare)
    conn.execute(
        "CREATE TABLE financial_facts (id INTEGER PRIMARY KEY, ticker TEXT,"
        " period_end TIMESTAMP, fiscal_period_type TEXT, line_item TEXT,"
        " value TEXT, source_doc_id INTEGER)"
    )
    conn.commit()
    conn.close()
    assert load_financial_series_with_provenance("TST", "revenue", db_path=bare) == []


# ----------------------------------------------------------------------------
# engine
# ----------------------------------------------------------------------------


def _spec(**overrides: object) -> ViewSpec:
    base: dict[str, object] = {
        "tickers": ["TST"],
        "metrics": ["fin:revenue"],
        "transform": "level",
        "cadence": "quarterly",
        "periods": 12,
    }
    base.update(overrides)
    return ViewSpec.from_dict(base)


def test_engine_level_alignment_and_chips(db: Path) -> None:
    result = execute_view(_spec(tickers=["TST", "ALT"]), db_path=db)
    assert result.period_labels == [
        "Q1'24",
        "Q2'24",
        "Q3'24",
        "Q4'24",
        "Q1'25",
        "Q2'25",
        "Q3'25",
        "Q4'25",
    ]
    assert [r.label for r in result.rows] == ["TST · revenue", "ALT · revenue"]
    tst, alt = result.rows
    assert [c.value for c in tst.cells] == [100, 110, 120, 130, 120, 132, 150, 160]
    # ALT has no 2024 history: leading buckets are empty, not misaligned.
    assert [c.value for c in alt.cells] == [None, None, None, None, 200, 210, 220, 230]
    last = tst.cells[-1]
    assert last.source is not None
    assert last.source.source == "sec_official"
    assert last.source.doc_id == 2
    assert result.warnings == []


def test_engine_yoy_and_periods_window(db: Path) -> None:
    result = execute_view(_spec(transform="yoy", periods=4), db_path=db)
    (row,) = result.rows
    assert result.period_labels == ["Q1'25", "Q2'25", "Q3'25", "Q4'25"]
    values = [c.value for c in row.cells]
    assert values[0] == pytest.approx(20.0)  # 120 vs 100
    assert values[1] == pytest.approx(20.0)  # 132 vs 110
    assert values[2] == pytest.approx(25.0)  # 150 vs 120
    assert values[3] == pytest.approx((160 / 130 - 1) * 100)
    assert row.cells[0].raw == 120  # the underlying level rides along


def test_engine_cagr(db: Path) -> None:
    result = execute_view(_spec(transform="cagr", cagr_years=1, periods=4), db_path=db)
    (row,) = result.rows
    # 1y CAGR == YoY by construction.
    assert row.cells[0].value == pytest.approx(20.0)
    assert row.cells[3].value == pytest.approx((160 / 130 - 1) * 100)


def test_engine_margin(db: Path) -> None:
    result = execute_view(
        _spec(metrics=["fin:net_income"], transform="margin", periods=4), db_path=db
    )
    (row,) = result.rows
    values = [c.value for c in row.cells]
    assert values[0] == pytest.approx(10.0)  # 12 / 120
    assert values[3] == pytest.approx(15.0)  # 24 / 160 (SEC-picked divisor)


def test_engine_annual_cadence(db: Path) -> None:
    result = execute_view(_spec(cadence="annual", transform="yoy"), db_path=db)
    (row,) = result.rows
    assert result.period_labels == ["FY2024", "FY2025"]
    assert row.cells[0].value is None
    assert row.cells[1].value == pytest.approx((562 / 460 - 1) * 100)


def test_engine_segments_and_warnings(db: Path) -> None:
    result = execute_view(_spec(metrics=["seg:product:Cloud:revenue", "kpi:Nope"]), db_path=db)
    assert [r.label for r in result.rows] == ["TST · Cloud revenue"]
    (row,) = result.rows
    assert [c.value for c in row.cells] == [50, 64]
    assert all(c.source is None for c in row.cells)  # seg cells unchipped (v1)
    assert result.warnings == ["TST: no data for kpi:Nope"]


def test_engine_missing_db(tmp_path: Path) -> None:
    result = execute_view(_spec(), db_path=tmp_path / "absent.db")
    assert result.rows == []
    assert result.warnings == ["TST: no data for fin:revenue"]


# ----------------------------------------------------------------------------
# renderer
# ----------------------------------------------------------------------------


def test_render_fragment_matrix_chips_chart(db: Path) -> None:
    result = execute_view(_spec(metrics=["fin:revenue", "kpi:ROE"]), db_path=db)
    html_out = render_view_fragment(result)
    assert 'class="vx-matrix"' in html_out
    assert "Q1&#x27;24" in html_out  # escaped period header
    assert 'class="src-chip src-sec-official"' in html_out  # chips rendered
    assert "open in viewer" in html_out  # doc_id deep link present
    assert "<svg" in html_out  # chart included by default
    assert "12.5%" in html_out  # percent-unit KPI level formatting
    no_chart = render_view_fragment(result, include_chart=False)
    assert "<svg" not in no_chart


def test_render_yoy_heat_shading(db: Path) -> None:
    result = execute_view(_spec(transform="yoy", periods=4), db_path=db)
    html_out = render_view_fragment(result, include_chart=False)
    assert "background:rgba(2,158,115" in html_out  # positive growth shaded green
    assert "+25.0%" in html_out


def _nm_result(values: list[float | None], transform: str) -> ViewResult:
    """A hand-built single-series result — direct control over absurd values
    the fixture DB's tame revenue series can't produce."""
    spec = _spec(transform=transform)
    row = ViewRow(
        ticker="TST",
        metric=spec.metrics[0],
        label="TST · revenue",
        unit=None,
        cells=[ViewCell(value=v, raw=None, source=None) for v in values],
    )
    labels = [f"P{i}" for i in range(len(values))]
    return ViewResult(spec=spec, period_labels=labels, rows=[row], warnings=[])


def test_render_nm_policy_yoy() -> None:
    result = _nm_result([10.0, 500.0, -1748.7, 25.0], transform="yoy")
    html_out = render_view_fragment(result)
    # Beyond ±500 -> muted n/m cell, real value kept in the title tooltip.
    assert '<td class="vx-nm" title="-1748.7% — beyond ±500%, not meaningful">n/m</td>' in html_out
    # Exactly at the threshold still renders (strictly-beyond policy).
    assert "+500.0%" in html_out
    # The n/m cell is excluded from heat shading: only the 3 sane cells shade.
    assert html_out.count("background:rgba") == 3
    # Meta line counts the suppressed cells.
    assert "1 value n/m" in html_out
    # Chart y-scale excludes the absurd value: it appears once (tooltip), never
    # in the SVG's axis / end-of-line labels.
    assert html_out.count("-1748.7%") == 1
    assert "<svg" in html_out


def test_render_nm_policy_cagr_threshold() -> None:
    result = _nm_result([150.0, 250.0, -300.0], transform="cagr")
    html_out = render_view_fragment(result, include_chart=False)
    assert "+150.0%" in html_out  # under the 200% cagr limit
    assert html_out.count(">n/m</td>") == 2
    assert "beyond ±200%" in html_out
    assert "2 values n/m" in html_out


def test_render_nm_only_for_growth_transforms() -> None:
    # margin / level values are read as-is, however large.
    html_out = render_view_fragment(_nm_result([1000.0], transform="margin"), include_chart=False)
    assert "+1000.0%" in html_out
    assert ">n/m" not in html_out
    assert "n/m</div>" not in html_out  # meta line carries no count


def test_render_empty_result(db: Path) -> None:
    result = execute_view(_spec(metrics=["kpi:Nope"]), db_path=db)
    html_out = render_view_fragment(result)
    assert "Nothing to plot" in html_out
    assert "kpi:Nope" in html_out


# ----------------------------------------------------------------------------
# saved views store
# ----------------------------------------------------------------------------


def test_saved_views_crud_and_upsert(db: Path) -> None:
    spec = _spec().to_dict()
    row = save_view(name="My pivot", spec=spec, db_path=db)
    assert row.id > 0
    assert row.spec == spec

    # Same name upserts (the chip handle stays stable), different name inserts.
    spec2 = _spec(transform="yoy").to_dict()
    again = save_view(name="My pivot", spec=spec2, db_path=db)
    assert again.id == row.id
    assert again.spec["transform"] == "yoy"
    save_view(name="Another", spec=spec, db_path=db)
    assert [v.name for v in list_views(db_path=db)] == ["Another", "My pivot"]

    fetched = get_view(row.id, db_path=db)
    assert fetched is not None
    assert fetched.name == "My pivot"

    assert delete_view(row.id, db_path=db) is True
    assert delete_view(row.id, db_path=db) is False
    assert get_view(row.id, db_path=db) is None


def test_saved_views_constraints(db: Path) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        save_view(name="   ", spec={}, db_path=db)
    conn = sqlite3.connect(db)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO saved_views (user_id, name, spec_json, created_at, updated_at)"
            " VALUES ('bhanu', 'bad', 'not json', 'x', 'x')"
        )
    conn.close()


# ----------------------------------------------------------------------------
# metric catalog
# ----------------------------------------------------------------------------


def test_metric_catalog(db: Path) -> None:
    cat = metric_catalog(db, ["TST", "ALT"])
    fin_tokens = [e["token"] for e in cat["fin"]]
    assert fin_tokens[0] == "fin:revenue"  # both tickers carry it → ranked first

    def _entry(domain: str, token: str) -> dict[str, object]:
        return next(e for e in cat[domain] if e["token"] == token)

    fin = _entry("fin", "fin:revenue")
    assert fin["label"] == "revenue"
    assert fin["tickers"] == 2
    assert "title" in fin  # definition tooltip (Ask v4)
    kpi = _entry("kpi", "kpi:ROE")
    assert kpi["label"] == "ROE"
    assert kpi["tickers"] == 1
    seg = _entry("seg", "seg:product:Cloud:revenue")
    assert seg["label"] == "Cloud revenue (product)"
    assert seg["tickers"] == 1
    assert "product axis" in str(seg["title"])
    assert metric_catalog(db, []) == {"fin": [], "kpi": [], "seg": []}
    empty = metric_catalog(db.parent / "absent.db", ["TST"])
    assert empty == {"fin": [], "kpi": [], "seg": []}
