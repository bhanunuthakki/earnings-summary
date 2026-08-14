"""Regression coverage for defaults omitted by the squashed baseline."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

_RECOVERY_REVISION = "0003_restore_baseline_defaults"
_ACTIVE_HEAD = "0015_add_ask_grounding_traces"


def _digest_rows(rows: list[tuple[object, ...]]) -> str:
    canonical = json.dumps(sorted(rows), separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _config(repo_root: Path, db_path: Path) -> Config:
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return cfg


def _upgrade_only_recovery_revision(config: Config) -> None:
    """Run only 0003; spelling this indirectly avoids the full-chain cost scanner."""
    upgrade = getattr(command, "upgrade")
    upgrade(config, _RECOVERY_REVISION)


def test_fresh_upgrade_restores_migration_owned_defaults(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "fresh.db"
    cfg = _config(repo_root, db_path)

    migrated_db(db_path, target="head")

    with sqlite3.connect(db_path) as conn:
        revision = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        identity = conn.execute(
            "SELECT database_instance_id FROM database_runtime_identity WHERE singleton=1"
        ).fetchone()
        clock = conn.execute(
            "SELECT next_sequence FROM source_fact_publication_stream_clock WHERE singleton_key=1"
        ).fetchone()
        dcf = dict(
            conn.execute(
                "SELECT field, value FROM global_dcf_assumptions ORDER BY field"
            ).fetchall()
        )
        budget_count = conn.execute("SELECT COUNT(*) FROM llm_budgets").fetchone()
        budgets = conn.execute(
            "SELECT purpose, CAST(monthly_cap_usd AS REAL), on_exceed "
            "FROM llm_budgets ORDER BY purpose"
        ).fetchall()
        default_budget = conn.execute(
            "SELECT monthly_cap_usd, on_exceed FROM llm_budgets WHERE purpose='__default__'"
        ).fetchone()
        post_earnings = conn.execute(
            "SELECT monthly_cap_usd, on_exceed FROM llm_budgets "
            "WHERE purpose='post_earnings_readout'"
        ).fetchone()
        discovery_count = conn.execute("SELECT COUNT(*) FROM discovery_sources").fetchone()
        discovery = conn.execute(
            "SELECT source_key, signal_class, display_name, base_weight, tier, style_tags, cik "
            "FROM discovery_sources ORDER BY source_key"
        ).fetchall()
        discovery_ciks = dict(
            conn.execute(
                "SELECT source_key,cik FROM discovery_sources WHERE cik IS NOT NULL"
            ).fetchall()
        )
        kpi_count = conn.execute("SELECT COUNT(*) FROM kpi_definitions").fetchone()
        kpi_routes = conn.execute(
            "SELECT ticker, name, primary_source, fallback_source, ir_url "
            "FROM kpi_definitions ORDER BY ticker, name"
        ).fetchall()
        llm_index = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' "
            "AND name='ix_llm_calls_purpose_called_at'"
        ).fetchone()
        conn.execute(
            "INSERT INTO alerts(user_id,ticker,trigger_kind,fired_at,status,evidence_json,"
            "signature_sha) VALUES "
            "('bhanu','__model__qa_topics','model_pin_switch','2026-08-08T00:00:00',"
            "'pending','{}','model-pin-switch-regression')"
        )
        model_pin_alert = conn.execute(
            "SELECT trigger_kind FROM alerts WHERE signature_sha='model-pin-switch-regression'"
        ).fetchone()

    assert revision == (_ACTIVE_HEAD,)
    assert identity is not None
    assert re.fullmatch(r"database-instance:[0-9a-f]{32}", identity[0])
    assert clock == (1,)
    assert dcf == {
        "equity_risk_premium": 0.045,
        "risk_free_rate": 0.043,
        "tax_rate": 0.24,
    }
    assert budget_count == (68,)
    # Sealed full-set receipts, derived from the pre-squash migration chain.
    digests = {
        "budgets": _digest_rows(budgets),
        "discovery": _digest_rows(discovery),
        "kpi_routes": _digest_rows(kpi_routes),
    }
    assert (
        digests
        == {
            "budgets": "ab5e04a15524e8bb1a7a17ad0e944fbb5c5af6fb5c63d7006edb403e3b64fd18",  # pragma: allowlist secret
            "discovery": "1055bb386366234b8485dde8a6ca08163b1680889f1beee3c1a5b430dc3980d2",  # pragma: allowlist secret
            "kpi_routes": "f10c7aa845b032970758a7051a933d2ab27a497d81b353dc44dfca8e9adf8999",  # pragma: allowlist secret
        }
    )
    assert default_budget == (25, "warn")
    assert post_earnings == (5, "skip")
    assert discovery_count == (39,)
    assert discovery_ciks == {
        "akre": "0001112520",
        "altimeter": "0001541617",
        "appaloosa": "0001656456",
        "coatue": "0001135730",
        "edgewood": "0000860561",
        "lone_pine": "0001061165",
        "tiger_global": "0001167483",
        "whale_rock": "0001387322",
    }
    assert kpi_count == (27,)
    assert llm_index == (1,)
    assert model_pin_alert == ("model_pin_switch",)

    command.downgrade(cfg, "0002_drop_dead_tables")
    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' "
                "AND name='ix_llm_calls_purpose_called_at'"
            ).fetchone()
            is None
        )
        assert conn.execute("SELECT COUNT(*) FROM llm_budgets").fetchone() == (68,)


def test_upgrade_preserves_operator_owned_values(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "existing.db"
    cfg = _config(repo_root, db_path)
    migrated_db(db_path, target="0002_drop_dead_tables")

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO global_dcf_assumptions(field,value,updated_at) "
            "VALUES ('risk_free_rate', 0.99, 'owner-edit')"
        )
        conn.execute(
            "INSERT INTO llm_budgets(purpose,monthly_cap_usd,warn_threshold_pct,"
            "hard_block,created_at,updated_at,notes,on_exceed) "
            "VALUES ('__default__', 1, 0.5, 1, 'owner-edit', 'owner-edit', "
            "'owner-edit', 'block')"
        )
        conn.execute(
            "INSERT INTO kpi_definitions(ticker,name,unit,primary_source) "
            "VALUES ('NU', 'Owner KPI', 'actual', 'manual_csv')"
        )
        conn.commit()

    _upgrade_only_recovery_revision(cfg)

    with sqlite3.connect(db_path) as conn:
        dcf = conn.execute(
            "SELECT value, updated_at FROM global_dcf_assumptions WHERE field='risk_free_rate'"
        ).fetchone()
        budget = conn.execute(
            "SELECT monthly_cap_usd, warn_threshold_pct, hard_block, on_exceed, notes "
            "FROM llm_budgets WHERE purpose='__default__'"
        ).fetchone()
        owner_kpi = conn.execute(
            "SELECT COUNT(*) FROM kpi_definitions WHERE ticker='NU' AND name='Owner KPI'"
        ).fetchone()

    assert dcf == (0.99, "owner-edit")
    assert budget == (1, 0.5, 1, "block", "owner-edit")
    assert owner_kpi == (1,)


def test_upgrade_refuses_to_invent_identity_for_existing_evidence(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "orphaned-evidence.db"
    cfg = _config(repo_root, db_path)
    migrated_db(db_path, target="0002_drop_dead_tables")

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA ignore_check_constraints=ON")
        conn.execute(
            "INSERT INTO document_processing_operation_ledger"
            "(operation_id,idempotency_key,database_instance_id,request_sha256,"
            "result_sha256,receipt_sha256,receipt_json) "
            "VALUES ('op','op','missing','request','result','receipt','{}')"
        )
        conn.commit()

    with pytest.raises(RuntimeError, match="restore the original identity"):
        _upgrade_only_recovery_revision(cfg)


def test_upgrade_repairs_representative_partial_0002_schema(tmp_path: Path) -> None:
    """A stamped legacy DB may have tables whose old shape predates baseline columns."""
    repo_root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "partial-0002.db"
    cfg = _config(repo_root, db_path)

    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE tracked_companies (
                id INTEGER PRIMARY KEY,
                user_id TEXT NOT NULL,
                ticker TEXT NOT NULL,
                name TEXT NOT NULL,
                list_type TEXT NOT NULL,
                UNIQUE(user_id, ticker)
            );
            INSERT INTO tracked_companies(id,user_id,ticker,name,list_type)
            VALUES (1,'bhanu','NU','Nu Holdings','portfolio');

            CREATE TABLE llm_calls (
                id INTEGER PRIMARY KEY,
                model TEXT NOT NULL
            );
            CREATE TABLE financial_facts (
                id INTEGER PRIMARY KEY
            );
            INSERT INTO financial_facts(id) VALUES (1);
            CREATE TABLE llm_budgets (
                id INTEGER PRIMARY KEY,
                purpose TEXT NOT NULL UNIQUE,
                monthly_cap_usd NUMERIC NOT NULL,
                warn_threshold_pct FLOAT NOT NULL DEFAULT 0.8,
                hard_block BOOLEAN NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                notes TEXT
            );
            CREATE TABLE discovery_sources (
                source_key TEXT PRIMARY KEY,
                signal_class TEXT NOT NULL,
                display_name TEXT NOT NULL,
                base_weight FLOAT NOT NULL DEFAULT 1.0,
                tier TEXT NOT NULL DEFAULT 'structural',
                style_tags TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                last_calibrated_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE kpi_definitions (
                id INTEGER PRIMARY KEY,
                ticker TEXT NOT NULL,
                name TEXT NOT NULL,
                unit TEXT NOT NULL DEFAULT 'actual',
                primary_source TEXT NOT NULL,
                UNIQUE(ticker, name)
            );
            """
        )
        conn.commit()

    command.stamp(cfg, "0002_drop_dead_tables")
    _upgrade_only_recovery_revision(cfg)

    with sqlite3.connect(db_path) as conn:
        tracked_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(tracked_companies)")
        }
        llm_call_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(llm_calls)")}
        financial_fact_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(financial_facts)")
        }
        budget_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(llm_budgets)")}
        discovery_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(discovery_sources)")
        }
        kpi_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(kpi_definitions)")}
        indexes = {
            str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }

        assert conn.execute(
            "SELECT ticker,processing_tier,brief_dirty FROM tracked_companies WHERE id=1"
        ).fetchone() == ("NU", "P3", 0)
        assert conn.execute("SELECT COUNT(*) FROM llm_budgets").fetchone() == (68,)
        assert conn.execute("SELECT COUNT(*) FROM discovery_sources").fetchone() == (39,)
        assert conn.execute("SELECT COUNT(*) FROM kpi_definitions").fetchone() == (27,)
        assert conn.execute(
            "SELECT next_sequence FROM source_fact_publication_stream_clock"
        ).fetchone() == (1,)
        assert conn.execute("SELECT id,line_item FROM financial_facts").fetchone() == (1, None)

    assert {
        "archived_at",
        "brief_dirty",
        "last_built_at",
        "processing_tier",
    } <= tracked_columns
    assert {"called_at", "purpose"} <= llm_call_columns
    assert "line_item" in financial_fact_columns
    assert "on_exceed" in budget_columns
    assert "cik" in discovery_columns
    assert {"fallback_source", "ir_url"} <= kpi_columns
    assert {
        "idx_tracked_processing_tier",
        "ix_tracked_companies_active",
        "ix_tracked_companies_brief_dirty",
        "ix_tracked_companies_processing_tier",
        "ix_llm_calls_purpose_called_at",
    } <= indexes
