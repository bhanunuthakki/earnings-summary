"""Deterministic ranked-metric projection contracts."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from report.models import CellSource
from viewspec.engine import ViewCell, ViewResult, ViewRow
from viewspec.spec import MetricRef, ViewSpec
from viewspec.workbench import RankedMetricCandidate, build_ranked_metric_workbench


def _result(*, raw_values: list[float | None]) -> ViewResult:
    source = CellSource(source="sec_official", doc_id=7, source_url="https://example.test/ir")
    return ViewResult(
        spec=ViewSpec.from_dict({"tickers": ["MELI"], "metrics": ["fin:operating_cash_flow"]}),
        period_labels=["Q1'26", "Q2'26"],
        rows=[
            ViewRow(
                ticker="MELI",
                metric=MetricRef.parse_token("fin:operating_cash_flow"),
                label="MELI · operating_cash_flow",
                unit="millions",
                cells=[
                    ViewCell(value=value, raw=value, source=source if value is not None else None)
                    for value in raw_values
                ],
            )
        ],
        warnings=[],
    )


def test_workbench_projects_one_validated_view_and_preserves_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "portfolio.db"
    db_path.touch()
    calls: list[ViewSpec] = []

    def fake_execute(spec: ViewSpec, *, db_path: Path) -> ViewResult:
        calls.append(spec)
        return _result(raw_values=[100.0, 125.0])

    monkeypatch.setattr("viewspec.workbench.execute_view", fake_execute)
    workbench = build_ranked_metric_workbench(
        db_path,
        ["MELI"],
        [RankedMetricCandidate("fin:operating_cash_flow", "llm", "funds investment capacity")],
    )

    assert len(calls) == 1
    assert calls[0].transform == "level"
    assert calls[0].metrics == (MetricRef.parse_token("fin:operating_cash_flow"),)
    assert workbench.state == "ready"
    (row,) = workbench.rows
    assert row.label == "Operating cash flow"
    assert row.token == "fin:operating_cash_flow"
    assert row.why == "funds investment capacity"
    assert row.as_of == "Q2'26"
    assert row.value == 125.0
    assert row.change_pct == 25.0
    assert row.source is not None and row.source.doc_id == 7


def test_workbench_reports_explicit_empty_stale_and_unavailable_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "missing.db"
    candidate = RankedMetricCandidate("fin:revenue", "tier", "Core sales signal")
    assert build_ranked_metric_workbench(missing, ["MELI"], [candidate]).state == "unavailable"

    db_path = tmp_path / "portfolio.db"
    db_path.touch()
    assert build_ranked_metric_workbench(db_path, ["MELI"], []).state == "empty"

    def fake_execute(spec: ViewSpec, *, db_path: Path) -> ViewResult:
        return _result(raw_values=[None, None])

    monkeypatch.setattr("viewspec.workbench.execute_view", fake_execute)
    assert build_ranked_metric_workbench(db_path, ["MELI"], [candidate]).state == "stale"


def test_workbench_drops_unvalidated_runtime_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "portfolio.db"
    db_path.touch()

    def fake_execute(spec: ViewSpec, *, db_path: Path) -> ViewResult:
        result = _result(raw_values=[100.0, 125.0])
        result.rows[0].cells[-1].source = cast(CellSource | None, "legacy source")
        return result

    monkeypatch.setattr("viewspec.workbench.execute_view", fake_execute)
    workbench = build_ranked_metric_workbench(
        db_path,
        ["MELI"],
        [RankedMetricCandidate("fin:operating_cash_flow", "tier", "Cash conversion")],
    )

    assert workbench.state == "ready"
    assert workbench.rows[0].source is None


def test_workbench_caps_candidates_before_the_single_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "portfolio.db"
    db_path.touch()
    seen: list[ViewSpec] = []

    def fake_execute(spec: ViewSpec, *, db_path: Path) -> ViewResult:
        seen.append(spec)
        return _result(raw_values=[100.0, 125.0])

    monkeypatch.setattr("viewspec.workbench.execute_view", fake_execute)
    candidates = [RankedMetricCandidate(f"fin:metric_{i}", "tier", "signal") for i in range(10)]
    build_ranked_metric_workbench(db_path, ["MELI"], candidates)
    assert len(seen) == 1
    assert len(seen[0].metrics) == 8
