"""Tests for the ticker-specific extractor dispatcher in execution/build_artifacts.py.

`_TICKER_SPECIFIC_EXTRACTORS` maps a ticker to a list of (script, args) pairs.
`_run_ticker_specific_extractors` walks the entry for the given ticker and
subprocess-isolates each call. Failures must not abort the build.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Generator
from pathlib import Path
from typing import cast

import build_artifacts
import pytest

from db_paths import db_path_context

PROJECT_ROOT = Path(__file__).resolve().parents[1]

run_extractors = cast(
    Callable[[str, Path], None], getattr(build_artifacts, "_run_ticker_specific_extractors")
)


@pytest.fixture(autouse=True)
def explicit_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[Path]:
    database = tmp_path / "explicit-authority.sqlite"
    database.touch()
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(database))
    with db_path_context(database):
        yield database


def test_nvo_is_in_extractor_map() -> None:
    """The audit memo's reference example: NVO has a patent-timeline extractor.
    The dispatcher must auto-fire it."""
    entries = cast(
        dict[str, list[tuple[str, list[str]]]],
        getattr(build_artifacts, "_TICKER_SPECIFIC_EXTRACTORS"),
    ).get("NVO")
    assert entries is not None
    assert any("extract_nvo_patent_timeline_state.py" in e[0] for e in entries)


def test_unknown_ticker_is_silent_noop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Tickers not in the map produce no subprocess call."""
    captured: list[list[str]] = []

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.append(cmd)

        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(build_artifacts.subprocess, "run", _fake_run)
    run_extractors("AAPL", tmp_path)
    assert captured == []


def test_mapped_ticker_invokes_subprocess(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """NVO build calls the state-bound patent extractor exactly once."""
    captured: list[list[str]] = []

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.append(cmd)

        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(build_artifacts.subprocess, "run", _fake_run)
    run_extractors("NVO", tmp_path)
    assert len(captured) == 1
    assert "extract_nvo_patent_timeline_state.py" in " ".join(captured[0])
    assert captured[0][captured[0].index("--repo-root") + 1] == str(tmp_path)
    assert captured[0][captured[0].index("--db") + 1] == str(tmp_path / "explicit-authority.sqlite")


def test_subprocess_failure_does_not_raise(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A non-zero exit from the extractor must not abort the build."""

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError("simulated extractor crash")

    monkeypatch.setattr(build_artifacts.subprocess, "run", _fake_run)
    # Should not raise.
    run_extractors("NVO", tmp_path)


def test_timeout_does_not_raise(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A subprocess timeout must not abort the build."""
    import subprocess as _sp

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise _sp.TimeoutExpired(cmd, 300)

    monkeypatch.setattr(build_artifacts.subprocess, "run", _fake_run)
    run_extractors("NVO", tmp_path)
