"""Publisher-universe completeness requires explicit, hash-bound authority evidence."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from ir_pipeline.authority import (
    IRAuthorityEvidence,
    PublisherSurfaceEvidence,
    authority_is_complete,
)
from ir_pipeline.discover._docmeta import CandidateDoc
from ir_pipeline.discover.generic import CrawlPageOutcome, DocumentDiscoveryInventory
from ir_pipeline.source_inventory import source_inventory_request, sync_ir_source_inventory
from provenance.evidence_ledger import ContentBlob, EvidenceLedger, SourceObservation

ROOT = Path(__file__).resolve().parents[1]
STAMP = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
CONFIG_SHA = "a" * 64
URL = "https://ir.acme.test/q4-2025-results.pdf"


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _conn(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "authority.db"
    config = _config(path)
    command.stamp(config, "0213_decision_draft_provider_id")
    command.upgrade(config, "0220_source_inventory_seals")
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _inventory(*, crawl_complete: bool = True) -> DocumentDiscoveryInventory:
    return DocumentDiscoveryInventory(
        candidates=(
            CandidateDoc(
                url=URL,
                link_text="Q4 2025 Results",
                filename_hint="q4-2025-results.pdf",
                doc_type_guess="press_release",
                year_guess=2025,
                quarter_guess=4,
                source_page="https://ir.acme.test/archive",
            ),
        ),
        pages=(
            CrawlPageOutcome(
                page_url="https://ir.acme.test/archive",
                outcome="succeeded",
                anchor_count=1,
                anchors=((URL, "Q4 2025 Results"),),
            ),
        ),
        crawl_complete=crawl_complete,
        crawl_stop_reason=("frontier_exhausted" if crawl_complete else "page_budget_exhausted"),
    )


def _authority(observation_id: str, digest: str) -> IRAuthorityEvidence:
    return IRAuthorityEvidence(
        authority_basis="publisher_archive",
        asserted_at=STAMP,
        surfaces=(
            PublisherSurfaceEvidence(
                surface_key="archive",
                surface_kind="archive",
                source_url="https://ir.acme.test/archive",
                source_observation_id=observation_id,
                raw_sha256=digest,
                traversal_kind="pagination",
                outcome="exhausted",
                required=True,
                terminal_condition="next_link_absent",
                observed_document_urls=(URL,),
            ),
        ),
    )


def _persist_authority_observation(conn: sqlite3.Connection, tmp_path: Path) -> tuple[str, str]:
    body = b"<html><a href='q4-2025-results.pdf'>Q4 2025 Results</a></html>"
    digest = hashlib.sha256(body).hexdigest()
    path = tmp_path / digest
    path.write_bytes(body)
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ContentBlob(
            sha256=digest,
            byte_size=len(body),
            media_type="text/html",
            storage_uri=path.resolve().as_uri(),
            recorded_at=STAMP,
        )
    )
    observation_id = "ir-authority-observation"
    ledger.persist(
        SourceObservation(
            observation_id=observation_id,
            idempotency_key=observation_id,
            source_kind="ir_publisher_authority",
            source_url="https://ir.acme.test/archive",
            blob_sha256=digest,
            source_published_at=None,
            filing_at=None,
            accepted_at=None,
            observed_at=STAMP,
            retrieved_at=STAMP,
            retrieval_config_sha256=CONFIG_SHA,
            collector_code_version="authority-fixture@1",
        )
    )
    conn.commit()
    return observation_id, digest


def _request(
    *,
    authority: IRAuthorityEvidence | None,
    apply: bool,
    crawl_complete: bool = True,
):
    return source_inventory_request(
        issuer_id="issuer-acme",
        ticker="ACME",
        ir_url="https://ir.acme.test/",
        revision=1,
        inventory=_inventory(crawl_complete=crawl_complete),
        authority=authority,
        retrieval_config_sha256=CONFIG_SHA,
        collector_code_version="sync-ir-source-inventory@2",
        started_at=STAMP,
        completed_at=STAMP,
        recorded_at=STAMP,
        reconciled_at=STAMP,
        apply=apply,
    )


def test_generic_frontier_exhaustion_cannot_create_complete_seal(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    try:
        result = sync_ir_source_inventory(
            conn,
            _request(authority=None, apply=True),
            blob_root=tmp_path / "blobs",
        )
        assert not result.complete
        assert conn.execute(
            "SELECT authoritative, outcome FROM source_inventory_snapshots"
        ).fetchone() == (0, "partial")
        assert conn.execute(
            "SELECT completion_status FROM source_inventory_snapshot_seals"
        ).fetchone() == ("incomplete",)
    finally:
        conn.close()


def test_authority_requires_every_required_surface_exhausted() -> None:
    complete = _authority("obs", "b" * 64)
    incomplete = complete.model_copy(
        update={
            "surfaces": (
                complete.surfaces[0].model_copy(
                    update={"outcome": "observed", "terminal_condition": None}
                ),
            )
        }
    )
    assert not authority_is_complete(incomplete, discovered_urls=(URL,))


def test_hash_bound_authority_can_seal_complete_publisher_universe(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    observation_id, digest = _persist_authority_observation(conn, tmp_path)
    try:
        result = sync_ir_source_inventory(
            conn,
            _request(authority=_authority(observation_id, digest), apply=True),
            blob_root=tmp_path / "blobs",
        )
        assert result.complete
        assert conn.execute(
            "SELECT authoritative, outcome FROM source_inventory_snapshots"
        ).fetchone() == (1, "succeeded")
        assert conn.execute(
            "SELECT completion_status FROM source_inventory_snapshot_seals"
        ).fetchone() == ("complete",)
        assert conn.execute(
            "SELECT required, outcome FROM source_inventory_components "
            "WHERE component_key = 'authority:archive'"
        ).fetchone() == (1, "succeeded")
    finally:
        conn.close()


def test_complete_authority_supersedes_generic_crawl_budget_exhaustion(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    observation_id, digest = _persist_authority_observation(conn, tmp_path)
    try:
        result = sync_ir_source_inventory(
            conn,
            _request(
                authority=_authority(observation_id, digest),
                apply=True,
                crawl_complete=False,
            ),
            blob_root=tmp_path / "blobs",
        )
        assert result.complete
        assert conn.execute(
            "SELECT authoritative, outcome FROM source_inventory_snapshots"
        ).fetchone() == (1, "succeeded")
        assert conn.execute(
            "SELECT completion_status FROM source_inventory_snapshot_seals"
        ).fetchone() == ("complete",)
        assert (
            conn.execute(
                "SELECT 1 FROM source_inventory_components "
                "WHERE component_key = 'crawl-completeness'"
            ).fetchone()
            is None
        )
    finally:
        conn.close()


@pytest.mark.parametrize("authority_variant", ["mismatched", "incomplete"])
def test_non_complete_authority_preserves_generic_crawl_budget_failure(
    tmp_path: Path,
    authority_variant: str,
) -> None:
    conn = _conn(tmp_path)
    observation_id, digest = _persist_authority_observation(conn, tmp_path)
    authority = _authority(observation_id, digest)
    surface_update: dict[str, object]
    if authority_variant == "mismatched":
        surface_update = {"observed_document_urls": ("https://ir.acme.test/q3-2025-results.pdf",)}
    else:
        surface_update = {"outcome": "observed", "terminal_condition": None}
    non_complete = authority.model_copy(
        update={"surfaces": (authority.surfaces[0].model_copy(update=surface_update),)}
    )
    try:
        result = sync_ir_source_inventory(
            conn,
            _request(
                authority=non_complete,
                apply=True,
                crawl_complete=False,
            ),
            blob_root=tmp_path / "blobs",
        )
        assert not result.complete
        assert conn.execute(
            "SELECT authoritative, outcome FROM source_inventory_snapshots"
        ).fetchone() == (0, "partial")
        assert conn.execute(
            "SELECT completion_status FROM source_inventory_snapshot_seals"
        ).fetchone() == ("incomplete",)
        assert conn.execute(
            "SELECT outcome, required, failure_reason "
            "FROM source_inventory_components "
            "WHERE component_key = 'crawl-completeness'"
        ).fetchone() == ("failed", 1, "page_budget_exhausted")
    finally:
        conn.close()


def test_authority_observation_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    observation_id, _ = _persist_authority_observation(conn, tmp_path)
    try:
        with pytest.raises(ValueError, match="authority observation"):
            sync_ir_source_inventory(
                conn,
                _request(
                    authority=_authority(observation_id, "f" * 64),
                    apply=True,
                ),
                blob_root=tmp_path / "blobs",
            )
    finally:
        conn.close()
