"""Smoke tests — confirm model package imports and core enums are consistent."""

from __future__ import annotations

from models.artifacts import (
    ArtifactFlags,
    ArtifactKind,
    Quarter,
    parse_tmp_artifact,
    parse_transcript_processed,
)
from models.documents import DocType, FetchStatus, SourceType
from models.facts import Currency, FiscalPeriodType, Unit
from models.kpis import KpiDefinition, ThesisTier
from models.runs import IngestionRun, StageName, StageStatus
from models.validation import Severity, ValidationIssue, ValidationRule


def test_source_type_enum_complete() -> None:
    """The seven source types defined in directives/data_provenance.md exist."""
    assert len(set(SourceType)) == 7


def test_doc_type_distinct_from_source() -> None:
    """DocType and SourceType values are independent — never substring-match between them."""
    overlap = {m.value for m in DocType} & {m.value for m in SourceType}
    assert overlap == set()


def test_stage_status_partitions_cleanly() -> None:
    """Terminal and non-terminal stage statuses are disjoint and exhaustive."""
    terminal = {StageStatus.OK, StageStatus.FAILED, StageStatus.SKIPPED, StageStatus.NEEDS_REVIEW}
    in_flight = {StageStatus.NOT_STARTED, StageStatus.IN_PROGRESS}
    assert terminal.isdisjoint(in_flight)
    assert terminal | in_flight == set(StageStatus)


def test_validation_severity_binary() -> None:
    """Severity is binary: WARN allows continuation, HALT terminates."""
    assert set(Severity) == {Severity.WARN, Severity.HALT}


def test_currency_required_for_value_types() -> None:
    """Spot-check enums — these would silently break code that hardcodes USD."""
    assert Currency.USD in Currency
    assert Currency.BRL in Currency
    assert Currency.DKK in Currency
    assert Currency.KRW in Currency


def test_models_construct_without_error() -> None:
    """Model classes are importable and have populated field schemas."""
    assert KpiDefinition.model_fields
    assert IngestionRun.model_fields
    assert ValidationIssue.model_fields
    assert FiscalPeriodType.FY in FiscalPeriodType
    assert StageName.INGEST in StageName
    assert ThesisTier.TIER_1_BREAK in ThesisTier
    assert ValidationRule.SCHEMA_DRIFT in ValidationRule
    assert FetchStatus.OK in FetchStatus
    assert Unit.ACTUAL in Unit


def test_artifact_parser_canonical_names() -> None:
    """Canonical filenames parse to the right kind and period."""
    transcript = parse_transcript_processed("AAPL_Q3_2024.pdf")
    assert transcript is not None
    assert transcript.ticker == "AAPL"
    assert transcript.quarter == Quarter.Q3
    assert transcript.year == 2024
    assert transcript.kind == ArtifactKind.TRANSCRIPT_PROCESSED

    audio = parse_tmp_artifact("MSFT_Q1_2025.mp3")
    assert audio is not None
    assert audio.kind == ArtifactKind.AUDIO

    saydo = parse_tmp_artifact("SayDo_NVDA_Q4_2023_Q1_2024.txt")
    assert saydo is not None
    assert saydo.kind == ArtifactKind.SAYDO
    assert saydo.quarter == Quarter.Q1
    assert saydo.year == 2024

    summary = parse_tmp_artifact("GOOGL_Q2_2024_summary.txt")
    assert summary is not None
    assert summary.kind == ArtifactKind.LLM_SUMMARY


def test_artifact_parser_silent_misclassification_fixed() -> None:
    """Names with stray underscores or wrong format return None instead of guessing."""
    assert parse_tmp_artifact("AAPL_extra_Q3_2024.mp3") is None
    assert parse_tmp_artifact("AAPL_Q3_24.mp3") is None
    assert parse_tmp_artifact("AAPL_Q5_2024.mp3") is None
    assert parse_tmp_artifact("AAPL_Q3_2024_extra.txt") is None


def test_artifact_parser_lowercase_normalizes_to_uppercase() -> None:
    """Lowercase or mixed-case ticker letters parse and normalize to uppercase."""
    a = parse_tmp_artifact("aapl_Q3_2024.mp3")
    assert a is not None
    assert a.ticker == "AAPL"


def test_artifact_flags_default_all_false() -> None:
    """ArtifactFlags fields all default to False."""
    f = ArtifactFlags()
    assert not f.has_release_file
    assert not f.has_transcript_file
    assert not f.has_audio_file
    assert not f.step_llm_summarized
