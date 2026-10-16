"""Service startup cannot turn a state-root shadow into database authority."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

import db
from db_paths import db_path_context


def _no_environment(_root: Path) -> bool:
    return False


@pytest.mark.parametrize("entrypoint", ["comments_server", "execution.capture_poller"])
@pytest.mark.parametrize("configured", [None, "", "   "])
def test_service_refuses_shadow_and_prior_binding_without_configured_authority(
    entrypoint: str,
    configured: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module(entrypoint)
    state_root = tmp_path / "state"
    shadow = state_root / "data" / "portfolio.db"
    shadow.parent.mkdir(parents=True)
    shadow.write_bytes(b"unapproved shadow")
    prior = tmp_path / "prior.db"
    prior.touch()
    monkeypatch.setattr(db, "DB_PATH", prior)
    monkeypatch.setattr(module, "load_project_env", _no_environment)
    if configured is None:
        monkeypatch.delenv("EARNINGS_SUMMARY_DB_PATH", raising=False)
    else:
        monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", configured)

    with db_path_context(prior), pytest.raises(RuntimeError, match="configured"):
        module.configure_runtime_db(state_root)

    assert Path(db.DB_PATH) == prior
    assert shadow.read_bytes() == b"unapproved shadow"


@pytest.mark.parametrize("entrypoint", ["comments_server", "execution.capture_poller"])
def test_service_binds_explicit_env_authority_after_environment_load(
    entrypoint: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = importlib.import_module(entrypoint)
    state_root = tmp_path / "state"
    canonical = tmp_path / "approved.db"
    canonical.touch()
    for field in ("DB_PATH", "DATA_DIR", "FMP_DIR", "STATE_ROOT"):
        monkeypatch.setattr(db, field, getattr(db, field))
    monkeypatch.delenv("EARNINGS_SUMMARY_DB_PATH", raising=False)

    def load_environment(_root: Path) -> bool:
        assert _root == state_root
        monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(canonical))
        return True

    monkeypatch.setattr(module, "load_project_env", load_environment)

    assert module.configure_runtime_db(state_root) == canonical
    assert Path(db.DB_PATH) == canonical
    assert Path(db.STATE_ROOT) == state_root
    assert not (state_root / "data" / "portfolio.db").exists()


@pytest.mark.parametrize("entrypoint", ["comments_server", "execution.capture_poller"])
def test_service_refuses_unavailable_configured_database(
    entrypoint: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = importlib.import_module(entrypoint)
    missing = tmp_path / "missing.db"
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(missing))
    monkeypatch.setattr(module, "load_project_env", _no_environment)

    with pytest.raises(FileNotFoundError, match="unavailable"):
        module.configure_runtime_db(tmp_path)

    assert not missing.exists()
