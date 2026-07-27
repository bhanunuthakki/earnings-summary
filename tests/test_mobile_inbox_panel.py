from __future__ import annotations

import sqlite3
from pathlib import Path

from pipeline.mobile_inbox_panel import render_mobile_inbox


def test_card_dispositions_render_every_unresolved_current_card(tmp_path: Path) -> None:
    db_path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE tracked_companies (
            ticker TEXT PRIMARY KEY,
            list_type TEXT NOT NULL
        );
        CREATE TABLE llm_artifacts (
            id INTEGER PRIMARY KEY,
            ticker TEXT,
            purpose TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            superseded_by_id INTEGER
        );
        CREATE TABLE decisions (
            advice_artifact_id INTEGER,
            recommendation_kind TEXT
        );
        """
    )
    for index in range(12):
        ticker = f"T{index:02d}"
        conn.execute(
            "INSERT INTO tracked_companies (ticker, list_type) VALUES (?, 'evaluation')",
            (ticker,),
        )
        conn.execute(
            "INSERT INTO llm_artifacts "
            "(id, ticker, purpose, generated_at, superseded_by_id) "
            "VALUES (?, ?, 'investment_decision_card', ?, NULL)",
            (index + 1, ticker, f"2026-07-{index + 1:02d}"),
        )
    conn.commit()
    conn.close()

    html = render_mobile_inbox(db_path)

    assert "12 evaluation names await Pass, Watch, or Promote." in html
    assert html.count("investment decision card") == 12
    assert html.count('data-card-disposition="pass"') == 12
    assert html.count('data-card-disposition="watch"') == 12
    assert html.count('data-card-disposition="promote"') == 12
    assert "T00" in html
    assert "T11" in html

    assert "/api/research/card/" in html
    assert "data-card-artifact-id" in html
