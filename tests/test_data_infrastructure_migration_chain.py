"""Active squashed migration-chain invariants."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from alembic import command
from execution.evaluate_deletion_catalog import migration_schema_targets
from schema_compat import expected_head

ROOT = Path(__file__).resolve().parents[1]
HEAD = expected_head()
SCHEDULE_CLASS_REVISION = "0028_remove_processing_tier_and_rename_research_tasks"
RETAINED_TABLES = {
    "archive_generations",
    "ask_exchange_artifacts",
    "ask_exchanges",
    "ask_grounding_traces",
    "ask_session_contexts",
    "canonical_resolution_operation_ledger",
    "data_coverage_dispositions",
    "commitment_scan_receipts",
    "document_processing_operation_ledger",
    "evidence_content_blobs",
    "metric_ontology_operation_ledger",
    "llm_circuit_breakers",
    "ir_approval_candidates",
    "ir_approval_decisions",
    "research_snapshot_headers",
    "search_corpus_manifests",
    "managed_ir_publications",
    "managed_ir_inventory_evidence",
    "operations_attention_findings",
    "operations_attention_lifecycle_events",
    "operations_attention_action_receipts",
    "operations_attention_repair_references",
    "position_sizing_intent_withdrawals",
    "position_sizing_intent_supersessions",
    "investment_profile_label_reviews",
    "kpi_definition_comparability_revisions",
    "kpi_definition_revisions",
}


def _deleted_tables() -> set[str]:
    targets, error = migration_schema_targets(ROOT)
    assert error is None
    return targets


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def test_squashed_chain_has_one_head_and_preserves_active_schema(tmp_path: Path) -> None:
    path = tmp_path / "migration-chain.db"
    config = _config(path)
    assert ScriptDirectory.from_config(config).get_heads() == [HEAD]

    command.upgrade(config, "head")

    with sqlite3.connect(path) as conn:
        revision = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        tables = {
            str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        identity = conn.execute(
            "SELECT database_instance_id FROM database_runtime_identity WHERE singleton=1"
        ).fetchone()
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()

    assert revision == (HEAD,)
    assert tables >= RETAINED_TABLES
    assert tables.isdisjoint(_deleted_tables())
    assert identity is not None and str(identity[0]).startswith("database-instance:")
    assert integrity == ("ok",)
    assert foreign_keys == []


def test_full_retirement_chain_preserves_pre_and_post_earnings_artifact_history(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    """Normal earnings artifacts and their source context are retained by 0065."""
    path = migrated_db(
        tmp_path / "earnings-history.db",
        target="0063_transcript_reported_period_receipts",
    )
    config = _config(path)
    with sqlite3.connect(path) as conn:
        parent_id = int(
            conn.execute("SELECT COALESCE(MAX(id),0)+1 FROM llm_artifacts").fetchone()[0]
        )
        artifact_ids = {
            name: parent_id + index
            for index, name in enumerate(
                ("pre_old", "pre_current", "post_old", "post_current", "retired"), start=1
            )
        }
        call_base = int(conn.execute("SELECT COALESCE(MAX(id),0)+100 FROM llm_calls").fetchone()[0])
        call_ids = {
            name: call_base + index
            for index, name in enumerate(
                ("pre_old", "pre_current", "post_old", "post_current"), start=1
            )
        }
        for name, purpose, artifact_name in (
            ("pre_old", "pre_earnings_brief", "pre_old"),
            ("pre_current", "pre_earnings_brief", "pre_current"),
            ("post_old", "post_earnings_readout", "post_old"),
            ("post_current", "post_earnings_readout", "post_current"),
        ):
            conn.execute(
                "INSERT INTO llm_calls "
                "(id,called_at,purpose,ticker,scope,model,prompt_sha256,prompt_chars,elapsed_ms,"
                "artifact_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    call_ids[name],
                    "2026-11-01T12:00:00Z",
                    purpose,
                    "ACME",
                    "ticker",
                    "test-model",
                    "a" * 64,
                    10,
                    1,
                    artifact_ids[artifact_name],
                ),
            )

        def add_artifact(
            name: str,
            purpose: str,
            *,
            call_name: str | None = None,
            superseded_by: str | None = None,
            parent_ids: list[int] | None = None,
        ) -> None:
            artifact_id = artifact_ids[name]
            conn.execute(
                "INSERT INTO llm_artifacts "
                "(id,ticker,scope,purpose,fiscal_period,content_md,input_sha256,generated_at,"
                "superseded_by_id,parent_artifact_ids,llm_call_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    artifact_id,
                    "ACME",
                    "ticker",
                    purpose,
                    "2026Q3",
                    name,
                    f"{artifact_id:064x}",
                    "2026-11-01T12:00:00Z",
                    artifact_ids[superseded_by] if superseded_by else None,
                    json.dumps(parent_ids) if parent_ids else None,
                    call_ids[call_name] if call_name else None,
                ),
            )

        # Use a candidate lens artifact as a parent to exercise dependency closure.
        parent_id = max(artifact_ids.values()) + 1
        conn.execute(
            "INSERT INTO llm_artifacts "
            "(id,ticker,scope,purpose,fiscal_period,content_md,input_sha256,generated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                parent_id,
                "ACME",
                "ticker",
                "lens:earnings-context",
                "2026Q3",
                "source context",
                f"{parent_id:064x}",
                "2026-11-01T11:00:00Z",
            ),
        )
        add_artifact(
            "pre_current", "pre_earnings_brief", call_name="pre_current", parent_ids=[parent_id]
        )
        add_artifact(
            "post_current",
            "post_earnings_readout",
            call_name="post_current",
            parent_ids=[parent_id],
        )
        add_artifact(
            "pre_old",
            "pre_earnings_brief",
            call_name="pre_old",
            superseded_by="pre_current",
            parent_ids=[parent_id],
        )
        add_artifact(
            "post_old",
            "post_earnings_readout",
            call_name="post_old",
            superseded_by="post_current",
            parent_ids=[parent_id],
        )
        add_artifact("retired", "filing_intelligence")
        for name, purpose in (
            ("pre_current", "pre_earnings_brief"),
            ("post_current", "post_earnings_readout"),
            ("retired", "filing_intelligence"),
        ):
            conn.execute(
                "INSERT INTO prompt_calibration_scores "
                "(purpose,prompt_version,ticker,score,scored_by,artifact_id) VALUES (?,?,?,?,?,?)",
                (purpose, "v1", "ACME", 0.9, "migration-regression", artifact_ids[name]),
            )
        for purpose in ("pre_earnings_brief", "post_earnings_readout"):
            conn.execute(
                "INSERT INTO llm_budget_alerts "
                "(purpose,month,threshold_pct,alerted_at,spend_at_alert_usd) VALUES (?,?,?,?,?)",
                (purpose, "2026-11", 0.91, "2026-11-01T12:00:00Z", 4.0),
            )
            conn.execute(
                "INSERT INTO model_eval_verdicts "
                "(purpose,candidate,incumbent,verdict,run_id,recorded_at) VALUES (?,?,?,?,?,?)",
                (
                    purpose,
                    "candidate-model",
                    "incumbent-model",
                    "KEEP_INCUMBENT",
                    f"run-{purpose}",
                    "2026-11-01T12:00:00Z",
                ),
            )
        budget_purposes = {
            str(row[0])
            for row in conn.execute(
                "SELECT purpose FROM llm_budgets "
                "WHERE purpose IN ('pre_earnings_brief','post_earnings_readout')"
            )
        }
        assert budget_purposes == {"pre_earnings_brief", "post_earnings_readout"}

    command.upgrade(config, "head")

    with sqlite3.connect(path) as conn:
        artifacts = {
            int(row[0]): (str(row[1]), row[2], row[3], row[4])
            for row in conn.execute(
                "SELECT id,purpose,superseded_by_id,parent_artifact_ids,llm_call_id "
                "FROM llm_artifacts"
            )
        }
        calls = {
            int(row[0]): str(row[1]) for row in conn.execute("SELECT id,purpose FROM llm_calls")
        }
        calibration = {
            (str(row[0]), int(row[1]))
            for row in conn.execute("SELECT purpose,artifact_id FROM prompt_calibration_scores")
        }
        budgets = {
            str(row[0])
            for row in conn.execute(
                "SELECT purpose FROM llm_budgets "
                "WHERE purpose IN ('pre_earnings_brief','post_earnings_readout')"
            )
        }
        alerts = {
            str(row[0])
            for row in conn.execute(
                "SELECT purpose FROM llm_budget_alerts "
                "WHERE purpose IN ('pre_earnings_brief','post_earnings_readout')"
            )
        }
        evals = {
            str(row[0])
            for row in conn.execute(
                "SELECT purpose FROM model_eval_verdicts "
                "WHERE purpose IN ('pre_earnings_brief','post_earnings_readout')"
            )
        }
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (HEAD,)

    for name in ("pre_old", "pre_current", "post_old", "post_current"):
        artifact_id = artifact_ids[name]
        assert artifacts[artifact_id][0] == (
            "pre_earnings_brief" if name.startswith("pre_") else "post_earnings_readout"
        )
        expected_purpose = (
            "pre_earnings_brief" if name.startswith("pre_") else "post_earnings_readout"
        )
        assert calls[call_ids[name]] == expected_purpose
        assert artifacts[artifact_id][3] == call_ids[name]
    for name in ("pre_current", "post_current"):
        artifact_id = artifact_ids[name]
        assert (artifacts[artifact_id][0], artifact_id) in calibration
    for name in ("pre_old", "post_old"):
        artifact_id = artifact_ids[name]
        assert (artifacts[artifact_id][0], artifact_id) not in calibration
    assert artifacts[artifact_ids["pre_old"]][1] == artifact_ids["pre_current"]
    assert artifacts[artifact_ids["post_old"]][1] == artifact_ids["post_current"]
    for name in ("pre_old", "pre_current", "post_old", "post_current"):
        assert json.loads(artifacts[artifact_ids[name]][2]) == [parent_id]
    assert artifacts[parent_id][0] == "lens:earnings-context"
    assert artifact_ids["retired"] not in artifacts
    assert ("filing_intelligence", artifact_ids["retired"]) not in calibration
    assert budgets == alerts == evals == {"pre_earnings_brief", "post_earnings_readout"}


def test_investment_profile_review_ledger_accepts_company_and_etf_labels(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    feature_revision = "0034_add_investment_profile_label_reviews"
    parent_revision = "0033_add_report_kpi_reference_resolutions"
    path = migrated_db(tmp_path / "investment-profile-reviews.db", target=parent_revision)
    config = _config(path)
    command.upgrade(config, feature_revision)

    with sqlite3.connect(path) as conn:
        for index, label in enumerate(("long_term_compounder", "defensive_hedge"), start=1):
            conn.execute(
                "INSERT INTO investment_profile_label_reviews "
                "(ticker,label,action,suggestion_fingerprint,evidence_json,reviewed_by,"
                "idempotency_key) VALUES (?,?,?,?,?,?,?)",
                (
                    "ACME" if index == 1 else "TAIL",
                    label,
                    "ratify",
                    str(index) * 64,
                    "{}",
                    "bhanu",
                    str(index + 2) * 64,
                ),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO investment_profile_label_reviews "
                "(ticker,label,action,suggestion_fingerprint,evidence_json,reviewed_by,"
                "idempotency_key) VALUES ('BAD','fake_label','ratify',?,?,?,?)",
                ("a" * 64, "{}", "bhanu", "b" * 64),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE investment_profile_label_reviews SET action='reject' WHERE id=1")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM investment_profile_label_reviews WHERE id=2")

    command.downgrade(config, parent_revision)
    with sqlite3.connect(path) as conn:
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='investment_profile_label_reviews'"
            ).fetchone()
            is None
        )
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            parent_revision,
        )

    command.upgrade(config, feature_revision)
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='investment_profile_label_reviews'"
        ).fetchone() == ("investment_profile_label_reviews",)


def test_managed_ir_publication_migration_is_reversible(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    revision = "0021_managed_ir_publications"
    path = migrated_db(tmp_path / "managed-ir-publications.db", target="0020_kpi_fact_currency")
    config = _config(path)
    with sqlite3.connect(path) as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='managed_ir_publications'"
            ).fetchone()
            is None
        )

    command.upgrade(config, revision)
    with sqlite3.connect(path) as conn:
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(managed_ir_publications)")
        }
        assert {
            "attempt_id",
            "created_paths_json",
            "staging_receipt_path",
            "inventory_receipt_path",
            "publication_result_path",
            "intent_sha256",
            "payload_sha256",
        } <= columns
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (revision,)
        conn.execute(
            "INSERT INTO managed_ir_publications VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "attempt-0001",
                "a" * 64,
                "b" * 64,
                "[]",
                "[]",
                "[]",
                "[]",
                "data/managed_ir_publications/attempt-0001/staging_receipt.json",
                "data/managed_ir_publications/attempt-0001/inventory_receipt.json",
                "data/managed_ir_publications/attempt-0001/publication_result.json",
                "c" * 64,
                "d" * 64,
                "now",
                "committed",
                "e" * 64,
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM managed_ir_publications")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE managed_ir_publications SET state='committed'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "INSERT OR REPLACE INTO managed_ir_publications VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "attempt-0001",
                    "a" * 64,
                    "b" * 64,
                    "[]",
                    "[]",
                    "[]",
                    "[]",
                    "data/managed_ir_publications/attempt-0001/staging_receipt.json",
                    "data/managed_ir_publications/attempt-0001/inventory_receipt.json",
                    "data/managed_ir_publications/attempt-0001/publication_result.json",
                    "c" * 64,
                    "d" * 64,
                    "now",
                    "committed",
                    "e" * 64,
                ),
            )
        conn.execute(
            "INSERT INTO managed_ir_inventory_evidence VALUES (?,?,?,?,?,?)",
            (
                "attempt-0001",
                "e" * 64,
                "data/managed_ir_publications/attempt-0001/inventory_receipt.json",
                "f" * 64,
                "d" * 64,
                "a" * 64,
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "INSERT OR REPLACE INTO managed_ir_inventory_evidence VALUES (?,?,?,?,?,?)",
                (
                    "attempt-0001",
                    "e" * 64,
                    "data/managed_ir_publications/attempt-0001/inventory_receipt.json",
                    "f" * 64,
                    "d" * 64,
                    "a" * 64,
                ),
            )

    command.downgrade(config, "0020_kpi_fact_currency")
    with sqlite3.connect(path) as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='managed_ir_publications'"
            ).fetchone()
            is None
        )
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='managed_ir_inventory_evidence'"
            ).fetchone()
            is None
        )
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'managed_ir_publications_%'"
            ).fetchone()
            is None
        )
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0020_kpi_fact_currency",
        )


def test_schedule_class_migration_preserves_research_storage_and_derives_downgrade_tiers(
    tmp_path: Path,
) -> None:
    path = tmp_path / "schedule-class.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE alembic_version (version_num VARCHAR(64) NOT NULL);
            INSERT INTO alembic_version VALUES ('0027_add_sizing_intent_supersessions');
            CREATE TABLE tracked_companies (
                ticker TEXT, list_type TEXT, last_built_at TEXT, archived_at TEXT,
                processing_tier TEXT DEFAULT 'P3'
            );
            INSERT INTO tracked_companies VALUES
                ('A', 'portfolio', NULL, NULL, 'P1'),
                ('B', 'watchlist', NULL, NULL, 'P2'),
                ('C', 'evaluation', NULL, NULL, 'P2'),
                ('D', 'index_member', NULL, NULL, 'P3'),
                ('E', 'none', NULL, NULL, 'P3');
            CREATE TABLE research_tasks (id INTEGER, cost_usd FLOAT, run_id TEXT);
            INSERT INTO research_tasks VALUES (1, 12.5, '{"run":"r1"}');
            CREATE INDEX idx_tracked_processing_tier
                ON tracked_companies(processing_tier,last_built_at);
            CREATE INDEX ix_tracked_companies_processing_tier
                ON tracked_companies(processing_tier);
            """
        )

    config = _config(path)
    command.upgrade(config, SCHEDULE_CLASS_REVISION)
    with sqlite3.connect(path) as conn:
        tracked_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(tracked_companies)")
        }
        research_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(research_tasks)")
        }
        assert "processing_tier" not in tracked_columns
        assert {"cost_usd", "run_id"} <= research_columns
        assert conn.execute("SELECT cost_usd, run_id FROM research_tasks").fetchone() == (
            12.5,
            '{"run":"r1"}',
        )

    command.downgrade(config, "0027_add_sizing_intent_supersessions")
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT ticker, processing_tier FROM tracked_companies ORDER BY ticker"
        ).fetchall() == [
            ("A", "P1"),
            ("B", "P2"),
            ("C", "P2"),
            ("D", "P3"),
            ("E", "P3"),
        ]
