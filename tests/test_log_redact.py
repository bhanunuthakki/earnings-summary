"""Tests for src/log_redact.py — credential + PII masking before logging."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from log_redact import redact  # noqa: E402


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://api.fmp.com/v3/quote/NU?apikey=SECRET123", "https://api.fmp.com/v3/quote/NU?apikey=***"),
        ("https://x.com/p?api_key=ABC&page=2", "https://x.com/p?api_key=***&page=2"),
        ("https://x.com/p?access_token=ABC", "https://x.com/p?access_token=***"),
        ("https://x.com/p?auth_token=ABC", "https://x.com/p?auth_token=***"),
        ("https://x.com/p?password=hunter2", "https://x.com/p?password=***"),
    ],
)
def test_url_query_params_masked(raw: str, expected: str) -> None:
    assert redact(raw) == expected


def test_page_value_preserved() -> None:
    # only the credential is masked, surrounding params survive
    assert "page=2" in redact("https://x.com/p?api_key=ABC&page=2")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9", "Authorization: Bearer ***"),
        ("bearer abcdef1234567890", "bearer ***"),
    ],
)
def test_bearer_tokens_masked(raw: str, expected: str) -> None:
    assert redact(raw) == expected


def test_bearer_word_not_overmatched() -> None:
    # the English word "bearer" + a short word must NOT be masked
    assert redact("the bearer of the news") == "the bearer of the news"


@pytest.mark.parametrize(
    "key",
    ["api_key", "apikey", "token", "secret", "password", "access_token", "auth_token"],
)
def test_json_body_secrets_masked(key: str) -> None:
    out = redact(f'{{"{key}": "sk-supersecret-value"}}')
    assert "sk-supersecret-value" not in out
    assert f'"{key}": "***"' in out


def test_email_local_part_masked_domain_kept() -> None:
    assert redact("contact bhanu@gmail.com for access") == "contact ***@gmail.com for access"


def test_email_in_user_agent_header_masked() -> None:
    out = redact("User-Agent: earnings-summary (analyst@example.com)")
    assert "analyst" not in out
    assert "@example.com" in out


def test_non_string_input_coerced() -> None:
    # exceptions and other objects go through str() first
    assert redact(ValueError("failed: https://x.com?apikey=KEY")) == "failed: https://x.com?apikey=***"


def test_clean_text_unchanged() -> None:
    assert redact("nothing sensitive here, just NU Q1 2026 revenue") == (
        "nothing sensitive here, just NU Q1 2026 revenue"
    )
