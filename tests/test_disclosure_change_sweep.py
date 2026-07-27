"""Tests for the resumable disclosure-change operation.

The sweep is deliberately orchestration-only: every detector remains a
single-purpose Layer-3 script.  These tests replace ``subprocess.run`` so no
detector, network request, LLM call, or production write can occur.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from execution import ingest_filing_sections
from execution import run_disclosure_change_sweep as sweep


def test_task_definition_is_scheduler_compatible_and_least_privilege() -> None:
    task_path = Path(__file__).resolve().parents[1] / "cron" / "disclosure_change_sweep.task.xml"
    raw = task_path.read_bytes()

    assert raw.startswith((b"\xff\xfe", b"\xfe\xff"))
    text = raw.decode("utf-16")
    assert '<?xml version="1.0" encoding="UTF-16"?>' in text
    assert "<LogonType>InteractiveToken</LogonType>" in text
    assert "<RunLevel>LeastPrivilege</RunLevel>" in text


class _Result:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode


class _RecordingRunner:
    def __init__(self, returncodes: dict[str, int] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.returncodes = returncodes or {}

    def __call__(self, argv: list[str], **_: object) -> _Result:
        self.calls.append(list(argv))
        return _Result(self.returncodes.get(Path(argv[1]).name, 0))


def _seed_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE tracked_companies (
            ticker TEXT PRIMARY KEY,
            list_type TEXT,
            instrument_type TEXT
        );
        CREATE TABLE filing_sections (
            ticker TEXT NOT NULL,
            accession_number TEXT
        );
        INSERT INTO tracked_companies VALUES ('NU', 'portfolio', 'stock');
        INSERT INTO tracked_companies VALUES ('WIX', 'watchlist', 'stock');
        INSERT INTO tracked_companies VALUES ('SPY', 'portfolio', 'etf');
        INSERT INTO filing_sections VALUES ('NU', '0001');
        INSERT INTO filing_sections VALUES ('WIX', '0002');
        """
    )
    conn.commit()
    conn.close()


def test_weekly_sweep_runs_only_tickers_with_unseen_accessions(tmp_path: Path) -> None:
    db_path = tmp_path / "portfolio.db"
    state_path = tmp_path / "state.json"
    _seed_db(db_path)
    state_path.write_text(
        json.dumps({"processed_accessions": {"NU": ["0001"]}}),
        encoding="utf-8",
    )
    runner = _RecordingRunner()

    result = sweep.run(
        db_path=db_path,
        state_path=state_path,
        project_root=tmp_path,
        runner=runner,
    )

    assert result.tickers == ("WIX",)
    assert len(runner.calls) == len(sweep.STEPS)
    assert all(call[call.index("--tickers") + 1] == "WIX" for call in runner.calls)
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["processed_accessions"] == {"NU": ["0001"], "WIX": ["0002"]}


def test_successful_ticker_runs_detectors_in_dependency_order(tmp_path: Path) -> None:
    db_path = tmp_path / "portfolio.db"
    _seed_db(db_path)
    runner = _RecordingRunner()

    result = sweep.run(
        db_path=db_path,
        state_path=tmp_path / "state.json",
        project_root=tmp_path,
        tickers=("NU",),
        runner=runner,
    )

    assert result.failed_tickers == ()
    assert [Path(call[1]).name for call in runner.calls] == [
        "detect_disclosure_changes.py",
        "detect_discontinued_metrics.py",
        "detect_guidance_lifecycle.py",
        "detect_section_similarity.py",
        "detect_transcript_disclosure_events.py",
        "detrend_disclosure_events.py",
        "classify_disclosure_specificity.py",
    ]
    assert all("--db-path" in call for call in runner.calls)


def test_failed_step_defers_ticker_and_does_not_advance_checkpoint(tmp_path: Path) -> None:
    db_path = tmp_path / "portfolio.db"
    state_path = tmp_path / "state.json"
    _seed_db(db_path)
    runner = _RecordingRunner({"detect_guidance_lifecycle.py": 1})

    result = sweep.run(
        db_path=db_path,
        state_path=state_path,
        project_root=tmp_path,
        tickers=("NU",),
        runner=runner,
    )

    assert result.failed_tickers == ("NU",)
    assert result.failures == 1
    assert [Path(call[1]).name for call in runner.calls] == [
        "detect_disclosure_changes.py",
        "detect_discontinued_metrics.py",
        "detect_guidance_lifecycle.py",
    ]
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["processed_accessions"] == {}


def test_no_new_accessions_is_an_idempotent_noop(tmp_path: Path) -> None:
    db_path = tmp_path / "portfolio.db"
    state_path = tmp_path / "state.json"
    _seed_db(db_path)
    state_path.write_text(
        json.dumps({"processed_accessions": {"NU": ["0001"], "WIX": ["0002"]}}),
        encoding="utf-8",
    )
    runner = _RecordingRunner()

    result = sweep.run(
        db_path=db_path,
        state_path=state_path,
        project_root=tmp_path,
        runner=runner,
    )

    assert result.tickers == ()
    assert runner.calls == []


def test_explicit_fast_path_runs_even_when_accession_was_seen(tmp_path: Path) -> None:
    db_path = tmp_path / "portfolio.db"
    state_path = tmp_path / "state.json"
    _seed_db(db_path)
    state_path.write_text(
        json.dumps({"processed_accessions": {"NU": ["0001"]}}),
        encoding="utf-8",
    )
    runner = _RecordingRunner()

    result = sweep.run(
        db_path=db_path,
        state_path=state_path,
        project_root=tmp_path,
        tickers=("NU",),
        runner=runner,
    )

    assert result.tickers == ("NU",)
    assert len(runner.calls) == len(sweep.STEPS)


def test_operator_bootstrap_checkpoints_verified_current_corpus_without_running(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "portfolio.db"
    state_path = tmp_path / "state.json"
    _seed_db(db_path)
    runner = _RecordingRunner()

    result = sweep.run(
        db_path=db_path,
        state_path=state_path,
        project_root=tmp_path,
        bootstrap_current=True,
        runner=runner,
    )

    assert result.completed_tickers == ("NU", "WIX")
    assert result.steps_completed == 0
    assert runner.calls == []
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["processed_accessions"] == {"NU": ["0001"], "WIX": ["0002"]}


def test_ingest_fast_path_defers_inside_protected_llm_window(tmp_path: Path) -> None:
    runner = _RecordingRunner()
    now = datetime(2026, 7, 27, 4, 15, tzinfo=ZoneInfo("America/Los_Angeles"))

    outcome = ingest_filing_sections._trigger_disclosure_fast_path(
        tickers=("NU",),
        db_path=tmp_path / "portfolio.db",
        project_root=tmp_path,
        now=now,
        runner=runner,
    )

    assert outcome == "deferred_protected_window"
    assert runner.calls == []


def test_ingest_fast_path_invokes_same_sweep_outside_protected_window(tmp_path: Path) -> None:
    runner = _RecordingRunner()
    now = datetime(2026, 7, 27, 12, 15, tzinfo=ZoneInfo("America/Los_Angeles"))

    outcome = ingest_filing_sections._trigger_disclosure_fast_path(
        tickers=("NU", "WIX"),
        db_path=tmp_path / "portfolio.db",
        project_root=tmp_path,
        now=now,
        runner=runner,
    )

    assert outcome == "complete"
    assert len(runner.calls) == 1
    assert Path(runner.calls[0][1]).name == "run_disclosure_change_sweep.py"
    assert runner.calls[0][runner.calls[0].index("--tickers") + 1] == "NU,WIX"
