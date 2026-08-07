"""Unit tests for Phase A0 security audit fixes (H1-H4, M1-M5, LOW)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from chat_session import _chat_path
from research.artifact import ArtifactFetchError, fetch_url_text, is_safe_url
from server_runtime.access import is_allowed_origin
from ui.cite_marks import linkify


def test_h1_ssrf_url_guard():
    assert is_safe_url("https://www.google.com") is True
    assert is_safe_url("http://127.0.0.1") is False
    assert is_safe_url("http://localhost/secret") is False
    assert is_safe_url("http://169.254.169.254/latest/meta-data/") is False

    with pytest.raises(ArtifactFetchError):
        fetch_url_text("http://127.0.0.1:8080/admin")


def test_h2_chat_session_path_traversal(tmp_path: Path):
    with pytest.raises(ValueError, match="invalid ticker"):
        _chat_path(tmp_path, "..\\..\\etc\\passwd", date(2026, 8, 3))


def test_m3_cors_null_origin():
    # Null origin without whitelist returns null
    assert is_allowed_origin("null", allow_tailscale=False, whitelist=()) == "null"
    # Invalid origins return None
    assert is_allowed_origin("http://evil.com", allow_tailscale=False, whitelist=()) is None


def test_m4_javascript_scheme_xss():
    # JS linkify test
    items = [{"n": 1, "href": "javascript:alert(1)", "label": "evil"}]
    rendered = linkify("Prose [1]", items)
    assert "javascript:" not in rendered
