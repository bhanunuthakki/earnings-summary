"""Tests for cockpit_fundamentals — the precomputed fundamentals cache.

Covers: round-trip (materialize → read), missing-cache degradation, and
integration with build_cockpit_rows (cache takes priority over the DB scan
when present; DB scan fires as fallback when cache is absent).
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db as dbmod  # noqa: E402
from cockpit_fundamentals import (  # noqa: E402
    compute_from_db,
    materialize_fundamentals,
    read_materialized_fundamentals,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def head_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    db = tmp_path_factory.mktemp("fund_tmpl") / "head.db"
    dbmod.set_db_path(str(db))
    dbmod.init_db()
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.stamp(cfg, "0000_baseline")
    command.upgrade(cfg, "head")
    return db


@pytest.fixture
def conn(head_template: Path, tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db = tmp_path / "portfolio.db"
    shutil.copy(head_template, db)
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    yield c
    c.close()


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "data").mkdir(parents=True)
    return root


def _seed_quarters(
    conn: sqlite3.Connection,
    ticker: str,
    rows: list[tuple[str, str, float, float | None, float | None, float | None]],
) -> None:
    """Insert financial_facts rows for the given ticker.

    Each tuple: (period_end, fiscal_period_type, revenue, ocf, capex, fcf).
    source_doc_id increments so ROW_NUMBER() dedup works correctly.
    """
    conn.execute(
        "INSERT OR IGNORE INTO tenants (id, created_at) VALUES ('bhanu', '2026-01-01')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO tracked_companies "
        "(user_id, ticker, name, list_type, instrument_type) "
        "VALUES ('bhanu', ?, ?, 'evaluation', 'equity')",
        (ticker, ticker),
    )
    for i, (period_end, fp_type, rev, ocf, capex, fcf) in enumerate(rows):
        doc_id = i + 1
        conn.execute(
            "INSERT OR IGNORE INTO documents "
            "(ticker, source_type, doc_type, file_path, sha256, fetched_at, fetch_status, "
            "raw_bytes_size) VALUES (?, 'fmp', 'fmp_statements', ?, ?, '2026-01-01', 'ok', 10)",
            (ticker, f"data/{ticker}_{i}.json", f"{hash(ticker + str(i)):064x}"[:64]),
        )
        for line_item, value in [
            ("revenue", rev),
            ("operating_cash_flow", ocf),
            ("capital_expenditure", capex),
            ("free_cash_flow", fcf),
        ]:
            if value is not None:
                conn.execute(
                    "INSERT INTO financial_facts "
                    "(ticker, period_end, fiscal_period_type, line_item, value, unit, source_doc_id) "
                    "VALUES (?, ?, ?, ?, ?, 'actual', ?)",
                    (ticker, period_end, fp_type, line_item, value, doc_id),
                )
    conn.commit()


# ---------------------------------------------------------------------------
# compute_from_db — mirrors the _eval_fundamentals tests in test_research_cockpit
# so we confirm the logic survives the move.
# ---------------------------------------------------------------------------


def test_compute_from_db_basic(conn: sqlite3.Connection) -> None:
    _seed_quarters(
        conn,
        "BASIC",
        [
            ("2026-03-31 00:00:00", "Q1", 120.0, 30.0, -5.0, 25.0),
            ("2025-12-31 00:00:00", "Q4", 110.0, 25.0, -5.0, 20.0),
            ("2025-09-30 00:00:00", "Q3", 105.0, 20.0, -5.0, 15.0),
            ("2025-06-30 00:00:00", "Q2", 100.0, 25.0, -5.0, 20.0),
            ("2025-03-31 00:00:00", "Q1", 100.0, None, None, 99.0),  # excluded
        ],
    )
    result = compute_from_db(conn)
    rev_yoy, margin = result["BASIC"]
    assert rev_yoy == pytest.approx(20.0)
    assert margin == pytest.approx(80.0 / 435.0 * 100.0)


def test_compute_from_db_empty_table(conn: sqlite3.Connection) -> None:
    assert compute_from_db(conn) == {}


# ---------------------------------------------------------------------------
# materialize_fundamentals + read_materialized_fundamentals
# ---------------------------------------------------------------------------


def test_materialize_round_trip(conn: sqlite3.Connection, repo_root: Path) -> None:
    _seed_quarters(
        conn,
        "RT",
        [
            ("2026-03-31 00:00:00", "Q1", 120.0, 30.0, -5.0, 25.0),
            ("2025-12-31 00:00:00", "Q4", 110.0, 25.0, -5.0, 20.0),
            ("2025-09-30 00:00:00", "Q3", 105.0, 20.0, -5.0, 15.0),
            ("2025-06-30 00:00:00", "Q2", 100.0, 25.0, -5.0, 20.0),
            ("2025-03-31 00:00:00", "Q1", 100.0, None, None, 99.0),
        ],
    )
    n = materialize_fundamentals(conn, repo_root)
    assert n == 1

    cache_file = repo_root / "data" / "cockpit_fundamentals.json"
    assert cache_file.exists()

    payload = json.loads(cache_file.read_text(encoding="utf-8"))
    assert "computed_at" in payload
    assert "RT" in payload["fundamentals"]

    restored = read_materialized_fundamentals(repo_root)
    rev_yoy, margin = restored["RT"]
    assert rev_yoy == pytest.approx(20.0)
    assert margin == pytest.approx(80.0 / 435.0 * 100.0)


def test_read_missing_cache_returns_empty(repo_root: Path) -> None:
    assert read_materialized_fundamentals(repo_root) == {}


def test_read_corrupt_cache_returns_empty(repo_root: Path) -> None:
    cache = repo_root / "data" / "cockpit_fundamentals.json"
    cache.write_text("not json", encoding="utf-8")
    assert read_materialized_fundamentals(repo_root) == {}


def test_read_null_values_round_trip(conn: sqlite3.Connection, repo_root: Path) -> None:
    """A ticker with rev_yoy=None (only one quarter) round-trips correctly."""
    _seed_quarters(
        conn,
        "ONEK",
        [("2026-03-31 00:00:00", "Q1", 100.0, 30.0, -5.0, 25.0)],
    )
    materialize_fundamentals(conn, repo_root)
    restored = read_materialized_fundamentals(repo_root)
    rev_yoy, margin = restored["ONEK"]
    assert rev_yoy is None
    # margin is still computable from a single quarter (only one FCF-bearing row
    # → _ttm_fcf_margin needs 4 quarters to be confident, so it returns None too)
    assert margin is None


def test_materialize_overwrites_stale_cache(conn: sqlite3.Connection, repo_root: Path) -> None:
    _seed_quarters(
        conn,
        "STALE",
        [
            ("2026-03-31 00:00:00", "Q1", 200.0, 40.0, -10.0, 30.0),
            ("2025-12-31 00:00:00", "Q4", 190.0, 35.0, -10.0, 25.0),
            ("2025-09-30 00:00:00", "Q3", 185.0, 30.0, -10.0, 20.0),
            ("2025-06-30 00:00:00", "Q2", 180.0, 35.0, -10.0, 25.0),
            ("2025-03-31 00:00:00", "Q1", 100.0, None, None, 99.0),
        ],
    )
    # Write once, then update the DB and rematerialise.
    materialize_fundamentals(conn, repo_root)
    first = read_materialized_fundamentals(repo_root)
    rev_yoy_first = first["STALE"][0]

    # Update revenue so YoY changes.
    conn.execute(
        "UPDATE financial_facts SET value=300.0 "
        "WHERE ticker='STALE' AND line_item='revenue' AND period_end='2026-03-31 00:00:00'"
    )
    conn.commit()
    materialize_fundamentals(conn, repo_root)
    second = read_materialized_fundamentals(repo_root)
    rev_yoy_second = second["STALE"][0]

    assert rev_yoy_second is not None
    assert rev_yoy_second != pytest.approx(rev_yoy_first)


# ---------------------------------------------------------------------------
# Integration: build_cockpit_rows uses cache when present, falls back to DB
# ---------------------------------------------------------------------------


def test_build_cockpit_rows_uses_cache(conn: sqlite3.Connection, repo_root: Path) -> None:
    """build_cockpit_rows reads precomputed fundamentals from the JSON cache."""
    from pipeline.research_cockpit import build_cockpit_rows

    _seed_quarters(
        conn,
        "V",
        [
            ("2026-03-31 00:00:00", "Q1", 120.0, 30.0, -5.0, 25.0),
            ("2025-12-31 00:00:00", "Q4", 110.0, 25.0, -5.0, 20.0),
            ("2025-09-30 00:00:00", "Q3", 105.0, 20.0, -5.0, 15.0),
            ("2025-06-30 00:00:00", "Q2", 100.0, 25.0, -5.0, 20.0),
            ("2025-03-31 00:00:00", "Q1", 100.0, None, None, 99.0),
        ],
    )
    materialize_fundamentals(conn, repo_root)
    rows = build_cockpit_rows(conn, repo_root)
    by_ticker = {r.base.ticker: r for r in rows.get("evaluation", [])}
    assert "V" in by_ticker
    assert by_ticker["V"].fcf_margin_pct == pytest.approx(80.0 / 435.0 * 100.0)


def test_build_cockpit_rows_fallback_without_cache(
    conn: sqlite3.Connection, repo_root: Path
) -> None:
    """build_cockpit_rows falls back to the DB scan when no cache exists."""
    from pipeline.research_cockpit import build_cockpit_rows

    _seed_quarters(
        conn,
        "V",
        [
            ("2026-03-31 00:00:00", "Q1", 120.0, 30.0, -5.0, 25.0),
            ("2025-12-31 00:00:00", "Q4", 110.0, 25.0, -5.0, 20.0),
            ("2025-09-30 00:00:00", "Q3", 105.0, 20.0, -5.0, 15.0),
            ("2025-06-30 00:00:00", "Q2", 100.0, 25.0, -5.0, 20.0),
            ("2025-03-31 00:00:00", "Q1", 100.0, None, None, 99.0),
        ],
    )
    # No cache file — fallback path must still populate.
    rows = build_cockpit_rows(conn, repo_root)
    by_ticker = {r.base.ticker: r for r in rows.get("evaluation", [])}
    assert "V" in by_ticker
    assert by_ticker["V"].fcf_margin_pct == pytest.approx(80.0 / 435.0 * 100.0)
