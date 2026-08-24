"""Portfolio-only hydration contract for the Work OS prototype."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pytest

from integrations.portfolio_allocation import (
    PortfolioAllocationBucket,
    PortfolioAllocationBuckets,
    PortfolioAllocationProjection,
    PortfolioAllocationReconciliation,
    unavailable_portfolio_allocation,
)
from integrations.portfolio_offline_snapshot import read_configured_offline_portfolio_snapshot
from integrations.portfolio_tracker_client import LivePortfolio, LivePosition
from pipeline.dashboard_status import DashboardRow, TranscriptStatus
from pipeline.research_cockpit import CockpitRow, PendingAlertRef
from pipeline.work_os_earnings import EarningsReadoutSummary
from pipeline.work_os_portfolio import build_work_os_portfolio
from portfolio_risk_snapshot_store import RiskSnapshot


def _available_allocation(
    *,
    state: Literal["available", "incomplete"] = "available",
    as_of: date | None = None,
) -> PortfolioAllocationProjection:
    empty = PortfolioAllocationBucket(value=Decimal(0), weight_pct=Decimal(0))
    return PortfolioAllocationProjection(
        state=state,
        source_identity="portfolio_tracker_api_v1",
        as_of=as_of,
        currency="USD",
        buckets=PortfolioAllocationBuckets(
            us_equity=empty,
            international_equity=empty,
            us_etf=empty,
            international_etf=empty,
            cash=empty,
            unclassified=empty,
        ),
        reconciliation=PortfolioAllocationReconciliation(
            position_total=Decimal(0),
            bucket_total=Decimal(0),
            difference=Decimal(0),
            is_reconciled=True,
        ),
        reason_codes=("portfolio_allocation_incomplete",) if state == "incomplete" else (),
    )


def _row(
    ticker: str,
    *,
    name: str,
    breach_status: str = "intact",
    pending_alerts: int = 0,
    pending_tier1_alerts: int = 0,
    pending_alert_refs: tuple[PendingAlertRef, ...] = (),
    new_docs: int = 0,
    last_transcript: TranscriptStatus | None = None,
) -> CockpitRow:
    return CockpitRow(
        base=DashboardRow(
            ticker=ticker,
            list_type="portfolio",
            fmp_last_pulled="2026-08-08T10:00:00+00:00",
            last_transcript=last_transcript,
            last_build_at="2026-08-08T09:00:00+00:00",
            open_comments_count=0,
            breach_status=breach_status,
        ),
        name=name,
        price=14.25,
        day_move_pct=1.5,
        fair_value=18.50,
        fv_gap_pct=-23.0,
        next_earnings="2026-08-20",
        pending_alerts=pending_alerts,
        pending_tier1_alerts=pending_tier1_alerts,
        pending_alert_refs=pending_alert_refs,
        new_docs=new_docs,
    )


def test_portfolio_hydration_keeps_only_research_portfolio_companies() -> None:
    live = LivePortfolio(
        available=True,
        api_url="http://tracker.test",
        total_market_value=1_000_000.0,
        as_of="2026-08-08",
        positions=[
            LivePosition("NU", "Nubank", 10.0, 125_000.0, 90_000.0, 35_000.0, 12.5),
            LivePosition("MELI", "MercadoLibre", 1.0, 50_000.0, 40_000.0, 10_000.0, 5.0),
        ],
    )

    payload = build_work_os_portfolio(
        [_row("NU", name="Nu Holdings")],
        live,
        _available_allocation(as_of=date(2026, 8, 8)),
        latest_readouts={
            "NU": EarningsReadoutSummary(
                artifact_id=44,
                ticker="NU",
                fiscal_period="2026-06-30",
                period_label="Q2 · Jun 2026",
                generated_at="2026-08-14T11:44:51Z",
                route="/api/peek/earnings-readout?ticker=NU&artifact_id=44",
            )
        },
        generated_at=datetime(2026, 8, 8, 12, tzinfo=UTC),
    )

    assert payload.status == "ok"
    assert payload.tracker_state == "current"
    assert payload.tracker_detail == "Tracker connected · current · As of 2026-08-08"
    assert payload.total_market_value == 1_000_000.0
    assert payload.as_of == "2026-08-08"
    assert [company.ticker for company in payload.companies] == ["NU"]
    company = payload.companies[0]
    assert company.name == "Nu Holdings"
    assert company.current_weight_pct == 12.5
    assert company.market_value == 125_000.0
    assert company.report_url == "/reports/NU"
    assert company.latest_earnings_readout is not None
    assert company.latest_earnings_readout.period_label == "Q2 · Jun 2026"
    assert company.earnings_route == "/api/peek/earnings-readout?ticker=NU&artifact_id=44"
    assert company.earnings_label == "Q2 · Jun 2026 readout →"
    assert payload.asset_class_split.availability == "unavailable"
    assert payload.asset_class_split.source == "instrument_registry"
    assert payload.asset_class_split.unclassified_weight_pct == 17.5


def test_portfolio_hydration_projects_only_provenanced_risk_snapshot_metrics() -> None:
    payload = build_work_os_portfolio(
        [_row("NU", name="Nu Holdings")],
        LivePortfolio(available=True, api_url="http://tracker.test"),
        _available_allocation(),
        risk_snapshot=RiskSnapshot(
            captured_at="2026-08-08T11:30:00",
            metric_version="v1",
            rebase_basis="observed",
            window_start="2025-08-08",
            window_end="2026-08-08",
            benchmark="SPY",
            beta=1.03,
            sharpe=1.41,
            tracking_error_annualized=0.038,
            max_drawdown_pct=-9.6,
        ),
    )

    risk = payload.risk_metric_summary
    assert risk.availability == "available"
    assert risk.source == "portfolio_risk_snapshot"
    assert risk.captured_at == "2026-08-08T11:30:00"
    assert risk.metric_version == "v1"
    assert risk.rebase_basis == "observed"
    assert risk.portfolio_beta == 1.03
    assert risk.sharpe_ratio == 1.41
    assert risk.tracking_error_annualized == 0.038
    assert risk.max_drawdown_pct == -9.6


def test_portfolio_hydration_marks_missing_risk_snapshot_unavailable() -> None:
    payload = build_work_os_portfolio(
        [_row("NU", name="Nu Holdings")],
        LivePortfolio(available=True, api_url="http://tracker.test"),
        _available_allocation(),
    )

    risk = payload.risk_metric_summary
    assert risk.availability == "unavailable"
    assert risk.source == "portfolio_risk_snapshot"
    assert risk.portfolio_beta is None
    assert risk.sharpe_ratio is None


def test_portfolio_hydration_surfaces_readout_projection_failure() -> None:
    payload = build_work_os_portfolio(
        [_row("NU", name="Nu Holdings")],
        LivePortfolio(available=True, api_url="http://tracker.test"),
        _available_allocation(),
        readout_warnings=["earnings_readout_projection_unavailable"],
    )

    assert payload.status == "degraded"
    assert payload.tracker_state == "current"
    assert "earnings_readout_projection_unavailable" in payload.warnings


@pytest.mark.parametrize(
    ("is_stale", "is_partial", "as_of", "expected_state", "expected_detail"),
    (
        (
            False,
            False,
            None,
            "current",
            "Live tracker connected · current · observation date unavailable",
        ),
        (True, False, "2026-08-07", "stale", "Tracker connected · stale · As of 2026-08-07"),
        (False, True, "2026-08-08", "partial", "Tracker connected · partial · As of 2026-08-08"),
    ),
)
def test_portfolio_hydration_labels_tracker_state_independently_of_as_of(
    is_stale: bool,
    is_partial: bool,
    as_of: str | None,
    expected_state: str,
    expected_detail: str,
) -> None:
    payload = build_work_os_portfolio(
        [_row("NU", name="Nu Holdings")],
        LivePortfolio(
            available=True,
            api_url="http://tracker.test",
            is_stale=is_stale,
            is_partial=is_partial,
            as_of=as_of,
        ),
        _available_allocation(as_of=date.fromisoformat(as_of) if as_of else None),
    )

    assert payload.tracker_state == expected_state
    assert payload.tracker_detail == expected_detail


def test_portfolio_hydration_keeps_generation_doorway_without_persisted_readout() -> None:
    payload = build_work_os_portfolio(
        [
            _row(
                "NU",
                name="Nu Holdings",
                last_transcript=TranscriptStatus(
                    period_end="2026-06-30",
                    has_qa_section=True,
                    call_date="2026-08-12",
                ),
            )
        ],
        LivePortfolio(available=True, api_url="http://tracker.test"),
        _available_allocation(),
    )

    company = payload.companies[0]
    assert company.latest_earnings_readout is None
    assert company.earnings_route == "/api/peek/earnings-readout?ticker=NU"
    assert company.earnings_label == "Generate readout →"


def test_portfolio_hydration_fails_closed_when_tracker_is_offline() -> None:
    payload = build_work_os_portfolio(
        [_row("BKNG", name="Booking Holdings")],
        LivePortfolio(
            available=False,
            api_url="http://tracker.test",
            error="connection refused with internal detail",
        ),
        _available_allocation(),
    )

    assert payload.status == "degraded"
    assert payload.tracker_state == "unavailable"
    assert payload.tracker_detail == "Tracker unavailable · research data only"
    assert payload.total_market_value is None
    assert payload.warnings == ["portfolio_tracker_unavailable"]
    assert payload.companies[0].ticker == "BKNG"
    assert payload.companies[0].current_weight_pct is None
    assert "internal detail" not in payload.model_dump_json()


def _write_governed_snapshot(path: Path) -> None:
    fixtures = Path(__file__).parent / "fixtures" / "tracker_v1"
    path.write_text(
        json.dumps(
            {
                "source_identity": "test-governed-local-snapshot",
                "health": json.loads((fixtures / "health.json").read_text(encoding="utf-8")),
                "portfolio_snapshot": json.loads(
                    (fixtures / "portfolio-snapshot.json").read_text(encoding="utf-8")
                ),
            }
        ),
        encoding="utf-8",
    )


def test_portfolio_hydration_uses_valid_offline_snapshot_only_after_live_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot_path = tmp_path / "governed-tracker-snapshot.json"
    _write_governed_snapshot(snapshot_path)
    before = snapshot_path.read_bytes()
    monkeypatch.setenv("PORTFOLIO_TRACKER_SNAPSHOT_PATH", str(snapshot_path))

    snapshot = read_configured_offline_portfolio_snapshot()
    assert snapshot is not None
    assert snapshot.as_of == "2026-07-22"
    assert snapshot.source_identity.startswith("test-governed-local-snapshot:sha256:")
    assert snapshot_path.read_bytes() == before  # adapter has no writer path

    live_payload = build_work_os_portfolio(
        [_row("AAAA", name="Snapshot Company")],
        LivePortfolio(available=True, api_url="http://tracker.test", as_of="2026-08-08"),
        _available_allocation(as_of=date(2026, 8, 8)),
        offline_snapshot=snapshot,
    )
    assert live_payload.tracker_state == "current"
    assert "Offline snapshot" not in live_payload.tracker_detail

    unavailable_payload = build_work_os_portfolio(
        [_row("AAAA", name="Snapshot Company")],
        LivePortfolio(available=False, api_url="http://tracker.test", error="account 1234"),
        _available_allocation(),
        offline_snapshot=snapshot,
    )
    assert unavailable_payload.tracker_state == "offline_snapshot"
    assert unavailable_payload.tracker_detail == "Offline snapshot · 2026-07-22"
    assert unavailable_payload.total_market_value == pytest.approx(20000.0)
    assert unavailable_payload.companies[0].current_weight_pct == pytest.approx(66.0)
    assert unavailable_payload.status == "degraded"
    assert "portfolio_offline_snapshot" in unavailable_payload.warnings
    assert "account 1234" not in unavailable_payload.model_dump_json()


def test_portfolio_hydration_rejects_invalid_snapshot_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot_path = tmp_path / "invalid-governed-tracker-snapshot.json"
    snapshot_path.write_text('{"source_identity":"untrusted"}', encoding="utf-8")
    monkeypatch.setenv("PORTFOLIO_TRACKER_SNAPSHOT_PATH", str(snapshot_path))

    assert read_configured_offline_portfolio_snapshot() is None
    payload = build_work_os_portfolio(
        [_row("NU", name="Nu Holdings")],
        LivePortfolio(
            available=False, api_url="http://tracker.test", error="secret transport detail"
        ),
        _available_allocation(),
    )
    assert payload.tracker_state == "unavailable"
    assert payload.tracker_detail == "Tracker unavailable · research data only"
    assert "secret transport detail" not in payload.model_dump_json()


@pytest.mark.parametrize(
    ("available", "is_stale", "is_partial", "expected_state"),
    (
        (False, True, True, "unavailable"),
        (True, True, True, "stale"),
        (True, False, True, "partial"),
        (True, False, False, "current"),
    ),
)
def test_portfolio_hydration_preserves_tracker_state_precedence_and_safe_warning_codes(
    available: bool,
    is_stale: bool,
    is_partial: bool,
    expected_state: str,
) -> None:
    payload = build_work_os_portfolio(
        [_row("NU", name="Nu Holdings")],
        LivePortfolio(
            available=available,
            api_url="http://tracker.test",
            error="ConnectionError: account 1234",
            is_stale=is_stale,
            is_partial=is_partial,
            envelope_warnings=[
                "PARTIAL_COVERAGE",
                "STALE_HOLDINGS",
                "NO_CANONICAL_LINK",
                "PARTIAL_COVERAGE",
                "provider_stale",
                "ConnectionError: account 1234",
            ],
        ),
        _available_allocation(),
        readout_warnings=["earnings_readout_projection_unavailable", "provider_stale"],
    )

    assert payload.tracker_state == expected_state
    assert payload.warnings == [
        "earnings_readout_projection_unavailable",
        "provider_stale",
        "PARTIAL_COVERAGE",
        "STALE_HOLDINGS",
        "NO_CANONICAL_LINK",
        *(["portfolio_tracker_unavailable"] if not available else []),
        *(["portfolio_tracker_stale"] if available and is_stale else []),
        *(["portfolio_tracker_partial"] if is_partial else []),
    ]
    payload_json = payload.model_dump_json()
    assert "account 1234" not in payload_json
    assert "ConnectionError" not in payload_json


def test_portfolio_action_queue_is_material_and_bounded_to_three() -> None:
    rows = [
        _row(
            f"T{i}",
            name=f"Company {i}",
            breach_status="breach" if i == 0 else "warn",
            pending_alerts=i + 1,
            pending_tier1_alerts=1 if i == 0 else 0,
            new_docs=i,
        )
        for i in range(5)
    ]

    payload = build_work_os_portfolio(
        rows,
        LivePortfolio(available=False, api_url="http://tracker.test"),
        _available_allocation(),
    )

    assert len(payload.actions) == 3
    assert payload.actions[0].ticker == "T0"
    assert payload.actions[0].tone == "bad"
    assert payload.actions[0].headline == "Review thesis-decisive alert"


@pytest.mark.parametrize(
    ("allocation", "warning"),
    (
        (_available_allocation(state="incomplete"), "portfolio_allocation_incomplete"),
        (
            unavailable_portfolio_allocation("positions_unavailable"),
            "portfolio_allocation_unavailable",
        ),
    ),
)
def test_portfolio_hydration_degrades_for_non_available_allocation(
    allocation: PortfolioAllocationProjection,
    warning: str,
) -> None:
    payload = build_work_os_portfolio(
        [_row("NU", name="Nu Holdings")],
        LivePortfolio(available=True, api_url="http://tracker.test"),
        allocation,
    )

    assert payload.status == "degraded"
    assert payload.allocation == allocation
    assert warning in payload.warnings


def test_portfolio_hydration_fails_closed_when_source_dates_disagree() -> None:
    payload = build_work_os_portfolio(
        [_row("NU", name="Nu Holdings")],
        LivePortfolio(
            available=True,
            api_url="http://tracker.test",
            as_of="2026-08-08",
        ),
        _available_allocation(as_of=date(2026, 8, 7)),
    )

    assert payload.status == "degraded"
    assert payload.allocation.state == "unavailable"
    assert payload.allocation.reason_codes == ("snapshot_date_mismatch",)
    assert "portfolio_allocation_unavailable" in payload.warnings


def test_single_pending_alert_action_exposes_existing_identity_and_provenance() -> None:
    payload = build_work_os_portfolio(
        [
            _row(
                "NU",
                name="Nu Holdings",
                pending_alerts=1,
                pending_tier1_alerts=1,
                pending_alert_refs=(
                    PendingAlertRef(
                        alert_id=17,
                        trigger_kind="thesis_drift",
                        signature_sha="sig-thesis-17",
                        is_decisive=True,
                    ),
                ),
            )
        ],
        LivePortfolio(available=False, api_url="http://tracker.test"),
        _available_allocation(),
    )

    action = payload.actions[0]
    assert action.action_id == "alert:17"
    assert action.action_type == "thesis_drift"
    assert action.lifecycle_state == "pending"
    assert action.source_ref == "alert:17"
    assert action.evidence_ref == "sig-thesis-17"
    serialized = payload.model_dump(mode="json")["actions"][0]
    assert serialized["action_id"] == "alert:17"
    assert serialized["evidence_ref"] == "sig-thesis-17"


def test_aggregate_alert_card_does_not_invent_identity() -> None:
    payload = build_work_os_portfolio(
        [
            _row(
                "NU",
                name="Nu Holdings",
                pending_alerts=2,
                pending_alert_refs=(
                    PendingAlertRef(1, "earnings_tone", "sig-1"),
                    PendingAlertRef(2, "material_news", "sig-2"),
                ),
            )
        ],
        LivePortfolio(available=False, api_url="http://tracker.test"),
        _available_allocation(),
    )

    action = payload.actions[0]
    assert action.headline == "Review 2 pending alerts"
    assert action.action_id is None
    assert action.source_ref is None
    assert action.evidence_ref is None
