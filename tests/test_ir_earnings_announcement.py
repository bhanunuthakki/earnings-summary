"""Calendar notices stay supporting evidence and cannot claim financial coverage."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

import ir_uploads
from execution import categorize_ir_uploads as registration
from ir_pipeline.discover import generic
from ir_pipeline.discover._docmeta import classify
from ir_uploads import CategorizationResult, classify_ir_file
from provenance.evidence_ledger import ContentBlob, EvidenceLedger, SourceObservation
from provenance.reporting_document_scope import (
    classify_reporting_document,
    reporting_document_scope,
)
from provenance.source_coverage import ExpectedDocument, SourceCoverageLedger
from provenance.source_coverage_reconcile import (
    ExpectedDocumentImport,
    ExplicitAbsence,
    InventoryComponentImport,
    SourceCoverageImport,
    reconcile_source_coverage,
)
from sqlite_runtime import register_sqlite_integrity_functions

NOTICE = "Meta to Announce Second Quarter 2026 Results\nMENLO PARK, Calif., July 14, 2026 /PRNewswire/ -- Meta announced today.\nMeta will hold its conference call on July 29, 2026."
RESULTS = "Meta Reports Second Quarter 2026 Results\nMENLO PARK, Calif., July 29, 2026\nMeta today reported financial results for the second quarter ended June 30, 2026.\nMeta will announce third quarter results in October."


@pytest.mark.parametrize(
    "text,expected,period",
    [
        (NOTICE, "ir_earnings_announcement", date(2026, 7, 14)),
        (
            "MENLO PARK, Calif., July 14, 2026 /PRNewswire/ -- Meta announced today.\n" + NOTICE,
            "ir_earnings_announcement",
            date(2026, 7, 14),
        ),
        (RESULTS, "ir_press_release", date(2026, 6, 30)),
        (
            "Second Quarter 2026 Results\nMENLO PARK, Calif., July 29, 2026\nMeta will announce third quarter results in October.",
            "ir_press_release",
            date(2026, 6, 30),
        ),
        ("Earnings Call Script\n" + NOTICE, "ir_transcript", date(2026, 6, 30)),
        (
            "Earnings Presentation\nSecond Quarter 2026\n" + NOTICE,
            "ir_presentation",
            date(2026, 6, 30),
        ),
        (
            "Meta to Announce Second Quarter 2026 Results\nPublished: July 14, 2026\nAudio Replay, July 29, 2026",
            "ir_earnings_announcement",
            date(2026, 7, 14),
        ),
    ],
)
def test_notice_title_does_not_replace_financial_or_transcript_title(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    expected: str,
    period: date,
) -> None:
    path = tmp_path / "source.pdf"
    path.write_bytes(b"%PDF-1.4 synthetic")
    monkeypatch.setattr(ir_uploads, "fingerprint", Mock(return_value=text))
    result = classify_ir_file(path, ticker_hint="META", calendar_override="calendar")
    assert isinstance(result, CategorizationResult)
    assert result.doc_type.value == expected
    assert result.period_end == period


def test_discovery_and_source_inventory_label_are_supporting() -> None:
    name = "Meta-to-Announce-Second-Quarter-2026-Results.pdf"
    assert classify(name) == "earnings_announcement"
    assert (
        getattr(generic, "_guess_doc_type")(name, "Meta to Announce Second Quarter 2026 Results")
        == "earnings_announcement"
    )
    assert classify_reporting_document(
        source_kind="ir_document", document_type="earnings_announcement", form_type=None
    ) == ("excluded_supporting", None, "earnings_calendar_announcement")


def test_notice_registration_skips_legacy_analytical_indexes(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "ir_documents/incoming.pdf"
    source.parent.mkdir()
    source.write_bytes(b"%PDF-1.4 synthetic notice")
    monkeypatch.setattr(ir_uploads, "fingerprint", Mock(return_value=NOTICE))

    def classify_notice(
        path: Path, **kwargs: object
    ) -> ir_uploads.CategorizationResult | ir_uploads.CategorizationFailure:
        return classify_ir_file(path, ticker_hint="META", calendar_override="calendar")

    monkeypatch.setattr(registration, "classify_ir_file", classify_notice)
    quarterly = Mock(side_effect=AssertionError("notice entered quarterly index"))
    events = Mock(side_effect=AssertionError("notice entered analytical event index"))
    monkeypatch.setattr(registration.index_manager, "register_ir_document", quarterly)
    monkeypatch.setattr(registration.index_manager, "register_event_document", events)
    with sqlite3.connect(migrated_db(tmp_path / "synthetic.db")) as conn:
        conn.row_factory = sqlite3.Row
        result = registration.process_ir_document(
            source, conn, False, None, source.parent, tmp_path
        )
        row = conn.execute("SELECT doc_type, period_end FROM documents").fetchone()
        assert result["status"] == "categorized"
        assert row["doc_type"] == "ir_earnings_announcement"
        assert str(row["period_end"]).startswith("2026-07-14")
        assert quarterly.call_count == events.call_count == 0


def test_supporting_html_notice_does_not_loosen_release_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "notice.html"
    path.write_text("<html><body>" + NOTICE + "</body></html>")
    monkeypatch.setattr(ir_uploads, "fingerprint", Mock(return_value=NOTICE))
    result = classify_ir_file(path, ticker_hint="META", calendar_override="calendar")
    assert isinstance(result, ir_uploads.CategorizationFailure)
    assert result.reason == "html_earnings_release_unidentified"


def test_discovery_preserves_deck_transcript_and_spreadsheet_hints() -> None:
    for name, expected in [
        ("presentation", "deck"),
        ("transcript", "transcript"),
        ("table.xlsx", "spreadsheet"),
    ]:
        assert classify(name + " Meta to Announce Second Quarter Results") == expected
    assert (
        classify_reporting_document(
            source_kind="ir_document", document_type="investor_presentation", form_type=None
        )[0]
        == "governed_reporting"
    )


@pytest.mark.parametrize(
    "text,filename",
    [
        (
            "Meta to Announce Second Quarter 2026 Results\nMeta will hold its conference call on July 29, 2026.",
            "notice.pdf",
        ),
        ("Meta to Announce Second Quarter 2026 Results", "notice-2026.pdf"),
        (
            "Meta to Announce Second Quarter 2026 Results\nConference call on Wednesday, July 29, 2026",
            "notice.pdf",
        ),
        (
            "Meta to Announce Second Quarter 2026 Results\nConference Call On Wednesday, July 29, 2026",
            "notice.pdf",
        ),
        (
            "Meta to Announce Second Quarter 2026 Results\nLive webcast, July 29, 2026",
            "notice.pdf",
        ),
        (
            "Meta to Announce Second Quarter 2026 Results\nAudio Replay, July 29, 2026",
            "notice.pdf",
        ),
    ],
)
def test_announcement_requires_publication_dateline(
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    filename: str,
) -> None:
    monkeypatch.setattr(ir_uploads, "fingerprint", Mock(return_value=text))
    result = classify_ir_file(Path(filename), ticker_hint="META", calendar_override="calendar")
    assert isinstance(result, ir_uploads.CategorizationFailure)
    assert result.reason == "publication_date_unidentified"


def test_announcement_period_date_cannot_replace_publication_dateline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = "Meta to Announce Second Quarter 2026 Results\nFor the quarter ended June 30, 2026\nMENLO PARK, Calif., July 14, 2026 /PRNewswire/ -- Meta announced today.\nMeta will hold its call on July 29, 2026."
    monkeypatch.setattr(ir_uploads, "fingerprint", Mock(return_value=text))
    result = classify_ir_file(
        Path("notice-2026.pdf"), ticker_hint="META", calendar_override="calendar"
    )
    assert isinstance(result, CategorizationResult)
    assert result.period_end == date(2026, 7, 14)


def test_sealed_inventory_retains_notice_without_financial_duty_binding(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    stamp = datetime(2026, 7, 30, tzinfo=UTC)
    with sqlite3.connect(migrated_db(tmp_path / "inventory.db")) as conn:
        register_sqlite_integrity_functions(conn)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "INSERT INTO issuer_entities VALUES (?,?,?,?)",
            ("issuer-meta", "issuer-meta", "operating_company", stamp),
        )
        conn.execute(
            "INSERT INTO source_obligation_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "earnings",
                "earnings",
                "earnings",
                1,
                "issuer-meta",
                None,
                "issuer_publisher",
                "issuer_earnings_materials",
                "required",
                "publisher_surface_exhaustion",
                stamp,
                None,
                "deterministic",
                "fixture",
                "{}",
                stamp,
                stamp,
                stamp,
                None,
            ),
        )
        ledger = EvidenceLedger(conn)
        ledger.persist(
            ContentBlob(
                sha256="a" * 64,
                byte_size=10,
                media_type="text/html",
                storage_uri="file:///synthetic-meta-archive",
                recorded_at=stamp,
            )
        )
        ledger.persist(
            SourceObservation(
                observation_id="archive",
                idempotency_key="archive",
                source_kind="ir_crawl",
                source_url="https://ir.example.test/meta",
                blob_sha256="a" * 64,
                source_published_at=None,
                filing_at=None,
                accepted_at=None,
                observed_at=stamp,
                retrieved_at=stamp,
                retrieval_config_sha256="b" * 64,
                collector_code_version="fixture",
            )
        )
        request = SourceCoverageImport(
            inventory_key="issuer-meta:ir-crawl",
            revision=1,
            issuer_id="issuer-meta",
            ticker="META",
            source_kind="ir_crawl",
            source_url="https://ir.example.test/meta",
            source_observation_id="archive",
            outcome="succeeded",
            authoritative=True,
            retrieval_config_sha256="b" * 64,
            collector_code_version="fixture",
            started_at=stamp,
            completed_at=stamp,
            recorded_at=stamp,
            reconciled_at=stamp,
            components=(
                InventoryComponentImport(
                    component_key="archive",
                    component_kind="primary",
                    source_url="https://ir.example.test/meta",
                    source_observation_id="archive",
                    outcome="succeeded",
                    ordinal=0,
                ),
            ),
            expected_documents=tuple(
                ExpectedDocumentImport(
                    expected_document_key=key,
                    source_kind="ir_document",
                    document_type=kind,
                    source_url=f"https://ir.example.test/{key}.pdf",
                    period_end=period,
                    expectation_basis="authoritative",
                    absence=ExplicitAbsence(
                        coverage_status="available",
                        reason_code="source_inventory_reconciled",
                        reason_details=(("source", "synthetic_archive"),),
                    ),
                )
                for key, kind, period in (
                    ("notice", "earnings_announcement", datetime(2026, 7, 14, tzinfo=UTC)),
                    ("results", "earnings_release", datetime(2026, 6, 30, tzinfo=UTC)),
                )
            ),
            apply=True,
        )
        conn.commit()
        result = reconcile_source_coverage(conn, request)
        assert result.expected_document_count == 2
        assert conn.execute(
            "SELECT completion_status FROM source_inventory_snapshot_seals WHERE snapshot_id=?",
            (result.snapshot_id,),
        ).fetchone() == ("complete",)
        assert conn.execute(
            "SELECT COUNT(*) FROM expected_document_lifecycle_revisions"
        ).fetchone() == (2,)
        assert conn.execute(
            "SELECT expected.expected_document_key,binding.document_family "
            "FROM expected_documents expected LEFT JOIN expected_document_obligation_bindings binding "
            "USING (expected_document_id) ORDER BY expected.expected_document_key"
        ).fetchall() == [("notice", None), ("results", "issuer_earnings_materials")]
        decisions, captured, incomplete = reporting_document_scope(conn, stamp, stamp)
        assert [(item.outcome, item.reason_code) for item in decisions] == [
            ("excluded_supporting", "earnings_calendar_announcement"),
            ("governed_reporting", "governed_ir_reporting_document"),
        ]
        assert captured == {}  # Available source links are not captured financial facts.
        assert incomplete == 0
        assert conn.execute("SELECT COUNT(*) FROM fact_observations_v2").fetchone() == (0,)
        assert reconcile_source_coverage(conn, request).records_created == 0
        unsupported = ExpectedDocument(
            expected_document_id="bad-notice",
            idempotency_key="bad-notice",
            snapshot_id=result.snapshot_id,
            expected_document_key="bad-notice",
            issuer_id="issuer-meta",
            ticker="META",
            source_kind="ir_document",
            document_type="earnings_announcement",
            expectation_basis="authoritative",
            recorded_at=stamp,
            source_obligation_revision_id="earnings",
        )
        with pytest.raises(ValueError, match="cannot claim a financial source obligation"):
            SourceCoverageLedger(conn).persist(unsupported)
        with pytest.raises(ValueError, match="explicit governed document_type"):
            SourceCoverageLedger(conn).persist(
                unsupported.model_copy(
                    update={
                        "source_obligation_revision_id": None,
                        "document_type": "unknown_artifact",
                    }
                )
            )
        with pytest.raises(ValueError):
            SourceCoverageLedger(conn).persist(
                unsupported.model_copy(
                    update={
                        "source_obligation_revision_id": None,
                        "issuer_id": "missing-issuer",
                    }
                )
            )
        assert conn.execute("SELECT COUNT(*) FROM expected_documents").fetchone() == (2,)
