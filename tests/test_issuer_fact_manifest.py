"""Atomic issuer manifest application regressions."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from models.facts import Currency, FactLocator, FiscalPeriodType, LocatorKind, Unit
from pipeline.issuer_fact_manifest import (
    IssuerFactManifest,
    IssuerFactValue,
    IssuerManifestFactKind,
    apply_issuer_fact_manifest,
)


def _noop_resolve(*_args: object, **_kwargs: object) -> None:
    return None


def _document(conn: sqlite3.Connection, *, doc_id: int = 9001, sha: str = "a" * 64) -> None:
    conn.execute(
        "INSERT INTO documents "
        "(id,ticker,source_type,doc_type,period_end,file_path,sha256,fetched_at,"
        "fetch_status,raw_bytes_size) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            doc_id,
            "MELI",
            "ir_doc",
            "ir_presentation",
            "2026-06-30",
            "fixture.pdf",
            sha,
            "2026-08-05T00:00:00Z",
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
        segment_dim_type="business_unit",
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
        import pipeline.restatement_detector as restatement_detector

        monkeypatch.setattr(restatement_detector, "resolve_fact_row", _noop_resolve)
        manifest = _manifest().model_copy(
            update={
                "values": (_manifest().values[0],),
                "expected": (_manifest().expected[0], _manifest().expected[1]),
            }
        )
        with pytest.raises(ValueError, match=r"expected fact.*missing"):
            apply_issuer_fact_manifest(conn, manifest, apply=True)
        assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM segment_periods").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM issuer_fact_coverage_receipts").fetchone()[0] == 0
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
