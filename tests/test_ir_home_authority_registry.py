"""The legacy IR URL map is a projection of typed authority candidates."""

from __future__ import annotations

from ir_pipeline.home_authority_registry import (
    IR_HOME_AUTHORITY_CANDIDATES,
    candidate_for_ticker,
)
from ir_pipeline.ir_url_overrides import IR_URL_OVERRIDES, resolve_ir_url


def test_typed_candidates_are_unique_and_preserve_legacy_url_coverage() -> None:
    tickers = [candidate.ticker for candidate in IR_HOME_AUTHORITY_CANDIDATES]
    assert len(tickers) == len(set(tickers))
    assert len(IR_HOME_AUTHORITY_CANDIDATES) == 35
    assert len(IR_URL_OVERRIDES) == 36
    assert IR_URL_OVERRIDES["GOOG"] == IR_URL_OVERRIDES["GOOGL"]
    assert all(
        candidate.required_marker_groups
        and all(group for group in candidate.required_marker_groups)
        for candidate in IR_HOME_AUTHORITY_CANDIDATES
    )


def test_candidate_lookup_is_case_insensitive_and_closed() -> None:
    candidate = candidate_for_ticker("nu")
    assert candidate is not None
    assert candidate.ticker == "NU"
    alphabet_alias = candidate_for_ticker("googl")
    assert alphabet_alias is not None
    assert alphabet_alias.ticker == "GOOG"
    assert candidate_for_ticker("not-tracked") is None


def test_reviewed_result_centers_use_current_publisher_routes() -> None:
    meli = candidate_for_ticker("MELI")
    rubrik = candidate_for_ticker("RBRK")
    current_routes = {
        "NOW": "https://investor.servicenow.com/overview/default.aspx",
        "LLY": "https://investor.lilly.com/financial-information/quarterly-results",
        "TEM": "https://investors.tempus.com/financials/financial-information",
        "FIGR": "https://investors.figure.com/financial-information/quarterly-results",
        "BHP": "https://www.bhp.com/financial-results",
    }

    assert meli is not None
    assert meli.requested_url == "https://investor.mercadolibre.com/sec-filings"
    assert rubrik is not None
    assert rubrik.requested_url == "https://ir.rubrik.com/financials/quarterly-results/default.aspx"
    for ticker, expected_url in current_routes.items():
        candidate = candidate_for_ticker(ticker)
        assert candidate is not None
        assert candidate.requested_url == expected_url


def test_effective_meli_route_matches_reviewed_candidate_and_wins_precedence() -> None:
    meli = candidate_for_ticker("MELI")

    assert meli is not None
    assert IR_URL_OVERRIDES["MELI"] == meli.requested_url
    assert (
        resolve_ir_url(
            "meli",
            "https://database.example/investors",
            "https://config.example/investors",
        )
        == meli.requested_url
    )
