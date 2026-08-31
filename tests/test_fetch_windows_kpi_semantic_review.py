from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import ValidationError

import execution.fetch_windows_kpi_semantic_review as fetch_module
from execution.fetch_windows_kpi_semantic_review import (
    KpiSemanticReviewFetchError,
    fetch_kpi_semantic_review_bytes,
    persist_kpi_semantic_review_export,
    read_bounded_input,
    validate_semantic_review_export,
)
from execution.fetch_windows_review_bundle import WindowsReviewPins
from operations.kpi_semantic_review_export import (
    MAX_KPI_SEMANTIC_EXPORT_BYTES,
    KpiSemanticReviewExport,
    payload_sha256,
    seal_kpi_semantic_review_export,
)
from pipeline.kpi_semantic_review import KpiSemanticReviewBatch

NOW = datetime(2026, 8, 30, tzinfo=UTC)
ORIGIN = "https://windows.example.ts.net"
CODE_SHA = "a" * 64
DATABASE_SHA = "b" * 64
SCHEMA_REVISION = "0035_add_report_kpi_reference_resolution_states"


def _review_batch() -> KpiSemanticReviewBatch:
    payload: dict[str, object] = {
        "schema_version": "kpi_semantic_review.v3",
        "user_id": "owner",
        "ticker": "NU",
        "observed_at": NOW,
        "limit": 1_000,
        "total_items": 0,
        "truncated": False,
        "state_counts": {},
        "items": (),
    }
    return KpiSemanticReviewBatch.model_validate(
        {**payload, "content_sha256": payload_sha256(payload)}
    )


def _export() -> KpiSemanticReviewExport:
    return seal_kpi_semantic_review_export(
        review=_review_batch(),
        code_instance_sha256=CODE_SHA,
        database_instance_sha256=DATABASE_SHA,
        schema_revision=SCHEMA_REVISION,
    )


def _bundle() -> SimpleNamespace:
    return SimpleNamespace(
        identity=SimpleNamespace(
            code_instance_sha256=CODE_SHA,
            database_instance_sha256=DATABASE_SHA,
        ),
        schema_revision=SimpleNamespace(actual_heads=(SCHEMA_REVISION,)),
    )


def _pins() -> WindowsReviewPins:
    return WindowsReviewPins.model_construct()


def test_validator_requires_fresh_independently_validated_bundle_and_exact_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def validate_bundle(
        payload: bytes,
        *,
        origin: str,
        now: datetime,
        max_age: timedelta,
        pins: WindowsReviewPins,
    ) -> SimpleNamespace:
        captured["payload"] = payload
        captured.update({"origin": origin, "now": now, "max_age": max_age, "pins": pins})
        return _bundle()

    monkeypatch.setattr(fetch_module, "validate_bundle", validate_bundle)
    export = _export()

    validated = validate_semantic_review_export(
        export.model_dump_json().encode(),
        ticker="NU",
        expected_user_id="owner",
        now=NOW + timedelta(minutes=1),
        max_age=timedelta(minutes=20),
        review_bundle_payload=b"trusted-bundle",
        origin=ORIGIN,
        pins=_pins(),
    )

    assert validated == export
    assert captured["payload"] == b"trusted-bundle"
    assert captured["origin"] == ORIGIN
    assert captured["pins"] is not None


@pytest.mark.parametrize(
    ("field", "message"),
    (
        ("code_instance_sha256", "code authority"),
        ("database_instance_sha256", "database authority"),
        ("schema_revision", "schema authority"),
    ),
)
def test_validator_rejects_authority_drift(
    monkeypatch: pytest.MonkeyPatch, field: str, message: str
) -> None:
    export = _export().model_copy(update={field: "f" * 64})
    object.__setattr__(
        export,
        "content_sha256",
        payload_sha256(export.model_dump(mode="json", exclude={"content_sha256"})),
    )

    def validate_bundle(
        payload: bytes,
        *,
        origin: str,
        now: datetime,
        max_age: timedelta,
        pins: WindowsReviewPins,
    ) -> SimpleNamespace:
        del payload, origin, now, max_age, pins
        return _bundle()

    monkeypatch.setattr(fetch_module, "validate_bundle", validate_bundle)

    with pytest.raises(KpiSemanticReviewFetchError, match=message):
        validate_semantic_review_export(
            export.model_dump_json().encode(),
            ticker="NU",
            expected_user_id="owner",
            now=NOW,
            max_age=timedelta(minutes=20),
            review_bundle_payload=b"trusted-bundle",
            origin=ORIGIN,
            pins=_pins(),
        )


def test_validator_rejects_v1_and_stale_artifacts(monkeypatch: pytest.MonkeyPatch) -> None:
    def validate_bundle(
        payload: bytes,
        *,
        origin: str,
        now: datetime,
        max_age: timedelta,
        pins: WindowsReviewPins,
    ) -> SimpleNamespace:
        del payload, origin, now, max_age, pins
        return _bundle()

    monkeypatch.setattr(fetch_module, "validate_bundle", validate_bundle)
    export = _export()
    legacy_raw: object = json.loads(export.model_dump_json())
    assert isinstance(legacy_raw, dict)
    legacy = cast(dict[str, object], legacy_raw)
    review_raw = legacy["review"]
    assert isinstance(review_raw, dict)
    review = cast(dict[str, object], review_raw)
    review["schema_version"] = "kpi_semantic_review.v1"
    with pytest.raises(KpiSemanticReviewFetchError, match="schema validation"):
        validate_semantic_review_export(
            json.dumps(legacy).encode(),
            ticker="NU",
            expected_user_id="owner",
            now=NOW,
            max_age=timedelta(minutes=20),
            review_bundle_payload=b"trusted-bundle",
            origin=ORIGIN,
            pins=_pins(),
        )
    with pytest.raises(KpiSemanticReviewFetchError, match="stale"):
        validate_semantic_review_export(
            export.model_dump_json().encode(),
            ticker="NU",
            expected_user_id="owner",
            now=NOW + timedelta(hours=1),
            max_age=timedelta(minutes=20),
            review_bundle_payload=b"trusted-bundle",
            origin=ORIGIN,
            pins=_pins(),
        )


def test_validator_rejects_wrong_independently_expected_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def validate_bundle(
        payload: bytes,
        *,
        origin: str,
        now: datetime,
        max_age: timedelta,
        pins: WindowsReviewPins,
    ) -> SimpleNamespace:
        del payload, origin, now, max_age, pins
        return _bundle()

    monkeypatch.setattr(fetch_module, "validate_bundle", validate_bundle)
    with pytest.raises(KpiSemanticReviewFetchError, match="owner"):
        validate_semantic_review_export(
            _export().model_dump_json().encode(),
            ticker="NU",
            expected_user_id="different-owner",
            now=NOW,
            max_age=timedelta(minutes=20),
            review_bundle_payload=b"trusted-bundle",
            origin=ORIGIN,
            pins=_pins(),
        )


def test_validator_rejects_export_and_nested_review_owner_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def validate_bundle(
        payload: bytes,
        *,
        origin: str,
        now: datetime,
        max_age: timedelta,
        pins: WindowsReviewPins,
    ) -> SimpleNamespace:
        del payload, origin, now, max_age, pins
        return _bundle()

    monkeypatch.setattr(fetch_module, "validate_bundle", validate_bundle)
    raw: dict[str, object] = _export().model_dump(mode="json")
    review_raw = raw["review"]
    assert isinstance(review_raw, dict)
    review = cast(dict[str, object], review_raw)
    review["user_id"] = "different-owner"
    review["content_sha256"] = payload_sha256(
        {key: value for key, value in review.items() if key != "content_sha256"}
    )
    raw["content_sha256"] = payload_sha256(
        {key: value for key, value in raw.items() if key != "content_sha256"}
    )

    with pytest.raises(KpiSemanticReviewFetchError, match="schema validation"):
        validate_semantic_review_export(
            json.dumps(raw).encode(),
            ticker="NU",
            expected_user_id="owner",
            now=NOW,
            max_age=timedelta(minutes=20),
            review_bundle_payload=b"trusted-bundle",
            origin=ORIGIN,
            pins=_pins(),
        )


def test_persist_is_content_addressed_and_conflicts_fail_closed(tmp_path: Path) -> None:
    export = _export()
    output = tmp_path / ".tmp"

    destination = persist_kpi_semantic_review_export(export, output_root=output)
    assert destination.name == f"{export.content_sha256}.json"
    assert KpiSemanticReviewExport.model_validate_json(destination.read_bytes()) == export

    destination.write_text("conflict", encoding="utf-8")
    with pytest.raises(KpiSemanticReviewFetchError, match="conflicts"):
        persist_kpi_semantic_review_export(export, output_root=output)

    destination.write_bytes(b"x" * (MAX_KPI_SEMANTIC_EXPORT_BYTES + 1))
    with pytest.raises(KpiSemanticReviewFetchError, match="byte bound"):
        persist_kpi_semantic_review_export(export, output_root=output)


def test_export_model_itself_rejects_v1_batch() -> None:
    payload: dict[str, object] = _export().model_dump(mode="json")
    review_raw = payload["review"]
    assert isinstance(review_raw, dict)
    review = cast(dict[str, object], review_raw)
    review["schema_version"] = "kpi_semantic_review.v1"
    with pytest.raises(ValidationError, match=r"kpi_semantic_review\.v3"):
        KpiSemanticReviewExport.model_validate(payload)


def test_http_fetch_rejects_redirects_and_declared_oversize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = ORIGIN + "/api/operations/kpi-semantic-review/NU"

    class Response:
        def __init__(self, *, response_url: str, content_length: int = 2) -> None:
            self._url = response_url
            self.headers = {"Content-Length": str(content_length), "ETag": '"hash"'}

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def geturl(self) -> str:
            return self._url

        def read(self, _: int) -> bytes:
            return b"{}"

    class Opener:
        def __init__(self, response: Response) -> None:
            self.response = response

        def open(self, *_: object, **__: object) -> Response:
            return self.response

    def current_opener(*_: object) -> Opener:
        return Opener(Response(response_url=url))

    monkeypatch.setattr(fetch_module, "build_opener", current_opener)
    assert fetch_kpi_semantic_review_bytes(url, timeout_seconds=1) == (b"{}", '"hash"')

    def redirected_opener(*_: object) -> Opener:
        return Opener(Response(response_url=ORIGIN + "/redirected"))

    monkeypatch.setattr(fetch_module, "build_opener", redirected_opener)
    with pytest.raises(KpiSemanticReviewFetchError, match="redirected"):
        fetch_kpi_semantic_review_bytes(url, timeout_seconds=1)

    def oversized_opener(*_: object) -> Opener:
        return Opener(
            Response(
                response_url=url, content_length=fetch_module.MAX_KPI_SEMANTIC_EXPORT_BYTES + 1
            )
        )

    monkeypatch.setattr(fetch_module, "build_opener", oversized_opener)
    with pytest.raises(KpiSemanticReviewFetchError, match="byte bound"):
        fetch_kpi_semantic_review_bytes(url, timeout_seconds=1)


def test_local_authority_inputs_are_bounded(tmp_path: Path) -> None:
    authority = tmp_path / "authority.json"
    authority.write_bytes(b"1234")
    assert read_bounded_input(authority, maximum_bytes=4, label="authority") == b"1234"
    with pytest.raises(KpiSemanticReviewFetchError, match="byte bound"):
        read_bounded_input(authority, maximum_bytes=3, label="authority")
