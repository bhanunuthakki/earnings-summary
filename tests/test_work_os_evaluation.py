"""Focused contracts for the Work OS Evaluation/Candidates projection."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from typing import NoReturn

import pytest

import pipeline.work_os_evaluation as evaluation
from dcf.availability import DcfRouteArtifact
from pipeline.dashboard_status import DashboardRow
from pipeline.research_cockpit import CockpitRow
from pipeline.work_os_briefs import BriefLibraryItem, BriefLibraryResponse


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
        body_url=None,
        standalone_url=f"/reports/{ticker}?artifact_id=report_{ticker}",
        section_count=1,
    )


def _brief_response(*items: BriefLibraryItem) -> BriefLibraryResponse:
    return BriefLibraryResponse(
        inventory_revision="2026-08-20T00:00:00Z",
        items=items,
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

    assert payload.schema_version == "evaluation_surface.v1"
    assert payload.generated_at == "2026-08-25T12:00:00Z"
    assert payload.count == 2
    assert [item.ticker for item in payload.items] == ["VDE", "MELI"]
    assert payload.items[0].instrument_type == "etf"
    assert payload.items[0].workup_url == "/api/peek/etf_workup?ticker=VDE"
    assert payload.items[0].company_desk_url is None
    assert payload.items[0].dcf_url is None
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
