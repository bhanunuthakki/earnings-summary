"""Focused contracts for the Work OS Evaluation/Candidates projection."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from typing import NoReturn

import pytest

import pipeline.work_os_evaluation as evaluation
from allocation.candidate_fit import CandidateFit, FitFactor
from dcf.availability import DcfRouteArtifact
from models.instruments import EtfProfile
from pipeline.dashboard_status import DashboardRow
from pipeline.etf_score import StyleLoadingRead
from pipeline.peeks import render_investment_profile_peek, render_portfolio_impact_peek
from pipeline.research_cockpit import CockpitRow
from pipeline.work_os_briefs import BriefLibraryFacets, BriefLibraryItem, BriefLibraryResponse
from research.investment_profile import CompanyProfileProjection


def _row(
    ticker: str,
    *,
    name: str | None = None,
    etf: bool = False,
    score: float | None = 1.2,
    score_why: str | None = "DCF + growth",
    score_partial: bool = False,
    fit: float | None = 1.05,
    fit_why: str | None = "diversifies the book",
    fit_partial: bool = False,
    sharpe_delta_bps: float | None = 12.0,
    held_weight: float | None = 0.025,
    fair_value: float | None = 120.0,
    dcf_price: float | None = 100.0,
    dcf_unreviewed: bool = False,
    rev_yoy_pct: float | None = 30.0,
    fcf_margin_pct: float | None = 18.0,
) -> CockpitRow:
    return CockpitRow(
        base=DashboardRow(
            ticker=ticker,
            list_type="evaluation",
            fmp_last_pulled=None,
            last_transcript=None,
            last_build_at=None,
            open_comments_count=0,
            breach_status=None,
        ),
        name=name,
        fair_value=fair_value,
        dcf_price=dcf_price,
        dcf_unreviewed=dcf_unreviewed,
        rev_yoy_pct=rev_yoy_pct,
        fcf_margin_pct=fcf_margin_pct,
        attractiveness=score,
        attractiveness_why=score_why,
        attractiveness_partial=score_partial,
        fit=fit,
        fit_why=fit_why,
        fit_partial=fit_partial,
        sharpe_delta_bps=sharpe_delta_bps,
        held_weight=held_weight,
        is_etf=etf,
    )


def _brief(ticker: str) -> BriefLibraryItem:
    return BriefLibraryItem(
        artifact_id=f"report_{ticker}",
        ticker=ticker,
        title=f"{ticker} Full Research Brief",
        artifact_kind="full_brief",
        coverage_role="evaluation",
        report_date=date(2026, 8, 20).isoformat(),
        generated_at="2026-08-20T00:00:00Z",
        reader_mode="legacy_standalone",
        status="available",
        open_url=f"/reports/{ticker}?artifact_id=report_{ticker}",
        body_url=None,
        standalone_url=f"/reports/{ticker}?artifact_id=report_{ticker}",
        section_count=1,
    )


def _brief_response(*items: BriefLibraryItem) -> BriefLibraryResponse:
    return BriefLibraryResponse(
        inventory_revision="2026-08-20T00:00:00Z",
        items=items,
        facets=BriefLibraryFacets(artifact_kind=(), ticker=(), coverage_role=()),
        next_cursor=None,
    )


def test_mixed_company_and_etf_preserve_input_order_and_user_doorways(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = sqlite3.connect(":memory:")
    (tmp_path / "micro_thesis" / "holdings").mkdir(parents=True)
    (tmp_path / "micro_thesis" / "holdings" / "MELI.json").write_text(
        json.dumps({"thesis": "A durable marketplace with improving monetization."}),
        encoding="utf-8",
    )

    calls: dict[str, object] = {}

    def brief_builder(*args: object, **kwargs: object) -> BriefLibraryResponse:
        calls.update(kwargs)
        return _brief_response(_brief("MELI"))

    monkeypatch.setattr(evaluation, "build_brief_library", brief_builder)

    def resolve_dcf(_repo_root: Path, ticker: str) -> DcfRouteArtifact | None:
        return DcfRouteArtifact(kind="workbook", target="MELI.xlsx") if ticker == "MELI" else None

    monkeypatch.setattr(evaluation, "resolve_dcf_route_artifact", resolve_dcf)

    payload = evaluation.build_work_os_evaluation(
        [_row("VDE", name="Vanguard Energy ETF", etf=True), _row("MELI", name="MercadoLibre")],
        tmp_path,
        conn,
        generated_at=datetime(2026, 8, 25, 12, 0, tzinfo=UTC),
    )

    assert payload.schema_version == "evaluation_surface.v2"
    assert payload.generated_at == "2026-08-25T12:00:00Z"
    assert payload.count == 2
    assert [item.ticker for item in payload.items] == ["VDE", "MELI"]
    assert payload.items[0].instrument_type == "etf"
    assert payload.items[0].workup_url == "/api/peek/etf_workup?ticker=VDE"
    assert payload.items[0].company_desk_url is None
    assert payload.items[0].dcf_url is None
    assert payload.items[0].dcf_upside_pct is None
    assert payload.items[0].report_url is None
    assert payload.items[1].instrument_type == "company"
    assert payload.items[1].company_desk_url == "/ticker/MELI"
    assert payload.items[1].dcf_url == "/dcf/MELI"
    assert payload.items[1].report_url == "/reports/MELI?artifact_id=report_MELI"
    assert payload.items[1].source == "micro_thesis"
    assert payload.items[1].thesis_excerpt == "A durable marketplace with improving monetization."
    assert "stock" not in payload.model_dump_json().lower()
    assert calls["coverage_role"] == "evaluation"
    assert calls["conn"] is conn


def test_thesis_prefers_micro_thesis_then_falls_back_to_latest_position_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE position_entries (
            id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            entry_thesis_excerpt TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO position_entries VALUES (?, ?, ?, ?, ?)",
        [
            (1, "VDE", "old position thesis", "2026-01-01", "2026-01-01"),
            (2, "VDE", "latest position thesis", "2026-02-01", "2026-08-01"),
            (3, "MELI", "must not win over JSON", "2026-08-01", "2026-08-01"),
        ],
    )
    conn.commit()
    holdings = tmp_path / "micro_thesis" / "holdings"
    holdings.mkdir(parents=True)
    long_thesis = "word " * 100
    (holdings / "MELI.json").write_text(json.dumps({"thesis": long_thesis}), encoding="utf-8")
    (holdings / "NU.json").write_text("{malformed", encoding="utf-8")

    def empty_brief_builder(*args: object, **kwargs: object) -> BriefLibraryResponse:
        return _brief_response()

    monkeypatch.setattr(evaluation, "build_brief_library", empty_brief_builder)

    payload = evaluation.build_work_os_evaluation(
        [_row("MELI"), _row("VDE", etf=True), _row("NU")],
        tmp_path,
        conn,
    )

    meli, vde, nu = payload.items
    assert meli.source == "micro_thesis"
    assert meli.thesis_excerpt is not None
    assert len(meli.thesis_excerpt) <= 320
    assert meli.thesis_excerpt.endswith("…")
    assert vde.source == "position_entry"
    assert vde.thesis_excerpt == "latest position thesis"
    assert nu.source == "unavailable"
    assert "micro_thesis_unavailable" in payload.warnings


@pytest.mark.parametrize(
    ("fair_value", "dcf_price", "expected"),
    [
        (120.0, 100.0, 20.0),
        (120.0, 0.0, None),
        (-120.0, 100.0, None),
        (float("nan"), 100.0, None),
        (float("inf"), 100.0, None),
    ],
)
def test_dcf_math_and_numeric_fields_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fair_value: float,
    dcf_price: float,
    expected: float | None,
) -> None:
    conn = sqlite3.connect(":memory:")

    def empty_brief_builder(*args: object, **kwargs: object) -> BriefLibraryResponse:
        return _brief_response()

    monkeypatch.setattr(evaluation, "build_brief_library", empty_brief_builder)
    row = _row(
        "MELI",
        fair_value=fair_value,
        dcf_price=dcf_price,
        score=float("nan"),
        fit=float("inf"),
        sharpe_delta_bps=float("nan"),
        held_weight=float("inf"),
    )

    item = evaluation.build_work_os_evaluation([row], tmp_path, conn).items[0]

    assert item.dcf_upside_pct == pytest.approx(expected)
    assert item.score is None
    assert item.fit is None
    assert item.sharpe_delta_bps is None
    assert item.held_weight_pct is None


def test_malformed_artifacts_and_missing_position_schema_degrade_without_connection_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = sqlite3.connect(":memory:")
    holdings = tmp_path / "micro_thesis" / "holdings"
    holdings.mkdir(parents=True)
    (holdings / "MELI.json").write_text("not-json", encoding="utf-8")

    def forbidden_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        raise AssertionError(f"unexpected extra sqlite connection: {args} {kwargs}")

    monkeypatch.setattr(evaluation.sqlite3, "connect", forbidden_connect)

    def broken_brief_builder(*args: object, **kwargs: object) -> NoReturn:
        raise ValueError("bad artifact index")

    def broken_dcf_resolver(repo_root: Path, ticker: str) -> NoReturn:
        raise OSError(f"unreadable holdings for {ticker} under {repo_root}")

    monkeypatch.setattr(evaluation, "build_brief_library", broken_brief_builder)
    monkeypatch.setattr(evaluation, "resolve_dcf_route_artifact", broken_dcf_resolver)

    payload = evaluation.build_work_os_evaluation([_row("MELI")], tmp_path, conn)

    item = payload.items[0]
    assert item.source == "unavailable"
    assert item.thesis_excerpt is None
    assert item.report_url is None
    assert item.dcf_url is None
    assert "evaluation_briefs_unavailable" in payload.warnings
    assert "micro_thesis_unavailable" in payload.warnings
    assert "position_entries_unavailable" in payload.warnings
    assert "dcf_route_unavailable" in payload.warnings


def test_unreviewed_dcf_and_large_machine_explanations_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    machine_ref = "a" * 64
    oversized = f"Evidence {machine_ref} " + ("detail " * 20_000)

    def empty_brief_builder(*args: object, **kwargs: object) -> BriefLibraryResponse:
        return _brief_response()

    monkeypatch.setattr(evaluation, "build_brief_library", empty_brief_builder)

    item = evaluation.build_work_os_evaluation(
        [
            _row(
                "MELI",
                score_why=oversized,
                fit_why=oversized,
                dcf_unreviewed=True,
            )
        ],
        tmp_path,
        conn,
    ).items[0]

    assert item.dcf_upside_pct is None
    assert item.score_why is not None
    assert item.fit_why is not None
    assert len(item.score_why) <= 320
    assert len(item.fit_why) <= 320
    assert machine_ref not in item.score_why
    assert "source reference" in item.score_why


def test_invalid_machine_reference_ticker_is_omitted_before_serialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    machine_ref = "a" * 64

    def empty_brief_builder(*args: object, **kwargs: object) -> BriefLibraryResponse:
        return _brief_response()

    monkeypatch.setattr(evaluation, "build_brief_library", empty_brief_builder)

    payload = evaluation.build_work_os_evaluation(
        [_row(machine_ref), _row("meli")],
        tmp_path,
        conn,
    )

    assert [item.ticker for item in payload.items] == ["MELI"]
    assert payload.count == 1
    assert "invalid_ticker_omitted" in payload.warnings
    assert machine_ref not in payload.model_dump_json()


def test_company_profile_and_direct_portfolio_indicators_reuse_current_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE llm_artifacts (
            id INTEGER PRIMARY KEY,
            ticker TEXT,
            purpose TEXT NOT NULL,
            content_json TEXT,
            input_sha256 TEXT NOT NULL,
            superseded_by_id INTEGER
        );
        CREATE TABLE investment_profile_label_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            label TEXT NOT NULL,
            action TEXT NOT NULL,
            suggestion_fingerprint TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            reviewed_by TEXT NOT NULL,
            reviewed_at TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE
        );
        """
    )
    card = {
        "investment_profile": {
            "labels": ["long_term_compounder"],
            "summary": "A durable core engine with reinvestment runway.",
            "moat": {
                "level": "core_business",
                "evidence_coverage": "sufficient",
                "rationale": "Scale and embedded workflows defend the core business.",
            },
        }
    }
    conn.execute(
        "INSERT INTO llm_artifacts VALUES (9,'MELI','investment_decision_card',?,?,NULL)",
        (json.dumps(card), "card-input-v2"),
    )

    def fake_brief_library(*_args: object, **_kwargs: object) -> BriefLibraryResponse:
        return _brief_response()

    monkeypatch.setattr(evaluation, "build_brief_library", fake_brief_library)
    fit = CandidateFit(
        ticker="MELI",
        fit=1.12,
        why="structured factors",
        partial=False,
        sharpe_delta_bps=8.0,
        factors=[
            FitFactor("sharpe", "Marginal Sharpe", 1.12, "SR +0.8 vs hurdle +0.4", False),
            FitFactor("divers", "Diversification", 1.10, "corr +0.31 to book", False),
            FitFactor("factor", "Factor fit", 1.12, "balances the book growth tilt", False),
            FitFactor("sector", "Sector fit", 1.08, "under-represented sector", False),
        ],
    )

    def fake_candidate_fit(_root: Path) -> dict[str, CandidateFit]:
        return {"MELI": fit}

    monkeypatch.setattr(evaluation, "read_materialized_candidate_fit", fake_candidate_fit)

    item = evaluation.build_work_os_evaluation([_row("MELI")], tmp_path, conn).items[0]

    assert isinstance(item.profile, CompanyProfileProjection)
    assert [label.display_label for label in item.profile.labels] == [
        "Long-term compounder",
        "GARP",
    ]
    assert item.profile.moat.level is not None
    assert item.profile.moat.level.display_label == "Core-business moat"
    assert [indicator.key for indicator in item.portfolio_indicators] == [
        "sharpe",
        "divers",
        "factor",
        "sector",
    ]
    assert "Diversifier" in item.portfolio_role_labels
    assert "Balances factor tilt" in item.portfolio_role_labels
    assert "Risk-adjusted accretive" in item.portfolio_role_labels

    profile_html = render_investment_profile_peek(item)
    assert profile_html is not None
    assert "Core-business moat" in profile_html
    assert "Ratify" in profile_html
    assert 'data-profile-review-label="garp"' in profile_html
    assert "DCF refreshes never overwrite it" in profile_html

    impact_html = render_portfolio_impact_peek(item)
    assert impact_html is not None
    assert "Candidate vs held book" in impact_html
    assert "Marginal Sharpe" in impact_html
    assert "not a composite fit score" in impact_html


def test_etf_profile_reuses_current_profile_loadings_fit_and_whatif(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE investment_profile_label_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            label TEXT NOT NULL,
            action TEXT NOT NULL,
            suggestion_fingerprint TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            reviewed_by TEXT NOT NULL,
            reviewed_at TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE
        );
        """
    )

    def fake_brief_library(*_args: object, **_kwargs: object) -> BriefLibraryResponse:
        return _brief_response()

    monkeypatch.setattr(evaluation, "build_brief_library", fake_brief_library)
    fit = CandidateFit(
        ticker="VDE",
        fit=1.12,
        why="structured ETF factors",
        partial=False,
        sharpe_delta_bps=18.0,
        factors=[
            FitFactor("divers", "Diversification", 1.2, "corr +0.12 to book", False),
            FitFactor("overlap", "Look-through overlap", 1.08, "3% overlap", False),
            FitFactor("factor", "Factor fit", 1.12, "balances the book", False),
            FitFactor("sector", "Sector fit", 1.08, "adds energy breadth", False),
        ],
    )

    def fake_candidate_fit(_root: Path) -> dict[str, CandidateFit]:
        return {"VDE": fit}

    def fake_etf_loadings(_root: Path) -> dict[str, list[StyleLoadingRead]]:
        return {"VDE": [StyleLoadingRead(key="value", beta=0.48, r_squared=0.32, n_obs=252)]}

    def fake_etf_whatif(
        _root: Path,
    ) -> dict[str, dict[str, dict[str, float | list[str]]]]:
        return {
            "VDE": {
                "0.03": {
                    "vol_before_ann": 0.15,
                    "vol_after_ann": 0.14,
                    "sharpe_delta_bps": 18.0,
                    "degraded": [],
                }
            }
        }

    def fake_etf_profile(_conn: sqlite3.Connection, _ticker: str) -> EtfProfile:
        return EtfProfile(
            ticker="VDE",
            name="Vanguard Energy ETF",
            asset_class="equity",
            sector_label="Energy",
            expense_ratio=0.001,
            distribution_yield=0.035,
            source="issuer:test",
            profile_fetched_at=datetime(2026, 8, 20, tzinfo=UTC),
        )

    monkeypatch.setattr(evaluation, "read_materialized_candidate_fit", fake_candidate_fit)
    monkeypatch.setattr(
        evaluation, "read_materialized_etf_loadings", fake_etf_loadings, raising=False
    )
    monkeypatch.setattr(evaluation, "read_materialized_etf_whatif", fake_etf_whatif, raising=False)
    monkeypatch.setattr(evaluation, "get_etf_profile", fake_etf_profile, raising=False)

    item = evaluation.build_work_os_evaluation(
        [_row("VDE", name="Vanguard Energy ETF", etf=True, sharpe_delta_bps=18.0)],
        tmp_path,
        conn,
    ).items[0]

    assert item.profile is not None
    assert {label.label.value for label in item.profile.labels} == {
        "factor_sleeve",
        "thematic_exposure",
        "diversifier",
        "defensive_hedge",
        "income",
        "tactical_cyclical",
    }
    html = render_investment_profile_peek(item)
    assert html is not None
    assert "Defensive / hedge" in html
    assert "Published fund profile targets Energy" in html
    assert "Company moat vocabulary is not applied" in html
    assert "Ratify" in html
