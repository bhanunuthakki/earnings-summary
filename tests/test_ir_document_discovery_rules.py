"""IR document formats and cross-host publisher authorization."""

from __future__ import annotations

from ir_pipeline.authority import PublisherEndpointRule
from ir_pipeline.discover._docmeta import classify
from ir_pipeline.discover.generic import discover_document_inventory


def _render(_url: str, _timeout_ms: int) -> list[tuple[str, str]]:
    return [
        ("https://ir.x/news/q4-results", "Q4 2025 Earnings Release"),
        ("https://ir.x/q4-deck.pptx", "Q4 2025 Presentation"),
        ("https://ir.x/q4-deck.ppt", "Q4 2025 Presentation"),
        ("https://ir.x/q4-model.xls", "Q4 2025 Financial Workbook"),
        ("https://ir.x/q4-model.xlsx", "Q4 2025 Financial Workbook"),
        ("https://assets.publisher-cdn.test/q4.pdf", "Q4 2025 Results"),
    ]


def test_recognizes_html_presentation_spreadsheet_and_pdf_formats() -> None:
    inventory = discover_document_inventory(
        ir_url="https://ir.x/",
        render=_render,
        fetch_filename=lambda _url: "",
        rate_limit_s=0,
        check_robots=False,
        publisher_file_rules=(
            PublisherEndpointRule(
                host="assets.publisher-cdn.test",
                path_prefix="/",
            ),
        ),
    )
    assert {item.url for item in inventory.candidates} == {
        "https://ir.x/news/q4-results",
        "https://ir.x/q4-deck.pptx",
        "https://ir.x/q4-deck.ppt",
        "https://ir.x/q4-model.xls",
        "https://ir.x/q4-model.xlsx",
        "https://assets.publisher-cdn.test/q4.pdf",
    }


def test_cross_host_document_requires_explicit_publisher_endpoint_rule() -> None:
    inventory = discover_document_inventory(
        ir_url="https://ir.x/",
        render=lambda _url, _timeout: [
            ("https://assets.publisher-cdn.test/q4.pdf", "Q4 2025 Results")
        ],
        fetch_filename=lambda _url: "",
        rate_limit_s=0,
        check_robots=False,
    )
    assert inventory.candidates == ()


def test_publisher_endpoint_rule_rejects_normalized_path_escape() -> None:
    rule = PublisherEndpointRule(
        host="assets.publisher-cdn.test",
        path_prefix="/reports/",
    )
    assert rule.allows("https://assets.publisher-cdn.test/reports/q4.pdf")
    assert not rule.allows("https://assets.publisher-cdn.test/reports/../private/q4.pdf")


def test_financial_results_reporting_page_is_a_press_release() -> None:
    assert (
        classify("Rubrik Reports Fourth Quarter and Fiscal Year 2026 Financial Results")
        == "press_release"
    )


def test_generic_results_and_url_only_hints_are_not_press_releases() -> None:
    assert classify("Rubrik Reports Fourth Quarter Results") is None
    assert classify("financial-results-q4-2026") is None
