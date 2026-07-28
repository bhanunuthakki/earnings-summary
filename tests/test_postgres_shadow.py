from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from pydantic import ValidationError

from provenance.postgres_shadow import (
    AnnParityGate,
    BatchLimits,
    ImportCheckpoint,
    ProjectionRecord,
    RetrievalHit,
    RetrievalKind,
    ShadowBatch,
    ShadowContractError,
    ShadowSeal,
    ShadowSourceBinding,
    build_retrieval_trace,
    build_source_binding,
    compare_projection_parity,
    compare_retrieval_parity,
    export_projection_batches,
    export_retrieval_trace_batches,
    finish_shadow_import,
    import_shadow_batch,
)

STAMP = datetime(2026, 7, 27, 12, tzinfo=UTC)
ZERO = "0" * 64


def _sha(character: str) -> str:
    return character * 64


def _binding(
    *,
    cutoff_at: datetime = STAMP,
    projection_sha256: str = _sha("e"),
) -> ShadowSourceBinding:
    return build_source_binding(
        stream_id="source-fact-publication-stream-v1",
        publication_sequence=17,
        publication_event_sha256=_sha("1"),
        ontology=ShadowSeal(seal_id="ontology-1", seal_sha256=_sha("a")),
        resolution=ShadowSeal(seal_id="resolution-1", seal_sha256=_sha("b")),
        research=ShadowSeal(seal_id="research-1", seal_sha256=_sha("c")),
        projection=ShadowSeal(
            seal_id="projection-1",
            seal_sha256=projection_sha256,
        ),
        cutoff_at=cutoff_at,
    )


def _record(index: int, *, pad: int = 0) -> ProjectionRecord:
    return ProjectionRecord.from_payload(
        record_id=f"fact-{index}",
        idempotency_key=f"projection:fact-{index}",
        canonical_payload_json=(
            f'{{"canonical_metric_cell_id":"fact-{index}","text":"{"x" * pad}"}}'
        ),
    )


def _trace(kind: RetrievalKind = "lexical"):
    return build_retrieval_trace(
        trace_id=f"trace-{kind}",
        idempotency_key=f"trace:{kind}",
        retrieval_kind=kind,
        query_sha256=_sha("4"),
        retrieval_config_sha256=_sha("5"),
        source_binding=_binding(),
        hits=(
            RetrievalHit(
                rank=1,
                hit_id="hit-1",
                payload_sha256=_sha("6"),
                score="0.91",
            ),
            RetrievalHit(
                rank=2,
                hit_id="hit-2",
                payload_sha256=_sha("7"),
                score="0.80",
            ),
        ),
    )


def _import(
    checkpoint: ImportCheckpoint,
    batch_index: int,
    batches: tuple[ShadowBatch, ...],
):
    return import_shadow_batch(
        checkpoint,
        batches[batch_index],
        expected_checkpoint_sha256=checkpoint.checkpoint_sha256,
    )


def test_projection_replay_is_exactly_once() -> None:
    batches = export_projection_batches(
        _binding(),
        tuple(_record(index) for index in range(5)),
        limits=BatchLimits(max_rows=2, max_bytes=4_096),
    )
    checkpoint = ImportCheckpoint.initial(
        source_binding=_binding(),
        stream_kind="canonical_projection",
    )

    for index in range(len(batches)):
        result = _import(checkpoint, index, batches)
        checkpoint = result.checkpoint

    completed = finish_shadow_import(checkpoint)
    replay = _import(checkpoint, len(batches) - 1, batches)

    assert completed.admitted is True
    assert len(checkpoint.applied_records) == 5
    assert replay.status == "duplicate"
    assert replay.checkpoint == checkpoint


def test_reordered_batches_buffer_then_apply_contiguously() -> None:
    batches = export_projection_batches(
        _binding(),
        tuple(_record(index) for index in range(4)),
        limits=BatchLimits(max_rows=2, max_bytes=4_096),
    )
    checkpoint = ImportCheckpoint.initial(
        source_binding=_binding(),
        stream_kind="canonical_projection",
    )

    buffered = _import(checkpoint, 1, batches)
    applied = _import(buffered.checkpoint, 0, batches)

    assert buffered.status == "buffered"
    assert buffered.checkpoint.applied_through_sequence == 0
    assert applied.status == "applied"
    assert applied.checkpoint.applied_through_sequence == 4
    assert finish_shadow_import(applied.checkpoint).admitted is True


def test_missing_batch_range_fails_closed() -> None:
    batches = export_projection_batches(
        _binding(),
        tuple(_record(index) for index in range(4)),
        limits=BatchLimits(max_rows=2, max_bytes=4_096),
    )
    checkpoint = ImportCheckpoint.initial(
        source_binding=_binding(),
        stream_kind="canonical_projection",
    )

    checkpoint = _import(checkpoint, 1, batches).checkpoint
    report = finish_shadow_import(checkpoint)

    assert report.admitted is False
    assert report.divergences[0].reason_code == "missing_batch_range"


def test_tampered_batch_is_rejected_before_state_changes() -> None:
    batch = export_projection_batches(
        _binding(),
        (_record(1),),
        limits=BatchLimits(max_rows=2, max_bytes=4_096),
    )[0]
    tampered_record = batch.records[0].model_copy(
        update={"canonical_payload_json": '{"tampered":true}'}
    )
    tampered_batch = batch.model_copy(update={"records": (tampered_record,)})
    checkpoint = ImportCheckpoint.initial(
        source_binding=_binding(),
        stream_kind="canonical_projection",
    )

    with pytest.raises(ShadowContractError) as error:
        import_shadow_batch(
            checkpoint,
            tampered_batch,
            expected_checkpoint_sha256=checkpoint.checkpoint_sha256,
        )

    assert error.value.report.divergences[0].reason_code == "batch_tampered"
    assert checkpoint.applied_through_sequence == 0


@pytest.mark.parametrize(
    "foreign_binding",
    (
        _binding(projection_sha256=_sha("f")),
        _binding(cutoff_at=STAMP + timedelta(seconds=1)),
    ),
)
def test_wrong_seal_or_cutoff_is_rejected(
    foreign_binding: ShadowSourceBinding,
) -> None:
    batch = export_projection_batches(
        foreign_binding,
        (_record(1),),
        limits=BatchLimits(max_rows=2, max_bytes=4_096),
    )[0]
    checkpoint = ImportCheckpoint.initial(
        source_binding=_binding(),
        stream_kind="canonical_projection",
    )

    with pytest.raises(ShadowContractError) as error:
        import_shadow_batch(
            checkpoint,
            batch,
            expected_checkpoint_sha256=checkpoint.checkpoint_sha256,
        )

    assert error.value.report.divergences[0].reason_code == "source_binding_mismatch"


def test_checkpoint_compare_and_swap_is_required() -> None:
    batch = export_projection_batches(
        _binding(),
        (_record(1),),
        limits=BatchLimits(max_rows=2, max_bytes=4_096),
    )[0]
    checkpoint = ImportCheckpoint.initial(
        source_binding=_binding(),
        stream_kind="canonical_projection",
    )

    with pytest.raises(ShadowContractError) as error:
        import_shadow_batch(
            checkpoint,
            batch,
            expected_checkpoint_sha256=_sha("f"),
        )

    assert error.value.report.divergences[0].reason_code == "checkpoint_cas_mismatch"


def test_batch_bounds_are_hard_caps() -> None:
    with pytest.raises(ValidationError):
        BatchLimits(max_rows=1_001, max_bytes=4_096)
    with pytest.raises(ValueError, match="record_exceeds_batch_byte_cap"):
        export_projection_batches(
            _binding(),
            (_record(1, pad=4_096),),
            limits=BatchLimits(max_rows=2, max_bytes=1_024),
        )

    batches = export_projection_batches(
        _binding(),
        tuple(_record(index, pad=50) for index in range(5)),
        limits=BatchLimits(max_rows=2, max_bytes=1_024),
    )

    assert all(batch.row_count <= 2 for batch in batches)
    assert all(batch.payload_bytes <= 1_024 for batch in batches)


def test_retrieval_traces_use_the_same_resumable_transport() -> None:
    trace = _trace()
    batches = export_retrieval_trace_batches(
        _binding(),
        (trace,),
        limits=BatchLimits(max_rows=1, max_bytes=8_192),
    )
    checkpoint = ImportCheckpoint.initial(
        source_binding=_binding(),
        stream_kind="retrieval_trace",
    )

    result = _import(checkpoint, 0, batches)

    assert result.status == "applied"
    assert finish_shadow_import(result.checkpoint).admitted is True
    assert result.checkpoint.applied_records[0].payload_sha256 == trace.trace_sha256


def test_projection_parity_is_exact_and_reports_mismatch() -> None:
    report = compare_projection_parity(
        authoritative_binding=_binding(),
        shadow_binding=_binding(),
        authoritative_records=(_record(1), _record(2)),
        shadow_records=(_record(1), _record(3)),
    )

    assert report.admitted is False
    assert report.parity_mode == "fact_exact"
    assert report.divergences[0].reason_code == "exact_projection_mismatch"


@pytest.mark.parametrize("kind", ("fact", "lexical"))
def test_fact_and_lexical_retrieval_require_exact_parity(kind: str) -> None:
    retrieval_kind = cast(RetrievalKind, kind)
    authoritative = _trace(retrieval_kind)
    shadow = build_retrieval_trace(
        trace_id=f"trace-{kind}-shadow",
        idempotency_key=f"trace:{kind}:shadow",
        retrieval_kind=retrieval_kind,
        query_sha256=authoritative.query_sha256,
        retrieval_config_sha256=authoritative.retrieval_config_sha256,
        source_binding=_binding(),
        hits=(
            RetrievalHit(
                rank=1,
                hit_id="different-hit",
                payload_sha256=_sha("8"),
                score="0.99",
            ),
        ),
    )

    report = compare_retrieval_parity(authoritative, shadow)

    assert report.admitted is False
    assert report.parity_mode == f"{kind}_exact"
    assert report.divergences[0].reason_code == "exact_retrieval_mismatch"


def test_ann_parity_is_eval_gated_not_result_identity() -> None:
    authoritative = _trace("ann")
    shadow = build_retrieval_trace(
        trace_id="trace-ann-shadow",
        idempotency_key="trace:ann:shadow",
        retrieval_kind="ann",
        query_sha256=authoritative.query_sha256,
        retrieval_config_sha256=authoritative.retrieval_config_sha256,
        source_binding=_binding(),
        hits=(
            RetrievalHit(
                rank=1,
                hit_id="different-hit",
                payload_sha256=_sha("8"),
                score="0.83",
            ),
        ),
    )
    gate = AnnParityGate(
        eval_run_id="eval-1",
        eval_config_sha256=_sha("9"),
        authoritative_trace_sha256=authoritative.trace_sha256,
        shadow_trace_sha256=shadow.trace_sha256,
        source_binding_sha256=authoritative.source_binding_sha256,
        cutoff_at=authoritative.cutoff_at,
        recall_at_k_ppm=970_000,
        minimum_recall_at_k_ppm=950_000,
        evaluated_query_count=100,
    )

    missing_gate = compare_retrieval_parity(authoritative, shadow)
    passed = compare_retrieval_parity(authoritative, shadow, ann_gate=gate)

    assert missing_gate.admitted is False
    assert missing_gate.divergences[0].reason_code == "ann_eval_gate_required"
    assert passed.admitted is True
    assert passed.parity_mode == "ann_eval_gated"


def test_source_binding_rejects_noncanonical_initial_cursor() -> None:
    with pytest.raises(ValueError, match="initial publication cursor"):
        build_source_binding(
            stream_id="source-fact-publication-stream-v1",
            publication_sequence=0,
            publication_event_sha256=_sha("1"),
            ontology=ShadowSeal(seal_id="ontology-1", seal_sha256=_sha("a")),
            resolution=ShadowSeal(seal_id="resolution-1", seal_sha256=_sha("b")),
            research=ShadowSeal(seal_id="research-1", seal_sha256=_sha("c")),
            projection=ShadowSeal(seal_id="projection-1", seal_sha256=_sha("d")),
            cutoff_at=STAMP,
        )

    assert ZERO == "0" * 64
