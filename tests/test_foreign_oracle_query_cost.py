"""Missing semantic inputs cannot trigger per-observation publication verification."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

from sources.foreign_oracle_run import compare_foreign_sources
from tests.test_foreign_oracle_run import seed_source_oracle


def test_missing_ontology_retains_candidate_census_without_loading_fact_values(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    with sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn:
        manifest = seed_source_oracle(conn, tmp_path)
        # Initialize canonical SQLite UDF/schema probes before measuring reads.
        compare_foreign_sources(conn, manifest, repo_root=tmp_path)
        queries: list[str] = []
        conn.set_trace_callback(queries.append)
        small = compare_foreign_sources(conn, manifest, repo_root=tmp_path)
        small_query_count = len(queries)
        assert not any("canonical_payload_json" in query for query in queries)
        conn.set_trace_callback(None)
        # Deliberately unpublished candidates: their identities may be counted,
        # but their unadmitted values must never appear in a diagnostic receipt.
        columns = [str(row[1]) for row in conn.execute("PRAGMA table_info(fact_observations_v2)")]
        selected = [
            "?" if name in {"observation_id", "idempotency_key"} else name for name in columns
        ]
        sql = (
            f"INSERT INTO fact_observations_v2 ({','.join(columns)}) "
            f"SELECT {','.join(selected)} FROM fact_observations_v2 LIMIT 1"
        )
        conn.executemany(sql, [(f"unpublished-{i}", f"unpublished-{i}") for i in range(200)])
        queries.clear()
        before = conn.total_changes
        conn.set_trace_callback(queries.append)
        large = json.loads(json.dumps(compare_foreign_sources(conn, manifest, repo_root=tmp_path)))
        conn.set_trace_callback(None)
        assert len(queries) == small_query_count
        assert not any("canonical_payload_json" in query for query in queries)
        assert conn.total_changes == before
        assert small["total_exact_matches"] == large["total_exact_matches"] == 0
        assert large["status"] == "PARTIAL" and large["decision_grade"] is False
        assert "ontology_snapshot_unavailable" in large["reason_codes"]
        receipts = large["receipts"]
        assert receipts[0]["observations"] == 201
        assert receipts[0]["observation_admission"] == "not_evaluated_missing_ontology"
        comparisons = receipts[0]["comparisons"]
        assert len(comparisons) == 201
        assert all(item["reason"] == "canonical_metric_binding_unavailable" for item in comparisons)
        assert all(item["oracle"] is None for item in comparisons)
        assert all(
            set(item["source"]) == {"observation_id", "document_version_id"} for item in comparisons
        )
