"""Historical intake changes an exact requested period, never global policy."""

from datetime import date

import pytest
from pydantic import ValidationError

from pipeline.managed_ir_sources import (
    HistoricalIssuerDocumentWindow,
    IssuerDocumentStagingRequest,
    issuer_request_period_is_allowed,
)
from tests.test_managed_ir_sources import request_fixture


def _historical() -> IssuerDocumentStagingRequest:
    base = request_fixture()
    inventory = base.inventory_request.model_copy(
        update={
            "fiscal_year": 2024,
            "period_end": date(2024, 6, 30),
        }
    )
    return IssuerDocumentStagingRequest(
        schema_version="issuer_document_staging_request.v2",
        attempt_id=base.attempt_id,
        inventory_request=inventory,
        inventory_request_sha256=inventory.request_sha256,
        historical_window=HistoricalIssuerDocumentWindow(
            period_ends=(date(2024, 6, 30), date(2026, 6, 30)),
            reviewed_by="synthetic-owner",
            review_reference_sha256="a" * 64,
        ),
    )


def test_exact_historical_period_is_allowed_and_default_is_unchanged() -> None:
    request = _historical()
    assert issuer_request_period_is_allowed(
        request, fiscal_year_end_month=12, as_of=date(2026, 10, 3)
    )
    legacy = IssuerDocumentStagingRequest(
        attempt_id=request.attempt_id,
        inventory_request=request.inventory_request,
        inventory_request_sha256=request.inventory_request_sha256,
    )
    assert not issuer_request_period_is_allowed(
        legacy, fiscal_year_end_month=12, as_of=date(2026, 10, 3)
    )
    assert "historical_window" not in legacy.model_dump(mode="json")


def test_future_or_unknown_calendar_remains_unavailable() -> None:
    request = _historical()
    assert not issuer_request_period_is_allowed(
        request, fiscal_year_end_month=None, as_of=date(2026, 10, 3)
    )
    assert not issuer_request_period_is_allowed(
        request, fiscal_year_end_month=6, as_of=date(2026, 10, 3)
    )
    assert not issuer_request_period_is_allowed(
        request, fiscal_year_end_month=12, as_of=date(2025, 1, 1)
    )


def test_historical_extension_is_versioned_exact_and_bounded() -> None:
    request = _historical()
    for change in (
        {"schema_version": "issuer_document_staging_request.v1"},
        {"historical_window": None},
    ):
        with pytest.raises(ValidationError):
            IssuerDocumentStagingRequest.model_validate(
                {**request.model_dump(mode="json"), **change}
            )
    with pytest.raises(ValidationError, match="twelve quarters"):
        HistoricalIssuerDocumentWindow(
            period_ends=(date(2020, 6, 30), date(2026, 6, 30)),
            reviewed_by="owner",
            review_reference_sha256="a" * 64,
        )
    with pytest.raises(ValidationError, match="outside"):
        IssuerDocumentStagingRequest.model_validate(
            {
                **request.model_dump(mode="json"),
                "historical_window": {
                    "period_ends": ["2026-06-30"],
                    "reviewed_by": "owner",
                    "review_reference_sha256": "a" * 64,
                },
            }
        )
