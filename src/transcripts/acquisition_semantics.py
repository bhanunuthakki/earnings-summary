"""Dormant, pure semantics for transcript-acquisition authorization receipts.

This module performs no database, filesystem, network, or entrypoint work. A
later caller may use the validator at a mutation boundary, but importing this
module does not activate transcript acquisition or authorize any side effect.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
    validate_call,
)

from models.companies import ListType
from models.documents import DocType, SourceType
from provenance.source_regime import (
    EvidenceAuthority,
    SourceDomain,
    SourceRegime,
    SourceRegimeReceiptIdentity,
    classification_for_source_type,
    contract_for,
    receipt_identity,
)

_STRICT_FROZEN = ConfigDict(
    extra="forbid",
    frozen=True,
    strict=True,
    revalidate_instances="always",
)
_CanonicalTicker = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Z0-9][A-Z0-9.-]{0,15}$"),
]
TRANSCRIPT_ACQUISITION_POLICY_VERSION = "2026-08-12.2"
_MAX_REPORTED_QUARTERS = 5


class TranscriptAcquisitionEntrypoint(StrEnum):
    QUARTERLY_REFRESH = "quarterly_refresh"
    FETCH_QA_TRANSCRIPT = "fetch_qa_transcript"
    REFETCH_AGGREGATOR_TRANSCRIPTS = "refetch_aggregator_transcripts"
    FETCH_AUDIO_TRANSCRIPTS = "fetch_audio_transcripts"


class TranscriptProvider(StrEnum):
    ISSUER_IR = "issuer_ir"
    ROIC = "roic"
    STOCKANALYSIS = "stockanalysis"
    TICKERTRENDS = "tickertrends"
    YOUTUBE_AUDIO = "youtube_audio"


class ExistingArtifactBehavior(StrEnum):
    SKIP = "skip"
    REUSE = "reuse"
    REFRESH = "refresh"


class StoredTargetStatus(StrEnum):
    AUTHORIZED = "authorized"
    IDENTITY_UNAVAILABLE = "stored_identity_unavailable"
    IDENTITY_NOT_FOUND = "stored_identity_not_found"
    IDENTITY_AMBIGUOUS = "stored_identity_ambiguous"
    ROLE_INVALID = "stored_role_invalid"
    POLICY_DENIED = "policy_denied"


class TranscriptReportingStatus(StrEnum):
    ELIGIBLE = "eligible"
    OUT_OF_WINDOW = "reported_quarter_window_denied"
    FISCAL_CALENDAR_UNAVAILABLE = "fiscal_calendar_unavailable"
    NOT_EVALUATED = "not_evaluated"


class TranscriptAuthorizationStatus(StrEnum):
    AUTHORIZED = "authorized"
    DENIED = "denied"
    IDEMPOTENT_REPLAY = "idempotent_replay"


class TranscriptAuthorizationFailure(StrEnum):
    SOURCE_REGIME_IDENTITY = "source_regime_identity"
    ENTRYPOINT_PROVIDER_GRANT = "entrypoint_provider_grant"
    SOURCE_DOCUMENT_CLASS = "source_document_class"
    STORED_IDENTITY_POLICY = "stored_identity_policy"
    REPORTING_PERIOD = "reporting_period"
    AUDIO_WEBCAST_EXCLUDED = "audio_webcast_excluded"


class TranscriptAuthorizationReason(StrEnum):
    AUTHORIZED = "authorized"
    EXISTING_ARTIFACT = "existing_artifact"
    SOURCE_REGIME_MISMATCH = "source_regime_mismatch"
    ENTRYPOINT_PROVIDER_MISMATCH = "entrypoint_provider_mismatch"
    PROVIDER_SOURCE_MISMATCH = "provider_source_mismatch"
    SOURCE_NOT_ADMITTED = "source_not_admitted"
    STORED_IDENTITY_DENIED = "stored_identity_denied"
    REPORTED_QUARTER_WINDOW_DENIED = "reported_quarter_window_denied"
    FISCAL_CALENDAR_UNAVAILABLE = "fiscal_calendar_unavailable"
    WEBCAST_EXCLUDED = "webcast_excluded"


_COMBINED_REGIME_IDENTITY = receipt_identity(contract_for(SourceRegime.COMBINED))
_PROVIDERS_BY_ENTRYPOINT = {
    TranscriptAcquisitionEntrypoint.QUARTERLY_REFRESH: frozenset({TranscriptProvider.ISSUER_IR}),
    TranscriptAcquisitionEntrypoint.FETCH_QA_TRANSCRIPT: frozenset(
        {
            TranscriptProvider.ISSUER_IR,
            TranscriptProvider.ROIC,
            TranscriptProvider.STOCKANALYSIS,
            TranscriptProvider.TICKERTRENDS,
        }
    ),
    TranscriptAcquisitionEntrypoint.REFETCH_AGGREGATOR_TRANSCRIPTS: frozenset(
        {
            TranscriptProvider.ROIC,
            TranscriptProvider.STOCKANALYSIS,
            TranscriptProvider.TICKERTRENDS,
        }
    ),
    TranscriptAcquisitionEntrypoint.FETCH_AUDIO_TRANSCRIPTS: frozenset(
        {TranscriptProvider.YOUTUBE_AUDIO}
    ),
}
_SOURCE_TYPE_BY_PROVIDER: dict[TranscriptProvider, SourceType | None] = {
    TranscriptProvider.ISSUER_IR: SourceType.IR_DOC,
    # BHA-29 on this base has no aggregator source identity. Aggregators stay
    # unadmitted instead of being mislabeled as issuer evidence.
    TranscriptProvider.ROIC: None,
    TranscriptProvider.STOCKANALYSIS: None,
    TranscriptProvider.TICKERTRENDS: None,
    TranscriptProvider.YOUTUBE_AUDIO: SourceType.TRANSCRIPT_AUDIO,
}


class TranscriptAcquisitionRequest(BaseModel):
    """The exact acquisition intent whose semantic receipt is being checked."""

    model_config = _STRICT_FROZEN

    schema_version: Literal["transcript-acquisition-request@1"] = "transcript-acquisition-request@1"
    entrypoint: TranscriptAcquisitionEntrypoint
    canonical_ticker: _CanonicalTicker
    fiscal_year: int = Field(ge=2000, le=2100)
    fiscal_quarter: int = Field(ge=1, le=4)
    as_of: date
    source_type: SourceType
    document_type: DocType
    provider: TranscriptProvider
    owner_requested: bool
    existing_artifact: bool
    existing_artifact_behavior: ExistingArtifactBehavior
    source_policy_version: Literal["2026-08-12.2"]
    source_regime_identity: SourceRegimeReceiptIdentity

    @model_validator(mode="after")
    def _closed_existing_artifact_behavior(self) -> Self:
        if (
            not self.existing_artifact
            and self.existing_artifact_behavior is ExistingArtifactBehavior.REUSE
        ):
            raise ValueError("reuse requires an existing artifact")
        return self


class TranscriptAuthorizationProvenance(BaseModel):
    """Fields a receipt reports about the request it evaluated."""

    model_config = _STRICT_FROZEN

    entrypoint: TranscriptAcquisitionEntrypoint
    canonical_ticker: _CanonicalTicker
    fiscal_year: int = Field(ge=2000, le=2100)
    fiscal_quarter: int = Field(ge=1, le=4)
    as_of: date
    source_type: SourceType
    document_type: DocType
    provider: TranscriptProvider
    source_policy_version: Literal["2026-08-12.2"]
    source_regime_identity: SourceRegimeReceiptIdentity
    source_authority: EvidenceAuthority | None


class TranscriptStoredTarget(BaseModel):
    """Pure projection of stored identity and reporting-policy evaluation."""

    model_config = _STRICT_FROZEN

    canonical_ticker: _CanonicalTicker
    fiscal_year: int = Field(ge=2000, le=2100)
    fiscal_quarter: int = Field(ge=1, le=4)
    as_of: date
    owner_requested: bool
    coverage_role: ListType | None
    fiscal_year_end_month: int | None = Field(default=None, ge=1, le=12)
    source_policy_version: Literal["2026-08-12.2"]
    source_regime_identity: SourceRegimeReceiptIdentity
    status: StoredTargetStatus
    reporting_status: TranscriptReportingStatus

    @model_validator(mode="after")
    def _closed_status_pair(self) -> Self:
        role_allowed = _role_is_authorized(self.coverage_role, requested=self.owner_requested)
        if self.status is StoredTargetStatus.AUTHORIZED:
            if not role_allowed:
                raise ValueError("stored target status does not match coverage-role policy")
            expected_reporting = (
                TranscriptReportingStatus.FISCAL_CALENDAR_UNAVAILABLE
                if self.fiscal_year_end_month is None
                else TranscriptReportingStatus.ELIGIBLE
                if _reported_quarter_is_in_window(
                    fiscal_year=self.fiscal_year,
                    fiscal_quarter=self.fiscal_quarter,
                    fiscal_year_end_month=self.fiscal_year_end_month,
                    as_of=self.as_of,
                )
                else TranscriptReportingStatus.OUT_OF_WINDOW
            )
            if self.reporting_status is not expected_reporting:
                raise ValueError("stored reporting status does not match reporting policy")
            return self
        if self.reporting_status is not TranscriptReportingStatus.NOT_EVALUATED:
            raise ValueError("reporting status must be unevaluated for a denied stored target")
        if self.status is StoredTargetStatus.POLICY_DENIED:
            if self.coverage_role is None or role_allowed:
                raise ValueError("stored target denial does not match coverage-role policy")
        elif self.coverage_role is not None or self.fiscal_year_end_month is not None:
            raise ValueError("unresolved stored identity must not report company policy fields")
        return self


class TranscriptAcquisitionAuthorization(BaseModel):
    """A proposed receipt; callers must pass it through the canonical validator."""

    model_config = _STRICT_FROZEN

    schema_version: Literal["transcript-acquisition-authorization@1"] = (
        "transcript-acquisition-authorization@1"
    )
    request: TranscriptAcquisitionRequest
    status: TranscriptAuthorizationStatus
    reason: TranscriptAuthorizationReason
    failure: TranscriptAuthorizationFailure | None
    idempotency_key: str = Field(pattern=r"^transcript:[0-9a-f]{64}$")
    stored_target: TranscriptStoredTarget
    provenance: TranscriptAuthorizationProvenance

    @property
    def allowed(self) -> bool:
        return self.status is TranscriptAuthorizationStatus.AUTHORIZED


class _TranscriptIdempotencyInput(BaseModel):
    model_config = _STRICT_FROZEN

    canonical_ticker: _CanonicalTicker
    fiscal_year: int
    fiscal_quarter: int
    provider: TranscriptProvider
    source_type: SourceType
    document_type: DocType
    source_policy_version: Literal["2026-08-12.2"]
    source_regime_identity: SourceRegimeReceiptIdentity


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _role_is_authorized(role: ListType | None, *, requested: bool) -> bool:
    return role is ListType.PORTFOLIO or (role is ListType.EVALUATION and requested)


def _reported_quarter_is_in_window(
    *,
    fiscal_year: int,
    fiscal_quarter: int,
    fiscal_year_end_month: int,
    as_of: date,
) -> bool:
    completed: list[tuple[date, int, int]] = []
    for year in range(as_of.year - 2, as_of.year + 2):
        for quarter in range(1, 5):
            end_year = year
            end_month = fiscal_year_end_month - (4 - quarter) * 3
            while end_month < 1:
                end_month += 12
                end_year -= 1
            next_month = (
                date(end_year + 1, 1, 1) if end_month == 12 else date(end_year, end_month + 1, 1)
            )
            period_end = date.fromordinal(next_month.toordinal() - 1)
            if period_end <= as_of:
                completed.append((period_end, year, quarter))
    newest = sorted(completed, reverse=True)[:_MAX_REPORTED_QUARTERS]
    return any(year == fiscal_year and quarter == fiscal_quarter for _, year, quarter in newest)


@validate_call(config=_STRICT_FROZEN)
def transcript_authorization_idempotency_key(request: TranscriptAcquisitionRequest) -> str:
    """Return the target-level key; replay flags and entrypoint are intentionally excluded."""

    payload = _TranscriptIdempotencyInput(
        canonical_ticker=request.canonical_ticker,
        fiscal_year=request.fiscal_year,
        fiscal_quarter=request.fiscal_quarter,
        provider=request.provider,
        source_type=request.source_type,
        document_type=request.document_type,
        source_policy_version=request.source_policy_version,
        source_regime_identity=request.source_regime_identity,
    )
    digest = hashlib.sha256(
        _canonical_json(payload.model_dump(mode="json")).encode("utf-8")
    ).hexdigest()
    return f"transcript:{digest}"


def _request_provenance_identity(
    request: TranscriptAcquisitionRequest,
) -> tuple[object, ...]:
    return (
        request.entrypoint,
        request.canonical_ticker,
        request.fiscal_year,
        request.fiscal_quarter,
        request.as_of,
        request.source_type,
        request.document_type,
        request.provider,
        request.source_policy_version,
        request.source_regime_identity,
    )


def _provenance_identity(provenance: TranscriptAuthorizationProvenance) -> tuple[object, ...]:
    return (
        provenance.entrypoint,
        provenance.canonical_ticker,
        provenance.fiscal_year,
        provenance.fiscal_quarter,
        provenance.as_of,
        provenance.source_type,
        provenance.document_type,
        provenance.provider,
        provenance.source_policy_version,
        provenance.source_regime_identity,
        provenance.source_authority,
    )


def _request_stored_identity(request: TranscriptAcquisitionRequest) -> tuple[object, ...]:
    return (
        request.canonical_ticker,
        request.fiscal_year,
        request.fiscal_quarter,
        request.as_of,
        request.owner_requested,
        request.source_policy_version,
        request.source_regime_identity,
    )


def _stored_identity(stored_target: TranscriptStoredTarget) -> tuple[object, ...]:
    return (
        stored_target.canonical_ticker,
        stored_target.fiscal_year,
        stored_target.fiscal_quarter,
        stored_target.as_of,
        stored_target.owner_requested,
        stored_target.source_policy_version,
        stored_target.source_regime_identity,
    )


def _source_document_is_admitted(request: TranscriptAcquisitionRequest) -> bool:
    policy = contract_for(SourceRegime.COMBINED).policy_for(SourceDomain.TRANSCRIPT)
    return any(
        grant.source_type is request.source_type and request.document_type in grant.document_types
        for grant in policy.source_grants
    )


def _expected_outcome(
    request: TranscriptAcquisitionRequest,
    stored_target: TranscriptStoredTarget,
) -> tuple[
    TranscriptAuthorizationStatus,
    TranscriptAuthorizationReason,
    TranscriptAuthorizationFailure | None,
]:
    if request.source_regime_identity != _COMBINED_REGIME_IDENTITY:
        return (
            TranscriptAuthorizationStatus.DENIED,
            TranscriptAuthorizationReason.SOURCE_REGIME_MISMATCH,
            TranscriptAuthorizationFailure.SOURCE_REGIME_IDENTITY,
        )
    if request.provider not in _PROVIDERS_BY_ENTRYPOINT[request.entrypoint]:
        return (
            TranscriptAuthorizationStatus.DENIED,
            TranscriptAuthorizationReason.ENTRYPOINT_PROVIDER_MISMATCH,
            TranscriptAuthorizationFailure.ENTRYPOINT_PROVIDER_GRANT,
        )
    expected_source_type = _SOURCE_TYPE_BY_PROVIDER[request.provider]
    if expected_source_type is None or request.source_type is not expected_source_type:
        return (
            TranscriptAuthorizationStatus.DENIED,
            TranscriptAuthorizationReason.PROVIDER_SOURCE_MISMATCH,
            TranscriptAuthorizationFailure.SOURCE_DOCUMENT_CLASS,
        )
    if not _source_document_is_admitted(request):
        return (
            TranscriptAuthorizationStatus.DENIED,
            TranscriptAuthorizationReason.SOURCE_NOT_ADMITTED,
            TranscriptAuthorizationFailure.SOURCE_DOCUMENT_CLASS,
        )
    if request.source_type is SourceType.TRANSCRIPT_AUDIO:
        return (
            TranscriptAuthorizationStatus.DENIED,
            TranscriptAuthorizationReason.WEBCAST_EXCLUDED,
            TranscriptAuthorizationFailure.AUDIO_WEBCAST_EXCLUDED,
        )
    if stored_target.status is not StoredTargetStatus.AUTHORIZED:
        return (
            TranscriptAuthorizationStatus.DENIED,
            TranscriptAuthorizationReason.STORED_IDENTITY_DENIED,
            TranscriptAuthorizationFailure.STORED_IDENTITY_POLICY,
        )
    if stored_target.reporting_status is TranscriptReportingStatus.OUT_OF_WINDOW:
        return (
            TranscriptAuthorizationStatus.DENIED,
            TranscriptAuthorizationReason.REPORTED_QUARTER_WINDOW_DENIED,
            TranscriptAuthorizationFailure.REPORTING_PERIOD,
        )
    if stored_target.reporting_status is TranscriptReportingStatus.FISCAL_CALENDAR_UNAVAILABLE:
        return (
            TranscriptAuthorizationStatus.DENIED,
            TranscriptAuthorizationReason.FISCAL_CALENDAR_UNAVAILABLE,
            TranscriptAuthorizationFailure.REPORTING_PERIOD,
        )
    if request.existing_artifact and request.existing_artifact_behavior in {
        ExistingArtifactBehavior.SKIP,
        ExistingArtifactBehavior.REUSE,
    }:
        return (
            TranscriptAuthorizationStatus.IDEMPOTENT_REPLAY,
            TranscriptAuthorizationReason.EXISTING_ARTIFACT,
            None,
        )
    return (
        TranscriptAuthorizationStatus.AUTHORIZED,
        TranscriptAuthorizationReason.AUTHORIZED,
        None,
    )


@validate_call(config=_STRICT_FROZEN)
def validate_transcript_acquisition_authorization(
    authorization: TranscriptAcquisitionAuthorization,
) -> TranscriptAcquisitionAuthorization:
    """Fail closed unless every duplicated identity and derived field is canonical."""

    # Reparse the complete tree so model_construct/model_copy cannot smuggle an
    # unvalidated nested receipt across either a Python or future UDF boundary.
    validated = TranscriptAcquisitionAuthorization.model_validate(
        authorization.model_dump(mode="python")
    )
    request = validated.request
    request_provenance = (
        *_request_provenance_identity(request),
        classification_for_source_type(request.source_type).authority,
    )
    if request_provenance != _provenance_identity(validated.provenance):
        raise ValueError("authorization provenance does not exactly match request")
    if _request_stored_identity(request) != _stored_identity(validated.stored_target):
        raise ValueError("authorization stored target does not exactly match request")
    expected_key = transcript_authorization_idempotency_key(request)
    if validated.idempotency_key != expected_key:
        raise ValueError("authorization idempotency key does not match canonical request")
    expected_outcome = _expected_outcome(request, validated.stored_target)
    actual_outcome = (validated.status, validated.reason, validated.failure)
    if actual_outcome != expected_outcome:
        raise ValueError("authorization status/reason/failure combination is not canonical")
    return validated
