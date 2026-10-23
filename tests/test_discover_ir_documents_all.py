"""Tests for execution/discover_ir_documents_all.py (PR5 — batch orchestrator).

Its load-bearing contract is resilience: attempt every roster ticker even when
one fails or times out; report the FAILED count as the exit code only after all
ran; treat a no-ir_url ticker as SKIPPED (not a failure). subprocess.run is
monkeypatched throughout (no real children), and the roster is injected by
string-path monkeypatch — except the one test that drives the real DB filter.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from execution import discover_ir_documents_all as batch
from execution import onboard_ticker


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
    return "discover" if _script_of(argv) == "discover_ir_documents.py" else "fetch"


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
        if _script_of(argv) == "process_document_evidence.py":
            return _FakeCompleted(
                0,
                json.dumps(
                    {
                        "mode": "apply",
                        "items": [
                            {"document_id": 1, "ticker": ticker, "status": "already-covered"}
                        ],
                        "status_counts": {"already-covered": 2},
                        "has_more": False,
                        "degraded": False,
                    }
                ),
            )
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
            {
                "ticker": ticker,
                "status": "done",
                "downloaded": self._downloaded.get(ticker, 2),
                "failed": 0,
                "categorize_rc": 0 if self._downloaded.get(ticker, 2) else None,
            }
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
    # The batch now requires an existing state file before it invokes children.
    sqlite3.connect(tmp_path / "x.db").close()
    return [
        "--repo-root",
        str(tmp_path),
        "--db",
        str(tmp_path / "x.db"),
        "--no-process",
        *(extra or []),
    ]


def _script_of(argv: list[str]) -> str:
    assert len(argv) >= 3
    assert Path(argv[1]).name == "sqlite_bootstrap.py"
    target = Path(argv[2])
    assert target.suffix == ".py"
    return target.name


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
    # Network-only discovery fans out in scheduler-dependent order; shared
    # fetch/register work stays serialized in deterministic ticker order.
    assert sorted(item for item in fake.stages if item[1] == "discover") == [
        ("NU", "discover"),
        ("ORCL", "discover"),
    ]
    assert [item for item in fake.stages if item[1] == "fetch"] == [
        ("NU", "fetch"),
        ("ORCL", "fetch"),
    ]
    first_fetch = next(i for i, item in enumerate(fake.stages) if item[1] == "fetch")
    assert all(item[1] == "discover" for item in fake.stages[:first_fetch])


def test_discovery_auth_denial_is_typed_for_batch_halt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def auth_child(_argv: list[str], _timeout_s: float) -> _FakeCompleted:
        return _FakeCompleted(
            10,
            "",
            '{"event": "source_authentication_denied", "status": 403}\n',
        )

    monkeypatch.setattr(batch, "_run_child", auth_child)

    result = cast(Callable[..., batch.DiscoveryResult], getattr(batch, "_run_discovery"))(
        "NU",
        repo_root=tmp_path,
        db_path=tmp_path / "portfolio.db",
        quarters=5,
        timeout_s=1,
        owner_requested=False,
    )

    assert result.status is batch.TickerStatus.FAILED
    assert result.error == "auth_denial: discover halted"


def test_onboarding_marks_ir_collection_as_owner_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def run_ticker(ticker: str, **kwargs: object) -> object:
        captured.update(ticker=ticker, **kwargs)
        return type("Result", (), {"status": batch.TickerStatus.OK})()

    monkeypatch.setattr(batch, "run_ticker", run_ticker)

    assert cast(Callable[[str], int], getattr(onboard_ticker, "_run_ir_documents"))("EVAL") == 0
    assert captured["owner_requested"] is True


def test_batch_halts_before_fetch_when_discovery_reports_auth_denial(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def roster(_db: Path, _requested: list[str] | None) -> tuple[list[str], list[str]]:
        return ["NU"], []

    def denied_discovery(
        ticker: str,
        *,
        repo_root: Path,
        db_path: Path,
        quarters: int,
        timeout_s: float,
        owner_requested: bool,
    ) -> batch.DiscoveryResult:
        del repo_root, db_path, quarters, timeout_s, owner_requested
        return batch.DiscoveryResult(
            ticker,
            batch.TickerStatus.FAILED,
            None,
            0.0,
            error="auth_denial: discover halted",
        )

    def fail_fetch(*_args: object, **_kwargs: object) -> None:
        pytest.fail("fetch boundary crossed after auth denial")

    monkeypatch.setattr(batch, "_resolve_roster", roster)
    monkeypatch.setattr(
        batch,
        "_run_discovery",
        denied_discovery,
    )
    monkeypatch.setattr(batch, "_finish_discovery", fail_fetch)

    assert batch.main(_argv(tmp_path)) == 10


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


def test_no_documents_discovered_fails_without_using_stale_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _RecordingRun(discover_status={"NU": "no_docs"})
    _install(monkeypatch, fake, ["NU"])
    assert batch.main(_argv(tmp_path)) == 1
    assert ("NU", "fetch") not in fake.stages


def test_partial_crawl_retains_candidates_and_retries_discovery(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    first = _RecordingRun(discover_status={"NU": "partial"}, downloaded={"NU": 1})
    _install(monkeypatch, first, ["NU"])
    argv = _argv(tmp_path)
    assert batch.main(argv) == 1
    assert first.stages == [("NU", "discover"), ("NU", "fetch")]
    result = _summary(capsys.readouterr().out)
    assert result["downloaded"] == 1
    assert result["failed"] == 1
    retry = _RecordingRun(downloaded={"NU": 0})
    _install(monkeypatch, retry, ["NU"])
    assert batch.main(argv) == 0
    assert retry.stages == [("NU", "discover"), ("NU", "fetch")]


def test_discovery_reads_nested_inventory_diagnostics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = {
        "ticker": "NU",
        "status": "done",
        "discovered": 1,
        "crawl_complete": True,
        "archive_complete": False,
        "pages": [{"page_url": "https://issuer.example/results", "outcome": "succeeded"}],
    }

    def child(argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    monkeypatch.setattr(batch, "_run_child", child)
    result = cast(Callable[..., batch.DiscoveryResult], getattr(batch, "_run_discovery"))(
        "NU",
        repo_root=tmp_path,
        db_path=tmp_path / "db.sqlite",
        quarters=5,
        timeout_s=30,
        owner_requested=True,
    )
    assert result.status is batch.TickerStatus.OK
    assert result.discovered == 1


def test_fetch_reported_partial_failure_is_not_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, _RecordingRun(), ["NU"])

    def child(argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        if _script_of(argv) == "discover_ir_documents.py":
            payload = {"ticker": "NU", "status": "done", "discovered": 2}
        else:
            payload = {
                "ticker": "NU",
                "status": "done",
                "downloaded": 1,
                "failed": 1,
                "categorize_rc": 0,
            }
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    monkeypatch.setattr(batch, "_run_child", child)
    assert batch.main(_argv(tmp_path)) == 1


def test_missing_or_invalid_roster_fails_closed(tmp_path: Path) -> None:
    db = tmp_path / "missing.db"
    assert batch.main(["--repo-root", str(tmp_path), "--db", str(db)]) == 2
    assert not db.exists()
    sqlite3.connect(db).close()
    assert batch.main(["--repo-root", str(tmp_path), "--db", str(db)]) == 2


def test_malformed_stored_roster_role_fails_closed(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE tracked_companies (ticker TEXT, list_type TEXT, archived_at TEXT)"
        )
        conn.execute("INSERT INTO tracked_companies VALUES ('NU', 'unknown_role', NULL)")
    assert batch.main(["--repo-root", str(tmp_path), "--db", str(db)]) == 2


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


def test_checkpoint_resumes_after_fetch_failure_without_repeating_discovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first = _RecordingRun(fetch_rc={"NU": 3})
    _install(monkeypatch, first, ["NU"])
    assert batch.main(_argv(tmp_path)) == 1
    checkpoint = tmp_path / ".tmp" / "ir_document_discovery_all" / "state.json"
    assert checkpoint.is_file()

    second = _RecordingRun(downloaded={"NU": 2})
    monkeypatch.setattr("execution.discover_ir_documents_all.subprocess.run", second)
    assert batch.main(_argv(tmp_path)) == 0
    assert second.stages == [("NU", "fetch")]
    assert not checkpoint.exists()


def test_checkpoint_retries_failed_discovery_on_resume(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first = _RecordingRun(discover_rc={"NU": 3})
    _install(monkeypatch, first, ["NU"])
    assert batch.main(_argv(tmp_path)) == 1
    checkpoint = tmp_path / ".tmp" / "ir_document_discovery_all" / "state.json"
    assert checkpoint.is_file()

    second = _RecordingRun(downloaded={"NU": 2})
    monkeypatch.setattr("execution.discover_ir_documents_all.subprocess.run", second)

    assert batch.main(_argv(tmp_path)) == 0
    assert second.stages == [("NU", "discover"), ("NU", "fetch")]
    assert not checkpoint.exists()


def test_old_completed_checkpoint_reuses_crawl_but_continues_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "x.db"
    _make_tracked_db(db, "NU")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO tracked_companies (ticker, list_type, fiscal_year_end) "
            "VALUES ('ORCL', 'evaluation', '05-31')"
        )
    first = _RecordingRun(fetch_rc={"ORCL": 3})
    _install(monkeypatch, first, ["NU", "ORCL"])
    argv = ["--repo-root", str(tmp_path), "--db", str(db)]
    assert batch.main(argv) == 1
    checkpoint = tmp_path / ".tmp" / "ir_document_discovery_all" / "state.json"
    old = json.loads(checkpoint.read_text())
    old.pop("processing_contract_version")
    checkpoint.write_text(json.dumps(old))
    second = _RecordingRun(downloaded={"NU": 0, "ORCL": 0})
    _install(monkeypatch, second, ["NU", "ORCL"])
    assert batch.main(argv) == 0
    assert not any(_script_of(command) == "discover_ir_documents.py" for command in second.calls)
    evidence = [
        command for command in second.calls if _script_of(command) == "process_document_evidence.py"
    ]
    assert {_ticker_of(command) for command in evidence} == {"NU", "ORCL"}
    assert not checkpoint.exists()


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
    """--tickers is intersected with the active full-coverage roster."""
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
        [
            "--repo-root",
            str(tmp_path),
            "--db",
            str(db),
            "--no-process",
            "--tickers",
            "NU",
            "XYZ",
            "FOO",
        ]
    )
    assert rc == 0
    s = _summary(capsys.readouterr().out)
    assert set(cast("list[str]", s["skipped_not_in_roster"])) == {"FOO"}
    ran = {t for t, _ in fake.stages}
    assert ran == {"NU", "XYZ"}


def test_default_roster_covers_all_three_roles_and_keeps_index_excluded(
    tmp_path: Path,
) -> None:
    db = tmp_path / "x.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE tracked_companies (ticker TEXT, list_type TEXT, archived_at TEXT)")
    conn.executemany(
        "INSERT INTO tracked_companies VALUES (?, ?, NULL)",
        [
            ("PORT", "portfolio"),
            ("EVAL", "evaluation"),
            ("WATCH", "watchlist"),
            ("IDX", "index_member"),
        ],
    )
    conn.commit()
    conn.close()

    assert cast(
        Callable[[Path, list[str] | None], tuple[list[str], list[str]]],
        getattr(batch, "_resolve_roster"),
    )(db, None) == (["PORT", "EVAL", "WATCH"], [])
    assert cast(
        Callable[[Path, list[str] | None], tuple[list[str], list[str]]],
        getattr(batch, "_resolve_roster"),
    )(db, ["EVAL", "WATCH", "IDX"]) == (
        ["EVAL", "WATCH"],
        ["IDX"],
    )


def test_ir_quarter_window_is_capped_at_five() -> None:
    with pytest.raises(SystemExit):
        cast(Callable[[list[str] | None], argparse.Namespace], getattr(batch, "_parse_args"))(
            ["--max-quarters", "6"]
        )


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
    assert scripts.index("process_document_evidence.py") < scripts.index("ir_narrative.py")
    assert "ir_narrative.py" in scripts  # anchor refresh ran (cheap)
    assert "process_ir_documents_state.py" not in scripts  # LLM summaries are opt-in
    conn = sqlite3.connect(str(db))
    val = conn.execute("SELECT brief_dirty FROM tracked_companies WHERE ticker='NU'").fetchone()[0]
    conn.close()
    assert val == 1  # queued for the daily --enable-llm rebuild
    assert _summary(capsys.readouterr().out)["processed"] == 1


def test_unchanged_downloads_still_continue_canonical_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "x.db"
    _make_tracked_db(db, "NU")
    fake = _RecordingRun(downloaded={"NU": 0})
    _install(monkeypatch, fake, ["NU"])
    assert batch.main(["--repo-root", str(tmp_path), "--db", str(db)]) == 0
    command = next(c for c in fake.calls if _script_of(c) == "process_document_evidence.py")
    assert command[command.index("--db") + 1] == str(db)
    assert command[command.index("--repo-root") + 1] == str(tmp_path)
    assert command[command.index("--ticker") + 1] == "NU"
    assert command[command.index("--source-type") + 1] == "ir_doc"
    assert "--apply" in command
    assert "--resume" not in command  # Do not create a ticker-specific cursor owner.


def test_incomplete_evidence_blocks_summary_handoff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "x.db"
    _make_tracked_db(db, "NU")
    calls: list[str] = []

    def child(argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        script = _script_of(argv)
        calls.append(script)
        assert script == "process_document_evidence.py"
        return subprocess.CompletedProcess(
            argv,
            2,
            json.dumps(
                {
                    "mode": "apply",
                    "status_counts": {"quarantined": 1},
                    "items": [{"document_id": 1, "ticker": "NU", "status": "quarantined"}],
                    "has_more": False,
                    "degraded": True,
                }
            ),
            "",
        )

    monkeypatch.setattr(batch, "_run_child", child)
    assert not cast(Callable[..., bool], getattr(batch, "_run_process_stage"))(
        "NU", repo_root=tmp_path, db_path=db, summaries=True, timeout_s=30
    )
    assert calls == ["process_document_evidence.py"]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT brief_dirty FROM tracked_companies").fetchone() == (0,)


@pytest.mark.parametrize("stuck_cursor", [False, True])
def test_evidence_uses_existing_cursor_to_drain_large_ticker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stuck_cursor: bool
) -> None:
    calls: list[list[str]] = []

    def child(argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        assert _script_of(argv) == "process_document_evidence.py"
        payload = {
            "mode": "apply",
            "items": [
                {
                    "document_id": 501 if len(calls) == 1 else 1,
                    "ticker": "NU",
                    "status": "already-covered",
                    "inventory_state": "linked",
                    "coverage_statuses": ["extracted"],
                }
            ],
            "has_more": len(calls) == 1 or stuck_cursor,
            "next_before_document_id": 501,
            "degraded": len(calls) == 1 or stuck_cursor,
        }
        return subprocess.CompletedProcess(
            argv, 2 if payload["degraded"] else 0, json.dumps(payload), ""
        )

    monkeypatch.setattr(batch, "_run_child", child)
    result = cast(Callable[..., bool], getattr(batch, "_run_process_stage"))(
        "NU",
        repo_root=tmp_path,
        db_path=tmp_path / "fixture.db",
        summaries=False,
        timeout_s=30,
        refresh_views=False,
    )
    # The first page reports more work. That alone is not a permanent degradation.
    assert result is (not stuck_cursor)
    assert len(calls) == 2
    assert "--before-document-id" not in calls[0]
    assert calls[1][calls[1].index("--before-document-id") + 1] == "501"
    assert all(command[command.index("--source-type") + 1] == "ir_doc" for command in calls)


def test_missing_inventory_allows_existing_views_but_stays_degraded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "x.db"
    _make_tracked_db(db, "NU")
    calls: list[str] = []

    def child(argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        script = _script_of(argv)
        calls.append(script)
        if script == "process_document_evidence.py":
            return subprocess.CompletedProcess(
                argv,
                2,
                json.dumps(
                    {
                        "mode": "apply",
                        "status_counts": {"extracted": 1},
                        "items": [{"document_id": 1, "ticker": "NU", "status": "extracted"}],
                        "has_more": False,
                        "degraded": True,
                        "inventory_uninitialized": 1,
                    }
                ),
                "",
            )
        return subprocess.CompletedProcess(argv, 0, '{"failed": 0}', "")

    monkeypatch.setattr(batch, "_run_child", child)
    assert not cast(Callable[..., bool], getattr(batch, "_run_process_stage"))(
        "NU", repo_root=tmp_path, db_path=db, summaries=True, timeout_s=30
    )
    assert calls == [
        "process_document_evidence.py",
        "ir_narrative.py",
        "process_ir_documents_state.py",
    ]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT brief_dirty FROM tracked_companies").fetchone() == (1,)


def test_summaries_flag_runs_process_ir_documents(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "x.db"
    _make_tracked_db(db, "NU")
    fake = _RecordingRun(downloaded={"NU": 2})
    _install(monkeypatch, fake, ["NU"])
    batch.main(["--repo-root", str(tmp_path), "--db", str(db), "--summaries"])
    command = next(c for c in fake.calls if _script_of(c) == "process_ir_documents_state.py")
    assert command[command.index("--db") + 1] == str(db)
    assert command[command.index("--repo-root") + 1] == str(tmp_path)
    assert "--regenerate-missing" in command


def test_existing_uncached_narrative_is_processed_without_new_downloads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "x.db"
    _make_tracked_db(db, "NU")
    source = tmp_path / "ir_documents" / "NU" / "2026-06-30" / "ir_presentation__abcd1234.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source already downloaded")
    fake = _RecordingRun(downloaded={"NU": 0})
    _install(monkeypatch, fake, ["NU"])
    assert batch.main(["--repo-root", str(tmp_path), "--db", str(db)]) == 0
    assert "ir_narrative.py" in [_script_of(c) for c in fake.calls]


def test_processing_failure_is_failed_and_retried_after_download_count_drops_to_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "x.db"
    _make_tracked_db(db, "NU")
    source = tmp_path / "ir_documents" / "NU" / "2026-06-30" / "ir_presentation__abcd1234.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source already downloaded")
    fake = _RecordingRun(downloaded={"NU": 2})
    _install(monkeypatch, fake, ["NU"])

    def failed_process(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(batch, "_run_process_stage", failed_process)
    argv = ["--repo-root", str(tmp_path), "--db", str(db)]
    assert batch.main(argv) == 1
    # Extraction may have completed before a later handoff failed. The checkpoint
    # must retain that pending handoff even when the narrative cache now exists.
    cache = tmp_path / "data" / "ir_narrative" / "NU" / "ir_presentation__2026-06-30.txt"
    cache.parent.mkdir(parents=True)
    cache.write_text("extraction succeeded before handoff failed", encoding="utf-8")
    retry = _RecordingRun(downloaded={"NU": 0})
    _install(monkeypatch, retry, ["NU"])
    calls: list[str] = []

    def process(ticker: str, **_kwargs: object) -> bool:
        calls.append(ticker)
        return True

    monkeypatch.setattr(batch, "_run_process_stage", process)
    assert batch.main(argv) == 0
    assert calls == ["NU"]


def test_unverified_existing_narrative_is_rechecked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "x.db"
    _make_tracked_db(db, "NU")
    source = tmp_path / "ir_documents" / "NU" / "2026-06-30" / "ir_presentation__abcd1234.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source already downloaded")
    cache = tmp_path / "data" / "ir_narrative" / "NU" / "ir_presentation__2026-06-30.txt"
    cache.parent.mkdir(parents=True)
    cache.write_text("processed narrative", encoding="utf-8")
    fake = _RecordingRun(downloaded={"NU": 0})
    _install(monkeypatch, fake, ["NU"])
    assert batch.main(["--repo-root", str(tmp_path), "--db", str(db)]) == 0
    assert "ir_narrative.py" in [_script_of(c) for c in fake.calls]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT brief_dirty FROM tracked_companies").fetchone() == (1,)


def test_summary_reported_failure_is_not_hidden_by_zero_exit_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "x.db"
    _make_tracked_db(db, "NU")

    def child(argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        if _script_of(argv) == "process_document_evidence.py":
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps(
                    {
                        "mode": "apply",
                        "status_counts": {"already-covered": 1},
                        "items": [{"document_id": 1, "ticker": "NU", "status": "already-covered"}],
                        "has_more": False,
                        "degraded": False,
                    }
                ),
                "",
            )
        summary = _script_of(argv) == "process_ir_documents_state.py"
        return subprocess.CompletedProcess(argv, 0, json.dumps({"failed": int(summary)}), "")

    monkeypatch.setattr(batch, "_run_child", child)
    assert not cast(Callable[..., bool], getattr(batch, "_run_process_stage"))(
        "NU", repo_root=tmp_path, db_path=db, summaries=True, timeout_s=30
    )
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT brief_dirty FROM tracked_companies").fetchone() == (0,)


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


@pytest.mark.parametrize("owner_requested", [False, True])
def test_processing_continuation_retains_request_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    owner_requested: bool,
) -> None:
    fake = _RecordingRun()
    monkeypatch.setattr(batch.subprocess, "run", fake)
    # The exact ticker is always present, but it must not imply a request flag.
    getattr(batch, "_run_process_stage")(
        "NU",
        repo_root=tmp_path,
        db_path=tmp_path / "test.db",
        summaries=False,
        timeout_s=30,
        refresh_views=False,
        owner_requested=owner_requested,
    )
    evidence = [
        command for command in fake.calls if _script_of(command) == "process_document_evidence.py"
    ]
    assert len(evidence) == 1
    assert ("--requested" in evidence[0]) is owner_requested
    assert _ticker_of(evidence[0]) == "NU"
