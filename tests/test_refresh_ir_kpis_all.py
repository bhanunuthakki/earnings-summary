"""Tests for execution/refresh_ir_kpis_all.py — the batch IR-refresh orchestrator.

The orchestrator runs ``refresh_ir_kpis.py --ticker <T> --discover`` once per
CONFIGURED ticker, as subprocess-isolated children. Its load-bearing contract is
resilience: it must attempt every selected ticker even when one fails or times
out, report the count of failed tickers as the exit code only after all have run,
and refresh ONLY tickers that have a parser config.

``subprocess.run`` is monkeypatched throughout (no real children spawn) and
``configured_tickers`` is monkeypatched to a fixed set (no dependence on the
on-disk ``micro_thesis/ir_config/`` contents). A recording fake captures each
child's argv (so we can assert dispatch, the ``--discover`` flag, and forwarded
args) and returns a per-ticker-configurable exit code + stdout (so one ticker can
fail while others succeed, and ``rows_inserted`` parsing can be exercised).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import cast

import pytest

from execution import refresh_ir_kpis_all

CHILD_SCRIPT = "refresh_ir_kpis.py"


class _FakeCompleted:
    """Stand-in for subprocess.CompletedProcess (only the read attributes)."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _RecordingRun:
    """Records subprocess.run calls; returns a per-ticker-configurable result.

    ``returncodes`` maps a ticker -> exit code (default 0). ``timeout_tickers`` is
    the set of tickers for which the call raises ``subprocess.TimeoutExpired``
    instead of returning. ``stdouts`` maps a ticker -> the child's stdout (so the
    ``rows_inserted`` JSON parse can be exercised).
    """

    def __init__(
        self,
        *,
        returncodes: dict[str, int] | None = None,
        timeout_tickers: set[str] | None = None,
        stdouts: dict[str, str] | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self._returncodes = returncodes or {}
        self._timeout_tickers = timeout_tickers or set()
        self._stdouts = stdouts or {}

    def __call__(self, argv: list[str], **kwargs: object) -> _FakeCompleted:
        self.calls.append(list(argv))
        assert _script_of(argv) == CHILD_SCRIPT
        ticker = _ticker_of(argv) or ""
        if ticker in self._timeout_tickers:
            timeout = kwargs.get("timeout")
            raise subprocess.TimeoutExpired(
                cmd=argv, timeout=timeout if isinstance(timeout, float | int) else 0
            )
        return _FakeCompleted(
            returncode=self._returncodes.get(ticker, 0),
            stdout=self._stdouts.get(ticker, ""),
        )

    @property
    def tickers(self) -> list[str]:
        """The ordered list of ticker values passed to each child."""
        return [t for c in self.calls if (t := _ticker_of(c)) is not None]

    @property
    def scripts(self) -> list[str]:
        """The ordered list of script basenames invoked (all should be the child)."""
        return [_script_of(c) for c in self.calls]


def _script_of(argv: list[str]) -> str:
    assert len(argv) >= 3
    assert Path(argv[1]).name == "sqlite_bootstrap.py"
    target = Path(argv[2])
    assert target.suffix == ".py"
    return target.name


def _ticker_of(argv: list[str]) -> str | None:
    """The value following ``--ticker`` in an argv."""
    for i, tok in enumerate(argv[:-1]):
        if tok == "--ticker":
            return argv[i + 1]
    return None


def _flag_value(argv: list[str], flag: str) -> str | None:
    for i, tok in enumerate(argv[:-1]):
        if tok == flag:
            return argv[i + 1]
    return None


def _top_level_json_objects(out: str) -> list[dict[str, object]]:
    """Every balanced top-level ``{...}`` object in ``out``, JSON-decoded.

    Robust to nested objects (the summary nests per-ticker dicts) and to multiple
    blobs (each echoed child stdout + the final summary). String-aware so braces
    inside string values don't unbalance the scan. The orchestrator prints the
    summary last, so callers take ``[-1]``.
    """
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


def _mode_of(argv: list[str]) -> str:
    """'file' if the child was told to bootstrap from a local spreadsheet, else
    'discover'."""
    return "file" if "--file" in argv else "discover"


def _install(
    monkeypatch: pytest.MonkeyPatch,
    fake: _RecordingRun,
    configured: list[str],
    on_disk: dict[str, Path] | None = None,
) -> None:
    monkeypatch.setattr("execution.refresh_ir_kpis_all.subprocess.run", fake)

    def _fake_configured(_repo_root: Path) -> list[str]:
        return list(configured)

    def _fake_on_disk(_repo_root: Path) -> dict[str, Path]:
        return dict(on_disk or {})

    monkeypatch.setattr("execution.refresh_ir_kpis_all.configured_tickers", _fake_configured)
    monkeypatch.setattr("execution.refresh_ir_kpis_all._spreadsheet_tickers", _fake_on_disk)


# ---------------------------------------------------------------------------
# Happy path — every configured ticker refreshes
# ---------------------------------------------------------------------------


def test_all_configured_tickers_succeed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every configured ticker is refreshed once, in sorted order; exit 0; the
    summary reports all ok and sums rows_inserted across children."""
    fake = _RecordingRun(
        stdouts={
            "MELI": json.dumps({"ticker": "MELI", "rows_inserted": 10}),
            "NU": json.dumps({"ticker": "NU", "rows_inserted": 24}),
            "WIX": json.dumps({"ticker": "WIX", "rows_inserted": 6}),
        }
    )
    _install(monkeypatch, fake, ["WIX", "NU", "MELI"])  # unsorted on purpose

    rc = refresh_ir_kpis_all.main([])

    assert rc == 0
    # Sorted, one child each, all the child script (never the orchestrator).
    assert fake.tickers == ["MELI", "NU", "WIX"]
    assert set(fake.scripts) == {CHILD_SCRIPT}

    summary = _summary(capsys.readouterr().out)
    assert summary["ok"] == 3
    assert summary["failed"] == 0
    assert summary["rows_inserted"] == 40


def test_discover_and_args_forwarded_to_each_child(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each child argv carries --ticker, --discover, the forwarded --quarters, and
    --repo-root."""
    fake = _RecordingRun()
    _install(monkeypatch, fake, ["NU"])

    rc = refresh_ir_kpis_all.main(["--quarters", "12"])
    assert rc == 0

    argv = fake.calls[0]
    assert "--discover" in argv
    assert _ticker_of(argv) == "NU"
    assert _flag_value(argv, "--quarters") == "12"
    assert _flag_value(argv, "--repo-root") is not None


# ---------------------------------------------------------------------------
# Contract — only configured tickers run
# ---------------------------------------------------------------------------


def test_tickers_filter_intersects_configured(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--tickers is intersected with the configured set: a requested ticker with
    no config is skipped (never spawned) and surfaced under skipped_no_config."""
    fake = _RecordingRun()
    _install(monkeypatch, fake, ["NU", "MELI"])

    rc = refresh_ir_kpis_all.main(["--tickers", "NU", "FOO"])

    assert rc == 0
    assert fake.tickers == ["NU"]  # FOO never spawned
    summary = _summary(capsys.readouterr().out)
    assert summary["skipped_no_config"] == ["FOO"]
    assert summary["ok"] == 1


def test_no_configured_tickers_exits_zero_without_spawning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With no configured tickers, nothing is spawned and the batch exits 0."""
    fake = _RecordingRun()
    _install(monkeypatch, fake, [])

    rc = refresh_ir_kpis_all.main([])

    assert rc == 0
    assert fake.calls == []
    summary = _summary(capsys.readouterr().out)
    assert summary["ok"] == 0
    assert summary["failed"] == 0
    assert summary["tickers"] == []


# ---------------------------------------------------------------------------
# Resilience — one ticker failing/timing out never aborts the batch
# ---------------------------------------------------------------------------


def test_one_failure_does_not_abort_batch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A non-zero child exit for one ticker is counted but the remaining tickers
    still run; exit code == number of failures."""
    fake = _RecordingRun(returncodes={"NU": 4})  # 4 = discover found no URL
    _install(monkeypatch, fake, ["MELI", "NU", "WIX"])

    rc = refresh_ir_kpis_all.main([])

    assert rc == 1
    assert fake.tickers == ["MELI", "NU", "WIX"]  # all attempted

    summary = _summary(capsys.readouterr().out)
    assert summary["ok"] == 2
    assert summary["failed"] == 1
    by_ticker = {
        cast("dict[str, object]", t)["ticker"]: cast("dict[str, object]", t)
        for t in cast("list[object]", summary["tickers"])
    }
    assert by_ticker["NU"]["status"] == "failed"
    assert by_ticker["NU"]["exit_code"] == 4


def test_timeout_is_caught_and_batch_continues(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A TimeoutExpired from one child is caught (not propagated), counted as a
    failure, and does NOT prevent later tickers from running."""
    fake = _RecordingRun(timeout_tickers={"MELI"})
    _install(monkeypatch, fake, ["MELI", "NU"])

    rc = refresh_ir_kpis_all.main([])

    assert rc == 1
    assert fake.tickers == ["MELI", "NU"]  # NU still ran after MELI timed out

    summary = _summary(capsys.readouterr().out)
    assert summary["failed"] == 1
    assert summary["ok"] == 1


def test_all_fail_exit_code_counts_failures(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every ticker failing → all attempted, exit code == ticker count."""
    fake = _RecordingRun(returncodes={"MELI": 1, "NU": 1, "WIX": 1})
    _install(monkeypatch, fake, ["MELI", "NU", "WIX"])

    rc = refresh_ir_kpis_all.main([])

    assert rc == 3
    summary = _summary(capsys.readouterr().out)
    assert summary["failed"] == 3
    assert summary["ok"] == 0


# ---------------------------------------------------------------------------
# rows_inserted parsing from the child's JSON stdout
# ---------------------------------------------------------------------------


def test_rows_inserted_parsed_from_child_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """rows_inserted is lifted from the child's (indented) JSON stdout."""
    fake = _RecordingRun(
        stdouts={"NU": json.dumps({"ticker": "NU", "rows_inserted": 24, "doc_id": 5}, indent=2)}
    )
    _install(monkeypatch, fake, ["NU"])

    rc = refresh_ir_kpis_all.main([])
    assert rc == 0

    summary = _summary(capsys.readouterr().out)
    nu = cast("dict[str, object]", cast("list[object]", summary["tickers"])[0])
    assert nu["rows_inserted"] == 24


def test_exit_zero_with_unparseable_stdout_is_ok_with_null_rows(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A child that exits 0 but prints no parseable JSON still counts as ok; its
    rows_inserted is reported as null rather than crashing the parse."""
    fake = _RecordingRun(stdouts={"NU": "some warning line, no json here"})
    _install(monkeypatch, fake, ["NU"])

    rc = refresh_ir_kpis_all.main([])
    assert rc == 0

    summary = _summary(capsys.readouterr().out)
    nu = cast("dict[str, object]", cast("list[object]", summary["tickers"])[0])
    assert nu["status"] == "ok"
    assert nu["rows_inserted"] is None


# ---------------------------------------------------------------------------
# Lifting the configured-only (NU-only) ceiling: tickers with an on-disk IR
# spreadsheet but no config are bootstrapped via --file
# ---------------------------------------------------------------------------


def test_spreadsheet_only_ticker_runs_via_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ticker with a downloaded spreadsheet but NO config still runs — via
    --file (not --discover), so its config is auto-built from the file."""
    fake = _RecordingRun()
    path = Path("/data/ir_spreadsheets/FOO/FOO_1Q26.xlsx")
    _install(monkeypatch, fake, configured=[], on_disk={"FOO": path})

    rc = refresh_ir_kpis_all.main([])

    assert rc == 0
    assert fake.tickers == ["FOO"]
    argv = fake.calls[0]
    assert _mode_of(argv) == "file"
    assert _flag_value(argv, "--file") == str(path)
    assert "--discover" not in argv


def test_configured_ticker_discovers_even_when_file_on_disk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured ticker always re-discovers its current spreadsheet, even if a
    stale file also sits on disk."""
    fake = _RecordingRun()
    _install(
        monkeypatch,
        fake,
        configured=["NU"],
        on_disk={"NU": Path("/data/ir_spreadsheets/NU/old.xlsx")},
    )

    rc = refresh_ir_kpis_all.main([])

    assert rc == 0
    assert fake.tickers == ["NU"]
    assert _mode_of(fake.calls[0]) == "discover"


def test_universe_is_union_of_configured_and_on_disk_sorted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With no filter, the universe = configured (discover) ∪ on-disk (file),
    sorted; each ticker uses the right mode."""
    fake = _RecordingRun()
    _install(
        monkeypatch,
        fake,
        configured=["NU"],
        on_disk={"MELI": Path("/d/MELI/x.xlsx"), "WIX": Path("/d/WIX/y.xlsx")},
    )

    rc = refresh_ir_kpis_all.main([])

    assert rc == 0
    assert fake.tickers == ["MELI", "NU", "WIX"]
    modes = {_ticker_of(c): _mode_of(c) for c in fake.calls}
    assert modes == {"MELI": "file", "NU": "discover", "WIX": "file"}


def test_requested_ticker_in_neither_set_is_skipped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--tickers intersects the WHOLE universe; a request with neither a config
    nor an on-disk file is skipped, while an on-disk-only ticker still runs."""
    fake = _RecordingRun()
    _install(
        monkeypatch,
        fake,
        configured=["NU"],
        on_disk={"FOO": Path("/d/FOO/x.xlsx")},
    )

    rc = refresh_ir_kpis_all.main(["--tickers", "NU", "FOO", "BAR"])

    assert rc == 0
    assert fake.tickers == ["FOO", "NU"]  # BAR skipped; sorted
    summary = _summary(capsys.readouterr().out)
    assert summary["skipped_no_config"] == ["BAR"]


def test_spreadsheet_tickers_scans_disk(tmp_path: Path) -> None:
    """`_spreadsheet_tickers` returns the newest .xlsx per ticker folder and
    ignores lock files / non-dirs."""
    base = tmp_path / "data" / "ir_spreadsheets"
    (base / "NU").mkdir(parents=True)
    (base / "MELI").mkdir(parents=True)
    old = base / "NU" / "NU_4Q25.xlsx"
    new = base / "NU" / "NU_1Q26.xlsx"
    old.write_bytes(b"old")
    new.write_bytes(b"new")
    import os

    os.utime(old, (1, 1))  # force `new` to be the most-recent
    (base / "NU" / "~$lock.xlsx").write_bytes(b"lock")  # ignored
    (base / "MELI" / "MELI_1Q26.xlsx").write_bytes(b"x")
    (base / "stray_file.txt").write_bytes(b"x")  # non-dir ignored

    found = refresh_ir_kpis_all._spreadsheet_tickers(tmp_path)
    assert set(found) == {"NU", "MELI"}
    assert found["NU"].name == "NU_1Q26.xlsx"
