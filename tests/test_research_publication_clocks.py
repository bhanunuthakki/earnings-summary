"""Public sealed snapshot replay with original source-publication clocks."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import provenance.research_snapshot as snapshots
from provenance.filing_xbrl_fact_adapter import (
    FilingXbrlFactAdapter,
    NormalizedFilingXbrlFact,
)
from provenance.meli_role_admission import RoleAdmissionRequest
from provenance.population_canonical_resolution import (
    CanonicalResolutionPopulationRequest,
    populate_canonical_resolution,
)
from provenance.population_research_snapshots import assemble_research_snapshot_request
from provenance.research_snapshot import (
    ResearchSnapshotRequest,
    VerifiedResearchReference,
    build_research_snapshot,
    verify_research_snapshot,
    verify_source_publication_research_reference,
)
from provenance.source_fact_publication import (
    PublicationVerificationError,
    verify_source_fact_publication,
)
from provenance.source_fact_repository import SourceFactPublication, SourceFactRepository
from tests.test_filing_xbrl_extraction_ledger import filing_xbrl_output
from tests.test_meli_role_admission import synthetic_current_population


def test_sealed_source_references_preserve_original_clocks(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, request = synthetic_current_population(tmp_path, migrated_db)
    try:
        admission = verify_research_snapshot(conn, request.research_snapshot_id)
        assert admission.admitted
        assert admission.legacy_source_publication_clock_lanes == ()
        assert "legacy_source_publication_clock_lanes" not in admission.model_dump(mode="json")
        rows = conn.execute(
            "SELECT publication.publication_id,member.canonical_member_json "
            "FROM research_snapshot_members member "
            "JOIN source_fact_publication_seals publication "
            "ON publication.publication_seal_id=member.reference_id "
            "WHERE member.research_snapshot_id=? "
            "AND member.requested_lane LIKE 'source_fact_publication:%'",
            (request.research_snapshot_id,),
        ).fetchall()
        assert rows
        for publication_id, member_json in rows:
            verified = verify_source_fact_publication(
                conn,
                publication_id=str(publication_id),
                cutoff=request.as_of,
                observed_through=request.as_of,
            )
            member = json.loads(str(member_json))
            assert datetime.fromisoformat(member["reference_knowledge_at"]) == verified.created_at
            assert datetime.fromisoformat(member["reference_recorded_at"]) == max(
                verified.recorded_at, verified.sealed_at
            )
            assert verified.created_at < request.as_of
            for knowledge, observed in (
                (verified.created_at - timedelta(microseconds=1), request.as_of),
                (
                    min(verified.created_at, verified.recorded_at - timedelta(microseconds=1)),
                    verified.recorded_at - timedelta(microseconds=1),
                ),
            ):
                with pytest.raises(
                    PublicationVerificationError, match="publication_graph_after_cutoff"
                ):
                    verify_source_fact_publication(
                        conn,
                        publication_id=str(publication_id),
                        cutoff=knowledge,
                        observed_through=observed,
                    )
        assert datetime.now(UTC) >= request.as_of
    finally:
        conn.close()


def _delayed_publication_snapshot(
    conn: sqlite3.Connection, original: RoleAdmissionRequest
) -> ResearchSnapshotRequest:
    """Use real source owners to record old known facts after the research cutoff."""
    entries = tuple(
        NormalizedFilingXbrlFact.model_validate_json(str(row[0]))
        for row in conn.execute(
            "SELECT canonical_normalized_entry_json FROM filing_xbrl_extraction_dispositions "
            "ORDER BY input_ordinal"
        )
    )
    assert len(entries) == 28
    original_publication = FilingXbrlFactAdapter().adapt(filing_xbrl_output(entries)).publication
    recorded = datetime.now(UTC)
    publication = SourceFactPublication.model_validate(
        original_publication.model_copy(
            update={
                "publication_id": "publication:delayed-recording",
                "idempotency_key": "publication:delayed-recording",
                "recorded_at": recorded,
            }
        ).model_dump()
    )
    assert publication.created_at < original.as_of < publication.recorded_at
    SourceFactRepository(conn).publish(publication)
    assert (
        populate_canonical_resolution(
            conn,
            CanonicalResolutionPopulationRequest(
                cutoff_at=original.as_of, operation_recorded_at=recorded, apply=True
            ),
        ).state
        == "complete"
    )
    row = conn.execute(
        "SELECT request_json FROM research_snapshot_headers WHERE research_snapshot_id=?",
        (original.research_snapshot_id,),
    ).fetchone()
    assert row is not None
    prior_snapshot = ResearchSnapshotRequest.model_validate_json(str(row[0]))
    request = assemble_research_snapshot_request(
        conn,
        "issuer-1",
        original.as_of,
        observed_through=recorded,
        projection_mode="lexical_only",
        analysis_scope=prior_snapshot.research_universe.analysis_scope,
    )
    assert publication.publication_id in request.source_fact_publication_ids
    return request


def _immutable_snapshot_bytes(conn: sqlite3.Connection, snapshot_id: str) -> bytes:
    """Exact persisted receipt bytes before and after a read-only replay."""
    tables = (
        "research_snapshot_headers",
        "research_snapshot_universe_commitments",
        "research_snapshot_members",
        "research_snapshot_seals",
    )
    rows: list[object] = []
    for table in tables:
        rows.append(
            [
                table,
                [
                    list(row)
                    for row in conn.execute(
                        f"SELECT * FROM {table} WHERE research_snapshot_id=? ORDER BY rowid",
                        (snapshot_id,),
                    )
                ],
            ]
        )
    return json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()


def test_real_old_mapper_snapshot_replays_without_rewriting(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, original = synthetic_current_population(tmp_path, migrated_db)
    try:
        request = _delayed_publication_snapshot(conn, original)
        mapper = verify_source_publication_research_reference

        def historical_writer(
            db: sqlite3.Connection, prior: ResearchSnapshotRequest, publication_id: str
        ) -> VerifiedResearchReference:
            # Fixture construction ONLY: reproduce the exact pre-aca8baee line.
            # Identity, hash, recorded clock and all real source checks stay intact.
            reference = mapper(db, prior, publication_id)
            return reference.model_copy(update={"knowledge_at": prior.cutoff_at})

        with monkeypatch.context() as patch:
            patch.setattr(
                snapshots, "verify_source_publication_research_reference", historical_writer
            )
            old_admission = build_research_snapshot(conn, request)
        conn.commit()
        before = _immutable_snapshot_bytes(conn, request.research_snapshot_id)
        (tmp_path / "historical-snapshot-before.json").write_bytes(before)
        changes = conn.total_changes
        # The current public verifier is fully unpatched for every acceptance assertion.
        replay = verify_research_snapshot(conn, request.research_snapshot_id)
        assert replay.member_set_sha256 == old_admission.member_set_sha256
        assert replay.legacy_source_publication_clock_lanes == (
            "source_fact_publication:publication:delayed-recording",
        )
        assert conn.total_changes == changes
        assert _immutable_snapshot_bytes(conn, request.research_snapshot_id) == before
        real_reference = mapper(conn, request, "publication:delayed-recording")
        assert real_reference.knowledge_at < request.cutoff_at <= real_reference.recorded_at
        current_request = request.model_copy(
            update={
                "research_snapshot_id": "snapshot:correct-source-clocks",
                "idempotency_key": "snapshot:correct-source-clocks",
            }
        )
        current = build_research_snapshot(conn, current_request)
        assert current.legacy_source_publication_clock_lanes == ()
        assert "legacy_source_publication_clock_lanes" not in current.model_dump(mode="json")
        current_member = conn.execute(
            "SELECT canonical_member_json FROM research_snapshot_members "
            "WHERE research_snapshot_id=? AND requested_lane=?",
            (current_request.research_snapshot_id, real_reference.requested_lane),
        ).fetchone()
        assert current_member is not None
        assert (
            datetime.fromisoformat(json.loads(str(current_member[0]))["reference_knowledge_at"])
            == real_reference.knowledge_at
        )

        def unrecognized_writer(
            db: sqlite3.Connection, prior: ResearchSnapshotRequest, publication_id: str
        ) -> VerifiedResearchReference:
            # Deliberately corrupt fixture: this clock is neither old nor current.
            reference = mapper(db, prior, publication_id)
            return reference.model_copy(
                update={"knowledge_at": prior.cutoff_at - timedelta(microseconds=1)}
            )

        corrupt_request = request.model_copy(
            update={
                "research_snapshot_id": "snapshot:unrecognized-source-clock",
                "idempotency_key": "snapshot:unrecognized-source-clock",
            }
        )
        with monkeypatch.context() as patch:
            patch.setattr(
                snapshots, "verify_source_publication_research_reference", unrecognized_writer
            )
            build_research_snapshot(conn, corrupt_request)
        corrupt_before = _immutable_snapshot_bytes(conn, corrupt_request.research_snapshot_id)
        with pytest.raises(ValueError, match="reference commitment mismatch"):
            verify_research_snapshot(conn, corrupt_request.research_snapshot_id)
        assert (
            _immutable_snapshot_bytes(conn, corrupt_request.research_snapshot_id) == corrupt_before
        )
        for invalid in (
            request.model_copy(
                update={"cutoff_at": real_reference.knowledge_at - timedelta(microseconds=1)}
            ),
            request.model_copy(
                update={"recorded_at": real_reference.recorded_at - timedelta(microseconds=1)}
            ),
        ):
            with pytest.raises(
                PublicationVerificationError, match="publication_graph_after_cutoff"
            ):
                mapper(conn, invalid, "publication:delayed-recording")
        with pytest.raises(PublicationVerificationError, match="publication_graph_missing"):
            mapper(conn, request, "missing-publication")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE source_fact_publications SET member_set_sha256=? WHERE publication_id=?",
                ("0" * 64, "publication:delayed-recording"),
            )
        assert verify_research_snapshot(conn, request.research_snapshot_id) == replay
        assert _immutable_snapshot_bytes(conn, request.research_snapshot_id) == before
    finally:
        conn.close()
