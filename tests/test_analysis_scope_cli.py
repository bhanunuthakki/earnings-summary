"""Evidence selection is immutable and failed publication leaves no partial state."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from execution import plan_analysis_evidence_scope as cli
from provenance import population_research_snapshots as population
from provenance.analysis_scope import AnalysisScopeRequest, build_analysis_scope
from provenance.immutable_artifact import ImmutableArtifactConflictError
from provenance.research_snapshot import (
    CorpusProjectionBundle,
    ResearchSnapshotAdmission,
    ResearchSnapshotRequest,
    ResearchUniverse,
)
from tests.test_analysis_scope import K, scope_db


def test_selection_cli_is_read_only_and_replays_exact_receipt(
    tmp_path: Path, migrated_db: Callable[..., Path], capsys: pytest.CaptureFixture[str]
) -> None:
    conn, scope = scope_db(tmp_path, migrated_db)
    before = conn.execute("SELECT COUNT(*) FROM expected_documents").fetchone()[0]
    conn.commit()
    conn.close()
    request = tmp_path / "request.json"
    output = tmp_path / "scope.json"
    request.write_text(scope.request.model_dump_json() + "\n", encoding="utf-8")
    args = [
        "--db",
        str(tmp_path / "analysis-scope.db"),
        "--request",
        str(request),
        "--scope-receipt",
        str(output),
    ]
    assert cli.main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "selection_only_not_model_ready"
    assert result["scope_id"] == scope.scope_id
    assert result["outside_scope_count"] == 1
    original = output.read_bytes()
    assert cli.main(args) == 0
    assert output.read_bytes() == original
    with sqlite3.connect(tmp_path / "analysis-scope.db") as check:
        assert check.execute("SELECT COUNT(*) FROM expected_documents").fetchone()[0] == before
        assert check.execute("SELECT COUNT(*) FROM research_snapshot_headers").fetchone()[0] == 0


def test_selection_cli_refuses_changed_request_before_publishing(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, scope = scope_db(tmp_path, migrated_db)
    conn.commit()
    conn.close()
    request = tmp_path / "request.json"
    output = tmp_path / "scope.json"
    request.write_text(scope.request.model_dump_json() + "\n", encoding="utf-8")

    def changed_request(db: sqlite3.Connection, selected: AnalysisScopeRequest):
        built = build_analysis_scope(db, selected)
        request.write_text(selected.model_dump_json() + " \n", encoding="utf-8")
        return built

    monkeypatch.setattr(cli, "build_analysis_scope", changed_request)
    with pytest.raises(ImmutableArtifactConflictError, match="changed after admission"):
        cli.main(
            [
                "--db",
                str(tmp_path / "analysis-scope.db"),
                "--request",
                str(request),
                "--scope-receipt",
                str(output),
            ]
        )
    assert not output.exists()


def test_selection_cli_cannot_replace_request(tmp_path: Path) -> None:
    request = tmp_path / "request.json"
    request.write_text("preserved", encoding="utf-8")
    with pytest.raises(ValueError, match="must not replace"):
        cli.main(
            [
                "--db",
                str(tmp_path / "absent.db"),
                "--request",
                str(request),
                "--scope-receipt",
                str(request),
            ]
        )
    assert request.read_text(encoding="utf-8") == "preserved"


def test_publication_recheck_rolls_back_released_inner_savepoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE research_snapshot_seals(research_snapshot_id TEXT);"
        "CREATE TABLE published_evidence(value TEXT);"
    )
    plan = ResearchSnapshotRequest(
        research_snapshot_id="snapshot",
        idempotency_key="snapshot",
        research_universe=ResearchUniverse(
            issuer_id="issuer",
            reporting_entity_ids=("entity",),
            document_version_ids=("document",),
            source_obligation_revision_ids=("obligation",),
        ),
        processing_snapshot_ids=("processing",),
        corpus_bundles=(
            CorpusProjectionBundle(corpus_manifest_id="manifest", lexical_index_run_id="lexical"),
        ),
        source_fact_publication_ids=(),
        ontology_snapshot_id="ontology",
        canonical_fact_resolution_snapshot_id="resolution",
        canonical_fact_projection_run_id="projection",
        cutoff_at=K,
        recorded_at=K,
    )

    def no_check(*_args: object, **_kwargs: object) -> None:
        return None

    def issuers(*_args: object) -> tuple[str, ...]:
        return ("issuer",)

    def assemble(*_args: object, **_kwargs: object) -> ResearchSnapshotRequest:
        return plan

    def manifest(*_args: object) -> dict[str, object]:
        return {"issuer_id": "issuer"}

    def output_hash(*_args: object, **_kwargs: object) -> str:
        return "a" * 64

    def publish(
        db: sqlite3.Connection, selected: ResearchSnapshotRequest
    ) -> ResearchSnapshotAdmission:
        # The real publisher uses a savepoint. Releasing it must not commit the outer operation.
        db.execute("SAVEPOINT inner_publisher")
        db.execute("INSERT INTO published_evidence VALUES ('candidate')")
        db.execute("RELEASE inner_publisher")
        return ResearchSnapshotAdmission(
            research_snapshot_id=selected.research_snapshot_id,
            member_set_sha256="b" * 64,
            cutoff_at=selected.cutoff_at,
            member_count=1,
            requested_lanes=("test",),
        )

    checks = 0

    def recheck() -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise ValueError("immutable evidence changed before publication")

    for name in ("_require_schema", "_require_unambiguous_terminal"):
        monkeypatch.setattr(population, name, no_check)
    monkeypatch.setattr(population, "_issuer_ids", issuers)
    monkeypatch.setattr(population, "assemble_research_snapshot_request", assemble)
    monkeypatch.setattr(population, "_request_input_manifest", manifest)
    monkeypatch.setattr(population, "_output_commitment", output_hash)
    monkeypatch.setattr(population, "build_research_snapshot", publish)
    try:
        result = population.populate_research_snapshots(
            conn,
            population.ResearchSnapshotPopulationRequest(
                cutoff_at=plan.cutoff_at, operation_recorded_at=plan.recorded_at, apply=True
            ),
            before_publish=recheck,
        )
        assert result.blocked_issuer_count == 1
        assert result.created_snapshot_count == 0
        assert checks == 2
        assert conn.execute("SELECT COUNT(*) FROM published_evidence").fetchone()[0] == 0
    finally:
        conn.close()
