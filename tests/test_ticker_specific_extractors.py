"""Tests for the ticker-specific extractor dispatcher in execution/build_artifacts.py.

`_TICKER_SPECIFIC_EXTRACTORS` maps a ticker to a list of (script, args) pairs.
`_run_ticker_specific_extractors` walks the entry for the given ticker and
subprocess-isolates each call. Failures must not abort the build.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import build_artifacts  # noqa: E402


def test_nvo_is_in_extractor_map() -> None:
    """The audit memo's reference example: NVO has a patent-timeline extractor.
    The dispatcher must auto-fire it."""
    entries = build_artifacts._TICKER_SPECIFIC_EXTRACTORS.get("NVO")
    assert entries is not None
    assert any("extract_nvo_patent_timeline.py" in e[0] for e in entries)


def test_unknown_ticker_is_silent_noop(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Tickers not in the map produce no subprocess call."""
    captured: list[list[str]] = []

    def _fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        captured.append(cmd)
        class _R:
            returncode = 0
            stderr = ""
        return _R()

    monkeypatch.setattr(build_artifacts.subprocess, "run", _fake_run)
    build_artifacts._run_ticker_specific_extractors("AAPL", tmp_path)
    assert captured == []


def test_mapped_ticker_invokes_subprocess(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """NVO build calls extract_nvo_patent_timeline.py exactly once."""
    captured: list[list[str]] = []

    def _fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        captured.append(cmd)
        class _R:
            returncode = 0
            stderr = ""
        return _R()

    monkeypatch.setattr(build_artifacts.subprocess, "run", _fake_run)
    build_artifacts._run_ticker_specific_extractors("NVO", tmp_path)
    assert len(captured) == 1
    assert "extract_nvo_patent_timeline.py" in " ".join(captured[0])


def test_subprocess_failure_does_not_raise(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """A non-zero exit from the extractor must not abort the build."""
    import subprocess as _sp

    def _fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("simulated extractor crash")

    monkeypatch.setattr(build_artifacts.subprocess, "run", _fake_run)
    # Should not raise.
    build_artifacts._run_ticker_specific_extractors("NVO", tmp_path)


def test_timeout_does_not_raise(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """A subprocess timeout must not abort the build."""
    import subprocess as _sp

    def _fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        raise _sp.TimeoutExpired(cmd, 300)

    monkeypatch.setattr(build_artifacts.subprocess, "run", _fake_run)
    build_artifacts._run_ticker_specific_extractors("NVO", tmp_path)
