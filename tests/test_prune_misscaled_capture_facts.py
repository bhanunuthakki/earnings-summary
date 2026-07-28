"""Regression coverage for append-only count-unit repair decisions."""

from __future__ import annotations

import importlib.util
import sqlite3
from datetime import datetime
from pathlib import Path
from types import ModuleType

from alembic.config import Config

from alembic import command

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "execution" / "prune_misscaled_capture_facts.py"
PRIOR = "0216_search_corpus_foundation"
HEAD = "0217_fact_selection_ledger"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("prune_misscaled_capture_facts", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _conn(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "prune.db"
    config = _config(path)
    command.stamp(config, PRIOR)
    command.upgrade(config, HEAD)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("CREATE TABLE evidence_nodes (node_id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE validation_issues (id INTEGER PRIMARY KEY)")
    conn.execute(
        "CREATE TABLE kpi_definitions (id INTEGER PRIMARY KEY, ticker TEXT, name TEXT, definition_origin TEXT)"
    )
    conn.execute(
        "CREATE TABLE kpi_facts (id INTEGER PRIMARY KEY, kpi_definition_id INTEGER, unit TEXT, value REAL)"
    )
    conn.execute(
        "INSERT INTO kpi_definitions (id, ticker, name, definition_origin) "
        "VALUES (1, 'ACME', 'Authorized Shares (in shares)', 'capture')"
    )
    conn.execute(
        "INSERT INTO kpi_facts (id, kpi_definition_id, unit, value) VALUES (7, 1, 'actual', 24000000000)"
    )
    return conn


def test_apply_appends_one_exclusion_and_preserves_legacy_facts(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    try:
        module = _module()
        matches = module.select_misscaled_capture_facts(conn)
        summary = module.append_exclusions(conn, matches, recorded_at=datetime(2026, 7, 26, 23))
        assert summary.decisions_appended == 1
        assert (
            conn.execute("SELECT value FROM kpi_facts WHERE id = 7").fetchone()[0] == 24000000000.0
        )
        current = conn.execute(
            "SELECT decision_id, selection_state, reason_code FROM v_fact_selection_current "
            "WHERE target_row_id = 7"
        ).fetchone()
        assert (current["selection_state"], current["reason_code"]) == (
            "excluded",
            "mis_scaled_count_unit",
        )
        assert (
            module.append_exclusions(
                conn, matches, recorded_at=datetime(2026, 7, 27)
            ).decisions_appended
            == 0
        )
        module.FactSelectionLedger(conn).persist(
            module.FactSelectionDecision(
                decision_id="manual-include-r2",
                idempotency_key="manual-include-r2",
                target_table="kpi_facts",
                target_row_id=7,
                revision=2,
                selection_state="included",
                reason_code="manual_review",
                reason_details=(("reviewer", "analyst"),),
                decision_kind="manual",
                policy_name="analyst_override",
                policy_version="1",
                policy_config_sha256="b" * 64,
                evidence_node_id=None,
                validation_issue_id=None,
                effective_at=datetime(2026, 7, 28),
                knowledge_at=datetime(2026, 7, 28),
                recorded_at=datetime(2026, 7, 28),
                supersedes_decision_id=current["decision_id"],
                material_dissent=True,
            )
        )
        assert (
            module.append_exclusions(
                conn, matches, recorded_at=datetime(2026, 7, 29)
            ).decisions_appended
            == 1
        )
        assert (
            conn.execute(
                "SELECT revision FROM v_fact_selection_current WHERE target_row_id = 7"
            ).fetchone()[0]
            == 3
        )
    finally:
        conn.close()


def test_dry_run_summary_does_not_require_a_writer(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    try:
        module = _module()
        summary = module.summarize_matches(
            module.select_misscaled_capture_facts(conn), applied=False
        )
        assert summary.mode == "dry_run"
        assert summary.matched_facts == 1
        assert conn.execute("SELECT COUNT(*) FROM fact_selection_decisions").fetchone()[0] == 0
    finally:
        conn.close()
