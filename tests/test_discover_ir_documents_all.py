"""Tests for execution/discover_ir_documents_all.py (PR5 — batch orchestrator).

Its load-bearing contract is resilience: attempt every roster ticker even when
one fails or times out; report the FAILED count as the exit code only after all
ran; treat a no-ir_url ticker as SKIPPED (not a failure). subprocess.run is
monkeypatched throughout (no real children), and the roster is injected by
string-path monkeypatch — except the one test that drives the real DB filter.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from execution import discover_ir_documents_all as batch  # noqa: E402


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _ticker_of(argv: list[str]) -> str:
    for i, tok in enumerate(argv[:-1]):
        if tok == "--ticker":
            return argv[i + 1]
    return ""


def _stage_of(argv: list[str]) -> str:
    return "discover" if any("discover_ir_documents.py" in a for a in argv) else "fetch"


class _RecordingRun:
    """Per-(ticker, stage) configurable fake for subprocess.run."""

    def __init__(
        self,
        *,
        discover_status: dict[str, str] | None = None,
        discover_rc: dict[str, int] | None = None,
        fetch_rc: dict[str, int] | None = None,
        timeout_discover: set[str] | None = None,
        downloaded: dict[str, int] | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self._dstatus = discover_status or {}
        self._drc = discover_rc or {}
        self._frc = fetch_rc or {}
        self._timeout = timeout_discover or set()
        self._downloaded = downloaded or {}

    def __call__(self, argv: list[str], **kwargs: object) -> _FakeCompleted:
        self.calls.append(list(argv))
        ticker = _ticker_of(argv)
        stage = _stage_of(argv)
        if stage == "discover" and ticker in self._timeout:
            raise subprocess.TimeoutExpired(
                cmd=argv, timeout=cast("float", kwargs.get("timeout", 0))
            )
        if stage == "discover":
            status = self._dstatus.get(ticker, "done")
            out = json.dumps({"ticker": ticker, "status": status, "discovered": 2})
            return _FakeCompleted(self._drc.get(ticker, 0), out)
        out = json.dumps(
            {"ticker": ticker, "status": "done", "downloaded": self._downloaded.get(ticker, 2)}
        )
        return _FakeCompleted(self._frc.get(ticker, 0), out)

    @property
    def stages(self) -> list[tuple[str, str]]:
        return [(_ticker_of(c), _stage_of(c)) for c in self.calls]


def _top_level_json_objects(out: str) -> list[dict[str, object]]:
    objs: list[dict[str, object]] = []
    depth = 0
    start: int | None = None
    in_str = False
    esc = False
    for i, ch in enumerate(out):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    parsed = json.loads(out[start : i + 1])
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict):
                    objs.append(cast("dict[str, object]", parsed))
                start = None
    return objs


def _summary(out: str) -> dict[str, object]:
    objs = _top_level_json_objects(out)
    assert objs, f"no JSON summary in:\n{out}"
    return objs[-1]


def _install(monkeypatch: pytest.MonkeyPatch, fake: _RecordingRun, roster: list[str]) -> None:
    monkeypatch.setattr("execution.discover_ir_documents_all.subprocess.run", fake)

    def _fake_roster(_db: Path, _req: list[str] | None) -> tuple[list[str], list[str]]:
        return sorted(roster), []

    monkeypatch.setattr("execution.discover_ir_documents_all._resolve_roster", _fake_roster)


def _argv(tmp_path: Path, extra: list[str] | None = None) -> list[str]:
    # The resilience tests isolate the discover/fetch stages with --no-process;
    # the post-registration stage gets its own dedicated tests below.
    return [
        "--repo-root",
        str(tmp_path),
        "--db",
        str(tmp_path / "x.db"),
        "--no-process",
        *(extra or []),
    ]


def _script_of(argv: list[str]) -> str:
    for tok in argv:
        if tok.endswith(".py"):
            return Path(tok).name
    return ""


def _make_tracked_db(db: Path, ticker: str) -> None:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE tracked_companies (ticker TEXT, list_type TEXT, archived_at TEXT,"
        " fiscal_year_end TEXT, brief_dirty INTEGER DEFAULT 0)"
    )
    conn.execute(
        "INSERT INTO tracked_companies (ticker, list_type, fiscal_year_end) VALUES (?, 'portfolio', '12-31')",
        (ticker,),
    )
    conn.commit()
    conn.close()


def test_all_roster_tickers_succeed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    fake = _RecordingRun(downloaded={"NU": 4, "ORCL": 3})
    _install(monkeypatch, fake, ["ORCL", "NU"])
    rc = batch.main(_argv(tmp_path))
    assert rc == 0
    s = _summary(capsys.readouterr().out)
    assert s["ok"] == 2
    assert s["failed"] == 0
    assert s["downloaded"] == 7
    # Two stages per ticker, discover before fetch.
    assert fake.stages == [
        ("NU", "discover"),
        ("NU", "fetch"),
        ("ORCL", "discover"),
        ("ORCL", "fetch"),
    ]


def test_no_ir_url_is_skipped_not_failed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    fake = _RecordingRun(discover_status={"ZZ": "no_ir_url"})
    _install(monkeypatch, fake, ["NU", "ZZ"])
    rc = batch.main(_argv(tmp_path))
    assert rc == 0  # SKIPPED is not a failure
    s = _summary(capsys.readouterr().out)
    assert s["skipped"] == 1
    assert s["ok"] == 1
    assert s["failed"] == 0
    # ZZ's fetch stage must NOT run.
    assert ("ZZ", "fetch") not in fake.stages


def test_discover_failure_does_not_abort_batch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    fake = _RecordingRun(discover_rc={"NU": 3})
    _install(monkeypatch, fake, ["MELI", "NU", "ORCL"])
    rc = batch.main(_argv(tmp_path))
    assert rc == 1  # exit code == failure count
    s = _summary(capsys.readouterr().out)
    assert s["failed"] == 1
    assert s["ok"] == 2
    # NU's fetch never ran, but MELI + ORCL fully ran.
    assert ("NU", "fetch") not in fake.stages
    assert ("ORCL", "fetch") in fake.stages


def test_discover_timeout_is_caught_and_batch_continues(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    fake = _RecordingRun(timeout_discover={"MELI"})
    _install(monkeypatch, fake, ["MELI", "NU"])
    rc = batch.main(_argv(tmp_path))
    assert rc == 1
    s = _summary(capsys.readouterr().out)
    assert s["failed"] == 1
    assert s["ok"] == 1
    assert ("NU", "discover") in fake.stages  # NU still ran after MELI timed out


def test_skip_download_runs_discover_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    fake = _RecordingRun()
    _install(monkeypatch, fake, ["NU"])
    rc = batch.main(_argv(tmp_path, ["--skip-download"]))
    assert rc == 0
    assert fake.stages == [("NU", "discover")]  # no fetch
    s = _summary(capsys.readouterr().out)
    assert s["ok"] == 1


def test_roster_filter_uses_real_db(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """--tickers is intersected with the DB roster (portfolio+evaluation only)."""
    db = tmp_path / "x.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE tracked_companies (ticker TEXT, list_type TEXT, archived_at TEXT, fiscal_year_end TEXT)"
    )
    conn.executemany(
        "INSERT INTO tracked_companies (ticker, list_type, archived_at, fiscal_year_end) VALUES (?, ?, ?, ?)",
        [
            ("NU", "portfolio", None, "12-31"),
            ("ORCL", "evaluation", None, "05-31"),
            ("XYZ", "watchlist", None, None),
            ("OLD", "portfolio", "2026-01-01", None),
        ],
    )
    conn.commit()
    conn.close()

    fake = _RecordingRun()
    monkeypatch.setattr("execution.discover_ir_documents_all.subprocess.run", fake)
    rc = batch.main(
        ["--repo-root", str(tmp_path), "--db", str(db), "--tickers", "NU", "XYZ", "FOO"]
    )
    assert rc == 0
    s = _summary(capsys.readouterr().out)
    # XYZ is watchlist (out of roster); FOO not tracked; both surface as not-in-roster.
    assert set(cast("list[str]", s["skipped_not_in_roster"])) == {"XYZ", "FOO"}
    ran = {t for t, _ in fake.stages}
    assert ran == {"NU"}  # only the portfolio/evaluation match ran


# ---------------------------------------------------------------------------
# Stage 3 — post-registration processing into the --enable-llm pipeline
# ---------------------------------------------------------------------------


def test_process_stage_runs_anchor_and_flags_brief_dirty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    db = tmp_path / "x.db"
    _make_tracked_db(db, "NU")
    fake = _RecordingRun(downloaded={"NU": 3})
    _install(monkeypatch, fake, ["NU"])
    rc = batch.main(["--repo-root", str(tmp_path), "--db", str(db)])  # process ON, summaries off
    assert rc == 0
    scripts = [_script_of(c) for c in fake.calls]
    assert "ir_narrative.py" in scripts  # anchor refresh ran (cheap)
    assert "process_ir_documents.py" not in scripts  # LLM summaries are opt-in
    conn = sqlite3.connect(str(db))
    val = conn.execute("SELECT brief_dirty FROM tracked_companies WHERE ticker='NU'").fetchone()[0]
    conn.close()
    assert val == 1  # queued for the daily --enable-llm rebuild
    assert _summary(capsys.readouterr().out)["processed"] == 1


def test_summaries_flag_runs_process_ir_documents(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "x.db"
    _make_tracked_db(db, "NU")
    fake = _RecordingRun(downloaded={"NU": 2})
    _install(monkeypatch, fake, ["NU"])
    batch.main(["--repo-root", str(tmp_path), "--db", str(db), "--summaries"])
    assert "process_ir_documents.py" in [_script_of(c) for c in fake.calls]


def test_no_process_skips_stage3(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    _make_tracked_db(db, "NU")
    fake = _RecordingRun(downloaded={"NU": 2})
    _install(monkeypatch, fake, ["NU"])
    batch.main(["--repo-root", str(tmp_path), "--db", str(db), "--no-process"])
    assert "ir_narrative.py" not in [_script_of(c) for c in fake.calls]
    conn = sqlite3.connect(str(db))
    val = conn.execute("SELECT brief_dirty FROM tracked_companies WHERE ticker='NU'").fetchone()[0]
    conn.close()
    assert val == 0  # untouched


# ---------------------------------------------------------------------------
# Status persistence + the --only-failing rescan
# ---------------------------------------------------------------------------


def _add_status_table(db: Path) -> None:
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE ir_fetch_status (ticker TEXT PRIMARY KEY, last_attempt_at TEXT, "
        "last_status TEXT, discovered INTEGER, downloaded INTEGER, reason TEXT, updated_at TEXT)"
    )
    conn.commit()
    conn.close()


def test_run_ticker_records_status_row(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    _make_tracked_db(db, "NU")
    _add_status_table(db)
    fake = _RecordingRun(downloaded={"NU": 3})
    _install(monkeypatch, fake, ["NU"])
    batch.main(["--repo-root", str(tmp_path), "--db", str(db), "--no-process"])
    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT last_status, downloaded FROM ir_fetch_status WHERE ticker='NU'"
    ).fetchone()
    conn.close()
    assert row == ("ok", 3)  # the attempt outcome is persisted for the dashboard + rescan


def test_only_failing_rescans_only_zero_doc_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "x.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE tracked_companies (ticker TEXT, list_type TEXT, archived_at TEXT,"
        " fiscal_year_end TEXT, brief_dirty INTEGER DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE documents (id INTEGER PRIMARY KEY, ticker TEXT, source_type TEXT,"
        " period_end TEXT, fetched_at TEXT)"
    )
    # NU already has an auto-fetched IR doc; NOW has none (the gap to rescan).
    conn.execute("INSERT INTO documents (ticker, source_type) VALUES ('NU', 'ir_doc')")
    conn.commit()
    conn.close()
    fake = _RecordingRun(downloaded={"NOW": 2})
    _install(monkeypatch, fake, ["NU", "NOW"])
    rc = batch.main(
        ["--repo-root", str(tmp_path), "--db", str(db), "--only-failing", "--no-process"]
    )
    assert rc == 0
    ran = {t for t, _ in fake.stages}
    assert ran == {"NOW"}  # NU already has docs → excluded from the failing-only rescan
