"""The existing publisher admits one closed calendar-year HTML release cell."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from execution import fetch_ir_documents as fetch
from pipeline import managed_ir_sources as managed
from pipeline.issuer_document_inventory import (
    ExpectedIssuerDocument,
    IssuerDocumentInventoryRequest,
)

URL = "https://issuer.test/second-quarter-2026-results"
PAYLOAD = b"""<html><body><main><h1>Meta Reports Second Quarter 2026 Results</h1>
<p>Meta Platforms today reported financial results for the second quarter 2026.</p>
<table><tr><td>Revenue</td><td>$40 billion</td></tr></table></main></body></html>"""


def _qualified_period(*_args: object, **_kwargs: object) -> bool:
    # Source-period selection has its own real positive/negative suite. These
    # fixtures exercise classification, media/calendar admission and publication.
    return True


@pytest.fixture
def state(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    root = tmp_path / "state"
    root.mkdir()
    database = migrated_db(root / "data" / "portfolio.db")
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(database))
    monkeypatch.setattr(managed, "reported_quarter_selection", _qualified_period)
    with sqlite3.connect(database) as conn:
        conn.execute(
            "INSERT INTO tracked_companies(ticker,name,list_type,instrument_type,fiscal_year_end) "
            "VALUES ('META','Synthetic issuer','portfolio','equity','12-31')"
        )
    return root, database


def _download(_url: str, objects: Path, _name: str) -> Path:
    objects.mkdir(parents=True, exist_ok=True)
    path = objects / "release.html"
    path.write_bytes(PAYLOAD)
    return path


def _prepare(root: Path, database: Path, mode: str) -> managed.IssuerDocumentStagingRequest:
    if mode == "discovery":
        _path, request, _classification = managed.prepare_issuer_document_candidate(
            "META",
            URL,
            state_root=root,
            db_path=database,
            owner_requested=False,
            calendar_override="calendar",
            fiscal_year=2026,
            fiscal_quarter=2,
            legacy_base_name="META_q2",
            download=_download,
        )
        assert request is not None
        return request
    inventory = IssuerDocumentInventoryRequest(
        ticker="META",
        fiscal_year=2026,
        fiscal_quarter=2,
        period_end=date(2026, 6, 30),
        expected_documents=(
            ExpectedIssuerDocument(source_url=URL, document_type="ir_press_release"),
        ),
    )
    request = managed.IssuerDocumentStagingRequest(
        attempt_id="html-release-fixture",
        inventory_request=inventory,
        inventory_request_sha256=inventory.request_sha256,
        owner_requested=False,
        calendar_override="calendar",
    )
    managed.prepare_issuer_document_sources(
        request, state_root=root, db_path=database, download=_download
    )
    return request


@pytest.mark.parametrize("mode", ["discovery", "direct_staging"])
def test_html_release_uses_existing_publication_and_replay(
    state: tuple[Path, Path], mode: str
) -> None:
    root, database = state
    request = _prepare(root, database, mode)
    result = managed.publish_prepared_issuer_documents(request, state_root=root, db_path=database)
    assert result.inserted_document_ids and not result.reused_document_ids
    assert result.staging_receipt.documents[0].media_type == "text/html"
    assert result.staging_receipt.documents[0].source_url == URL
    assert result.canonical_paths == (
        f"ir_documents/META/2026-06-30/ir_press_release__{hashlib.sha256(PAYLOAD).hexdigest()[:8]}.html",
    )
    assert (root / result.canonical_paths[0]).read_bytes() == PAYLOAD
    assert (
        managed.publish_prepared_issuer_documents(request, state_root=root, db_path=database)
        == result
    )
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone() == (1,)
        assert conn.execute(
            "SELECT media_type,byte_size FROM evidence_content_blobs"
        ).fetchall() == [("text/html", len(PAYLOAD))]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.execute("UPDATE tracked_companies SET fiscal_year_end='12-30'")
    shutil.rmtree(root / ".tmp/managed_ir_staging" / request.attempt_id)
    # Committed recovery uses its durable byte-bound proof after policy changes.
    assert (
        managed.publish_prepared_issuer_documents(request, state_root=root, db_path=database)
        == result
    )


@pytest.mark.parametrize("stored", [None, "12", "12-30", "12-99", "06-30"])
@pytest.mark.parametrize("mode", ["discovery", "direct_staging"])
def test_html_candidate_keeps_incomplete_or_noncalendar_metadata_on_legacy_handoff(
    state: tuple[Path, Path], stored: str | None, mode: str
) -> None:
    root, database = state
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE tracked_companies SET fiscal_year_end=?", (stored,))
    if mode == "direct_staging":
        with pytest.raises(managed.IssuerDocumentPreparationError) as exc:
            _prepare(root, database, mode)
        assert exc.value.code in {"reported_quarter_window_denied", "staging_media_not_admitted"}
        with sqlite3.connect(database) as conn:
            assert conn.execute("SELECT COUNT(*) FROM documents").fetchone() == (0,)
        return
    if stored is None:
        # The existing quarter-window guard rejects missing metadata before downloading.
        with pytest.raises(
            managed.IssuerDocumentPreparationError, match="reported_quarter_window_denied"
        ):
            _prepare(root, database, "discovery")
    else:
        path, request, bound = managed.prepare_issuer_document_candidate(
            "META",
            URL,
            state_root=root,
            db_path=database,
            owner_requested=False,
            calendar_override="calendar",
            fiscal_year=2026,
            fiscal_quarter=2,
            legacy_base_name="META_q2",
            download=_download,
        )
        assert request is None and bound is not None
        assert path.read_bytes() == PAYLOAD
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone() == (0,)


@pytest.mark.parametrize("mode", ["discovery", "direct_staging"])
def test_uncommitted_html_rechecks_full_calendar_before_publication(
    state: tuple[Path, Path], mode: str
) -> None:
    root, database = state
    request = _prepare(root, database, mode)
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE tracked_companies SET fiscal_year_end='12-30'")
    with pytest.raises(
        managed.PreparedIssuerDocumentPublisherError, match="staged_media_not_admitted"
    ):
        managed.publish_prepared_issuer_documents(request, state_root=root, db_path=database)
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone() == (0,)
    assert not (root / "ir_documents").exists()


@pytest.mark.parametrize(
    ("family", "media", "suffix"),
    [
        ("ir_presentation", "text/html", ".html"),
        ("ir_press_release", "text/html", ".pdf"),
        ("ir_press_release", "application/pdf", ".html"),
        (
            "ir_press_release",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".xlsx",
        ),
    ],
)
def test_staged_html_rejects_family_media_and_suffix_substitution(
    state: tuple[Path, Path], family: str, media: str, suffix: str
) -> None:
    root, database = state
    request = _prepare(root, database, "discovery")
    receipt = managed.validate_prepared_staging(request, state_root=root, db_path=database)
    item = receipt.documents[0].model_dump(mode="json")
    item.update(document_type=family, media_type=media, object_path=f"objects/source{suffix}")
    with pytest.raises(ValidationError, match="family/media/object extension mismatch"):
        managed.StagedIssuerDocument.model_validate(item)


def test_discovery_html_success_has_no_legacy_categorization_or_sidecar(
    state: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, database = state
    folder = fetch.manifest_dir(root)
    folder.mkdir(parents=True)
    (folder / "META_urls.json").write_text(
        json.dumps([{"url": URL, "doc_type": "press_release", "year": 2026, "quarter": "Q2"}])
    )

    def download(url: str) -> tuple[bytes, str, str]:
        assert url == URL
        return PAYLOAD, "", "text/html"

    def no_legacy(*_args: object, **_kwargs: object) -> tuple[()]:
        raise AssertionError("managed HTML must not enter legacy categorization")

    def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(fetch, "_fetch_bytes", download)
    monkeypatch.setattr(fetch, "_run_categorize", no_legacy)
    monkeypatch.setattr(fetch, "reported_quarter_selection_in_connection", _qualified_period)
    monkeypatch.setattr(fetch.time, "sleep", no_sleep)
    summary = fetch.process_ticker(
        "META", root=root, db_path=database, owner_requested=False, categorize=True
    )
    assert summary["managed_registered"] == 1
    assert summary["legacy_outcomes"] == []
    assert not fetch.incoming_urls_path(root).exists()
    with sqlite3.connect(database) as conn:
        row = conn.execute("SELECT source_url,file_path FROM documents").fetchone()
        assert row is not None and row[0] == URL
        assert (root / row[1]).read_bytes() == PAYLOAD
