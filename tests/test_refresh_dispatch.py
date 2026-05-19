"""Tests for execution/refresh_dispatch.py.

Two layers:
- `build_plan` (pure) — given DB state + clock, decide what to skip.
- `execute` (impure) — with a mock runner, verify step ordering, the
  --skip routing, exit-code aggregation.
"""

from __future__ import annotations

import io
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "execution"))

import refresh_dispatch  # noqa: E402
from refresh_dispatch import Plan, build_plan, execute  # noqa: E402


def _seed_fmp(db_path: Path, ticker: str, last_pulled: str) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fmp_endpoint_status (
            ticker TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            period TEXT NOT NULL,
            status TEXT,
            http_code INTEGER,
            record_count INTEGER,
            earliest_date TEXT,
            latest_date TEXT,
            file_path TEXT,
            file_bytes INTEGER,
            error_msg TEXT,
            last_pulled TIMESTAMP
        )
        """
    )
    conn.execute(
        "INSERT INTO fmp_endpoint_status (ticker, endpoint, period, status, last_pulled) "
        "VALUES (?, 'income-statement', 'annual', 'ok', ?)",
        (ticker.upper(), last_pulled),
    )
    conn.commit()
    conn.close()


# ---- build_plan tests ----------------------------------------------------


def test_plan_full_mode_never_skips(tmp_path):
    db = tmp_path / "p.db"
    _seed_fmp(db, "NU", "2026-05-11T01:02:14")
    plan = build_plan(ticker="NU", mode="full", db_path=db, now=datetime(2026, 5, 18, tzinfo=UTC))
    assert plan == Plan(ticker="NU", mode="full", skip_fmp=False, skip_fmp_reason=None)


def test_plan_stale_skips_fmp_when_pulled_within_window(tmp_path):
    db = tmp_path / "p.db"
    _seed_fmp(db, "NU", "2026-05-15T01:00:00")  # 3 days before now
    plan = build_plan(
        ticker="NU",
        mode="stale",
        db_path=db,
        stale_fmp_days=7,
        now=datetime(2026, 5, 18, tzinfo=UTC),
    )
    assert plan.skip_fmp is True
    assert plan.skip_fmp_reason is not None
    assert "fresh last_pulled=2026-05-15T01:00:00" in plan.skip_fmp_reason


def test_plan_stale_runs_fmp_when_outside_window(tmp_path):
    db = tmp_path / "p.db"
    _seed_fmp(db, "NU", "2026-05-01T01:00:00")  # 17 days before now
    plan = build_plan(
        ticker="NU",
        mode="stale",
        db_path=db,
        stale_fmp_days=7,
        now=datetime(2026, 5, 18, tzinfo=UTC),
    )
    assert plan.skip_fmp is False
    assert plan.skip_fmp_reason is None


def test_plan_stale_runs_fmp_when_no_history(tmp_path):
    """No fmp_endpoint_status rows at all → run the FMP step."""
    db = tmp_path / "p.db"
    sqlite3.connect(str(db)).executescript(
        """
        CREATE TABLE fmp_endpoint_status (
            ticker TEXT, endpoint TEXT, period TEXT, status TEXT,
            http_code INTEGER, record_count INTEGER, earliest_date TEXT,
            latest_date TEXT, file_path TEXT, file_bytes INTEGER,
            error_msg TEXT, last_pulled TIMESTAMP
        );
        """
    )
    plan = build_plan(
        ticker="NU",
        mode="stale",
        db_path=db,
        stale_fmp_days=7,
        now=datetime(2026, 5, 18, tzinfo=UTC),
    )
    assert plan.skip_fmp is False


def test_plan_stale_runs_fmp_when_db_missing(tmp_path):
    plan = build_plan(
        ticker="NU",
        mode="stale",
        db_path=tmp_path / "does_not_exist.db",
        now=datetime(2026, 5, 18, tzinfo=UTC),
    )
    assert plan.skip_fmp is False


def test_plan_stale_respects_custom_window(tmp_path):
    db = tmp_path / "p.db"
    _seed_fmp(db, "NU", "2026-05-15T01:00:00")  # 3 days before now
    # Window = 2 days → 3-day-old data is OUTSIDE → run FMP
    plan = build_plan(
        ticker="NU", mode="stale", db_path=db, stale_fmp_days=2,
        now=datetime(2026, 5, 18, tzinfo=UTC),
    )
    assert plan.skip_fmp is False


# ---- execute tests -------------------------------------------------------


class _MockRunner:
    """Replacement for subprocess.run that records argv lists + scripted exit codes."""

    def __init__(self, exit_codes: list[int] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._exit_codes = list(exit_codes or [])

    def __call__(self, argv: list[str], *, out):
        self.calls.append(argv)
        rc = self._exit_codes.pop(0) if self._exit_codes else 0
        out.write(f"<mock step output for {Path(argv[1]).stem}>\n")

        class _Result:
            returncode = rc

        return _Result()


def test_execute_full_runs_all_six_steps(tmp_path):
    runner = _MockRunner()
    plan = Plan(ticker="NU", mode="full", skip_fmp=False, skip_fmp_reason=None)
    out = io.StringIO()
    rc = execute(plan, project_root=tmp_path, out=out, runner=runner)
    assert rc == 0
    script_names = [Path(call[1]).stem for call in runner.calls]
    assert script_names == [
        "fetch_fmp_historical_data",
        "backfill_transcripts",
        "process_ir_documents",
        "extract_kpis_from_summaries",
        "build_saydo_pairs",
        "build_artifacts",
    ]


def test_execute_stale_skips_fmp_when_planned(tmp_path):
    runner = _MockRunner()
    plan = Plan(
        ticker="NU", mode="stale", skip_fmp=True,
        skip_fmp_reason="fresh last_pulled=2026-05-15T01:00:00",
    )
    out = io.StringIO()
    rc = execute(plan, project_root=tmp_path, out=out, runner=runner)
    assert rc == 0
    script_names = [Path(call[1]).stem for call in runner.calls]
    assert "fetch_fmp_historical_data" not in script_names
    assert script_names == [
        "backfill_transcripts",
        "process_ir_documents",
        "extract_kpis_from_summaries",
        "build_saydo_pairs",
        "build_artifacts",
    ]
    assert "step=fmp action=skip reason=fresh" in out.getvalue()


def test_execute_emits_dispatch_markers(tmp_path):
    runner = _MockRunner()
    plan = Plan(ticker="NU", mode="full", skip_fmp=False, skip_fmp_reason=None)
    out = io.StringIO()
    execute(plan, project_root=tmp_path, out=out, runner=runner)
    log = out.getvalue()
    assert "[dispatch] ticker=NU mode=full" in log
    assert "[dispatch] step=fmp action=start" in log
    assert "[dispatch] step=fmp action=end rc=0" in log
    assert "[dispatch] all_done rc=0" in log


def test_execute_keeps_going_after_step_failure(tmp_path):
    """A failed FMP step must not block subsequent steps — same semantics as full_refresh.bat."""
    runner = _MockRunner(exit_codes=[1, 0, 0, 0, 0, 0])  # FMP fails, rest succeed
    plan = Plan(ticker="NU", mode="full", skip_fmp=False, skip_fmp_reason=None)
    out = io.StringIO()
    rc = execute(plan, project_root=tmp_path, out=out, runner=runner)
    assert rc == 1  # remembered the failure
    assert len(runner.calls) == 6  # all steps still attempted
    assert "step=fmp action=end rc=1" in out.getvalue()


def test_execute_includes_ticker_in_argv(tmp_path):
    runner = _MockRunner()
    plan = Plan(ticker="GOOG", mode="full", skip_fmp=False, skip_fmp_reason=None)
    execute(plan, project_root=tmp_path, out=io.StringIO(), runner=runner)
    for call in runner.calls:
        assert "GOOG" in call


def test_execute_includes_repo_root_where_needed(tmp_path):
    """extract_kpis, build_saydo, and build_artifacts need --repo-root."""
    runner = _MockRunner()
    plan = Plan(ticker="NU", mode="full", skip_fmp=False, skip_fmp_reason=None)
    execute(plan, project_root=tmp_path, out=io.StringIO(), runner=runner)
    needs_repo_root = {
        "extract_kpis_from_summaries",
        "build_saydo_pairs",
        "build_artifacts",
    }
    for call in runner.calls:
        script = Path(call[1]).stem
        if script in needs_repo_root:
            assert "--repo-root" in call
            assert str(tmp_path) in call


def test_build_step_uses_workspace_renderer_with_enable_llm(tmp_path):
    runner = _MockRunner()
    plan = Plan(ticker="NU", mode="full", skip_fmp=False, skip_fmp_reason=None)
    execute(plan, project_root=tmp_path, out=io.StringIO(), runner=runner)
    build_argv = next(c for c in runner.calls if "build_artifacts" in c[1])
    assert "--renderer" in build_argv
    assert "workspace" in build_argv
    assert "--enable-llm" in build_argv
