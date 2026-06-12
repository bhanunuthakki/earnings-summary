"""Tests for per-line-item provenance in brief_provenance_log.

The audit log's `sources_used` JSON now carries two keys:

  * `sections` — unchanged section-level LIVE/STUB snapshot.
  * `per_metric` — sparse map of metric → {source, fact_id, fetched_at, ...}
    for the metrics whose section opted in (snapshot + financials today).

Tests cover three layers:

  * `load_financial_fact_provenance` — the tier-aware diagnostic loader that
    surfaces the chosen row's metadata. Same tier ordering as
    `load_financial_series`.
  * `snapshot.build_per_metric` / `financials.build_per_metric` — the
    section-level builders that translate facts into per_metric entries.
  * `_log_brief_provenance` — the writer that lands per_metric into
    `brief_provenance_log.sources_used`, with the `sections` key
    unchanged for backward compatibility.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from execution.build_artifacts import _log_brief_provenance  # noqa: E402
from report.sections import financials as financials_section  # noqa: E402
from report.sections import snapshot as snapshot_section  # noqa: E402
from timeseries.loaders import load_financial_fact_provenance  # noqa: E402

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _make_audit_db(db_path: Path) -> None:
    """Build a portfolio.db with the columns the production loaders expect.

    Mirrors the post-0054 schema: documents.source_quality_tier exists and
    financial_facts joins back through source_doc_id. The brief_provenance_log
    table is also present so the writer has a target.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker VARCHAR NOT NULL,
                source_type VARCHAR NOT NULL,
                doc_type VARCHAR NOT NULL,
                period_end DATETIME,
                file_path VARCHAR NOT NULL,
                sha256 VARCHAR NOT NULL,
                fetched_at DATETIME NOT NULL,
                fetch_status VARCHAR NOT NULL,
                raw_bytes_size INTEGER NOT NULL,
                source_quality_tier VARCHAR NOT NULL DEFAULT 'fmp_normalized'
            );
            CREATE TABLE financial_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker VARCHAR NOT NULL,
                period_end DATETIME NOT NULL,
                fiscal_period_type VARCHAR NOT NULL,
                line_item VARCHAR NOT NULL,
                value NUMERIC(24,6) NOT NULL,
                currency VARCHAR(3),
                unit VARCHAR NOT NULL,
                source_doc_id INTEGER NOT NULL,
                confidence FLOAT NOT NULL DEFAULT 1.0
            );
            CREATE TABLE dcf_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker VARCHAR NOT NULL,
                valuation_date VARCHAR(10) NOT NULL,
                horizon_years INTEGER NOT NULL,
                base_revenue NUMERIC(24,6),
                revenue_growths_json TEXT NOT NULL,
                fcf_margin FLOAT NOT NULL,
                wacc FLOAT NOT NULL,
                terminal_growth FLOAT NOT NULL,
                npv NUMERIC(24,6) NOT NULL,
                npv_per_share NUMERIC(24,6),
                shares_outstanding NUMERIC(24,6),
                currency VARCHAR(3),
                live_price NUMERIC(24,6),
                live_price_at DATETIME,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE brief_provenance_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                generation_date TEXT NOT NULL,
                generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                sources_used TEXT NOT NULL,
                sections_status TEXT NOT NULL,
                trigger TEXT NOT NULL,
                artifact_path TEXT NOT NULL
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def _add_document(
    db: Path,
    *,
    doc_id: int,
    ticker: str,
    source_type: str,
    source_quality_tier: str,
    fetched_at: str,
    doc_type: str = "10q",
) -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO documents(id, ticker, source_type, doc_type, file_path, "
            "sha256, fetched_at, fetch_status, raw_bytes_size, source_quality_tier) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                doc_id,
                ticker.upper(),
                source_type,
                doc_type,
                f"data/synthetic/{doc_id}.json",
                f"sha-{doc_id}",
                fetched_at,
                "ok",
                100,
                source_quality_tier,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _add_financial_fact(
    db: Path,
    *,
    ticker: str,
    line_item: str,
    period_end: str,
    fiscal_period_type: str,
    value: float,
    source_doc_id: int,
) -> int:
    conn = sqlite3.connect(str(db))
    try:
        cur = conn.execute(
            "INSERT INTO financial_facts(ticker, period_end, fiscal_period_type, "
            "line_item, value, currency, unit, source_doc_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ticker.upper(),
                period_end,
                fiscal_period_type,
                line_item,
                value,
                "USD",
                "USD",
                source_doc_id,
            ),
        )
        conn.commit()
        fact_id = cur.lastrowid
    finally:
        conn.close()
    assert fact_id is not None
    return int(fact_id)


def _add_dcf_run(
    db: Path,
    *,
    ticker: str,
    valuation_date: str,
    npv: float = 1000.0,
    npv_per_share: float = 150.0,
) -> int:
    conn = sqlite3.connect(str(db))
    try:
        cur = conn.execute(
            "INSERT INTO dcf_runs(ticker, valuation_date, horizon_years, "
            "revenue_growths_json, fcf_margin, wacc, terminal_growth, "
            "npv, npv_per_share) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ticker.upper(),
                valuation_date,
                10,
                "[]",
                0.25,
                0.10,
                0.02,
                npv,
                npv_per_share,
            ),
        )
        conn.commit()
        run_id = cur.lastrowid
    finally:
        conn.close()
    assert run_id is not None
    return int(run_id)


def _seed_facts_for_two_quarters(db: Path, ticker: str) -> dict[str, int]:
    """Insert revenue + operating_income + capital_expenditure for two quarters.

    Returns a map of (line_item -> latest-quarter winning fact_id) for
    assertions.
    """
    _add_document(
        db,
        doc_id=1,
        ticker=ticker,
        source_type="fmp",
        source_quality_tier="fmp_normalized",
        fetched_at="2026-04-10 12:00:00",
    )
    _add_document(
        db,
        doc_id=2,
        ticker=ticker,
        source_type="sec_xbrl",
        source_quality_tier="sec_official",
        fetched_at="2026-05-15 12:00:00",
    )
    # Two quarters: 2025-12-31 (Q4) and 2026-03-31 (Q1). The Q1 row from the
    # sec_xbrl doc should win the latest-period pick.
    _add_financial_fact(
        db,
        ticker=ticker,
        line_item="revenue",
        period_end="2025-12-31 00:00:00",
        fiscal_period_type="Q4",
        value=1000.0,
        source_doc_id=1,
    )
    _add_financial_fact(
        db,
        ticker=ticker,
        line_item="operating_income",
        period_end="2025-12-31 00:00:00",
        fiscal_period_type="Q4",
        value=200.0,
        source_doc_id=1,
    )
    _add_financial_fact(
        db,
        ticker=ticker,
        line_item="capital_expenditure",
        period_end="2025-12-31 00:00:00",
        fiscal_period_type="Q4",
        value=50.0,
        source_doc_id=1,
    )
    rev_q1 = _add_financial_fact(
        db,
        ticker=ticker,
        line_item="revenue",
        period_end="2026-03-31 00:00:00",
        fiscal_period_type="Q1",
        value=1100.0,
        source_doc_id=2,
    )
    oi_q1 = _add_financial_fact(
        db,
        ticker=ticker,
        line_item="operating_income",
        period_end="2026-03-31 00:00:00",
        fiscal_period_type="Q1",
        value=240.0,
        source_doc_id=2,
    )
    capex_q1 = _add_financial_fact(
        db,
        ticker=ticker,
        line_item="capital_expenditure",
        period_end="2026-03-31 00:00:00",
        fiscal_period_type="Q1",
        value=60.0,
        source_doc_id=2,
    )
    return {
        "revenue": rev_q1,
        "operating_income": oi_q1,
        "capital_expenditure": capex_q1,
    }


# ---------------------------------------------------------------------------
# Loader: tier-aware diagnostic
# ---------------------------------------------------------------------------


def test_load_financial_fact_provenance_picks_latest_period(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    _make_audit_db(db)
    expected = _seed_facts_for_two_quarters(db, "TEST")

    prov = load_financial_fact_provenance("TEST", "revenue", db_path=db)
    assert prov is not None
    assert prov["fact_id"] == expected["revenue"]
    assert prov["period_end"] == "2026-03-31"
    assert prov["source"] == "sec_official"
    assert prov["fetched_at"] == "2026-05-15 12:00:00"


def test_load_financial_fact_provenance_returns_none_for_unknown_line_item(
    tmp_path: Path,
) -> None:
    db = tmp_path / "portfolio.db"
    _make_audit_db(db)
    _seed_facts_for_two_quarters(db, "TEST")

    assert load_financial_fact_provenance("TEST", "no_such_line_item", db_path=db) is None


def test_load_financial_fact_provenance_prefers_higher_tier_within_period(
    tmp_path: Path,
) -> None:
    """When two documents target the same period, the higher tier wins —
    even when the lower-tier row was inserted later (higher id)."""
    db = tmp_path / "portfolio.db"
    _make_audit_db(db)
    _add_document(
        db,
        doc_id=1,
        ticker="TEST",
        source_type="sec_xbrl",
        source_quality_tier="sec_official",
        fetched_at="2026-05-01 09:00:00",
    )
    _add_document(
        db,
        doc_id=2,
        ticker="TEST",
        source_type="fmp",
        source_quality_tier="fmp_normalized",
        fetched_at="2026-05-20 09:00:00",
    )
    sec_id = _add_financial_fact(
        db,
        ticker="TEST",
        line_item="revenue",
        period_end="2026-03-31 00:00:00",
        fiscal_period_type="Q1",
        value=100.0,
        source_doc_id=1,
    )
    _add_financial_fact(
        db,
        ticker="TEST",
        line_item="revenue",
        period_end="2026-03-31 00:00:00",
        fiscal_period_type="Q1",
        value=99.0,
        source_doc_id=2,
    )
    prov = load_financial_fact_provenance("TEST", "revenue", db_path=db)
    assert prov is not None
    assert prov["fact_id"] == sec_id
    assert prov["source"] == "sec_official"


def test_load_financial_fact_provenance_missing_db_returns_none(tmp_path: Path) -> None:
    assert load_financial_fact_provenance("TEST", "revenue", db_path=tmp_path / "no.db") is None


# ---------------------------------------------------------------------------
# Section builders: snapshot + financials
# ---------------------------------------------------------------------------


def test_financials_build_per_metric_emits_expected_keys(tmp_path: Path) -> None:
    repo = tmp_path
    (repo / "data").mkdir()
    db = repo / "data" / "portfolio.db"
    _make_audit_db(db)
    expected = _seed_facts_for_two_quarters(db, "TEST")

    per_metric = financials_section.build_per_metric("TEST", repo)
    # Three line items seeded — revenue, operating_income, capital_expenditure.
    assert "revenue_q_latest" in per_metric
    assert "operating_income_q_latest" in per_metric
    assert "capex_q_latest" in per_metric
    # Untouched line items have no entry.
    assert "gross_profit_q_latest" not in per_metric

    assert per_metric["revenue_q_latest"]["fact_id"] == expected["revenue"]
    assert per_metric["revenue_q_latest"]["source"] == "sec_official"
    assert per_metric["revenue_q_latest"]["period_end"] == "2026-03-31"
    assert per_metric["capex_q_latest"]["fact_id"] == expected["capital_expenditure"]


def test_financials_build_per_metric_empty_when_db_missing(tmp_path: Path) -> None:
    repo = tmp_path
    # No data/portfolio.db at all.
    assert financials_section.build_per_metric("TEST", repo) == {}


def test_snapshot_build_per_metric_returns_dcf_run(tmp_path: Path) -> None:
    repo = tmp_path
    (repo / "data").mkdir()
    db = repo / "data" / "portfolio.db"
    _make_audit_db(db)
    run_id = _add_dcf_run(db, ticker="TEST", valuation_date="2026-03-31")

    per_metric = snapshot_section.build_per_metric("TEST", repo)
    assert "valuation" in per_metric
    assert per_metric["valuation"]["source"] == "dcf_run"
    assert per_metric["valuation"]["fact_id"] == run_id
    assert per_metric["valuation"]["valuation_date"] == "2026-03-31"


def test_snapshot_build_per_metric_empty_when_no_dcf(tmp_path: Path) -> None:
    repo = tmp_path
    (repo / "data").mkdir()
    db = repo / "data" / "portfolio.db"
    _make_audit_db(db)
    # No dcf_runs row inserted.
    assert snapshot_section.build_per_metric("TEST", repo) == {}


# ---------------------------------------------------------------------------
# Writer: per_metric is additive to sources_used
# ---------------------------------------------------------------------------


def test_log_brief_provenance_writes_per_metric(tmp_path: Path) -> None:
    repo = tmp_path
    (repo / "data").mkdir()
    db = repo / "data" / "portfolio.db"
    _make_audit_db(db)

    artifact = repo / "output" / "research" / "GOOG" / "2026-05-27_workspace.html"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("<html/>", encoding="utf-8")

    sections_status = {"snapshot": "ok", "financials": "ok", "bear_case": "llm_pending"}
    per_metric: dict[str, dict[str, object]] = {
        "revenue_q_latest": {
            "source": "sec_official",
            "fact_id": 12345,
            "fetched_at": "2026-05-15 12:00:00",
            "period_end": "2026-03-31",
        },
        "operating_income_q_latest": {
            "source": "fmp_normalized",
            "fact_id": 67890,
            "fetched_at": "2026-04-01 09:00:00",
            "period_end": "2026-03-31",
        },
        "valuation": {
            "source": "dcf_run",
            "fact_id": 7,
            "valuation_date": "2026-03-31",
            "fetched_at": "2026-03-31",
        },
    }

    _log_brief_provenance(
        repo_root=repo,
        ticker="GOOG",
        generation_date="2026-05-27",
        sections_status=sections_status,
        trigger="manual",
        artifact_path=artifact,
        per_metric=per_metric,
    )

    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT sources_used, sections_status FROM brief_provenance_log WHERE ticker = 'GOOG'"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    sources_used = json.loads(rows[0][0])

    # Sections key is preserved unchanged — backward compat.
    assert sources_used["sections"] == sections_status
    # Per-metric envelope is populated.
    assert sources_used["per_metric"]["revenue_q_latest"]["source"] == "sec_official"
    assert sources_used["per_metric"]["revenue_q_latest"]["fact_id"] == 12345
    assert sources_used["per_metric"]["operating_income_q_latest"]["fact_id"] == 67890
    assert sources_used["per_metric"]["valuation"]["source"] == "dcf_run"

    # sections_status column unaffected by per_metric.
    assert json.loads(rows[0][1]) == sections_status


def test_log_brief_provenance_omits_per_metric_when_none(tmp_path: Path) -> None:
    """Backward-compat regression: writer must not add an empty `per_metric`
    key when the caller doesn't pass one — keeps the JSON byte-identical to
    pre-change output for callers/readers that don't yet know about it."""
    repo = tmp_path
    (repo / "data").mkdir()
    db = repo / "data" / "portfolio.db"
    _make_audit_db(db)
    artifact = repo / "out.html"
    artifact.write_text("<html/>", encoding="utf-8")

    _log_brief_provenance(
        repo_root=repo,
        ticker="AMZN",
        generation_date="2026-05-27",
        sections_status={"snapshot": "ok"},
        trigger="manual",
        artifact_path=artifact,
    )

    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT sources_used FROM brief_provenance_log WHERE ticker = 'AMZN'"
        ).fetchone()
    finally:
        conn.close()
    sources_used = json.loads(row[0])
    assert sources_used == {"sections": {"snapshot": "ok"}}
    assert "per_metric" not in sources_used


def test_log_brief_provenance_omits_per_metric_when_empty_dict(tmp_path: Path) -> None:
    """An empty dict from a synthetic env (no facts seeded) must also keep
    the on-disk JSON in the pre-change shape — empty != absent for some
    readers, so we suppress the key when there's nothing to surface."""
    repo = tmp_path
    (repo / "data").mkdir()
    db = repo / "data" / "portfolio.db"
    _make_audit_db(db)
    artifact = repo / "out.html"
    artifact.write_text("<html/>", encoding="utf-8")

    _log_brief_provenance(
        repo_root=repo,
        ticker="META",
        generation_date="2026-05-27",
        sections_status={"snapshot": "ok"},
        trigger="manual",
        artifact_path=artifact,
        per_metric={},
    )

    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT sources_used FROM brief_provenance_log WHERE ticker = 'META'"
        ).fetchone()
    finally:
        conn.close()
    sources_used = json.loads(row[0])
    assert "per_metric" not in sources_used
