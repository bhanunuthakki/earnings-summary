"""Historical ETF output remains readable without an active research product."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

from llm_artifact_store import UpsertRequest, upsert
from pipeline.etf_workup import render_etf_workup


def _seed(database: Path, *, kind: str = "etf") -> None:
    with closing(sqlite3.connect(database)) as conn:
        conn.execute(
            "INSERT INTO tracked_companies(ticker,name,list_type,instrument_type) "
            "VALUES('FUND','Synthetic fund','evaluation',?)",
            (kind,),
        )
        conn.commit()


def _version(database: Path, index: int) -> int:
    ident, _ = upsert(
        UpsertRequest(
            ticker="FUND",
            purpose="etf_role_synthesis",
            scope="etf",
            content_json={
                "role_summary": f"Saved diversification note {index}.",
                "what_it_adds": ["Saved international exposure observation"],
                "overlap_caution": None,
                "verdict": "diversifier",
                "suggested_weight_band": "3-5%",
                "watch_items": [],
            },
            cache_inputs=[f"synthetic-saved-version-{index}"],
        ),
        db_path=database,
    )
    assert ident is not None
    return ident


def test_current_and_superseded_saved_versions_keep_identity_dates_and_bytes(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "data/portfolio.db")
    _seed(database)
    old = _version(database, 1)
    new = _version(database, 2)
    with closing(sqlite3.connect(database)) as conn:
        conn.execute(
            "UPDATE llm_artifacts SET generated_at='2020-01-01T00:00:00+00:00' WHERE id=?", (old,)
        )
        conn.execute(
            "UPDATE llm_artifacts SET generated_at='2020-02-01T00:00:00+00:00' WHERE id=?", (new,)
        )
        conn.commit()
        before = conn.execute("SELECT * FROM llm_artifacts ORDER BY id").fetchall()
        factory = conn.row_factory
        html = render_etf_workup(conn, tmp_path, database, "FUND")
        after = conn.execute("SELECT * FROM llm_artifacts ORDER BY id").fetchall()
        assert conn.row_factory is factory
    assert before == after
    assert html is not None
    assert f"Version #{old}" in html and f"Version #{new}" in html
    assert "Saved diversification note 1." in html and "Saved diversification note 2." in html
    assert "2020-01-01" in html and "2020-02-01" in html
    assert "superseded" in html and "Historical" in html
    assert "Historical proposed weight: 3-5%" in html
    assert "build_etf_workup" not in html and "fetch_etf" not in html
    assert "Style loadings" not in html and "What-if" not in html
    assert "P/E" not in html
    assert "/ticker/FUND" not in html


def test_unreadable_version_does_not_hide_older_history_pages(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "data/portfolio.db")
    _seed(database)
    identifiers = [_version(database, i) for i in range(22)]
    with closing(sqlite3.connect(database)) as conn:
        # A changed body without a matching commitment is unreadable, not absent.
        conn.execute("UPDATE llm_artifacts SET content_json='{}' WHERE id=?", (identifiers[4],))
        conn.commit()
        before = conn.execute("SELECT * FROM llm_artifacts ORDER BY id").fetchall()
        first = render_etf_workup(conn, tmp_path, database, "FUND")
        second = render_etf_workup(conn, tmp_path, database, "FUND", page=1)
        assert before == conn.execute("SELECT * FROM llm_artifacts ORDER BY id").fetchall()
    assert first is not None and second is not None
    assert first.count("<details>") == 20 and second.count("<details>") == 2
    assert f"Version #{identifiers[4]}" in first and "Unavailable" in first
    assert f"Version #{identifiers[0]}" in second and "Saved diversification note 0." in second
    assert "more saved versions" in first and "page=1" in first


def test_empty_and_wrong_instrument_history_are_explicit(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "data/portfolio.db")
    _seed(database, kind="equity")
    with closing(sqlite3.connect(database)) as conn:
        assert render_etf_workup(conn, tmp_path, database, "FUND") is None
        conn.execute("UPDATE tracked_companies SET instrument_type='etf' WHERE ticker='FUND'")
        conn.commit()
        html = render_etf_workup(conn, tmp_path, database, "FUND")
    assert html is not None and "No saved versions" in html
    assert "build_etf_workup" not in html and "fetch_etf" not in html
