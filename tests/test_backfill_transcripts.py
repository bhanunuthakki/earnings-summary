"""Tests for execution/backfill_transcripts.py — subprocess phase targeting.

Focuses on the worktree-vs-main-repo path bug: when the script is run from a
worktree with `--repo-root <main>`, the `_run_ingest` and `_run_extract`
subprocesses must invoke the main repo's copy of the helper scripts AND set
`cwd=<main>`. Otherwise the helpers' own `Path(__file__).resolve().parents[1]`
lands in the worktree and `db.py` resolves to the worktree's stub DB.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_module() -> Any:
    src = PROJECT_ROOT / "execution" / "backfill_transcripts.py"
    spec = importlib.util.spec_from_file_location("backfill_transcripts", src)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["backfill_transcripts"] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeProc:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode


def test_run_ingest_uses_repo_root_for_cwd_and_script_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_module()
    repo_root = tmp_path / "main-repo"
    repo_root.mkdir()

    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> _FakeProc:
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeProc(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    rc = mod._run_ingest(repo_root, dry_run=False)
    assert rc == 0
    assert captured["kwargs"]["cwd"] == str(repo_root)
    assert captured["cmd"][1] == str(repo_root / "execution" / "ingest_transcripts.py")


def test_run_extract_uses_repo_root_for_cwd_and_script_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_module()
    repo_root = tmp_path / "main-repo"
    repo_root.mkdir()

    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> _FakeProc:
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeProc(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    rc = mod._run_extract(repo_root, "AAPL", dry_run=False)
    assert rc == 0
    assert captured["kwargs"]["cwd"] == str(repo_root)
    assert captured["cmd"][1] == str(
        repo_root / "execution" / "extract_commitments_from_transcript.py"
    )
    assert "--auto" in captured["cmd"]
    assert "AAPL" in captured["cmd"]


def test_run_ingest_dry_run_skips_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_module()

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("subprocess.run must not be called in dry-run")

    monkeypatch.setattr(mod.subprocess, "run", boom)
    assert mod._run_ingest(Path("/nonexistent"), dry_run=True) == 0


def test_run_extract_dry_run_skips_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_module()

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("subprocess.run must not be called in dry-run")

    monkeypatch.setattr(mod.subprocess, "run", boom)
    assert mod._run_extract(Path("/nonexistent"), "AAPL", dry_run=True) == 0
