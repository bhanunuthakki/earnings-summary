"""CLI connection-role regressions for issuer fact manifest activation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from execution import apply_issuer_fact_manifest as cli
from models.facts import Currency, FactLocator, FiscalPeriodType, LocatorKind, Unit
from pipeline.issuer_fact_manifest import (
    IssuerFactManifest,
    IssuerFactValue,
    IssuerManifestFactKind,
)
from sqlite_runtime import SQLiteConnectionRole


def _noop_resolve(*_args: object, **_kwargs: object) -> None:
    return None


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fact_counts(db_path: Path) -> tuple[int, int, int]:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM kpi_facts), "
            "(SELECT COUNT(*) FROM segment_periods), "
            "(SELECT COUNT(*) FROM issuer_fact_coverage_receipts)"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return (int(row[0]), int(row[1]), int(row[2]))


def _seed_source_document(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO documents "
            "(id,ticker,source_type,doc_type,period_end,file_path,sha256,fetched_at,"
            "fetch_status,raw_bytes_size) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                9001,
                "MELI",
                "ir_doc",
                "ir_presentation",
                "2026-06-30",
                "fixture.pdf",
                "a" * 64,
                "2026-08-05T00:00:00Z",
                "fetched",
                10,
            ),
        )
        conn.execute("DROP TRIGGER IF EXISTS trg_kpi_facts_observation_insert")
        conn.execute("DROP TRIGGER IF EXISTS trg_kpi_facts_observation_update")
        conn.commit()
        assert conn.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
    finally:
        conn.close()


def _manifest(path: Path) -> None:
    value = IssuerFactValue(
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
    manifest = IssuerFactManifest(
        ticker="MELI",
        source_doc_id=9001,
        source_doc_sha256="a" * 64,
        period_end=date(2026, 6, 30),
        fiscal_period_type=FiscalPeriodType.Q2,
        values=(value,),
        expected=(value.expected(),),
        extracted_at=datetime(2026, 8, 5, tzinfo=UTC),
    )
    path.write_text(manifest.model_dump_json(), encoding="utf-8")


def _observe_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[SQLiteConnectionRole, bool | None]]:
    connection_requests: list[tuple[SQLiteConnectionRole, bool | None]] = []
    real_connect = cli.connect_sqlite

    def observed_connect(
        path: str | Path,
        *,
        role: SQLiteConnectionRole,
        schema_preflight: bool | None = None,
    ) -> sqlite3.Connection:
        connection_requests.append((role, schema_preflight))
        return real_connect(path, role=role, schema_preflight=schema_preflight)

    monkeypatch.setattr(cli, "connect_sqlite", observed_connect)
    return connection_requests


def test_default_validation_uses_read_only_sqlite_without_side_effects(
    migrated_db: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = migrated_db(tmp_path / "manifest-validation.db")
    manifest_path = tmp_path / "manifest.json"
    _seed_source_document(db_path)
    _manifest(manifest_path)
    before_sha256 = _sha256(db_path)
    before_counts = _fact_counts(db_path)
    connection_requests = _observe_connections(monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(Path(cli.__file__).resolve()),
            "--db",
            str(db_path),
            "--manifest",
            str(manifest_path),
        ],
    )

    assert cli.main() == 0

    output = json.loads(capsys.readouterr().out)
    assert output["applied"] is False
    assert connection_requests == [(SQLiteConnectionRole.READ_ONLY, True)]
    assert _sha256(db_path) == before_sha256
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    finally:
        conn.close()
    after_counts = _fact_counts(db_path)
    assert after_counts == before_counts == (0, 0, 0)
    assert not Path(f"{db_path}-wal").exists()
    assert not Path(f"{db_path}-shm").exists()


def test_apply_uses_writer_sqlite_and_commits_fact_with_coverage(
    migrated_db: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = migrated_db(tmp_path / "manifest-apply.db")
    manifest_path = tmp_path / "manifest.json"
    _seed_source_document(db_path)
    _manifest(manifest_path)
    connection_requests = _observe_connections(monkeypatch)

    import pipeline.restatement_detector as restatement_detector

    monkeypatch.setattr(restatement_detector, "resolve_fact_row", _noop_resolve)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(Path(cli.__file__).resolve()),
            "--db",
            str(db_path),
            "--manifest",
            str(manifest_path),
            "--apply",
        ],
    )

    assert cli.main() == 0

    output = json.loads(capsys.readouterr().out)
    assert output["applied"] is True
    assert output["kpi_inserted"] == 1
    assert output["coverage_receipts_created"] == 1
    assert connection_requests == [(SQLiteConnectionRole.WRITER, True)]
    assert _fact_counts(db_path) == (1, 0, 1)
