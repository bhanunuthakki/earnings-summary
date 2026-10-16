from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from compute import thesis_kpi_registration as registration
from compute.thesis_metric_series import MetricSeriesPoint, MetricSeriesResult
from pipeline.kpi_report_reference_dispositions import load_report_kpi_reference_inventory
from pipeline.kpi_report_reference_resolver import VerifiedReportKpiReferenceDefinition


def _holdings(root: Path, candidates: list[dict[str, str]]) -> Path:
    holdings = root / "micro_thesis" / "holdings"
    holdings.mkdir(parents=True)
    (holdings / "SYNTH.json").write_text(
        json.dumps(
            {
                "ticker": "SYNTH",
                "thesis": "Synthetic company",
                "kpi_registry_candidates": candidates,
            }
        )
    )
    return holdings


def _series(quarters: int) -> MetricSeriesResult:
    return MetricSeriesResult(
        status="available",
        points=tuple(
            MetricSeriesPoint(
                period_end=datetime(2022 + i // 4, (i % 4 + 1) * 3, 28, tzinfo=UTC),
                value=Decimal(10),
                fiscal_year=2022 + i // 4,
                fiscal_period=f"Q{i % 4 + 1}",
                reporting_entity_id="synthetic-entity",
                unit="percent",
                currency=None,
                accounting_basis="operational",
                consolidation_scope="consolidated",
            )
            for i in range(quarters)
        ),
    )


def test_registration_waits_for_eight_admitted_quarters_and_preserves_owner_settings(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = migrated_db(tmp_path / "registry.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    holdings = _holdings(tmp_path / "repo", [{"name": "Synthetic growth"}])

    def verified(*args: object, **kwargs: object) -> VerifiedReportKpiReferenceDefinition:
        return VerifiedReportKpiReferenceDefinition(
            kpi_definition_id=1,
            definition_name="Synthetic growth",
            resolution_id=None,
        )

    def seven(*args: object) -> MetricSeriesResult:
        return _series(7)

    def eight(*args: object) -> MetricSeriesResult:
        return _series(8)

    monkeypatch.setattr(registration, "verified_report_kpi_reference_definition", verified)
    monkeypatch.setattr(registration, "calculate_metric_series", seven)
    try:
        result = registration.refresh_thesis_kpi_registration(
            conn, holdings_dir=holdings, ticker="SYNTH", apply=True
        )
        assert result[0].status == "pending"
        assert conn.execute("SELECT COUNT(*) FROM user_kpi_registry").fetchone()[0] == 0
        monkeypatch.setattr(registration, "calculate_metric_series", eight)
        result = registration.refresh_thesis_kpi_registration(
            conn, holdings_dir=holdings, ticker="SYNTH", apply=True
        )
        assert result[0].status == "registered"
        row = conn.execute("SELECT * FROM user_kpi_registry").fetchone()
        assert row["threshold_direction"] is None and row["threshold_value"] is None
        assert row["is_thesis_breaker"] == 0
        conn.execute(
            "UPDATE user_kpi_registry SET threshold_direction=?,threshold_value=?,is_thesis_breaker=1",
            ("below", 9),
        )
        registration.refresh_thesis_kpi_registration(
            conn, holdings_dir=holdings, ticker="SYNTH", apply=True
        )
        rows = conn.execute("SELECT * FROM user_kpi_registry").fetchall()
        assert len(rows) == 1
        assert rows[0]["threshold_value"] == 9 and rows[0]["is_thesis_breaker"] == 1
    finally:
        conn.close()


def test_registry_reference_review_cannot_be_bypassed(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(migrated_db(tmp_path / "blocked.db"))
    holdings = _holdings(tmp_path / "repo", [{"name": "Synthetic growth"}])

    def blocked(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(registration, "verified_report_kpi_reference_definition", blocked)
    try:
        result = registration.refresh_thesis_kpi_registration(
            conn, holdings_dir=holdings, ticker="SYNTH", apply=True
        )
        assert result[0].reason_code == "unverified_report_kpi_reference"
        assert conn.execute("SELECT COUNT(*) FROM user_kpi_registry").fetchone()[0] == 0
    finally:
        conn.close()


def test_calculated_references_inventory_nested_leaves(tmp_path: Path) -> None:
    holdings = _holdings(tmp_path, [{"name": "Synthetic growth"}])
    payload = json.loads((holdings / "SYNTH.json").read_text())
    payload["business_model_rules"] = [
        {
            "kpi_name": "Display only",
            "metric_expression": {"operation": "ttm_fcf_margin"},
        }
    ]
    payload["break_rules_soft"] = [
        {
            "predicate": {
                "type": "compound",
                "params": {
                    "predicates": [
                        {
                            "type": "metric_threshold",
                            "params": {
                                "expression": {
                                    "operation": "yoy_pp",
                                    "input": {
                                        "operation": "level",
                                        "source": "kpi",
                                        "name": "Direct mix",
                                    },
                                }
                            },
                        },
                    ]
                },
            }
        }
    ]
    (holdings / "SYNTH.json").write_text(json.dumps(payload))
    inventory = load_report_kpi_reference_inventory(tmp_path, ("SYNTH",))
    assert inventory.source_states[0].status == "valid"
    assert {item.json_pointer for item in inventory.references} == {
        "/kpi_registry_candidates/0/name",
        "/break_rules_soft/0/predicate/params/predicates/0/params/expression/input/name",
    }
    assert all(item.requested_label != "Display only" for item in inventory.references)
