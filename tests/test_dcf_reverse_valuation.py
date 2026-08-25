"""Archetype-neutral reverse-valuation snapshots and honest root finding."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from dcf import reverse_valuation
from dcf.provenance import build_file_provenance


def test_monotonic_solver_returns_an_exact_bracketed_root() -> None:
    result = reverse_valuation.solve_monotonic(lambda value: 2.0 * value, 14.0, 0.0, 10.0)

    assert result.status == "solved"
    assert result.implied_value == pytest.approx(7.0)


def test_monotonic_solver_refuses_an_unbracketed_target() -> None:
    result = reverse_valuation.solve_monotonic(lambda value: value, 20.0, 0.0, 10.0)

    assert result.status == "unreachable"
    assert result.implied_value is None
    assert "outside" in result.note


def test_snapshot_preserves_archetype_specific_lever_list() -> None:
    inversion = reverse_valuation.solve_lever(
        lever_id="implied_exit_multiple",
        label="Implied operating exit multiple",
        unit="turns",
        base_value=12.0,
        method="monotonic_bisection",
        price_at=lambda value: value,
        target_price=15.0,
        lower_bound=1.0,
        upper_bound=30.0,
    )
    residual = reverse_valuation.residual_lever(
        lever_id="implied_credit_equity_value",
        label="Market-implied credit equity value",
        unit="usd_m",
        base_value=100.0,
        implied_value=80.0,
    )

    snapshot = reverse_valuation.ReverseValuation(
        archetype="meli_platform_sotp",
        price=15.0,
        base_value_per_share_usd=12.0,
        valuation_scope="equity",
        levers=(inversion, residual),
    ).to_snapshot_dict()

    assert snapshot["schema_version"] == 1
    assert snapshot["archetype"] == "meli_platform_sotp"
    levers = cast("list[dict[str, object]]", snapshot["levers"])
    assert [item["id"] for item in levers] == [
        "implied_exit_multiple",
        "implied_credit_equity_value",
    ]


def test_file_provenance_hashes_specialized_sources_without_hashing_the_db(tmp_path: Path) -> None:
    assumption = tmp_path / "data" / "bank_assumptions" / "NU_platform.json"
    workbook = tmp_path / "dcf" / "NU.xlsx"
    assumption.parent.mkdir(parents=True)
    workbook.parent.mkdir(parents=True)
    assumption.write_text('{"ke": 0.125}', encoding="utf-8")
    workbook.write_bytes(b"workbook")

    provenance = build_file_provenance(
        ticker="NU",
        repo_root=tmp_path,
        workbook_path=workbook,
        engine_version="nu_platform_fcfe_v1",
        effective_inputs={"ke": 0.125},
        assumption_snapshot={"model": "platform_dcf"},
        live_price=14.0,
        live_price_at=datetime(2026, 8, 25, 12, 0, tzinfo=UTC),
        live_price_source="fmp_profile",
        source_files=((assumption, "owner_assumptions"),),
    )

    assert provenance.engine_version == "nu_platform_fcfe_v1"
    assert provenance.workbook_sha256 is not None
    assert provenance.inputs_as_of.tzinfo is not None
    assert provenance.detail is not None
    sources = cast("list[dict[str, object]]", provenance.detail["sources"])
    assert {row["role"] for row in sources} == {
        "owner_assumptions",
        "calculation_workbook",
    }


def test_generated_workbook_mtime_does_not_make_specialized_inputs_look_fresh(
    tmp_path: Path,
) -> None:
    assumption = tmp_path / "assumptions.json"
    workbook = tmp_path / "model.xlsx"
    assumption.write_text("{}", encoding="utf-8")
    workbook.write_bytes(b"new output")
    old_timestamp = datetime(2024, 1, 2, 3, 4, tzinfo=UTC).timestamp()
    os.utime(assumption, (old_timestamp, old_timestamp))

    provenance = build_file_provenance(
        ticker="NU",
        repo_root=tmp_path,
        workbook_path=workbook,
        engine_version="test",
        effective_inputs={},
        assumption_snapshot={},
        live_price=None,
        live_price_at=None,
        live_price_source=None,
        source_files=((assumption, "owner_assumptions"),),
    )

    assert provenance.inputs_as_of == datetime(2024, 1, 2, 3, 4, tzinfo=UTC)
