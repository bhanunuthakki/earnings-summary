"""Historical intake changes an exact requested period, never global policy."""

import json
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


def test_fiscal_reported_window_preserves_exact_calendar_and_legacy_wire() -> None:
    from datetime import UTC, datetime

    from pipeline.issuer_document_inventory import (
        FiscalPeriodWitness,
        IssuerDocumentInventoryRequest,
    )
    from pipeline.source_policy import fiscal_quarter_end

    base = request_fixture()
    legacy_payload = base.inventory_request.model_dump(mode="json")
    assert "fiscal_period_witness" not in legacy_payload
    assert base.inventory_request.canonical_json == json.dumps(
        legacy_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    witness = FiscalPeriodWitness(
        issuer_id="synthetic:issuer",
        profile_revision_id="synthetic:profile",
        fiscal_year_end="01-31",
        document_version_id="synthetic:document",
        document_sha256="b" * 64,
        knowledge_cutoff=datetime(2026, 10, 1, tzinfo=UTC),
        observed_through=datetime(2026, 10, 1, tzinfo=UTC),
    )
    inv = IssuerDocumentInventoryRequest.model_validate(
        {
            **legacy_payload,
            "fiscal_year": 2027,
            "fiscal_quarter": 2,
            "period_end": "2026-07-31",
            "fiscal_period_witness": witness,
        }
    )
    window = HistoricalIssuerDocumentWindow(
        period_ends=(
            date(2025, 7, 31),
            date(2025, 10, 31),
            date(2026, 1, 31),
            date(2026, 4, 30),
            date(2026, 7, 31),
        ),
        reviewed_by="synthetic-owner",
        review_reference_sha256="c" * 64,
        fiscal_period_witness=witness,
    )
    staged = IssuerDocumentStagingRequest(
        schema_version="issuer_document_staging_request.v2",
        attempt_id=base.attempt_id,
        inventory_request=inv,
        inventory_request_sha256=inv.request_sha256,
        historical_window=window,
    )
    assert issuer_request_period_is_allowed(
        staged, fiscal_year_end_month=1, as_of=date(2026, 10, 31)
    )
    assert issuer_request_period_is_allowed(
        staged, fiscal_year_end_month=12, as_of=date(2026, 10, 31)
    )
    with pytest.raises(ValidationError):
        IssuerDocumentStagingRequest.model_validate(
            {
                **staged.model_dump(),
                "historical_window": window.model_copy(update={"fiscal_period_witness": None}),
            }
        )
    with pytest.raises(ValidationError):
        IssuerDocumentInventoryRequest.model_validate(
            {**inv.model_dump(), "period_end": "2026-07-30"}
        )
    with pytest.raises(ValueError, match="non-month-end"):
        fiscal_quarter_end(fiscal_year=2026, fiscal_quarter=4, fiscal_year_end="01-30")
    assert fiscal_quarter_end(fiscal_year=2027, fiscal_quarter=1, fiscal_year_end="01-31") == date(
        2026, 4, 30
    )
