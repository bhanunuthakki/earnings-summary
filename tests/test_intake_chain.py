"""Tests for execution/intake_documents.py chain wiring.

`chain_processing` must route every newly-filed doc through the LLM summarizer
(`process_ir_documents.py`), and additionally bridge transcripts into the
`transcripts` + `transcript_segments` tables (`ingest_transcripts.py
--include-ir-transcripts`) and trigger Say-Do extraction
(`extract_commitments_from_transcript.py --auto`) so manual drops match what
`backfill_transcripts.py` does for auto-fetched transcripts.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import intake_documents  # noqa: E402

from intake import IntakeClassification, IntakeResult  # noqa: E402
from models.documents import DocType  # noqa: E402


def _result(ticker: str, doc_type: DocType, *, skipped: bool = False) -> IntakeResult:
    return IntakeResult(
        source=Path(f"{ticker}_Q1_2026_doc.pdf"),
        classification=IntakeClassification(
            ticker=ticker,
            period_end=date(2026, 3, 31),
            doc_type=doc_type,
            confidence=0.9,
            reasoning="test fixture",
        ),
        dest=Path(f"ir_documents/{ticker}/2026-03-31/ir_X__abc.pdf"),
        sha256="a" * 64,
        skipped=skipped,
        skip_reason=None,
    )


def _called_script_names(mock_run: Mock) -> list[str]:
    return [_managed_target(c.args[0]).name for c in mock_run.mock_calls]


def _managed_target(argv: list[str]) -> Path:
    assert len(argv) >= 3
    assert Path(argv[1]).name == "sqlite_bootstrap.py"
    target = Path(argv[2])
    assert target.suffix == ".py"
    return target


def _ticker_for_call(call_args: list[str]) -> str:
    """Pull the `--ticker X` value from a subprocess argv list."""
    idx = call_args.index("--ticker")
    return call_args[idx + 1]


def test_chain_processing_runs_only_summarizer_for_non_transcript_docs():
    results = [_result("NU", DocType.IR_PRESS_RELEASE)]
    with patch.object(
        intake_documents.subprocess,
        "run",
        return_value=subprocess.CompletedProcess[str]([], 0),
    ) as mock_run:
        intake_documents.chain_processing(results)
    assert _called_script_names(mock_run) == ["process_ir_documents.py"]


def test_chain_processing_runs_only_summarizer_for_presentation_docs():
    results = [_result("MELI", DocType.IR_PRESENTATION)]
    with patch.object(
        intake_documents.subprocess,
        "run",
        return_value=subprocess.CompletedProcess[str]([], 0),
    ) as mock_run:
        intake_documents.chain_processing(results)
    assert _called_script_names(mock_run) == ["process_ir_documents.py"]


def test_chain_processing_bridges_transcripts_into_transcripts_table():
    results = [_result("META", DocType.EARNINGS_CALL_TRANSCRIPT)]
    with patch.object(
        intake_documents.subprocess,
        "run",
        return_value=subprocess.CompletedProcess[str]([], 0),
    ) as mock_run:
        intake_documents.chain_processing(results)

    assert _called_script_names(mock_run) == [
        "process_ir_documents.py",
        "ingest_transcripts.py",
        "extract_commitments_from_transcript.py",
    ]
    ingest_argv = mock_run.mock_calls[1].args[0]
    assert "--include-ir-transcripts" in ingest_argv
    commit_argv = mock_run.mock_calls[2].args[0]
    assert "--auto" in commit_argv


def test_chain_processing_stops_before_say_do_when_ingest_fails() -> None:
    results = [_result("META", DocType.EARNINGS_CALL_TRANSCRIPT)]
    completed: list[subprocess.CompletedProcess[str]] = [
        intake_documents.subprocess.CompletedProcess([], 0),
        intake_documents.subprocess.CompletedProcess([], 7),
    ]
    with patch.object(intake_documents.subprocess, "run", side_effect=completed) as mock_run:
        result = intake_documents.chain_processing(results)

    assert _called_script_names(mock_run) == [
        "process_ir_documents.py",
        "ingest_transcripts.py",
    ]
    assert result.ingest_failures == 1
    assert result.commitment_failures == 0
    assert result.exit_code == 7


def test_chain_processing_returns_commitment_failure() -> None:
    results = [_result("META", DocType.EARNINGS_CALL_TRANSCRIPT)]
    completed: list[subprocess.CompletedProcess[str]] = [
        intake_documents.subprocess.CompletedProcess([], 0),
        intake_documents.subprocess.CompletedProcess([], 0),
        intake_documents.subprocess.CompletedProcess([], 5),
    ]
    with patch.object(intake_documents.subprocess, "run", side_effect=completed):
        result = intake_documents.chain_processing(results)

    assert result.commitment_failures == 1
    assert result.exit_code == 5


def test_chain_processing_handles_mixed_doctype_batch():
    """Press release for NU + transcript for META: both get summarizer; only META
    gets the transcripts-table bridge + Say-Do extraction."""
    results = [
        _result("NU", DocType.IR_PRESS_RELEASE),
        _result("META", DocType.EARNINGS_CALL_TRANSCRIPT),
    ]
    with patch.object(
        intake_documents.subprocess,
        "run",
        return_value=subprocess.CompletedProcess[str]([], 0),
    ) as mock_run:
        intake_documents.chain_processing(results)

    process_tickers = {
        _ticker_for_call(c.args[0])
        for c in mock_run.mock_calls
        if _managed_target(c.args[0]).name == "process_ir_documents.py"
    }
    ingest_tickers = {
        _ticker_for_call(c.args[0])
        for c in mock_run.mock_calls
        if _managed_target(c.args[0]).name == "ingest_transcripts.py"
    }
    commit_tickers = {
        _ticker_for_call(c.args[0])
        for c in mock_run.mock_calls
        if _managed_target(c.args[0]).name == "extract_commitments_from_transcript.py"
    }

    assert process_tickers == {"NU", "META"}
    assert ingest_tickers == {"META"}
    assert commit_tickers == {"META"}


def test_chain_processing_deduplicates_multiple_transcripts_for_same_ticker():
    """Two transcripts for META (e.g. Q1 + Q2 in one drop): chain runs once per ticker."""
    results = [
        _result("META", DocType.EARNINGS_CALL_TRANSCRIPT),
        _result("META", DocType.EARNINGS_CALL_TRANSCRIPT),
    ]
    with patch.object(
        intake_documents.subprocess,
        "run",
        return_value=subprocess.CompletedProcess[str]([], 0),
    ) as mock_run:
        intake_documents.chain_processing(results)

    assert _called_script_names(mock_run) == [
        "process_ir_documents.py",
        "ingest_transcripts.py",
        "extract_commitments_from_transcript.py",
    ]


def test_chain_processing_skips_when_only_skipped_results():
    results = [_result("NU", DocType.IR_PRESS_RELEASE, skipped=True)]
    with patch.object(
        intake_documents.subprocess,
        "run",
        return_value=subprocess.CompletedProcess[str]([], 0),
    ) as mock_run:
        intake_documents.chain_processing(results)
    mock_run.assert_not_called()


def test_chain_processing_skips_when_classification_is_none():
    """Audio files have classification=None; chain must not crash on them."""
    audio = IntakeResult(
        source=Path("META_Q1_2026.mp3"),
        classification=None,
        dest=Path("transcripts/raw/META_Q1_2026.mp3"),
        sha256=None,
        skipped=False,
        skip_reason=None,
    )
    with patch.object(
        intake_documents.subprocess,
        "run",
        return_value=subprocess.CompletedProcess[str]([], 0),
    ) as mock_run:
        intake_documents.chain_processing([audio])
    mock_run.assert_not_called()


def test_main_accepts_include_ir_transcripts_without_running_summarizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = [_result("META", DocType.EARNINGS_CALL_TRANSCRIPT)]
    mock_chain = Mock()
    mock_chain.return_value = intake_documents.ChainResult()

    def fake_scan(_inbox: Path, *, dry_run: bool) -> list[IntakeResult]:
        del dry_run
        return results

    monkeypatch.setattr(intake_documents, "scan_inbox", fake_scan)
    monkeypatch.setattr(intake_documents, "chain_processing", mock_chain)
    monkeypatch.setattr(
        sys,
        "argv",
        ["intake_documents.py", "--include-ir-transcripts"],
    )

    assert intake_documents.main() == 0

    mock_chain.assert_called_once_with(
        results,
        process_documents=False,
        include_ir_transcripts=True,
    )
