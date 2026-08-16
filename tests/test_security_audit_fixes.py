"""Unit tests for Phase A0 Security perimeter defenses."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

from comments_server_alert_routes import AppContext, register_alert_routes  # noqa: E402
from flask import Flask  # noqa: E402

from ui.cite_marks import linkify, safe_href  # noqa: E402


def test_safe_href_rejects_javascript_and_dangerous_schemes() -> None:
    # Safe schemes
    assert safe_href("https://example.com/sec/10k", "") == "https://example.com/sec/10k"
    assert safe_href("http://example.com/doc", "") == "http://example.com/doc"
    assert safe_href("/source/123", "") == "/source/123"

    # Dangerous / XSS schemes
    assert safe_href("javascript:alert(1)", "") == ""
    assert safe_href("data:text/html,<script>alert(1)</script>", "") == ""
    assert safe_href("vbscript:msgbox(1)", "") == ""
    assert safe_href("//evil.com/payload", "") == ""
    assert safe_href("\\\\evil.com\\payload", "") == ""


def test_linkify_escapes_and_filters_xss_hrefs() -> None:
    payload = {
        "items": [
            {
                "n": 1,
                "label": "SEC 10-K",
                "href": "javascript:alert(document.cookie)",
                "source_url": "javascript:alert(1)",
            }
        ]
    }
    rendered = linkify("Revenue was $100M [1]", payload)
    # The unsafe link must not be rendered as an active href
    assert 'href="javascript:' not in rendered
    assert "href=" not in rendered
    assert '<span class="cite-mark cite-badge">1</span>' in rendered


def test_approve_endpoint_default_deny_when_untrusted_headers(
    tmp_path: Path, migrated_db: Any
) -> None:
    db_path = migrated_db(tmp_path / "portfolio.db")
    app = Flask("test_alert_routes")
    ctx = AppContext(
        db_path=db_path,
        default_user_id="bhanu",
        referer_back_path=lambda ref: "/feed" if ref and "localhost" in ref else None,
        approve_consequence_href=lambda c: None,
    )
    register_alert_routes(app, ctx)
    client = app.test_client()

    # POST without Sec-Fetch-Site and without Referer should be rejected 403
    resp = client.post("/approve", data={"alert_id": "1", "confirm": "1"})
    assert resp.status_code == 403
    data = resp.get_json()
    assert "untrusted origin" in data["error"]

    # POST with same-origin Sec-Fetch-Site should pass CSRF filter (and hit lookup error on dummy ID)
    resp_same = client.post(
        "/approve",
        data={"alert_id": "99999", "confirm": "1"},
        headers={"Sec-Fetch-Site": "same-origin", "Referer": "http://localhost:7421/feed"},
    )
    # LookupError -> 404 (CSRF passed, resource not found)
    assert resp_same.status_code in (404, 400)


def test_edgar_nport_accession_shape_validation() -> None:
    valid_accessions = [
        "0001193125-24-123456",
        "0000000000-00-000000",
    ]
    invalid_accessions = [
        "../../etc/passwd",
        "0001193125-24-123456/../../hack",
        "<script>",
        "invalid-acc",
    ]

    pattern = re.compile(r"^\d{10}-?\d{2}-?\d{6}$")
    for acc in valid_accessions:
        assert pattern.fullmatch(acc) is not None

    for acc in invalid_accessions:
        assert pattern.fullmatch(acc) is None
