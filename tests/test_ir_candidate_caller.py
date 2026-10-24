"""Production caller from durable Wix/Rubrik observations to approval candidates."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import urllib.parse
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

import ir_pipeline.approved_ir_observation_capture as capture_module
import pipeline.ir_candidate_caller as caller_module
from ir_pipeline.approved_ir_observation_capture import (
    ApprovedIrObservationBundle,
    ObservationArtifactRole,
    SealedObservationArtifact,
    load_approved_ir_observation_bundle,
)
from ir_pipeline.authority import IRAuthorityEvidence, PublisherSurfaceEvidence
from ir_pipeline.discover._docmeta import CandidateDoc
from ir_pipeline.discover.generic import CrawlPageOutcome, DocumentDiscoveryInventory
from ir_pipeline.source_inventory import source_inventory_request, sync_ir_source_inventory
from models.documents import DocType
from pipeline.ir_approval_store import CandidateWriteResult, IrCandidateRequest
from pipeline.ir_candidate_caller import (
    IrCandidateCallerError,
    IrCandidateCallerRequest,
    apply_ir_candidate_plan,
    plan_ir_candidates,
)
from pipeline.source_policy import (
    ReportedPeriodStatus,
    reported_period_window_from_inventory,
    reported_quarter_selection_in_connection,
)
from provenance.evidence_ledger import ContentBlob, EvidenceLedger, SourceObservation
from provenance.issuer_registry import IssuerEntity, IssuerRegistry
from provenance.reporting_entity_registry import ReportingEntityRegistry, SourceObligationRevision
from provenance.source_coverage_reconcile import (
    ExpectedDocumentImport,
    ExplicitAbsence,
    InventoryComponentImport,
    SourceCoverageImport,
    reconcile_source_coverage,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

NOW = datetime(2026, 8, 13, 12, 0, 0)


def _seed_source_duties(connection: sqlite3.Connection, issuer_id: str) -> None:
    IssuerRegistry(connection).persist(
        IssuerEntity(
            issuer_id=issuer_id,
            idempotency_key=f"test-period-issuer:{issuer_id}",
            entity_kind="operating_company",
            created_at=NOW,
        )
    )
    for name, authority, family, rule in (
        ("sec", "sec_edgar", "operating_company_periodic", "regulator_inventory"),
        ("ir", "issuer_publisher", "issuer_earnings_materials", "publisher_surface_exhaustion"),
    ):
        ReportingEntityRegistry(connection).persist(
            SourceObligationRevision(
                obligation_revision_id=f"test-period-duty:{name}:{issuer_id}",
                idempotency_key=f"test-period-duty:{name}:{issuer_id}",
                obligation_key=f"test-period-duty:{name}:{issuer_id}",
                revision=1,
                issuer_id=issuer_id,
                authority_kind=authority,
                document_family=family,
                obligation_state="required",
                completeness_rule=rule,
                active_from=NOW,
                active_to=None,
                decision_kind="manual",
                effective_at=NOW,
                knowledge_at=NOW,
                recorded_at=NOW,
                reason_code="synthetic_test_duty",
                reason_details=(("scope", "reported_period"),),
            )
        )
    connection.commit()


def _seed_source_observation(
    connection: sqlite3.Connection,
    *,
    tmp_path: Path,
    observation_id: str,
    source_kind: str,
    source_url: str,
    body: bytes,
) -> str:
    digest = hashlib.sha256(body).hexdigest()
    path = tmp_path / digest
    path.write_bytes(body)
    ledger = EvidenceLedger(connection)
    ledger.persist(
        ContentBlob(
            sha256=digest,
            byte_size=len(body),
            media_type="application/json",
            storage_uri=path.as_uri(),
            recorded_at=NOW,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id=observation_id,
            idempotency_key=observation_id,
            source_kind=source_kind,
            source_url=source_url,
            blob_sha256=digest,
            source_published_at=None,
            filing_at=None,
            accepted_at=None,
            observed_at=NOW,
            retrieved_at=NOW,
            retrieval_config_sha256="c" * 64,
            collector_code_version="synthetic-source@1",
        )
    )
    connection.commit()
    return digest


_seal_artifact_value = getattr(capture_module, "_seal_observation_artifact")
_seal_bundle_value = getattr(capture_module, "_seal_approved_ir_observation_bundle")
if not callable(_seal_artifact_value) or not callable(_seal_bundle_value):
    raise AssertionError("collector-private test sealing boundary is unavailable")
_seal_observation_artifact = cast("Callable[..., SealedObservationArtifact]", _seal_artifact_value)
_seal_approved_ir_observation_bundle = cast(
    "Callable[..., ApprovedIrObservationBundle]", _seal_bundle_value
)
WIX_PERIODS = (
    "2026-06-30",
    "2026-03-31",
    "2025-12-31",
    "2025-09-30",
    "2025-06-30",
)
RBRK_PERIODS = (
    "2026-04-30",
    "2026-01-31",
    "2025-10-31",
    "2025-07-31",
    "2025-04-30",
)


def _wix_payload(
    *,
    omit_last_transcript: bool = False,
    periods: tuple[str, ...] = WIX_PERIODS,
    duplicate_first_presentation: bool = False,
) -> bytes:
    observations: list[dict[str, object]] = []
    kinds = (
        ("earnings-release", "Press release", "release.pdf"),
        ("presentation", "Earnings slides", "slides.pdf"),
        ("investor-update", "Shareholder update", "update.pdf"),
        ("transcript", "Text transcript", "transcript.txt"),
    )
    for index, period in enumerate(periods):
        links = [
            {
                "title": title,
                "url": f"https://investors.wix.com/static-files/{period}-{suffix}",
                "declared_kind": kind,
                "evidence_locator": f"panel[{period}] > link[{title}]",
            }
            for kind, title, suffix in kinds
            if not (omit_last_transcript and index == len(periods) - 1 and kind == "transcript")
        ]
        if duplicate_first_presentation and index == 0:
            links.append(
                dict(
                    links[1],
                    title="Second earnings slides",
                    url=f"https://investors.wix.com/static-files/{period}-slides-v2.pdf",
                )
            )
        links.append(
            {
                "title": "Watch webcast",
                "url": f"https://events.example.test/wix-{period}",
                "declared_kind": "webcast",
                "evidence_locator": f"panel[{period}] > link[webcast]",
            }
        )
        observations.append(
            {
                "observation_key": f"wix-{period}",
                "authority_url": "https://investors.wix.com/financials",
                "raw_sha256": "0" * 64,
                "requested_year": int(period[:4]),
                "selected_year": int(period[:4]),
                "year_control_locator": "quarterly-results > year-dropdown",
                "requested_quarter_end": period,
                "panels": [
                    {
                        "panel_locator": f"year-dropdown > quarter[{period}]",
                        "quarter_end": period,
                        "selected": True,
                        "visible": True,
                        "links": links,
                    }
                ],
            }
        )
    return _sealed_bundle("WIX", observations)


def _rubrik_payload() -> bytes:
    observations: list[dict[str, object]] = []
    for index, period in enumerate(RBRK_PERIODS):
        presentation_url = (
            "https://s203.q4cdn.com/667520861/files/"
            f"doc_presentation/{period}/rbrk-{period}-presentation.pdf"
        )
        links = [
            {
                "title": "Earnings Release",
                "url": f"https://ir.rubrik.com/static-files/{period}-release.pdf",
                "declared_kind": "earnings-release",
                "evidence_locator": f"row[{period}] > link[release]",
            },
            {
                "title": "Investor Presentation",
                "url": presentation_url,
                "declared_kind": "presentation",
                "evidence_locator": f"row[{period}] > link[presentation]",
            },
            {
                "title": "Investor Presentation duplicate",
                "url": presentation_url,
                "declared_kind": "presentation",
                "evidence_locator": f"row[{period}] > link[presentation duplicate]",
            },
            {
                "title": "Prepared remarks",
                "url": f"https://ir.rubrik.com/static-files/{period}-remarks.pdf",
                "declared_kind": "transcript",
                "evidence_locator": f"row[{period}] > link[remarks]",
            },
            {
                "title": "10-Q",
                "url": (
                    "https://www.sec.gov/Archives/edgar/data/1943896/"
                    f"0001943896260000{index + 10}/rbrk.htm"
                ),
                "declared_kind": "sec-filing",
                "evidence_locator": f"row[{period}] > link[10-Q]",
            },
            {
                "title": "Webcast replay",
                "url": f"https://events.example.test/rbrk-{period}",
                "declared_kind": "webcast",
                "evidence_locator": f"row[{period}] > link[webcast]",
            },
        ]
        observations.append(
            {
                "observation_key": f"rbrk-{period}",
                "authority_url": (
                    "https://ir.rubrik.com/financials/quarterly-results/default.aspx"
                ),
                "raw_sha256": "0" * 64,
                "quarter_end": period,
                "row_locator": f"quarter-table > row[{period}]",
                "links": links,
            }
        )
    return _sealed_bundle("RBRK", observations)


def _sealed_bundle(issuer: str, observations: list[dict[str, object]]) -> bytes:
    authority_url = (
        "https://investors.wix.com/financials"
        if issuer == "WIX"
        else "https://ir.rubrik.com/financials/quarterly-results/default.aspx"
    )
    artifacts = [
        _seal_observation_artifact(
            observation_key="publisher-authority",
            role=ObservationArtifactRole.AUTHORITY_RAW,
            content_bytes=b"<html>publisher authority</html>",
            media_type="text/html; charset=utf-8",
            source_url=authority_url,
            evidence_locator="publisher-authority-response",
            observed_at=NOW,
            retrieved_at=NOW,
        )
    ]
    for observation in observations:
        observation_key = str(observation["observation_key"])
        row_locator = observation.get("row_locator")
        if row_locator is not None:
            locator = str(row_locator)
            proof_links = observation["links"]
        else:
            panels = observation.get("panels")
            if not isinstance(panels, list) or not panels or not isinstance(panels[0], dict):
                raise AssertionError("test Wix observation has no panel locator")
            first_panel = json.loads(json.dumps(panels[0]))
            if not isinstance(first_panel, dict):
                raise AssertionError("test Wix panel is not an object")
            validated_panel = cast("dict[str, object]", first_panel)
            panel_locator = validated_panel.get("panel_locator")
            if not isinstance(panel_locator, str):
                raise AssertionError("test Wix panel locator is not a string")
            locator = panel_locator
            proof_links = validated_panel["links"]
        rendered_bytes = json.dumps(
            {
                "document_title": "Approved IR test fixture",
                "page_url": authority_url,
                "schema_version": "approved-ir-rendered-link-proof@1",
                "visible_state": {"links": proof_links},
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        digest = hashlib.sha256(rendered_bytes).hexdigest()
        observation["raw_sha256"] = digest
        artifacts.append(
            _seal_observation_artifact(
                observation_key=observation_key,
                role=ObservationArtifactRole.RENDERED_STATE,
                content_bytes=rendered_bytes,
                media_type="application/json",
                source_url=authority_url,
                evidence_locator=locator,
                observed_at=NOW,
                retrieved_at=NOW,
            )
        )
    normalized = json.dumps(
        observations, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode()
    return _seal_approved_ir_observation_bundle(
        issuer_identifier=issuer,
        authority_url=authority_url,
        normalized_observations_bytes=normalized,
        artifacts=tuple(artifacts),
        captured_at=NOW,
    ).to_bytes()


def _request(
    issuer: str, *, at: datetime = NOW, recorded_by: str = "bha-16-caller"
) -> IrCandidateCallerRequest:
    return IrCandidateCallerRequest(
        issuer_identifier=issuer,
        recorded_by=recorded_by,
        recorded_at=at,
        reason="Owner-approved rolling five-quarter IR scope",
    )


def test_wix_plan_requires_year_toggle_sequence_and_emits_exact_twenty() -> None:
    payload = _wix_payload()
    plan = plan_ir_candidates(payload, _request("WIX"))

    assert plan.issuer_id == "sec-cik-0001576789"
    assert plan.ticker == "WIX"
    assert plan.candidate_count == 20
    assert plan.excluded_webcast_count == 5
    assert plan.excluded_out_of_scope_count == 0
    assert plan.sec_handoff_count == 0
    assert plan.reported_quarters == tuple(date.fromisoformat(item) for item in WIX_PERIODS)
    assert {entry.doc_type for entry in plan.candidates} == {
        DocType.IR_PRESS_RELEASE,
        DocType.IR_PRESENTATION,
        DocType.IR_INVESTOR_UPDATE,
        DocType.IR_TRANSCRIPT,
    }
    assert all("webcast" not in entry.url for entry in plan.candidates)
    assert len({entry.url for entry in plan.candidates}) == 20
    bundle_sha = hashlib.sha256(payload).hexdigest()
    assert plan.bundle_input_sha256 == bundle_sha
    for entry in plan.candidates:
        assert entry.evidence[0].content_sha256 == entry.observation_raw_sha256
        assert entry.evidence[0].locator.startswith("evidence://source-observation/")
        assert entry.evidence[1].content_sha256 == plan.catalog_sha256
        assert entry.evidence_locator in entry.evidence[0].locator


def test_rubrik_plan_emits_exact_ten_and_excludes_duplicates_and_non_scope() -> None:
    plan = plan_ir_candidates(_rubrik_payload(), _request("RBRK"))

    assert plan.candidate_count == 10
    assert plan.excluded_webcast_count == 5
    assert plan.excluded_out_of_scope_count == 5
    assert plan.sec_handoff_count == 5
    assert {entry.doc_type for entry in plan.candidates} == {
        DocType.IR_PRESS_RELEASE,
        DocType.IR_PRESENTATION,
    }
    assert len({entry.url for entry in plan.candidates}) == 10
    assert all("remarks" not in entry.url for entry in plan.candidates)


def test_hand_authored_normalized_url_without_rendered_link_proof_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _wix_payload()
    original = load_approved_ir_observation_bundle(payload)
    observations = json.loads(original.normalized_observations_bytes)
    observations[0]["panels"][0]["links"][0]["url"] = (
        "https://investors.wix.com/static-files/hand-authored-release.pdf"
    )
    normalized = json.dumps(
        observations, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode()
    forged = original.model_copy(update={"normalized_observations_bytes": normalized})

    def _load_forged(_payload: bytes) -> ApprovedIrObservationBundle:
        return forged

    monkeypatch.setattr(
        caller_module,
        "load_approved_ir_observation_bundle",
        _load_forged,
    )

    with pytest.raises(IrCandidateCallerError, match="not proven by exact rendered evidence"):
        plan_ir_candidates(payload, _request("WIX"))


def test_plan_fails_closed_when_one_approved_period_is_incomplete() -> None:
    with pytest.raises(IrCandidateCallerError, match="approved per-period document shape"):
        plan_ir_candidates(_wix_payload(omit_last_transcript=True), _request("WIX"))


def test_plan_accepts_new_source_reported_period_and_rejects_duplicate_type() -> None:
    shifted = ("2026-09-30", *WIX_PERIODS[:-1])
    plan = plan_ir_candidates(_wix_payload(periods=shifted), _request("WIX"))
    assert plan.reported_quarters == tuple(date.fromisoformat(item) for item in shifted)
    assert date.fromisoformat(WIX_PERIODS[-1]) not in plan.reported_quarters
    with pytest.raises(IrCandidateCallerError, match="approved per-period document shape"):
        plan_ir_candidates(
            _wix_payload(duplicate_first_presentation=True),
            _request("WIX"),
        )


def test_plan_requires_five_source_reported_periods() -> None:
    with pytest.raises(IrCandidateCallerError, match="five source-reported periods"):
        plan_ir_candidates(_wix_payload(periods=WIX_PERIODS[:-1]), _request("WIX"))


def test_apply_is_atomic_and_later_clock_is_exact_replay(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = migrated_db(tmp_path / "candidate-caller.db")
    payload = _wix_payload()
    blob_root = tmp_path / "blobs"
    replay_blob_root = tmp_path / "replay-blobs"
    first = apply_ir_candidate_plan(
        db_path, blob_root, payload, plan_ir_candidates(payload, _request("WIX"))
    )
    replay = apply_ir_candidate_plan(
        db_path,
        replay_blob_root,
        payload,
        plan_ir_candidates(payload, _request("WIX", at=NOW + timedelta(hours=1))),
    )

    assert (first.created, first.replayed, first.total) == (20, 0, 20)
    assert (replay.created, replay.replayed, replay.total) == (0, 20, 20)
    assert first.candidate_ids == replay.candidate_ids
    assert not replay_blob_root.exists()
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM ir_approval_candidates").fetchone() == (20,)
        assert connection.execute("SELECT COUNT(*) FROM evidence_content_blobs").fetchone() == (8,)
        assert connection.execute(
            "SELECT COUNT(*) FROM evidence_source_observations"
        ).fetchone() == (8,)
        assert connection.execute(
            "SELECT COUNT(*) FROM evidence_blob_location_observations"
        ).fetchone() == (8,)
        clocks = connection.execute(
            "SELECT DISTINCT recorded_at FROM ir_approval_candidates"
        ).fetchall()
        assert clocks == [(NOW.isoformat(),)]


def test_ir_inventory_records_exact_reviewed_period_without_guessing_other_links(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = migrated_db(tmp_path / "reviewed-period.db")
    payload = _wix_payload()
    plan = plan_ir_candidates(payload, _request("WIX"))
    apply_ir_candidate_plan(db_path, tmp_path / "candidate-blobs", payload, plan)
    release = next(item for item in plan.candidates if item.doc_type is DocType.IR_PRESS_RELEASE)
    unknown_url = "https://investors.wix.com/static-files/unreviewed-release.pdf"
    inventory = DocumentDiscoveryInventory(
        candidates=(
            CandidateDoc(
                url=release.url,
                link_text=release.title,
                filename_hint="release.pdf",
                doc_type_guess="press_release",
                year_guess=2026,
                quarter_guess=2,
                source_page="https://investors.wix.com/financials",
            ),
            CandidateDoc(
                url=unknown_url,
                link_text="Other release",
                filename_hint="other.pdf",
                doc_type_guess="press_release",
                year_guess=2026,
                quarter_guess=3,
                source_page="https://investors.wix.com/financials",
            ),
        ),
        pages=(
            CrawlPageOutcome(
                page_url="https://investors.wix.com/financials",
                outcome="succeeded",
                anchor_count=2,
                anchors=((release.url, release.title), (unknown_url, "Other release")),
            ),
        ),
        crawl_complete=True,
        crawl_stop_reason="frontier_exhausted",
    )
    stamp = NOW + timedelta(hours=1)
    request = source_inventory_request(
        issuer_id=plan.issuer_id,
        ticker=plan.ticker,
        ir_url="https://investors.wix.com/financials",
        revision=1,
        inventory=inventory,
        retrieval_config_sha256="c" * 64,
        collector_code_version="test-ir-inventory@1",
        started_at=stamp,
        completed_at=stamp,
        recorded_at=stamp,
        reconciled_at=stamp,
        apply=True,
    )
    with connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER) as connection:
        _seed_source_duties(connection, plan.issuer_id)
        sync_ir_source_inventory(connection, request, blob_root=tmp_path / "inventory-blobs")
        rows = connection.execute(
            "SELECT source_url,period_end,document_type FROM expected_documents ORDER BY source_url"
        ).fetchall()
    by_url = {str(url): (period, doc_type) for url, period, doc_type in rows}
    assert str(by_url[release.url][0]).startswith(release.quarter_end.isoformat())
    assert by_url[release.url][1] == "earnings_release"
    assert by_url[unknown_url] == (None, "ir_document")


def test_sealed_expected_rows_without_captured_period_proof_remain_pending(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = migrated_db(tmp_path / "reported-window.db")
    payload = _rubrik_payload()
    plan = plan_ir_candidates(payload, _request("RBRK"))
    apply_ir_candidate_plan(db_path, tmp_path / "candidate-blobs", payload, plan)
    releases = tuple(item for item in plan.candidates if item.doc_type is DocType.IR_PRESS_RELEASE)
    urls = tuple(item.url for item in releases)
    archive = "https://ir.rubrik.com/financials/quarterly-results/default.aspx"
    sec_url = "https://data.sec.gov/submissions/CIK0001943896.json"
    with connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER) as connection:
        _seed_source_duties(connection, plan.issuer_id)
        archive_digest = _seed_source_observation(
            connection,
            tmp_path=tmp_path,
            observation_id="synthetic-rbrk-archive",
            source_kind="ir_publisher_authority",
            source_url=archive,
            body=json.dumps({"links": urls}, sort_keys=True).encode(),
        )
        inventory = DocumentDiscoveryInventory(
            candidates=tuple(
                CandidateDoc(
                    url=item.url,
                    link_text=item.title,
                    filename_hint="release.pdf",
                    doc_type_guess="press_release",
                    year_guess=None,
                    quarter_guess=None,
                    source_page=archive,
                )
                for item in releases
            ),
            pages=(
                CrawlPageOutcome(
                    page_url=archive,
                    outcome="succeeded",
                    anchor_count=len(urls),
                    anchors=tuple((item.url, item.title) for item in releases),
                ),
            ),
            crawl_complete=True,
            crawl_stop_reason="frontier_exhausted",
        )
        authority = IRAuthorityEvidence(
            authority_basis="publisher_archive",
            asserted_at=NOW,
            surfaces=(
                PublisherSurfaceEvidence(
                    surface_key="archive",
                    surface_kind="archive",
                    source_url=archive,
                    source_observation_id="synthetic-rbrk-archive",
                    raw_sha256=archive_digest,
                    traversal_kind="pagination",
                    outcome="exhausted",
                    terminal_condition="next_link_absent",
                    observed_document_urls=urls,
                ),
            ),
        )
        ir = source_inventory_request(
            issuer_id=plan.issuer_id,
            ticker=plan.ticker,
            ir_url=archive,
            revision=1,
            inventory=inventory,
            authority=authority,
            retrieval_config_sha256="c" * 64,
            collector_code_version="synthetic-ir-inventory@1",
            started_at=NOW,
            completed_at=NOW,
            recorded_at=NOW,
            reconciled_at=NOW,
            apply=True,
        )
        result = sync_ir_source_inventory(connection, ir, blob_root=tmp_path / "inventory-blobs")
        assert result.complete
        _seed_source_observation(
            connection,
            tmp_path=tmp_path,
            observation_id="synthetic-rbrk-sec",
            source_kind="sec_submissions",
            source_url=sec_url,
            body=json.dumps({"period_ends": RBRK_PERIODS}, sort_keys=True).encode(),
        )
        sec = SourceCoverageImport(
            inventory_key=f"{plan.issuer_id}:sec-submissions",
            revision=1,
            issuer_id=plan.issuer_id,
            ticker=plan.ticker,
            source_kind="sec_submissions",
            source_url=sec_url,
            source_observation_id="synthetic-rbrk-sec",
            outcome="succeeded",
            authoritative=True,
            retrieval_config_sha256="c" * 64,
            collector_code_version="synthetic-sec-inventory@1",
            started_at=NOW,
            completed_at=NOW,
            recorded_at=NOW,
            reconciled_at=NOW,
            components=(
                InventoryComponentImport(
                    component_key="primary",
                    component_kind="primary",
                    source_url=sec_url,
                    source_observation_id="synthetic-rbrk-sec",
                    outcome="succeeded",
                    required=True,
                    ordinal=0,
                ),
            ),
            expected_documents=tuple(
                ExpectedDocumentImport(
                    expected_document_key=f"{plan.issuer_id}:sec:{period}",
                    source_kind="sec_filing",
                    document_type="filing",
                    form_type="10-K" if period.endswith("01-31") else "10-Q",
                    accession_number=f"synthetic-{period}",
                    source_url=f"https://www.sec.gov/Archives/edgar/data/1943896/{period}.htm",
                    period_end=datetime.fromisoformat(period),
                    filing_at=NOW,
                    expected_at=NOW,
                    expectation_basis="authoritative",
                    absence=ExplicitAbsence(
                        coverage_status="available",
                        reason_code="synthetic_sec_filing",
                        reason_details=(("period", period),),
                    ),
                )
                for period in RBRK_PERIODS
            ),
            apply=True,
        )
        reconcile_source_coverage(connection, sec)
        window = reported_period_window_from_inventory(connection, ticker="RBRK", as_of=NOW.date())
        assert window.status is ReportedPeriodStatus.UNKNOWN
        assert window.reason == "captured_period_proof_missing"
        assert window.period_ends == ()
        assert (
            reported_quarter_selection_in_connection(
                connection,
                ticker="RBRK",
                fiscal_year=2026,
                fiscal_quarter=1,
                fiscal_year_end_month=1,
                as_of=NOW.date(),
            )
            is None
        )
        assert (
            reported_quarter_selection_in_connection(
                connection,
                ticker="RBRK",
                fiscal_year=2027,
                fiscal_quarter=2,
                fiscal_year_end_month=1,
                as_of=NOW.date(),
            )
            is None
        )


def test_replay_rejects_changed_attribution_without_partial_writes(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = migrated_db(tmp_path / "candidate-conflict.db")
    payload = _rubrik_payload()
    blob_root = tmp_path / "blobs"
    apply_ir_candidate_plan(
        db_path, blob_root, payload, plan_ir_candidates(payload, _request("RBRK"))
    )

    with pytest.raises(IrCandidateCallerError, match="recorded_by changed"):
        apply_ir_candidate_plan(
            db_path,
            blob_root,
            payload,
            plan_ir_candidates(payload, _request("RBRK", recorded_by="different-caller")),
        )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM ir_approval_candidates").fetchone() == (10,)


@pytest.mark.parametrize("failure", ["missing", "corrupt"])
def test_replay_reopens_and_verifies_durable_evidence_bytes(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    failure: str,
) -> None:
    db_path = migrated_db(tmp_path / f"candidate-{failure}.db")
    payload = _rubrik_payload()
    blob_root = tmp_path / "blobs"
    plan = plan_ir_candidates(payload, _request("RBRK"))
    apply_ir_candidate_plan(db_path, blob_root, payload, plan)
    with sqlite3.connect(db_path) as connection:
        uri = str(
            connection.execute(
                "SELECT storage_uri FROM evidence_content_blobs ORDER BY sha256 LIMIT 1"
            ).fetchone()[0]
        )
    parsed = urllib.parse.urlsplit(uri)
    decoded = urllib.parse.unquote(parsed.path)
    if len(decoded) >= 3 and decoded[0] == "/" and decoded[2] == ":":
        decoded = decoded[1:]
    path = Path(decoded)
    if failure == "missing":
        path.unlink()
    else:
        path.write_bytes(b"corrupt")

    with pytest.raises(IrCandidateCallerError, match=failure):
        apply_ir_candidate_plan(db_path, tmp_path / "replay", payload, plan)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM ir_approval_candidates").fetchone() == (10,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_apply_rolls_back_evidence_candidates_and_new_blob_files(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = migrated_db(tmp_path / "candidate-rollback.db")
    blob_root = tmp_path / "blobs"
    payload = _rubrik_payload()
    plan = plan_ir_candidates(payload, _request("RBRK"))
    original = caller_module.persist_candidate
    calls = 0

    def _fail_second(
        connection: sqlite3.Connection,
        request: IrCandidateRequest,
    ) -> CandidateWriteResult:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("forced candidate failure")
        return original(connection, request)

    monkeypatch.setattr(caller_module, "persist_candidate", _fail_second)
    with pytest.raises(RuntimeError, match="forced candidate failure"):
        apply_ir_candidate_plan(db_path, blob_root, payload, plan)

    with sqlite3.connect(db_path) as connection:
        for table in (
            "ir_approval_candidates",
            "evidence_content_blobs",
            "evidence_source_observations",
            "evidence_blob_location_observations",
        ):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)  # nosec B608
    assert not any(blob_root.rglob("*"))
