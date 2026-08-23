"""MELI's publisher-embedded quarterly-results discovery contract."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import cast

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from ir_pipeline.discover.meli import (  # noqa: E402
    MeliEmbeddedDiscoveryError,
    discover_embedded_quarterly_inventory,
)


def _page(items: list[dict[str, object]]) -> str:
    """Match the Nordic context's JSON assignment without real publisher values."""
    payload = {"page": {"quarterlyResults": {"items": items}}}
    return f'<script id="__NORDIC_RENDERING_CTX__">_n.ctx.r={json.dumps(payload)};_n.ctx.r.assets={{}};</script>'


def _q2_items(*, first_label: str = "Letter to Shareholders") -> list[dict[str, object]]:
    return [
        {
            "id": "q2-id",
            "title": "Results Q2'26",
            "links": [
                {"label": first_label, "href": "https://files.example.test/q2-letter.pdf"},
                {
                    "label": "Earnings Presentation",
                    "href": "https://files.example.test/q2-deck.pdf",
                },
                {"label": "SEC Filing", "href": "https://files.example.test/q2-10q.pdf"},
                {
                    "label": "Webcast Transcript",
                    "href": "https://files.example.test/q2-transcript.pdf",
                },
            ],
        }
    ]


def test_builds_complete_typed_q2_inventory_from_the_embedded_publisher_state() -> None:
    inventory = discover_embedded_quarterly_inventory(
        _page(_q2_items()),
        source_page="https://investor.example.test/sec-filings",
        fiscal_year=2026,
        fiscal_quarter=2,
    )

    assert inventory.period_end.isoformat() == "2026-06-30"
    assert [(item.document_type, item.source_url) for item in inventory.documents] == [
        ("ir_investor_update", "https://files.example.test/q2-letter.pdf"),
        ("ir_presentation", "https://files.example.test/q2-deck.pdf"),
        ("ir_transcript", "https://files.example.test/q2-transcript.pdf"),
        ("sec_10q", "https://files.example.test/q2-10q.pdf"),
    ]


def test_accepts_current_shaped_publisher_fields_that_are_outside_the_closed_inventory() -> None:
    """Vendor display metadata must not expand the persisted inventory contract."""
    items = _q2_items()
    result = items[0]
    result["publishedAt"] = "2026-08-04T20:00:00Z"
    links = cast(list[dict[str, object]], result["links"])
    links[0]["target"] = "_blank"
    payload = {
        "page": {
            "quarterlyResults": {
                "items": items,
                "component": "quarterly-results",
                "displayOrder": "descending",
            }
        },
        "site": {"locale": "en-US"},
    }
    rendered_html = (
        f'<script id="__NORDIC_RENDERING_CTX__">_n.ctx.r={json.dumps(payload)};'
        "_n.ctx.r.assets={};</script>"
    )

    inventory = discover_embedded_quarterly_inventory(
        rendered_html,
        source_page="https://investor.example.test/sec-filings",
        fiscal_year=2026,
        fiscal_quarter=2,
    )

    assert inventory.result_id == "q2-id"
    assert [document.label for document in inventory.documents] == [
        "Letter to Shareholders",
        "Earnings Presentation",
        "Webcast Transcript",
        "SEC Filing",
    ]


def test_rejects_multiple_publisher_contexts_instead_of_using_the_first() -> None:
    first_context = _page(_q2_items())
    second_items = _q2_items()
    second_items[0]["id"] = "contradictory-q2-id"

    with pytest.raises(MeliEmbeddedDiscoveryError, match="nordic_context_ambiguous"):
        discover_embedded_quarterly_inventory(
            first_context + _page(second_items),
            source_page="https://investor.example.test/sec-filings",
            fiscal_year=2026,
            fiscal_quarter=2,
        )


def test_rejects_an_unmapped_publisher_document_label_instead_of_dropping_it() -> None:
    with pytest.raises(MeliEmbeddedDiscoveryError, match="unknown_document_label"):
        discover_embedded_quarterly_inventory(
            _page(_q2_items(first_label="Quarterly Letter")),
            source_page="https://investor.example.test/sec-filings",
            fiscal_year=2026,
            fiscal_quarter=2,
        )


def test_rejects_missing_target_period_instead_of_guessing_a_nearby_quarter() -> None:
    with pytest.raises(MeliEmbeddedDiscoveryError, match="requested_period_missing"):
        discover_embedded_quarterly_inventory(
            _page(
                [
                    {
                        "id": "q1-id",
                        "title": "Results Q1'26",
                        "links": _q2_items()[0]["links"],
                    }
                ]
            ),
            source_page="https://investor.example.test/sec-filings",
            fiscal_year=2026,
            fiscal_quarter=2,
        )
