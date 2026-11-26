"""End-to-end tests for execution/compact_evidence_and_coverage.py and post-compaction readers."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from execution import compact_evidence_and_coverage as compactor
from provenance import document_processing_evidence as dpe
from provenance import evidence_compaction
from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    EvidenceLocator,
    EvidenceNode,
    ExtractionRun,
    SourceObservation,
)
from provenance.evidence_node_store import load_run_nodes
from provenance.meli_reported_tables import load_reported_table_evidence
from provenance.reviewed_sec_financial_tables import load_reviewed_sec_html_evidence
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from tests.test_meli_reported_tables import FIXTURES as MELI_FIXTURES
from tests.test_meli_reported_tables import seed_reported_html
from tests.test_reviewed_sec_financial_tables import synthetic_sec_request

STAMP = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _seed_step_a_database(db_path: Path) -> dict[str, str]:
    """Seed eligible leaf table_cells, protected FK/non-leaf cells, and non-backfill cells."""
    with connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER) as conn:
        ledger = EvidenceLedger(conn)
        blob_bytes = b"<html><body>synthetic bloat fixture</body></html>"
        blob_sha = hashlib.sha256(blob_bytes).hexdigest()
        ledger.persist(
            ContentBlob(
                sha256=blob_sha,
                byte_size=len(blob_bytes),
                media_type="text/html",
                storage_uri="file:///synthetic/bloat.html",
                recorded_at=STAMP,
            )
        )
        ledger.persist(
            SourceObservation(
                observation_id="obs-cmp-1",
                idempotency_key="obs-cmp-1",
                source_kind="sec_filing_document",
                source_url="https://www.sec.gov/Archives/edgar/data/99/000000009926000001/f.htm",
                blob_sha256=blob_sha,
                source_published_at=STAMP,
                filing_at=STAMP,
                accepted_at=STAMP,
                observed_at=STAMP,
                retrieved_at=STAMP,
                retrieval_config_sha256="a" * 64,
                collector_code_version="test.v1",
            )
        )
        ledger.persist(
            DocumentVersion(
                document_version_id="doc-cmp-1",
                document_key="doc-cmp-key-1",
                version_sequence=1,
                observation_id="obs-cmp-1",
                blob_sha256=blob_sha,
                issuer_id="issuer-cmp-1",
                ticker="CMP",
                document_type="filing",
                form_type="10-Q",
                accession_number="0000000099-26-000001",
                period_start=datetime(2026, 4, 1, tzinfo=UTC),
                period_end=datetime(2026, 6, 30, tzinfo=UTC),
                language="en",
                recorded_at=STAMP,
            )
        )
        fulltext_run_id = "run-fulltext-1"
        other_run_id = "run-other-extractor-1"
        ledger.persist(
            ExtractionRun(
                extraction_run_id=fulltext_run_id,
                idempotency_key=fulltext_run_id,
                document_version_id="doc-cmp-1",
                input_sha256=blob_sha,
                extractor_name="fulltext-evidence-backfill",
                extractor_config_sha256="b" * 64,
                extractor_code_version="fulltext-evidence-backfill@4-replica-aware-web-archive",
                output_sha256="c" * 64,
                started_at=STAMP,
                completed_at=STAMP,
                outcome="succeeded",
            )
        )
        ledger.persist(
            ExtractionRun(
                extraction_run_id=other_run_id,
                idempotency_key=other_run_id,
                document_version_id="doc-cmp-1",
                input_sha256=blob_sha,
                extractor_name="other-extractor",
                extractor_config_sha256="d" * 64,
                extractor_code_version="other@1",
                output_sha256="e" * 64,
                started_at=STAMP,
                completed_at=STAMP,
                outcome="succeeded",
            )
        )

        # Structural non-table_cell nodes that must survive.
        for node in (
            EvidenceNode(
                node_id="n-doc",
                evidence_key="n-doc",
                revision=1,
                extraction_run_id=fulltext_run_id,
                node_kind="document",
                text="root document",
                locator=EvidenceLocator(
                    source_ref="https://www.sec.gov/Archives/edgar/data/99/000000009926000001/f.htm"
                ),
                recorded_at=STAMP,
            ),
            EvidenceNode(
                node_id="n-sec",
                evidence_key="n-sec",
                revision=1,
                extraction_run_id=fulltext_run_id,
                parent_node_id="n-doc",
                node_kind="section",
                text="section body",
                locator=EvidenceLocator(
                    source_ref="https://www.sec.gov/Archives/edgar/data/99/000000009926000001/f.htm"
                ),
                recorded_at=STAMP,
            ),
            EvidenceNode(
                node_id="n-pass",
                evidence_key="n-pass",
                revision=1,
                extraction_run_id=fulltext_run_id,
                parent_node_id="n-sec",
                node_kind="passage",
                text="narrative passage",
                locator=EvidenceLocator(
                    source_ref="https://www.sec.gov/Archives/edgar/data/99/000000009926000001/f.htm"
                ),
                recorded_at=STAMP,
            ),
            EvidenceNode(
                node_id="n-row",
                evidence_key="n-row",
                revision=1,
                extraction_run_id=fulltext_run_id,
                parent_node_id="n-sec",
                node_kind="table_row",
                text="row summary",
                locator=EvidenceLocator(
                    source_ref="https://www.sec.gov/Archives/edgar/data/99/000000009926000001/f.htm",
                    table_name="t1",
                    table_row_index=0,
                ),
                recorded_at=STAMP,
            ),
        ):
            ledger.persist(node)

        # 120 large unreferenced leaf table_cell rows so VACUUM measurably shrinks the DB file.
        for idx in range(120):
            ledger.persist(
                EvidenceNode(
                    node_id=f"n-leaf-cell-{idx:03d}",
                    evidence_key=f"n-leaf-cell-{idx:03d}",
                    revision=1,
                    extraction_run_id=fulltext_run_id,
                    parent_node_id="n-row",
                    node_kind="table_cell",
                    text=f"cell-{idx:03d}-" + ("X" * 4096),
                    locator=EvidenceLocator(
                        source_ref="https://www.sec.gov/Archives/edgar/data/99/000000009926000001/f.htm",
                        table_name="t1",
                        table_row_index=idx // 4,
                        table_column_index=idx % 4,
                    ),
                    recorded_at=STAMP,
                )
            )

        # FK-referenced table_cell (referenced by search_chunks) — MUST survive.
        ledger.persist(
            EvidenceNode(
                node_id="n-fk-cell",
                evidence_key="n-fk-cell",
                revision=1,
                extraction_run_id=fulltext_run_id,
                parent_node_id="n-row",
                node_kind="table_cell",
                text="chunk-anchored table cell",
                locator=EvidenceLocator(
                    source_ref="https://www.sec.gov/Archives/edgar/data/99/000000009926000001/f.htm",
                    table_name="t1",
                    table_row_index=99,
                    table_column_index=0,
                ),
                recorded_at=STAMP,
            )
        )
        conn.execute(
            "INSERT INTO search_corpus_manifests ("
            "manifest_id, idempotency_key, corpus_key, revision, selection_config_sha256, "
            "selector_code_version, recorded_at"
            ") VALUES ('man-1', 'man-1', 'corpus-1', 1, ?, 'selector.v1', ?)",
            ("1" * 64, STAMP.isoformat()),
        )
        conn.execute(
            "INSERT INTO search_corpus_document_memberships ("
            "membership_id, manifest_id, expected_document_key, document_version_id, "
            "membership_status, reason, recorded_at"
            ") VALUES ('mem-1', 'man-1', 'doc-cmp-key-1', 'doc-cmp-1', 'included', 'qualified', ?)",
            (STAMP.isoformat(),),
        )
        conn.execute(
            "INSERT INTO search_chunks ("
            "chunk_id, idempotency_key, manifest_id, evidence_node_id, chunk_key, chunk_revision, "
            "text, content_sha256, char_start, char_end, chunker_config_sha256, "
            "chunker_code_version, available_at, recorded_at"
            ") VALUES ('chunk-1', 'chunk-1', 'man-1', 'n-fk-cell', 'ck-1', 1, "
            "'chunk-anchored table cell', ?, 0, 25, ?, 'chunker.v1', ?, ?)",
            (_sha("chunk-anchored table cell"), "2" * 64, STAMP.isoformat(), STAMP.isoformat()),
        )

        # Non-leaf table_cell (referenced as parent_node_id by a child passage) — MUST survive.
        ledger.persist(
            EvidenceNode(
                node_id="n-parent-cell",
                evidence_key="n-parent-cell",
                revision=1,
                extraction_run_id=fulltext_run_id,
                parent_node_id="n-row",
                node_kind="table_cell",
                text="parent table cell",
                locator=EvidenceLocator(
                    source_ref="https://www.sec.gov/Archives/edgar/data/99/000000009926000001/f.htm",
                    table_name="t1",
                    table_row_index=100,
                    table_column_index=0,
                ),
                recorded_at=STAMP,
            )
        )
        ledger.persist(
            EvidenceNode(
                node_id="n-child-of-cell",
                evidence_key="n-child-of-cell",
                revision=1,
                extraction_run_id=fulltext_run_id,
                parent_node_id="n-parent-cell",
                node_kind="passage",
                text="child attached to table_cell",
                locator=EvidenceLocator(
                    source_ref="https://www.sec.gov/Archives/edgar/data/99/000000009926000001/f.htm"
                ),
                recorded_at=STAMP,
            )
        )

        # table_cell from a non-fulltext-backfill extractor — MUST survive.
        ledger.persist(
            EvidenceNode(
                node_id="n-other-ext-cell",
                evidence_key="n-other-ext-cell",
                revision=1,
                extraction_run_id=other_run_id,
                node_kind="table_cell",
                text="other extractor cell",
                locator=EvidenceLocator(
                    source_ref="https://www.sec.gov/Archives/edgar/data/99/000000009926000001/f.htm"
                ),
                recorded_at=STAMP,
            )
        )
        conn.commit()
    return {"fulltext_run_id": fulltext_run_id, "other_run_id": other_run_id}


def test_compaction_dry_run_apply_vacuum_and_trigger_preservation(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_root = tmp_path / "state"
    db_path = migrated_db(state_root / "data" / "portfolio.db")
    ids = _seed_step_a_database(db_path)

    # 1. Dry-run by default: measures eligible rows, zero mutations.
    rc = compactor.main(["--db-path", str(db_path), "--state-root", str(state_root), "--vacuum"])
    assert rc == 0
    dry_receipt = json.loads(capsys.readouterr().out)
    assert dry_receipt["mode"] == "dry_run"
    assert dry_receipt["step_a_evidence_nodes"]["eligible_runs"] == 1
    assert dry_receipt["step_a_evidence_nodes"]["eligible_nodes"] == 120
    assert dry_receipt["step_a_evidence_nodes"]["deleted_nodes"] == 0
    assert dry_receipt["step_d_vacuum"]["executed"] is False
    assert not (state_root / "data" / "derived_evidence").exists()

    # 2. Apply with small batch size + VACUUM.
    rc = compactor.main(
        [
            "--db-path",
            str(db_path),
            "--state-root",
            str(state_root),
            "--apply",
            "--batch-size",
            "17",
            "--vacuum",
        ]
    )
    assert rc == 0
    apply_receipt = json.loads(capsys.readouterr().out)
    assert apply_receipt["mode"] == "apply"
    assert apply_receipt["step_a_evidence_nodes"]["archived_runs"] == 1
    assert apply_receipt["step_a_evidence_nodes"]["archived_nodes"] == 120
    assert apply_receipt["step_a_evidence_nodes"]["deleted_nodes"] == 120
    assert apply_receipt["step_d_vacuum"]["executed"] is True
    assert apply_receipt["step_d_vacuum"]["foreign_key_check"] == "ok"
    assert apply_receipt["step_d_vacuum"]["integrity_check"] == "ok"
    assert (
        apply_receipt["step_d_vacuum"]["db_bytes_after"]
        < apply_receipt["step_d_vacuum"]["db_bytes_before"]
    )

    # 3. Verify surviving rows, append-only delete trigger, and transparent load_run_nodes.
    with connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER) as conn:
        live_ids = {
            str(row[0]) for row in conn.execute("SELECT node_id FROM evidence_nodes").fetchall()
        }
        assert live_ids == {
            "n-doc",
            "n-sec",
            "n-pass",
            "n-row",
            "n-fk-cell",
            "n-parent-cell",
            "n-child-of-cell",
            "n-other-ext-cell",
        }
        # Append-only trigger is still active and blocks raw deletes.
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM evidence_nodes WHERE node_id = 'n-doc'")

        # Transparent read merges live + 120 archived rows = 127 total for fulltext_run_id.
        merged = load_run_nodes(conn, ids["fulltext_run_id"], state_root=state_root)
        assert len(merged) == 127
        assert sum(1 for row in merged if row["node_kind"] == "table_cell") == 122

    # 4. Idempotent second run finds 0 eligible runs.
    rc = compactor.main(["--db-path", str(db_path), "--state-root", str(state_root), "--apply"])
    assert rc == 0
    second_receipt = json.loads(capsys.readouterr().out)
    assert second_receipt["step_a_evidence_nodes"]["eligible_runs"] == 0
    assert second_receipt["step_a_evidence_nodes"]["deleted_nodes"] == 0


def test_meli_and_reviewed_sec_readers_and_processing_evidence_work_after_compaction(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A) MELI reported tables end-to-end after compaction.
    meli_root = tmp_path / "meli_state"
    meli_root.mkdir()
    meli_db = migrated_db(meli_root / "data" / "portfolio.db")
    raw_meli = (MELI_FIXTURES / "h1-2026-snippet.html").read_bytes()
    with connect_sqlite(meli_db, role=SQLiteConnectionRole.WRITER) as conn:
        meli_request = seed_reported_html(conn, meli_root, raw_meli)
        conn.commit()
        before_cells = conn.execute(
            "SELECT COUNT(*) FROM evidence_nodes WHERE extraction_run_id = ? AND node_kind = 'table_cell'",
            (meli_request.fulltext_run_id,),
        ).fetchone()[0]
        assert before_cells > 0

    rc = compactor.main(["--db-path", str(meli_db), "--state-root", str(meli_root), "--apply"])
    assert rc == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["step_a_evidence_nodes"]["archived_nodes"] == before_cells

    with connect_sqlite(meli_db, role=SQLiteConnectionRole.READ_ONLY) as conn:
        after_cells = conn.execute(
            "SELECT COUNT(*) FROM evidence_nodes WHERE extraction_run_id = ? AND node_kind = 'table_cell'",
            (meli_request.fulltext_run_id,),
        ).fetchone()[0]
        assert after_cells == 0
        # MELI reader rehydrates archived table_cell rows seamlessly and passes verify_native_html_replay!
        evidence = load_reported_table_evidence(conn, meli_request, state_root=meli_root)
        assert sum(1 for node in evidence.nodes if node.kind == "table_cell") == before_cells
        # document_processing_evidence._all_run_nodes also verifies exact output_sha256 via native_rowid!
        run_row = conn.execute(
            "SELECT extraction_run_id, output_sha256 FROM evidence_extraction_runs WHERE extraction_run_id = ?",
            (meli_request.fulltext_run_id,),
        ).fetchone()
        assert run_row is not None
        all_run_nodes = getattr(dpe, "_all_run_nodes")
        replayed = all_run_nodes(
            conn,
            {"extraction_run_id": str(run_row[0]), "output_sha256": str(run_row[1])},
            meli_request.recorded_at,
            verify_output=True,
            state_root=meli_root,
        )
        assert len(replayed) == len(evidence.nodes)

    # B) Reviewed SEC financial tables end-to-end after compaction.
    sec_root = tmp_path / "sec_state"
    sec_root.mkdir()
    sec_db = migrated_db(sec_root / "data" / "portfolio.db")
    with connect_sqlite(sec_db, role=SQLiteConnectionRole.WRITER) as conn:
        sec_req = synthetic_sec_request(conn, sec_root)
        conn.commit()
        sec_fact = sec_req.facts[0]
        sec_cells_before = conn.execute(
            "SELECT COUNT(*) FROM evidence_nodes WHERE extraction_run_id = ? AND node_kind = 'table_cell'",
            (sec_fact.fulltext_run_id,),
        ).fetchone()[0]
        assert sec_cells_before > 0

    rc = compactor.main(["--db-path", str(sec_db), "--state-root", str(sec_root), "--apply"])
    assert rc == 0
    capsys.readouterr()

    with connect_sqlite(sec_db, role=SQLiteConnectionRole.READ_ONLY) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM evidence_nodes WHERE extraction_run_id = ? AND node_kind = 'table_cell'",
                (sec_fact.fulltext_run_id,),
            ).fetchone()[0]
            == 0
        )
        sec_evidence = load_reviewed_sec_html_evidence(
            conn,
            document_version_id=sec_fact.document_version_id,
            fulltext_run_id=sec_fact.fulltext_run_id,
            source_kind=sec_fact.source_kind,
            accession_number=sec_fact.accession_number,
            sec_cik=sec_fact.sec_cik,
            source_doc_sha256=sec_fact.source_doc_sha256,
            content_roots=sec_req.content_roots,
            knowledge_cutoff=sec_req.reviewed_at,
            state_root=sec_root,
        )
        assert (
            sum(1 for node in sec_evidence.nodes if node.kind == "table_cell") == sec_cells_before
        )


def test_step_b_coverage_filing_sections_and_step_c_legacy_matches_pruning(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_root = tmp_path / "prune_state"
    db_path = migrated_db(state_root / "data" / "portfolio.db")

    with connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER) as conn:
        # 1. Tracked companies: IDX1 (index_member only), PORT1 (portfolio), DUAL1 (portfolio + index_member via issuer).
        conn.execute(
            "INSERT INTO tracked_companies (ticker, name, list_type) VALUES "
            "('IDX1', 'Index Only Co', 'index_member'), "
            "('PORT1', 'Portfolio Co', 'portfolio'), "
            "('DUAL1', 'Dual Role Co', 'portfolio'), "
            "('DUAL1B', 'Dual Role Class B', 'index_member')"
        )

        # Shared content blob + source observation for snapshots and lifecycle anchors.
        blob_sha = _sha("inventory-payload")
        conn.execute(
            "INSERT INTO evidence_content_blobs (sha256, byte_size, media_type, storage_uri, recorded_at) "
            "VALUES (?, 17, 'application/json', 'file:///synthetic/inv.json', ?)",
            (blob_sha, STAMP.isoformat()),
        )
        conn.execute(
            "INSERT INTO evidence_source_observations ("
            "observation_id, idempotency_key, source_kind, source_url, blob_sha256, "
            "observed_at, retrieved_at, retrieval_config_sha256, collector_code_version"
            ") VALUES ('obs-inv', 'obs-inv', 'sec_submissions_index', "
            "'https://data.sec.gov/submissions/CIK0000000001.json', ?, ?, ?, ?, 'test.v1')",
            (blob_sha, STAMP.isoformat(), STAMP.isoformat(), "a" * 64),
        )

        # Issuers, securities, obligations.
        for issuer_id, ticker in (
            ("iss-idx", "IDX1"),
            ("iss-port", "PORT1"),
            ("iss-dual", "DUAL1"),
        ):
            conn.execute(
                "INSERT INTO issuer_entities (issuer_id, idempotency_key, entity_kind, created_at) "
                "VALUES (?, ?, 'operating_company', ?)",
                (issuer_id, issuer_id, STAMP.isoformat()),
            )
            conn.execute(
                "INSERT INTO source_obligation_revisions ("
                "obligation_revision_id, idempotency_key, obligation_key, revision, issuer_id, "
                "reporting_entity_id, authority_kind, document_family, obligation_state, "
                "completeness_rule, active_from, active_to, decision_kind, reason_code, "
                "reason_details_json, effective_at, knowledge_at, recorded_at"
                ") VALUES (?, ?, ?, 1, ?, NULL, 'sec_edgar', 'operating_company_periodic', "
                "'required', 'regulator_inventory', ?, NULL, 'deterministic', 'seeded', '{}', ?, ?, ?)",
                (
                    f"obl-{issuer_id}",
                    f"obl-{issuer_id}",
                    f"obl-key-{issuer_id}",
                    issuer_id,
                    (STAMP - timedelta(days=30)).isoformat(),
                    STAMP.isoformat(),
                    STAMP.isoformat(),
                    STAMP.isoformat(),
                ),
            )
            conn.execute(
                "INSERT INTO source_inventory_snapshots ("
                "snapshot_id, idempotency_key, inventory_key, revision, issuer_id, ticker, "
                "source_kind, source_url, source_observation_id, outcome, authoritative, "
                "retrieval_config_sha256, collector_code_version, started_at, completed_at, recorded_at"
                ") VALUES (?, ?, ?, 1, ?, ?, 'sec_submissions', "
                "'https://data.sec.gov/submissions/CIK0000000001.json', 'obs-inv', 'succeeded', 1, "
                "?, 'test.v1', ?, ?, ?)",
                (
                    f"snap-{issuer_id}",
                    f"snap-{issuer_id}",
                    f"inv:{issuer_id}",
                    issuer_id,
                    ticker,
                    "b" * 64,
                    STAMP.isoformat(),
                    STAMP.isoformat(),
                    STAMP.isoformat(),
                ),
            )

        # Dual-role issuer has a second security with ticker DUAL1B (index_member) alongside DUAL1 (portfolio).
        conn.execute(
            "INSERT INTO securities (security_id, idempotency_key, issuer_id, security_kind, created_at) "
            "VALUES ('sec-dual-a', 'sec-dual-a', 'iss-dual', 'common_stock', ?), "
            "('sec-dual-b', 'sec-dual-b', 'iss-dual', 'common_stock', ?)",
            (STAMP.isoformat(), STAMP.isoformat()),
        )
        conn.execute(
            "INSERT INTO security_listing_assertions ("
            "assertion_id, idempotency_key, security_id, market_mic, ticker, normalized_ticker, "
            "currency, status, authority, source_observation_id, effective_at, knowledge_at, recorded_at"
            ") VALUES ('lst-a', 'lst-a', 'sec-dual-a', 'XNAS', 'DUAL1', 'DUAL1', 'USD', 'listed', 'manual', NULL, ?, ?, ?), "
            "('lst-b', 'lst-b', 'sec-dual-b', 'XNAS', 'DUAL1B', 'DUAL1B', 'USD', 'listed', 'manual', NULL, ?, ?, ?)",
            (
                STAMP.isoformat(),
                STAMP.isoformat(),
                STAMP.isoformat(),
                STAMP.isoformat(),
                STAMP.isoformat(),
                STAMP.isoformat(),
            ),
        )

        # Expected documents + assessments (2-rev chain) + obligation binding + lifecycle (2-rev chain).
        for issuer_id, ticker in (
            ("iss-idx", "IDX1"),
            ("iss-port", "PORT1"),
            ("iss-dual", "DUAL1B"),
        ):
            ed_id = f"ed-{issuer_id}"
            snap_id = f"snap-{issuer_id}"
            # For iss-dual, create a snapshot matching DUAL1B ticker to satisfy trigger.
            if issuer_id == "iss-dual":
                snap_id = "snap-iss-dual-b"
                conn.execute(
                    "INSERT INTO source_inventory_snapshots ("
                    "snapshot_id, idempotency_key, inventory_key, revision, issuer_id, ticker, "
                    "source_kind, source_url, source_observation_id, outcome, authoritative, "
                    "retrieval_config_sha256, collector_code_version, started_at, completed_at, recorded_at"
                    ") VALUES (?, ?, 'inv:iss-dual-b', 1, 'iss-dual', 'DUAL1B', 'sec_submissions', "
                    "'https://data.sec.gov/submissions/CIK0000000001.json', 'obs-inv', 'succeeded', 1, "
                    "?, 'test.v1', ?, ?, ?)",
                    (
                        snap_id,
                        snap_id,
                        "b" * 64,
                        STAMP.isoformat(),
                        STAMP.isoformat(),
                        STAMP.isoformat(),
                    ),
                )
            conn.execute(
                "INSERT INTO expected_documents ("
                "expected_document_id, idempotency_key, snapshot_id, expected_document_key, "
                "issuer_id, ticker, source_kind, document_type, expectation_basis, recorded_at"
                ") VALUES (?, ?, ?, ?, ?, ?, 'sec_filing', 'filing', 'authoritative', ?)",
                (ed_id, ed_id, snap_id, f"key:{ed_id}", issuer_id, ticker, STAMP.isoformat()),
            )
            # 2-revision assessment chain.
            conn.execute(
                "INSERT INTO source_coverage_assessments ("
                "assessment_id, idempotency_key, expected_document_id, revision, coverage_status, "
                "reason_code, reason_details_json, decision_kind, policy_name, policy_version, "
                "policy_config_sha256, effective_at, knowledge_at, recorded_at, "
                "supersedes_assessment_id, material_dissent"
                ") VALUES (?, ?, ?, 1, 'available', 'init', '{}', 'deterministic', 'pol', 'v1', ?, ?, ?, ?, NULL, 0)",
                (
                    f"asm-{issuer_id}-1",
                    f"asm-{issuer_id}-1",
                    ed_id,
                    "c" * 64,
                    STAMP.isoformat(),
                    STAMP.isoformat(),
                    STAMP.isoformat(),
                ),
            )
            conn.execute(
                "INSERT INTO source_coverage_assessments ("
                "assessment_id, idempotency_key, expected_document_id, revision, coverage_status, "
                "reason_code, reason_details_json, decision_kind, policy_name, policy_version, "
                "policy_config_sha256, effective_at, knowledge_at, recorded_at, "
                "supersedes_assessment_id, material_dissent"
                ") VALUES (?, ?, ?, 2, 'fetch_failed', 'retry', '{}', 'deterministic', 'pol', 'v1', ?, ?, ?, ?, ?, 0)",
                (
                    f"asm-{issuer_id}-2",
                    f"asm-{issuer_id}-2",
                    ed_id,
                    "c" * 64,
                    STAMP.isoformat(),
                    STAMP.isoformat(),
                    STAMP.isoformat(),
                    f"asm-{issuer_id}-1",
                ),
            )
            # Obligation binding (exact canonical JSON verified by trigger).
            binding_json = json.dumps(
                {
                    "document_family": "operating_company_periodic",
                    "expected_document_id": ed_id,
                    "issuer_id": issuer_id,
                    "reporting_entity_id": None,
                    "source_obligation_revision_id": f"obl-{issuer_id}",
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            conn.execute(
                "INSERT INTO expected_document_obligation_bindings ("
                "binding_id, idempotency_key, expected_document_id, source_obligation_revision_id, "
                "issuer_id, reporting_entity_id, document_family, canonical_binding_json, "
                "binding_sha256, effective_at, knowledge_at, recorded_at"
                ") VALUES (?, ?, ?, ?, ?, NULL, 'operating_company_periodic', ?, ?, ?, ?, ?)",
                (
                    f"bind-{issuer_id}",
                    f"bind-{issuer_id}",
                    ed_id,
                    f"obl-{issuer_id}",
                    issuer_id,
                    binding_json,
                    _sha(binding_json),
                    STAMP.isoformat(),
                    STAMP.isoformat(),
                    STAMP.isoformat(),
                ),
            )
            # 2-revision lifecycle chain (rev2 withdraws rev1 with expected_document_id NULL!).
            snap2 = f"snap2-{issuer_id}"
            inv_key_lc = "inv:iss-dual-b" if issuer_id == "iss-dual" else f"inv:{issuer_id}"
            conn.execute(
                "INSERT INTO source_inventory_snapshots ("
                "snapshot_id, idempotency_key, inventory_key, revision, issuer_id, ticker, "
                "source_kind, source_url, source_observation_id, outcome, authoritative, "
                "retrieval_config_sha256, collector_code_version, started_at, completed_at, "
                "recorded_at, supersedes_snapshot_id"
                ") VALUES (?, ?, ?, 2, ?, ?, 'sec_submissions', "
                "'https://data.sec.gov/submissions/CIK0000000001.json', 'obs-inv', 'succeeded', 1, "
                "?, 'test.v1', ?, ?, ?, ?)",
                (
                    snap2,
                    snap2,
                    inv_key_lc,
                    issuer_id,
                    ticker,
                    "b" * 64,
                    STAMP.isoformat(),
                    STAMP.isoformat(),
                    STAMP.isoformat(),
                    snap_id,
                ),
            )
            conn.execute(
                "INSERT INTO expected_document_lifecycle_revisions ("
                "lifecycle_id, idempotency_key, inventory_key, expected_document_key, "
                "source_inventory_snapshot_id, revision, status, expected_document_id, "
                "authority_observation_id, reason_code, reason_details_json, "
                "effective_at, knowledge_at, recorded_at, supersedes_lifecycle_id"
                ") VALUES (?, ?, ?, ?, ?, 1, 'expected', ?, 'obs-inv', 'init', '{}', ?, ?, ?, NULL)",
                (
                    f"lc-{issuer_id}-1",
                    f"lc-{issuer_id}-1",
                    inv_key_lc,
                    f"key:{ed_id}",
                    snap_id,
                    ed_id,
                    STAMP.isoformat(),
                    STAMP.isoformat(),
                    STAMP.isoformat(),
                ),
            )
            conn.execute(
                "INSERT INTO expected_document_lifecycle_revisions ("
                "lifecycle_id, idempotency_key, inventory_key, expected_document_key, "
                "source_inventory_snapshot_id, revision, status, expected_document_id, "
                "authority_observation_id, reason_code, reason_details_json, "
                "effective_at, knowledge_at, recorded_at, supersedes_lifecycle_id"
                ") VALUES (?, ?, ?, ?, ?, 2, 'withdrawn_by_authority', NULL, 'obs-inv', 'gone', '{}', ?, ?, ?, ?)",
                (
                    f"lc-{issuer_id}-2",
                    f"lc-{issuer_id}-2",
                    inv_key_lc,
                    f"key:{ed_id}",
                    snap2,
                    STAMP.isoformat(),
                    STAMP.isoformat(),
                    STAMP.isoformat(),
                    f"lc-{issuer_id}-1",
                ),
            )

        # 2. filing_sections: active portfolio (keep), inactive portfolio (prune), active index_member (prune).
        for idx, (ticker, is_active) in enumerate(
            [("PORT1", 1), ("PORT1", 0), ("IDX1", 1)], start=1
        ):
            body = f"section-{idx}-" + ("S" * 2048)
            conn.execute(
                "INSERT INTO filing_sections ("
                "ticker, source, source_ref, form, fiscal_period, section_key_raw, section_stem, "
                "ordinal, text, text_sha256, char_len, extractor_version, created_at, is_active"
                ") VALUES (?, 'sec', ?, '10-K', 'FY', 'item_1', 'item_1', ?, ?, ?, ?, 'v1', ?, ?)",
                (
                    ticker,
                    f"ref-{idx}",
                    idx,
                    body,
                    _sha(body),
                    len(body),
                    STAMP.isoformat(),
                    is_active,
                ),
            )

        # 3. Legacy fact evidence match revisions (Step C).
        # Seed minimal legacy document, evidence document version, financial_facts, and binding revision.
        conn.execute(
            "INSERT INTO documents ("
            "id, ticker, source_type, doc_type, file_path, sha256, fetched_at, fetch_status, raw_bytes_size"
            ") VALUES (101, 'PORT1', 'sec', 'sec_companyfacts', '/synthetic/cf.json', ?, ?, 'ok', 17)",
            (blob_sha, STAMP.isoformat()),
        )
        conn.execute(
            "INSERT INTO evidence_document_versions ("
            "document_version_id, document_key, version_sequence, observation_id, blob_sha256, "
            "issuer_id, ticker, document_type, form_type, language, legacy_document_id, recorded_at"
            ") VALUES ('doc-cf-1', 'doc-cf-key-1', 1, 'obs-inv', ?, 'iss-port', 'PORT1', "
            "'sec_companyfacts', 'companyfacts', 'en', 101, ?)",
            (blob_sha, STAMP.isoformat()),
        )
        conn.execute(
            "INSERT INTO evidence_extraction_runs ("
            "extraction_run_id, idempotency_key, document_version_id, input_sha256, "
            "extractor_name, extractor_config_sha256, extractor_code_version, output_sha256, "
            "started_at, completed_at, outcome"
            ") VALUES ('run-cf-1', 'run-cf-1', 'doc-cf-1', ?, 'cf-ext', ?, 'v1', ?, ?, ?, 'succeeded')",
            (blob_sha, "e" * 64, "f" * 64, STAMP.isoformat(), STAMP.isoformat()),
        )
        conn.execute(
            "INSERT INTO evidence_nodes ("
            "node_id, evidence_key, revision, extraction_run_id, node_kind, text, recorded_at"
            ") VALUES ('node-cf-1', 'node-cf-1', 1, 'run-cf-1', 'document', 'cf', ?)",
            (STAMP.isoformat(),),
        )
        conn.execute(
            "INSERT INTO legacy_document_evidence_binding_revisions ("
            "binding_revision_id, idempotency_key, legacy_document_id, revision, "
            "document_version_id, evidence_node_id, scope_locator_json, "
            "scope_locator_sha256, scope_content_sha256, effective_at, knowledge_at, recorded_at"
            ") VALUES ('lbind-1', 'lbind-1', 101, 1, 'doc-cf-1', 'node-cf-1', '{}', ?, ?, ?, ?, ?)",
            (_sha("{}"), blob_sha, STAMP.isoformat(), STAMP.isoformat(), STAMP.isoformat()),
        )
        for fact_id, line_item in ((1, "revenue"), (2, "net_income")):
            conn.execute(
                "INSERT INTO financial_facts ("
                "id, ticker, period_end, fiscal_period_type, line_item, value, unit, currency, "
                "source_doc_id, extracted_by"
                ") VALUES (?, 'PORT1', '2026-06-30', 'Q2', ?, 100.5, 'USD', 'USD', 101, 'sec_xbrl')",
                (fact_id, line_item),
            )

        relocated = {
            "accession_number": "0000000001-26-000001",
            "namespace": "us-gaap",
            "concept": "Revenues",
            "unit": "USD",
            "entry_index": 0,
            "json_path": "facts.us-gaap.Revenues.units.USD[0]",
        }
        relocated_json = json.dumps(relocated, sort_keys=True, separators=(",", ":"))
        entry_sha = _sha("matched-entry")
        cand_manifest_accepted = json.dumps(
            {
                "schema_version": "companyfacts_candidate_manifest.v1",
                "candidates": [{"entry_sha256": entry_sha, "relocated_locator": relocated}],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        cand_manifest_empty = json.dumps(
            {"schema_version": "companyfacts_candidate_manifest.v1", "candidates": []},
            sort_keys=True,
            separators=(",", ":"),
        )

        def _insert_match(
            match_id: str,
            fact_id: int,
            revision: int,
            outcome: str,
            supersedes: str | None,
        ) -> None:
            payload = json.dumps(
                {
                    "schema_version": "financial_fact_payload.v1",
                    "fact_table": "financial_facts",
                    "fact_row_id": fact_id,
                    "ticker": "PORT1",
                    "period_end": "2026-06-30",
                    "fiscal_period_type": "Q2",
                    "line_item": "revenue" if fact_id == 1 else "net_income",
                    "value": "100.5",
                    "unit": "USD",
                    "currency": "USD",
                    "source_doc_id": 101,
                    "extracted_by": "sec_xbrl",
                    "locator": None,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            is_acc = outcome == "accepted"
            manifest = cand_manifest_accepted if is_acc else cand_manifest_empty
            conn.execute(
                "INSERT INTO legacy_fact_evidence_match_revisions ("
                "match_revision_id, idempotency_key, fact_table, fact_row_id, issuer_id, revision, "
                "fact_payload_json, fact_payload_fingerprint_sha256, original_locator_json, "
                "original_locator_sha256, relocated_locator_json, relocated_locator_sha256, "
                "legacy_binding_revision_id, legacy_binding_revision, binding_scope_content_sha256, "
                "evidence_node_id, matched_entry_sha256, candidate_manifest_json, "
                "candidate_manifest_sha256, candidate_count, matched_candidate_count, "
                "issuer_check, context_check, unit_check, sign_check, fiscal_period_check, "
                "value_check, matcher_name, matcher_version, matcher_config_sha256, outcome, "
                "reason_code, reason_details_json, effective_at, knowledge_at, recorded_at, "
                "supersedes_match_revision_id"
                ") VALUES (?, ?, 'financial_facts', ?, 'iss-port', ?, ?, ?, NULL, NULL, ?, ?, "
                "'lbind-1', 1, ?, 'node-cf-1', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'm', 'v1', ?, ?, "
                "'test', '{}', ?, ?, ?, ?)",
                (
                    match_id,
                    match_id,
                    fact_id,
                    revision,
                    payload,
                    _sha(payload),
                    relocated_json if is_acc else None,
                    _sha(relocated_json) if is_acc else None,
                    blob_sha,
                    entry_sha if is_acc else None,
                    manifest,
                    _sha(manifest),
                    1 if is_acc else 0,
                    1 if is_acc else 0,
                    "pass" if is_acc else "fail",
                    "pass" if is_acc else "not_evaluated",
                    "pass" if is_acc else "not_evaluated",
                    "pass" if is_acc else "not_evaluated",
                    "pass" if is_acc else "not_evaluated",
                    "pass" if is_acc else "not_evaluated",
                    "2" * 64,
                    outcome,
                    STAMP.isoformat(),
                    STAMP.isoformat(),
                    STAMP.isoformat(),
                    supersedes,
                ),
            )

        # Fact 1: terminal rev1 -> superseded by accepted rev2 (BOTH must be retained!).
        _insert_match("m-f1-r1", 1, 1, "terminal", None)
        _insert_match("m-f1-r2", 1, 2, "accepted", "m-f1-r1")
        # Fact 2: terminal rev1 -> superseded by terminal rev2 (BOTH must be pruned leaf-to-root!).
        _insert_match("m-f2-r1", 2, 1, "terminal", None)
        _insert_match("m-f2-r2", 2, 2, "terminal", "m-f2-r1")
        conn.commit()

    # Run compactor with Step B + Step C + VACUUM.
    rc = compactor.main(
        [
            "--db-path",
            str(db_path),
            "--state-root",
            str(state_root),
            "--apply",
            "--prune-index-member-coverage",
            "--prune-inactive-filing-sections",
            "--prune-unreferenced-legacy-matches",
            "--vacuum",
        ]
    )
    assert rc == 0
    receipt = json.loads(capsys.readouterr().out)

    # Verify Step B1 (index_member coverage): only iss-idx pruned; iss-port and iss-dual survive.
    cov = receipt["step_b_index_member_coverage"]
    assert cov["deleted_expected_documents"] == 1
    assert cov["deleted_source_coverage_assessments"] == 2
    assert cov["deleted_obligation_bindings"] == 1
    assert cov["deleted_lifecycle_revisions"] == 2

    # Verify Step B2 (filing_sections): 2 pruned (inactive PORT1 + active IDX1), 1 survives.
    sec = receipt["step_b_filing_sections"]
    assert sec["deleted_rows"] == 2

    # Verify Step C (legacy matches): 2 pruned (m-f2-r2 + m-f2-r1), 2 survive (m-f1-r2 + m-f1-r1).
    leg = receipt["step_c_legacy_matches"]
    assert leg["deleted_rows"] == 2

    assert receipt["step_d_vacuum"]["foreign_key_check"] == "ok"
    assert receipt["step_d_vacuum"]["integrity_check"] == "ok"

    with connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER) as conn:
        assert {
            str(r[0]) for r in conn.execute("SELECT expected_document_id FROM expected_documents")
        } == {"ed-iss-port", "ed-iss-dual"}
        assert {str(r[0]) for r in conn.execute("SELECT ticker FROM filing_sections")} == {"PORT1"}
        assert {
            str(r[0])
            for r in conn.execute(
                "SELECT match_revision_id FROM legacy_fact_evidence_match_revisions"
            )
        } == {"m-f1-r1", "m-f1-r2"}

        # Every append-only delete trigger was restored verbatim.
        for trigger_name in (
            "trg_source_coverage_assessments_append_only_delete",
            "trg_expected_document_obligation_bindings_append_only_delete",
            "trg_expected_document_lifecycle_revisions_append_only_delete",
            "trg_expected_documents_append_only_delete",
            "trg_legacy_fact_evidence_match_revisions_append_only_delete",
        ):
            assert (
                conn.execute(
                    "SELECT 1 FROM sqlite_schema WHERE type='trigger' AND name=?", (trigger_name,)
                ).fetchone()
                is not None
            )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_maintenance_transaction_restores_foreign_keys_on_commit_and_error(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = migrated_db(tmp_path / "fk_restore.db")
    _seed_step_a_database(db_path)
    maintenance_tx = getattr(evidence_compaction, "_maintenance_transaction")
    trigger_name = "trg_evidence_nodes_append_only_delete"

    with connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER) as conn:
        assert int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 1

        # 1. Normal completion: FK is 0 inside block, restored to 1 on commit, trigger preserved.
        with maintenance_tx(conn, (trigger_name,), defer_fk_checks=True):
            assert int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 0
            conn.execute("DELETE FROM evidence_nodes WHERE node_id = 'n-leaf-cell-000'")
        assert int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
        assert (
            conn.execute(
                "SELECT 1 FROM evidence_nodes WHERE node_id = 'n-leaf-cell-000'"
            ).fetchone()
            is None
        )
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_schema WHERE type='trigger' AND name=?", (trigger_name,)
            ).fetchone()
            is not None
        )

        # 2. Simulated failure: rolls back deletes, restores trigger verbatim, restores FK to 1.
        with (
            pytest.raises(RuntimeError, match="simulated crash"),
            maintenance_tx(conn, (trigger_name,), defer_fk_checks=True),
        ):
            assert int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 0
            conn.execute("DELETE FROM evidence_nodes WHERE node_id = 'n-leaf-cell-001'")
            raise RuntimeError("simulated crash")

        assert int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
        assert (
            conn.execute(
                "SELECT 1 FROM evidence_nodes WHERE node_id = 'n-leaf-cell-001'"
            ).fetchone()
            is not None
        )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM evidence_nodes WHERE node_id = 'n-leaf-cell-001'")


def test_step_a_protects_polymorphic_processing_evidence_members(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_root = tmp_path / "poly_state"
    db_path = migrated_db(state_root / "data" / "portfolio.db")
    ids = _seed_step_a_database(db_path)
    protected_node_id = "n-leaf-cell-007"

    with connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER) as conn:
        run_row = conn.execute(
            "SELECT document_version_id, input_sha256, output_sha256 "
            "FROM evidence_extraction_runs WHERE extraction_run_id = ?",
            (ids["fulltext_run_id"],),
        ).fetchone()
        assert run_row is not None
        doc_ver_id, input_sha, output_sha = str(run_row[0]), str(run_row[1]), str(run_row[2])
        empty_obj = "{}"
        empty_sha = _sha(empty_obj)
        conn.execute(
            "INSERT INTO document_processing_evidence_headers ("
            "evidence_seal_id, idempotency_key, document_version_id, processing_lane, "
            "extraction_run_id, assessment_table, assessment_id, adapter_name, adapter_version, "
            "adapter_config_sha256, input_blob_sha256, native_output_sha256, native_scope_json, "
            "native_scope_sha256, canonical_header_json, header_sha256, cutoff_at, knowledge_at, "
            "recorded_at"
            ") VALUES ('dpe-seal-1', 'dpe-seal-1', ?, 'html_native_hierarchy', ?, NULL, NULL, "
            "'adapter', 'v1', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                doc_ver_id,
                ids["fulltext_run_id"],
                "a" * 64,
                input_sha,
                output_sha,
                empty_obj,
                empty_sha,
                empty_obj,
                empty_sha,
                STAMP.isoformat(),
                STAMP.isoformat(),
                STAMP.isoformat(),
            ),
        )
        conn.execute(
            "INSERT INTO document_processing_evidence_members ("
            "evidence_seal_id, member_ordinal, native_table, native_id, native_parent_id, "
            "locator_json, locator_sha256, content_sha256, native_commitment_json, "
            "native_commitment_sha256, canonical_member_json, member_sha256, "
            "native_knowledge_at, native_recorded_at"
            ") VALUES ('dpe-seal-1', 0, 'evidence_nodes', ?, 'n-row', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                protected_node_id,
                empty_obj,
                empty_sha,
                "b" * 64,
                empty_obj,
                empty_sha,
                empty_obj,
                empty_sha,
                STAMP.isoformat(),
                STAMP.isoformat(),
            ),
        )
        conn.commit()

    rc = compactor.main(["--db-path", str(db_path), "--state-root", str(state_root), "--apply"])
    assert rc == 0
    receipt = json.loads(capsys.readouterr().out)
    # 119 of 120 leaf table_cells archived/deleted; the 1 referenced by document_processing_evidence_members survives!
    assert receipt["step_a_evidence_nodes"]["eligible_nodes"] == 119
    assert receipt["step_a_evidence_nodes"]["archived_nodes"] == 119
    assert receipt["step_a_evidence_nodes"]["deleted_nodes"] == 119

    with connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY) as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM evidence_nodes WHERE node_id = ?", (protected_node_id,)
            ).fetchone()
            is not None
        )
        assert (
            conn.execute(
                "SELECT 1 FROM evidence_nodes WHERE node_id = 'n-leaf-cell-006'"
            ).fetchone()
            is None
        )
