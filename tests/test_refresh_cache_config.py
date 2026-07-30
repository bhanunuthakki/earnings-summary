from __future__ import annotations

import sys
from pathlib import Path

import pytest

import execution.refresh_cache as refresh_cache


def test_fmp_auth_accepts_process_environment_without_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    process_value = "-".join(("process", "only", "canary"))
    monkeypatch.setenv("FMP_API_KEY", process_value)

    config = refresh_cache.load_fmp_auth(env_file=tmp_path / "missing.env")

    assert config.api_key == process_value
    assert config.source == "environment"
    assert process_value not in repr(config)


def test_fmp_auth_falls_back_to_project_dotenv(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    dotenv_value = "-".join(("dotenv", "only", "canary"))
    env_file.write_text(f"FMP_API_KEY={dotenv_value}\n", encoding="utf-8")

    config = refresh_cache.load_fmp_auth(environ={}, env_file=env_file)

    assert config.api_key == dotenv_value
    assert config.source == "project_dotenv"
    assert dotenv_value not in repr(config)


def test_fmp_auth_prefers_process_environment_over_project_dotenv(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    dotenv_value = "-".join(("dotenv", "canary"))
    process_value = "-".join(("process", "canary"))
    env_file.write_text(f"FMP_API_KEY={dotenv_value}\n", encoding="utf-8")

    config = refresh_cache.load_fmp_auth(
        environ={"FMP_API_KEY": process_value},
        env_file=env_file,
    )

    assert config.api_key == process_value
    assert config.source == "environment"
    assert process_value not in repr(config)
    assert dotenv_value not in repr(config)


def test_run_with_missing_fmp_auth_fails_loud_before_dispatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    monkeypatch.setattr(refresh_cache, "ENV_FILE", tmp_path / "missing.env")
    monkeypatch.setattr(sys, "argv", ["refresh_cache.py", "run"])
    dispatched = False

    def unexpected_dispatch(_args: object) -> int:
        nonlocal dispatched
        dispatched = True
        return 0

    monkeypatch.setattr(refresh_cache, "cmd_run", unexpected_dispatch)

    exit_code = refresh_cache.main()
    captured = capsys.readouterr()

    assert exit_code != 0
    assert not dispatched
    assert "FMP_API_KEY" in captured.err
    assert "missing.env" not in captured.err
    assert "process-only-canary" not in captured.err
