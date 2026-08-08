"""Regression coverage for defaults omitted by the squashed baseline."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command


def _digest_rows(rows: list[tuple[object, ...]]) -> str:
    canonical = json.dumps(sorted(rows), separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _config(repo_root: Path, db_path: Path) -> Config:
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return cfg


def test_fresh_upgrade_restores_migration_owned_defaults(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "fresh.db"
    cfg = _config(repo_root, db_path)

    command.upgrade(cfg, "head")

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

    assert revision == ("0003_restore_baseline_defaults",)
    assert identity is not None
    assert re.fullmatch(r"database-instance:[0-9a-f]{32}", identity[0])
    assert clock == (1,)
    assert dcf == {
        "equity_risk_premium": 0.045,
        "risk_free_rate": 0.043,
        "tax_rate": 0.24,
    }
    assert budget_count == (66,)
    # Sealed full-set receipts, derived from the pre-squash migration chain.
    digests = {
        "budgets": _digest_rows(budgets),
        "discovery": _digest_rows(discovery),
        "kpi_routes": _digest_rows(kpi_routes),
    }
    assert digests == {
        "budgets": "e7d9d3699bcf7fdef916f9838f979ee35cc5c5f6529d4ef068b67ff253ac6f9f",
        "discovery": "1055bb386366234b8485dde8a6ca08163b1680889f1beee3c1a5b430dc3980d2",
        "kpi_routes": "f10c7aa845b032970758a7051a933d2ab27a497d81b353dc44dfca8e9adf8999",
    }
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

    command.downgrade(cfg, "0002_drop_dead_tables")
    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' "
                "AND name='ix_llm_calls_purpose_called_at'"
            ).fetchone()
            is None
        )
        assert conn.execute("SELECT COUNT(*) FROM llm_budgets").fetchone() == (66,)


def test_upgrade_preserves_operator_owned_values(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "existing.db"
    cfg = _config(repo_root, db_path)
    command.upgrade(cfg, "0002_drop_dead_tables")

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

    command.upgrade(cfg, "head")

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
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "orphaned-evidence.db"
    cfg = _config(repo_root, db_path)
    command.upgrade(cfg, "0002_drop_dead_tables")

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
        command.upgrade(cfg, "head")
