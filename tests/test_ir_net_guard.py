"""Tests for the IR outbound-fetch URL guard (src/ir_pipeline/_net.py) and the
Content-Disposition filename hardening in src/ir_pipeline/download.py.

The discovery crawler dereferences raw ``<a href>`` values harvested off an
externally controlled issuer page, so a crafted link must not be able to read a
local file (``file://``) or reach an internal host, and a server-supplied
filename must not steer the write out of the destination directory.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from ir_pipeline._net import UnsafeURLError, ensure_safe_public_url  # noqa: E402
from ir_pipeline.download import _filename_from_content_disposition  # noqa: E402


@pytest.mark.parametrize(
    "url",
    [
        "https://s201.q4cdn.com/files/doc.xlsx",
        "http://investors.example.com/q1.pdf",
        "https://example.com:8443/files/historical.xlsx",
    ],
)
def test_public_urls_pass_through(url: str) -> None:
    assert ensure_safe_public_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "file:///C:/Users/bhanu/.env",
        "file:///etc/passwd",
        "ftp://example.com/secret.xlsx",
        "data:text/plain,hello",
        "gopher://example.com/",
    ],
)
def test_non_http_schemes_blocked(url: str) -> None:
    with pytest.raises(UnsafeURLError):
        ensure_safe_public_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/files/x.pdf",
        "http://127.0.0.1:8000/files/x.pdf",
        "http://[::1]/x.pdf",
        "http://10.0.0.5/internal.xlsx",
        "http://192.168.1.10/internal.xlsx",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata pivot
        "http://172.16.5.5/x.pdf",
    ],
)
def test_internal_hosts_blocked(url: str) -> None:
    with pytest.raises(UnsafeURLError):
        ensure_safe_public_url(url)


def test_missing_host_blocked() -> None:
    with pytest.raises(UnsafeURLError):
        ensure_safe_public_url("http:///no-host")


def test_redirect_to_private_target_is_blocked() -> None:
    from ir_pipeline._net import safe_redirect_url

    with pytest.raises(UnsafeURLError):
        safe_redirect_url("https://issuer.example/a.pdf", "http://127.0.0.1/admin")


def test_relative_redirect_is_resolved_and_allowed() -> None:
    from ir_pipeline._net import safe_redirect_url

    assert safe_redirect_url("https://issuer.example/a.pdf", "/documents/q1.pdf") == (
        "https://issuer.example/documents/q1.pdf"
    )


@pytest.mark.parametrize(
    ("cd", "expected"),
    [
        ('attachment; filename="Nu Historical 1Q26.xlsx"', "Nu Historical 1Q26.xlsx"),
        # path separators in the advertised name must collapse to the leaf only
        ('attachment; filename="../../evil.xlsx"', "evil.xlsx"),
        ('attachment; filename="..\\..\\evil.xlsx"', "evil.xlsx"),
        ('attachment; filename="/abs/path/report.pdf"', "report.pdf"),
    ],
)
def test_content_disposition_keeps_leaf_name_only(cd: str, expected: str) -> None:
    assert _filename_from_content_disposition(cd, "fallback.xlsx") == expected


@pytest.mark.parametrize("cd", ["", "attachment", 'attachment; filename=".."'])
def test_content_disposition_falls_back(cd: str) -> None:
    assert _filename_from_content_disposition(cd, "fallback.xlsx") == "fallback.xlsx"
