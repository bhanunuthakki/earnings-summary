from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from models.companies import ListType
from models.documents import DocType, SourceType
from provenance.source_regime import (
    SourceRegime,
    SourceRegimeReceiptIdentity,
    classification_for_source_type,
    contract_for,
    receipt_identity,
)
from transcripts.acquisition_semantics import (
    TRANSCRIPT_ACQUISITION_POLICY_VERSION,
    ExistingArtifactBehavior,
    StoredTargetStatus,
    TranscriptAcquisitionAuthorization,
    TranscriptAcquisitionEntrypoint,
    TranscriptAcquisitionRequest,
    TranscriptAuthorizationFailure,
    TranscriptAuthorizationProvenance,
    TranscriptAuthorizationReason,
    TranscriptAuthorizationStatus,
    TranscriptProvider,
    TranscriptReportingStatus,
    TranscriptStoredTarget,
    transcript_authorization_idempotency_key,
    validate_transcript_acquisition_authorization,
)

_REGIME_IDENTITY = receipt_identity(contract_for(SourceRegime.COMBINED))
_POLICY_VERSION = TRANSCRIPT_ACQUISITION_POLICY_VERSION


def _request(
    *,
    entrypoint: TranscriptAcquisitionEntrypoint = (
        TranscriptAcquisitionEntrypoint.FETCH_QA_TRANSCRIPT
    ),
    ticker: str = "ACME",
    source_type: SourceType = SourceType.IR_DOC,
    document_type: DocType = DocType.EARNINGS_CALL_TRANSCRIPT,
    provider: TranscriptProvider = TranscriptProvider.ISSUER_IR,
    existing_artifact: bool = False,
    existing_artifact_behavior: ExistingArtifactBehavior = ExistingArtifactBehavior.SKIP,
    source_regime_identity: SourceRegimeReceiptIdentity = _REGIME_IDENTITY,
) -> TranscriptAcquisitionRequest:
    return TranscriptAcquisitionRequest(
        entrypoint=entrypoint,
        canonical_ticker=ticker,
        fiscal_year=2026,
        fiscal_quarter=2,
        source_type=source_type,
        document_type=document_type,
        provider=provider,
        owner_requested=False,
        as_of=date(2026, 8, 12),
        existing_artifact=existing_artifact,
        existing_artifact_behavior=existing_artifact_behavior,
        source_policy_version=_POLICY_VERSION,
        source_regime_identity=source_regime_identity,
    )


def _provenance(
    request: TranscriptAcquisitionRequest,
    *,
    entrypoint: TranscriptAcquisitionEntrypoint | None = None,
    ticker: str | None = None,
    provider: TranscriptProvider | None = None,
) -> TranscriptAuthorizationProvenance:
    return TranscriptAuthorizationProvenance(
        entrypoint=request.entrypoint if entrypoint is None else entrypoint,
        canonical_ticker=request.canonical_ticker if ticker is None else ticker,
        fiscal_year=request.fiscal_year,
        fiscal_quarter=request.fiscal_quarter,
        as_of=request.as_of,
        source_type=request.source_type,
        document_type=request.document_type,
        provider=request.provider if provider is None else provider,
        source_policy_version=request.source_policy_version,
        source_regime_identity=request.source_regime_identity,
        source_authority=classification_for_source_type(request.source_type).authority,
    )


def _stored_target(
    request: TranscriptAcquisitionRequest,
    *,
    ticker: str | None = None,
    status: StoredTargetStatus = StoredTargetStatus.AUTHORIZED,
    reporting_status: TranscriptReportingStatus = TranscriptReportingStatus.ELIGIBLE,
    coverage_role: ListType | None = ListType.PORTFOLIO,
    fiscal_year_end_month: int | None = 12,
) -> TranscriptStoredTarget:
    return TranscriptStoredTarget(
        canonical_ticker=request.canonical_ticker if ticker is None else ticker,
        fiscal_year=request.fiscal_year,
        fiscal_quarter=request.fiscal_quarter,
        as_of=request.as_of,
        owner_requested=request.owner_requested,
        coverage_role=coverage_role,
        fiscal_year_end_month=fiscal_year_end_month,
        source_policy_version=request.source_policy_version,
        source_regime_identity=request.source_regime_identity,
        status=status,
        reporting_status=reporting_status,
    )


def _authorization(
    request: TranscriptAcquisitionRequest,
    *,
    status: TranscriptAuthorizationStatus = TranscriptAuthorizationStatus.AUTHORIZED,
    reason: TranscriptAuthorizationReason = TranscriptAuthorizationReason.AUTHORIZED,
    failure: TranscriptAuthorizationFailure | None = None,
    provenance: TranscriptAuthorizationProvenance | None = None,
    stored_target: TranscriptStoredTarget | None = None,
) -> TranscriptAcquisitionAuthorization:
    return TranscriptAcquisitionAuthorization(
        request=request,
        status=status,
        reason=reason,
        failure=failure,
        idempotency_key=transcript_authorization_idempotency_key(request),
        stored_target=_stored_target(request) if stored_target is None else stored_target,
        provenance=_provenance(request) if provenance is None else provenance,
    )


def test_valid_authorization_is_deterministic_and_dormant() -> None:
    request = _request()
    authorization = _authorization(request)

    assert validate_transcript_acquisition_authorization(authorization) == authorization
    assert transcript_authorization_idempotency_key(request) == (
        transcript_authorization_idempotency_key(request)
    )
    assert authorization.idempotency_key.startswith("transcript:")


def test_request_issuer_ir_cannot_carry_youtube_provenance() -> None:
    request = _request(provider=TranscriptProvider.ISSUER_IR)
    forged = _authorization(
        request,
        provenance=_provenance(request, provider=TranscriptProvider.YOUTUBE_AUDIO),
    )

    with pytest.raises(ValueError, match="provenance does not exactly match request"):
        validate_transcript_acquisition_authorization(forged)


def test_request_ticker_must_match_stored_target() -> None:
    request = _request(ticker="ACME")
    forged = _authorization(request, stored_target=_stored_target(request, ticker="OTHER"))

    with pytest.raises(ValueError, match="stored target does not exactly match request"):
        validate_transcript_acquisition_authorization(forged)


def test_authorized_status_cannot_hide_existing_artifact() -> None:
    request = _request(existing_artifact=True)
    forged = _authorization(request)

    with pytest.raises(ValueError, match="status/reason/failure combination"):
        validate_transcript_acquisition_authorization(forged)


def test_reuse_behavior_requires_an_existing_artifact() -> None:
    with pytest.raises(ValidationError, match="reuse requires an existing artifact"):
        _request(existing_artifact_behavior=ExistingArtifactBehavior.REUSE)


def test_request_entrypoint_must_match_provenance() -> None:
    request = _request(entrypoint=TranscriptAcquisitionEntrypoint.FETCH_QA_TRANSCRIPT)
    forged = _authorization(
        request,
        provenance=_provenance(
            request,
            entrypoint=TranscriptAcquisitionEntrypoint.QUARTERLY_REFRESH,
        ),
    )

    with pytest.raises(ValueError, match="provenance does not exactly match request"):
        validate_transcript_acquisition_authorization(forged)


def test_valid_policy_denial_and_exact_replay_are_admitted() -> None:
    denied_request = _request()
    denied_target = _stored_target(
        denied_request,
        status=StoredTargetStatus.POLICY_DENIED,
        reporting_status=TranscriptReportingStatus.NOT_EVALUATED,
        coverage_role=ListType.WATCHLIST,
    )
    denied = _authorization(
        denied_request,
        status=TranscriptAuthorizationStatus.DENIED,
        reason=TranscriptAuthorizationReason.STORED_IDENTITY_DENIED,
        failure=TranscriptAuthorizationFailure.STORED_IDENTITY_POLICY,
        stored_target=denied_target,
    )
    replay_request = _request(existing_artifact=True)
    replay = _authorization(
        replay_request,
        status=TranscriptAuthorizationStatus.IDEMPOTENT_REPLAY,
        reason=TranscriptAuthorizationReason.EXISTING_ARTIFACT,
    )

    assert validate_transcript_acquisition_authorization(denied) == denied
    assert validate_transcript_acquisition_authorization(replay) == replay
    assert replay.idempotency_key == transcript_authorization_idempotency_key(_request())


def test_audio_is_a_canonical_bha29_grant_but_acquisition_remains_denied() -> None:
    request = _request(
        entrypoint=TranscriptAcquisitionEntrypoint.FETCH_AUDIO_TRANSCRIPTS,
        source_type=SourceType.TRANSCRIPT_AUDIO,
        document_type=DocType.EARNINGS_CALL_AUDIO,
        provider=TranscriptProvider.YOUTUBE_AUDIO,
    )
    denied = _authorization(
        request,
        status=TranscriptAuthorizationStatus.DENIED,
        reason=TranscriptAuthorizationReason.WEBCAST_EXCLUDED,
        failure=TranscriptAuthorizationFailure.AUDIO_WEBCAST_EXCLUDED,
    )

    assert validate_transcript_acquisition_authorization(denied) == denied


def test_aggregator_cannot_be_mislabeled_as_issuer_under_current_bha29_contract() -> None:
    request = _request(
        source_type=SourceType.IR_DOC,
        document_type=DocType.EARNINGS_CALL_TRANSCRIPT,
        provider=TranscriptProvider.ROIC,
    )
    denied = _authorization(
        request,
        status=TranscriptAuthorizationStatus.DENIED,
        reason=TranscriptAuthorizationReason.PROVIDER_SOURCE_MISMATCH,
        failure=TranscriptAuthorizationFailure.SOURCE_DOCUMENT_CLASS,
    )

    assert validate_transcript_acquisition_authorization(denied) == denied


def test_provider_source_document_and_entrypoint_grants_are_exact() -> None:
    request = _request(
        entrypoint=TranscriptAcquisitionEntrypoint.REFETCH_AGGREGATOR_TRANSCRIPTS,
        source_type=SourceType.IR_DOC,
        document_type=DocType.EARNINGS_CALL_TRANSCRIPT,
        provider=TranscriptProvider.ISSUER_IR,
    )
    forged = _authorization(request)

    with pytest.raises(ValueError, match="status/reason/failure combination"):
        validate_transcript_acquisition_authorization(forged)


def test_reporting_and_source_policy_identity_must_match_stored_target() -> None:
    request = _request()
    valid = _authorization(request)
    reporting_forgery = _stored_target(request).model_copy(update={"fiscal_quarter": 1})
    policy_forgery = _stored_target(request).model_copy(
        update={"source_policy_version": "coordinated-forgery"}
    )

    reporting_receipt = valid.model_copy(update={"stored_target": reporting_forgery})
    with pytest.raises(ValueError, match="stored target does not exactly match request"):
        validate_transcript_acquisition_authorization(reporting_receipt)
    policy_receipt = valid.model_copy(update={"stored_target": policy_forgery})
    with pytest.raises(ValidationError, match=r"Input should be '2026-08-12\.2'"):
        validate_transcript_acquisition_authorization(policy_receipt)


def test_reporting_denial_has_one_exact_outcome() -> None:
    request = _request().model_copy(update={"fiscal_year": 2020})
    stored_target = _stored_target(
        request,
        reporting_status=TranscriptReportingStatus.OUT_OF_WINDOW,
    )
    denied = _authorization(
        request,
        status=TranscriptAuthorizationStatus.DENIED,
        reason=TranscriptAuthorizationReason.REPORTED_QUARTER_WINDOW_DENIED,
        failure=TranscriptAuthorizationFailure.REPORTING_PERIOD,
        stored_target=stored_target,
    )

    assert validate_transcript_acquisition_authorization(denied) == denied
    forged = denied.model_copy(update={"failure": None})
    with pytest.raises(ValueError, match="status/reason/failure combination"):
        validate_transcript_acquisition_authorization(forged)


def test_coordinated_reporting_and_role_policy_forgeries_fail_closed() -> None:
    request = _request()
    valid = _authorization(request)
    old_request = request.model_copy(update={"fiscal_year": 2020})
    reporting_forgery = valid.model_copy(
        update={
            "request": old_request,
            "provenance": valid.provenance.model_copy(update={"fiscal_year": 2020}),
            "stored_target": valid.stored_target.model_copy(update={"fiscal_year": 2020}),
            "idempotency_key": transcript_authorization_idempotency_key(old_request),
        }
    )
    role_forgery = valid.model_copy(
        update={
            "stored_target": valid.stored_target.model_copy(
                update={"coverage_role": ListType.WATCHLIST}
            )
        }
    )

    with pytest.raises(ValidationError, match="reporting status does not match"):
        validate_transcript_acquisition_authorization(reporting_forgery)
    with pytest.raises(ValidationError, match="status does not match coverage-role policy"):
        validate_transcript_acquisition_authorization(role_forgery)


def test_idempotency_key_and_regime_identity_cannot_be_coordinated_forgeries() -> None:
    request = _request()
    forged_key = _authorization(request).model_copy(
        update={"idempotency_key": f"transcript:{'b' * 64}"}
    )
    forged_regime = SourceRegimeReceiptIdentity.model_construct(
        schema_version="source-regime-contract@2",
        regime=SourceRegime.COMBINED,
        contract_sha256="b" * 64,
    )
    forged_request = request.model_copy(update={"source_regime_identity": forged_regime})
    valid_receipt = _authorization(request)
    forged_receipt = valid_receipt.model_copy(
        update={
            "request": forged_request,
            "provenance": valid_receipt.provenance.model_copy(
                update={"source_regime_identity": forged_regime}
            ),
            "stored_target": valid_receipt.stored_target.model_copy(
                update={"source_regime_identity": forged_regime}
            ),
        }
    )

    with pytest.raises(ValueError, match="idempotency key"):
        validate_transcript_acquisition_authorization(forged_key)
    with pytest.raises(ValidationError, match="canonical registered contract"):
        validate_transcript_acquisition_authorization(forged_receipt)


def test_provenance_authority_and_policy_version_are_canonical() -> None:
    request = _request()
    authority_forgery = _authorization(request).model_copy(
        update={"provenance": _provenance(request).model_copy(update={"source_authority": None})}
    )

    with pytest.raises(ValueError, match="provenance does not exactly match request"):
        validate_transcript_acquisition_authorization(authority_forgery)
    with pytest.raises(ValidationError, match=r"Input should be '2026-08-12\.2'"):
        TranscriptAcquisitionRequest.model_validate(
            {
                **request.model_dump(mode="python"),
                "source_policy_version": "coordinated-forgery",
            }
        )


def test_model_construct_and_extra_fields_do_not_bypass_semantics() -> None:
    request = _request(existing_artifact=True)
    forged = TranscriptAcquisitionAuthorization.model_construct(
        request=request,
        status=TranscriptAuthorizationStatus.AUTHORIZED,
        reason=TranscriptAuthorizationReason.AUTHORIZED,
        failure=None,
        idempotency_key=transcript_authorization_idempotency_key(request),
        stored_target=_stored_target(request),
        provenance=_provenance(request),
    )

    with pytest.raises(ValueError, match="status/reason/failure combination"):
        validate_transcript_acquisition_authorization(forged)
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        TranscriptAcquisitionRequest.model_validate(
            {**_request().model_dump(mode="python"), "network_allowed": True}
        )
