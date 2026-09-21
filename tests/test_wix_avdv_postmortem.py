"""Hermetic unit and contract tests for WIX lifecycle closure & AVDV postmortem (BHA-49)."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

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


def test_evaluation_reports_missing_evidence_without_fabricated_exit(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db_path = migrated_db(tmp_path / "unavailable.db")
    with sqlite3.connect(db_path) as conn:
        _seed_postmortem_db(conn)
        conn.commit()
        before = list(conn.iterdump())
        result = evaluate_wix_avdv_postmortem(conn)
        assert result.ticker == "WIX"
        assert result.position_entry_id == 11
        assert result.status == "unavailable"
        assert "refreshed_holdings_proving_WIX_absent" in result.missing_evidence
        assert result.avdv_status == "counterfactual_not_executed"
        assert result.exit_date is None
        assert result.exit_price is None
        assert result.outcome_vs_thesis is None
        assert result.exit_reason is None
        assert result.lessons is None
        assert result.factor_attribution is None
        assert result.avdv_allocation_pct is None
        assert list(conn.iterdump()) == before


@pytest.mark.parametrize("force", [False, True])
def test_unavailable_persistence_preserves_all_owner_records(
    tmp_path: Path, migrated_db: Callable[..., Path], force: bool
) -> None:
    db_path = migrated_db(tmp_path / "preserve.db")
    with sqlite3.connect(db_path) as conn:
        _seed_postmortem_db(conn)
        conn.commit()
        result = evaluate_wix_avdv_postmortem(conn, entry_id=11)
        # Neither a caller-supplied result nor force may impersonate evidence.
        forged = result.model_copy(update={"exit_price": 85.0, "outcome_vs_thesis": "broke"})
        before = list(conn.iterdump())
        changes = conn.total_changes
        with pytest.raises(ValueError, match="evidence"):
            persist_wix_avdv_postmortem(conn, forged, force=force)
        assert conn.total_changes == changes
        assert list(conn.iterdump()) == before


def test_unknown_entry_is_not_invented(tmp_path: Path, migrated_db: Callable[..., Path]) -> None:
    with (
        sqlite3.connect(migrated_db(tmp_path / "empty.db")) as conn,
        pytest.raises(LookupError, match="WIX"),
    ):
        evaluate_wix_avdv_postmortem(conn)


@pytest.mark.parametrize("ticker", ["WIX", "OTHER"])
def test_unverified_postmortem_cannot_close_or_overwrite(
    tmp_path: Path, migrated_db: Callable[..., Path], ticker: str
) -> None:
    db_path = migrated_db(tmp_path / "guard.db")
    with sqlite3.connect(db_path) as conn:
        _seed_postmortem_db(conn)
        conn.execute(
            "UPDATE position_entries SET ticker=?, exit_reason='Owner capital allocation', "
            "lessons='Owner lesson', outcome_vs_thesis='unrelated' WHERE id=11",
            (ticker,),
        )
        conn.commit()
        before = list(conn.iterdump())
        if ticker == "OTHER":
            with pytest.raises(ValueError, match="WIX"):
                evaluate_wix_avdv_postmortem(conn, entry_id=11)
        else:
            result = evaluate_wix_avdv_postmortem(conn, entry_id=11)
            with pytest.raises(ValueError, match="evidence"):
                persist_wix_avdv_postmortem(conn, result, force=True)
        assert list(conn.iterdump()) == before


@pytest.mark.parametrize("flags", [[], ["--dry-run"], ["--force"], ["--dry-run", "--force"]])
def test_cli_holds_before_database_access(tmp_path: Path, flags: list[str]) -> None:
    # Stage the real script in an isolated checkout shape so a regression can
    # never touch the shared checkout or canonical runtime database.
    script = tmp_path / "execution" / "run_wix_avdv_postmortem.py"
    script.parent.mkdir()
    original = Path(__file__).resolve().parents[1] / "execution" / script.name
    script.write_bytes(original.read_bytes())
    result = subprocess.run(
        [sys.executable, str(script), *flags],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["status"] == "hold"
    assert json.loads(result.stdout)["reason"] == "postmortem_evidence_unavailable"
    assert not (tmp_path / "data").exists()
    assert not list(tmp_path.rglob("*.db"))
