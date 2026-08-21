"""Atomic issuer manifest application regressions."""

from __future__ import annotations

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
        locator=FactLocator(pdf_page=3, kind=LocatorKind.PDF_SLIDE, verbatim_snippet="TPV 1,000"),
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
            pdf_page=5, kind=LocatorKind.PDF_SLIDE, verbatim_snippet="Commerce 500"
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
