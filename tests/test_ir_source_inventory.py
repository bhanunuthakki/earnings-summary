"""Durable IR discovery evidence and source-coverage contracts."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import pytest
from alembic.config import Config

from alembic import command
from execution import sync_ir_source_inventory as cli
from ir_pipeline.discover._docmeta import CandidateDoc
from ir_pipeline.discover.generic import CrawlPageOutcome, DocumentDiscoveryInventory
from ir_pipeline.source_inventory import (
    IRSourceInventoryRequest,
    source_inventory_request,
    sync_ir_source_inventory,
)
from runtime.job_runtime import JobAlreadyRunningError

ROOT = Path(__file__).resolve().parents[1]
STAMP = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)
CONFIG_SHA = "c" * 64


class _BusyLock:
    write_sets: ClassVar[list[str]] = []

    def __init__(self, repo_root: Path, job_name: str, write_sets: list[str]) -> None:
        assert repo_root.exists()
        assert job_name
        type(self).write_sets = write_sets

    def __enter__(self) -> _BusyLock:
        raise JobAlreadyRunningError("already owned")

    def __exit__(self, *args: object) -> None:
        return None


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _conn(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "ir-source-inventory.db"
    config = _config(path)
    command.stamp(config, "0213_decision_draft_provider_id")
    command.upgrade(config, "0220_source_inventory_seals")
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _inventory(*, partial: bool = False) -> DocumentDiscoveryInventory:
    root_anchors = (
        ("https://ir.acme.test/archive", "Results Archive"),
        ("https://cdn.acme.test/q4-2025.pdf", "Q4 2025 Results"),
    )
    pages = [
        CrawlPageOutcome(
            page_url="https://ir.acme.test/",
            outcome="succeeded",
            anchor_count=len(root_anchors),
            anchors=root_anchors,
        )
    ]
    if partial:
        pages.append(
            CrawlPageOutcome(
                page_url="https://ir.acme.test/archive",
                outcome="robots_denied",
                anchor_count=0,
                failure_reason="robots_txt",
            )
        )
    return DocumentDiscoveryInventory(
        candidates=(
            CandidateDoc(
                url="https://cdn.acme.test/q4-2025.pdf",
                link_text="Q4 2025 Results",
                filename_hint="q4-2025.pdf",
                doc_type_guess="press_release",
                year_guess=2025,
                quarter_guess=4,
                source_page="https://ir.acme.test/",
            ),
        ),
        pages=tuple(pages),
        crawl_complete=not partial,
        crawl_stop_reason="page_failure" if partial else "frontier_exhausted",
    )


def _request(inventory: DocumentDiscoveryInventory, *, apply: bool) -> IRSourceInventoryRequest:
    return source_inventory_request(
        issuer_id="issuer-acme",
        ticker="ACME",
        ir_url="https://ir.acme.test/",
        revision=1,
        inventory=inventory,
        retrieval_config_sha256=CONFIG_SHA,
        collector_code_version="sync-ir-source-inventory@1",
        started_at=STAMP,
        completed_at=STAMP,
        recorded_at=STAMP,
        reconciled_at=STAMP,
        apply=apply,
    )


def test_dry_run_plans_without_database_or_blob_writes(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    blob_root = tmp_path / "blobs"
    try:
        result = sync_ir_source_inventory(
            conn, _request(_inventory(), apply=False), blob_root=blob_root
        )
        assert result.mode == "dry_run"
        assert result.candidate_count == 1
        assert conn.execute("SELECT COUNT(*) FROM evidence_content_blobs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM source_inventory_snapshots").fetchone()[0] == 0
        assert not blob_root.exists()
    finally:
        conn.close()


def test_apply_preserves_page_and_candidate_artifacts_before_coverage(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    blob_root = tmp_path / "blobs"
    try:
        result = sync_ir_source_inventory(
            conn, _request(_inventory(), apply=True), blob_root=blob_root
        )
        assert result.mode == "apply"
        assert not result.complete
        assert conn.execute("SELECT COUNT(*) FROM evidence_content_blobs").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM evidence_source_observations").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM expected_documents").fetchone()[0] == 1
        assert conn.execute(
            "SELECT completion_status FROM source_inventory_snapshot_seals"
        ).fetchone() == ("incomplete",)
        assert len(tuple(path for path in blob_root.rglob("*") if path.is_file())) == 2
    finally:
        conn.close()


def test_partial_crawl_retains_failure_and_every_discovered_candidate(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    try:
        result = sync_ir_source_inventory(
            conn,
            _request(_inventory(partial=True), apply=True),
            blob_root=tmp_path / "blobs",
        )
        assert not result.complete
        assert conn.execute("SELECT outcome FROM source_inventory_snapshots").fetchone() == (
            "partial",
        )
        assert conn.execute(
            "SELECT completion_status FROM source_inventory_snapshot_seals"
        ).fetchone() == ("incomplete",)
        assert conn.execute(
            "SELECT failure_reason FROM source_inventory_components "
            "WHERE outcome = 'failed' AND component_key LIKE 'page-access:%'"
        ).fetchone() == ("robots_txt",)
        assert conn.execute("SELECT COUNT(*) FROM expected_documents").fetchone()[0] == 1
    finally:
        conn.close()


def test_exact_apply_replay_is_idempotent(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    request = _request(_inventory(), apply=True)
    try:
        first = sync_ir_source_inventory(conn, request, blob_root=tmp_path / "blobs")
        second = sync_ir_source_inventory(conn, request, blob_root=tmp_path / "blobs")
        assert first.records_created > 0
        assert second.records_created == 0
        assert conn.execute("SELECT COUNT(*) FROM source_inventory_snapshots").fetchone()[0] == 1
    finally:
        conn.close()


def test_empty_successful_crawl_is_an_explicit_coverage_gap(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    inventory = DocumentDiscoveryInventory(
        candidates=(),
        pages=(
            CrawlPageOutcome(
                page_url="https://ir.acme.test/",
                outcome="succeeded",
                anchor_count=0,
                anchors=(),
            ),
        ),
        crawl_complete=True,
        crawl_stop_reason="frontier_exhausted",
    )
    try:
        sync_ir_source_inventory(
            conn, _request(inventory, apply=True), blob_root=tmp_path / "blobs"
        )
        assert conn.execute("SELECT COUNT(*) FROM expected_documents").fetchone()[0] == 0
        assert conn.execute(
            "SELECT completion_status FROM source_inventory_snapshot_seals"
        ).fetchone() == ("incomplete",)
    finally:
        conn.close()


def test_cli_defaults_to_structured_read_only_dry_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    conn = _conn(tmp_path)
    db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    conn.close()
    capsys.readouterr()

    def _discover(**_kwargs: object) -> DocumentDiscoveryInventory:
        return _inventory()

    monkeypatch.setattr(
        cli,
        "discover_document_inventory",
        _discover,
    )

    exit_code = cli.main(
        [
            "--db",
            str(db_path),
            "--issuer-id",
            "issuer-acme",
            "--ticker",
            "ACME",
            "--ir-url",
            "https://ir.acme.test/",
            "--revision",
            "1",
            "--blob-root",
            str(tmp_path / "blobs"),
        ]
    )

    captured = capsys.readouterr()
    output = json.loads(captured.out)
    events = [json.loads(line) for line in captured.err.splitlines()]
    assert exit_code == 0
    assert output["mode"] == "dry_run"
    assert output["crawl_stop_reason"] == "frontier_exhausted"
    assert output["page_outcomes"] == [
        {
            "page_ordinal": 1,
            "outcome": "succeeded",
            "anchor_count": 2,
            "failure_reason": None,
        }
    ]
    assert [event["event"] for event in events] == [
        "ir_source_inventory_started",
        "ir_source_inventory_completed",
    ]
    assert not (tmp_path / "blobs").exists()


def test_cli_apply_returns_retryable_exit_before_opening_locked_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "JobLock", _BusyLock)
    db_path = tmp_path / "absent.db"
    blob_root = tmp_path / "blobs"

    exit_code = cli.main(
        [
            "--db",
            str(db_path),
            "--issuer-id",
            "issuer-acme",
            "--ticker",
            "ACME",
            "--ir-url",
            "https://ir.acme.test/",
            "--revision",
            "1",
            "--blob-root",
            str(blob_root),
            "--apply",
        ]
    )

    assert exit_code == 75
    assert any(item.startswith("sqlite:") for item in _BusyLock.write_sets)
    assert any(item.startswith("evidence-blobs:") for item in _BusyLock.write_sets)
    assert any(item.startswith("source-inventory:") for item in _BusyLock.write_sets)
    assert not db_path.exists()
    assert not blob_root.exists()


def test_cli_failure_log_omits_raw_exception_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    conn = _conn(tmp_path)
    db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    conn.close()

    def _fail(**_kwargs: object) -> DocumentDiscoveryInventory:
        raise RuntimeError("credential-bearing response body")

    monkeypatch.setattr(cli, "discover_document_inventory", _fail)
    exit_code = cli.main(
        [
            "--db",
            str(db_path),
            "--issuer-id",
            "issuer-acme",
            "--ticker",
            "ACME",
            "--ir-url",
            "https://ir.acme.test/",
            "--revision",
            "1",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "credential-bearing" not in captured.err
    assert '"error_type": "RuntimeError"' in captured.err
    assert captured.out == ""
