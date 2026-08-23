"""Hermetic production-contract tests for the issuer portfolio coverage CLI."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

import pytest

from execution import build_issuer_portfolio_coverage as cli
from models.facts import Currency, Unit
from pipeline.issuer_document_coverage import (
    DownstreamAvailability,
    DownstreamAvailabilityStatus,
    ExpectedIssuerFact,
    ExtractorCoverageReconciliationOutput,
    ExtractorFactPopulationFrame,
    IssuerDocumentCoverageReceipt,
    IssuerFactCoverageResult,
    IssuerFactKind,
    PortfolioCoverageReport,
    reconciliation_output,
)

AS_OF = datetime(2026, 8, 5, tzinfo=UTC)
STALE_BEFORE = datetime(2026, 8, 1, tzinfo=UTC)
PERIOD_END = date(2026, 6, 30)


def _expected(ticker: str, name: str) -> ExpectedIssuerFact:
    return ExpectedIssuerFact(
        ticker=ticker,
        kind=IssuerFactKind.KPI,
        canonical_name=name,
        period_end=PERIOD_END,
        fiscal_period_type="Q2",
        unit=Unit.ACTUAL,
        currency=Currency.USD,
    )


def _frame_json(frame: ExtractorFactPopulationFrame) -> str:
    return json.dumps(
        frame.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _output(
    ticker: str,
    document_id: int,
    name: str,
    *,
    coverage_status: Literal["captured", "rejected", "missing"],
    downstream_status: DownstreamAvailabilityStatus,
    as_of: datetime | None = AS_OF,
    stale_before: datetime | None = STALE_BEFORE,
    source_url: str | None = None,
) -> ExtractorCoverageReconciliationOutput:
    expected = _expected(ticker, name)
    rejection_reason = "source_column_is_guidance" if coverage_status == "rejected" else None
    result = IssuerFactCoverageResult(
        expected=expected,
        coverage_status=coverage_status,
        captured_fact_ids=[document_id * 10] if coverage_status == "captured" else [],
        rejection_reason=rejection_reason,
        downstream=DownstreamAvailability(
            status=downstream_status,
            fact_id=document_id * 10
            if downstream_status
            in {DownstreamAvailabilityStatus.AVAILABLE, DownstreamAvailabilityStatus.STALE}
            else None,
            document_id=document_id
            if downstream_status
            in {DownstreamAvailabilityStatus.AVAILABLE, DownstreamAvailabilityStatus.STALE}
            else None,
        ),
    )
    rejection_frame_json: str | None = None
    rejection_frame_sha256: str | None = None
    if coverage_status == "rejected":
        frame = ExtractorFactPopulationFrame(
            document_id=document_id,
            ticker=ticker,
            expected=(expected,),
            rejected={expected.identity_key: rejection_reason or ""},
            extracted_at=AS_OF,
        )
        frame_json = _frame_json(frame)
        rejection_frame_json = frame_json
        rejection_frame_sha256 = hashlib.sha256(frame_json.encode("utf-8")).hexdigest()
    receipt = IssuerDocumentCoverageReceipt(
        document_id=document_id,
        ticker=ticker,
        source_type="ir_doc",
        doc_type="ir_presentation",
        source_url=source_url or f"https://example.test/{ticker}/{document_id}",
        source_fetched_at=STALE_BEFORE,
        as_of=as_of,
        stale_before=stale_before,
        extracted_at=AS_OF,
        results=[result],
        rejection_frame_json=rejection_frame_json,
        rejection_frame_sha256=rejection_frame_sha256,
    )
    return reconciliation_output(receipt)


def _zero_output(ticker: str, document_id: int) -> ExtractorCoverageReconciliationOutput:
    frame = ExtractorFactPopulationFrame(
        document_id=document_id,
        ticker=ticker,
        expected=(),
        extracted_at=AS_OF,
        expected_population_status="zero_expected",
    )
    frame_json = _frame_json(frame)
    receipt = IssuerDocumentCoverageReceipt(
        document_id=document_id,
        ticker=ticker,
        source_type="ir_doc",
        doc_type="ir_presentation",
        source_fetched_at=STALE_BEFORE,
        as_of=AS_OF,
        stale_before=STALE_BEFORE,
        extracted_at=AS_OF,
        population_frame_json=frame_json,
        population_frame_sha256=hashlib.sha256(frame_json.encode("utf-8")).hexdigest(),
        results=[],
    )
    return reconciliation_output(receipt)


def _write(path: Path, output: ExtractorCoverageReconciliationOutput) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(output.model_dump_json(indent=2), encoding="utf-8")
    return path


def _run(repo: Path, output: Path, receipts: tuple[Path, ...]) -> int:
    argv = ["--repo-root", str(repo), "--output", str(output)]
    for receipt in receipts:
        argv.extend(("--receipt", str(receipt)))
    return cli.main(argv)


def _reason(capsys: pytest.CaptureFixture[str]) -> str:
    captured = capsys.readouterr()
    return str(json.loads(captured.err.strip().splitlines()[-1])["reason_code"])


def test_cli_builds_stable_document_axis_report_for_portfolio_and_sparse_receipts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    receipts = (
        _write(
            repo / "receipts" / "meli-captured.json",
            _output(
                "MELI",
                1,
                "Total Payment Volume",
                coverage_status="captured",
                downstream_status=DownstreamAvailabilityStatus.AVAILABLE,
            ),
        ),
        _write(
            repo / "receipts" / "meli-rejected.json",
            _output(
                "MELI",
                2,
                "Total Payment Volume",
                coverage_status="rejected",
                downstream_status=DownstreamAvailabilityStatus.MISSING,
            ),
        ),
        _write(
            repo / "receipts" / "nu.json",
            _output(
                "NU",
                3,
                "Monthly active customers",
                coverage_status="missing",
                downstream_status=DownstreamAvailabilityStatus.NOT_AVAILABLE_AS_OF,
            ),
        ),
        _write(
            repo / "receipts" / "nvo.json",
            _output(
                "NVO",
                4,
                "Obesity care sales",
                coverage_status="captured",
                downstream_status=DownstreamAvailabilityStatus.STALE,
            ),
        ),
        _write(
            repo / "receipts" / "sparse.json",
            _output(
                "SPARSE",
                5,
                "Reported deposits",
                coverage_status="missing",
                downstream_status=DownstreamAvailabilityStatus.UNVERIFIABLE,
            ),
        ),
    )
    first_path = repo / ".tmp" / "coverage-first.json"
    second_path = repo / ".tmp" / "coverage-second.json"

    assert _run(repo, first_path, tuple(reversed(receipts))) == 0
    capsys.readouterr()
    assert _run(repo, second_path, receipts) == 0
    capsys.readouterr()

    assert first_path.read_bytes() == second_path.read_bytes()
    report = PortfolioCoverageReport.model_validate_json(first_path.read_text(encoding="utf-8"))
    assert report.schema_version == "issuer_portfolio_coverage.v1"
    assert report.as_of == AS_OF
    assert report.stale_before == STALE_BEFORE
    assert report.source_receipt_keys == tuple(sorted(report.source_receipt_keys))
    assert [row.ticker for row in report.rows] == ["MELI", "NU", "NVO", "SPARSE"]
    by_ticker = {row.ticker: row for row in report.rows}
    assert by_ticker["MELI"].document_count == 2
    assert by_ticker["MELI"].expected_count == 2
    assert by_ticker["MELI"].captured_count == 1
    assert by_ticker["MELI"].rejected_count == 1
    assert by_ticker["MELI"].downstream_available_count == 1
    assert by_ticker["MELI"].downstream_missing_count == 1
    assert by_ticker["NU"].missing_count == 1
    assert by_ticker["NU"].downstream_missing_count == 1
    assert by_ticker["NVO"].downstream_stale_count == 1
    assert by_ticker["SPARSE"].downstream_unverifiable_count == 1
    assert list(repo.rglob("*.db")) == []


def test_cli_rejects_malformed_and_tampered_receipts_without_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    valid = _output(
        "MELI",
        1,
        "Total Payment Volume",
        coverage_status="captured",
        downstream_status=DownstreamAvailabilityStatus.AVAILABLE,
    )
    malformed = repo / "receipts" / "malformed.json"
    malformed.parent.mkdir(parents=True)
    malformed.write_text("{", encoding="utf-8")
    invalid_utf8 = repo / "receipts" / "invalid-utf8.json"
    invalid_utf8.write_bytes(b"\xff")
    tampered_key_payload = valid.model_dump(mode="json")
    tampered_key_payload["idempotency_key"] = "0" * 64
    tampered_key = repo / "receipts" / "tampered-key.json"
    tampered_key.write_text(json.dumps(tampered_key_payload), encoding="utf-8")
    tampered_receipt_payload = valid.model_dump(mode="json")
    tampered_receipt_payload["receipt"]["source_url"] = "https://tampered.example"
    tampered_receipt = repo / "receipts" / "tampered-receipt.json"
    tampered_receipt.write_text(json.dumps(tampered_receipt_payload), encoding="utf-8")

    cases = (
        (malformed, "invalid_receipt"),
        (invalid_utf8, "invalid_receipt"),
        (tampered_key, "receipt_digest_mismatch"),
        (tampered_receipt, "receipt_digest_mismatch"),
    )
    for index, (receipt, reason_code) in enumerate(cases):
        output = repo / ".tmp" / f"invalid-{index}.json"
        assert _run(repo, output, (receipt,)) == 2
        assert _reason(capsys) == reason_code
        assert not output.exists()
    assert list(repo.rglob("*.db")) == []


def test_cli_rejects_duplicate_conflicting_and_invalid_document_inputs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    base = _output(
        "NU",
        1,
        "Monthly active customers",
        coverage_status="missing",
        downstream_status=DownstreamAvailabilityStatus.MISSING,
    )
    base_path = _write(repo / "receipts" / "base.json", base)
    output = repo / ".tmp" / "duplicate.json"
    assert _run(repo, output, (base_path, base_path)) == 2
    assert _reason(capsys) == "duplicate_receipt"
    assert not output.exists()

    conflict = reconciliation_output(
        base.receipt.model_copy(update={"source_url": "https://example.test/NU/revised"})
    )
    conflict_path = _write(repo / "receipts" / "conflict.json", conflict)
    assert _run(repo, output, (base_path, conflict_path)) == 2
    assert _reason(capsys) == "conflicting_document_receipt"
    assert not output.exists()

    duplicate_fact = reconciliation_output(
        base.receipt.model_copy(update={"results": [base.receipt.results[0]] * 2})
    )
    duplicate_fact_path = _write(repo / "receipts" / "duplicate-fact.json", duplicate_fact)
    assert _run(repo, output, (duplicate_fact_path,)) == 2
    assert _reason(capsys) == "duplicate_document_fact"
    assert not output.exists()

    lower_result = base.receipt.results[0].model_copy(
        update={"expected": base.receipt.results[0].expected.model_copy(update={"ticker": "nu"})}
    )
    lowercase = reconciliation_output(
        base.receipt.model_copy(update={"ticker": "nu", "results": [lower_result]})
    )
    lowercase_path = _write(repo / "receipts" / "lowercase.json", lowercase)
    assert _run(repo, output, (lowercase_path,)) == 2
    assert _reason(capsys) == "noncanonical_ticker"
    assert not output.exists()

    zero_path = _write(repo / "receipts" / "zero.json", _zero_output("NU", 2))
    assert _run(repo, output, (zero_path,)) == 2
    assert _reason(capsys) == "period_axis_unavailable"
    assert not output.exists()


def test_cli_normalizes_equivalent_offsets_and_rejects_different_instants(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    utc_path = _write(
        repo / "receipts" / "utc.json",
        _output(
            "NU",
            1,
            "Monthly active customers",
            coverage_status="missing",
            downstream_status=DownstreamAvailabilityStatus.MISSING,
        ),
    )
    equivalent_path = _write(
        repo / "receipts" / "offset.json",
        _output(
            "NVO",
            2,
            "Obesity care sales",
            coverage_status="missing",
            downstream_status=DownstreamAvailabilityStatus.MISSING,
            as_of=datetime.fromisoformat("2026-08-04T17:00:00-07:00"),
            stale_before=datetime.fromisoformat("2026-07-31T17:00:00-07:00"),
        ),
    )
    accepted = repo / ".tmp" / "equivalent.json"
    assert _run(repo, accepted, (utc_path, equivalent_path)) == 0
    capsys.readouterr()
    assert (
        PortfolioCoverageReport.model_validate_json(accepted.read_text(encoding="utf-8")).as_of
        == AS_OF
    )

    mixed_path = _write(
        repo / "receipts" / "mixed.json",
        _output(
            "NVO",
            3,
            "Obesity care sales",
            coverage_status="missing",
            downstream_status=DownstreamAvailabilityStatus.MISSING,
            as_of=datetime(2026, 8, 6, tzinfo=UTC),
        ),
    )
    rejected = repo / ".tmp" / "mixed.json"
    assert _run(repo, rejected, (utc_path, mixed_path)) == 2
    assert _reason(capsys) == "incompatible_coverage_basis"
    assert not rejected.exists()


def test_cli_rejects_traversal_symlink_escape_and_input_output_collision(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    receipt = _write(
        repo / "receipts" / "receipt.json",
        _output(
            "NVO",
            1,
            "Obesity care sales",
            coverage_status="missing",
            downstream_status=DownstreamAvailabilityStatus.MISSING,
        ),
    )
    traversal = repo / ".tmp" / ".." / "escape.json"
    assert _run(repo, traversal, (receipt,)) == 2
    assert _reason(capsys) == "output_outside_tmp"
    assert not (repo / "escape.json").exists()

    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / ".tmp").mkdir(parents=True, exist_ok=True)
    (repo / ".tmp" / "link").symlink_to(outside, target_is_directory=True)
    escaped = repo / ".tmp" / "link" / "escaped.json"
    assert _run(repo, escaped, (receipt,)) == 2
    assert _reason(capsys) == "output_outside_tmp"
    assert not (outside / "escaped.json").exists()

    collision = repo / ".tmp" / "receipt.json"
    collision.write_bytes(receipt.read_bytes())
    assert _run(repo, collision, (collision,)) == 2
    assert _reason(capsys) == "input_output_collision"


def test_cli_replays_identical_output_and_never_overwrites_conflict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    receipt = _write(
        repo / "receipts" / "receipt.json",
        _output(
            "SPARSE",
            1,
            "Reported deposits",
            coverage_status="missing",
            downstream_status=DownstreamAvailabilityStatus.UNVERIFIABLE,
        ),
    )
    output = repo / ".tmp" / "coverage.json"
    assert _run(repo, output, (receipt,)) == 0
    first_stdout = json.loads(capsys.readouterr().out)
    first = output.read_bytes()
    assert first_stdout["replayed"] is False

    assert _run(repo, output, (receipt,)) == 0
    replay_stdout = json.loads(capsys.readouterr().out)
    assert replay_stdout["replayed"] is True
    assert output.read_bytes() == first

    conflict = b'{"different":true}\n'
    output.write_bytes(conflict)
    assert _run(repo, output, (receipt,)) == 2
    assert _reason(capsys) == "output_conflict"
    assert output.read_bytes() == conflict


def test_cli_preserves_output_created_by_a_concurrent_writer(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    receipt = _write(
        repo / "receipts" / "receipt.json",
        _output(
            "MELI",
            1,
            "Total Payment Volume",
            coverage_status="missing",
            downstream_status=DownstreamAvailabilityStatus.MISSING,
        ),
    )
    output = repo / ".tmp" / "coverage.json"
    competing_payload = b'{"concurrent":true}\n'
    real_link = cli.os.link

    def publish_competing_file(source: Path, destination: Path) -> None:
        Path(destination).write_bytes(competing_payload)
        real_link(source, destination)

    monkeypatch.setattr(cli.os, "link", publish_competing_file)
    assert _run(repo, output, (receipt,)) == 2
    assert _reason(capsys) == "output_conflict"
    assert output.read_bytes() == competing_payload
    assert list((repo / ".tmp").glob(".coverage.json.*.tmp")) == []


def test_cli_removes_new_output_and_temporary_file_when_readback_fails(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    receipt = _write(
        repo / "receipts" / "receipt.json",
        _output(
            "NVO",
            1,
            "Obesity care sales",
            coverage_status="missing",
            downstream_status=DownstreamAvailabilityStatus.MISSING,
        ),
    )
    output = repo / ".tmp" / "coverage.json"

    def fail_readback(_path: Path, _expected: PortfolioCoverageReport) -> None:
        raise cli.CoverageBuildError("output_readback_failed")

    monkeypatch.setattr(cli, "_validated_readback", fail_readback)
    assert _run(repo, output, (receipt,)) == 2
    assert _reason(capsys) == "output_readback_failed"
    assert not output.exists()
    assert list((repo / ".tmp").glob(".coverage.json.*.tmp")) == []
    assert list(repo.rglob("*.db")) == []
