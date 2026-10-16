"""Supporting issuers remain distinct sealed contexts; legacy review stays exact."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from pydantic import ValidationError

from dcf.input_evidence import SourceReadContext
from research.decision_brief import (
    MemoContextReview,
    MemoContextReviewV2,
    MemoEvidenceError,
    MemoSupportingContext,
    ReviewedMemoClaimV2,
    memo_reader_blocks,
    parse_memo_context_review,
    verify_memo_claim_population,
    verify_memo_composed_source_bytes,
    verify_memo_context_source_bytes,
    verify_memo_supporting_context,
)
from research.memo_model_evidence import MemoModelValue
from tests.test_decision_brief import STAMP
from tests.test_source_fact_repository import seed_foundation


def _review(**updates: object) -> dict[str, object]:
    return dict(
        artifact_id="memo",
        body_sha256="a" * 64,
        research_snapshot_id="onon-snapshot",
        claims=[
            dict(
                block_id="b" * 64,
                passage="Analyst inference: case.",
                kind="analyst_inference",
                rationale="Explicit analyst assessment.",
            )
        ],
        reviewed_section_ids=["company"],
        reviewer="analyst",
        reviewed_at=STAMP.isoformat(),
        rationale="Exact retained body and source review.",
        **updates,
    )


def test_legacy_roundtrip_remains_schema_free() -> None:
    original = MemoContextReview.model_validate(_review())
    parsed = parse_memo_context_review(original.model_dump_json())
    assert type(parsed) is MemoContextReview
    assert parsed.model_dump_json() == original.model_dump_json()
    assert "schema_version" not in original.model_dump()
    with pytest.raises(ValueError):
        parse_memo_context_review(json.dumps(_review(schema_version="memo_context_review.v99")))


def test_supporting_context_ids_sorted_unique_and_primary_reserved() -> None:
    context = dict(
        context_id="peer",
        issuer_id="issuer-deck",
        ticker="DECK",
        research_snapshot_id="deck-snapshot",
        member_set_sha256="c" * 64,
    )
    review = MemoContextReviewV2.model_validate(
        _review(schema_version="memo_context_review.v2", supporting_contexts=[context])
    )
    assert review.supporting_contexts[0].ticker == "DECK"
    for contexts in (
        [context, context],
        [{**context, "context_id": "primary"}],
        [{**context, "research_snapshot_id": "onon-snapshot"}],
    ):
        with pytest.raises(ValidationError):
            MemoContextReviewV2.model_validate(
                _review(schema_version="memo_context_review.v2", supporting_contexts=contexts)
            )
    with pytest.raises(ValidationError):
        MemoSupportingContext.model_validate({**context, "issuer_id": ""})


def test_calculation_block_accepts_only_declared_model_numbers() -> None:
    soup = BeautifulSoup("<p>Base value 34.47.</p>", "html.parser")
    block = memo_reader_blocks(str(soup))[0]
    value = MemoModelValue(
        source="output",
        key="vps",
        unit="USD_per_share",
        displayed_unit="USD_per_share",
        display_format="number2",
        displayed_value="34.47",
    )
    claim = ReviewedMemoClaimV2(
        block_id=block.block_id,
        passage=block.text,
        kind="calculation",
        rationale="Shared model replay proves this value.",
        model_values=(value,),
    )
    review = MemoContextReviewV2.model_validate(
        _review(schema_version="memo_context_review.v2")
    ).model_copy(update={"claims": (claim,)})
    verify_memo_claim_population(soup, review)
    for update in ({"model_values": ()}, {"kind": "reported_fact"}):
        with pytest.raises(MemoEvidenceError):
            verify_memo_claim_population(
                soup, review.model_copy(update={"claims": (claim.model_copy(update=update),)})
            )


def test_v2_actual_source_reconstruction_has_no_path_or_missing_byte_bypass(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    path = tmp_path / "explicit-temporary.db"
    migrated_db(path)
    root = tmp_path / "sources"
    source = root / "document.json"
    with sqlite3.connect(path) as conn:
        seed_foundation(conn, source_path=source)
        context = SourceReadContext(content_roots=(root,))
        assert verify_memo_context_source_bytes(conn, ("document-1",), context) == (1, 12)
        assert conn.row_factory is None
        with pytest.raises(MemoEvidenceError, match="memo_source_read_context_missing"):
            verify_memo_context_source_bytes(conn, ("document-1",), None)
        with pytest.raises(MemoEvidenceError, match="memo_source_location_unapproved"):
            verify_memo_context_source_bytes(
                conn, ("document-1",), SourceReadContext(content_roots=(tmp_path / "other",))
            )
        source.write_bytes(b"altered same")
        with pytest.raises(MemoEvidenceError, match="memo_source_bytes_unavailable_or_changed"):
            verify_memo_context_source_bytes(conn, ("document-1",), context)
        source.write_bytes(b"filing bytes")
        link = root / "second-link"
        os.link(source, link)
        with pytest.raises(MemoEvidenceError, match="memo_source_multiple_links"):
            verify_memo_context_source_bytes(conn, ("document-1",), context)
        link.unlink()
        source.unlink()
        with pytest.raises(OSError):
            verify_memo_context_source_bytes(conn, ("document-1",), context)


def test_composed_contexts_never_reset_the_total_source_budget(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    path = tmp_path / "explicit-temporary.db"
    migrated_db(path)
    source = tmp_path / "sources" / "document.json"
    with sqlite3.connect(path) as conn:
        seed_foundation(conn, source_path=source)
        contexts = (("document-1",), ("document-1",))
        # Reader-level test: two independent context reads consume24 bytes,
        # even when a document repeats. Snapshot admission remains separate.
        adequate = SourceReadContext(content_roots=(source.parent,), max_total_bytes=24)
        assert verify_memo_composed_source_bytes(conn, contexts, adequate) == 1
        insufficient = adequate.model_copy(update={"max_total_bytes": 23})
        with pytest.raises(MemoEvidenceError, match="memo_source_byte_limit"):
            verify_memo_composed_source_bytes(conn, contexts, insufficient)
        with pytest.raises(MemoEvidenceError, match="context_population_limit"):
            verify_memo_composed_source_bytes(conn, contexts * 5, adequate)


def test_unsealed_peer_context_is_not_an_issuer_allowlist(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    path = tmp_path / "explicit-temporary.db"
    migrated_db(path)
    with sqlite3.connect(path) as conn:
        seed_foundation(conn, source_path=tmp_path / "sources" / "document.json")
        context = MemoSupportingContext(
            context_id="peer",
            issuer_id="issuer-1",
            ticker="DECK",
            research_snapshot_id="invented-seal",
            member_set_sha256="a" * 64,
        )
        with pytest.raises((ValueError, RuntimeError)):
            verify_memo_supporting_context(conn, context, STAMP)


def test_one_peer_issuer_cannot_split_into_alias_contexts() -> None:
    context = dict(
        context_id="a",
        issuer_id="issuer-deck",
        ticker="DECK",
        research_snapshot_id="deck-a",
        member_set_sha256="c" * 64,
    )
    alias = {**context, "context_id": "b", "ticker": "DECK.A", "research_snapshot_id": "deck-b"}
    with pytest.raises(ValidationError, match="membership_invalid"):
        MemoContextReviewV2.model_validate(
            _review(schema_version="memo_context_review.v2", supporting_contexts=[context, alias])
        )
