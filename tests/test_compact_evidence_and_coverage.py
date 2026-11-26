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


def test_archive_substantive_nodes_bottom_up_peel_two_pass_merge_and_section_items(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    from provenance.evidence_node_store import archive_receipt_for_run
    from provenance.integrity_audit import AuditOptions, audit_connection

    state_root = tmp_path / "substantive_state"
    db_path = migrated_db(state_root / "data" / "portfolio.db")
    ids = _seed_step_a_database(db_path)
    fulltext_run_id = ids["fulltext_run_id"]
    src_ref = "https://www.sec.gov/Archives/edgar/data/99/000000009926000001/f.htm"

    with connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER) as conn:
        ledger = EvidenceLedger(conn)
        blob_sha = str(
            conn.execute(
                "SELECT blob_sha256 FROM evidence_document_versions WHERE document_version_id = 'doc-cmp-1'"
            ).fetchone()[0]
        )
        # Add a free table -> table_row -> table_cell subtree that must peel together bottom-up.
        for node in (
            EvidenceNode(
                node_id="n-free-table",
                evidence_key="n-free-table",
                revision=1,
                extraction_run_id=fulltext_run_id,
                parent_node_id="n-sec",
                node_kind="table",
                text="Free Table",
                locator=EvidenceLocator(source_ref=src_ref, table_name="free_t"),
                recorded_at=STAMP,
            ),
            EvidenceNode(
                node_id="n-free-row",
                evidence_key="n-free-row",
                revision=1,
                extraction_run_id=fulltext_run_id,
                parent_node_id="n-free-table",
                node_kind="table_row",
                text="Free Row",
                locator=EvidenceLocator(source_ref=src_ref, table_name="free_t", table_row_index=0),
                recorded_at=STAMP,
            ),
            EvidenceNode(
                node_id="n-free-cell",
                evidence_key="n-free-cell",
                revision=1,
                extraction_run_id=fulltext_run_id,
                parent_node_id="n-free-row",
                node_kind="table_cell",
                text="Free Cell",
                locator=EvidenceLocator(
                    source_ref=src_ref,
                    table_name="free_t",
                    table_row_index=0,
                    table_column_index=0,
                ),
                recorded_at=STAMP,
            ),
            # Protected table -> row -> cell_poly (referenced by document_processing_evidence_members).
            EvidenceNode(
                node_id="n-poly-table",
                evidence_key="n-poly-table",
                revision=1,
                extraction_run_id=fulltext_run_id,
                parent_node_id="n-sec",
                node_kind="table",
                text="Poly Table",
                locator=EvidenceLocator(source_ref=src_ref, table_name="poly_t"),
                recorded_at=STAMP,
            ),
            EvidenceNode(
                node_id="n-poly-row",
                evidence_key="n-poly-row",
                revision=1,
                extraction_run_id=fulltext_run_id,
                parent_node_id="n-poly-table",
                node_kind="table_row",
                text="Poly Row",
                locator=EvidenceLocator(source_ref=src_ref, table_name="poly_t", table_row_index=0),
                recorded_at=STAMP,
            ),
            EvidenceNode(
                node_id="n-poly-cell",
                evidence_key="n-poly-cell",
                revision=1,
                extraction_run_id=fulltext_run_id,
                parent_node_id="n-poly-row",
                node_kind="table_cell",
                text="Poly Cell",
                locator=EvidenceLocator(
                    source_ref=src_ref,
                    table_name="poly_t",
                    table_row_index=0,
                    table_column_index=0,
                ),
                recorded_at=STAMP,
            ),
        ):
            ledger.persist(node)

        # Attach polymorphic member reference to n-poly-cell (must keep n-poly-cell, n-poly-row, n-poly-table live).
        empty_sha = _sha("{}")
        conn.execute(
            "INSERT INTO document_processing_evidence_headers ("
            "evidence_seal_id, idempotency_key, document_version_id, processing_lane, "
            "extraction_run_id, assessment_table, assessment_id, adapter_name, adapter_version, "
            "adapter_config_sha256, input_blob_sha256, native_output_sha256, native_scope_json, "
            "native_scope_sha256, canonical_header_json, header_sha256, cutoff_at, knowledge_at, "
            "recorded_at"
            ") VALUES ('dpe-sub-1', 'dpe-sub-1', 'doc-cmp-1', 'html_native_hierarchy', ?, NULL, NULL, "
            "'adapter', 'v1', ?, ?, ?, '{}', ?, '{}', ?, ?, ?, ?)",
            (
                fulltext_run_id,
                "a" * 64,
                blob_sha,
                "c" * 64,
                empty_sha,
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
            ") VALUES ('dpe-sub-1', 0, 'evidence_nodes', 'n-poly-cell', 'n-poly-row', "
            "'{}', ?, ?, '{}', ?, '{}', ?, ?, ?)",
            (
                empty_sha,
                "b" * 64,
                empty_sha,
                empty_sha,
                STAMP.isoformat(),
                STAMP.isoformat(),
            ),
        )

        # Add a second fulltext run sealed in fact_extraction_run_completeness_seals_v2 -> must be untouched!
        sealed_run_id = "run-fulltext-sealed-v2"
        ledger.persist(
            ExtractionRun(
                extraction_run_id=sealed_run_id,
                idempotency_key=sealed_run_id,
                document_version_id="doc-cmp-1",
                input_sha256=blob_sha,
                extractor_name="fulltext-evidence-backfill",
                extractor_config_sha256="1" * 64,
                extractor_code_version="fulltext-evidence-backfill@4-replica-aware-web-archive",
                output_sha256="2" * 64,
                started_at=STAMP,
                completed_at=STAMP,
                outcome="succeeded",
            )
        )
        ledger.persist(
            EvidenceNode(
                node_id="n-sealed-pass",
                evidence_key="n-sealed-pass",
                revision=1,
                extraction_run_id=sealed_run_id,
                node_kind="passage",
                text="Sealed fact-plane v2 passage",
                locator=EvidenceLocator(source_ref=src_ref),
                recorded_at=STAMP,
            )
        )
        node_set_json = json.dumps(["n-sealed-pass"], separators=(",", ":"))
        conn.execute(
            "INSERT INTO fact_extraction_run_completeness_seals_v2 ("
            "extraction_seal_id, idempotency_key, extraction_run_id, "
            "expected_node_count, observed_node_count, reported_fact_count, "
            "node_set_json, node_set_sha256, observation_set_json, observation_set_sha256, "
            "extractor_config_sha256, extraction_output_sha256, "
            "completeness_policy_name, completeness_policy_version, completeness_policy_sha256, "
            "knowledge_at, recorded_at"
            ") VALUES ('seal-v2-1', 'seal-v2-1', ?, 1, 1, 0, ?, ?, '[]', ?, ?, ?, "
            "'fact-plane-v2', 'v1', ?, ?, ?)",
            (
                sealed_run_id,
                node_set_json,
                _sha(node_set_json),
                _sha("[]"),
                "1" * 64,
                "2" * 64,
                "3" * 64,
                STAMP.isoformat(),
                STAMP.isoformat(),
            ),
        )

        # Seed active + inactive filing_sections with matching filing_section_items.
        conn.execute(
            "INSERT INTO filing_sections ("
            "id, ticker, source, source_ref, form, fiscal_period, section_key_raw, section_stem, "
            "ordinal, text, text_sha256, char_len, extractor_version, created_at, is_active"
            ") VALUES "
            "(501, 'CMP', 'sec', 'ref-keep', '10-K', 'FY', 'item_1', 'item_1', 1, 'keep', ?, 4, 'v1', ?, 1), "
            "(502, 'CMP', 'sec', 'ref-drop', '10-K', 'FY', 'item_1', 'item_1', 1, 'drop', ?, 4, 'v1', ?, 0)",
            (_sha("keep"), STAMP.isoformat(), _sha("drop"), STAMP.isoformat()),
        )
        conn.execute(
            "INSERT INTO filing_section_items ("
            "ticker, section_id, source_ref, form, fiscal_year, fiscal_period, canonical_id, "
            "item_ordinal, heading, match_key, body, body_sha256, char_len, extractor_version, created_at"
            ") VALUES "
            "('CMP', 501, 'ref-keep', '10-K', 2026, 'FY', 'risk_factors', 1, 'H1', 'mk1', 'b1', ?, 2, 'v1', ?), "
            "('CMP', 502, 'ref-drop', '10-K', 2026, 'FY', 'risk_factors', 1, 'H2', 'mk2', 'b2', ?, 2, 'v1', ?)",
            (_sha("b1"), STAMP.isoformat(), _sha("b2"), STAMP.isoformat()),
        )
        conn.commit()

    # Pass 1: default table_cell-only compaction archives 120 leaf cells + n-free-cell = 121 nodes.
    rc = compactor.main(["--db-path", str(db_path), "--state-root", str(state_root), "--apply"])
    assert rc == 0
    pass1 = json.loads(capsys.readouterr().out)["step_a_evidence_nodes"]
    assert pass1["archive_substantive_nodes"] is False
    assert pass1["archived_runs"] == 1
    assert pass1["updated_archived_runs"] == 0
    assert pass1["archived_nodes"] == 121
    assert pass1["deleted_nodes"] == 121

    with connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY) as conn:
        receipt1 = archive_receipt_for_run(conn, fulltext_run_id)
        assert receipt1 is not None
        assert receipt1.archived_node_count == 121
        assert receipt1.archived_node_kinds == ("table_cell",)
        orig_output_sha = receipt1.original_output_sha256

    # Pass 2: --archive-substantive-nodes peels n-pass, n-child-of-cell -> n-parent-cell,
    # and n-free-row -> n-free-table (5 newly eligible nodes) and merges into existing archive!
    # Also test Step B deletes orphaned filing_section_items for section_id=502.
    rc = compactor.main(
        [
            "--db-path",
            str(db_path),
            "--state-root",
            str(state_root),
            "--apply",
            "--archive-substantive-nodes",
            "--prune-inactive-filing-sections",
        ]
    )
    assert rc == 0
    pass2_out = json.loads(capsys.readouterr().out)
    pass2 = pass2_out["step_a_evidence_nodes"]
    assert pass2["archive_substantive_nodes"] is True
    assert pass2["archived_runs"] == 1
    assert pass2["updated_archived_runs"] == 1
    assert pass2["archived_nodes"] == 5
    assert pass2["deleted_nodes"] == 5
    assert pass2_out["step_b_filing_sections"]["deleted_rows"] == 1

    with connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY) as conn:
        receipt2 = archive_receipt_for_run(conn, fulltext_run_id)
        assert receipt2 is not None
        assert receipt2.archived_node_count == 126
        assert receipt2.original_output_sha256 == orig_output_sha
        assert receipt2.archive_sha256 != receipt1.archive_sha256
        assert set(receipt2.archived_node_kinds) == {
            "passage",
            "table",
            "table_cell",
            "table_row",
        }

        # Live survivors: n-doc, n-sec, n-row -> n-fk-cell, n-poly-table -> n-poly-row -> n-poly-cell,
        # n-other-ext-cell, and n-sealed-pass (fact_extraction_run_completeness_seals_v2).
        live_ids = {
            str(r[0]) for r in conn.execute("SELECT node_id FROM evidence_nodes").fetchall()
        }
        assert live_ids == {
            "n-doc",
            "n-sec",
            "n-row",
            "n-fk-cell",
            "n-poly-table",
            "n-poly-row",
            "n-poly-cell",
            "n-other-ext-cell",
            "n-sealed-pass",
        }

        # Round-trip load_run_nodes recovers all 133 nodes of fulltext_run_id (7 live + 126 archived).
        merged = load_run_nodes(conn, fulltext_run_id, state_root=state_root)
        assert len(merged) == 133
        assert len({str(n["node_id"]) for n in merged}) == 133

        # Orphaned filing_section_items for section 502 deleted; section 501 item survives.
        remaining_item_sections = [
            int(r[0])
            for r in conn.execute("SELECT section_id FROM filing_section_items").fetchall()
        ]
        assert remaining_item_sections == [501]

        # Integrity audit clean (archive verified, sealed v2 extraction run intact).
        audit = audit_connection(conn, AuditOptions(state_root=state_root))
        assert not any(
            f.code.startswith(("EVIDENCE_ARCHIVE_", "FACT_PLANE_V2_EXTRACTION_SEAL"))
            for f in audit.findings
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    # Pass 3: idempotent second substantive pass finds 0 eligible nodes.
    rc = compactor.main(
        [
            "--db-path",
            str(db_path),
            "--state-root",
            str(state_root),
            "--apply",
            "--archive-substantive-nodes",
        ]
    )
    assert rc == 0
    pass3 = json.loads(capsys.readouterr().out)["step_a_evidence_nodes"]
    assert pass3["eligible_runs"] == 0
    assert pass3["deleted_nodes"] == 0


def test_six_readers_and_corpus_rehydration_after_full_substantive_compaction(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    from pipeline.kpi_semantic_review import (
        KpiSemanticReviewState,
        build_quarantined_kpi_correction_review,
    )
    from provenance import fulltext_backfill
    from provenance.fulltext_extractor_identity import (
        STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR,
        select_approved_fulltext_run,
    )
    from provenance.integrity_audit import AuditOptions, audit_connection
    from provenance.source_coverage_refresh import (
        CoverageRefreshRequest,
        refresh_source_coverage,
    )
    from search.corpus_builder import (
        CorpusBuildRequest,
        ExpectedDocument,
        build_grounded_search_corpus,
    )

    state_root = tmp_path / "readers_state"
    db_path = migrated_db(state_root / "data" / "portfolio.db")
    src_url = "https://www.sec.gov/Archives/edgar/data/77/000000007726000001/q2.htm"
    doc_ver_id = "doc-rdr-1"
    failed_run_id = "run-rdr-failed"
    good_run_id = "run-rdr-good"
    t_earlier = STAMP - timedelta(hours=2)
    t_extract = STAMP - timedelta(hours=1)
    t_refresh = STAMP

    with connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER) as conn:
        ledger = EvidenceLedger(conn)
        blob_bytes = b"<html><body><p>Revenue reached 42,500 units in Q2.</p></body></html>"
        blob_sha = hashlib.sha256(blob_bytes).hexdigest()

        # Seed tracked company + portfolio holding (for kpi_semantic_review).
        conn.execute(
            "INSERT INTO tracked_companies (ticker, name, list_type) "
            "VALUES ('RDR', 'Reader Corp', 'portfolio')"
        )
        conn.execute(
            "INSERT INTO documents ("
            "id, ticker, source_type, doc_type, period_end, file_path, sha256, "
            "fetched_at, fetch_status, raw_bytes_size"
            ") VALUES (701, 'RDR', 'ir_doc', 'earnings_release', '2026-06-30', ?, ?, ?, 'ok', ?)",
            (src_url, blob_sha, t_earlier.isoformat(), len(blob_bytes)),
        )
        ledger.persist(
            ContentBlob(
                sha256=blob_sha,
                byte_size=len(blob_bytes),
                media_type="text/html",
                storage_uri="file:///synthetic/q2.htm",
                recorded_at=t_earlier,
            )
        )
        ledger.persist(
            SourceObservation(
                observation_id="obs-rdr-1",
                idempotency_key="obs-rdr-1",
                source_kind="sec_filing_document",
                source_url=src_url,
                blob_sha256=blob_sha,
                source_published_at=t_earlier,
                filing_at=t_earlier,
                accepted_at=t_earlier,
                observed_at=t_earlier,
                retrieved_at=t_earlier,
                retrieval_config_sha256="a" * 64,
                collector_code_version="test.v1",
            )
        )
        ledger.persist(
            DocumentVersion(
                document_version_id=doc_ver_id,
                document_key="doc-rdr-key-1",
                version_sequence=1,
                observation_id="obs-rdr-1",
                blob_sha256=blob_sha,
                issuer_id="iss-rdr",
                ticker="RDR",
                document_type="filing",
                form_type="10-Q",
                accession_number="0000000077-26-000001",
                period_start=datetime(2026, 4, 1, tzinfo=UTC),
                period_end=datetime(2026, 6, 30, tzinfo=UTC),
                language="en",
                legacy_document_id=701,
                recorded_at=t_earlier,
            )
        )
        # Seed issuer + inventory + expected_document + captured coverage assessment.
        conn.execute(
            "INSERT INTO issuer_entities (issuer_id, idempotency_key, entity_kind, created_at) "
            "VALUES ('iss-rdr', 'iss-rdr', 'operating_company', ?)",
            (t_earlier.isoformat(),),
        )
        conn.execute(
            "INSERT INTO source_inventory_snapshots ("
            "snapshot_id, idempotency_key, inventory_key, revision, issuer_id, ticker, "
            "source_kind, source_url, source_observation_id, outcome, authoritative, "
            "retrieval_config_sha256, collector_code_version, started_at, completed_at, recorded_at"
            ") VALUES ('snap-rdr', 'snap-rdr', 'inv:rdr', 1, 'iss-rdr', 'RDR', 'sec_submissions', "
            "?, 'obs-rdr-1', 'succeeded', 1, ?, 'test.v1', ?, ?, ?)",
            (
                src_url,
                "b" * 64,
                t_earlier.isoformat(),
                t_earlier.isoformat(),
                t_earlier.isoformat(),
            ),
        )
        conn.execute(
            "INSERT INTO expected_documents ("
            "expected_document_id, idempotency_key, snapshot_id, expected_document_key, "
            "issuer_id, ticker, source_kind, document_type, expectation_basis, recorded_at"
            ") VALUES ('ed-rdr-1', 'ed-rdr-1', 'snap-rdr', 'exp:rdr:1', 'iss-rdr', 'RDR', "
            "'sec_filing', 'filing', 'authoritative', ?)",
            (t_earlier.isoformat(),),
        )
        conn.execute(
            "INSERT INTO source_coverage_assessments ("
            "assessment_id, idempotency_key, expected_document_id, revision, coverage_status, "
            "document_version_id, reason_code, reason_details_json, decision_kind, policy_name, "
            "policy_version, policy_config_sha256, effective_at, knowledge_at, recorded_at, "
            "supersedes_assessment_id, material_dissent"
            ") VALUES ('asm-rdr-1', 'asm-rdr-1', 'ed-rdr-1', 1, 'captured', ?, 'captured', "
            "'{}', 'deterministic', 'pol', 'v1', ?, ?, ?, ?, NULL, 0)",
            (
                doc_ver_id,
                "c" * 64,
                t_earlier.isoformat(),
                t_earlier.isoformat(),
                t_earlier.isoformat(),
            ),
        )

        # Earlier failed run (older extractor generation) + later succeeded promoted HTML run.
        ledger.persist(
            ExtractionRun(
                extraction_run_id=failed_run_id,
                idempotency_key=failed_run_id,
                document_version_id=doc_ver_id,
                input_sha256=blob_sha,
                extractor_name=STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR.name,
                extractor_config_sha256="f" * 64,
                extractor_code_version="fulltext-evidence-backfill@1",
                output_sha256="d" * 64,
                started_at=t_earlier,
                completed_at=t_earlier,
                outcome="failed",
            )
        )
        ledger.persist(
            ExtractionRun(
                extraction_run_id=good_run_id,
                idempotency_key=good_run_id,
                document_version_id=doc_ver_id,
                input_sha256=blob_sha,
                extractor_name=STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR.name,
                extractor_config_sha256=STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR.config_sha256,
                extractor_code_version=STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR.code_version,
                output_sha256="e" * 64,
                started_at=t_extract,
                completed_at=t_extract,
                outcome="succeeded",
            )
        )
        root_loc = EvidenceLocator(source_ref=src_url, char_start=0, char_end=50)
        for node in (
            EvidenceNode(
                node_id="rdr-doc",
                evidence_key="rdr-doc",
                revision=1,
                extraction_run_id=good_run_id,
                node_kind="document",
                text="root document",
                locator=root_loc,
                recorded_at=t_extract,
            ),
            EvidenceNode(
                node_id="rdr-pass",
                evidence_key="rdr-pass",
                revision=1,
                extraction_run_id=good_run_id,
                parent_node_id="rdr-doc",
                node_kind="passage",
                text="Revenue reached 42,500 units in Q2.",
                locator=EvidenceLocator(source_ref=src_url, char_start=12, char_end=47),
                recorded_at=t_extract,
            ),
            EvidenceNode(
                node_id="rdr-table",
                evidence_key="rdr-table",
                revision=1,
                extraction_run_id=good_run_id,
                parent_node_id="rdr-doc",
                node_kind="table",
                text="KPI Table",
                locator=EvidenceLocator(source_ref=src_url, table_name="kpi_tbl"),
                recorded_at=t_extract,
            ),
            EvidenceNode(
                node_id="rdr-row",
                evidence_key="rdr-row",
                revision=1,
                extraction_run_id=good_run_id,
                parent_node_id="rdr-table",
                node_kind="table_row",
                text="Active Users \u241f 42,500",
                locator=EvidenceLocator(
                    source_ref=src_url, table_name="kpi_tbl", table_row_index=0
                ),
                recorded_at=t_extract,
            ),
            EvidenceNode(
                node_id="rdr-cell",
                evidence_key="rdr-cell",
                revision=1,
                extraction_run_id=good_run_id,
                parent_node_id="rdr-row",
                node_kind="table_cell",
                text="42,500",
                locator=EvidenceLocator(
                    source_ref=src_url,
                    table_name="kpi_tbl",
                    table_row_index=0,
                    table_column_index=1,
                ),
                recorded_at=t_extract,
            ),
        ):
            ledger.persist(node)

        # Bind legacy document 701 -> rdr-doc + seed KPI definition & fact for kpi_semantic_review.
        conn.execute(
            "INSERT INTO legacy_document_evidence_binding_revisions ("
            "binding_revision_id, idempotency_key, legacy_document_id, revision, "
            "document_version_id, evidence_node_id, scope_locator_json, "
            "scope_locator_sha256, scope_content_sha256, effective_at, knowledge_at, recorded_at"
            ") VALUES ('bind-rdr-1', 'bind-rdr-1', 701, 1, ?, 'rdr-doc', ?, ?, ?, ?, ?, ?)",
            (
                doc_ver_id,
                root_loc.canonical_json,
                root_loc.canonical_sha256,
                blob_sha,
                t_extract.isoformat(),
                t_extract.isoformat(),
                t_extract.isoformat(),
            ),
        )
        conn.execute(
            "INSERT INTO kpi_definitions (id, ticker, name, unit, primary_source) "
            "VALUES (801, 'RDR', 'Active Users', 'count', 'ir_doc')"
        )
        conn.execute(
            "INSERT INTO kpi_facts ("
            "id, ticker, period_end, fiscal_period_type, kpi_definition_id, value, unit, "
            "source_doc_id"
            ") VALUES (901, 'RDR', '2026-06-30', 'Q', 801, 42500, 'count', 701)"
        )
        conn.commit()

    # Archive ALL substantive nodes (passage, table, table_row, table_cell), leaving only rdr-doc live!
    rc = compactor.main(
        [
            "--db-path",
            str(db_path),
            "--state-root",
            str(state_root),
            "--apply",
            "--archive-substantive-nodes",
        ]
    )
    assert rc == 0
    receipt = json.loads(capsys.readouterr().out)["step_a_evidence_nodes"]
    assert receipt["archived_runs"] == 1
    assert receipt["archived_nodes"] == 4
    assert receipt["deleted_nodes"] == 4

    with connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER) as conn:
        live_ids = {
            str(r[0]) for r in conn.execute("SELECT node_id FROM evidence_nodes").fetchall()
        }
        assert live_ids == {"rdr-doc"}

        # 1. select_approved_fulltext_run (with & without cutoff, zero disk I/O).
        assert (
            select_approved_fulltext_run(
                conn, doc_ver_id, STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR
            )
            == good_run_id
        )
        assert (
            select_approved_fulltext_run(
                conn,
                doc_ver_id,
                STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR,
                observed_through=t_refresh,
            )
            == good_run_id
        )
        assert (
            select_approved_fulltext_run(
                conn,
                doc_ver_id,
                STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR,
                observed_through=t_extract - timedelta(seconds=1),
            )
            is None
        )

        # 2. fulltext_backfill._has_substantive_coverage recognizes archived run.
        has_coverage = getattr(fulltext_backfill, "_has_substantive_coverage")
        assert has_coverage(conn, doc_ver_id, STRUCTURED_WEB_ARCHIVE_FULLTEXT_EXTRACTOR) is True

        # 3. source_coverage_refresh promotes captured -> extracted cleanly.
        refresh_summary = refresh_source_coverage(
            conn,
            CoverageRefreshRequest(
                inventory_keys=("inv:rdr",),
                recorded_at=t_refresh,
                apply=True,
            ),
        )
        assert refresh_summary.assessments_created == 1
        assert refresh_summary.target_status_counts == {"extracted": 1}

        # 4. integrity_audit reports zero false-positive failed extraction or archive findings.
        audit = audit_connection(conn, AuditOptions(state_root=state_root))
        assert not any(
            f.code
            in {
                "SEMANTIC_DISPOSITION_MISSING_AFTER_FAILED_EXTRACTION",
                "EVIDENCE_ARCHIVE_RUN_MISMATCH",
                "EVIDENCE_ARCHIVE_INVALID_METADATA",
                "EVIDENCE_ARCHIVE_FILE_INVALID",
            }
            for f in audit.findings
        )

        # 5. kpi_semantic_review reads archived nodes cleanly via load_run_nodes.
        from identity import DEFAULT_USER_ID

        kpi_batch = build_quarantined_kpi_correction_review(
            conn,
            repo_root=tmp_path,
            user_id=DEFAULT_USER_ID,
            fact_id=901,
            source_value_text="42,500",
            observed_at=t_refresh,
            state_root=state_root,
        )
        assert len(kpi_batch.items) == 1
        assert kpi_batch.items[0].state is KpiSemanticReviewState.SOURCE_REVIEW_REQUIRED
        assert {c.evidence_node_id for c in kpi_batch.items[0].evidence_candidates} >= {
            "rdr-pass",
            "rdr-row",
            "rdr-cell",
        }

        # 6. corpus_builder dry-run does NOT mutate evidence_nodes, then apply rehydrates & indexes!
        dry_req = CorpusBuildRequest(
            corpus_key="corpus:rdr",
            revision=1,
            selector_code_version="selector.v1",
            recorded_at=t_refresh,
            knowledge_cutoff=t_refresh,
            expected_documents=(
                ExpectedDocument(
                    expected_document_key="exp:rdr:1",
                    document_version_id=doc_ver_id,
                    membership_status="included",
                    reason="test",
                ),
            ),
            apply=False,
        )
        dry_res = build_grounded_search_corpus(conn, dry_req, state_root=state_root)
        assert dry_res.completion_status == "complete"
        assert dry_res.chunks_planned == 2
        assert {
            str(r[0]) for r in conn.execute("SELECT node_id FROM evidence_nodes").fetchall()
        } == {"rdr-doc"}

        apply_req = dry_req.model_copy(update={"apply": True})
        apply_res = build_grounded_search_corpus(conn, apply_req, state_root=state_root)
        assert apply_res.completion_status == "complete"
        assert apply_res.chunks_planned == 2

        # Rehydrated live nodes: rdr-doc + rdr-pass + rdr-table (ancestor) + rdr-row; rdr-cell stays archive-only.
        live_after_index = {
            str(r[0]) for r in conn.execute("SELECT node_id FROM evidence_nodes").fetchall()
        }
        assert live_after_index == {"rdr-doc", "rdr-pass", "rdr-table", "rdr-row"}
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

        # load_run_nodes deduplicates rehydrated live rows + archived rows cleanly!
        merged = load_run_nodes(conn, good_run_id, state_root=state_root)
        assert [str(n["node_id"]) for n in merged] == [
            "rdr-cell",
            "rdr-doc",
            "rdr-pass",
            "rdr-row",
            "rdr-table",
        ]
        merged_rowid = load_run_nodes(conn, good_run_id, state_root=state_root, include_rowid=True)
        assert [str(n["node_id"]) for n in merged_rowid] == [
            "rdr-doc",
            "rdr-pass",
            "rdr-table",
            "rdr-row",
            "rdr-cell",
        ]
