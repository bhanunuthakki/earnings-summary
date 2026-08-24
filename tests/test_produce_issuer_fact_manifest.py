"""Fail-closed conversion of reviewed IR facts into an apply manifest."""

from __future__ import annotations

import json
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from threading import Event, Thread

import pytest

from execution import produce_issuer_fact_manifest as cli
from models.documents import SourceType
from models.facts import (
    Currency,
    FactLocator,
    FiscalPeriodType,
    LocatorKind,
    SegmentDimType,
    Unit,
)
from pipeline.issuer_document_coverage import (
    ExpectedIssuerFact,
    ExtractorFactPopulationFrame,
    IssuerFactKind,
)
from pipeline.issuer_fact_manifest import IssuerFactValue, IssuerManifestFactKind
from pipeline.issuer_fact_manifest_producer import (
    ReviewedSegmentValues,
    produce_issuer_fact_manifest,
)
from pipeline.kpi_persistence import KpiExtractionManifest, KpiValue
from provenance import secure_file_install as install

_STAMP = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
_PERIOD = date(2026, 6, 30)
_SOURCE_SHA = "a" * 64


def _locator(page: int) -> FactLocator:
    return FactLocator(
        locator_version=2,
        kind=LocatorKind.PDF_SLIDE,
        pdf_page=page,
        verbatim_snippet=f"Reviewed page {page}",
    )


def _kpi(index: int) -> KpiValue:
    return KpiValue(
        name=f"MELI KPI {index:02d}",
        value=Decimal(index),
        unit=Unit.MILLIONS,
        currency=Currency.USD,
        source_excerpt=f"MELI KPI {index:02d}",
        locator=_locator(index),
    )


def _expected_kpi(index: int) -> ExpectedIssuerFact:
    return ExpectedIssuerFact(
        ticker="MELI",
        kind=IssuerFactKind.KPI,
        canonical_name=f"MELI KPI {index:02d}",
        period_end=_PERIOD,
        fiscal_period_type=FiscalPeriodType.Q2.value,
        unit=Unit.MILLIONS,
        currency=Currency.USD,
    )


def _expected_segment(name: str) -> ExpectedIssuerFact:
    return ExpectedIssuerFact(
        ticker="MELI",
        kind=IssuerFactKind.SEGMENT,
        canonical_name=f"{name} revenue",
        period_end=_PERIOD,
        fiscal_period_type=FiscalPeriodType.Q2.value,
        unit=Unit.BILLIONS,
        currency=Currency.USD,
        segment_dim_type=SegmentDimType.BUSINESS_UNIT.value,
        segment_name=name,
        metric="revenue",
    )


def _legacy(*, locator_version: int = 2) -> KpiExtractionManifest:
    values = [_kpi(index) for index in range(1, 54)]
    if locator_version != 2:
        values[0] = values[0].model_copy(
            update={"locator": FactLocator(locator_version=locator_version, pdf_page=1)}
        )
    return KpiExtractionManifest(
        ticker="MELI",
        period_end=datetime(2026, 6, 30, tzinfo=UTC),
        fiscal_period_type=FiscalPeriodType.Q2,
        source_doc_id=9101,
        values=values,
    )


def _segments(*, period_end: date = _PERIOD) -> ReviewedSegmentValues:
    return ReviewedSegmentValues(
        ticker="MELI",
        source_doc_id=9101,
        source_doc_sha256=_SOURCE_SHA,
        period_end=period_end,
        fiscal_period_type=FiscalPeriodType.Q2,
        extracted_at=_STAMP,
        values=(
            _segment_value("Commerce", Decimal("2.1"), 60, period_end),
            _segment_value("Fintech", Decimal("1.2"), 61, period_end),
        ),
    )


def _segment_value(name: str, value: Decimal, page: int, period_end: date) -> IssuerFactValue:
    return IssuerFactValue(
        ticker="MELI",
        kind=IssuerManifestFactKind.SEGMENT,
        canonical_name=f"{name} revenue",
        period_end=period_end,
        fiscal_period_type=FiscalPeriodType.Q2,
        unit=Unit.BILLIONS,
        currency=Currency.USD,
        value=value,
        locator=_locator(page),
        segment_dim_type=SegmentDimType.BUSINESS_UNIT,
        segment_name=name,
        metric="revenue",
    )


def _frame() -> ExtractorFactPopulationFrame:
    expected = (
        *(_expected_kpi(index) for index in range(1, 60)),
        _expected_segment("Commerce"),
        _expected_segment("Fintech"),
    )
    rejected = {
        item.identity_key: "Reviewed as not separately reported" for item in expected[53:59]
    }
    return ExtractorFactPopulationFrame(
        document_id=9101,
        ticker="MELI",
        expected=expected,
        rejected=rejected,
        extracted_at=_STAMP,
    )


def test_meli_53_kpis_two_segments_and_six_rejections_close_61_expected() -> None:
    manifest = produce_issuer_fact_manifest(_legacy(), _frame(), _segments())

    assert manifest.schema_version == "issuer_fact_manifest.v1"
    assert len(manifest.expected) == 61
    assert len(manifest.values) == 55
    assert len(manifest.rejected) == 6
    assert {value.kind.value for value in manifest.values} == {"kpi", "segment"}
    assert {
        (
            value.segment_name,
            value.metric,
            value.unit.value,
            value.currency.value if value.currency is not None else None,
        )
        for value in manifest.values
        if value.kind.value == "segment"
    } == {
        ("Commerce", "revenue", "billions", "USD"),
        ("Fintech", "revenue", "billions", "USD"),
    }


@pytest.mark.parametrize(
    ("legacy", "frame", "segments", "match"),
    [
        (_legacy(), _frame(), _segments(period_end=date(2026, 3, 31)), "period"),
        (_legacy(locator_version=1), _frame(), _segments(), "locator version 2"),
    ],
)
def test_producer_fails_closed_on_identity_or_locator_mismatch(
    legacy: KpiExtractionManifest,
    frame: ExtractorFactPopulationFrame,
    segments: ReviewedSegmentValues,
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        produce_issuer_fact_manifest(legacy, frame, segments)


def test_producer_rejects_missing_or_extra_population_members() -> None:
    frame = _frame()
    missing = frame.model_copy(update={"expected": frame.expected[:-1]})
    with pytest.raises(ValueError, match=r"captured facts.*expected population"):
        produce_issuer_fact_manifest(_legacy(), missing, _segments())

    captured = _legacy().model_copy(update={"values": [*_legacy().values, _kpi(60)]})
    with pytest.raises(ValueError, match=r"captured facts.*expected population"):
        produce_issuer_fact_manifest(captured, _frame(), _segments())


def test_producer_rejects_non_ir_legacy_manifest_before_conversion() -> None:
    non_ir = _legacy(locator_version=1).model_copy(update={"primary_source": SourceType.FMP})

    with pytest.raises(ValueError, match="IR_DOC"):
        produce_issuer_fact_manifest(non_ir, _frame(), _segments())


def _cli_inputs(tmp_path: Path) -> tuple[list[str], Path]:
    legacy_path = tmp_path / "legacy.json"
    frame_path = tmp_path / "frame.json"
    segment_path = tmp_path / "segments.json"
    output = tmp_path / "issuer-manifest.json"
    legacy_path.write_text(
        json.dumps({"manifests": [_legacy().model_dump(mode="json")]}), encoding="utf-8"
    )
    frame_path.write_text(_frame().model_dump_json(), encoding="utf-8")
    segment_path.write_text(_segments().model_dump_json(), encoding="utf-8")
    return (
        [
            "produce_issuer_fact_manifest.py",
            "--legacy-kpi-manifest",
            str(legacy_path),
            "--population-frame",
            str(frame_path),
            "--segment-values",
            str(segment_path),
            "--output",
            str(output),
        ],
        output,
    )


def test_cli_publishes_canonical_output_and_exact_replay_is_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    argv, output = _cli_inputs(tmp_path)
    monkeypatch.setattr(sys, "argv", argv)

    assert cli.main() == 0
    rendered = output.read_text(encoding="utf-8")
    assert rendered == json.dumps(
        json.loads(rendered), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert json.loads(capsys.readouterr().out)["expected_count"] == 61

    monkeypatch.setattr(sys, "argv", argv)
    assert cli.main() == 0
    assert output.read_text(encoding="utf-8") == rendered


def test_cli_never_exposes_partial_target_while_staged_write_is_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argv, output = _cli_inputs(tmp_path)
    real_write = install.os.write
    entered = Event()
    release = Event()
    result: list[int] = []

    def blocked_write(descriptor: int, payload: bytes | memoryview) -> int:
        entered.set()
        assert release.wait(timeout=2)
        return real_write(descriptor, payload)

    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(install.os, "write", blocked_write)
    thread = Thread(target=lambda: result.append(cli.main()))
    thread.start()
    assert entered.wait(timeout=2)
    assert not output.exists()
    release.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert result == [0]


def test_cli_collision_preserves_existing_target_and_failed_stage_creates_no_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argv, output = _cli_inputs(tmp_path)
    output.write_text("malformed target", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(install.SecureFileInstallError, match="existing_target_conflict"):
        cli.main()
    assert output.read_text(encoding="utf-8") == "malformed target"

    output.unlink()

    def failed_stage(_descriptor: int, _payload: bytes) -> None:
        raise OSError("interrupted staged write")

    monkeypatch.setattr(install, "_write_all", failed_stage)
    with pytest.raises(install.SecureFileInstallError, match="secure_install_failed"):
        cli.main()
    assert not output.exists()
