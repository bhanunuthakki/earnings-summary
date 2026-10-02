"""SEC contact identity reads only the configured header, without env injection."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sec_identity import DEFAULT_USER_AGENT, SecContactConfigurationError, sec_user_agent


def test_external_contact_is_read_per_call_without_importing_other_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / "runtime.env"
    other_key = "FMP_API_" + "KEY"
    other_value = "synthetic-other-provider-value"
    env_file.write_text(
        f'EDGAR_USER_AGENT="research first@example.test"\n{other_key}={other_value}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("EARNINGS_SUMMARY_ENV_FILE", str(env_file))
    monkeypatch.delenv("EDGAR_USER_AGENT", raising=False)
    monkeypatch.delenv(other_key, raising=False)

    assert sec_user_agent() == "research first@example.test"
    assert "EDGAR_USER_AGENT" not in os.environ
    assert other_key not in os.environ

    env_file.write_text(
        f'EDGAR_USER_AGENT="research next@example.test"\n{other_key}={other_value}\n',
        encoding="utf-8",
    )
    assert sec_user_agent() == "research next@example.test"
    assert other_key not in os.environ


def test_explicit_contact_precedes_external_file_and_whitespace_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / "runtime.env"
    env_file.write_text('EDGAR_USER_AGENT="research file@example.test"\n', encoding="utf-8")
    monkeypatch.setenv("EARNINGS_SUMMARY_ENV_FILE", str(env_file))
    monkeypatch.setenv("EDGAR_USER_AGENT", "  research explicit@example.test  ")
    assert sec_user_agent() == "research explicit@example.test"

    monkeypatch.setenv("EDGAR_USER_AGENT", "  ")
    assert sec_user_agent() == "research file@example.test"


def test_public_default_only_when_contact_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EARNINGS_SUMMARY_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.delenv("EDGAR_USER_AGENT", raising=False)
    assert sec_user_agent() == DEFAULT_USER_AGENT

    env_file = tmp_path / "runtime.env"
    env_file.write_text("EDGAR_USER_AGENT=  \n", encoding="utf-8")
    monkeypatch.setenv("EARNINGS_SUMMARY_ENV_FILE", str(env_file))
    assert sec_user_agent() == DEFAULT_USER_AGENT


@pytest.mark.parametrize(
    ("source", "contact"),
    [
        ("process", "research\r\nbad@example.test"),
        ("process", "research 😀@example.test"),
        ("file", "research\nbad@example.test"),
        ("file", "research 😀@example.test"),
    ],
)
def test_invalid_selected_contact_fails_opaque_without_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str, contact: str
) -> None:
    env_file = tmp_path / "runtime.env"
    env_file.write_text('EDGAR_USER_AGENT="research valid@example.test"\n', encoding="utf-8")
    monkeypatch.setenv("EARNINGS_SUMMARY_ENV_FILE", str(env_file))
    if source == "process":
        monkeypatch.setenv("EDGAR_USER_AGENT", contact)
    else:
        monkeypatch.delenv("EDGAR_USER_AGENT", raising=False)
        env_file.write_text(f'EDGAR_USER_AGENT="{contact}"\n', encoding="utf-8")

    with pytest.raises(SecContactConfigurationError) as raised:
        sec_user_agent()
    assert contact not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
