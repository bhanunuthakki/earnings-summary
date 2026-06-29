"""Tests for execution/run_weekly_validation.py — the weekly confidence backfill.

The validation-engine scan now runs only daily (run_morning_pipeline stage 3);
this weekly task used to re-run the identical full-population scan, inserting a
duplicate validation_issues set every Sunday. It must now do ONLY the confidence
backfill, reading the issues the daily run already inserted.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

import pytest

from execution import run_weekly_validation as mod


@dataclass
class _Outcome:
    examined: int = 1
    updated: int = 1


class _Conn:
    def close(self) -> None: ...


def test_runs_only_confidence_backfill_not_engine_scan(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The duplicate validation-engine scan was removed: the module must no longer
    # import/reference run_all_checks at all.
    assert not hasattr(mod, "run_all_checks")

    tables: list[str] = []
    statuses: list[object] = []

    def _open_db(_db: object) -> _Conn:
        return _Conn()

    def _start_run(conn: object, directive: str, ticker_scope: list[str]) -> str:
        return "RID"

    def _end_run(conn: object, rid: str, status: object, error_summary: str | None = None) -> None:
        statuses.append(status)

    def _apply(conn: object, table: str, ticker: object, apply: bool) -> _Outcome:
        tables.append(table)
        return _Outcome()

    monkeypatch.setattr(mod, "open_db", _open_db)
    monkeypatch.setattr(mod, "start_run", _start_run)
    monkeypatch.setattr(mod, "end_run", _end_run)
    monkeypatch.setattr(mod, "apply_confidence_scores", _apply)
    monkeypatch.setattr(sys, "argv", ["prog"])

    rc = mod.main()

    assert rc == 0
    # backfill ran for exactly the two fact tables — and nothing else (no scan)
    assert tables == ["financial_facts", "kpi_facts"]
    assert statuses == [mod.StageStatus.OK]
