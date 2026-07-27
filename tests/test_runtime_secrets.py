from __future__ import annotations

import os
from pathlib import Path

import pytest

from integrations import gsheets
from runtime.secrets import (
    create_secret_text,
    secret_read_path,
    secret_write_path,
    write_private_text,
    write_secret_text,
)
from server_runtime.access import ReportCapabilityStore


@pytest.fixture
def external_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "external"
    monkeypatch.setenv("EARNINGS_SUMMARY_SECRETS_DIR", str(target))
    monkeypatch.delenv("COMMENTS_SERVER_REPORT_CAPABILITY", raising=False)
    return target


def test_external_secret_wins_over_legacy(tmp_path: Path, external_secrets: Path) -> None:
    repo = tmp_path / "repo"
    legacy = repo / "data" / "secrets" / "value"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("legacy", encoding="utf-8")
    external_secrets.mkdir()
    external = external_secrets / "value"
    external.write_text("external", encoding="utf-8")

    assert secret_read_path("value", repo_root=repo) == external.resolve()


def test_json_sibling_legacy_is_a_read_only_fallback(
    tmp_path: Path, external_secrets: Path
) -> None:
    repo = tmp_path / "repo"
    legacy = repo / "data" / "secrets" / "telegram_bot_token.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"token":"legacy"}', encoding="utf-8")

    assert secret_read_path("telegram_bot_token", repo_root=repo) == legacy.with_suffix("")
    assert secret_write_path("telegram_bot_token") == external_secrets / "telegram_bot_token"


def test_secret_write_is_external_and_atomic(
    external_secrets: Path,
) -> None:
    path = secret_write_path("token")
    write_secret_text(path, "first")
    write_secret_text(path, "second")

    assert path.read_text(encoding="utf-8") == "second"
    assert list(external_secrets.glob(".token.*")) == []
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
        assert external_secrets.stat().st_mode & 0o777 == 0o700


def test_create_secret_is_exclusive(external_secrets: Path) -> None:
    path = secret_write_path("token")
    assert create_secret_text(path, "first") is True
    assert create_secret_text(path, "second") is False
    assert path.read_text(encoding="utf-8") == "first"


def test_report_capability_migrates_away_from_empty_legacy(
    tmp_path: Path, external_secrets: Path
) -> None:
    repo = tmp_path / "repo"
    legacy = repo / "data" / "secrets" / "report_capability"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("  ", encoding="utf-8")

    store = ReportCapabilityStore(repo)
    capability = store.load_or_create()

    assert capability
    assert store.matches(capability)
    assert (external_secrets / "report_capability").read_text(encoding="utf-8") == capability
    assert legacy.read_text(encoding="utf-8") == "  "


def test_gsheets_token_reads_legacy_but_writes_external(
    tmp_path: Path, external_secrets: Path
) -> None:
    repo = tmp_path / "repo"
    legacy = repo / "data" / "secrets" / "gsheets_token.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("legacy", encoding="utf-8")

    assert gsheets.existing_token_path(repo) == legacy
    assert gsheets.default_token_path(repo) == external_secrets / "gsheets_token.json"


def test_explicit_private_path_supports_secure_atomic_refresh(tmp_path: Path) -> None:
    custom = tmp_path / "custom-auth" / "token.json"
    write_private_text(custom, "old")
    write_private_text(custom, "refreshed")
    assert custom.read_text(encoding="utf-8") == "refreshed"
