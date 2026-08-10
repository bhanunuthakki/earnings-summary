"""Tests for execution/run_model_eval_sweep.py.

Mocks LLM calls so the test suite doesn't need a live Claude binary. Verifies:
- capture-file loading and deduplication
- active-purpose discovery (with and without a DB)
- verdicts accumulate correctly via the canonical writer (record_verdict)
- no-persist mode writes nothing
- cron files exist and are syntactically valid
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_sweep() -> Any:
    """Import run_model_eval_sweep as a module without executing main().

    Uses the same pattern as other execution-script tests (see
    test_backfill_earnings_surprises.py): execution/ scripts aren't on the
    package path, so we load by file path. Returns Any so callers aren't
    infected by Unknown types from the dynamic load.
    """
    src = PROJECT_ROOT / "execution" / "run_model_eval_sweep.py"
    spec = importlib.util.spec_from_file_location("run_model_eval_sweep", src)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys_src = str(PROJECT_ROOT / "src")
    if sys_src not in sys.path:
        sys.path.insert(0, sys_src)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sweep() -> Any:
    return _load_sweep()


@pytest.fixture()
def capture_dir(tmp_path: Path) -> Path:
    cdir = tmp_path / "llm_capture"
    cdir.mkdir()
    return cdir


def _write_capture(capture_dir: Path, records: list[dict[str, object]]) -> Path:
    """Write a capture JSONL file and return its path."""
    path = capture_dir / "capture_2026-06-10.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    return path


def _make_record(
    purpose: str,
    ticker: str = "META",
    prompt: str = "What is the bear case?",
    response: str = "The bear case is ...",
    sha: str | None = None,
) -> dict[str, object]:
    return {
        "purpose": purpose,
        "ticker": ticker,
        "prompt": prompt,
        "response": response,
        "prompt_sha256": sha or uuid.uuid4().hex,
        "model": "claude-sonnet-4-6",
        "backend": "claude",
    }


@pytest.fixture()
def minimal_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE llm_calls (
            id INTEGER PRIMARY KEY,
            called_at TEXT NOT NULL,
            purpose TEXT,
            ticker TEXT,
            scope TEXT,
            model TEXT NOT NULL DEFAULT 'claude-sonnet-4-6',
            prompt_sha256 TEXT NOT NULL DEFAULT '',
            prompt_chars INTEGER NOT NULL DEFAULT 0,
            elapsed_ms INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    # Mirrors the reconciled 0084 migration (#448) — the one canonical shape
    # record_verdict writes and apply_model_switches reads.
    conn.execute(
        """
        CREATE TABLE model_eval_verdicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            purpose TEXT NOT NULL,
            candidate TEXT NOT NULL,
            incumbent TEXT NOT NULL,
            verdict TEXT NOT NULL,
            run_id TEXT NOT NULL,
            parity_rate REAL,
            judge_agreement REAL,
            n_cases INTEGER,
            n_parity INTEGER,
            summary_json TEXT,
            recorded_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# _find_capture_files
# ---------------------------------------------------------------------------


def test_find_capture_files_empty(sweep: Any, tmp_path: Path) -> None:
    assert sweep._find_capture_files(tmp_path) == []


def test_find_capture_files_sorted(sweep: Any, capture_dir: Path) -> None:
    (capture_dir / "capture_2026-06-08.jsonl").write_text("", encoding="utf-8")
    (capture_dir / "capture_2026-06-10.jsonl").write_text("", encoding="utf-8")
    (capture_dir / "capture_2026-06-09.jsonl").write_text("", encoding="utf-8")
    files: list[Path] = list(sweep._find_capture_files(capture_dir))
    names = [f.name for f in files]
    assert names == [
        "capture_2026-06-08.jsonl",
        "capture_2026-06-09.jsonl",
        "capture_2026-06-10.jsonl",
    ]


# ---------------------------------------------------------------------------
# _load_cases_from_files
# ---------------------------------------------------------------------------


def test_load_cases_basic(sweep: Any, capture_dir: Path) -> None:
    rec = _make_record("bear_case", sha="abc123")
    _write_capture(capture_dir, [rec])
    files: list[Path] = list(sweep._find_capture_files(capture_dir))
    cases: list[Any] = list(sweep._load_cases_from_files(files, purpose="bear_case", limit=10))
    assert len(cases) == 1
    assert cases[0].ticker == "META"
    assert cases[0].incumbent_response == "The bear case is ..."


def test_load_cases_deduplicates_by_sha(sweep: Any, capture_dir: Path) -> None:
    sha = "dup_sha"
    records = [
        _make_record("bear_case", ticker="META", sha=sha),
        _make_record("bear_case", ticker="GOOG", sha=sha),  # same sha, different ticker
    ]
    _write_capture(capture_dir, records)
    files: list[Path] = list(sweep._find_capture_files(capture_dir))
    cases: list[Any] = list(sweep._load_cases_from_files(files, purpose="bear_case", limit=10))
    assert len(cases) == 1  # deduped on sha


def test_load_cases_filters_by_purpose(sweep: Any, capture_dir: Path) -> None:
    records = [
        _make_record("bear_case"),
        _make_record("transcript_summary"),
    ]
    _write_capture(capture_dir, records)
    files: list[Path] = list(sweep._find_capture_files(capture_dir))
    cases: list[Any] = list(sweep._load_cases_from_files(files, purpose="bear_case", limit=10))
    assert len(cases) == 1
    assert cases[0].label.startswith("bear_case:")


def test_load_cases_respects_limit(sweep: Any, capture_dir: Path) -> None:
    records = [_make_record("bear_case", sha=str(i)) for i in range(10)]
    _write_capture(capture_dir, records)
    files: list[Path] = list(sweep._find_capture_files(capture_dir))
    cases: list[Any] = list(sweep._load_cases_from_files(files, purpose="bear_case", limit=3))
    assert len(cases) == 3


def test_load_cases_skips_empty_response(sweep: Any, capture_dir: Path) -> None:
    records = [
        _make_record("bear_case", response="", sha="bad"),
        _make_record("bear_case", sha="good"),
    ]
    _write_capture(capture_dir, records)
    files: list[Path] = list(sweep._find_capture_files(capture_dir))
    cases: list[Any] = list(sweep._load_cases_from_files(files, purpose="bear_case", limit=10))
    assert len(cases) == 1
    assert cases[0].incumbent_response != ""


# ---------------------------------------------------------------------------
# _discover_active_purposes
# ---------------------------------------------------------------------------


def test_discover_active_purposes_from_db(sweep: Any, minimal_db: Path) -> None:
    conn = sqlite3.connect(str(minimal_db))
    for purpose in ("bear_case", "transcript_summary"):
        conn.execute(
            "INSERT INTO llm_calls (called_at, purpose, scope, model, prompt_sha256, prompt_chars, elapsed_ms) "
            "VALUES (datetime('now', '-1 day'), ?, NULL, 'claude-sonnet-4-6', 'x', 100, 500)",
            (purpose,),
        )
    conn.commit()
    conn.close()

    purposes: list[str] = list(
        sweep._discover_active_purposes(minimal_db, lookback_days=7, explicit_purposes=None)
    )
    assert set(purposes) >= {"bear_case", "transcript_summary"}


def test_discover_active_purposes_skips_model_eval_scope(sweep: Any, minimal_db: Path) -> None:
    conn = sqlite3.connect(str(minimal_db))
    conn.execute(
        "INSERT INTO llm_calls (called_at, purpose, scope, model, prompt_sha256, prompt_chars, elapsed_ms) "
        "VALUES (datetime('now', '-1 day'), 'bear_case', 'model_eval', 'claude-haiku-4-5-20251001', 'x', 100, 500)"
    )
    conn.commit()
    conn.close()

    purposes: list[str] = list(
        sweep._discover_active_purposes(minimal_db, lookback_days=7, explicit_purposes=None)
    )
    assert "bear_case" not in purposes


def test_discover_active_purposes_explicit_override(sweep: Any, tmp_path: Path) -> None:
    nonexistent_db = tmp_path / "missing.db"
    purposes: list[str] = list(
        sweep._discover_active_purposes(
            nonexistent_db, lookback_days=7, explicit_purposes=["bear_case"]
        )
    )
    assert purposes == ["bear_case"]


def test_discover_active_purposes_filters_to_explicit(sweep: Any, minimal_db: Path) -> None:
    conn = sqlite3.connect(str(minimal_db))
    for purpose in ("bear_case", "transcript_summary", "press_release_summary"):
        conn.execute(
            "INSERT INTO llm_calls (called_at, purpose, scope, model, prompt_sha256, prompt_chars, elapsed_ms) "
            "VALUES (datetime('now', '-1 day'), ?, NULL, 'claude-sonnet-4-6', 'x', 100, 500)",
            (purpose,),
        )
    conn.commit()
    conn.close()

    purposes: list[str] = list(
        sweep._discover_active_purposes(
            minimal_db,
            lookback_days=7,
            explicit_purposes=["bear_case"],
        )
    )
    assert "bear_case" in purposes
    assert "transcript_summary" not in purposes


# ---------------------------------------------------------------------------
# Verdict persistence (llm.model_overrides.record_verdict — the one writer)
# ---------------------------------------------------------------------------


def test_insert_verdict_accumulates(minimal_db: Path) -> None:
    """Verdicts accumulate through the ONE canonical writer
    (llm.model_overrides.record_verdict — #448 removed the sweep's divergent
    _insert_verdict): rolling INSERTs, never an upsert."""
    from llm.model_overrides import record_verdict

    for _ in range(2):
        record_verdict(
            purpose="bear_case",
            candidate="claude-haiku-4-5-20251001",
            incumbent="claude-sonnet-4-6",
            verdict="KEEP_INCUMBENT",
            run_id="run0001",
            parity_rate=0.25,
            judge_agreement=1.0,
            n_cases=4,
            n_parity=1,
            db_path=minimal_db,
        )

    conn = sqlite3.connect(str(minimal_db))
    rows = conn.execute("SELECT * FROM model_eval_verdicts").fetchall()
    conn.close()
    assert len(rows) == 2  # rolling INSERT, not upsert


def test_insert_verdict_fields(minimal_db: Path) -> None:
    """record_verdict persists every CandidateVerdict field (asserted by
    column NAME — the positional asserts broke once already in the #441/#443
    schema fork)."""
    from llm.model_eval import SWITCH_DOWN
    from llm.model_overrides import record_verdict

    record_verdict(
        purpose="transcript_summary",
        candidate="claude-haiku-4-5-20251001",
        incumbent="claude-sonnet-4-6",
        verdict=SWITCH_DOWN,
        run_id="run0042",
        parity_rate=5 / 6,
        judge_agreement=0.8,
        n_cases=6,
        n_parity=5,
        summary_json=json.dumps({"recommendation": SWITCH_DOWN, "reason": "haiku holds parity"}),
        db_path=minimal_db,
    )

    conn = sqlite3.connect(str(minimal_db))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM model_eval_verdicts").fetchone()
    conn.close()

    assert row["purpose"] == "transcript_summary"
    assert row["incumbent"] == "claude-sonnet-4-6"
    assert row["candidate"] == "claude-haiku-4-5-20251001"
    assert row["verdict"] == SWITCH_DOWN
    assert row["run_id"] == "run0042"
    assert abs(row["judge_agreement"] - 0.8) < 0.001
    assert row["n_cases"] == 6
    assert row["n_parity"] == 5
    assert row["recorded_at"]  # stamped now-UTC by the writer
    summary = json.loads(row["summary_json"])
    assert summary["recommendation"] == SWITCH_DOWN


# ---------------------------------------------------------------------------
# run_sweep — no-persist mode
# ---------------------------------------------------------------------------


def test_run_sweep_no_persist_writes_nothing(
    sweep: Any, capture_dir: Path, minimal_db: Path
) -> None:
    """no-persist mode: evaluations run but DB stays empty."""
    conn = sqlite3.connect(str(minimal_db))
    conn.execute(
        "INSERT INTO llm_calls (called_at, purpose, scope, model, prompt_sha256, prompt_chars, elapsed_ms) "
        "VALUES (datetime('now', '-1 day'), 'bear_case', NULL, 'claude-sonnet-4-6', 'x', 100, 500)"
    )
    conn.commit()
    conn.close()

    records = [_make_record("bear_case", sha=str(i)) for i in range(4)]
    _write_capture(capture_dir, records)

    from llm.backend_judge import JudgedPair

    fake_run = MagicMock()
    fake_run.return_value = MagicMock(ok=True, response="cheap candidate output", error=None)

    fake_judge = MagicMock()
    fake_judge.return_value = JudgedPair(
        purpose="bear_case",
        label="bear_case:META:abc",
        ticker="META",
        judge_backend="claude",
        judge_model="claude-opus-4-8",
        winner="claude",  # incumbent wins every case -> KEEP_INCUMBENT
        margin=0.8,
        facet_winners={},
        position_consistent=True,
        rationales=["incumbent better"],
    )

    with (
        patch.object(sweep, "run_model", fake_run),
        patch.object(sweep, "judge_case", fake_judge),
    ):
        verdicts: list[Any] = list(
            sweep.run_sweep(
                capture_dir=capture_dir,
                db_path=minimal_db,
                purposes=["bear_case"],
                judges=["claude"],
                limit=4,
                lookback_days=7,
                min_n=4,
                parity_threshold=0.8,
                timeout_seconds=None,
                persist=False,
            )
        )

    # Verdicts may be empty if haiku already cheapest — no assertion on count.
    _ = verdicts
    conn2 = sqlite3.connect(str(minimal_db))
    count = conn2.execute("SELECT COUNT(*) FROM model_eval_verdicts").fetchone()[0]
    conn2.close()
    assert count == 0


def test_run_sweep_routes_overlay_candidate_by_recorded_family(
    sweep: Any, capture_dir: Path, minimal_db: Path
) -> None:
    dynamic_model = "gemini-2.5-flash-lite"
    conn = sqlite3.connect(str(minimal_db))
    conn.execute(
        """
        CREATE TABLE candidate_models (
            model_id TEXT PRIMARY KEY,
            family TEXT NOT NULL,
            input_usd_per_mtok REAL NOT NULL,
            output_usd_per_mtok REAL NOT NULL,
            promise REAL NOT NULL DEFAULT 0.5,
            status TEXT NOT NULL DEFAULT 'active'
        )
        """
    )
    conn.execute(
        "INSERT INTO candidate_models VALUES (?, 'gemini', 0.1, 0.4, 0.5, 'active')",
        (dynamic_model,),
    )
    conn.commit()
    conn.close()
    _write_capture(capture_dir, [_make_record("bear_case", sha="overlay-case")])

    seen: dict[str, object] = {}

    def fake_evaluate(cases: list[Any], **kwargs: object) -> tuple[Any, list[object]]:
        seen.update(kwargs)
        return (
            sweep.CandidateVerdict(
                purpose="bear_case",
                incumbent="claude-sonnet-4-6",
                candidate=dynamic_model,
                n=len(cases),
                candidate_wins=0,
                incumbent_wins=len(cases),
                ties=0,
                parity_rate=0.0,
                judge_agreement=1.0,
                recommendation="KEEP_INCUMBENT",
                reason="test",
            ),
            [],
        )

    with (
        patch.object(
            sweep, "_work_items", return_value=[("bear_case", [dynamic_model], None, None)]
        ),
        patch.object(sweep, "_evaluate_candidate", side_effect=fake_evaluate),
    ):
        sweep.run_sweep(
            capture_dir=capture_dir,
            db_path=minimal_db,
            purposes=["bear_case"],
            judges=["claude"],
            limit=1,
            lookback_days=7,
            min_n=1,
            parity_threshold=0.8,
            timeout_seconds=None,
            persist=False,
        )

    assert seen["candidate_backend"] == "gemini"


def test_run_sweep_no_capture_files_returns_empty(
    sweep: Any, tmp_path: Path, minimal_db: Path
) -> None:
    empty_capture = tmp_path / "empty_capture"
    empty_capture.mkdir()
    verdicts: list[Any] = list(
        sweep.run_sweep(
            capture_dir=empty_capture,
            db_path=minimal_db,
            purposes=None,
            judges=["claude"],
            limit=6,
            lookback_days=7,
            min_n=4,
            parity_threshold=0.8,
            timeout_seconds=None,
            persist=False,
        )
    )
    assert verdicts == []


def test_main_writes_receipt_and_alerts_when_nothing_is_graded(
    sweep: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path
    (repo_root / "data").mkdir()
    verdict = sweep.CandidateVerdict(
        purpose="bear_case",
        incumbent="claude-sonnet-4-6",
        candidate="claude-haiku-4-5-20251001",
        n=0,
        candidate_wins=0,
        incumbent_wins=0,
        ties=0,
        parity_rate=0.0,
        judge_agreement=0.0,
        recommendation=sweep.INSUFFICIENT_FRAME,
        reason="thin capture frame",
    )

    def fake_run_sweep(**_kwargs: object) -> list[Any]:
        return [verdict]

    monkeypatch.setattr(sweep, "run_sweep", fake_run_sweep)
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_model_eval_sweep.py", "--repo-root", str(repo_root)],
    )

    assert sweep.main() == 2
    receipt = json.loads(
        (repo_root / "data" / "model_eval_runs" / "latest.json").read_text(encoding="utf-8")
    )
    assert receipt["attempted"] == 1
    assert receipt["graded"] == 0
    assert receipt["insufficient"] == 1
    assert receipt["errors"] == 0
    assert receipt["status"] == "alert"


# ---------------------------------------------------------------------------
# Cron file existence checks
# ---------------------------------------------------------------------------


def test_cron_bat_exists() -> None:
    # The model_eval_sweep task runs run_weekly_model_eval.bat (the auto-switch
    # orchestrator); the older run_model_eval_sweep.bat was an unused orphan and
    # was removed. run_model_eval_sweep.py remains as a manual entry point.
    bat = PROJECT_ROOT / "cron" / "run_weekly_model_eval.bat"
    assert bat.exists(), f"missing cron bat: {bat}"


def test_cron_bat_continues_arguments_before_capturing_exit_code() -> None:
    bat = (PROJECT_ROOT / "cron" / "run_weekly_model_eval.bat").read_text(encoding="utf-8")
    call_at = bat.index("execution\\run_weekly_model_eval.py ^")
    repo_arg_at = bat.index('--repo-root "%PROJECT_ROOT%"', call_at)
    rc_at = bat.index('set "RC=%ERRORLEVEL%"', call_at)
    assert call_at < repo_arg_at < rc_at
    assert "done (exit %RC%)" in bat


def test_cron_task_xml_exists() -> None:
    xml = PROJECT_ROOT / "cron" / "model_eval_sweep.task.xml"
    assert xml.exists(), f"missing task XML: {xml}"


def test_cron_task_xml_is_valid_xml() -> None:
    import xml.etree.ElementTree as ET

    xml_path = PROJECT_ROOT / "cron" / "model_eval_sweep.task.xml"
    tree = ET.parse(str(xml_path))
    root = tree.getroot()
    assert "Task" in root.tag
