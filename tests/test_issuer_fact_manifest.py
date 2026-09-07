"""Atomic issuer manifest application regressions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from models.facts import (
    Currency,
    FactLocator,
    FiscalPeriodType,
    LocatorKind,
    SegmentDimType,
    Unit,
)
from pipeline.issuer_document_coverage import persist_document_coverage_receipt
from pipeline.issuer_fact_manifest import (
    MAX_EXTRACTED_AT_FUTURE_SKEW,
    IssuerFactManifest,
    IssuerFactManifestV2,
    IssuerFactValue,
    IssuerManifestFactKind,
    ReviewedKpiDefinitionCapture,
    apply_issuer_fact_manifest,
)
from pipeline.kpi_definition_revisions import (
    IssuerKpiDefinitionRevision,
    KpiCurrencyDisposition,
    KpiDefinitionComparabilityDisposition,
    KpiDefinitionComparabilityRevision,
    KpiDefinitionLifecycle,
    KpiDefinitionPeriodKind,
    KpiDefinitionRelationKind,
    KpiDefinitionStatus,
    KpiDefinitionTextStatus,
    KpiStockFlowBehavior,
    KpiUnitFamily,
)
from pipeline.kpi_semantics import (
    KpiAccountingBasis,
    KpiConsolidationScope,
    KpiPeriodRole,
    KpiPublicationLane,
    KpiSemanticContext,
    KpiSemanticStatus,
    KpiUnitScale,
)
from provenance.evidence_ledger import EvidenceLocator
from sqlite_runtime import register_sqlite_integrity_functions


def _noop_resolve(*_args: object, **_kwargs: object) -> None:
    return None


def _no_segment_write(*_args: object, **_kwargs: object) -> tuple[int, int]:
    return (0, 0)


def _document(
    conn: sqlite3.Connection,
    *,
    doc_id: int = 9001,
    sha: str = "a" * 64,
    period_end: str = "2026-06-30",
    fetched_at: str = "2026-08-05T00:00:00Z",
) -> None:
    conn.execute(
        "INSERT INTO documents "
        "(id,ticker,source_type,doc_type,period_end,file_path,sha256,fetched_at,"
        "fetch_status,raw_bytes_size) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            doc_id,
            "MELI",
            "ir_doc",
            "ir_presentation",
            period_end,
            "fixture.pdf",
            sha,
            fetched_at,
            "fetched",
            10,
        ),
    )
    # These hermetic rows do not build the full evidence ledger.  Production
    # issuer documents are admitted through the governed evidence writer before
    # this manifest boundary is reached.
    conn.execute("DROP TRIGGER IF EXISTS trg_kpi_facts_observation_insert")
    conn.execute("DROP TRIGGER IF EXISTS trg_kpi_facts_observation_update")


def _manifest(*, sha: str = "a" * 64) -> IssuerFactManifest:
    kpi = IssuerFactValue(
        ticker="MELI",
        kind=IssuerManifestFactKind.KPI,
        canonical_name="Total Payment Volume",
        period_end=date(2026, 6, 30),
        fiscal_period_type=FiscalPeriodType.Q2,
        unit=Unit.MILLIONS,
        currency=Currency.USD,
        value=Decimal("1000"),
        locator=FactLocator(
            locator_version=2,
            pdf_page=3,
            kind=LocatorKind.PDF_SLIDE,
            verbatim_snippet="TPV 1,000",
        ),
    )
    segment = IssuerFactValue(
        ticker="MELI",
        kind=IssuerManifestFactKind.SEGMENT,
        canonical_name="Commerce",
        period_end=date(2026, 6, 30),
        fiscal_period_type=FiscalPeriodType.Q2,
        unit=Unit.MILLIONS,
        currency=Currency.USD,
        value=Decimal("500"),
        locator=FactLocator(
            locator_version=2,
            pdf_page=5,
            kind=LocatorKind.PDF_SLIDE,
            verbatim_snippet="Commerce 500",
        ),
        segment_dim_type=SegmentDimType.BUSINESS_UNIT,
        segment_name="Commerce",
        metric="revenue",
    )
    return IssuerFactManifest(
        ticker="MELI",
        source_doc_id=9001,
        source_doc_sha256=sha,
        period_end=date(2026, 6, 30),
        fiscal_period_type=FiscalPeriodType.Q2,
        values=(kpi, segment),
        expected=(kpi.expected(), segment.expected()),
        extracted_at=datetime(2026, 8, 5, tzinfo=UTC),
    )


def test_manifest_requires_expected_population_and_source_binding() -> None:
    with pytest.raises(ValueError, match="populated manifest requires expected facts"):
        IssuerFactManifest(
            ticker="MELI",
            source_doc_id=1,
            source_doc_sha256="a" * 64,
            period_end=date(2026, 6, 30),
            fiscal_period_type=FiscalPeriodType.Q2,
            values=(),
            expected=(),
            extracted_at=datetime(2026, 8, 5, tzinfo=UTC),
        )


def test_dry_run_does_not_write(migrated_db: Callable[..., Path], tmp_path: Path) -> None:
    db_path = migrated_db(tmp_path / "dry-run.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    register_sqlite_integrity_functions(conn)
    try:
        _document(conn)
        conn.commit()
        result = apply_issuer_fact_manifest(conn, _manifest())
        assert result.applied is False
        assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM segment_periods").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM issuer_fact_coverage_receipts").fetchone()[0] == 0
    finally:
        conn.close()


def test_apply_is_atomic_and_receipt_replays(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = migrated_db(tmp_path / "apply.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _document(conn)
        conn.commit()
        import pipeline.restatement_detector as restatement_detector

        monkeypatch.setattr(restatement_detector, "resolve_fact_row", _noop_resolve)
        first = apply_issuer_fact_manifest(conn, _manifest(), apply=True)
        assert first.kpi_inserted == 1
        assert first.segment_dimensions_inserted == 1
        assert first.coverage_receipts_created == 2
        second = apply_issuer_fact_manifest(conn, _manifest(), apply=True)
        assert second.kpi_inserted == 0
        assert second.kpi_skipped_existing == 1
        assert second.segment_dimensions_inserted == 0
        assert second.coverage_receipts_created == 0
        assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM segment_dimensions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM issuer_fact_coverage_receipts").fetchone()[0] == 2
    finally:
        conn.close()


def test_receipt_hash_binds_canonical_application_manifest_across_clean_databases(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pipeline.restatement_detector as restatement_detector

    monkeypatch.setattr(restatement_detector, "resolve_fact_row", _noop_resolve)
    baseline = _manifest()
    changed_kpi = baseline.values[0].model_copy(
        update={
            "value": Decimal("1001"),
            "locator": FactLocator(
                locator_version=2,
                pdf_page=4,
                kind=LocatorKind.PDF_SLIDE,
                verbatim_snippet="TPV 1,001",
            ),
            "source_excerpt": "Total payment volume was 1,001 million",
        }
    )
    changed = baseline.model_copy(update={"values": (changed_kpi, baseline.values[1])})
    receipt_hashes: list[tuple[str, ...]] = []

    for name, manifest in (("baseline", baseline), ("changed", changed)):
        db_path = migrated_db(tmp_path / f"manifest-receipt-{name}.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            _document(conn)
            conn.commit()
            result = apply_issuer_fact_manifest(conn, manifest, apply=True)
            assert result.receipt is not None
            assert result.receipt.application_manifest_json == manifest.canonical_json
            assert result.receipt.application_manifest_sha256 == manifest.manifest_sha256
            receipt_hashes.append(
                tuple(
                    str(row[0])
                    for row in conn.execute(
                        "SELECT receipt_sha256 FROM issuer_fact_coverage_receipts "
                        "ORDER BY fact_identity"
                    ).fetchall()
                )
            )
        finally:
            conn.close()

    assert receipt_hashes[0] != receipt_hashes[1]


def test_persistence_rejects_tampered_application_manifest_evidence(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = migrated_db(tmp_path / "manifest-receipt-tamper.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _document(conn)
        conn.commit()
        import pipeline.restatement_detector as restatement_detector

        monkeypatch.setattr(restatement_detector, "resolve_fact_row", _noop_resolve)
        result = apply_issuer_fact_manifest(conn, _manifest(), apply=True)
        assert result.receipt is not None
        payload = json.loads(result.receipt.application_manifest_json or "{}")
        payload["source_doc_sha256"] = "b" * 64
        tampered_json = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        unpaired = result.receipt.model_copy(update={"application_manifest_sha256": None})
        with pytest.raises(ValueError, match="must be supplied together"):
            persist_document_coverage_receipt(conn, unpaired)
        hash_mismatch = result.receipt.model_copy(
            update={"application_manifest_json": tampered_json}
        )
        with pytest.raises(ValueError, match="manifest hash does not match"):
            persist_document_coverage_receipt(conn, hash_mismatch)
        tampered = result.receipt.model_copy(
            update={
                "application_manifest_json": tampered_json,
                "application_manifest_sha256": hashlib.sha256(
                    tampered_json.encode("utf-8")
                ).hexdigest(),
            }
        )
        with pytest.raises(ValueError, match="source SHA-256 must match"):
            persist_document_coverage_receipt(conn, tampered)
        assert conn.execute("SELECT COUNT(*) FROM issuer_fact_coverage_receipts").fetchone()[0] == 2
    finally:
        conn.close()


def test_public_receipt_persistence_rejects_rehashed_value_and_locator_forgery(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = migrated_db(tmp_path / "manifest-receipt-semantic-forgery.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _document(conn)
        conn.commit()
        import pipeline.restatement_detector as restatement_detector

        monkeypatch.setattr(restatement_detector, "resolve_fact_row", _noop_resolve)
        baseline = _manifest()
        result = apply_issuer_fact_manifest(conn, baseline, apply=True)
        assert result.receipt is not None
        forged_kpi = baseline.values[0].model_copy(
            update={
                "value": Decimal("9999"),
                "locator": FactLocator(
                    locator_version=2,
                    pdf_page=99,
                    kind=LocatorKind.PDF_SLIDE,
                    verbatim_snippet="TPV 1,000",
                ),
            }
        )
        forged_manifest = baseline.model_copy(update={"values": (forged_kpi, baseline.values[1])})
        forged_receipt = result.receipt.model_copy(
            update={
                "application_manifest_json": forged_manifest.canonical_json,
                "application_manifest_sha256": forged_manifest.manifest_sha256,
            }
        )

        with pytest.raises(ValueError, match=r"same-document KPI.*value or provenance"):
            persist_document_coverage_receipt(conn, forged_receipt)
        assert conn.execute("SELECT value FROM kpi_facts").fetchone()[0] == 1000
        assert conn.execute("SELECT COUNT(*) FROM issuer_fact_coverage_receipts").fetchone()[0] == 2
    finally:
        conn.close()


def test_receipt_rejects_incomplete_or_incongruent_typed_manifest(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = migrated_db(tmp_path / "manifest-receipt-population-tamper.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _document(conn)
        conn.commit()
        import pipeline.restatement_detector as restatement_detector

        monkeypatch.setattr(restatement_detector, "resolve_fact_row", _noop_resolve)
        baseline = _manifest()
        result = apply_issuer_fact_manifest(conn, baseline, apply=True)
        assert result.receipt is not None

        incomplete_json = json.dumps(
            {
                "schema_version": "issuer_fact_manifest.v1",
                "source_doc_id": baseline.source_doc_id,
                "source_doc_sha256": baseline.source_doc_sha256,
                "ticker": baseline.ticker,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        incomplete = result.receipt.model_copy(
            update={
                "application_manifest_json": incomplete_json,
                "application_manifest_sha256": hashlib.sha256(
                    incomplete_json.encode("utf-8")
                ).hexdigest(),
            }
        )
        with pytest.raises(ValueError, match="typed issuer manifest schema"):
            persist_document_coverage_receipt(conn, incomplete)

        reduced_manifest = baseline.model_copy(
            update={
                "values": (baseline.values[0],),
                "expected": (baseline.expected[0],),
            }
        )
        reduced = result.receipt.model_copy(
            update={
                "application_manifest_json": reduced_manifest.canonical_json,
                "application_manifest_sha256": reduced_manifest.manifest_sha256,
            }
        )
        with pytest.raises(ValueError, match="expected identities must exactly match"):
            persist_document_coverage_receipt(conn, reduced)

        segment_identity = baseline.expected[1].identity_key
        incongruent_manifest = baseline.model_copy(
            update={
                "values": (baseline.values[0],),
                "rejected": {segment_identity: "not usable from this source"},
            }
        )
        incongruent = result.receipt.model_copy(
            update={
                "application_manifest_json": incongruent_manifest.canonical_json,
                "application_manifest_sha256": incongruent_manifest.manifest_sha256,
            }
        )
        with pytest.raises(ValueError, match="value identities must exactly match"):
            persist_document_coverage_receipt(conn, incongruent)
        assert conn.execute("SELECT COUNT(*) FROM issuer_fact_coverage_receipts").fetchone()[0] == 2
    finally:
        conn.close()


def test_receipt_rejection_reason_must_match_embedded_manifest(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = migrated_db(tmp_path / "manifest-receipt-rejection-tamper.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _document(conn)
        conn.commit()
        import pipeline.restatement_detector as restatement_detector

        monkeypatch.setattr(restatement_detector, "resolve_fact_row", _noop_resolve)
        baseline = _manifest()
        segment_identity = baseline.expected[1].identity_key
        rejected_manifest = baseline.model_copy(
            update={
                "values": (baseline.values[0],),
                "rejected": {segment_identity: "not usable from this source"},
            }
        )
        result = apply_issuer_fact_manifest(conn, rejected_manifest, apply=True)
        assert result.receipt is not None
        changed_reason_manifest = rejected_manifest.model_copy(
            update={"rejected": {segment_identity: "different rejection reason"}}
        )
        tampered = result.receipt.model_copy(
            update={
                "application_manifest_json": changed_reason_manifest.canonical_json,
                "application_manifest_sha256": changed_reason_manifest.manifest_sha256,
            }
        )
        with pytest.raises(ValueError, match="rejection map must exactly match"):
            persist_document_coverage_receipt(conn, tampered)
        assert conn.execute("SELECT COUNT(*) FROM issuer_fact_coverage_receipts").fetchone()[0] == 2
    finally:
        conn.close()


def test_apply_rolls_back_kpi_when_segment_population_is_missing(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = migrated_db(tmp_path / "rollback.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _document(conn)
        conn.commit()
        import pipeline.issuer_fact_manifest as issuer_manifest_module
        import pipeline.restatement_detector as restatement_detector

        monkeypatch.setattr(restatement_detector, "resolve_fact_row", _noop_resolve)
        monkeypatch.setattr(
            issuer_manifest_module, "write_segment_facts_junction", _no_segment_write
        )
        with pytest.raises(ValueError, match=r"expected fact.*missing"):
            apply_issuer_fact_manifest(conn, _manifest(), apply=True)
        assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM segment_periods").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM issuer_fact_coverage_receipts").fetchone()[0] == 0
    finally:
        conn.close()


def test_source_period_and_extraction_chronology_are_bound(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    db_path = migrated_db(tmp_path / "chronology.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _document(conn, period_end="2026-03-31")
        conn.commit()
        with pytest.raises(ValueError, match="period does not match"):
            apply_issuer_fact_manifest(conn, _manifest())
        conn.execute("UPDATE documents SET period_end='2026-06-30' WHERE id=9001")
        conn.commit()
        with pytest.raises(ValueError, match="cannot predate"):
            apply_issuer_fact_manifest(
                conn,
                _manifest().model_copy(update={"extracted_at": datetime(2020, 1, 1, tzinfo=UTC)}),
            )
        with pytest.raises(ValueError, match="timezone-aware UTC"):
            apply_issuer_fact_manifest(
                conn,
                _manifest().model_copy(
                    update={
                        "extracted_at": datetime(2026, 8, 5, tzinfo=timezone(timedelta(hours=1)))
                    }
                ),
            )
        with pytest.raises(ValueError, match="future clock skew"):
            apply_issuer_fact_manifest(
                conn,
                _manifest().model_copy(
                    update={
                        "extracted_at": datetime.now(UTC)
                        + MAX_EXTRACTED_AT_FUTURE_SKEW
                        + timedelta(minutes=1)
                    }
                ),
            )
    finally:
        conn.close()


def test_dry_run_revalidates_segment_dimension_type(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    db_path = migrated_db(tmp_path / "segment-type.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _document(conn)
        conn.commit()
        invalid_segment = (
            _manifest().values[1].model_copy(update={"segment_dim_type": "unsupported_axis"})
        )
        invalid_manifest = _manifest().model_copy(
            update={"values": (_manifest().values[0], invalid_segment)}
        )
        with pytest.raises(ValueError, match="segment_dim_type"):
            apply_issuer_fact_manifest(conn, invalid_manifest)
        assert conn.execute("SELECT COUNT(*) FROM segment_periods").fetchone()[0] == 0
    finally:
        conn.close()


def test_apply_failure_preserves_callers_existing_transaction(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = migrated_db(tmp_path / "caller-transaction.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _document(conn)
        conn.commit()
        import pipeline.issuer_fact_manifest as issuer_manifest_module
        import pipeline.restatement_detector as restatement_detector

        monkeypatch.setattr(restatement_detector, "resolve_fact_row", _noop_resolve)
        monkeypatch.setattr(
            issuer_manifest_module, "write_segment_facts_junction", _no_segment_write
        )
        conn.execute("BEGIN")
        conn.execute("CREATE TABLE caller_state (value TEXT NOT NULL)")
        conn.execute("INSERT INTO caller_state VALUES ('preserve-me')")
        with pytest.raises(ValueError, match=r"expected fact.*missing"):
            apply_issuer_fact_manifest(conn, _manifest(), apply=True)
        assert conn.in_transaction
        assert conn.execute("SELECT value FROM caller_state").fetchone()[0] == "preserve-me"
        assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 0
    finally:
        conn.rollback()
        conn.close()


def test_same_document_kpi_value_conflict_rolls_back(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = migrated_db(tmp_path / "kpi-conflict.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _document(conn)
        conn.commit()
        import pipeline.restatement_detector as restatement_detector

        monkeypatch.setattr(restatement_detector, "resolve_fact_row", _noop_resolve)
        apply_issuer_fact_manifest(conn, _manifest(), apply=True)
        conflicting_kpi = _manifest().values[0].model_copy(update={"value": Decimal("999")})
        conflict = _manifest().model_copy(
            update={"values": (conflicting_kpi, _manifest().values[1])}
        )
        with pytest.raises(ValueError, match="same-document KPI"):
            apply_issuer_fact_manifest(conn, conflict, apply=True)
        changed_locator = (
            _manifest()
            .values[0]
            .model_copy(
                update={
                    "locator": FactLocator(
                        locator_version=2,
                        pdf_page=4,
                        kind=LocatorKind.PDF_SLIDE,
                        verbatim_snippet="TPV 1,000",
                    )
                }
            )
        )
        with pytest.raises(ValueError, match="value or provenance"):
            apply_issuer_fact_manifest(
                conn,
                _manifest().model_copy(update={"values": (changed_locator, _manifest().values[1])}),
                apply=True,
            )
        changed_excerpt = (
            _manifest()
            .values[0]
            .model_copy(update={"source_excerpt": "different supporting quote"})
        )
        with pytest.raises(ValueError, match="value or provenance"):
            apply_issuer_fact_manifest(
                conn,
                _manifest().model_copy(update={"values": (changed_excerpt, _manifest().values[1])}),
                apply=True,
            )
        assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 1
        assert conn.execute("SELECT value FROM kpi_facts").fetchone()[0] == 1000
        assert conn.execute("SELECT COUNT(*) FROM issuer_fact_coverage_receipts").fetchone()[0] == 2
    finally:
        conn.close()


def test_same_document_segment_value_conflict_cannot_create_duplicate_capture(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = migrated_db(tmp_path / "segment-conflict.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _document(conn)
        conn.commit()
        import pipeline.restatement_detector as restatement_detector

        monkeypatch.setattr(restatement_detector, "resolve_fact_row", _noop_resolve)
        apply_issuer_fact_manifest(conn, _manifest(), apply=True)
        conflicting_segment = _manifest().values[1].model_copy(update={"value": Decimal("499")})
        conflict = _manifest().model_copy(
            update={"values": (_manifest().values[0], conflicting_segment)}
        )
        with pytest.raises(ValueError, match="same-document segment"):
            apply_issuer_fact_manifest(conn, conflict, apply=True)
        changed_locator = (
            _manifest()
            .values[1]
            .model_copy(
                update={
                    "locator": FactLocator(
                        locator_version=2,
                        pdf_page=6,
                        kind=LocatorKind.PDF_SLIDE,
                        verbatim_snippet="Commerce 500",
                    )
                }
            )
        )
        with pytest.raises(ValueError, match="value or provenance"):
            apply_issuer_fact_manifest(
                conn,
                _manifest().model_copy(update={"values": (_manifest().values[0], changed_locator)}),
                apply=True,
            )
        assert conn.execute("SELECT COUNT(*) FROM segment_dimensions").fetchone()[0] == 1
        assert conn.execute("SELECT value FROM segment_dimensions").fetchone()[0] == 500
        assert conn.execute("SELECT COUNT(*) FROM issuer_fact_coverage_receipts").fetchone()[0] == 2
        conn.execute(
            "INSERT INTO segment_dimensions (period_id,dim_type,dim_name,value,metric) "
            "SELECT period_id,dim_type,dim_name,value,metric FROM segment_dimensions LIMIT 1"
        )
        conn.commit()
        with pytest.raises(ValueError, match="duplicate existing captures"):
            apply_issuer_fact_manifest(conn, _manifest(), apply=True)
        assert conn.execute("SELECT COUNT(*) FROM issuer_fact_coverage_receipts").fetchone()[0] == 2
    finally:
        conn.close()


def test_source_sha_tampering_is_rejected(migrated_db: Callable[..., Path], tmp_path: Path) -> None:
    db_path = migrated_db(tmp_path / "tamper.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _document(conn)
        conn.commit()
        with pytest.raises(ValueError, match="SHA-256"):
            apply_issuer_fact_manifest(conn, _manifest(sha="b" * 64), apply=True)
    finally:
        conn.close()


def _seed_v2_authority(conn: sqlite3.Connection) -> EvidenceLocator:
    stamp = "2026-08-05T00:00:00Z"
    locator = EvidenceLocator(slide_number=3)
    conn.execute(
        "INSERT INTO issuer_entities VALUES (?,?,?,?)",
        ("issuer-meli", "issuer:meli", "operating_company", stamp),
    )
    conn.execute(
        "INSERT INTO reporting_entities VALUES (?,?,?,?,?,?)",
        (
            "entity-meli",
            "entity:meli",
            "issuer-meli",
            "legal_registrant",
            "MercadoLibre, Inc.",
            stamp,
        ),
    )
    conn.execute(
        "INSERT INTO evidence_content_blobs VALUES (?,?,?,?,?)",
        ("a" * 64, 1, "application/pdf", "evidence/meli.pdf", stamp),
    )
    conn.execute(
        "INSERT INTO evidence_source_observations "
        "(observation_id,idempotency_key,source_kind,source_url,blob_sha256,observed_at,"
        "retrieved_at,retrieval_config_sha256,collector_code_version) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "observation-meli",
            "observation:meli",
            "ir_document",
            "https://example.invalid/meli.pdf",
            "a" * 64,
            stamp,
            stamp,
            "b" * 64,
            "test-collector/v1",
        ),
    )
    conn.execute(
        "INSERT INTO evidence_document_versions "
        "(document_version_id,document_key,version_sequence,observation_id,blob_sha256,"
        "issuer_id,ticker,document_type,form_type,period_end,language,legacy_document_id,recorded_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "document-meli-v1",
            "document:meli",
            1,
            "observation-meli",
            "a" * 64,
            "issuer-meli",
            "MELI",
            "earnings_release",
            "earnings_release",
            "2026-06-30",
            "en",
            9001,
            stamp,
        ),
    )
    conn.execute(
        "INSERT INTO evidence_extraction_runs "
        "(extraction_run_id,idempotency_key,document_version_id,input_sha256,extractor_name,"
        "extractor_config_sha256,extractor_code_version,output_sha256,started_at,completed_at,outcome) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "run-meli-v1",
            "run:meli:v1",
            "document-meli-v1",
            "a" * 64,
            "test-extractor",
            "c" * 64,
            "test-extractor/v1",
            "d" * 64,
            stamp,
            stamp,
            "succeeded",
        ),
    )
    conn.execute(
        "INSERT INTO evidence_nodes "
        "(node_id,evidence_key,revision,extraction_run_id,node_kind,text,locator_json,"
        "locator_sha256,recorded_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "document-node-meli",
            "document-node:meli",
            1,
            "run-meli-v1",
            "document",
            "MELI Q2 earnings presentation",
            locator.canonical_json,
            locator.canonical_sha256,
            stamp,
        ),
    )
    conn.execute(
        "INSERT INTO evidence_nodes "
        "(node_id,evidence_key,revision,extraction_run_id,parent_node_id,node_kind,text,locator_json,"
        "locator_sha256,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "node-meli-tpv",
            "node:meli:tpv",
            1,
            "run-meli-v1",
            "document-node-meli",
            "pdf_page",
            "Total Payment Volume 1,000",
            locator.canonical_json,
            locator.canonical_sha256,
            stamp,
        ),
    )
    conn.execute(
        "INSERT INTO legacy_document_evidence_binding_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "binding-meli-v1",
            "binding:meli:v1",
            9001,
            1,
            "document-meli-v1",
            "document-node-meli",
            locator.canonical_json,
            locator.canonical_sha256,
            "a" * 64,
            stamp,
            stamp,
            stamp,
            None,
        ),
    )
    conn.execute(
        "INSERT INTO kpi_definitions (id,ticker,name,unit,primary_source) "
        "VALUES (?,?,?,'percent','ir_doc')",
        (6401, "MELI", "Total Payment Volume"),
    )
    return locator


def _v2_manifest(locator: EvidenceLocator) -> IssuerFactManifestV2:
    base = _manifest()
    kpi = base.values[0].model_copy(
        update={
            "source_excerpt": "Total Payment Volume 1,000",
            "locator": FactLocator(
                locator_version=2,
                pdf_page=3,
                kind=LocatorKind.PDF_SLIDE,
                verbatim_snippet="Total Payment Volume 1,000",
            ),
        }
    )
    context = KpiSemanticContext(
        metric_name_as_reported="Total Payment Volume",
        reported_period_end=base.period_end,
        period_role=KpiPeriodRole.CURRENT,
        publication_lane=KpiPublicationLane.CURRENT_ACTUAL,
        accounting_basis=KpiAccountingBasis.MANAGEMENT,
        consolidation_scope=KpiConsolidationScope.CONSOLIDATED,
        dimensions={},
        unit_scale=KpiUnitScale.MILLIONS,
        source_value_text="1,000",
        status=KpiSemanticStatus.ADMITTED,
    )
    definition = IssuerKpiDefinitionRevision(
        kpi_definition_revision_id="definition-meli-tpv-r1",
        idempotency_key="definition:meli:tpv:r1",
        kpi_definition_id=6401,
        reporting_entity_id="entity-meli",
        revision=1,
        status=KpiDefinitionStatus.ADMITTED,
        lifecycle=KpiDefinitionLifecycle.ACTIVE,
        reported_label="Total Payment Volume",
        reported_definition_text="Total payment volume processed on the platform.",
        definition_text_status=KpiDefinitionTextStatus.VERBATIM,
        period_kind=KpiDefinitionPeriodKind.DURATION,
        stock_flow_behavior=KpiStockFlowBehavior.FLOW,
        unit_family=KpiUnitFamily.CURRENCY,
        unit_key=Unit.MILLIONS,
        unit_scale=KpiUnitScale.MILLIONS,
        currency_disposition=KpiCurrencyDisposition.EXPLICIT,
        currency=Currency.USD,
        accounting_basis=KpiAccountingBasis.MANAGEMENT,
        consolidation_scope=KpiConsolidationScope.CONSOLIDATED,
        dimensions={},
        source_document_version_id="document-meli-v1",
        source_evidence_node_id="node-meli-tpv",
        source_locator=locator.model_dump(mode="json", exclude_none=True),
        reviewed_by="owner",
        effective_at=datetime(2026, 6, 30, tzinfo=UTC),
        knowledge_at=datetime(2026, 8, 5, tzinfo=UTC),
        recorded_at=datetime(2026, 8, 5, tzinfo=UTC),
    )
    fact_locator_json = kpi.locator.to_json()
    assert fact_locator_json is not None
    capture = ReviewedKpiDefinitionCapture(
        fact_identity=kpi.expected().identity_key,
        expected_kpi_definition_id=6401,
        expected_definition_head_id=None,
        expected_definition_revision=0,
        evidence_document_version_id="document-meli-v1",
        evidence_node_id="node-meli-tpv",
        evidence_locator_sha256=locator.canonical_sha256,
        fact_locator_sha256=hashlib.sha256(fact_locator_json.encode()).hexdigest(),
        reviewer="owner",
        knowledge_at=datetime(2026, 8, 5, tzinfo=UTC),
        context=context,
        definition_revision=definition,
    )
    payload = {
        "schema_version": "reviewed_kpi_definition_captures.v1",
        "ticker": base.ticker,
        "source_doc_id": base.source_doc_id,
        "source_doc_sha256": base.source_doc_sha256,
        "period_end": base.period_end.isoformat(),
        "fiscal_period_type": base.fiscal_period_type.value,
        "extracted_at": base.extracted_at.isoformat().replace("+00:00", "Z"),
        "reviewed_by": "owner",
        "captures": [capture.model_dump(mode="json")],
    }
    seal = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return IssuerFactManifestV2(
        ticker=base.ticker,
        source_doc_id=base.source_doc_id,
        source_doc_sha256=base.source_doc_sha256,
        period_end=base.period_end,
        fiscal_period_type=base.fiscal_period_type,
        values=(kpi, base.values[1]),
        expected=(kpi.expected(), base.expected[1]),
        extracted_at=base.extracted_at,
        reviewed_by="owner",
        reviewed_capture_set_sha256=seal,
        reviewed_kpi_definition_captures=(capture,),
    )


def build_v2_cli_fixture(conn: sqlite3.Connection) -> IssuerFactManifestV2:
    """Seed migrated evidence authority and return a sealed v2 CLI fixture."""

    _document(conn)
    return _v2_manifest(_seed_v2_authority(conn))


def _reseal_v2(
    manifest: IssuerFactManifestV2,
    captures: tuple[ReviewedKpiDefinitionCapture, ...],
) -> IssuerFactManifestV2:
    candidate = manifest.model_copy(update={"reviewed_kpi_definition_captures": captures})
    return IssuerFactManifestV2.model_validate(
        {
            **candidate.model_dump(mode="json"),
            "reviewed_capture_set_sha256": candidate.computed_reviewed_capture_set_sha256,
        }
    )


def test_v1_canonical_hash_and_null_definition_binding_are_frozen(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _manifest().manifest_sha256 == (
        "7e82a188d73b5c81f893f1e5200764987d31c0acab2ea5294c05578458f17c56"
    )
    db_path = migrated_db(tmp_path / "v1-null-binding.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _document(conn)
        conn.commit()
        import pipeline.restatement_detector as restatement_detector

        monkeypatch.setattr(restatement_detector, "resolve_fact_row", _noop_resolve)
        apply_issuer_fact_manifest(conn, _manifest(), apply=True)
        binding = conn.execute(
            "SELECT kpi_definition_revision_id FROM kpi_fact_semantic_contexts"
        ).fetchone()
        assert binding is not None and binding[0] is None
    finally:
        conn.close()


def test_v2_apply_and_receipt_replay_preserve_exact_reviewed_commitments(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = migrated_db(tmp_path / "v2-reviewed-capture.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    register_sqlite_integrity_functions(conn)
    try:
        _document(conn)
        locator = _seed_v2_authority(conn)
        conn.commit()
        import pipeline.kpi_source_review as source_review
        import pipeline.restatement_detector as restatement_detector

        monkeypatch.setattr(restatement_detector, "resolve_fact_row", _noop_resolve)
        monkeypatch.setattr(source_review, "require_canonical_kpi_resolution", _noop_resolve)
        manifest = _v2_manifest(locator)

        first = apply_issuer_fact_manifest(conn, manifest, apply=True)
        second = apply_issuer_fact_manifest(conn, manifest, apply=True)

        assert (
            first.kpi_inserted,
            first.semantic_contexts_inserted,
            first.definition_revisions_inserted,
            first.comparability_revisions_inserted,
        ) == (1, 1, 1, 0)
        assert first.definition_revision_ids == ("definition-meli-tpv-r1",)
        assert (
            second.kpi_inserted,
            second.kpi_skipped_existing,
            second.semantic_contexts_inserted,
            second.definition_revisions_inserted,
            second.comparability_revisions_inserted,
        ) == (0, 1, 0, 0, 0)
        stored = conn.execute(
            "SELECT fact.kpi_definition_id,context.kpi_definition_revision_id,"
            "context.reviewed_by,context.knowledge_at FROM kpi_facts fact "
            "JOIN kpi_fact_semantic_contexts context ON context.kpi_fact_id=fact.id"
        ).fetchone()
        assert tuple(stored) == (
            6401,
            "definition-meli-tpv-r1",
            "owner",
            "2026-08-05T00:00:00Z",
        )
        assert conn.execute("SELECT COUNT(*) FROM kpi_definition_revisions").fetchone()[0] == 1
        assert first.receipt is not None
        persist_document_coverage_receipt(conn, first.receipt)
    finally:
        conn.close()


def test_v1_replay_cannot_downgrade_a_reviewed_same_document_binding(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = migrated_db(tmp_path / "v1-cannot-downgrade-v2.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    register_sqlite_integrity_functions(conn)
    try:
        _document(conn)
        manifest = _v2_manifest(_seed_v2_authority(conn))
        conn.commit()
        import pipeline.kpi_source_review as source_review
        import pipeline.restatement_detector as restatement_detector

        monkeypatch.setattr(restatement_detector, "resolve_fact_row", _noop_resolve)
        monkeypatch.setattr(source_review, "require_canonical_kpi_resolution", _noop_resolve)
        apply_issuer_fact_manifest(conn, manifest, apply=True)
        conn.commit()
        v1 = IssuerFactManifest.model_validate(
            {
                key: value
                for key, value in manifest.model_dump(mode="json").items()
                if key
                not in {
                    "schema_version",
                    "reviewed_by",
                    "reviewed_capture_set_sha256",
                    "reviewed_kpi_definition_captures",
                }
            }
        )
        before = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "kpi_facts",
                "kpi_fact_semantic_contexts",
                "kpi_definition_revisions",
                "segment_periods",
                "segment_dimensions",
                "issuer_fact_coverage_receipts",
            )
        }

        with pytest.raises(ValueError, match="v1 cannot attest"):
            apply_issuer_fact_manifest(conn, v1, apply=True)

        assert {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in before
        } == before
    finally:
        conn.close()


def test_v2_apply_and_replay_count_an_exact_comparability_decision(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pipeline.kpi_definition_revisions import persist_kpi_definition_revision

    db_path = migrated_db(tmp_path / "v2-reviewed-comparability.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    register_sqlite_integrity_functions(conn)
    try:
        _document(conn)
        locator = _seed_v2_authority(conn)
        manifest = _v2_manifest(locator)
        original_capture = manifest.reviewed_kpi_definition_captures[0]
        incumbent = original_capture.definition_revision.model_copy(
            update={
                "kpi_definition_revision_id": "definition-meli-tpv-incumbent-r1",
                "idempotency_key": "definition:meli:tpv:incumbent:r1",
                "reported_definition_text": "Previously reviewed TPV definition.",
            }
        )
        persist_kpi_definition_revision(conn, incumbent)
        conn.commit()

        successor = original_capture.definition_revision.model_copy(
            update={
                "kpi_definition_revision_id": "definition-meli-tpv-r2",
                "idempotency_key": "definition:meli:tpv:r2",
                "revision": 2,
                "supersedes_definition_revision_id": incumbent.kpi_definition_revision_id,
            }
        )
        relation = KpiDefinitionComparabilityRevision(
            comparability_revision_id="relation-meli-tpv-r1-r2-v1",
            idempotency_key="relation:meli:tpv:r1:r2:v1",
            predecessor_definition_revision_id=incumbent.kpi_definition_revision_id,
            successor_definition_revision_id=successor.kpi_definition_revision_id,
            revision=1,
            relation_kind=KpiDefinitionRelationKind.SAME_DEFINITION,
            disposition=KpiDefinitionComparabilityDisposition.CONTINUOUS,
            reason_code="issuer_reaffirmed_definition",
            reviewed_by="owner",
            source_document_version_id="document-meli-v1",
            source_evidence_node_id="node-meli-tpv",
            source_locator=locator.model_dump(mode="json", exclude_none=True),
            effective_at=datetime(2026, 6, 30, tzinfo=UTC),
            knowledge_at=datetime(2026, 8, 5, tzinfo=UTC),
            recorded_at=datetime(2026, 8, 5, tzinfo=UTC),
        )
        capture = ReviewedKpiDefinitionCapture.model_validate(
            {
                **original_capture.model_dump(mode="json"),
                "expected_definition_head_id": incumbent.kpi_definition_revision_id,
                "expected_definition_revision": 1,
                "definition_revision": successor.model_dump(mode="json"),
                "comparability_revisions": [relation.model_dump(mode="json")],
            }
        )
        reviewed_manifest = _reseal_v2(manifest, (capture,))
        import pipeline.kpi_source_review as source_review
        import pipeline.restatement_detector as restatement_detector

        monkeypatch.setattr(restatement_detector, "resolve_fact_row", _noop_resolve)
        monkeypatch.setattr(source_review, "require_canonical_kpi_resolution", _noop_resolve)

        first = apply_issuer_fact_manifest(conn, reviewed_manifest, apply=True)
        second = apply_issuer_fact_manifest(conn, reviewed_manifest, apply=True)

        assert (
            first.definition_revisions_inserted,
            first.comparability_revisions_inserted,
            second.definition_revisions_inserted,
            second.comparability_revisions_inserted,
        ) == (1, 1, 0, 0)
        persisted = conn.execute(
            "SELECT comparability_revision_id,commitment_sha256 "
            "FROM kpi_definition_comparability_revisions"
        ).fetchone()
        assert tuple(persisted) == (
            relation.comparability_revision_id,
            relation.commitment_sha256,
        )
    finally:
        conn.close()


def test_v2_stale_definition_head_rejects_every_transaction_effect(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    from pipeline.kpi_definition_revisions import persist_kpi_definition_revision

    db_path = migrated_db(tmp_path / "v2-stale-head.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    register_sqlite_integrity_functions(conn)
    try:
        _document(conn)
        locator = _seed_v2_authority(conn)
        manifest = _v2_manifest(locator)
        reviewed = manifest.reviewed_kpi_definition_captures[0].definition_revision
        incumbent = reviewed.model_copy(
            update={
                "kpi_definition_revision_id": "definition-meli-incumbent-r1",
                "idempotency_key": "definition:meli:incumbent:r1",
                "reported_definition_text": "Incumbent reviewed definition.",
            }
        )
        persist_kpi_definition_revision(conn, incumbent)
        conn.commit()

        with pytest.raises(ValueError, match="head changed"):
            apply_issuer_fact_manifest(conn, manifest, apply=True)

        assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM segment_periods").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM issuer_fact_coverage_receipts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM kpi_definition_revisions").fetchone()[0] == 1
    finally:
        conn.close()


def test_v2_failure_after_fact_and_segment_work_rolls_back_all_effects(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = migrated_db(tmp_path / "v2-post-write-failure.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    register_sqlite_integrity_functions(conn)
    try:
        _document(conn)
        locator = _seed_v2_authority(conn)
        conn.commit()
        import pipeline.issuer_fact_manifest as issuer_manifest_module
        import pipeline.kpi_source_review as source_review
        import pipeline.restatement_detector as restatement_detector

        monkeypatch.setattr(restatement_detector, "resolve_fact_row", _noop_resolve)
        monkeypatch.setattr(source_review, "require_canonical_kpi_resolution", _noop_resolve)

        def fail_receipt(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("injected receipt failure")

        monkeypatch.setattr(
            issuer_manifest_module,
            "persist_document_coverage_receipt",
            fail_receipt,
        )
        with pytest.raises(RuntimeError, match="injected receipt failure"):
            apply_issuer_fact_manifest(conn, _v2_manifest(locator), apply=True)

        for table in (
            "kpi_facts",
            "kpi_fact_semantic_contexts",
            "kpi_definition_revisions",
            "kpi_definition_comparability_revisions",
            "segment_periods",
            "segment_dimensions",
            "issuer_fact_coverage_receipts",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    finally:
        conn.close()


def test_v2_success_preserves_caller_owned_transaction_boundary(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = migrated_db(tmp_path / "v2-caller-transaction.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    register_sqlite_integrity_functions(conn)
    try:
        _document(conn)
        locator = _seed_v2_authority(conn)
        conn.commit()
        import pipeline.kpi_source_review as source_review
        import pipeline.restatement_detector as restatement_detector

        monkeypatch.setattr(restatement_detector, "resolve_fact_row", _noop_resolve)
        monkeypatch.setattr(source_review, "require_canonical_kpi_resolution", _noop_resolve)
        conn.execute("BEGIN")
        conn.execute("CREATE TABLE caller_v2_state (value TEXT NOT NULL)")
        conn.execute("INSERT INTO caller_v2_state VALUES ('preserve-me')")

        result = apply_issuer_fact_manifest(conn, _v2_manifest(locator), apply=True)

        assert result.applied
        assert conn.in_transaction
        assert conn.execute("SELECT value FROM caller_v2_state").fetchone()[0] == "preserve-me"
        assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 1
        conn.rollback()
        assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='caller_v2_state'"
            ).fetchone()
            is None
        )
    finally:
        conn.rollback()
        conn.close()


def test_v2_receipt_rejects_rehashed_forged_definition_binding(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = migrated_db(tmp_path / "v2-forged-receipt.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    register_sqlite_integrity_functions(conn)
    try:
        _document(conn)
        locator = _seed_v2_authority(conn)
        conn.commit()
        import pipeline.kpi_source_review as source_review
        import pipeline.restatement_detector as restatement_detector

        monkeypatch.setattr(restatement_detector, "resolve_fact_row", _noop_resolve)
        monkeypatch.setattr(source_review, "require_canonical_kpi_resolution", _noop_resolve)
        manifest = _v2_manifest(locator)
        result = apply_issuer_fact_manifest(conn, manifest, apply=True)
        assert result.receipt is not None
        original_capture = manifest.reviewed_kpi_definition_captures[0]
        forged_definition = original_capture.definition_revision.model_copy(
            update={"reported_definition_text": "Forged reviewed wording."}
        )
        forged_capture = original_capture.model_copy(
            update={"definition_revision": forged_definition}
        )
        forged_manifest = _reseal_v2(manifest, (forged_capture,))
        forged_receipt = result.receipt.model_copy(
            update={
                "application_manifest_json": forged_manifest.canonical_json,
                "application_manifest_sha256": forged_manifest.manifest_sha256,
            }
        )

        with pytest.raises(ValueError, match="definition replay conflicts"):
            persist_document_coverage_receipt(conn, forged_receipt)
    finally:
        conn.close()
