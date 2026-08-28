"""Fetch and validate the Windows Operations review bundle from a Mac."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import to_jsonable_python

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from log_redact import redact  # noqa: E402
from operations.review_bundle import OperationsReviewBundle  # noqa: E402

_ENDPOINT = "/api/operations/review-bundle"
_MAX_RESPONSE_BYTES = 2_000_000


class ReviewFetchError(RuntimeError):
    """A closed, credential-safe fetch or validation failure."""


class FetchSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str
    observed_at: datetime
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_path: str


class TrustedSchedulerTaskPin(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_name: str = Field(min_length=1, max_length=240)
    registered_action_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    registered_checkout_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    registered_wrapper_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class WindowsReviewPins(BaseModel):
    """Owner-approved trust roots created independently of the fetched bundle."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["windows_review_pins.v1"] = "windows_review_pins.v1"
    approved_by: str = Field(min_length=1, max_length=128)
    approved_at: datetime
    serving_origin_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    code_instance_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    database_instance_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    registry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scheduler_definition_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scheduler_tasks: tuple[TrustedSchedulerTaskPin, ...] = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("approved_at")
    @classmethod
    def _approved_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("pin approval timestamp must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _pin_hash_matches(self) -> WindowsReviewPins:
        payload = self.model_dump(mode="json", exclude={"content_sha256"})
        if self.content_sha256 != _payload_sha256(payload):
            raise ValueError("Windows review pin content hash mismatch")
        names = [pin.task_name.casefold() for pin in self.scheduler_tasks]
        if len(names) != len(set(names)):
            raise ValueError("Windows review pins contain duplicate Scheduler tasks")
        return self


def seal_windows_review_pins(
    *,
    bundle: OperationsReviewBundle,
    approved_by: str,
    approved_at: datetime,
) -> WindowsReviewPins:
    if (
        bundle.identity.database_instance_sha256 is None
        or bundle.database.state != "current"
        or bundle.schema_revision.observation.state != "current"
        or bundle.schema_revision.matches is not True
        or bundle.scheduler.observation.state != "current"
    ):
        raise ValueError("cannot approve unavailable or unhealthy Windows authority identity")
    tasks: list[TrustedSchedulerTaskPin] = []
    for task in bundle.scheduler.tasks:
        if (
            task.registered_action_sha256 is None
            or task.registered_checkout_sha256 is None
            or task.registered_wrapper_sha256 is None
            or task.wrapper_match is not True
            or task.expectation_match is not True
        ):
            raise ValueError("cannot approve incomplete or mismatched Scheduler identity")
        tasks.append(
            TrustedSchedulerTaskPin(
                task_name=task.task_name,
                registered_action_sha256=task.registered_action_sha256,
                registered_checkout_sha256=task.registered_checkout_sha256,
                registered_wrapper_sha256=task.registered_wrapper_sha256,
            )
        )
    payload = {
        "schema_version": "windows_review_pins.v1",
        "approved_by": approved_by,
        "approved_at": approved_at,
        "serving_origin_sha256": bundle.identity.serving_origin_sha256,
        "code_instance_sha256": bundle.identity.code_instance_sha256,
        "database_instance_sha256": bundle.identity.database_instance_sha256,
        "registry_sha256": bundle.identity.registry_sha256,
        "scheduler_definition_sha256": bundle.identity.scheduler_definition_sha256,
        "scheduler_tasks": tuple(tasks),
    }
    return WindowsReviewPins.model_validate({**payload, "content_sha256": _payload_sha256(payload)})


def exact_https_origin(value: str) -> str:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise ReviewFetchError("origin must be an exact credential-free HTTPS origin")
    return f"https://{parsed.netloc}"


def identity_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _payload_sha256(value: object) -> str:
    canonical = json.dumps(
        to_jsonable_python(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return identity_sha256(canonical)


def _fetch(url: str, *, timeout_seconds: float) -> bytes:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != _ENDPOINT
    ):
        raise ReviewFetchError("review bundle URL must be the exact HTTPS endpoint")
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # nosec B310
            declared = response.headers.get("Content-Length")
            if declared is not None and int(declared) > _MAX_RESPONSE_BYTES:
                raise ReviewFetchError("review bundle exceeds the bounded response size")
            payload = response.read(_MAX_RESPONSE_BYTES + 1)
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        raise ReviewFetchError(f"review bundle fetch failed: {redact(exc)}") from None
    if len(payload) > _MAX_RESPONSE_BYTES:
        raise ReviewFetchError("review bundle exceeds the bounded response size")
    return payload


def validate_bundle(
    payload: bytes,
    *,
    origin: str,
    now: datetime,
    max_age: timedelta,
    pins: WindowsReviewPins,
) -> OperationsReviewBundle:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    try:
        bundle = OperationsReviewBundle.model_validate_json(payload)
    except Exception as exc:
        raise ReviewFetchError(f"review bundle schema validation failed: {redact(exc)}") from None
    try:
        validate_pinned_identity(bundle=bundle, pins=pins, now=now)
    except ValueError as exc:
        raise ReviewFetchError(str(exc)) from None
    if pins.serving_origin_sha256 != identity_sha256(origin.rstrip("/")):
        raise ReviewFetchError("requested origin does not match trusted host pin")
    if (
        bundle.database.state != "current"
        or bundle.schema_revision.observation.state != "current"
        or bundle.schema_revision.matches is not True
    ):
        raise ReviewFetchError("review bundle database or schema authority is unhealthy")
    if bundle.observed_at > now + timedelta(minutes=5):
        raise ReviewFetchError("review bundle observation time is in the future")
    if now - bundle.observed_at > max_age:
        raise ReviewFetchError("review bundle is stale")
    scheduler_recorded_at = bundle.scheduler.observation.evidence_recorded_at
    if bundle.scheduler.observation.state != "current":
        raise ReviewFetchError("review bundle Scheduler authority is unhealthy")
    if scheduler_recorded_at is not None and scheduler_recorded_at > now + timedelta(minutes=5):
        raise ReviewFetchError("review bundle Scheduler receipt is from the future")
    if scheduler_recorded_at is None or now - scheduler_recorded_at > max_age:
        raise ReviewFetchError("review bundle Scheduler receipt is stale or absent")
    return bundle


def validate_pinned_identity(
    *, bundle: OperationsReviewBundle, pins: WindowsReviewPins, now: datetime
) -> None:
    """Require a separately stored owner-approved identity root for one bundle."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if pins.approved_at > now + timedelta(minutes=5):
        raise ValueError("trusted pin approval time is in the future")
    if bundle.identity.serving_origin_sha256 != pins.serving_origin_sha256:
        raise ValueError("review bundle serving-origin identity changed")
    if bundle.identity.code_instance_sha256 != pins.code_instance_sha256:
        raise ValueError("review bundle code-instance identity changed")
    if bundle.identity.database_instance_sha256 != pins.database_instance_sha256:
        raise ValueError("review bundle database-instance identity changed")
    if bundle.identity.registry_sha256 != pins.registry_sha256:
        raise ValueError("review bundle registry identity changed")
    if bundle.identity.scheduler_definition_sha256 != pins.scheduler_definition_sha256:
        raise ValueError("review bundle Scheduler definition identity changed")
    expected_tasks = {pin.task_name.casefold(): pin for pin in pins.scheduler_tasks}
    observed_tasks = {task.task_name.casefold(): task for task in bundle.scheduler.tasks}
    if set(observed_tasks) != set(expected_tasks):
        raise ValueError("review bundle Scheduler task identity set changed")
    for name, expected in expected_tasks.items():
        observed = observed_tasks[name]
        if (
            observed.registered_action_sha256 != expected.registered_action_sha256
            or observed.registered_checkout_sha256 != expected.registered_checkout_sha256
            or observed.registered_wrapper_sha256 != expected.registered_wrapper_sha256
            or observed.wrapper_match is not True
            or observed.expectation_match is not True
        ):
            raise ValueError(f"review bundle Scheduler identity changed for {name}")


def _persist(bundle: OperationsReviewBundle, *, output_root: Path) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    destination = output_root / f"{bundle.content_sha256}.json"
    encoded = bundle.model_dump_json(indent=2).encode("utf-8") + b"\n"
    if destination.exists():
        if destination.read_bytes() != encoded:
            raise ReviewFetchError("content-addressed review bundle conflicts with existing bytes")
        return destination
    temporary = destination.with_suffix(".tmp")
    temporary.write_bytes(encoded)
    temporary.replace(destination)
    return destination


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--origin", required=True, help="Exact origin from live Windows Serve status"
    )
    parser.add_argument("--max-age-seconds", type=int, default=1200)
    parser.add_argument("--timeout-seconds", type=float, default=15.0)
    parser.add_argument("--pins", type=Path, required=True)
    parser.add_argument(
        "--output-root", type=Path, default=PROJECT_ROOT / ".tmp" / "windows_review"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_age_seconds <= 0 or args.timeout_seconds <= 0:
        raise ReviewFetchError("timeouts and maximum age must be positive")
    origin = exact_https_origin(args.origin)
    pins = WindowsReviewPins.model_validate_json(args.pins.read_text(encoding="utf-8"))
    payload = _fetch(origin + _ENDPOINT, timeout_seconds=args.timeout_seconds)
    bundle = validate_bundle(
        payload,
        origin=origin,
        now=datetime.now(UTC),
        max_age=timedelta(seconds=args.max_age_seconds),
        pins=pins,
    )
    destination = _persist(bundle, output_root=args.output_root)
    summary = FetchSummary(
        schema_version=bundle.schema_version,
        observed_at=bundle.observed_at,
        content_sha256=bundle.content_sha256,
        output_path=str(destination),
    )
    print(summary.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
