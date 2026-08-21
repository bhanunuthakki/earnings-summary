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
    IssuerFactValue,
    IssuerManifestFactKind,
    apply_issuer_fact_manifest,
)


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
