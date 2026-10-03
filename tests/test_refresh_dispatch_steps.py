"""PR C — refresh dispatcher step selection (--steps/--skip-step) + --force.

The budget track already owns --force-budget-bypass; this covers the per-step
catalog, the canonical-order resolution, the stale-skip override, and the new
news/dcf/thesis_eval step builders.
"""

from __future__ import annotations

import io
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

import pytest

from execution import refresh_dispatch as rd

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class _Result:
    returncode: int | None = 0


def _managed_target(argv: list[str]) -> Path:
    assert len(argv) >= 3
    assert Path(argv[1]).name == "sqlite_bootstrap.py"
    target = Path(argv[2])
    assert target.suffix == ".py"
    return target


def _fresh_db(tmp_path: Path) -> Path:
    """All statement receipts are fresh relative to the tests' fixed June 1 clock."""
    db = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE fmp_endpoint_status (ticker TEXT, endpoint TEXT, period TEXT, "
        "status TEXT, record_count INTEGER, last_pulled TIMESTAMP)"
    )
    conn.executemany(
        "INSERT INTO fmp_endpoint_status VALUES ('NU', ?, ?, 'ok', 12, ?)",
        [
            (endpoint, period, "2026-05-31T12:00:00+00:00")
            for endpoint in ("income-statement", "balance-sheet-statement", "cashflow-statement")
            for period in ("annual", "quarter")
        ],
    )
    conn.commit()
    conn.close()
    return db


# ----- resolve_steps -----


def test_resolve_steps_default_is_standard_chain() -> None:
    assert rd.resolve_steps() == list(rd.DEFAULT_STEPS)
    # news / dcf / thesis_eval are opt-in, not in the default chain.
    for opt_in in ("news", "dcf", "thesis_eval"):
        assert opt_in not in rd.resolve_steps()


def test_resolve_steps_subset_reordered_to_canonical() -> None:
    assert rd.resolve_steps(["dcf", "fmp"]) == ["fmp", "dcf"]


def test_resolve_steps_rejects_unknown_instead_of_running_partial_selection() -> None:
    with pytest.raises(ValueError, match="Invalid refresh step"):
        rd.resolve_steps(["dcf", "bogus"])


def test_resolve_steps_skip_removes() -> None:
    out = rd.resolve_steps(skip=["fmp"])
    assert "fmp" not in out
    assert out == [s for s in rd.DEFAULT_STEPS if s != "fmp"]


# ----- build_plan -----


def test_build_plan_force_overrides_stale_skip(tmp_path: Path) -> None:
    db = _fresh_db(tmp_path)
    now = datetime(2026, 6, 1, tzinfo=UTC)
    assert rd.build_plan(ticker="NU", mode="stale", db_path=db, now=now).skip_fmp is True
    forced = rd.build_plan(ticker="NU", mode="stale", db_path=db, now=now, force=True)
    assert forced.skip_fmp is False
    assert forced.force is True


def test_build_plan_carries_resolved_steps() -> None:
    p = rd.build_plan(
        ticker="NU", mode="full", db_path=Path("nope.db"), steps=["dcf", "build_report"]
    )
    assert p.steps == ("dcf", "build_report")


# ----- execute -----


def test_execute_runs_only_selected_steps_in_order() -> None:
    ran: list[str] = []

    def runner(argv: list[str], *, out: TextIO) -> _Result:
        del out
        ran.append(_managed_target(argv).name)
        return _Result()

    plan = rd.build_plan(
        ticker="NU", mode="full", db_path=Path("nope.db"), steps=["dcf", "thesis_eval"]
    )
    rc = rd.execute(plan, runner=runner, out=io.StringIO())
    assert rc == 0
    assert ran == ["refresh_dcf.py", "run_thesis_evaluator.py"]


def test_execute_skips_fmp_when_fresh(tmp_path: Path) -> None:
    db = _fresh_db(tmp_path)
    ran: list[str] = []

    def runner(argv: list[str], *, out: TextIO) -> _Result:
        del out
        ran.append(_managed_target(argv).name)
        return _Result()

    plan = rd.build_plan(
        ticker="NU",
        mode="stale",
        db_path=db,
        now=datetime(2026, 6, 1, tzinfo=UTC),
        steps=["fmp", "dcf"],
    )
    rd.execute(plan, runner=runner, out=io.StringIO())
    assert "fetch_fmp_historical_data.py" not in ran  # fmp skipped (fresh)
    assert "refresh_dcf.py" in ran


# ----- new step builders -----


def test_new_step_builders_point_at_the_right_clis() -> None:
    commands: list[list[str]] = []

    def runner(argv: list[str], *, out: TextIO) -> _Result:
        del out
        commands.append(argv)
        return _Result()

    plan = rd.build_plan(
        ticker="NU", mode="full", db_path=Path("nope.db"), steps=["news", "dcf", "thesis_eval"]
    )
    assert (
        rd.execute(
            plan,
            project_root=PROJECT_ROOT,
            state_root=PROJECT_ROOT / "state",
            runner=runner,
            out=io.StringIO(),
        )
        == 0
    )
    assert [_managed_target(argv).name for argv in commands] == [
        "fetch_news.py",
        "refresh_dcf.py",
        "run_thesis_evaluator.py",
    ]
    assert "--tickers" in commands[0] and "NU" in commands[0]
