from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import execution.fetch_windows_kpi_semantic_review as fetch_module
from execution.fetch_windows_kpi_semantic_review import (
    KpiSemanticReviewFetchError,
    fetch_kpi_semantic_review_bytes,
    persist_kpi_semantic_review_export,
    persist_kpi_semantic_review_manifest,
    read_bounded_input,
    validate_semantic_review_manifest,
    validate_semantic_review_partition,
    validate_semantic_review_partition_set,
)
from execution.fetch_windows_review_bundle import WindowsReviewPins
from operations.kpi_semantic_review_export import (
    MAX_KPI_SEMANTIC_EXPORT_BYTES,
    KpiSemanticReviewArtifactPointer,
    KpiSemanticReviewExport,
    KpiSemanticReviewTickerManifest,
    encoded_kpi_semantic_review_export,
    encoded_kpi_semantic_review_ticker_manifest,
    payload_sha256,
    seal_kpi_semantic_review_export,
)
from operations.review_bundle import OperationsReviewBundle
from pipeline.kpi_semantic_review import (
    KpiSemanticReviewBatch,
    KpiSemanticReviewItem,
    KpiSemanticReviewState,
)

NOW = datetime(2026, 8, 30, tzinfo=UTC)
ORIGIN = "https://windows.example.ts.net"
CODE_SHA = "a" * 64
DATABASE_SHA = "b" * 64
SCHEMA_REVISION = "0036_add_data_coverage_dispositions"
STATE = KpiSemanticReviewState.SOURCE_DOCUMENT_MISSING


def _item(fact_id: int, *, ticker: str = "NU") -> KpiSemanticReviewItem:
    return KpiSemanticReviewItem(
        ticker=ticker,
        kpi_definition_id=1,
        kpi_name="Customers",
        scope_reasons=("report_reference",),
        fact_id=fact_id,
        period_end="2026-06-30",
        fiscal_period_type="Q2",
        value="100",
        unit="count",
        context_status=None,
        legacy_source_doc_id=None,
        source_doc_id=None,
        source_type=None,
        doc_type=None,
        source_content_sha256=None,
        source_observation_version=None,
        source_period_end=None,
        state=STATE,
        state_reason_code=STATE.value,
        quarantine_reason_code=STATE.value,
        evidence_candidate_total=0,
        evidence_candidates_truncated=False,
        evidence_search_incomplete=False,
        evidence_search_reason_codes=(),
        evidence_candidates=(),
    )


def _batch(
    fact_ids: tuple[int, ...], *, truncated: bool, ticker: str = "NU"
) -> KpiSemanticReviewBatch:
    items = tuple(_item(fact_id, ticker=ticker) for fact_id in fact_ids)
    state_counts = {STATE.value: len(items)} if items else {}
    raw: dict[str, object] = {
        "schema_version": "kpi_semantic_review.v3",
        "user_id": "owner",
        "ticker": ticker,
        "observed_at": NOW,
        "limit": 2,
        "total_items": len(items),
        "truncated": truncated,
        "state_counts": state_counts,
        "items": items,
    }
    return KpiSemanticReviewBatch.model_validate({**raw, "content_sha256": payload_sha256(raw)})


def _partition(
    fact_ids: tuple[int, ...],
    *,
    ordinal: int,
    after_fact_id: int,
    next_after_fact_id: int | None,
    ticker: str = "NU",
) -> KpiSemanticReviewExport:
    return seal_kpi_semantic_review_export(
        review=_batch(fact_ids, truncated=next_after_fact_id is not None, ticker=ticker),
        code_instance_sha256=CODE_SHA,
        database_instance_sha256=DATABASE_SHA,
        schema_revision=SCHEMA_REVISION,
        partition_ordinal=ordinal,
        after_fact_id=after_fact_id,
        next_after_fact_id=next_after_fact_id,
    )


def _partitions() -> tuple[KpiSemanticReviewExport, ...]:
    return (
        _partition((1, 2), ordinal=0, after_fact_id=0, next_after_fact_id=2),
        _partition((3,), ordinal=1, after_fact_id=2, next_after_fact_id=None),
    )


def _manifest(
    partitions: tuple[KpiSemanticReviewExport, ...] | None = None,
) -> KpiSemanticReviewTickerManifest:
    exports = partitions or _partitions()
    pointers = tuple(
        KpiSemanticReviewArtifactPointer(
            ticker=export.ticker,
            ordinal=export.partition_ordinal,
            content_sha256=export.content_sha256,
            byte_size=len(encoded_kpi_semantic_review_export(export)),
            item_count=export.review.total_items,
            after_fact_id=export.after_fact_id,
            next_after_fact_id=export.next_after_fact_id,
        )
        for export in exports
    )
    raw: dict[str, object] = {
        "schema_version": "windows_kpi_semantic_review_ticker_manifest.v1",
        "observed_at": NOW,
        "user_id": "owner",
        "ticker": "NU",
        "code_instance_sha256": CODE_SHA,
        "database_instance_sha256": DATABASE_SHA,
        "schema_revision": SCHEMA_REVISION,
        "total_items": sum(pointer.item_count for pointer in pointers),
        "state_counts": {STATE.value: sum(pointer.item_count for pointer in pointers)},
        "partitions": pointers,
    }
    return KpiSemanticReviewTickerManifest.model_validate(
        {**raw, "content_sha256": payload_sha256(raw)}
    )


def _bundle() -> OperationsReviewBundle:
    value = SimpleNamespace(
        identity=SimpleNamespace(
            code_instance_sha256=CODE_SHA,
            database_instance_sha256=DATABASE_SHA,
        ),
        schema_revision=SimpleNamespace(actual_heads=(SCHEMA_REVISION,)),
    )
    return cast(OperationsReviewBundle, value)


def _pins() -> WindowsReviewPins:
    return WindowsReviewPins.model_construct()


def _install_bundle_validator(monkeypatch: pytest.MonkeyPatch) -> None:
    def validate_bundle(
        payload: bytes,
        *,
        origin: str,
        now: datetime,
        max_age: timedelta,
        pins: WindowsReviewPins,
    ) -> OperationsReviewBundle:
        assert payload == b"trusted-bundle"
        assert origin == ORIGIN
        assert now.tzinfo is not None
        assert max_age == timedelta(minutes=20)
        assert pins is not None
        return _bundle()

    monkeypatch.setattr(fetch_module, "validate_bundle", validate_bundle)


def test_manifest_requires_independent_pins_exact_etag_and_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_bundle_validator(monkeypatch)
    manifest = _manifest()

    validated = validate_semantic_review_manifest(
        encoded_kpi_semantic_review_ticker_manifest(manifest),
        response_etag=f'"{manifest.content_sha256}"',
        ticker="NU",
        expected_user_id="owner",
        now=NOW + timedelta(minutes=1),
        max_age=timedelta(minutes=20),
        review_bundle_payload=b"trusted-bundle",
        origin=ORIGIN,
        pins=_pins(),
    )

    assert validated == manifest


@pytest.mark.parametrize(
    ("field", "message"),
    (
        ("code_instance_sha256", "code authority"),
        ("database_instance_sha256", "database authority"),
        ("schema_revision", "schema authority"),
    ),
)
def test_manifest_rejects_authority_drift(
    monkeypatch: pytest.MonkeyPatch, field: str, message: str
) -> None:
    _install_bundle_validator(monkeypatch)
    manifest = _manifest()
    raw = manifest.model_dump(mode="json", exclude={"content_sha256"})
    raw[field] = "f" * 64 if field.endswith("sha256") else "other-head"
    drifted = KpiSemanticReviewTickerManifest.model_validate(
        {**raw, "content_sha256": payload_sha256(raw)}
    )

    with pytest.raises(KpiSemanticReviewFetchError, match=message):
        validate_semantic_review_manifest(
            encoded_kpi_semantic_review_ticker_manifest(drifted),
            response_etag=f'"{drifted.content_sha256}"',
            ticker="NU",
            expected_user_id="owner",
            now=NOW,
            max_age=timedelta(minutes=20),
            review_bundle_payload=b"trusted-bundle",
            origin=ORIGIN,
            pins=_pins(),
        )


def test_manifest_rejects_legacy_payload_etag_owner_ticker_and_duplicate_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_bundle_validator(monkeypatch)
    manifest = _manifest()
    with pytest.raises(KpiSemanticReviewFetchError, match="schema validation"):
        validate_semantic_review_manifest(
            json.dumps(
                {
                    "schema_version": "windows_kpi_semantic_review_export.v1",
                    "ticker": "NU",
                }
            ).encode(),
            response_etag='"legacy"',
            ticker="NU",
            expected_user_id="owner",
            now=NOW,
            max_age=timedelta(minutes=20),
            review_bundle_payload=b"trusted-bundle",
            origin=ORIGIN,
            pins=_pins(),
        )
    with pytest.raises(KpiSemanticReviewFetchError, match="ETag"):
        validate_semantic_review_manifest(
            encoded_kpi_semantic_review_ticker_manifest(manifest),
            response_etag='"wrong"',
            ticker="NU",
            expected_user_id="owner",
            now=NOW,
            max_age=timedelta(minutes=20),
            review_bundle_payload=b"trusted-bundle",
            origin=ORIGIN,
            pins=_pins(),
        )
    with pytest.raises(KpiSemanticReviewFetchError, match="owner"):
        validate_semantic_review_manifest(
            encoded_kpi_semantic_review_ticker_manifest(manifest),
            response_etag=f'"{manifest.content_sha256}"',
            ticker="NU",
            expected_user_id="other-owner",
            now=NOW,
            max_age=timedelta(minutes=20),
            review_bundle_payload=b"trusted-bundle",
            origin=ORIGIN,
            pins=_pins(),
        )
    with pytest.raises(KpiSemanticReviewFetchError, match="ticker"):
        validate_semantic_review_manifest(
            encoded_kpi_semantic_review_ticker_manifest(manifest),
            response_etag=f'"{manifest.content_sha256}"',
            ticker="MELI",
            expected_user_id="owner",
            now=NOW,
            max_age=timedelta(minutes=20),
            review_bundle_payload=b"trusted-bundle",
            origin=ORIGIN,
            pins=_pins(),
        )

    raw = manifest.model_dump(mode="json", exclude={"content_sha256"})
    pointers_raw = raw["partitions"]
    assert isinstance(pointers_raw, list)
    pointers = cast(list[dict[str, object]], pointers_raw)
    pointers[1]["content_sha256"] = pointers[0]["content_sha256"]
    duplicate = KpiSemanticReviewTickerManifest.model_validate(
        {**raw, "content_sha256": payload_sha256(raw)}
    )
    with pytest.raises(KpiSemanticReviewFetchError, match="repeats"):
        validate_semantic_review_manifest(
            encoded_kpi_semantic_review_ticker_manifest(duplicate),
            response_etag=f'"{duplicate.content_sha256}"',
            ticker="NU",
            expected_user_id="owner",
            now=NOW,
            max_age=timedelta(minutes=20),
            review_bundle_payload=b"trusted-bundle",
            origin=ORIGIN,
            pins=_pins(),
        )


def test_partition_validation_rejects_tamper_etag_size_and_cross_identity() -> None:
    partitions = _partitions()
    manifest = _manifest(partitions)
    pointer = manifest.partitions[0]
    export = partitions[0]
    payload = encoded_kpi_semantic_review_export(export)

    assert (
        validate_semantic_review_partition(
            payload,
            response_etag=f'"{pointer.content_sha256}"',
            manifest=manifest,
            pointer=pointer,
        )
        == export
    )
    with pytest.raises(KpiSemanticReviewFetchError, match="byte size"):
        validate_semantic_review_partition(
            payload + b" ",
            response_etag=f'"{pointer.content_sha256}"',
            manifest=manifest,
            pointer=pointer,
        )
    with pytest.raises(KpiSemanticReviewFetchError, match="ETag"):
        validate_semantic_review_partition(
            payload,
            response_etag='"wrong"',
            manifest=manifest,
            pointer=pointer,
        )
    tampered: dict[str, object] = export.model_dump(mode="json")
    tampered["ticker"] = "MELI"
    tampered_payload = json.dumps(tampered).encode()
    tampered_pointer = pointer.model_copy(update={"byte_size": len(tampered_payload)})
    with pytest.raises(KpiSemanticReviewFetchError, match="schema validation"):
        validate_semantic_review_partition(
            tampered_payload,
            response_etag=f'"{pointer.content_sha256}"',
            manifest=manifest,
            pointer=tampered_pointer,
        )


def test_partition_set_rejects_missing_out_of_order_and_state_mismatch() -> None:
    partitions = _partitions()
    manifest = _manifest(partitions)
    validate_semantic_review_partition_set(manifest, partitions)

    with pytest.raises(KpiSemanticReviewFetchError, match="incomplete"):
        validate_semantic_review_partition_set(manifest, partitions[:1])
    with pytest.raises(KpiSemanticReviewFetchError, match="out of order"):
        validate_semantic_review_partition_set(manifest, tuple(reversed(partitions)))

    raw = manifest.model_dump(mode="json", exclude={"content_sha256"})
    raw["state_counts"] = {"different_state": manifest.total_items}
    mismatched = KpiSemanticReviewTickerManifest.model_validate(
        {**raw, "content_sha256": payload_sha256(raw)}
    )
    with pytest.raises(KpiSemanticReviewFetchError, match="totals"):
        validate_semantic_review_partition_set(mismatched, partitions)


def test_persistence_is_content_addressed_and_conflicts_fail_closed(tmp_path: Path) -> None:
    manifest = _manifest()
    export = _partitions()[0]
    output = tmp_path / ".tmp"

    manifest_path = persist_kpi_semantic_review_manifest(manifest, output_root=output)
    partition_path = persist_kpi_semantic_review_export(export, output_root=output)
    assert manifest_path == output / "manifests" / f"{manifest.content_sha256}.json"
    assert partition_path == output / "partitions" / f"{export.content_sha256}.json"
    assert (
        KpiSemanticReviewTickerManifest.model_validate_json(manifest_path.read_bytes()) == manifest
    )
    assert KpiSemanticReviewExport.model_validate_json(partition_path.read_bytes()) == export

    partition_path.write_text("conflict", encoding="utf-8")
    with pytest.raises(KpiSemanticReviewFetchError, match="conflicts"):
        persist_kpi_semantic_review_export(export, output_root=output)
    partition_path.write_bytes(b"x" * (MAX_KPI_SEMANTIC_EXPORT_BYTES + 1))
    with pytest.raises(KpiSemanticReviewFetchError, match="byte bound"):
        persist_kpi_semantic_review_export(export, output_root=output)


def test_http_fetch_rejects_nonexact_urls_redirects_and_declared_oversize(
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
    assert fetch_kpi_semantic_review_bytes(url, timeout_seconds=1, maximum_bytes=10) == (
        b"{}",
        '"hash"',
    )

    with pytest.raises(KpiSemanticReviewFetchError, match="exact"):
        fetch_kpi_semantic_review_bytes(url + "?token=secret", timeout_seconds=1, maximum_bytes=10)

    def redirected_opener(*_: object) -> Opener:
        return Opener(Response(response_url=ORIGIN + "/redirected"))

    monkeypatch.setattr(fetch_module, "build_opener", redirected_opener)
    with pytest.raises(KpiSemanticReviewFetchError, match="redirected"):
        fetch_kpi_semantic_review_bytes(url, timeout_seconds=1, maximum_bytes=10)

    def oversized_opener(*_: object) -> Opener:
        return Opener(Response(response_url=url, content_length=11))

    monkeypatch.setattr(fetch_module, "build_opener", oversized_opener)
    with pytest.raises(KpiSemanticReviewFetchError, match="byte bound"):
        fetch_kpi_semantic_review_bytes(url, timeout_seconds=1, maximum_bytes=10)


def test_local_authority_inputs_are_bounded(tmp_path: Path) -> None:
    authority = tmp_path / "authority.json"
    authority.write_bytes(b"1234")
    assert read_bounded_input(authority, maximum_bytes=4, label="authority") == b"1234"
    with pytest.raises(KpiSemanticReviewFetchError, match="byte bound"):
        read_bounded_input(authority, maximum_bytes=3, label="authority")
