"""Search build writers must share the repo's cross-process lock discipline."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from runtime.job_runtime import JobAlreadyRunningError


class _BusyLock:
    write_sets: ClassVar[list[str]] = []

    def __init__(self, repo_root: Path, job_name: str, write_sets: list[str]) -> None:
        assert repo_root.exists()
        assert job_name
        type(self).write_sets = write_sets

    def __enter__(self) -> _BusyLock:
        raise JobAlreadyRunningError("already owned")

    def __exit__(self, *args: object) -> None:
        return None


def test_vector_apply_returns_retryable_exit_before_opening_locked_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import execution.build_evidence_vector_index as command

    monkeypatch.setattr(command, "JobLock", _BusyLock)
    result = command.main(
        [
            "--db",
            str(tmp_path / "absent.db"),
            "--manifest-id",
            "manifest",
            "--index-run-id",
            "run",
            "--index-key",
            "key",
            "--revision",
            "1",
            "--model",
            "model",
            "--dimensions",
            "2",
            "--runtime-artifact",
            str(tmp_path / "runtime-artifact.json"),
            "--runtime-root",
            str(tmp_path / "runtime"),
            "--index-root",
            str(tmp_path / "index"),
            "--apply",
        ]
    )
    assert result == 75
    assert "portfolio-db" in _BusyLock.write_sets
    assert any(item.startswith("sqlite:") for item in _BusyLock.write_sets)
    assert any(item.startswith("vector-index:") for item in _BusyLock.write_sets)
    assert any(item.startswith("vector-checkpoint:") for item in _BusyLock.write_sets)
    assert not (tmp_path / "absent.db").exists()


def test_corpus_apply_returns_retryable_exit_before_opening_locked_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import execution.build_grounded_search_corpus as command

    inventory = tmp_path / "inventory.json"
    inventory.write_text('{"expected_documents":[]}', encoding="utf-8")
    monkeypatch.setattr(command, "JobLock", _BusyLock)
    result = command.main(
        [
            "--db",
            str(tmp_path / "absent.db"),
            "--inventory",
            str(inventory),
            "--allow-unsealed-inventory",
            "--corpus-key",
            "issuer:ACME",
            "--revision",
            "1",
            "--selector-code-version",
            "test@1",
            "--recorded-at",
            "2026-07-27T00:00:00",
            "--apply",
        ]
    )
    assert result == 75
    assert "portfolio-db" in _BusyLock.write_sets
    assert any(item.startswith("sqlite:") for item in _BusyLock.write_sets)
    assert any(item.startswith("search-corpus:") for item in _BusyLock.write_sets)
    assert not (tmp_path / "absent.db").exists()
