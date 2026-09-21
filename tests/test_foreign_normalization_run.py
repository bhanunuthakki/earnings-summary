"""Offline source publication with real migrated tables and immutable fixture bytes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from execution import normalize_foreign_filings as cli
from sources.foreign_filers import ForeignFilingForm
from sources.foreign_normalization_run import (
    ForeignDocumentInput,
    ForeignNormalizationManifest,
    ForeignPeriodAssertion,
    normalize_foreign_sources,
)

STAMP = datetime(2026, 9, 18, 12, tzinfo=UTC)


def seed_foreign_fixture(
    conn: sqlite3.Connection,
    root: Path,
    *,
    role: str = "portfolio",
    outcome: str = "succeeded",
    bind_subject: bool = True,
) -> ForeignNormalizationManifest:
    """Synthetic retained statement extraction; no issuer/live-source claim."""
    payload = b'{"currency":"USD","period":"2026-Q1","revenue":100}'
    artifact = root / "statement.json"
    artifact.write_bytes(payload)
    sha = hashlib.sha256(payload).hexdigest()
    conn.execute(
        "INSERT INTO tracked_companies (ticker,name,list_type,instrument_type) VALUES ('WIX','Synthetic fixture',?,'equity')",
        (role,),
    )
    conn.execute(
        "INSERT INTO documents (id,ticker,source_type,doc_type,file_path,sha256,fetched_at,fetch_status,raw_bytes_size) VALUES (1,'WIX','fmp','fmp_income_statement',?,? ,?,'ok',?)",
        (str(artifact), sha, STAMP.strftime("%Y-%m-%d %H:%M:%S"), len(payload)),
    )
    conn.execute(
        "INSERT INTO issuer_entities VALUES ('issuer-1','issuer-1','operating_company',?)",
        (STAMP.strftime("%Y-%m-%d %H:%M:%S"),),
    )
    conn.execute(
        "INSERT INTO reporting_entities VALUES ('reporting-1','reporting-1','issuer-1','legal_registrant','Synthetic fixture',?)",
        (STAMP.strftime("%Y-%m-%d %H:%M:%S"),),
    )
    conn.execute(
        "INSERT INTO evidence_content_blobs VALUES (?,?,?,?,?)",
        (
            sha,
            len(payload),
            "application/json",
            artifact.as_uri(),
            STAMP.strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    conn.execute(
        "INSERT INTO evidence_source_observations (observation_id,idempotency_key,source_kind,source_url,blob_sha256,observed_at,retrieved_at,retrieval_config_sha256,collector_code_version) VALUES ('source-1','source-1','fmp','https://example.invalid/fixture',?,?,?,?, 'test')",
        (sha, STAMP.strftime("%Y-%m-%d %H:%M:%S"), STAMP.strftime("%Y-%m-%d %H:%M:%S"), "1" * 64),
    )
    conn.execute(
        "INSERT INTO evidence_document_versions (document_version_id,document_key,version_sequence,observation_id,blob_sha256,issuer_id,ticker,document_type,form_type,language,legacy_document_id,recorded_at) VALUES ('document-1','document-1',1,'source-1',?,'issuer-1','WIX','fmp_income_statement','statement','en',1,?)",
        (sha, STAMP.strftime("%Y-%m-%d %H:%M:%S")),
    )
    if bind_subject:
        conn.execute(
            "INSERT INTO recorded_subject_binding_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "binding-1",
                "binding-1",
                "issuer-1",
                1,
                "issuer-1",
                "reporting-1",
                None,
                "selected",
                "deterministic",
                "fixture",
                "{}",
                0,
                STAMP.strftime("%Y-%m-%d %H:%M:%S"),
                STAMP.strftime("%Y-%m-%d %H:%M:%S"),
                STAMP.strftime("%Y-%m-%d %H:%M:%S"),
                None,
            ),
        )
    conn.execute(
        "INSERT INTO evidence_extraction_runs VALUES ('run-1','run-1','document-1',?,'fixture-parser',?,'fixture-v1',?,?,?,?)",
        (
            sha,
            "2" * 64,
            "3" * 64,
            STAMP.strftime("%Y-%m-%d %H:%M:%S"),
            STAMP.strftime("%Y-%m-%d %H:%M:%S"),
            outcome,
        ),
    )
    if outcome == "succeeded":
        locator = '{"path":"revenue"}'
        conn.execute(
            "INSERT INTO evidence_nodes VALUES ('node-1','node-1',1,'run-1',NULL,NULL,'table_cell','100',?,?,?)",
            (
                locator,
                hashlib.sha256(locator.encode()).hexdigest(),
                STAMP.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        conn.execute(
            "INSERT INTO reported_observations (observation_id,idempotency_key,issuer_id,ticker,concept_key,period_start,period_end,fiscal_period_type,dimensions_json,numeric_value,currency,unit,observation_status,evidence_node_id,available_at,recorded_at,method,method_version,confidence) VALUES ('observation-1','observation-1','issuer-1','WIX','revenue','2026-01-01','2026-03-31','quarter','[]','100','USD','USD','reported','node-1',?,?,'fixture-parser','1',1)",
            (STAMP.strftime("%Y-%m-%d %H:%M:%S"), STAMP.strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.execute(
            "INSERT INTO fact_observation_revisions VALUES ('financial_facts',1,1,'observation-1','financial-1',1,'primary',?,?)",
            (locator, STAMP.strftime("%Y-%m-%d %H:%M:%S")),
        )
    conn.commit()
    return ForeignNormalizationManifest(
        data_cutoff_at=STAMP,
        recorded_at=STAMP,
        documents=(
            ForeignDocumentInput(
                ticker="WIX",
                issuer_id="issuer-1",
                document_version_id="document-1",
                document_sha256=sha,
                form=ForeignFilingForm.ISSUER_STATEMENT_CACHE,
                currencies=("USD",),
                units=("USD",),
                periods=(
                    ForeignPeriodAssertion(
                        start=date(2026, 1, 1), end=date(2026, 3, 31), fiscal_period="quarter"
                    ),
                ),
            ),
        ),
    )


@pytest.mark.parametrize("role", ["portfolio", "watchlist", "evaluation"])
def test_real_source_publication_replay_and_no_false_canonical_success(
    tmp_path: Path, migrated_db: Callable[..., Path], role: str
) -> None:
    database = migrated_db(tmp_path / "fixture.db")
    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        manifest = seed_foreign_fixture(conn, tmp_path, role=role)
        planned = normalize_foreign_sources(conn, manifest, input_manifest_sha256="4" * 64)
        assert planned.status == "DRY_RUN"
        assert conn.execute("SELECT count(*) FROM fact_observations_v2").fetchone()[0] == 0
        result = normalize_foreign_sources(
            conn, manifest, input_manifest_sha256="4" * 64, apply=True
        )
        assert result.status == "PARTIAL"
        assert result.receipts[0].status == "normalized"
        assert result.receipts[0].observations == 1
        assert len(result.receipts[0].publications) == 1
        assert not result.decision_grade
        before = conn.total_changes
        repeated = normalize_foreign_sources(
            conn, manifest, input_manifest_sha256="4" * 64, apply=True
        )
        assert repeated == result
        assert conn.total_changes == before
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("currency", "EUR", "source_currency_mismatch"),
        ("unit", "unknown", "source_unit_mismatch"),
        ("period_end", "2026-06-30", "source_period_mismatch"),
    ],
)
def test_semantic_rejection_before_write(
    tmp_path: Path, migrated_db: Callable[..., Path], field: str, value: str, reason: str
) -> None:
    with sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn:
        manifest = seed_foreign_fixture(conn, tmp_path)
        item = manifest.documents[0]
        if field == "currency":
            item = item.model_copy(update={"currencies": (value,)})
        elif field == "unit":
            item = item.model_copy(update={"units": (value,)})
        else:
            period = item.periods[0].model_copy(update={"end": date.fromisoformat(value)})
            item = item.model_copy(update={"periods": (period,)})
        manifest = manifest.model_copy(update={"documents": (item,)})
        result = normalize_foreign_sources(
            conn, manifest, input_manifest_sha256="4" * 64, apply=True
        )
        assert result.status == "HOLD"
        assert result.receipts[0].findings == (reason,)
        assert conn.execute("SELECT count(*) FROM fact_observations_v2").fetchone()[0] == 0


def test_changed_bytes_and_incomplete_extraction_are_rejected(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    with sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn:
        manifest = seed_foreign_fixture(conn, tmp_path, outcome="failed")
        result = normalize_foreign_sources(
            conn, manifest, input_manifest_sha256="4" * 64, apply=True
        )
        assert result.receipts[0].findings == ("incomplete_extraction_run",)
        (tmp_path / "statement.json").write_bytes(b"changed")
        result = normalize_foreign_sources(
            conn, manifest, input_manifest_sha256="4" * 64, apply=True
        )
        assert result.receipts[0].findings == ("captured_source_bytes_mismatch",)


def test_cli_real_plan_and_apply_receipt(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    database = migrated_db(tmp_path / "fixture.db")
    with sqlite3.connect(database) as conn:
        manifest = seed_foreign_fixture(conn, tmp_path)
    path = tmp_path / "inputs.json"
    path.write_text(manifest.model_dump_json())
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(tmp_path / "unused-live-target.db"))
    output = tmp_path / "plan.json"
    args = ["--db", str(database), "--input-manifest", str(path), "--output-receipt", str(output)]
    assert cli.main(args) == 0
    assert json.loads(output.read_text())["status"] == "DRY_RUN"
    output = tmp_path / "apply.json"
    assert cli.main([*args[:-1], str(output), "--apply"]) == 1
    result = json.loads(output.read_text())
    assert result["status"] == "PARTIAL"
    assert result["receipts"][0]["observations"] == 1
    assert result["receipts"][0]["status"] == "normalized"


def test_source_identity_and_transaction_boundary_fail_closed(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    with sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn:
        manifest = seed_foreign_fixture(conn, tmp_path)
        wrong = manifest.model_copy(
            update={
                "documents": (
                    manifest.documents[0].model_copy(update={"issuer_id": "wrong-issuer"}),
                )
            }
        )
        result = normalize_foreign_sources(conn, wrong, input_manifest_sha256="4" * 64, apply=True)
        assert result.receipts[0].findings == ("captured_source_identity_mismatch",)
        conn.execute(
            "INSERT INTO tracked_companies (ticker,name,list_type,instrument_type) VALUES ('OTHER','uncommitted','watchlist','equity')"
        )
        with pytest.raises(ValueError, match="transaction_boundary"):
            normalize_foreign_sources(conn, manifest, input_manifest_sha256="4" * 64, apply=True)
        assert conn.in_transaction
        conn.rollback()


@pytest.mark.parametrize(
    "form",
    [
        ForeignFilingForm.FORM_20F,
        ForeignFilingForm.FORM_20FA,
        ForeignFilingForm.FORM_40F,
        ForeignFilingForm.FORM_40FA,
        ForeignFilingForm.FORM_6K,
        ForeignFilingForm.FORM_6KA,
    ],
)
def test_native_and_amended_forms_cannot_relabel_statement_fixture(
    tmp_path: Path, migrated_db: Callable[..., Path], form: ForeignFilingForm
) -> None:
    from sources.foreign_normalization_run import ForeignProcessorRuntime

    with sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn:
        manifest = seed_foreign_fixture(conn, tmp_path)
        item = manifest.documents[0].model_copy(
            update={
                "form": form,
                "inventory_key": "fixture-inventory",
                "accession_number": "0000000001-26-000001",
                "expected_cik": "0000000001",
            }
        )
        runtime = ForeignProcessorRuntime(
            bundle_manifest=tmp_path / "absent.json",
            runtime_root=tmp_path,
            bundle_python=tmp_path / "python",
            sandbox_launcher=tmp_path / "launcher",
        )
        manifest = manifest.model_copy(update={"documents": (item,), "processor": runtime})
        result = normalize_foreign_sources(
            conn, manifest, input_manifest_sha256="4" * 64, apply=True
        )
        assert result.receipts[0].findings == ("native_form_or_accession_mismatch",)
        assert conn.execute("SELECT count(*) FROM fact_observations_v2").fetchone()[0] == 0


def test_publisher_failure_rolls_back_run_and_retains_checkpoint(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from typing import Never

    from provenance.source_fact_repository import SourceFactPublication, SourceFactRepository

    publish = SourceFactRepository.publish

    def fail_after_write(self: SourceFactRepository, publication: SourceFactPublication) -> Never:
        publish(self, publication)
        raise RuntimeError("injected failure before owner transaction commits")

    monkeypatch.setattr(SourceFactRepository, "publish", fail_after_write)
    with sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn:
        manifest = seed_foreign_fixture(conn, tmp_path)
        result = normalize_foreign_sources(
            conn, manifest, input_manifest_sha256="4" * 64, apply=True
        )
        assert result.status == "HOLD"
        assert result.publication_checkpoint is not None
        assert result.publication_checkpoint["failed_extraction_run_id"] == "run-1"
        assert result.receipts[0].status == "failed"
        assert not conn.in_transaction
        assert conn.execute("SELECT count(*) FROM fact_observations_v2").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM fact_cells_v2").fetchone()[0] == 0


def test_missing_subject_is_closed_from_canonical_authority_before_source_publish(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    with sqlite3.connect(migrated_db(tmp_path / "identity-stage.db")) as conn:
        manifest = seed_foreign_fixture(conn, tmp_path, bind_subject=False)
        before = list(conn.iterdump())
        dry_run = normalize_foreign_sources(conn, manifest, input_manifest_sha256="4" * 64)
        assert list(conn.iterdump()) == before
        assert dry_run.subject_identity is not None
        assert dry_run.subject_identity.selected_count == 1
        assert dry_run.subject_identity.created_count == 0
        receipt = normalize_foreign_sources(
            conn, manifest, input_manifest_sha256="4" * 64, apply=True
        )
        assert receipt.subject_identity is not None
        assert receipt.subject_identity.created_count == 1
        assert receipt.receipts[0].observations > 0
        assert receipt.status == "PARTIAL"
        assert not receipt.decision_grade
        replay = normalize_foreign_sources(
            conn, manifest, input_manifest_sha256="4" * 64, apply=True
        )
        assert replay.subject_identity is not None
        assert replay.subject_identity.created_count == 0
        assert replay.receipts[0].publications == receipt.receipts[0].publications


def test_partial_extraction_publishes_only_admitted_source_rows(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    with sqlite3.connect(migrated_db(tmp_path / "partial.db")) as conn:
        manifest = seed_foreign_fixture(conn, tmp_path, bind_subject=False)
        conn.execute(
            "INSERT INTO reported_observations "
            "(observation_id,idempotency_key,issuer_id,ticker,concept_key,period_start,period_end,"
            "fiscal_period_type,dimensions_json,numeric_value,currency,unit,observation_status,"
            "evidence_node_id,available_at,recorded_at,method,method_version,confidence) "
            "SELECT 'derived-1','derived-1',issuer_id,ticker,'unsupported_formula',period_start,"
            "period_end,fiscal_period_type,dimensions_json,'200',currency,unit,'derived',"
            "evidence_node_id,available_at,recorded_at,method,method_version,confidence "
            "FROM reported_observations WHERE observation_id='observation-1'"
        )
        conn.execute(
            "INSERT INTO fact_observation_revisions "
            "SELECT fact_table,2,fact_revision,'derived-1','derived-financial-1',"
            "source_document_id,source_tier,locator_json,captured_at FROM fact_observation_revisions"
        )
        conn.commit()
        result = normalize_foreign_sources(
            conn, manifest, input_manifest_sha256="4" * 64, apply=True
        )
        assert result.status == "PARTIAL"
        assert result.subject_identity is not None
        assert result.subject_identity.created_count == 1
        assert result.source_population_plan is not None
        assert result.source_population_plan.expected_count == 2
        assert result.source_population_plan.eligible_count == 1
        assert (
            result.source_population_plan.exclusion_counts["derived_without_formula_lineage"] == 1
        )
        assert "source_observations_excluded" in result.reason_codes
        assert result.receipts[0].status == "partial"
        assert result.receipts[0].observations == 1
        assert not result.decision_grade
        assert conn.execute("SELECT count(*) FROM fact_observations_v2").fetchone()[0] == 1
        replay = normalize_foreign_sources(
            conn, manifest, input_manifest_sha256="4" * 64, apply=True
        )
        assert replay.receipts == result.receipts
        assert replay.source_population_plan == result.source_population_plan
