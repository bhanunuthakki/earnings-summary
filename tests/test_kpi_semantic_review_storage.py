"""Isolated synthetic storage fixtures; no application, network or database access."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypedDict, cast

import pytest

import operations.kpi_semantic_review_export as storage
from pipeline.kpi_semantic_review import (
    KpiSemanticReviewBatch,
    KpiSemanticReviewItem,
    KpiSemanticReviewState,
)

NOW = datetime(2026, 10, 10, tzinfo=UTC)


class ExportChange(TypedDict, total=False):
    owner: str
    evidence_version: str
    value: str
    code: str
    database: str
    schema: str


def review_export(
    observed_at: datetime = NOW,
    *,
    owner: str = "synthetic-owner",
    evidence_version: str = "synthetic-observation-1",
    value: str = "12.3",
    code: str = "b" * 64,
    database: str = "c" * 64,
    schema: str = "synthetic-schema",
    ordinal: int = 0,
    after: int = 0,
    fact_id: int = 1,
    truncated: bool = False,
) -> storage.KpiSemanticReviewExport:
    item = KpiSemanticReviewItem(
        ticker="SYN",
        kpi_definition_id=1,
        kpi_name="Synthetic customers",
        scope_reasons=("synthetic-fixture",),
        fact_id=fact_id,
        period_end="2026-09-30",
        fiscal_period_type="quarterly",
        value=value,
        unit="count",
        context_status=None,
        source_doc_id=1,
        source_type="ir_doc",
        doc_type="ir_presentation",
        source_content_sha256="a" * 64,
        source_observation_version=evidence_version,
        source_period_end="2026-09-30",
        state=KpiSemanticReviewState.NEEDS_LEDGER_CAPTURE,
        state_reason_code="synthetic-ledger-needed",
        quarantine_reason_code="synthetic-ledger-needed",
        evidence_candidate_total=0,
        evidence_candidates_truncated=False,
        evidence_search_incomplete=False,
        evidence_search_reason_codes=(),
        evidence_candidates=(),
    )
    payload = {
        "schema_version": "kpi_semantic_review.v3",
        "user_id": owner,
        "ticker": "SYN",
        "observed_at": observed_at,
        "limit": 1000,
        "total_items": 1,
        "truncated": truncated,
        "state_counts": {"needs_ledger_capture": 1},
        "items": (item,),
    }
    batch = KpiSemanticReviewBatch.model_validate(
        {**payload, "content_sha256": storage.payload_sha256(payload)}
    )
    return storage.seal_kpi_semantic_review_export(
        review=batch,
        code_instance_sha256=code,
        database_instance_sha256=database,
        schema_revision=schema,
        partition_ordinal=ordinal,
        after_fact_id=after,
        next_after_fact_id=fact_id if truncated else None,
    )


def publish(root: Path, export: storage.KpiSemanticReviewExport) -> None:
    storage.publish_kpi_semantic_review_exports(
        root=root, exports=(export,), expected_tickers=("SYN",)
    )


def load(root: Path, at: datetime = NOW) -> tuple[storage.KpiSemanticReviewExport, bytes]:
    return storage.load_current_kpi_semantic_review_export(
        root=root, ticker="SYN", now=at, max_age=timedelta(minutes=20)
    )


def test_observation_only_reuses_blob_and_preserves_fresh_exact_v2(tmp_path: Path) -> None:
    root = tmp_path / "exports"
    first = review_export()
    publish(root, first)
    blobs = tuple((root / "artifacts" / "semantic").glob("*.json"))
    assert len(blobs) == 1
    original = blobs[0].read_bytes()
    second = review_export(NOW + timedelta(minutes=10))
    publish(root, second)
    assert tuple((root / "artifacts" / "semantic").glob("*.json")) == blobs
    assert blobs[0].read_bytes() == original
    assert first.content_sha256 != second.content_sha256
    assert first.review.content_sha256 != second.review.content_sha256
    loaded, wire = load(root, second.observed_at)
    assert loaded == second
    assert wire == storage.encoded_kpi_semantic_review_export(second)
    index = storage.KpiSemanticReviewExportIndex.model_validate_json(
        (root / "latest.json").read_bytes()
    )
    assert index.observed_at == second.observed_at
    preserved = storage.kpi_semantic_review_storage_preservation(root=root)
    assert preserved.previous_capture_index_sha256 is not None
    assert preserved.current_capture_index_sha256 == index.content_sha256
    assert len(preserved.semantic_blob_paths) == 1
    receipt = storage.KpiSemanticReviewStorageObservation.model_validate_json(
        (root / "storage-observation.json").read_bytes()
    )
    assert receipt.metric == "publication_created_logical_bytes"
    assert receipt.new_blob_count == 0
    assert receipt.reused_blob_count == 1
    assert receipt.served_wire_bytes == len(wire)
    assert receipt == storage.kpi_semantic_review_storage_observation(root=root)


@pytest.mark.parametrize(
    "change",
    [
        {"owner": "synthetic-other-owner"},
        {"evidence_version": "synthetic-observation-2"},
        {"value": "12.4"},
        {"code": "d" * 64},
        {"database": "e" * 64},
        {"schema": "synthetic-schema-2"},
    ],
)
def test_content_and_authority_changes_create_new_blob(
    tmp_path: Path, change: ExportChange
) -> None:
    root = tmp_path / "exports"
    publish(root, review_export())
    publish(root, review_export(NOW + timedelta(minutes=10), **change))
    assert len(tuple((root / "artifacts" / "semantic").glob("*.json"))) == 2


def test_only_two_envelope_clocks_and_derived_hashes_are_excluded() -> None:
    export = review_export()
    components = storage.prepare_kpi_semantic_review_storage(export)
    blob = json.loads(components.blob_bytes)
    expected = export.model_dump(mode="json")
    expected.pop("observed_at")
    expected.pop("content_sha256")
    review = export.review.model_dump(mode="json")
    review.pop("observed_at")
    review.pop("content_sha256")
    expected["review"] = review
    assert blob["payload"] == expected
    assert (
        storage.prepare_kpi_semantic_review_storage(review_export(fact_id=2)).semantic_sha256
        != components.semantic_sha256
    )
    assert (
        storage.prepare_kpi_semantic_review_storage(
            review_export(ordinal=1, after=1, fact_id=2)
        ).semantic_sha256
        != components.semantic_sha256
    )


def test_missing_corrupt_blob_and_missing_capture_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "exports"
    publish(root, review_export())
    blob = next((root / "artifacts" / "semantic").glob("*.json"))
    original = blob.read_bytes()
    blob.unlink()
    with pytest.raises(storage.KpiSemanticReviewExportError, match="unavailable"):
        load(root)
    blob.write_bytes(b"corrupt")
    with pytest.raises(storage.KpiSemanticReviewExportError, match="invalid"):
        load(root)
    blob.write_bytes(original)
    next((root / "artifacts" / "captures").glob("*.json")).unlink()
    with pytest.raises(storage.KpiSemanticReviewExportError, match="capture"):
        load(root)


def test_failed_pointer_keeps_prior_and_retry_reuses_original_accounting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "exports"
    first = review_export()
    publish(root, first)
    original = (root / "latest.json").read_bytes()
    atomic_write = cast(Callable[[Path, bytes], None], getattr(storage, "_atomic_write"))
    second = review_export(NOW + timedelta(minutes=10), value="12.4")

    def fail_pointer(path: Path, payload: bytes) -> None:
        if path == root / "latest.json":
            raise OSError("synthetic failure")
        atomic_write(path, payload)

    monkeypatch.setattr(storage, "_atomic_write", fail_pointer)
    with pytest.raises(storage.KpiSemanticReviewExportError):
        publish(root, second)
    assert (root / "latest.json").read_bytes() == original
    assert load(root)[0] == first
    assert not tuple(root.rglob("*.tmp"))
    capture_paths = tuple((root / "artifacts" / "captures").glob("*.json"))
    captures = {path: path.read_bytes() for path in capture_paths}
    monkeypatch.setattr(storage, "_atomic_write", atomic_write)
    publish(root, second)
    assert load(root, second.observed_at)[0] == second
    assert all(path.read_bytes() == payload for path, payload in captures.items())
    receipt = storage.kpi_semantic_review_storage_observation(root=root)
    assert receipt.new_blob_count == 1
    publish(root, second)
    assert storage.kpi_semantic_review_storage_observation(root=root) == receipt


def test_postcommit_optional_metadata_failure_returns_valid_new_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "exports"
    publish(root, review_export())
    atomic_write = cast(Callable[[Path, bytes], None], getattr(storage, "_atomic_write"))

    def fail_receipt(path: Path, payload: bytes) -> None:
        if path == root / "storage-observation.json":
            raise OSError("synthetic failure")
        atomic_write(path, payload)

    monkeypatch.setattr(storage, "_atomic_write", fail_receipt)
    second = review_export(NOW + timedelta(minutes=10))
    publish(root, second)
    assert load(root, second.observed_at)[0] == second
    assert "publication_committed_metadata_pending" in caplog.text
    assert (
        storage.kpi_semantic_review_storage_observation(root=root).observed_at == second.observed_at
    )
    assert (
        storage.kpi_semantic_review_storage_preservation(root=root).previous_capture_index_sha256
        is not None
    )


def test_reader_uses_one_snapshot_during_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "exports"
    first = review_export()
    publish(root, first)
    second = review_export(NOW + timedelta(minutes=10))
    read_capture = cast(
        Callable[[Path, storage.KpiSemanticReviewExportIndex], object],
        getattr(storage, "_read_capture"),
    )
    changed = False

    def interleave(path: Path, index: storage.KpiSemanticReviewExportIndex) -> object:
        nonlocal changed
        result = read_capture(path, index)
        if not changed:
            changed = True
            publish(root, second)
        return result

    monkeypatch.setattr(storage, "_read_capture", interleave)
    assert load(root, second.observed_at)[0] == first
    assert load(root, second.observed_at)[0] == second


def test_legacy_v2_is_readable_and_preserved_as_tested_predecessor(tmp_path: Path) -> None:
    root = tmp_path / "exports"
    first = review_export()
    publish(root, first)
    artifact = root / "artifacts" / f"{first.content_sha256}.json"
    artifact.write_bytes(storage.encoded_kpi_semantic_review_export(first))
    for path in (root / "artifacts" / "captures").glob("*.json"):
        path.unlink()
    assert load(root)[0] == first
    old_wire = artifact.read_bytes()
    publish(root, review_export(NOW + timedelta(minutes=10)))
    assert artifact.read_bytes() == old_wire
    assert (
        artifact
        in storage.kpi_semantic_review_storage_preservation(root=root).legacy_artifact_paths
    )


def test_explicit_path_decoder_and_pure_components_need_no_fabricated_roster(
    tmp_path: Path,
) -> None:
    export = review_export()
    components = storage.prepare_kpi_semantic_review_storage(export)
    artifact = tmp_path / "artifacts" / f"{export.content_sha256}.json"
    artifact.parent.mkdir()
    artifact.write_bytes(components.wire_bytes)
    assert storage.decode_kpi_semantic_review_artifact(path=artifact) == (
        export,
        components.wire_bytes,
    )
    semantic = artifact.parent / "semantic"
    semantic.mkdir()
    blob = semantic / f"{components.semantic_sha256}.json"
    blob.write_bytes(components.blob_bytes)
    artifact.write_bytes(components.descriptor_bytes)
    assert storage.decode_kpi_semantic_review_artifact(path=artifact) == (
        export,
        components.wire_bytes,
    )
    artifact.write_bytes(b"corrupt")
    with pytest.raises(storage.KpiSemanticReviewExportError, match="invalid"):
        storage.decode_kpi_semantic_review_artifact(path=artifact)


def test_links_large_and_conflicting_blobs_are_refused(tmp_path: Path) -> None:
    root = tmp_path / "exports"
    first = review_export()
    publish(root, first)
    blob = next((root / "artifacts" / "semantic").glob("*.json"))
    original = blob.read_bytes()
    external = tmp_path / "synthetic-external.json"
    external.write_bytes(original)
    blob.unlink()
    blob.symlink_to(external)
    try:
        with pytest.raises(storage.KpiSemanticReviewExportError, match="link"):
            load(root)
        assert external.read_bytes() == original
    finally:
        blob.unlink()
    os.link(external, blob)
    try:
        with pytest.raises(storage.KpiSemanticReviewExportError, match="link"):
            load(root)
    finally:
        blob.unlink()
    blob.write_bytes(b"x" * (storage.MAX_KPI_SEMANTIC_EXPORT_BYTES + 1))
    with pytest.raises(storage.KpiSemanticReviewExportError, match="byte bound"):
        load(root)
    blob.write_bytes(original)
    original_index = (root / "latest.json").read_bytes()
    blob.write_bytes(b"corrupt")
    with pytest.raises(storage.KpiSemanticReviewExportError):
        publish(root, review_export(NOW + timedelta(minutes=10)))
    assert (root / "latest.json").read_bytes() == original_index
    blob.write_bytes(original)


def test_oversized_capture_is_refused_before_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "exports"
    publish(root, review_export())
    original = (root / "latest.json").read_bytes()
    monkeypatch.setattr(storage, "_LATEST_MAX_BYTES", 100)
    with pytest.raises(storage.KpiSemanticReviewExportError, match="byte bound"):
        publish(root, review_export(NOW + timedelta(minutes=10)))
    assert (root / "latest.json").read_bytes() == original


def test_exact_accounting_matches_new_immutable_files(tmp_path: Path) -> None:
    root = tmp_path / "exports"
    publish(root, review_export())
    observation = storage.kpi_semantic_review_storage_observation(root=root)
    paths = tuple((root / "artifacts").rglob("*.json"))
    assert observation.logical_bytes == sum(path.stat().st_size for path in paths)
    assert observation.files == len(paths)
    for minutes in (1, 2, 3):
        publish(root, review_export(NOW + timedelta(minutes=minutes)))
    preservation = storage.kpi_semantic_review_storage_preservation(root=root)
    assert len(preservation.capture_paths) == 2
    assert len(preservation.descriptor_paths) == 2
    assert len(preservation.semantic_blob_paths) == 1
    # Earlier history remains physically present until a separately approved retention owner acts.
    assert len(tuple((root / "artifacts" / "captures").glob("*.json"))) == 4
    receipt = json.loads((root / "storage-observation.json").read_bytes())
    assert receipt["logical_bytes"] < observation.logical_bytes


def test_explicit_descriptor_link_and_wrong_name_are_refused(tmp_path: Path) -> None:
    export = review_export()
    components = storage.prepare_kpi_semantic_review_storage(export)
    artifacts = tmp_path / "artifacts"
    (artifacts / "semantic").mkdir(parents=True)
    blob = artifacts / "semantic" / f"{components.semantic_sha256}.json"
    blob.write_bytes(components.blob_bytes)
    wrong = artifacts / "wrong.json"
    wrong.write_bytes(components.descriptor_bytes)
    with pytest.raises(storage.KpiSemanticReviewExportError, match="path identity"):
        storage.decode_kpi_semantic_review_artifact(path=wrong)
    artifact = artifacts / f"{components.wire_sha256}.json"
    artifact.symlink_to(wrong)
    try:
        with pytest.raises(storage.KpiSemanticReviewExportError, match="link"):
            storage.decode_kpi_semantic_review_artifact(path=artifact)
        assert wrong.read_bytes() == components.descriptor_bytes
    finally:
        artifact.unlink()


def test_linked_parent_is_refused_without_writes(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    root = tmp_path / "exports"
    root.symlink_to(external, target_is_directory=True)
    try:
        with pytest.raises(storage.KpiSemanticReviewExportError, match="link"):
            publish(root, review_export())
        assert not tuple(external.iterdir())
    finally:
        root.unlink()


def test_partitioned_capture_preserves_cursor_chain_and_exact_wire(tmp_path: Path) -> None:
    root = tmp_path / "exports"
    first = review_export(fact_id=1, truncated=True)
    second = review_export(ordinal=1, after=1, fact_id=2)
    index = storage.publish_kpi_semantic_review_exports(
        root=root, exports=(second, first), expected_tickers=("SYN",)
    )
    assert index.total_items == 2
    for export in (first, second):
        loaded = storage.load_current_kpi_semantic_review_partition(
            root=root,
            ticker="SYN",
            content_sha256=export.content_sha256,
            now=NOW,
            max_age=timedelta(minutes=20),
        )
        assert loaded == (export, storage.encoded_kpi_semantic_review_export(export))
    with pytest.raises(storage.KpiSemanticReviewExportError, match="multiple"):
        load(root)
    old = (root / "latest.json").read_bytes()
    with pytest.raises(ValueError):
        storage.publish_kpi_semantic_review_exports(
            root=root, exports=(second,), expected_tickers=("SYN",)
        )
    assert (root / "latest.json").read_bytes() == old


def test_corrupt_predecessor_blocks_replacement_and_preservation(tmp_path: Path) -> None:
    root = tmp_path / "exports"
    publish(root, review_export())
    first_capture = next((root / "artifacts" / "captures").glob("*.json"))
    publish(root, review_export(NOW + timedelta(minutes=1)))
    original = (root / "latest.json").read_bytes()
    first_capture.write_bytes(b"corrupt")
    with pytest.raises(storage.KpiSemanticReviewExportError, match="previous"):
        storage.kpi_semantic_review_storage_preservation(root=root)
    with pytest.raises(storage.KpiSemanticReviewExportError, match="previous"):
        publish(root, review_export(NOW + timedelta(minutes=2)))
    assert (root / "latest.json").read_bytes() == original
    assert load(root, NOW + timedelta(minutes=1))[0].observed_at == NOW + timedelta(minutes=1)


def test_native_atomic_replace_failure_removes_own_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "exports"
    publish(root, review_export())
    old = (root / "latest.json").read_bytes()
    replace = Path.replace

    def fail_replace(path: Path, target: Path) -> Path:
        if target == root / "latest.json":
            raise OSError("synthetic replace failure")
        return replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(storage.KpiSemanticReviewExportError):
        publish(root, review_export(NOW + timedelta(minutes=1)))
    assert (root / "latest.json").read_bytes() == old
    assert not tuple(root.rglob("*.tmp"))


def test_postcommit_directory_sync_failure_does_not_claim_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "exports"
    first = review_export()
    publish(root, first)
    directory_sync = cast(Callable[[Path], None], getattr(storage, "_sync_directory"))

    def fail_root_sync(path: Path) -> None:
        if path == root:
            raise OSError("synthetic directory sync failure")
        directory_sync(path)

    monkeypatch.setattr(storage, "_sync_directory", fail_root_sync)
    second = review_export(NOW + timedelta(minutes=1))
    publish(root, second)
    assert load(root, second.observed_at)[0] == second
    assert "publication_committed_directory_sync_pending" in caplog.text
