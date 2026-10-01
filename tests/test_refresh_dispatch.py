"""Tests for execution/refresh_dispatch.py.

Two layers:
- `build_plan` (pure) — given DB state + clock, decide what to skip.
- `execute` (impure) — with a mock runner, verify step ordering, the
  --skip routing, exit-code aggregation.
"""

from __future__ import annotations

import io
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

import pytest

from execution import refresh_dispatch as rd
from execution.refresh_dispatch import STEP_NAMES, Plan, build_plan, execute


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
    conn.executemany(
        "INSERT INTO fmp_endpoint_status "
        "(ticker, endpoint, period, status, record_count, last_pulled) "
        "VALUES (?, ?, ?, 'ok', 12, ?)",
        [
            (ticker.upper(), endpoint, period, last_pulled)
            for endpoint in ("income-statement", "balance-sheet-statement", "cashflow-statement")
            for period in ("annual", "quarter")
        ],
    )
    conn.commit()
    conn.close()


# ---- build_plan tests ----------------------------------------------------


def test_plan_full_mode_never_skips(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    _seed_fmp(db, "NU", "2026-05-11T01:02:14")
    plan = build_plan(ticker="NU", mode="full", db_path=db, now=datetime(2026, 5, 18, tzinfo=UTC))
    assert plan.mode == "full"
    assert plan.skip_fmp is False
    assert plan.skip_fmp_reason is None


def test_plan_stale_skips_fmp_when_pulled_within_window(tmp_path: Path) -> None:
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


def test_plan_stale_runs_fmp_when_outside_window(tmp_path: Path) -> None:
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


def test_plan_stale_runs_fmp_when_no_history(tmp_path: Path) -> None:
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


def test_plan_stale_runs_fmp_when_db_missing(tmp_path: Path) -> None:
    plan = build_plan(
        ticker="NU",
        mode="stale",
        db_path=tmp_path / "does_not_exist.db",
        now=datetime(2026, 5, 18, tzinfo=UTC),
    )
    assert plan.skip_fmp is False


def test_plan_stale_respects_custom_window(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    _seed_fmp(db, "NU", "2026-05-15T01:00:00")  # 3 days before now
    # Window = 2 days → 3-day-old data is OUTSIDE → run FMP
    plan = build_plan(
        ticker="NU",
        mode="stale",
        db_path=db,
        stale_fmp_days=2,
        now=datetime(2026, 5, 18, tzinfo=UTC),
    )
    assert plan.skip_fmp is False


@pytest.mark.parametrize(
    "endpoint", ["income-statement", "balance-sheet-statement", "cashflow-statement"]
)
@pytest.mark.parametrize("period", ["annual", "quarter"])
def test_plan_requires_every_statement_receipt(tmp_path: Path, endpoint: str, period: str) -> None:
    db = tmp_path / "p.db"
    _seed_fmp(db, "NU", "2026-05-15T01:00:00")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "DELETE FROM fmp_endpoint_status WHERE endpoint = ? AND period = ?", (endpoint, period)
        )
    assert not build_plan(
        ticker="NU", mode="stale", db_path=db, now=datetime(2026, 5, 18, tzinfo=UTC)
    ).skip_fmp


@pytest.mark.parametrize(
    ("status", "record_count", "last_pulled"),
    [
        ("error", 12, "2026-05-17T00:00:00"),
        ("empty", 0, "2026-05-17T00:00:00"),
        ("ok", 0, "2026-05-17T00:00:00"),
        ("ok", None, "2026-05-17T00:00:00"),
        ("ok", 12, "2026-05-01T00:00:00"),
        ("ok", 12, "2026-05-19T00:00:00"),
        ("ok", 12, "invalid"),
        ("ok", 12, None),
    ],
)
def test_recent_unrelated_pull_cannot_hide_unusable_statement(
    tmp_path: Path, status: str, record_count: int | None, last_pulled: str | None
) -> None:
    db = tmp_path / "p.db"
    _seed_fmp(db, "NU", "2026-05-15T01:00:00")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE fmp_endpoint_status SET status=?, record_count=?, last_pulled=? "
            "WHERE endpoint='cashflow-statement' AND period='annual'",
            (status, record_count, last_pulled),
        )
        conn.execute(
            "INSERT INTO fmp_endpoint_status (ticker, endpoint, period, status, record_count, last_pulled) "
            "VALUES ('NU', 'profile', '', 'ok', 1, '2026-05-17T23:00:00')"
        )
    assert not build_plan(
        ticker="NU", mode="stale", db_path=db, now=datetime(2026, 5, 18, tzinfo=UTC)
    ).skip_fmp


def test_freshness_reports_oldest_required_capture_with_timezone(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    _seed_fmp(db, "NU", "2026-05-15T01:00:00")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE fmp_endpoint_status SET last_pulled='2026-05-10T20:00:00-07:00' "
            "WHERE endpoint='income-statement' AND period='quarter'"
        )
    plan = build_plan(ticker="nu", mode="stale", db_path=db, now=datetime(2026, 5, 18, tzinfo=UTC))
    assert plan.skip_fmp
    assert plan.skip_fmp_reason == "fresh last_pulled=2026-05-11T03:00:00+00:00"


def test_missing_receipt_table_refreshes_without_exception(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    db.touch()
    assert not build_plan(ticker="NU", mode="stale", db_path=db).skip_fmp


@pytest.mark.parametrize("explicit", [False, True])
def test_cli_resolves_database_before_planning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    explicit: bool,
) -> None:
    database = tmp_path / "approved.db"
    _seed_fmp(database, "NU", datetime.now(UTC).isoformat())
    resolved: list[Path | str | None] = []

    def resolver(override: Path | str | None = None) -> Path:
        resolved.append(override)
        return database

    monkeypatch.setattr(rd, "require_db_path", resolver, raising=False)
    argv = ["refresh_dispatch.py", "--ticker", "NU", "--plan-only"]
    if explicit:
        argv += ["--db", str(database)]
    monkeypatch.setattr(sys, "argv", argv)
    assert rd.main() == 0
    assert resolved == [database if explicit else None]
    assert '"skip_fmp": true' in capsys.readouterr().out


def test_cli_rejects_unavailable_database_before_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolver(override: Path | str | None = None) -> Path:
        raise RuntimeError("configured database required")

    monkeypatch.setattr(rd, "require_db_path", resolver, raising=False)
    monkeypatch.setattr(sys, "argv", ["refresh_dispatch.py", "--ticker", "NU", "--plan-only"])
    with pytest.raises(RuntimeError, match="configured database required"):
        rd.main()


def test_cli_database_mismatch_refuses_before_any_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "approved.db"
    _seed_fmp(database, "NU", datetime.now(UTC).isoformat())
    executed: list[Plan] = []

    def executor(plan: Plan, *, project_root: Path, state_root: Path) -> int:
        del project_root, state_root
        executed.append(plan)
        return 0

    monkeypatch.setattr(rd, "execute", executor)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "refresh_dispatch.py",
            "--ticker",
            "NU",
            "--mode",
            "full",
            "--db",
            str(database),
            "--state-root",
            str(tmp_path / "different-state"),
        ],
    )
    assert rd.main() == 3
    assert executed == []
    receipt = json.loads(capsys.readouterr().out)
    assert receipt == {"status": "refused", "reason_code": "database_state_root_mismatch"}


def test_cli_database_redirect_to_same_authority_executes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority = tmp_path / "authority"
    authority.mkdir()
    database = authority / "portfolio.db"
    _seed_fmp(database, "NU", datetime.now(UTC).isoformat())
    state_root = tmp_path / "runtime"
    state_root.mkdir()
    try:
        (state_root / "data").symlink_to(authority, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlink creation is unavailable on this test host")
    executed: list[Path] = []

    def executor(plan: Plan, *, project_root: Path, state_root: Path) -> int:
        del plan, project_root
        executed.append(state_root)
        return 0

    monkeypatch.setattr(rd, "execute", executor)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "refresh_dispatch.py",
            "--ticker",
            "NU",
            "--mode",
            "full",
            "--db",
            str(database),
            "--state-root",
            str(state_root),
        ],
    )
    assert rd.main() == 0
    assert executed == [state_root.resolve()]


# ---- execute tests -------------------------------------------------------


class _Result:
    def __init__(self, returncode: int) -> None:
        self.returncode: int | None = returncode


class _MockRunner:
    """Replacement for subprocess.run that records argv lists + scripted exit codes."""

    def __init__(self, exit_codes: list[int] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._exit_codes = list(exit_codes or [])

    def __call__(self, argv: list[str], *, out: TextIO) -> _Result:
        self.calls.append(argv)
        rc = self._exit_codes.pop(0) if self._exit_codes else 0
        out.write(f"<mock step output for {Path(argv[2]).stem}>\n")
        return _Result(rc)


def test_execute_full_runs_all_six_steps(tmp_path: Path) -> None:
    runner = _MockRunner()
    plan = Plan(ticker="NU", mode="full", skip_fmp=False, skip_fmp_reason=None)
    out = io.StringIO()
    rc = execute(plan, project_root=tmp_path, out=out, runner=runner)
    assert rc == 0
    script_names = [Path(call[2]).stem for call in runner.calls]
    assert script_names == [
        "fetch_fmp_historical_data",
        "backfill_transcripts",
        "process_ir_documents_state",
        "extract_kpis_from_summaries",
        "build_saydo_pairs",
        "build_artifacts",
    ]


def test_execute_stale_skips_fmp_when_planned(tmp_path: Path) -> None:
    runner = _MockRunner()
    plan = Plan(
        ticker="NU",
        mode="stale",
        skip_fmp=True,
        skip_fmp_reason="fresh last_pulled=2026-05-15T01:00:00",
    )
    out = io.StringIO()
    rc = execute(plan, project_root=tmp_path, out=out, runner=runner)
    assert rc == 0
    script_names = [Path(call[2]).stem for call in runner.calls]
    assert "fetch_fmp_historical_data" not in script_names
    assert script_names == [
        "backfill_transcripts",
        "process_ir_documents_state",
        "extract_kpis_from_summaries",
        "build_saydo_pairs",
        "build_artifacts",
    ]
    assert "step=fmp action=skip reason=fresh" in out.getvalue()


def test_execute_emits_dispatch_markers(tmp_path: Path) -> None:
    runner = _MockRunner()
    plan = Plan(ticker="NU", mode="full", skip_fmp=False, skip_fmp_reason=None)
    out = io.StringIO()
    execute(plan, project_root=tmp_path, out=out, runner=runner)
    log = out.getvalue()
    assert "[dispatch] ticker=NU mode=full" in log
    assert "[dispatch] step=fmp action=start" in log
    assert "[dispatch] step=fmp action=end rc=0" in log
    assert "[dispatch] all_done rc=0" in log


def test_execute_keeps_going_after_step_failure(tmp_path: Path) -> None:
    """A failed FMP step must not block independent subsequent steps."""
    runner = _MockRunner(exit_codes=[1, 0, 0, 0, 0, 0])  # FMP fails, rest succeed
    plan = Plan(ticker="NU", mode="full", skip_fmp=False, skip_fmp_reason=None)
    out = io.StringIO()
    rc = execute(plan, project_root=tmp_path, out=out, runner=runner)
    assert rc == 1  # remembered the failure
    assert len(runner.calls) == 6  # all steps still attempted
    assert "step=fmp action=end rc=1" in out.getvalue()


def test_execute_includes_ticker_in_argv(tmp_path: Path) -> None:
    runner = _MockRunner()
    plan = Plan(ticker="GOOG", mode="full", skip_fmp=False, skip_fmp_reason=None)
    execute(plan, project_root=tmp_path, out=io.StringIO(), runner=runner)
    for call in runner.calls:
        assert "GOOG" in call


def test_execute_includes_repo_root_where_needed(tmp_path: Path) -> None:
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
        script = Path(call[2]).stem
        if script in needs_repo_root:
            assert "--repo-root" in call
            assert str(tmp_path) in call


def test_execute_uses_code_root_for_scripts_and_state_root_for_outputs(tmp_path: Path) -> None:
    code_root = tmp_path / "runtime"
    state_root = tmp_path / "state"
    runner = _MockRunner()
    plan = Plan(
        ticker="NU",
        mode="full",
        skip_fmp=False,
        skip_fmp_reason=None,
        steps=STEP_NAMES,
    )

    execute(
        plan,
        project_root=code_root,
        state_root=state_root,
        out=io.StringIO(),
        runner=runner,
    )

    for call in runner.calls:
        assert Path(call[2]).is_relative_to(code_root)
    for script_name in (
        "fetch_fmp_historical_data",
        "backfill_transcripts",
        "process_ir_documents_state",
        "extract_kpis_from_summaries",
        "build_saydo_pairs",
        "refresh_dcf",
        "build_artifacts",
    ):
        call = next(item for item in runner.calls if script_name in item[2])
        assert call[call.index("--repo-root") + 1] == str(state_root)
    news = next(item for item in runner.calls if "fetch_news" in item[2])
    assert news[news.index("--db-path") + 1] == str(state_root / "data/portfolio.db")
    thesis = next(item for item in runner.calls if "run_thesis_evaluator" in item[2])
    assert thesis[thesis.index("--db") + 1] == str(state_root / "data/portfolio.db")
    assert thesis[thesis.index("--holdings-dir") + 1] == str(state_root / "micro_thesis/holdings")


def test_build_step_uses_workspace_renderer_with_enable_llm(tmp_path: Path) -> None:
    runner = _MockRunner()
    plan = Plan(ticker="NU", mode="full", skip_fmp=False, skip_fmp_reason=None)
    execute(plan, project_root=tmp_path, out=io.StringIO(), runner=runner)
    build_argv = next(c for c in runner.calls if "build_artifacts" in c[2])
    assert "--renderer" in build_argv
    assert "workspace" in build_argv
    assert "--enable-llm" in build_argv
