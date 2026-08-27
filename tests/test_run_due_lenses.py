from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Never, cast
from zoneinfo import ZoneInfo

import pytest

from execution import run_due_lenses
from scheduler_manifest import load_manifest

BuildPlan = Callable[[Path, run_due_lenses.Cadence], list[tuple[str, str, str]]]
ScheduledDeadline = Callable[..., datetime]
_BUILD_PLAN = cast("BuildPlan", getattr(run_due_lenses, "_build_plan"))
_SCHEDULED_DEADLINE = cast(
    "ScheduledDeadline", getattr(run_due_lenses, "_scheduled_window_deadline")
)


def test_weekly_plan_contains_only_p2(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "portfolio.db"
    db_path.parent.mkdir(parents=True)
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE tracked_companies (ticker TEXT, list_type TEXT, archived_at TEXT)"
        )
        conn.executemany(
            "INSERT INTO tracked_companies VALUES (?, ?, NULL)",
            (("HELD", "portfolio"), ("WATCH", "watchlist"), ("INDEX", "index_member")),
        )

    def all_due(_root: Path, _lens: str, _cadence: run_due_lenses.Cadence) -> list[str]:
        return ["HELD", "WATCH", "INDEX"]

    monkeypatch.setattr(run_due_lenses, "tickers_due_for_lens_regen", all_due)

    plan = _BUILD_PLAN(tmp_path, "weekly")

    assert plan == [
        ("P2", "WATCH", "five_min_reread"),
        ("P2", "WATCH", "thesis_drift_qoq"),
    ]


def test_monthly_p3_plan_is_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def index_due(_root: Path, _lens: str, _cadence: run_due_lenses.Cadence) -> list[str]:
        return ["INDEX"]

    monkeypatch.setattr(run_due_lenses, "tickers_due_for_lens_regen", index_due)
    assert _BUILD_PLAN(tmp_path, "monthly") == []


def test_scheduled_window_rolls_stop_to_tomorrow_after_open() -> None:
    tz = ZoneInfo("America/Los_Angeles")
    now = datetime(2026, 8, 9, 23, 0, tzinfo=tz)
    deadline = _SCHEDULED_DEADLINE(now, opens_at="22:00", stops_at="02:35")
    assert deadline.isoformat() == "2026-08-10T02:35:00-07:00"


def test_scheduled_window_refuses_midday_dispatch() -> None:
    tz = ZoneInfo("America/Los_Angeles")
    now = datetime(2026, 8, 9, 12, 0, tzinfo=tz)
    assert _SCHEDULED_DEADLINE(now, opens_at="22:00", stops_at="02:35") == now


def test_oversized_plan_fails_before_dispatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    args = argparse.Namespace(
        repo_root=tmp_path,
        cadence="weekly",
        verbose=False,
        dry_run=False,
        limit=0,
        max_plan_pairs=2,
        stop_before_local="",
        window_opens_local="",
    )
    monkeypatch.setattr(run_due_lenses, "_parse_args", lambda: args)

    def oversized_plan(_root: Path, _cadence: run_due_lenses.Cadence) -> list[tuple[str, str, str]]:
        return [("P2", f"T{i}", "five_min_reread") for i in range(3)]

    def fail_dispatch(*_args: object, **_kwargs: object) -> Never:
        pytest.fail("oversized plan must not dispatch")

    monkeypatch.setattr(run_due_lenses, "_build_plan", oversized_plan)
    monkeypatch.setattr(run_due_lenses, "run_lens", fail_dispatch)

    assert run_due_lenses.main() == 2


def test_transient_lens_failure_defers_item_and_returns_retry_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    args = argparse.Namespace(
        repo_root=tmp_path,
        cadence="weekly",
        verbose=False,
        dry_run=False,
        limit=0,
        max_plan_pairs=4,
        stop_before_local="",
        window_opens_local="",
    )
    plan = [
        ("P2", "FAIL", "five_min_reread"),
        ("P2", "PASS", "five_min_reread"),
    ]
    calls: list[str] = []

    def fixed_plan(_root: Path, _cadence: run_due_lenses.Cadence) -> list[tuple[str, str, str]]:
        return plan

    def fake_run_lens(
        _lens: object, *, ticker: str | None, repo_root: Path, force: bool
    ) -> SimpleNamespace:
        del repo_root, force
        assert ticker is not None
        calls.append(ticker)
        if ticker == "FAIL":
            raise RuntimeError("temporary transport failure")
        return SimpleNamespace(id=9)

    def never_hard_stop(_exc: BaseException) -> bool:
        return False

    monkeypatch.setattr(run_due_lenses, "_parse_args", lambda: args)
    monkeypatch.setattr(run_due_lenses, "_build_plan", fixed_plan)
    monkeypatch.setattr(
        run_due_lenses, "LENSES", {"five_min_reread": SimpleNamespace(scope="ticker")}
    )
    monkeypatch.setattr(run_due_lenses, "run_lens", fake_run_lens)
    monkeypatch.setattr(run_due_lenses, "is_hard_stop", never_hard_stop)

    assert run_due_lenses.main() == 75
    assert calls == ["FAIL", "PASS"]


def test_weekly_wrapper_enforces_plan_and_protected_window_caps() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    wrapper = repo_root / "cron" / "run_weekly_p2_lens_refresh.bat"
    text = wrapper.read_text(encoding="utf-8")
    assert "--max-plan-pairs 128" in text
    assert "--window-opens-local 21:30" in text
    assert "--stop-before-local 01:35" in text

    manifest = load_manifest(repo_root / "cron" / "task_manifest.json")
    task = next(
        item for item in manifest.tasks if item.task_name.endswith("weekly_p2_lens_refresh")
    )
    assert task.schedule.start_boundary is not None
    assert task.schedule.start_boundary.endswith("T22:00:00")
    assert task.schedule.days_of_week == ("Friday",)
