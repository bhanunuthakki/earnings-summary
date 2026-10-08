"""Cross-process single-writer evidence for ``execution/daily_fetch_and_brief.py``.

The pool's single-writer guarantee must hold across the driver AND its
writer-capable step subprocesses, not just the driver's threads: every chain
participant takes one cross-process writer gate per resolved database path
(``pipeline.db_writer_gate``) around its actual write windows, and the driver
hands the resolved path to each child entrypoint explicitly (``--db`` /
``--db-path``), so no child ever derives a checkout-local default. These tests
drive the REAL children — real subprocesses, real SQLite, disposable migrated
databases, no barrier fakes — and prove the guarantee from the writer-gate
trace (``EARNINGS_SUMMARY_WRITER_GATE_TRACE``), which every participating
process appends to:

* a real width-3 run over three tickers: the maximum simultaneous writer
  count per database path is 1 — driver windows and step windows interleave
  but never overlap — with the per-ticker chain order intact, no
  ``database is locked`` anywhere, and the accounting rows (evaluator run
  records, ``dcf_runs``) actually landed;
* the env-configured database outside ``repo/data`` is what every child
  receives in its argv, the driver's own writes land there, and no
  checkout-local ``data/portfolio.db`` is ever created;
* two REAL concurrent ``refresh_dcf`` subprocesses serialize their gated
  windows and both land their ``dcf_runs`` rows.

The real children's live-price lookup would egress (yfinance); a dead local
proxy endpoint makes that attempt fail fast so the price resolves from the
seeded FMP cache — hermetic, and still the real fallback code path.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from execution import daily_fetch_and_brief
from runtime.python_process import managed_python_prefix
from tests.fixtures.dcf_fmp_payloads import write_fmp_multi

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REFRESH_DCF = PROJECT_ROOT / "execution" / "refresh_dcf.py"
TICKERS = ("TESTA", "TESTB", "TESTC")
CHAIN_STAGE_NAMES = [
    "thesis_evaluator",
    "match_commitments",
    "refresh_dcf",
    "build_artifacts",
    "sweep_output_history",
]
# A dead local proxy endpoint: connection refused, fast, no egress.
_DEAD_PROXY = "http://127.0.0.1:9"


def _run_with_argv(argv: list[str]) -> int:
    """Run ``daily_fetch_and_brief.main()`` with argv (main reads sys.argv)."""
    previous = sys.argv
    sys.argv = ["daily_fetch_and_brief.py", *argv]
    try:
        return daily_fetch_and_brief.main()
    finally:
        sys.argv = previous


def _seed_tracked(repo_root: Path, tickers: tuple[str, ...]) -> None:
    conn = sqlite3.connect(str(repo_root / "data" / "portfolio.db"))
    try:
        conn.executemany(
            "INSERT INTO tracked_companies (ticker, name, list_type) VALUES (?, ?, ?)",
            [(ticker, f"{ticker} Co", "portfolio") for ticker in tickers],
        )
        conn.commit()
    finally:
        conn.close()


def _make_chain_repo(
    tmp_path: Path, migrated_db: Callable[..., Path], tickers: tuple[str, ...]
) -> Path:
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    (repo / "micro_thesis" / "holdings").mkdir(parents=True)
    (repo / "dcf").mkdir(parents=True)
    (repo / "output").mkdir(parents=True)
    template = migrated_db(tmp_path / "schema" / "template.sqlite")
    write_fmp_multi(template, repo, tickers)
    _seed_tracked(repo, tickers)
    return repo


@pytest.fixture()
def chain_repo(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> Path:
    """A repo whose disposable migrated database carries every ticker's FMP
    payloads and canonical statement facts, plus the tracked rows the pool
    selects from."""
    monkeypatch.delenv("EARNINGS_SUMMARY_DB_PATH", raising=False)
    return _make_chain_repo(tmp_path, migrated_db, TICKERS)


def _hermetic_child_env(monkeypatch: pytest.MonkeyPatch, trace: Path) -> None:
    """What the real children inherit: the writer-gate trace hook, and a dead
    proxy so the live-price lookup fails fast (FMP cache fallback) with no
    network egress. The database is NOT set through the environment here —
    the driver threads it explicitly, which is part of what these tests pin."""
    monkeypatch.delenv("EARNINGS_SUMMARY_DB_PATH", raising=False)
    monkeypatch.setenv("EARNINGS_SUMMARY_WRITER_GATE_TRACE", str(trace))
    for var in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.setenv(var, _DEAD_PROXY)


def _trace_max_depth_per_db(trace: Path) -> dict[str, int]:
    """Scan the enter/exit JSONL in file order.

    Every participant appends its ``enter`` line while holding the gate and
    its ``exit`` line before releasing, so at any prefix of the file the
    unmatched ``enter`` count per database key equals the number of writer
    windows simultaneously open on that database.
    """
    depths: dict[str, int] = {}
    open_now: dict[str, int] = {}
    for line in trace.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        key = str(event["db"])
        if event["event"] == "enter":
            open_now[key] = open_now.get(key, 0) + 1
            depths[key] = max(depths.get(key, 0), open_now[key])
        else:
            open_now[key] = open_now.get(key, 1) - 1
    return depths


def _trace_owners(trace: Path) -> set[str]:
    owners: set[str] = set()
    for line in trace.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event["event"] == "enter":
            owners.add(str(event["owner"]))
    return owners


def _summary_rows(summary: dict[str, object]) -> list[dict[str, object]]:
    return cast("list[dict[str, object]]", summary.get("tickers", []))


def _stage_errors(summary: dict[str, object]) -> list[str]:
    errors: list[str] = []
    for row in _summary_rows(summary):
        for stage in cast("list[dict[str, object]]", row.get("stages", [])):
            error = stage.get("error")
            if isinstance(error, str):
                errors.append(error)
    return errors


# ---------------------------------------------------------------------------
# The real chain: driver + step subprocesses, one writer per database path
# ---------------------------------------------------------------------------


def test_real_width3_chain_never_overlaps_writer_windows_per_database(
    chain_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Real driver, real step subprocesses, three tickers on three pool
    workers, one disposable migrated database. The gate trace must show a
    maximum of ONE open writer window per database path at any instant —
    across the driver process AND the child processes — with the chain order
    intact, no lock contention surfacing as a stage error, and the real
    writes (evaluator run records, dcf_runs) landed."""
    trace = tmp_path / "gate-trace.jsonl"
    _hermetic_child_env(monkeypatch, trace)

    rc = _run_with_argv(["--repo-root", str(chain_repo), "--workers", "3", "--force"])

    summary = json.loads(capsys.readouterr().out)
    assert rc == 0
    by_ticker = {str(row.get("ticker")): row for row in _summary_rows(summary)}
    assert set(by_ticker) == set(TICKERS)

    # The per-ticker chain order survives the pool, and the two writer-capable
    # steps that must have really run (evaluator run records, dcf upsert)
    # exited 0 on every ticker.
    for ticker in TICKERS:
        stages = cast("list[dict[str, object]]", by_ticker[ticker].get("stages", []))
        names = [str(stage.get("name")) for stage in stages]
        assert names == CHAIN_STAGE_NAMES, (ticker, names)
        by_name = {str(stage.get("name")): stage for stage in stages}
        assert by_name["thesis_evaluator"]["exit_code"] == 0, by_name["thesis_evaluator"]
        assert by_name["refresh_dcf"]["exit_code"] == 0, by_name["refresh_dcf"]

    # No step surfaced lock contention (a missing gate would show up as
    # SQLITE_BUSY "database is locked" stage errors under the pool's overlap).
    errors = _stage_errors(summary)
    assert not [error for error in errors if "database is locked" in error], errors

    db_path = chain_repo / "data" / "portfolio.db"
    depths = _trace_max_depth_per_db(trace)
    assert str(db_path) in depths, sorted(depths)
    assert depths[str(db_path)] == 1, f"concurrent writer windows: {depths[str(db_path)]}"

    # The windows came from BOTH the driver process and the child processes.
    owners = _trace_owners(trace)
    driver_owners = {owner for owner in owners if owner.startswith("daily-brief:")}
    evaluator_owners = {owner for owner in owners if owner.startswith("run_thesis_evaluator:")}
    assert driver_owners, owners
    assert evaluator_owners, owners
    assert "match_commitments" in owners, owners
    assert {f"refresh_dcf:{ticker}" for ticker in TICKERS} <= owners, owners

    # The gated writes really landed: the evaluator's run accounting rows and
    # every ticker's dcf_runs row exist in the disposable database.
    conn = sqlite3.connect(str(db_path))
    try:
        evaluator_runs = conn.execute(
            "SELECT COUNT(*) FROM ingestion_runs WHERE directive = 'run_thesis_evaluator'"
        ).fetchone()[0]
        dcf_tickers = {
            str(row[0])
            for row in conn.execute(
                "SELECT DISTINCT ticker FROM dcf_runs WHERE ticker IN (?, ?, ?)", TICKERS
            )
        }
    finally:
        conn.close()
    assert cast(int, evaluator_runs) >= len(TICKERS), evaluator_runs
    assert dcf_tickers == set(TICKERS), dcf_tickers


# ---------------------------------------------------------------------------
# The resolved database path reaches every child; no checkout-local default
# ---------------------------------------------------------------------------


def test_env_configured_database_outside_repo_data_reaches_every_child(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The env-configured database (outside ``repo/data``) is what the driver
    resolves, hands to every child entrypoint explicitly, and writes itself —
    and the checkout-local ``data/portfolio.db`` is never created."""
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    (repo / "micro_thesis" / "holdings").mkdir(parents=True)
    (repo / "dcf").mkdir(parents=True)
    (repo / "output").mkdir(parents=True)
    configured = tmp_path / "elsewhere" / "portfolio.db"
    migrated_db(configured)
    _seed_tracked_into(configured, ("TESTA",))

    monkeypatch.delenv("EARNINGS_SUMMARY_WRITER_GATE_TRACE", raising=False)
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(configured))

    child_calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        child_calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(daily_fetch_and_brief.subprocess, "run", fake_run)

    rc = _run_with_argv(["--repo-root", str(repo), "--workers", "3", "--force"])

    assert rc == 0
    assert child_calls, "no step subprocesses were dispatched"
    # The DB-bearing entrypoints each carry the resolved configured path; the
    # sweep step owns only file history and carries no database flag.
    for argv in child_calls:
        script = Path(next(token for token in argv if token.endswith(".py"))).name
        if script in {"run_thesis_evaluator.py", "match_commitments.py", "refresh_dcf.py"}:
            assert _flag_value(argv, "--db") == str(configured), (script, argv)
        elif script == "build_artifacts.py":
            assert _flag_value(argv, "--db-path") == str(configured), (script, argv)
    assert not (repo / "data" / "portfolio.db").exists()

    # The driver's own completed-build record landed in the CONFIGURED
    # database, not in any checkout-local default.
    conn = sqlite3.connect(str(configured))
    try:
        row = conn.execute(
            "SELECT last_built_at, last_brief_hash FROM tracked_companies "
            "WHERE UPPER(ticker) = 'TESTA'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] is not None and row[1] is not None, row


def _seed_tracked_into(db_path: Path, tickers: tuple[str, ...]) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executemany(
            "INSERT INTO tracked_companies (ticker, name, list_type) VALUES (?, ?, ?)",
            [(ticker, f"{ticker} Co", "portfolio") for ticker in tickers],
        )
        conn.commit()
    finally:
        conn.close()


def _flag_value(argv: list[str], flag: str) -> str | None:
    if flag in argv:
        return argv[argv.index(flag) + 1]
    return None


# ---------------------------------------------------------------------------
# Two real concurrent refresh children serialize their gated windows
# ---------------------------------------------------------------------------


def test_two_real_concurrent_refresh_children_serialize_their_gated_windows(
    chain_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two REAL ``refresh_dcf`` subprocesses running the same ticker pair's
    rebuild concurrently against one disposable migrated database. Their gated
    windows serialize (max one open per database path) and both tickers'
    ``dcf_runs`` rows land."""
    trace = tmp_path / "gate-trace.jsonl"
    _hermetic_child_env(monkeypatch, trace)
    db_path = chain_repo / "data" / "portfolio.db"
    outcomes: dict[str, subprocess.CompletedProcess[str]] = {}

    def run_refresh(ticker: str) -> None:
        outcomes[ticker] = subprocess.run(
            [
                *managed_python_prefix(PROJECT_ROOT),
                str(REFRESH_DCF),
                "--ticker",
                ticker,
                "--repo-root",
                str(chain_repo),
                "--db",
                str(db_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    threads = [
        threading.Thread(target=run_refresh, args=(ticker,), name=f"refresh-{ticker}")
        for ticker in ("TESTA", "TESTB")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(300.0)
        assert not thread.is_alive(), f"{thread.name} did not finish"

    for ticker, proc in outcomes.items():
        assert proc.returncode == 0, (ticker, proc.stderr[-2000:])

    depths = _trace_max_depth_per_db(trace)
    assert str(db_path) in depths, sorted(depths)
    assert depths[str(db_path)] == 1, f"concurrent writer windows: {depths[str(db_path)]}"
    owners = _trace_owners(trace)
    assert {"refresh_dcf:TESTA", "refresh_dcf:TESTB"} <= owners, owners

    conn = sqlite3.connect(str(db_path))
    try:
        dcf_tickers = {
            str(row[0])
            for row in conn.execute(
                "SELECT DISTINCT ticker FROM dcf_runs WHERE ticker IN ('TESTA', 'TESTB')"
            )
        }
    finally:
        conn.close()
    assert dcf_tickers == {"TESTA", "TESTB"}, dcf_tickers
