"""Fail-closed public-browser capture for approved Wix/Rubrik observations."""

from __future__ import annotations

import base64
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import ir_pipeline.approved_ir_observation_capture as capture_module
from ir_pipeline.approved_ir_observation_capture import (
    ApprovedIrObservationAuthenticationError,
    ApprovedIrObservationCaptureError,
    ApprovedIrObservationCaptureRequest,
    ObservationArtifactRole,
    PublisherAuthorityResponse,
    ReviewedIrPeriod,
    ReviewedIrPeriodSelection,
    RubrikRenderedRowCapture,
    WixRenderedPanelCapture,
    collect_approved_ir_observations,
    import_rubrik_visible_browser_export,
    import_wix_visible_browser_export,
    load_approved_ir_observation_bundle,
    verify_reviewed_ir_period_selection,
)
from pipeline.approved_ir_rubrik import RubrikLinkObservation
from pipeline.approved_ir_wix import WixLinkObservation, WixPanelObservation
from pipeline.ir_candidate_caller import IrCandidateCallerRequest, plan_ir_candidates
from provenance.issuer_registry import (
    IssuerEntity,
    IssuerRegistry,
    LegacyIssuerBindingRevision,
)

NOW = datetime(2026, 8, 13, 12, 0, 0)
WIX_AUTHORITY = "https://investors.wix.com/financials"
RBRK_AUTHORITY = "https://ir.rubrik.com/financials/quarterly-results/default.aspx"
WIX_PERIODS = (
    date(2026, 6, 30),
    date(2026, 3, 31),
    date(2025, 12, 31),
    date(2025, 9, 30),
    date(2025, 6, 30),
)
RBRK_PERIODS = (
    date(2026, 4, 30),
    date(2026, 1, 31),
    date(2025, 10, 31),
    date(2025, 7, 31),
    date(2025, 4, 30),
)


def _visible_wix_export(
    periods: tuple[date, ...] = WIX_PERIODS,
    *,
    captured_at: str = "2026-08-13T18:55:04.498Z",
) -> dict[str, object]:
    states: list[dict[str, object]] = []
    for period in periods:
        quarter = f"{((period.month - 1) // 3) + 1}Q {period.year}"
        heading = ("First", "Second", "Third", "Fourth")[(period.month - 1) // 3]
        links = [
            {
                "title": title,
                "url": (
                    "https://4f4a3186-9467-4c09-aa74-51fe1affec20.usrfiles.com/ugd/"
                    f"{period.isoformat()}-{suffix}.pdf"
                ),
            }
            for title, suffix in (
                ("Press Release", "release"),
                ("Earnings Slides", "slides"),
                ("Shareholder Update", "update"),
                ("Transcript", "transcript"),
            )
        ]
        html = (
            '<div id="period-container">'
            + "".join(f'<a href="{item["url"]}">{item["title"]}</a>' for item in links)
            + "</div>"
        )
        states.append(
            {
                "containerHtml": html,
                "containerId": "period-container",
                "heading": f"{heading} Quarter {period.year}",
                "links": links,
                "periodEnd": period.isoformat(),
                "quarterName": quarter,
                "requestedYear": period.year,
                "selectedYear": period.year,
            }
        )
    return {
        "schema_version": "wix-visible-browser-export@1",
        "authority_url": WIX_AUTHORITY,
        "captured_at": captured_at,
        "document_title": "Wix.com Investor Relations: Events & Presentations",
        "states": states,
    }


def test_visible_wix_export_seals_only_html_proven_links() -> None:
    bundle = import_wix_visible_browser_export(json.dumps(_visible_wix_export()).encode())

    assert bundle.issuer_identifier == "sec-cik-0001576789"
    assert len(bundle.artifacts) == 6
    assert len(load_approved_ir_observation_bundle(bundle.to_bytes()).artifacts) == 6


def test_visible_wix_export_rejects_hand_authored_link_overlay() -> None:
    exported = _visible_wix_export()
    states = exported["states"]
    assert isinstance(states, list)
    first_state = cast("dict[str, object]", states[0])
    links = cast("list[dict[str, object]]", first_state["links"])
    links[0]["url"] = "https://evil.example/forged.pdf"

    with pytest.raises(
        ApprovedIrObservationCaptureError,
        match="not exactly proven by container HTML",
    ):
        import_wix_visible_browser_export(json.dumps(exported).encode())


def test_visible_wix_export_rejects_hidden_declared_anchor() -> None:
    exported = _visible_wix_export()
    states = exported["states"]
    assert isinstance(states, list)
    state = cast("dict[str, object]", states[0])
    state["containerHtml"] = str(state["containerHtml"]).replace(
        "<a href=", '<a aria-hidden="true" href=', 1
    )

    with pytest.raises(
        ApprovedIrObservationCaptureError,
        match="not exactly proven by container HTML",
    ):
        import_wix_visible_browser_export(json.dumps(exported).encode())


def _visible_rubrik_export(
    periods: tuple[date, ...] = RBRK_PERIODS,
    *,
    captured_at: str = "2026-08-13T19:07:27.934Z",
) -> dict[str, object]:
    states: list[dict[str, object]] = []
    for period in periods:
        label = {
            date(2026, 7, 31): "Q2 2027",
            date(2026, 4, 30): "Q1 2027",
            date(2026, 1, 31): "Q4 2026",
            date(2025, 10, 31): "Q3 2026",
            date(2025, 7, 31): "Q2 2026",
            date(2025, 4, 30): "Q1 2026",
        }[period]
        links = [
            {
                "text": "News(opens in new window)",
                "href": f"https://s203.q4cdn.com/667520861/files/{period}-release.pdf",
                "ariaLabel": None,
            },
            {
                "text": "Presentation(opens in new window)",
                "href": f"https://s203.q4cdn.com/667520861/files/{period}-presentation.pdf",
                "ariaLabel": None,
            },
        ]
        if period != date(2025, 4, 30):
            links.extend(
                [
                    {
                        "text": "Webcast(opens in new window)",
                        "href": f"https://events.q4inc.com/{period}",
                        "ariaLabel": None,
                    },
                    {
                        "text": "Infographic(opens in new window)",
                        "href": f"https://s203.q4cdn.com/667520861/files/{period}.png",
                        "ariaLabel": None,
                    },
                    {
                        "text": "Quarterly Filing(opens in new window)",
                        "href": f"https://s203.q4cdn.com/667520861/files/{period}-filing.pdf",
                        "ariaLabel": None,
                    },
                ]
            )
        html = (
            "<div>"
            + "".join(
                (
                    f'<a aria-hidden="false" href="{item["href"]}"'
                    + (f' aria-label="{item["ariaLabel"]}"' if item["ariaLabel"] else "")
                    + f">{item['text']}</a>"
                )
                for item in links
            )
            + "</div>"
        )
        states.append(
            {
                "periodEnd": period.isoformat(),
                "selectedLabel": label,
                "tabOuterHtml": (
                    None
                    if period == date(2025, 4, 30)
                    else f'<button role="tab" aria-selected="true">{label}</button>'
                ),
                "containerHtml": html,
                "links": links,
            }
        )
    return {
        "schema": "rubrik-visible-browser-export@1",
        "issuerIdentifier": "RBRK",
        "authorityUrl": RBRK_AUTHORITY,
        "documentTitle": "Rubrik - Financials - Quarterly Results",
        "capturedAt": captured_at,
        "states": states,
    }


def test_visible_rubrik_export_plans_exact_ten_and_preserves_exclusions() -> None:
    bundle = import_rubrik_visible_browser_export(json.dumps(_visible_rubrik_export()).encode())
    plan = plan_ir_candidates(
        bundle.to_bytes(),
        IrCandidateCallerRequest(
            issuer_identifier="RBRK",
            recorded_by="test",
            recorded_at=NOW,
            reason="approved visible evidence",
        ),
    )

    assert len(plan.candidates) == 10
    assert plan.excluded_webcast_count == 4
    assert plan.excluded_out_of_scope_count == 0


def _reviewed_selection(
    issuer: str, periods: tuple[date, ...], *, cutoff_at: datetime | None = None
) -> ReviewedIrPeriodSelection:
    source_prefix = (
        "https://investors.wix.com/static-files/"
        if issuer == "WIX"
        else "https://s203.q4cdn.com/667520861/files/"
    )
    return ReviewedIrPeriodSelection(
        issuer_identifier="sec-cik-0001576789" if issuer == "WIX" else "sec-cik-0001943896",
        cutoff_at=cutoff_at or datetime(2026, 10, 24, tzinfo=UTC),
        periods=tuple(
            ReviewedIrPeriod(
                document_version_id=f"document-{period.isoformat()}",
                source_sha256=f"{index + 1:064x}",
                source_url=f"{source_prefix}{period.isoformat()}-release.pdf",
                period_end=period,
            )
            for index, period in enumerate(periods)
        ),
    )


def _stub_native_reviews(
    monkeypatch: pytest.MonkeyPatch,
    selection: ReviewedIrPeriodSelection,
    *,
    stub_registry: bool = True,
    reviewed_at: datetime | None = None,
) -> None:
    issuer = selection.issuer_identifier
    ticker = "WIX" if issuer == "sec-cik-0001576789" else "RBRK"
    if stub_registry:

        class _Registry:
            def __init__(self, _conn: sqlite3.Connection) -> None:
                pass

            def canonicalize_recorded_issuer(
                self, recorded_issuer_id: str, *, knowledge_at: datetime
            ) -> SimpleNamespace:
                expected = (
                    "sec-cik-0001576789"
                    if recorded_issuer_id == "legacy-ticker:WIX"
                    else "sec-cik-0001943896"
                    if recorded_issuer_id == "legacy-ticker:RBRK"
                    else recorded_issuer_id
                )
                return SimpleNamespace(issuer_id=expected, material_dissent=False)

        monkeypatch.setattr(capture_module, "IssuerRegistry", _Registry)
    by_id: dict[str, SimpleNamespace] = {}
    for item in selection.periods:
        review_time = reviewed_at or datetime.combine(
            max(item.period_end + timedelta(days=1), date(2026, 8, 13)),
            datetime.min.time(),
            tzinfo=UTC,
        )
        by_id[item.document_version_id] = SimpleNamespace(
            issuer_id=issuer,
            ticker=ticker,
            reviewed_at=review_time,
            knowledge_cutoff=review_time,
            observed_through=review_time,
        )
    by_url: dict[str, SimpleNamespace] = {
        item.source_url: by_id[item.document_version_id] for item in selection.periods
    }

    def verified(
        _conn: sqlite3.Connection,
        *,
        document_version_id: str,
        source_sha256: str,
        period_end: date,
    ) -> SimpleNamespace | None:
        item = next(
            (part for part in selection.periods if part.document_version_id == document_version_id),
            None,
        )
        if item is None or (source_sha256, period_end) != (
            item.source_sha256,
            item.period_end,
        ):
            return None
        return by_id[document_version_id]

    def current(
        _conn: sqlite3.Connection,
        *,
        issuer_id: str,
        ticker: str,
        source_url: str,
        observed_through: datetime,
    ) -> SimpleNamespace | None:
        if (issuer_id, ticker) != (issuer, "WIX" if issuer == "sec-cik-0001576789" else "RBRK"):
            return None
        return by_url.get(source_url)

    monkeypatch.setattr(capture_module, "verified_current_reported_period_review", verified)
    monkeypatch.setattr(capture_module, "reviewed_current_period_for_url", current)


def test_reviewed_period_selection_reconstructs_only_current_source_reviews(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    periods = (date(2026, 7, 31), *RBRK_PERIODS[:-1])
    selection = _reviewed_selection("RBRK", periods)
    _stub_native_reviews(monkeypatch, selection)
    with sqlite3.connect(":memory:") as conn:
        assert (
            verify_reviewed_ir_period_selection(conn, selection, issuer_identifier="RBRK")
            == periods
        )
        with pytest.raises(ApprovedIrObservationCaptureError, match="issuer"):
            verify_reviewed_ir_period_selection(conn, selection, issuer_identifier="WIX")
        changed = selection.model_copy(
            update={
                "periods": (
                    selection.periods[0].model_copy(update={"source_sha256": "f" * 64}),
                    *selection.periods[1:],
                )
            }
        )
        with pytest.raises(ApprovedIrObservationCaptureError, match="missing, stale"):
            verify_reviewed_ir_period_selection(conn, changed, issuer_identifier="RBRK")
        stale = selection.model_copy(update={"cutoff_at": datetime(2026, 8, 12, tzinfo=UTC)})
        with pytest.raises(ApprovedIrObservationCaptureError, match="missing, stale"):
            verify_reviewed_ir_period_selection(conn, stale, issuer_identifier="RBRK")

        def _stale(*_args: object, **_kwargs: object) -> None:
            return None

        monkeypatch.setattr(capture_module, "reviewed_current_period_for_url", _stale)
        with pytest.raises(ApprovedIrObservationCaptureError, match="missing, stale"):
            verify_reviewed_ir_period_selection(conn, selection, issuer_identifier="RBRK")
    assert conn.total_changes == 0


def test_reviewed_period_clock_rejects_future_cutoff_period_and_preperiod_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    periods = (date(2026, 9, 30), *WIX_PERIODS[:-1])
    with pytest.raises(ValueError, match="after the selection cutoff"):
        _reviewed_selection("WIX", periods, cutoff_at=datetime(2026, 8, 13, tzinfo=UTC))
    selection = _reviewed_selection("WIX", periods)
    with sqlite3.connect(":memory:") as conn:
        future = selection.model_copy(update={"cutoff_at": datetime.now(UTC) + timedelta(days=1)})
        with pytest.raises(ApprovedIrObservationCaptureError, match="cutoff is in the future"):
            verify_reviewed_ir_period_selection(conn, future, issuer_identifier="WIX")
        assert not conn.in_transaction
        _stub_native_reviews(monkeypatch, selection, reviewed_at=datetime(2026, 8, 13, tzinfo=UTC))
        with pytest.raises(ApprovedIrObservationCaptureError, match="missing, stale"):
            verify_reviewed_ir_period_selection(conn, selection, issuer_identifier="WIX")
    assert conn.total_changes == 0


def test_reviewed_period_import_rejects_export_before_review_and_future_capture() -> None:
    periods = (date(2026, 9, 30), *WIX_PERIODS[:-1])
    selection = _reviewed_selection("WIX", periods, cutoff_at=datetime(2026, 10, 1, 18, tzinfo=UTC))
    with sqlite3.connect(":memory:") as conn:
        early = json.dumps(
            _visible_wix_export(periods, captured_at="2026-10-01T01:00:00Z")
        ).encode()
        with pytest.raises(ApprovedIrObservationCaptureError, match="must follow selection cutoff"):
            import_wix_visible_browser_export(
                early, reviewed_selection=selection, source_connection=conn
            )
        assert not conn.in_transaction
        future = json.dumps(
            _visible_wix_export(periods, captured_at="2027-01-01T01:00:00Z")
        ).encode()
        with pytest.raises(ApprovedIrObservationCaptureError, match="precede current time"):
            import_wix_visible_browser_export(
                future, reviewed_selection=selection, source_connection=conn
            )
        assert not conn.in_transaction
        with pytest.raises(ApprovedIrObservationCaptureError, match="must follow selection cutoff"):
            verify_reviewed_ir_period_selection(
                conn,
                selection,
                issuer_identifier="WIX",
                capture_at=datetime(2026, 10, 1, 1, tzinfo=UTC),
            )
    assert conn.total_changes == 0


def test_reviewed_period_selection_uses_canonical_issuer_binding(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = migrated_db(tmp_path / "reviewed-selection.db")
    with sqlite3.connect(path) as conn:
        registry = IssuerRegistry(conn)
        asserted_at = datetime(2026, 8, 13, tzinfo=UTC)
        registry.persist(
            IssuerEntity(
                issuer_id="issuer-rbrk-canonical",
                idempotency_key="issuer-rbrk-canonical",
                entity_kind="operating_company",
                created_at=asserted_at,
            )
        )
        registry.persist(
            LegacyIssuerBindingRevision(
                binding_revision_id="legacy-rbrk-selection",
                idempotency_key="legacy-rbrk-selection",
                recorded_issuer_id="legacy-ticker:RBRK",
                revision=1,
                issuer_id="issuer-rbrk-canonical",
                outcome="selected",
                decision_kind="manual",
                reason_code="verified_identity_bridge",
                reason_details=(("legacy_ticker", "RBRK"),),
                material_dissent=False,
                effective_at=asserted_at,
                knowledge_at=asserted_at,
                recorded_at=asserted_at,
            )
        )
        conn.commit()
        periods = (date(2026, 7, 31), *RBRK_PERIODS[:-1])
        selection = _reviewed_selection("RBRK", periods).model_copy(
            update={"issuer_identifier": "issuer-rbrk-canonical"}
        )
        _stub_native_reviews(monkeypatch, selection, stub_registry=False)
        assert (
            verify_reviewed_ir_period_selection(conn, selection, issuer_identifier="RBRK")
            == periods
        )
        legacy_selection = selection.model_copy(update={"issuer_identifier": "sec-cik-0001943896"})
        with pytest.raises(ApprovedIrObservationCaptureError, match="approved canonical binding"):
            verify_reviewed_ir_period_selection(conn, legacy_selection, issuer_identifier="RBRK")
        registry.persist(
            LegacyIssuerBindingRevision(
                binding_revision_id="legacy-rbrk-sec-alias",
                idempotency_key="legacy-rbrk-sec-alias",
                recorded_issuer_id="sec-cik-0001943896",
                revision=1,
                issuer_id="issuer-rbrk-canonical",
                outcome="selected",
                decision_kind="manual",
                reason_code="verified_identity_bridge",
                reason_details=(("legacy_ticker", "RBRK"),),
                material_dissent=False,
                effective_at=asserted_at,
                knowledge_at=asserted_at,
                recorded_at=asserted_at,
            )
        )
        conn.commit()
        assert (
            verify_reviewed_ir_period_selection(conn, legacy_selection, issuer_identifier="RBRK")
            == periods
        )


def test_shifted_visible_exports_require_exact_reviewed_selection_and_keep_legacy_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wix_periods = (date(2026, 9, 30), *WIX_PERIODS[:-1])
    rubrik_periods = (date(2026, 7, 31), *RBRK_PERIODS[:-1])
    with sqlite3.connect(":memory:") as conn:
        for issuer, periods, visible_export, importer in (
            ("WIX", wix_periods, _visible_wix_export, import_wix_visible_browser_export),
            ("RBRK", rubrik_periods, _visible_rubrik_export, import_rubrik_visible_browser_export),
        ):
            selection = _reviewed_selection(issuer, periods)
            _stub_native_reviews(monkeypatch, selection)
            export = json.dumps(
                visible_export(periods, captured_at="2026-10-24T01:00:00Z")
            ).encode()
            bundle = importer(export, reviewed_selection=selection, source_connection=conn)
            assert len(load_approved_ir_observation_bundle(bundle.to_bytes()).artifacts) == 6
            with pytest.raises(
                ApprovedIrObservationCaptureError, match="exact approved period set"
            ):
                importer(export)
            wrong = selection.model_copy(
                update={
                    "periods": (
                        selection.periods[0].model_copy(
                            update={
                                "period_end": selection.periods[0].period_end + timedelta(days=1)
                            }
                        ),
                        *selection.periods[1:],
                    )
                }
            )
            with pytest.raises(ApprovedIrObservationCaptureError, match="missing, stale"):
                importer(export, reviewed_selection=wrong, source_connection=conn)
        for issuer, periods, visible_export, importer in (
            ("WIX", WIX_PERIODS, _visible_wix_export, import_wix_visible_browser_export),
            ("RBRK", RBRK_PERIODS, _visible_rubrik_export, import_rubrik_visible_browser_export),
        ):
            export = json.dumps(visible_export(periods)).encode()
            selection = _reviewed_selection(
                issuer, periods, cutoff_at=datetime(2026, 8, 13, 18, tzinfo=UTC)
            )
            _stub_native_reviews(monkeypatch, selection)
            assert (
                importer(export).to_bytes()
                == importer(export, reviewed_selection=selection, source_connection=conn).to_bytes()
            )


@pytest.mark.parametrize("mutation", ["forged", "hidden", "duplicate"])
def test_visible_rubrik_export_rejects_unproven_or_duplicate_links(mutation: str) -> None:
    exported = _visible_rubrik_export()
    states = cast("list[dict[str, object]]", exported["states"])
    first = states[0]
    links = cast("list[dict[str, object]]", first["links"])
    if mutation == "forged":
        links[0]["href"] = "https://evil.example/forged.pdf"
    elif mutation == "hidden":
        first["containerHtml"] = str(first["containerHtml"]).replace(
            'aria-hidden="false"', 'aria-hidden="true"', 1
        )
    else:
        links.append(dict(links[0]))

    with pytest.raises(ApprovedIrObservationCaptureError):
        import_rubrik_visible_browser_export(json.dumps(exported).encode())


def _wix_links(period: date) -> tuple[WixLinkObservation, ...]:
    return tuple(
        WixLinkObservation(
            title=title,
            url=f"https://investors.wix.com/static-files/{period.isoformat()}-{suffix}",
            declared_kind=kind,
            evidence_locator=f"panel[{period.isoformat()}] > link[{kind}]",
        )
        for kind, title, suffix in (
            ("earnings-release", "Press release", "release.pdf"),
            ("presentation", "Earnings slides", "slides.pdf"),
            ("investor-update", "Shareholder update", "update.pdf"),
            ("transcript", "Text transcript", "transcript.txt"),
        )
    )


class _FakeBrowser:
    def __init__(self) -> None:
        self.authority_status = 200
        self.final_url: str | None = None
        self.selected_year_override: int | None = None
        self.hidden_panel_has_links = False
        self.path_drift = False

    def fetch_authority(
        self,
        authority_url: str,
        *,
        user_agent: str,
        timeout_ms: int,
    ) -> PublisherAuthorityResponse:
        assert user_agent and timeout_ms > 0
        return PublisherAuthorityResponse(
            requested_url=authority_url,
            final_url=self.final_url or authority_url,
            status_code=self.authority_status,
            media_type="text/html; charset=utf-8",
            content_bytes=f"<html>{authority_url}</html>".encode(),
        )

    def capture_wix_year(
        self,
        authority_url: str,
        *,
        requested_year: int,
        requested_quarter_ends: tuple[date, ...],
        user_agent: str,
        timeout_ms: int,
    ) -> tuple[WixRenderedPanelCapture, ...]:
        del user_agent, timeout_ms
        captures: list[WixRenderedPanelCapture] = []
        for period in requested_quarter_ends:
            selected = WixPanelObservation(
                panel_locator=f"quarterly-results > panel[{period.isoformat()}]",
                quarter_end=period,
                selected=True,
                visible=True,
                links=_wix_links(period),
            )
            hidden = WixPanelObservation(
                panel_locator="quarterly-results > panel[hidden-stale]",
                quarter_end=date(2023, 3, 31),
                selected=False,
                visible=False,
                links=_wix_links(date(2023, 3, 31)) if self.hidden_panel_has_links else (),
            )
            rendered = _proof_bytes(authority_url, selected.links)
            captures.append(
                WixRenderedPanelCapture(
                    observation_key=f"wix-{period.isoformat()}",
                    authority_url=authority_url,
                    requested_year=requested_year,
                    selected_year=self.selected_year_override or requested_year,
                    year_control_locator="quarterly-results > button[aria-haspopup=listbox]",
                    requested_quarter_end=period,
                    panels=(selected, hidden),
                    rendered_state_bytes=rendered,
                    evidence_locator=selected.panel_locator,
                )
            )
        return tuple(captures)

    def capture_rubrik_rows(
        self,
        authority_url: str,
        *,
        requested_quarter_ends: tuple[date, ...],
        user_agent: str,
        timeout_ms: int,
    ) -> tuple[RubrikRenderedRowCapture, ...]:
        del user_agent, timeout_ms
        captures: list[RubrikRenderedRowCapture] = []
        for period in requested_quarter_ends:
            release_url = f"https://ir.rubrik.com/static-files/{period.isoformat()}-release.pdf"
            if self.path_drift:
                release_url = f"https://evil.example/{period.isoformat()}-release.pdf"
            links = (
                RubrikLinkObservation(
                    title="Earnings release",
                    url=release_url,
                    declared_kind="earnings-release",
                    evidence_locator=f"row[{period.isoformat()}] > link[release]",
                ),
                RubrikLinkObservation(
                    title="Investor presentation",
                    url=(
                        "https://s203.q4cdn.com/667520861/files/"
                        f"doc_presentation/{period.isoformat()}/presentation.pdf"
                    ),
                    declared_kind="presentation",
                    evidence_locator=f"row[{period.isoformat()}] > link[presentation]",
                ),
            )
            captures.append(
                RubrikRenderedRowCapture(
                    observation_key=f"rbrk-{period.isoformat()}",
                    authority_url=authority_url,
                    quarter_end=period,
                    row_locator=f"quarterly-results > row[{period.isoformat()}]",
                    links=links,
                    rendered_state_bytes=_proof_bytes(authority_url, links),
                )
            )
        return tuple(captures)


def _proof_bytes(
    authority_url: str,
    links: tuple[WixLinkObservation | RubrikLinkObservation, ...],
) -> bytes:
    return json.dumps(
        {
            "document_title": "Approved IR fixture",
            "page_url": authority_url,
            "schema_version": "approved-ir-rendered-link-proof@1",
            "visible_state": {"links": [item.model_dump(mode="json") for item in links]},
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _request(issuer: str) -> ApprovedIrObservationCaptureRequest:
    return ApprovedIrObservationCaptureRequest(
        issuer_identifier=issuer,
        requested_quarter_ends=WIX_PERIODS if issuer == "WIX" else RBRK_PERIODS,
        captured_at=NOW,
        user_agent="earnings-summary-approved-ir/1.0 contact@example.test",
    )


def test_wix_capture_selects_both_years_and_seals_exact_rendered_states() -> None:
    bundle = collect_approved_ir_observations(
        _request("WIX"),
        browser=_FakeBrowser(),
        robots_allows=lambda _url, _ua: True,
    )
    wire = bundle.to_bytes()
    replay = load_approved_ir_observation_bundle(wire)

    assert replay == bundle
    assert wire == replay.to_bytes()
    assert bundle.authority_url == WIX_AUTHORITY
    observations = json.loads(bundle.normalized_observations_bytes)
    assert [item["requested_year"] for item in observations] == [2026, 2026, 2025, 2025, 2025]
    assert all(item["requested_year"] == item["selected_year"] for item in observations)
    assert all(item["year_control_locator"] for item in observations)
    rendered = tuple(
        item for item in bundle.artifacts if item.role is ObservationArtifactRole.RENDERED_STATE
    )
    assert len(rendered) == 5
    assert {item.observation_key for item in rendered} == {
        item["observation_key"] for item in observations
    }
    assert {item.sha256 for item in rendered} == {item["raw_sha256"] for item in observations}
    assert (
        len(
            [
                item
                for item in bundle.artifacts
                if item.role is ObservationArtifactRole.AUTHORITY_RAW
            ]
        )
        == 1
    )


def test_rubrik_capture_seals_structured_visible_rows() -> None:
    bundle = collect_approved_ir_observations(
        _request("RBRK"),
        browser=_FakeBrowser(),
        robots_allows=lambda _url, _ua: True,
    )
    observations = json.loads(bundle.normalized_observations_bytes)

    assert bundle.authority_url == RBRK_AUTHORITY
    assert [item["quarter_end"] for item in observations] == [
        item.isoformat() for item in RBRK_PERIODS
    ]
    assert all(item["row_locator"].startswith("quarterly-results > row") for item in observations)
    assert len(bundle.artifacts) == 6


def test_wix_year_mismatch_and_hidden_anchor_fail_closed() -> None:
    mismatch = _FakeBrowser()
    mismatch.selected_year_override = 2024
    with pytest.raises(ApprovedIrObservationCaptureError, match="selected year"):
        collect_approved_ir_observations(
            _request("WIX"), browser=mismatch, robots_allows=lambda _url, _ua: True
        )

    hidden = _FakeBrowser()
    hidden.hidden_panel_has_links = True
    with pytest.raises(ApprovedIrObservationCaptureError, match="hidden Wix panel"):
        collect_approved_ir_observations(
            _request("WIX"), browser=hidden, robots_allows=lambda _url, _ua: True
        )


@pytest.mark.parametrize("status", [401, 403])
def test_authentication_response_is_a_hard_stop(status: int) -> None:
    browser = _FakeBrowser()
    browser.authority_status = status
    with pytest.raises(ApprovedIrObservationAuthenticationError, match="authentication"):
        collect_approved_ir_observations(
            _request("RBRK"), browser=browser, robots_allows=lambda _url, _ua: True
        )


def test_robots_authority_redirect_and_candidate_path_drift_fail_closed() -> None:
    with pytest.raises(ApprovedIrObservationCaptureError, match="robots"):
        collect_approved_ir_observations(
            _request("WIX"), browser=_FakeBrowser(), robots_allows=lambda _url, _ua: False
        )

    redirect = _FakeBrowser()
    redirect.final_url = "https://investors.wix.com/financials/redirected"
    with pytest.raises(ApprovedIrObservationCaptureError, match="redirect"):
        collect_approved_ir_observations(
            _request("WIX"), browser=redirect, robots_allows=lambda _url, _ua: True
        )

    drift = _FakeBrowser()
    drift.path_drift = True
    with pytest.raises(ApprovedIrObservationCaptureError, match="escaped exact endpoint policy"):
        collect_approved_ir_observations(
            _request("RBRK"), browser=drift, robots_allows=lambda _url, _ua: True
        )


def test_bundle_loader_rejects_tampering_and_noncanonical_wire() -> None:
    bundle = collect_approved_ir_observations(
        _request("WIX"),
        browser=_FakeBrowser(),
        robots_allows=lambda _url, _ua: True,
    )
    decoded = json.loads(bundle.to_bytes())
    artifact = decoded["artifacts"][1]
    artifact["content_bytes"] = base64.b64encode(b"tampered").decode("ascii")
    tampered = json.dumps(decoded, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(ApprovedIrObservationCaptureError, match=r"digest|hash|byte size"):
        load_approved_ir_observation_bundle(tampered)

    with pytest.raises(ApprovedIrObservationCaptureError, match="canonical"):
        load_approved_ir_observation_bundle(b" " + bundle.to_bytes())
