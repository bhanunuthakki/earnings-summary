"""Tests for exact Nordic-rendering publisher document-link verification."""

from __future__ import annotations

import hashlib
import json
from urllib.parse import urlunsplit

import pytest
from pydantic import ValidationError

from provenance.publisher_document_link import (
    PublisherDocumentLink,
    verify_publisher_document_link,
)
from runtime.deterministic import content_sha256_ascii

URL = "https://publisher.example/reports/q2-2026.pdf"
LINK = {
    "href": URL,
    "label": "Q2 2026 report",
    "id": "report-2026-q2",
    "target": "_blank",
}
CONTEXT = {"documents": {"quarterly": [LINK]}}


def page(context: object = CONTEXT, *, trailing: str = "") -> bytes:
    encoded = json.dumps(context, separators=(",", ":"))
    return (
        '<html><script id="__NORDIC_RENDERING_CTX__">'
        + "_n.ctx.r="
        + encoded
        + trailing
        + "</script></html>"
    ).encode()


def proof(
    raw: bytes | None = None,
    *,
    path: tuple[str | int, ...] = ("documents", "quarterly", 0),
    selected: object = LINK,
    link_hash: str | None = None,
) -> PublisherDocumentLink:
    retained = raw if raw is not None else page()
    return PublisherDocumentLink(
        publisher_page_observation_id="publisher-page-observation:abc123",
        publisher_page_sha256=hashlib.sha256(retained).hexdigest(),
        json_path=path,
        link_sha256=link_hash or content_sha256_ascii(selected),
    )


def test_verifies_exact_link_and_ignores_trailing_javascript() -> None:
    raw = page(trailing="; window.executed = true;")
    verify_publisher_document_link(raw, proof(raw), URL)


def test_rejects_wrong_retained_blob() -> None:
    raw = page()
    with pytest.raises(ValueError, match="publisher_document_link_page_sha256_mismatch"):
        verify_publisher_document_link(raw + b" ", proof(raw), URL)


def test_rejects_wrong_target_url() -> None:
    with pytest.raises(ValueError, match="publisher_document_link_href_mismatch"):
        verify_publisher_document_link(page(), proof(), "https://publisher.example/other.pdf")


def test_rejects_invalid_json_path() -> None:
    with pytest.raises(ValueError, match="publisher_document_link_path_invalid"):
        verify_publisher_document_link(page(), proof(path=("documents", "quarterly", 3)), URL)


def test_rejects_wrong_selected_link_hash() -> None:
    wrong_hash = "0" * 64
    with pytest.raises(ValueError, match="publisher_document_link_sha256_mismatch"):
        verify_publisher_document_link(page(), proof(link_hash=wrong_hash), URL)


def test_rejects_duplicate_context_scripts() -> None:
    raw = page() + page()
    with pytest.raises(ValueError, match="publisher_document_link_script_missing_or_duplicate"):
        verify_publisher_document_link(raw, proof(raw), URL)


def test_rejects_malformed_json() -> None:
    raw = b'<script id="__NORDIC_RENDERING_CTX__">_n.ctx.r={"documents":</script>'
    with pytest.raises(ValueError, match="publisher_document_link_context_json_invalid"):
        verify_publisher_document_link(raw, proof(raw), URL)


def test_rejects_javascript_expression_instead_of_json() -> None:
    raw = b'<script id="__NORDIC_RENDERING_CTX__">_n.ctx.r=(function(){return {};})()</script>'
    with pytest.raises(ValueError, match="publisher_document_link_context_json_invalid"):
        verify_publisher_document_link(raw, proof(raw), URL)


@pytest.mark.parametrize(
    "path",
    [(), ("",), (-1,), (True,), ("x",) * 33],
)
def test_rejects_invalid_proof_paths(path: tuple[str | int, ...]) -> None:
    with pytest.raises(ValidationError):
        PublisherDocumentLink(
            publisher_page_observation_id="obs",
            publisher_page_sha256="a" * 64,
            json_path=path,
            link_sha256="b" * 64,
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://publisher.example/file.pdf",
        urlunsplit(("https", "user:pass@publisher.example", "/file.pdf", "", "")),
    ],
)
def test_rejects_non_https_or_credentialed_expected_url(url: str) -> None:
    with pytest.raises(ValueError, match="publisher_document_link_expected_url_invalid"):
        verify_publisher_document_link(page(), proof(), url)
