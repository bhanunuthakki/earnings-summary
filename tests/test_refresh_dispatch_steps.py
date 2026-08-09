"""PR C — refresh dispatcher step selection (--steps/--skip-step) + --force.

The budget track already owns --force-budget-bypass; this covers the per-step
catalog, the canonical-order resolution, the stale-skip override, and the new
news/dcf/thesis_eval step builders.
"""

from __future__ import annotations

import io
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import refresh_dispatch as rd  # noqa: E402


class _Result:
    returncode = 0


def _managed_target(argv: list[str]) -> Path:
    assert len(argv) >= 3
    assert Path(argv[1]).name == "sqlite_bootstrap.py"
    target = Path(argv[2])
    assert target.suffix == ".py"
    return target


def _fresh_db(tmp_path: Path) -> Path:
    """A DB whose FMP pull is 'now' — so stale mode would skip fmp."""
    db = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE fmp_endpoint_status (ticker TEXT, last_pulled TIMESTAMP)")
    conn.execute(
        "INSERT INTO fmp_endpoint_status VALUES ('NU', ?)", (datetime.now(UTC).isoformat(),)
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


def test_resolve_steps_drops_unknown() -> None:
    assert rd.resolve_steps(["dcf", "bogus"]) == ["dcf"]


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

    def runner(argv: list[str], *, out: object) -> _Result:
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

    def runner(argv: list[str], *, out: object) -> _Result:
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
    news = rd._argv_news(PROJECT_ROOT, "NU")
    assert _managed_target(news).name == "fetch_news.py"
    assert "--tickers" in news and "NU" in news
    assert _managed_target(rd._argv_dcf(PROJECT_ROOT, "NU")).name == "refresh_dcf.py"
    assert (
        _managed_target(rd._argv_thesis_eval(PROJECT_ROOT, "NU")).name == "run_thesis_evaluator.py"
    )
