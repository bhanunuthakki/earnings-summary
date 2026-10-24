"""Tests for src/log_redact.py — credential + PII masking before logging."""

from __future__ import annotations

import json

import pytest

from log_redact import redact, redact_payload, sanitize_operational_text


@pytest.mark.parametrize("path", ["bot", "file/bot"])
def test_telegram_url_path_credentials_masked(path: str) -> None:
    marker = "123456:SYNTHETIC-TELEGRAM-CREDENTIAL"
    output = redact(f"failed https://api.telegram.org/{path}{marker}/getFile?offset=2")
    assert marker not in output
    assert f"https://api.telegram.org/{path}" in output
    assert "/getFile?offset=2" in output


@pytest.mark.parametrize(
    "key",
    [
        "OPENROUTER_API_KEY",
        "fmp_apikey",
        "telegram_bot_token",
        "GOOGLE_CLIENT_SECRET",
        "SMTP_PASSWORD",
        "aws-secret-access-key",
    ],
)
def test_provider_prefixed_credentials_masked_in_all_diagnostic_forms(key: str) -> None:
    marker = 'SYNTHETIC-CREDENTIAL-"-WITH-ESCAPE'
    assignment = redact(f"{key} = {json.dumps(marker)}; next=visible")
    assert "SYNTHETIC-CREDENTIAL" not in assignment
    assert key in assignment
    assert "next=visible" in assignment

    output = json.loads(redact(json.dumps({key: marker, "status": "visible"})))
    assert marker not in output.values()
    assert key in output
    assert output["status"] == "visible"

    payload = redact_payload({"nested": [{key: marker, "count": 2}], "ok": True})
    assert isinstance(payload, dict)
    assert "SYNTHETIC-CREDENTIAL" not in json.dumps(payload)
    assert payload["ok"] is True
    assert key in json.dumps(payload)


def test_normal_diagnostic_strings_and_json_preserved() -> None:
    for raw in (
        "https://api.telegram.org/docs/bots",
        "provider_status=ready; monkey=banana",
        json.dumps({"provider_status": "ready", "monkey": "banana", "count": 2}),
    ):
        assert redact(raw) == raw


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "https://api.fmp.com/v3/quote/NU?apikey=SECRET123",
            "https://api.fmp.com/v3/quote/NU?apikey=***",
        ),
        ("https://x.com/p?api_key=ABC&page=2", "https://x.com/p?api_key=***&page=2"),
        ("https://x.com/p?access_token=ABC", "https://x.com/p?access_token=***"),
        ("https://x.com/p?auth_token=ABC", "https://x.com/p?auth_token=***"),
        ("https://x.com/p?password=hunter2", "https://x.com/p?password=***"),
        ("https://x.com/p?client_secret=ABC", "https://x.com/p?client_secret=***"),
        ("https://x.com/p?refresh_token=ABC", "https://x.com/p?refresh_token=***"),
        ("https://x.com/p?session_id=ABC", "https://x.com/p?session_id=***"),
        ("https://x.com/p?key=ABC", "https://x.com/p?key=***"),
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


def test_basic_word_not_overmatched() -> None:
    assert redact("a basic analysis remains readable") == "a basic analysis remains readable"


@pytest.mark.parametrize(
    "raw",
    [
        "Authorization: Basic dXNlcjpwYXNzd29yZA==",
        "https" + "://owner:" + "password" + "@example.test/private",
        "x-api-key: top-secret-value",
        "x_api_key=top-secret-value",
        "x api key = top-secret-value",
        "api-key: top-secret-value",
        "api key = top-secret-value",
    ],
)
def test_basic_and_flexible_api_key_forms_are_masked(raw: str) -> None:
    output = redact(raw)
    assert "password" not in output
    assert "top-secret-value" not in output
    assert "dXNlcjpwYXNzd29yZA==" not in output


@pytest.mark.parametrize(
    "raw",
    [
        "Authorization: " + "Basic " + "ALPHABETONLYCREDENTIAL",
        "Proxy-Authorization: " + "Basic " + "ALPHABETONLYCREDENTIAL",
        "x-api-key: prefix ALPHABETONLYCREDENTIAL suffix; next=visible",
        ("api" + "_key") + ' = "prefix ALPHABETONLYCREDENTIAL suffix"; next=visible',
        "password = prefix ALPHABETONLYCREDENTIAL suffix, next=visible",
    ],
)
def test_header_and_assignment_values_are_fully_masked(raw: str) -> None:
    output = redact(raw)

    assert "ALPHABETONLYCREDENTIAL" not in output
    if "next=visible" in raw:
        assert "next=visible" in output


def test_bearer_headers_and_standalone_b64token_are_fully_masked() -> None:
    credential = "ALPHABETONLY" + "~+TAILONLY/=="
    samples = (
        "Authorization: " + "Bearer " + credential + "\ntrace=safe",
        "Proxy-Authorization: " + "Bearer " + credential + "\ntrace=safe",
        "request failed Bearer " + credential + "; trace=safe",
    )

    for raw in samples:
        output = redact(raw)
        assert credential not in output
        assert "TAILONLY" not in output
        assert "trace=safe" in output


@pytest.mark.parametrize("delimiter", [")", "]", "}", '"', "'", ":", ";"])
def test_standalone_bearer_stops_at_any_non_b64token_delimiter(delimiter: str) -> None:
    credential = "ALPHABETONLY" + "~+TAILONLY/=="

    output = redact("request failed Bearer " + credential + delimiter + " suffix=safe")

    assert "TAILONLY" not in output
    assert delimiter + " suffix=safe" in output


@pytest.mark.parametrize(
    "key",
    ["api_key", "apikey", "token", "secret", "password", "access_token", "auth_token"],
)
def test_json_body_secrets_masked(key: str) -> None:
    out = redact(f'{{"{key}": "sk-supersecret-value"}}')
    assert "sk-supersecret-value" not in out
    assert f'"{key}": "***"' in out


def test_email_local_part_masked_domain_kept() -> None:
    assert (
        redact("contact owner@example.invalid for access")
        == "contact ***@example.invalid for access"
    )


def test_email_in_user_agent_header_masked() -> None:
    out = redact("User-Agent: earnings-summary (analyst@example.com)")
    assert "analyst" not in out
    assert "@example.com" in out


def test_non_string_input_coerced() -> None:
    # exceptions and other objects go through str() first
    assert (
        redact(ValueError("failed: https://x.com?apikey=KEY")) == "failed: https://x.com?apikey=***"
    )


def test_clean_text_unchanged() -> None:
    assert redact("nothing sensitive here, just NU Q1 2026 revenue") == (
        "nothing sensitive here, just NU Q1 2026 revenue"
    )
    assert redact("monkey=banana") == "monkey=banana"


def test_operational_text_persisted_mode_removes_urls_paths_credentials_and_bounds() -> None:
    sentinel = "PERSISTED-RAW-CREDENTIAL-7319"
    output = sanitize_operational_text(
        f"failed https://example.test/private?x-api-key={sentinel} "
        + r"C:\private\owner\job.py "
        + "x" * 500,
        mode="persisted",
    )
    assert sentinel not in output
    assert "https://example.test" not in output
    assert "C:\\private" not in output
    assert "[url]" in output
    assert "[path]" in output
    assert len(output) <= 240


def test_operational_text_presentation_mode_preserves_public_urls_only() -> None:
    sentinel = "PRESENTATION-RAW-CREDENTIAL-7319"
    output = sanitize_operational_text(
        f"https://example.test/public?api_key={sentinel}; "
        + "file:///C:/private/owner/job.py; "
        + r"C:\private\owner\job.py",
        mode="presentation",
    )
    assert sentinel not in output
    assert "https://example.test/public?api_key=***" in output
    assert "file://[path]" in output
    assert "C:\\private" not in output
    assert len(output) <= 240


@pytest.mark.parametrize(
    "raw",
    [
        "https://bucket.s3.amazonaws.com/x?X-Amz-Credential=AKIA...&X-Amz-Signature=deadbeef&X-Amz-Security-Token=aws-session",
        "https://account.blob.core.windows.net/c/x?sv=2024-01-01&sig=azure-signature&se=2026-01-01",
        "https://storage.googleapis.com/bucket/x?X-Goog-Credential=abc&X-Goog-Signature=gcs-signature&X-Goog-Security-Token=gcs-session",
    ],
)
def test_cloud_signed_url_credentials_masked(raw: str) -> None:
    out = redact(raw)
    assert "deadbeef" not in out
    assert "azure-signature" not in out
    assert "gcs-signature" not in out
    assert "aws-session" not in out
    assert "gcs-session" not in out
