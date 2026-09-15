from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from viewspec.engine import execute_view, metric_catalog
from viewspec.spec import MetricRef, ViewSpec, ViewSpecError


def _seed(db: Path) -> None:
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE documents (
          id INTEGER PRIMARY KEY, ticker TEXT, source_type TEXT, doc_type TEXT,
          fetched_at TEXT, source_url TEXT, source_quality_tier TEXT,
          accession_number TEXT, filing_date TEXT
        );
        CREATE TABLE financial_facts (
          id INTEGER PRIMARY KEY, ticker TEXT, period_end TEXT,
          fiscal_period_type TEXT, line_item TEXT, value TEXT, unit TEXT,
          source_doc_id INTEGER
        );
        CREATE TABLE customer_concentrations (
          id INTEGER PRIMARY KEY, ticker TEXT, fiscal_period TEXT,
          fiscal_period_type TEXT, customer_label TEXT, pct_of_revenue REAL,
          revenue_amount REAL, revenue_currency TEXT, source_doc_id INTEGER,
          source_excerpt TEXT, extracted_at TEXT
        );
        CREATE TABLE lease_commitments (
          id INTEGER PRIMARY KEY, ticker TEXT, fiscal_year INTEGER,
          as_of_date TEXT, filing_doc_id INTEGER, lease_type TEXT,
          ladder_year TEXT, ladder_calendar_year INTEGER, amount REAL,
          currency TEXT, unit TEXT, source_section_key TEXT, extracted_at TEXT
        );
        INSERT INTO documents VALUES
          (1,'TST','sec','10-K','2026-02-01','https://example.test/10k',
           'sec_official','0001','2026-01-31');
        INSERT INTO customer_concentrations VALUES
          (1,'TST','2024','FY','Customer A',0.18,180,'USD',1,'note 7','2025-02-01'),
          (2,'TST','2025','FY','Customer A',0.22,242,'USD',1,'note 7','2026-02-01'),
          (3,'TST','2025','FY','No source',0.30,NULL,'USD',NULL,'note 7','2026-02-01'),
          (4,'TST','2025','Q1','Customer Quarterly',0.12,30,'USD',1,'note 7','2025-05-01'),
          (5,'TST','2025','Q2','Customer Quarterly',0.15,42,'USD',1,'note 7','2025-08-01');
        INSERT INTO lease_commitments VALUES
          (1,'TST',2025,'2025-12-31',1,'operating','Y1',2026,12,'USD','millions','leases','2026-02-01'),
          (2,'TST',2025,'2025-12-31',1,'operating','Y2',2027,10,'USD','millions','leases','2026-02-01'),
          (3,'TST',2025,'2025-12-31',1,'operating','Thereafter',NULL,20,'USD','millions','leases','2026-02-01');
        """
    )
    conn.commit()
    conn.close()


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "detail.db"
    _seed(path)
    return path


def test_detail_tokens_round_trip_and_reject_incompatible_views() -> None:
    token = "detail:customer:Customer%20A:pct_of_revenue"
    assert MetricRef.parse_token(token).token() == token
    with pytest.raises(ViewSpecError, match="annual cadence"):
        ViewSpec.from_dict({"tickers": ["TST"], "metrics": ["detail:lease:operating:amount"]})
    with pytest.raises(ViewSpecError, match="level transform"):
        ViewSpec.from_dict(
            {
                "tickers": ["TST"],
                "metrics": ["detail:lease:operating:amount"],
                "cadence": "annual",
                "transform": "yoy",
            }
        )
    with pytest.raises(ViewSpecError, match="calendar-aligned axis"):
        ViewSpec.from_dict(
            {
                "tickers": ["TST"],
                "metrics": [
                    "detail:customer:Customer%20A:pct_of_revenue",
                    "fin:revenue",
                ],
                "cadence": "quarterly",
            }
        )
    quarterly_customer = ViewSpec.from_dict(
        {"tickers": ["TST"], "metrics": ["detail:customer:Customer%20A:pct_of_revenue"]}
    )
    assert quarterly_customer.cadence == "quarterly"
    with pytest.raises(ViewSpecError, match="level transform"):
        ViewSpec.from_dict(
            {
                "tickers": ["TST"],
                "metrics": ["detail:customer:Customer%20A:pct_of_revenue"],
                "cadence": "annual",
                "transform": "yoy",
            }
        )


def test_catalog_exposes_only_source_backed_detail_series(db: Path) -> None:
    catalog = metric_catalog(db, ["TST"])
    by_token = {str(item["token"]): item for item in catalog["detail"]}
    assert "detail:customer:Customer%20A:pct_of_revenue" in by_token
    assert "detail:customer:Customer%20A:revenue_amount" in by_token
    assert "detail:customer:No%20source:pct_of_revenue" not in by_token
    customer = by_token["detail:customer:Customer%20A:pct_of_revenue"]
    assert customer["origin"] == "source_backed_legacy"
    assert customer["required_cadence"] == "annual"
    assert customer["supported_cadences"] == ["annual"]
    assert customer["supported_transforms"] == ["level"]
    assert customer["governance_status"] == "definition_pending"
    assert "canonical definition admission is pending" in str(customer["title"])
    lease = by_token["detail:lease:operating:amount"]
    assert lease["shape"] == "ladder"
    assert lease["supported_transforms"] == ["level"]
    assert lease["origin"] == "source_backed_legacy"
    assert lease["governance_status"] == "definition_pending"
    quarterly = by_token["detail:customer:Customer%20Quarterly:pct_of_revenue"]
    assert quarterly["required_cadence"] == "quarterly"
    assert quarterly["supported_cadences"] == ["quarterly"]


def test_customer_concentration_executes_with_clickable_provenance(db: Path) -> None:
    spec = ViewSpec.from_dict(
        {
            "tickers": ["TST"],
            "metrics": ["detail:customer:Customer%20A:pct_of_revenue"],
            "cadence": "annual",
            "periods": 2,
        }
    )
    result = execute_view(spec, db_path=db)
    assert result.period_labels == ["FY2024", "FY2025"]
    assert result.rows[0].cells[-1].raw == pytest.approx(22.0)
    source = result.rows[0].cells[-1].source
    assert source is not None
    assert source.doc_id == 1
    assert source.fact_table == "customer_concentrations"
    assert any(
        "canonical observation and definition admission are pending" in warning
        for warning in result.warnings
    )


def test_quarterly_customer_concentration_is_queryable_when_it_is_the_only_cadence(
    db: Path,
) -> None:
    result = execute_view(
        ViewSpec.from_dict(
            {
                "tickers": ["TST"],
                "metrics": ["detail:customer:Customer%20Quarterly:pct_of_revenue"],
                "cadence": "quarterly",
                "periods": 2,
            }
        ),
        db_path=db,
    )

    assert result.period_labels == ["FQ1 FY2025", "FQ2 FY2025"]
    assert [cell.raw for cell in result.rows[0].cells] == pytest.approx([12.0, 15.0])
    assert any("issuer fiscal-quarter labels" in warning for warning in result.warnings)


def test_lease_ladder_uses_explicit_calendar_years_and_omits_thereafter(db: Path) -> None:
    spec = ViewSpec.from_dict(
        {
            "tickers": ["TST"],
            "metrics": ["detail:lease:operating:amount"],
            "cadence": "annual",
            "transform": "level",
            "periods": 5,
        }
    )
    result = execute_view(spec, db_path=db)
    assert result.period_labels == ["FY2026", "FY2027"]
    assert [cell.raw for cell in result.rows[0].cells] == [12.0, 10.0]


@pytest.mark.parametrize("family", ["customer", "lease"])
def test_detail_series_refuses_mixed_currency_or_scale(db: Path, family: str) -> None:
    conn = sqlite3.connect(db)
    if family == "customer":
        conn.execute("UPDATE customer_concentrations SET revenue_currency='EUR' WHERE id=2")
        token = "detail:customer:Customer%20A:revenue_amount"
    else:
        conn.execute("UPDATE lease_commitments SET unit='thousands' WHERE id=2")
        token = "detail:lease:operating:amount"
    conn.commit()
    conn.close()

    result = execute_view(
        ViewSpec.from_dict(
            {
                "tickers": ["TST"],
                "metrics": [token],
                "cadence": "annual",
                "transform": "level",
            }
        ),
        db_path=db,
    )

    assert not result.rows
    assert any("mixed reported" in warning and "omitted" in warning for warning in result.warnings)
