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


def test_decision_correction_is_inline_and_supports_tracker_groups(tmp_path: Path) -> None:
    db_path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE decision_drafts (
            id INTEGER PRIMARY KEY,
            source_channel TEXT NOT NULL,
            source_external_id TEXT,
            original_text TEXT NOT NULL,
            draft_json TEXT,
            status TEXT NOT NULL,
            decision_id INTEGER
        );
        CREATE TABLE decisions (
            id INTEGER PRIMARY KEY,
            size_usd REAL
        );
        """
    )
    conn.execute(
        "INSERT INTO decision_drafts VALUES "
        "(1, 'tracker', 'NU:2026-07-24:buy', 'Confirmed tracker fill', "
        '\'{"proposed_ticker":"NU","proposed_action":"buy",'
        '"proposed_amount_usd":100,"proposed_rationale":"Initial"}\', '
        "'confirmed', 10)"
    )
    conn.execute("INSERT INTO decisions VALUES (10, 100)")
    conn.execute(
        "INSERT INTO decision_drafts VALUES "
        "(2, 'tracker', 'NU:2026-07-24:buy', 'Late tracker fill', "
        '\'{"proposed_ticker":"NU","proposed_action":"buy",'
        '"proposed_amount_usd":250,"proposed_rationale":"Late"}\', '
        "'awaiting_confirmation', NULL)"
    )
    conn.commit()
    conn.close()

    html = render_mobile_inbox(db_path)

    assert 'data-draft-group-id="2"' in html
    assert "data-mi-correct-form" in html
    assert 'name="proposed_ticker" value="NU"' in html
    assert 'name="proposed_amount_usd" value="350"' in html
    assert "/api/decision-draft-groups/" in html
    assert "/correct" in html
    assert "#ledger-console?draft=" not in html
    assert "if (amount) payload.proposed_amount_usd = Number(amount);" in html
