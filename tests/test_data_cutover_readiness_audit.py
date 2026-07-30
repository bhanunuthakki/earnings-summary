"""Contracts for the cutoff-pinned data cutover readiness audit."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import provenance.integrity_audit as audit_module
from execution.audit_data_cutover_readiness import main
from provenance.integrity_audit import (
    CutoverAuditOptions,
    audit_cutover_readiness,
)

CUTOFF = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def _publication_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE source_fact_publications (
            publication_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE source_fact_publication_members (
            publication_id TEXT NOT NULL
        );
        CREATE TABLE source_fact_publication_seals (
            publication_id TEXT PRIMARY KEY,
            sealed_at TEXT NOT NULL
        );
        CREATE TABLE source_fact_publication_stream (
            publication_sequence INTEGER PRIMARY KEY,
            publication_id TEXT NOT NULL
        );
        """
    )


def test_cutover_audit_pins_publication_and_stream_coverage_to_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    _publication_schema(conn)
    for publication_id, recorded_at in (
        ("before", CUTOFF - timedelta(seconds=1)),
        ("exact", CUTOFF),
        ("after", CUTOFF + timedelta(seconds=1)),
    ):
        conn.execute(
            "INSERT INTO source_fact_publications VALUES (?,?,?)",
            (publication_id, recorded_at.isoformat(), recorded_at.isoformat()),
        )
        conn.execute(
            "INSERT INTO source_fact_publication_seals VALUES (?,?)",
            (publication_id, recorded_at.isoformat()),
        )
    verified_publications: list[tuple[str, datetime, datetime]] = []
    verified_stream: list[str] = []

    def verify_publication(
        _conn: sqlite3.Connection,
        *,
        publication_id: str,
        cutoff: datetime,
        observed_through: datetime,
    ) -> object:
        _conn.create_function("cutover_audit_probe", 1, lambda value: value)
        verified_publications.append((publication_id, cutoff, observed_through))
        return object()

    def verify_stream(
        _conn: sqlite3.Connection,
        *,
        publication_id: str,
    ) -> object:
        _conn.create_function("cutover_audit_probe", 1, lambda value: value)
        verified_stream.append(publication_id)
        return object()

    monkeypatch.setattr(
        audit_module,
        "verify_source_fact_publication",
        verify_publication,
    )
    monkeypatch.setattr(
        audit_module,
        "publication_event_for_publication",
        verify_stream,
    )

    summary = audit_cutover_readiness(
        conn,
        CutoverAuditOptions(
            knowledge_cutoff=CUTOFF,
            observed_through=CUTOFF,
            sample_limit=2,
            fetch_size=1,
        ),
    )

    publication = next(item for item in summary.coverage if item.gate == "source_fact_publications")
    stream = next(
        item for item in summary.coverage if item.gate == "source_fact_publication_stream"
    )
    assert publication.eligible_count == publication.verified_count == 2
    assert stream.eligible_count == stream.verified_count == 2
    assert publication.failed_count == stream.failed_count == 0
    assert verified_publications == [
        ("before", CUTOFF, CUTOFF),
        ("exact", CUTOFF, CUTOFF),
    ]
    assert verified_stream == ["before", "exact"]
    conn.close()


def test_cutover_audit_reports_exact_missing_schema_with_bounded_samples() -> None:
    conn = sqlite3.connect(":memory:")

    summary = audit_cutover_readiness(
        conn,
        CutoverAuditOptions(
            knowledge_cutoff=CUTOFF,
            observed_through=CUTOFF,
            sample_limit=2,
        ),
    )

    finding = next(
        item
        for item in summary.findings
        if item.code == "CUTOVER_SOURCE_FACT_PUBLICATIONS_SCHEMA_MISSING"
    )
    assert finding.count == 3
    assert len(finding.samples) == 2
    assert summary.has_blockers is True
    assert summary.cutoff_at == CUTOFF
    conn.close()


def test_cutover_cli_always_exits_nonzero_on_blockers(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "empty.db"
    sqlite3.connect(db_path).close()

    status = main(
        [
            "--db-path",
            str(db_path),
            "--cutoff-at",
            CUTOFF.isoformat(),
            "--sample-limit",
            "1",
        ]
    )

    assert status == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["has_blockers"] is True
    assert payload["knowledge_cutoff"] == CUTOFF.isoformat().replace("+00:00", "Z")
    assert payload["observed_through"] == CUTOFF.isoformat().replace("+00:00", "Z")
