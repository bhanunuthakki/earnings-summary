"""Hermetic unit and contract tests for WIX lifecycle closure & AVDV postmortem (BHA-49)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from synthesis.wix_avdv_postmortem import (
    evaluate_wix_avdv_postmortem,
    persist_wix_avdv_postmortem,
)


def _seed_postmortem_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO position_entries (
            id, user_id, ticker, entry_date, entry_price, entry_conviction,
            entry_thesis_excerpt, entry_conditions, exit_date, exit_price,
            exit_reason, lessons, outcome_vs_thesis, source, created_at, updated_at
        ) VALUES (
            11, 'bhanu', 'WIX', '2026-02-15', 75.80, 'low',
            'Two-engine model thesis', '[]', NULL, NULL,
            NULL, NULL, NULL, 'backfill', '2026-02-15', '2026-02-15'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO decisions (
            id, ticker, recommendation_kind, decided_by, scope, size_pct, made_at, created_at
        ) VALUES (
            135, 'WIX', 'sell', 'owner', 'ticker', 2.5444, '2026-08-14T08:46:19', '2026-08-14'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO decisions (
            id, ticker, recommendation_kind, decided_by, scope, size_pct, made_at, created_at
        ) VALUES (
            136, 'AVDV', 'add', 'owner', 'ticker', 2.5444, '2026-08-14T08:46:25', '2026-08-14'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO tracked_companies (
            id, user_id, ticker, name, list_type, added_at, brief_dirty
        ) VALUES (
            84, 'bhanu', 'WIX', 'Wix', 'portfolio', '2026-05-19', 0
        )
        """
    )


def test_evaluate_wix_avdv_postmortem_counterfactual_invariant(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db_path = migrated_db(tmp_path / "postmortem_eval.db")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    _seed_postmortem_db(conn)

    result = evaluate_wix_avdv_postmortem(conn, entry_id=11)

    assert result.ticker == "WIX"
    assert result.position_entry_id == 11
    assert result.exit_date == "2026-08-14"
    assert result.exit_price == 85.0
    assert result.outcome_vs_thesis == "broke"
    # STRICT INVARIANT: AVDV comparison must be counterfactual_not_executed
    assert result.avdv_status == "counterfactual_not_executed"
    assert result.avdv_allocation_pct == 2.5444

    # Verify all 4 factors separated
    assert "Base44" in result.factor_attribution.selection
    assert "2.5444%" in result.factor_attribution.sizing
    assert "$85" in result.factor_attribution.timing
    assert len(result.factor_attribution.price_luck) > 10

    conn.close()


def test_persist_wix_avdv_postmortem_idempotency(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db_path = migrated_db(tmp_path / "postmortem_persist.db")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    _seed_postmortem_db(conn)

    result = evaluate_wix_avdv_postmortem(conn, entry_id=11)

    # First persistence pass
    ok1 = persist_wix_avdv_postmortem(conn, result, force=False)
    conn.commit()
    assert ok1 is True

    # Verify position_entries is closed
    row = conn.execute("SELECT * FROM position_entries WHERE id = 11").fetchone()
    assert row["exit_date"] == "2026-08-14"
    assert float(row["exit_price"]) == 85.0
    assert row["outcome_vs_thesis"] == "broke"
    assert row["exit_reason"] is not None
    assert row["lessons"] is not None

    # Verify analyst_notes created
    notes = conn.execute("SELECT * FROM analyst_notes WHERE position_entry_id = 11").fetchall()
    assert len(notes) == 1
    assert "counterfactual_not_executed" in notes[0]["context_json"]

    # Verify brief_dirty flag flipped
    tc = conn.execute("SELECT brief_dirty FROM tracked_companies WHERE ticker = 'WIX'").fetchone()
    assert tc["brief_dirty"] == 1

    # Second persistence pass (idempotency check)
    ok2 = persist_wix_avdv_postmortem(conn, result, force=True)
    conn.commit()
    assert ok2 is True

    # Ensure no duplicate notes created
    notes_after = conn.execute(
        "SELECT * FROM analyst_notes WHERE position_entry_id = 11"
    ).fetchall()
    assert len(notes_after) == 1

    conn.close()
