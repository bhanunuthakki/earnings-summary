# pyright: reportPrivateUsage=false
"""Per-ticker "run anyway" budget override (PR4). The `force_budget_bypass` flag
threads dashboard → POST /actions/refresh → refresh_dispatch → build_artifacts →
build_report → the section budget gates, so a skip-mode cap is ignored for the run.

Covers each hop: refresh_dispatch argv/plan, the /actions/refresh server
threading, and the section-level effect (bear_case runs despite a skip cap).
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from flask.testing import FlaskClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import comments_server  # noqa: E402
import refresh_dispatch  # noqa: E402

from dispatch_registry import Job, Registry  # noqa: E402
from report.models import (  # noqa: E402
    EarningsSection,
    FinancialsSection,
    SectionStatus,
    SegmentsSection,
    ThesisSection,
)
from report.sections import bear_case  # noqa: E402

_VALID_BEAR = (
    '{"failure_modes": [{"hypothesis": "h", "evidence_in_data": "e", '
    '"leading_indicator": "l", "quantitative_impact": "q", "refutation_criteria": "r"}], '
    '"most_underweighted": "m", "out_of_scope_flags": []}'
)


def _valid_bear(*args: object, **kwargs: object) -> str:
    del args, kwargs
    return _VALID_BEAR


# ---------------------------------------------------------------------------
# refresh_dispatch: argv + plan carry the flag
# ---------------------------------------------------------------------------


def test_argv_build_appends_flag_when_bypass() -> None:
    argv = refresh_dispatch._argv_build(
        PROJECT_ROOT,
        PROJECT_ROOT,
        "NU",
        force_budget_bypass=True,
    )
    assert "--force-budget-bypass" in argv


def test_argv_build_omits_flag_by_default() -> None:
    assert "--force-budget-bypass" not in refresh_dispatch._argv_build(
        PROJECT_ROOT,
        PROJECT_ROOT,
        "NU",
    )


def test_build_plan_carries_bypass(tmp_path: Path) -> None:
    plan = refresh_dispatch.build_plan(
        ticker="NU", mode="full", db_path=tmp_path / "x.db", force_budget_bypass=True
    )
    assert plan.force_budget_bypass is True


# ---------------------------------------------------------------------------
# /actions/refresh threads the flag into the dispatch argv
# ---------------------------------------------------------------------------


class _CapturingRegistry(Registry):
    """Records the argv of the last start() without spawning a subprocess."""

    def __init__(self) -> None:
        super().__init__()
        self.last_argv: list[str] = []

    def start(
        self,
        *,
        ticker: str,
        kind: str,
        argv: list[str],
        spawn: bool = True,
        cwd: str | None = None,
        write_sets: list[str] | None = None,
        code_root: str | Path | None = None,
    ) -> Job:
        self.last_argv = argv
        return super().start(
            ticker=ticker,
            kind=kind,
            argv=argv,
            spawn=False,
            cwd=cwd,
            write_sets=write_sets,
            code_root=code_root,
        )


def _server(tmp_path: Path) -> tuple[FlaskClient, _CapturingRegistry]:
    (tmp_path / "data").mkdir()
    sqlite3.connect(str(tmp_path / "data" / "portfolio.db")).close()
    reg = _CapturingRegistry()
    return comments_server.create_app(tmp_path, registry=reg).test_client(), reg


def test_refresh_threads_bypass_flag(tmp_path: Path) -> None:
    client, reg = _server(tmp_path)
    resp = client.post("/actions/refresh", json={"ticker": "NU", "force_budget_bypass": True})
    assert resp.status_code == 201
    assert "--force-budget-bypass" in reg.last_argv


def test_refresh_without_bypass_omits_flag(tmp_path: Path) -> None:
    client, reg = _server(tmp_path)
    resp = client.post("/actions/refresh", json={"ticker": "NU"})
    assert resp.status_code == 201
    assert "--force-budget-bypass" not in reg.last_argv


# ---------------------------------------------------------------------------
# Section-level effect: a skip-mode cap is ignored when bypassed
# ---------------------------------------------------------------------------


def _seed_skip(repo_root: Path, purpose: str) -> None:
    d = repo_root / "data"
    d.mkdir(exist_ok=True)
    now = datetime.now(UTC).isoformat()
    conn = sqlite3.connect(str(d / "portfolio.db"))
    try:
        conn.executescript(
            """
            CREATE TABLE llm_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT, called_at DATETIME NOT NULL,
                purpose VARCHAR(64), model VARCHAR(64) NOT NULL, prompt_sha256 VARCHAR(64) NOT NULL,
                prompt_chars INTEGER NOT NULL, elapsed_ms INTEGER NOT NULL, cost_estimate_usd FLOAT);
            CREATE TABLE llm_budgets (
                id INTEGER PRIMARY KEY AUTOINCREMENT, purpose VARCHAR(64) NOT NULL,
                monthly_cap_usd NUMERIC(10,2) NOT NULL, warn_threshold_pct FLOAT NOT NULL DEFAULT 0.80,
                hard_block BOOLEAN NOT NULL DEFAULT 0,
                on_exceed TEXT NOT NULL DEFAULT 'warn' CHECK (on_exceed IN ('skip', 'block', 'warn')),
                created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, notes TEXT,
                CONSTRAINT uq_llm_budgets_purpose UNIQUE (purpose));
            """
        )
        conn.execute(
            "INSERT INTO llm_budgets (purpose, monthly_cap_usd, hard_block, on_exceed, "
            "created_at, updated_at) VALUES (?, 10, 0, 'skip', ?, ?)",
            (purpose, now, now),
        )
        conn.execute(
            "INSERT INTO llm_calls (called_at, purpose, model, prompt_sha256, prompt_chars, "
            "elapsed_ms, cost_estimate_usd) VALUES (?, ?, 'm', 'x', 1, 1, 20.0)",
            (now, purpose),
        )
        conn.commit()
    finally:
        conn.close()


def _thesis() -> ThesisSection:
    return ThesisSection(
        status=SectionStatus.OK, thesis_full="NU is a bank.", break_conditions=["x"]
    )


def _build_bear(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, bypass: bool):
    _seed_skip(tmp_path, "bear_case")
    monkeypatch.setattr("report.sections.bear_case.generate_bear_case", _valid_bear)
    return bear_case.build(
        ticker="NU",
        repo_root=tmp_path,
        enable_llm=True,
        thesis=_thesis(),
        financials=FinancialsSection(status=SectionStatus.MISSING_DATA),
        segments=SegmentsSection(status=SectionStatus.MISSING_DATA),
        earnings=EarningsSection(status=SectionStatus.MISSING_DATA),
        force_refresh=True,
        force_budget_bypass=bypass,
    )


def test_bear_case_bypass_runs_despite_skip_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    section = _build_bear(tmp_path, monkeypatch, bypass=True)
    assert section.status == SectionStatus.OK  # ran the LLM, not forgone
    assert section.budget_skip is None
    assert len(section.failure_modes) == 1


def test_bear_case_skips_without_bypass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    section = _build_bear(tmp_path, monkeypatch, bypass=False)
    assert section.status == SectionStatus.BUDGET_SKIPPED
