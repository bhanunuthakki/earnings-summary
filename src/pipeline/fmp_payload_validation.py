"""One typed pre-write contract for live and corpus FMP payloads.

The provider response and the offline corpus are two transports for the same
artifact coordinate. This module owns the envelope, record, and issuer
invariants so neither transport can weaken them independently.
"""

from __future__ import annotations

import json
from typing import Literal, TypedDict, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from models.fmp_payloads import STABLE_FMP_RECORD_MODELS


class FmpPayloadCoordinate(BaseModel):
    """Catalog coordinate whose payload is about to cross a write boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    endpoint: str = Field(min_length=1)
    suffix: str = Field(min_length=1)
    envelope: Literal["record_list"] = "record_list"

    @field_validator("endpoint", "suffix")
    @classmethod
    def _strip_coordinate(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("payload coordinate components cannot be blank")
        return stripped

    @property
    def permits_cross_issuer_records(self) -> bool:
        return self.endpoint == "stock-peers" and self.suffix == "peers"


class FmpPayloadContractIssue(BaseModel):
    """Stable, serializable detail for one rejected payload field or record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    loc: tuple[str | int, ...]
    msg: str
    type: str


class FmpPayloadErrorDetail(TypedDict):
    loc: tuple[str | int, ...]
    msg: str
    type: str


class ValidatedFmpPayload(BaseModel):
    """Validated record view; callers retain the original bytes for persistence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    coordinate: FmpPayloadCoordinate
    expected_ticker: str
    records: tuple[dict[str, object], ...]

    @field_validator("expected_ticker")
    @classmethod
    def _normalize_ticker(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("expected ticker cannot be blank")
        return normalized


class FmpPayloadContractError(ValueError):
    """Typed failure raised before any FMP artifact or success-state write."""

    def __init__(
        self,
        coordinate: FmpPayloadCoordinate,
        expected_ticker: str,
        issues: tuple[FmpPayloadContractIssue, ...],
    ) -> None:
        self.coordinate = coordinate
        self.expected_ticker = expected_ticker
        self.issues = issues
        super().__init__(
            f"invalid FMP payload for {expected_ticker} "
            f"{coordinate.endpoint}/{coordinate.suffix}: {len(issues)} contract issue(s)"
        )

    def errors(self) -> list[FmpPayloadErrorDetail]:
        return [{"loc": issue.loc, "msg": issue.msg, "type": issue.type} for issue in self.issues]


_GENERIC_MEANINGFUL_FIELDS = frozenset(
    {
        "symbol",
        "ticker",
        "date",
        "year",
        "calendarYear",
        "fiscalYear",
        "companyName",
        "name",
    }
)


def _issue(loc: tuple[str | int, ...], msg: str, issue_type: str) -> FmpPayloadContractIssue:
    return FmpPayloadContractIssue(loc=loc, msg=msg, type=issue_type)


def validate_fmp_prewrite_payload(
    *,
    coordinate: FmpPayloadCoordinate,
    expected_ticker: str,
    payload: object,
) -> ValidatedFmpPayload:
    """Validate an entire payload before either transport writes success state."""
    normalized_ticker = expected_ticker.strip().upper()
    issues: list[FmpPayloadContractIssue] = []
    if not normalized_ticker:
        issues.append(
            _issue(("expected_ticker",), "expected ticker cannot be blank", "value_error")
        )

    if not isinstance(payload, list):
        issues.append(
            _issue(
                ("payload",),
                f"{coordinate.envelope} envelope requires a JSON list",
                "list_type",
            )
        )
        raise FmpPayloadContractError(coordinate, normalized_ticker, tuple(issues))

    raw_records = cast(list[object], payload)
    if not raw_records:
        issues.append(_issue(("payload",), "record-list payload cannot be empty", "too_short"))

    model = STABLE_FMP_RECORD_MODELS.get(coordinate.endpoint)
    records: list[dict[str, object]] = []
    for index, raw_record in enumerate(raw_records):
        if not isinstance(raw_record, dict):
            issues.append(
                _issue(("payload", index), "every payload record must be an object", "dict_type")
            )
            continue
        record = cast(dict[str, object], raw_record)
        records.append(record)
        if not record:
            issues.append(_issue(("payload", index), "payload record cannot be empty", "too_short"))
            continue

        if model is None and not any(
            field in record and record[field] not in (None, "")
            for field in _GENERIC_MEANINGFUL_FIELDS
        ):
            issues.append(
                _issue(
                    ("payload", index),
                    "generic payload record lacks a meaningful identity or period field",
                    "value_error",
                )
            )

        if model is not None:
            try:
                model.model_validate(record)
            except ValidationError as exc:
                for detail in exc.errors(
                    include_url=False,
                    include_context=False,
                    include_input=False,
                ):
                    loc = ("payload", index, *detail["loc"])
                    issues.append(
                        _issue(
                            cast(tuple[str | int, ...], loc),
                            str(detail["msg"]),
                            str(detail["type"]),
                        )
                    )

        if not coordinate.permits_cross_issuer_records:
            for field in ("symbol", "ticker"):
                if field not in record:
                    continue
                value = record[field]
                if not isinstance(value, str) or value.strip().upper() != normalized_ticker:
                    issues.append(
                        _issue(
                            ("payload", index, field),
                            f"record {field} must match expected ticker {normalized_ticker}",
                            "ticker_mismatch",
                        )
                    )

    if issues:
        raise FmpPayloadContractError(coordinate, normalized_ticker, tuple(issues))
    return ValidatedFmpPayload(
        coordinate=coordinate,
        expected_ticker=normalized_ticker,
        records=tuple(records),
    )


def validate_fmp_prewrite_bytes(
    *,
    coordinate: FmpPayloadCoordinate,
    expected_ticker: str,
    content: bytes,
) -> tuple[object, ValidatedFmpPayload]:
    """Decode and validate exact held corpus bytes as one typed boundary."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        issue = _issue(
            ("payload",),
            f"payload is not valid UTF-8 at byte {exc.start}",
            "string_unicode",
        )
        raise FmpPayloadContractError(
            coordinate, expected_ticker.strip().upper(), (issue,)
        ) from exc
    try:
        payload: object = json.loads(text)
    except json.JSONDecodeError as exc:
        issue = _issue(
            ("payload",),
            f"payload is not valid JSON at line {exc.lineno} column {exc.colno}",
            "json_invalid",
        )
        raise FmpPayloadContractError(
            coordinate, expected_ticker.strip().upper(), (issue,)
        ) from exc
    validated = validate_fmp_prewrite_payload(
        coordinate=coordinate,
        expected_ticker=expected_ticker,
        payload=payload,
    )
    return payload, validated


__all__ = [
    "FmpPayloadContractError",
    "FmpPayloadContractIssue",
    "FmpPayloadCoordinate",
    "ValidatedFmpPayload",
    "validate_fmp_prewrite_bytes",
    "validate_fmp_prewrite_payload",
]
