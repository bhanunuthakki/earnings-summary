"""Independent regressions for source-plan binding and historical read safety."""

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from position_lifecycle import get_entry, list_entries, update_exit_fields
from provenance.population_source_facts import (
    SourceFactPopulationRequest,
    SourceFactPopulationResult,
)
from sources import foreign_normalization_run as run
from tests.test_foreign_normalization_run import seed_foreign_fixture


def test_apply_does_not_publish_outside_retained_plan(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = migrated_db(tmp_path / "foreign.db")
    with sqlite3.connect(path) as conn:
        manifest = seed_foreign_fixture(conn, tmp_path)
        original = run.populate_source_fact_plane

        def changed(
            connection: sqlite3.Connection, request: SourceFactPopulationRequest
        ) -> SourceFactPopulationResult:
            if request.apply:
                connection.execute(
                    "INSERT INTO reported_observations (observation_id,idempotency_key,issuer_id,ticker,concept_key,period_start,period_end,fiscal_period_type,dimensions_json,numeric_value,currency,unit,observation_status,evidence_node_id,available_at,recorded_at,method,method_version,confidence) SELECT 'new-1','new-1',issuer_id,ticker,'new_metric',period_start,period_end,fiscal_period_type,dimensions_json,'200','EUR','EUR','reported',evidence_node_id,available_at,recorded_at,method,method_version,confidence FROM reported_observations WHERE observation_id='observation-1'"
                )
                connection.execute(
                    "INSERT INTO fact_observation_revisions SELECT fact_table,2,fact_revision,'new-1','new-financial-1',source_document_id,source_tier,locator_json,captured_at FROM fact_observation_revisions"
                )
                connection.commit()
            return original(connection, request)

        monkeypatch.setattr(run, "populate_source_fact_plane", changed)
        receipt = run.normalize_foreign_sources(
            conn, manifest, input_manifest_sha256="4" * 64, apply=True
        )
        assert receipt.source_population_plan is not None
        currencies = [r[0] for r in conn.execute("SELECT currency FROM fact_cells_v2")]
        assert receipt.status == "HOLD"
        assert receipt.reason_codes == ("source_population_plan_changed",)
        assert receipt.source_population_plan.expected_count == 1
        assert receipt.source_population_plan.eligible_count == 1
        assert receipt.receipts == ()
        assert currencies == []
        assert conn.execute("SELECT count(*) FROM source_fact_publications").fetchone()[0] == 0


def test_historical_versioned_lifecycle_is_visible(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "historical.db", target="0043_etf_profile_field_evidence")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO position_entries(user_id,ticker,source,created_at,updated_at) VALUES ('bhanu','TEST','manual','2026-01-01','2026-01-01')"
        )
        assert conn.execute("SELECT COUNT(*) FROM position_entries").fetchone()[0] == 1
    entries = list_entries(db_path=path, ticker="TEST")
    assert len(entries) == 1
    entry = entries[0]
    assert entry.superseded_by_entry_id is None
    assert get_entry(entry.id, db_path=path) == entry
    assert not update_exit_fields(db_path=path, entry_id=entry.id, lessons="must not write")
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT lessons FROM position_entries").fetchone() == (None,)
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0043_etf_profile_field_evidence",
        )
