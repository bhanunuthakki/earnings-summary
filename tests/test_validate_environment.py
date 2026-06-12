"""Tests for execution/validate_environment.py."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


def test_all_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("FMP_API_KEY=testkey123\nFMP_TIER=basic\n", encoding="utf-8")
    monkeypatch.setattr("execution.validate_environment.ENV_FILE", env_file)

    fake_pw_api = type(sys)("playwright.sync_api")
    monkeypatch.setitem(sys.modules, "playwright", type(sys)("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_pw_api)

    from execution.validate_environment import check

    results = check()
    failed = [(n, d) for n, ok, d in results if not ok]
    assert not failed, f"expected all checks to pass: {failed}"


def test_missing_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("execution.validate_environment.ENV_FILE", tmp_path / "missing.env")
    from execution.validate_environment import check

    results = {name: ok for name, ok, _ in check()}
    assert results[".env present"] is False


def test_empty_fmp_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("FMP_API_KEY=\nFMP_TIER=basic\n", encoding="utf-8")
    monkeypatch.setattr("execution.validate_environment.ENV_FILE", env_file)
    # Remove any real/suite-leaked FMP_API_KEY so the empty file value isn't
    # overridden — several test modules `os.environ.setdefault` a placeholder
    # key at import, which reaches this test in a full-suite run (same hazard
    # test_invalid_fmp_tier already guards for FMP_TIER).
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    from execution.validate_environment import check

    results = {name: ok for name, ok, _ in check()}
    assert results["FMP_API_KEY set"] is False


def test_invalid_fmp_tier(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("FMP_API_KEY=key\nFMP_TIER=ultra\n", encoding="utf-8")
    monkeypatch.setattr("execution.validate_environment.ENV_FILE", env_file)
    # Remove real FMP_TIER from the process env so the fake file value isn't overridden.
    monkeypatch.delenv("FMP_TIER", raising=False)
    from execution.validate_environment import check

    results = {name: ok for name, ok, _ in check()}
    assert results["FMP_TIER valid"] is False


def test_absent_fmp_tier_defaults_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("FMP_API_KEY=key\n", encoding="utf-8")
    monkeypatch.setattr("execution.validate_environment.ENV_FILE", env_file)
    from execution.validate_environment import check

    results = {name: ok for name, ok, _ in check()}
    assert results["FMP_TIER valid"] is True


def test_playwright_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("FMP_API_KEY=key\nFMP_TIER=basic\n", encoding="utf-8")
    monkeypatch.setattr("execution.validate_environment.ENV_FILE", env_file)

    monkeypatch.delitem(sys.modules, "playwright", raising=False)
    monkeypatch.delitem(sys.modules, "playwright.sync_api", raising=False)

    original_import = importlib.import_module

    def _blocked_import(name: str, *args: str | None, **kwargs: object) -> object:
        if name == "playwright.sync_api":
            raise ImportError("No module named 'playwright'")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", _blocked_import)

    from execution.validate_environment import check

    results = {name: ok for name, ok, _ in check()}
    assert results["playwright importable"] is False


def test_main_exit_0_all_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("FMP_API_KEY=k\nFMP_TIER=premium\n", encoding="utf-8")
    monkeypatch.setattr("execution.validate_environment.ENV_FILE", env_file)

    monkeypatch.setitem(sys.modules, "playwright", type(sys)("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", type(sys)("playwright.sync_api"))

    from execution.validate_environment import main

    assert main(["--quiet"]) == 0


def test_main_exit_1_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("execution.validate_environment.ENV_FILE", tmp_path / "no.env")
    from execution.validate_environment import main

    assert main(["--quiet"]) == 1
