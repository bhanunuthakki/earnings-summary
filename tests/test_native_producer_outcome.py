from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from report.models import (
    EarningsSection,
    FinancialsSection,
    SectionStatus,
    SegmentsSection,
    ThesisSection,
)
from report.sections import bear_case


def _outcome(
    reason: str = "thesis_missing", status: str = "blocked", purpose: str = "bear_case"
) -> str:
    return "ES_NATIVE_PRODUCER_OUTCOME " + json.dumps(
        {
            "schema_version": "native-producer-v1",
            "purpose": purpose,
            "status": status,
            "reason": reason,
        }
    )


def test_strict_bear_retains_operational_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fail(**_kwargs: object) -> str:
        raise RuntimeError("PRIVATE_PAYLOAD_DO_NOT_RETAIN")

    def empty(*_args: object, **_kwargs: object) -> str:
        return ""

    for name in (
        "_bear_inputs_sha256",
        "_last_summaries",
        "_financials_md",
        "_segments_md",
        "_kpi_status_md",
        "_ticker_specific_md",
        "_ts_signals_md",
        "load_ir_anchor",
    ):
        monkeypatch.setattr(bear_case, name, empty)

    def no_budget(*_args: object, **_kwargs: object) -> None:
        return None

    def not_hard(_error: BaseException) -> bool:
        return False

    monkeypatch.setattr(bear_case, "budget_gate", no_budget)
    monkeypatch.setattr(bear_case, "is_hard_stop", not_hard)
    monkeypatch.setattr(bear_case, "generate_bear_case", fail)
    with pytest.raises(RuntimeError, match="llm_operational_failure"):
        bear_case.build(
            ticker="TEST",
            repo_root=tmp_path,
            enable_llm=True,
            force_refresh=True,
            thesis=ThesisSection(status=SectionStatus.OK, thesis_full="Synthetic thesis"),
            financials=FinancialsSection(status=SectionStatus.MISSING_DATA),
            segments=SegmentsSection(status=SectionStatus.MISSING_DATA),
            earnings=EarningsSection(status=SectionStatus.MISSING_DATA),
            strict_regeneration=True,
        )


@pytest.mark.parametrize(
    "stream,error",
    [
        ("", "missing"),
        (_outcome() + "\n" + _outcome(), "duplicate"),
        (_outcome("source_missing", purpose="company_description"), "wrong_purpose"),
        (_outcome("unknown"), "invalid"),
        (_outcome("generated", "blocked"), "invalid"),
        ("ES_NATIVE_PRODUCER_OUTCOME {bad}", "invalid"),
        (_outcome().replace('"native-producer-v1"', '"foreign"'), "invalid"),
        (_outcome()[:-1] + ', "payload":"PRIVATE_PAYLOAD_DO_NOT_RETAIN"}', "invalid"),
        (_outcome() + " " * 512, "oversized"),
    ],
)
def test_protocol_rejects_untrusted_output(stream: str, error: str) -> None:
    from runtime.native_producer_outcome import (
        NativeProducerProtocolError,
        read_native_producer_outcome,
    )

    with pytest.raises(NativeProducerProtocolError, match=f"^{error}$"):
        read_native_producer_outcome(stream, "bear_case")


def test_protocol_emits_bounded_closed_fields(capsys: pytest.CaptureFixture[str]) -> None:
    from runtime.native_producer_outcome import (
        emit_native_producer_outcome,
        read_native_producer_outcome,
    )

    emit_native_producer_outcome("company_description", "source_missing")
    line = capsys.readouterr().out
    assert len(line.rstrip("\n").encode()) <= 512
    result = read_native_producer_outcome(line, "company_description")
    assert result.status == "blocked"
    assert set(result.model_dump()) == {"schema_version", "purpose", "status", "reason"}


@pytest.mark.parametrize(
    "mode,reason,exit_code",
    [
        ("generated", "generated", 0),
        ("missing", "source_missing", 0),
        ("exception", "producer_exception", 1),
    ],
)
def test_company_cli_terminal_outcome_excludes_source_payload(
    mode: str,
    reason: str,
    exit_code: int,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    import sys

    from compute.company_description import CompanyDescriptionResult
    from execution import extract_company_description as cli
    from runtime.native_producer_outcome import read_native_producer_outcome

    marker = "PRIVATE_PRODUCER_PAYLOAD"
    path = migrated_db(tmp_path / "fixture.sqlite")

    def produce(*_args: object, **_kwargs: object) -> CompanyDescriptionResult:
        if mode == "exception":
            raise RuntimeError(marker)
        return CompanyDescriptionResult(
            ticker="TEST",
            fiscal_year=None,
            source_path=None,
            source_sha256=None,
            elevator_pitch=marker,
            skipped_reason=marker if mode == "missing" else None,
        )

    monkeypatch.setattr(cli, "extract_for_ticker", produce)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "extract_company_description.py",
            "--ticker",
            "TEST",
            "--repo-root",
            str(tmp_path),
            "--db-path",
            str(path),
            "--native-producer-outcome",
        ],
    )
    assert cli.main() == exit_code
    output = capsys.readouterr()
    assert marker not in output.out + output.err
    assert read_native_producer_outcome(output.out, "company_description").reason == reason
