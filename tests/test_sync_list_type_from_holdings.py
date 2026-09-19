"""Operator boundary tests for the holdings-to-roster reconciler CLI."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

import db
from execution import sync_list_type_from_holdings as cli
from list_type_reconcile import Reclassification


def test_onboard_untracked_requires_apply(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["sync_list_type_from_holdings.py", "--onboard-untracked"])

    with pytest.raises(SystemExit, match="2"):
        cli.main()

    assert "requires --apply" in capsys.readouterr().err


def test_dry_run_prints_reviewed_onboarding_command(capsys: pytest.CaptureFixture[str]) -> None:
    plan = Reclassification(untracked_held=[("NEW", "equity", 12_345.0)])

    cli.print_plan(plan, {}, applied=False, onboarded_untracked=False)

    output = capsys.readouterr().out
    assert "--apply --onboard-untracked" in output


def test_onboard_untracked_reuses_governed_tracking_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[object, ...]] = []

    def record_db_path(path: str | os.PathLike[str]) -> None:
        calls.append(("db", path))

    def record_track(ticker: str, name: str, list_type: str, user_id: str) -> None:
        calls.append(("track", ticker, name, list_type, user_id))

    monkeypatch.setattr(db, "set_db_path", record_db_path)
    monkeypatch.setattr(db, "track_company", record_track)
    monkeypatch.delenv("EARNINGS_SUMMARY_DB_PATH", raising=False)
    db_path = tmp_path / "portfolio.db"
    plan = Reclassification(
        untracked_held=[
            ("NEW", "equity", 12_345.0),
            ("NEXT", "equity", 8_765.0),
        ]
    )

    count = cli.onboard_untracked(plan, db_path=db_path, user_id="owner")

    assert count == 2
    assert os.environ["EARNINGS_SUMMARY_DB_PATH"] == str(db_path.resolve())
    assert calls == [
        ("db", db_path.resolve()),
        ("track", "NEW", "NEW", "portfolio", "owner"),
        ("track", "NEXT", "NEXT", "portfolio", "owner"),
    ]
