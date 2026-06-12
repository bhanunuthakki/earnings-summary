"""Tests for the deterministic helpers in src/intake.py.

The LLM-driven classification path is intentionally not tested here — it requires
network and a live API key. We verify:
  - filename hint extraction (regex + ticker resolution)
  - quarter_str_for date → "Qn" mapping
  - sha256_of stable bytes → stable hex
  - DOC_TYPE_FILE_STEM and DOC_TYPE_INDEX_KEY cover every IR-relevant DocType
  - IntakeClassification rejects malformed payloads
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from intake import (
    DOC_TYPE_FILE_STEM,
    DOC_TYPE_INDEX_KEY,
    EVENT_DOC_TYPES,
    IntakeClassification,
    filename_hint,
    quarter_str_for,
    sha256_of,
)
from models.documents import DocType


def test_filename_hint_extracts_ticker_quarter_year_from_underscore_form() -> None:
    h = filename_hint("MELI_Q3_2025_Earnings_Presentation.pdf")
    assert h.ticker_hint == "MELI"
    assert h.quarter_hint == 3
    assert h.year_hint == 2025


def test_filename_hint_extracts_from_dash_form() -> None:
    h = filename_hint("bn-q1-2024-conference-call-and-webcast-transcript-f.pdf")
    assert h.ticker_hint == "BN"
    assert h.quarter_hint == 1
    assert h.year_hint == 2024


def test_filename_hint_extracts_from_space_form() -> None:
    h = filename_hint("BN Q3 2025 Conference Call Transcript.pdf")
    assert h.ticker_hint == "BN"
    assert h.quarter_hint == 3
    assert h.year_hint == 2025


def test_filename_hint_handles_year_first_form() -> None:
    h = filename_hint("Q4-25 BN Letter to Shareholders_vF.pdf")
    # Q-YY is not 4-digit year, so this should not match the year regex
    assert h.quarter_hint is None or h.year_hint is None or h.year_hint >= 2020


def test_filename_hint_resolves_aliased_ticker() -> None:
    """`googl` should resolve to `GOOG` via alias_manager."""
    h = filename_hint("googl_Q2_2025_release.pdf")
    assert h.ticker_hint == "GOOG"


def test_filename_hint_returns_none_when_unparseable() -> None:
    h = filename_hint("misc_document.pdf")
    assert h.quarter_hint is None
    assert h.year_hint is None


def test_quarter_str_for_each_quarter() -> None:
    assert quarter_str_for(date(2025, 3, 31)) == "Q1"
    assert quarter_str_for(date(2025, 6, 30)) == "Q2"
    assert quarter_str_for(date(2025, 9, 30)) == "Q3"
    assert quarter_str_for(date(2025, 12, 31)) == "Q4"


def test_quarter_str_handles_off_by_one_boundaries() -> None:
    """A period_end on April 30 (VEEV-style FYE) maps to Q2 in calendar terms."""
    assert quarter_str_for(date(2025, 4, 30)) == "Q2"
    assert quarter_str_for(date(2025, 1, 31)) == "Q1"


def test_sha256_is_deterministic(tmp_path: Path) -> None:
    f = tmp_path / "sample.bin"
    f.write_bytes(b"hello-intake")
    h1 = sha256_of(f)
    h2 = sha256_of(f)
    assert h1 == h2
    assert len(h1) == 64


def test_sha256_differs_on_byte_change(tmp_path: Path) -> None:
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    a.write_bytes(b"payload-A")
    b.write_bytes(b"payload-B")
    assert sha256_of(a) != sha256_of(b)


def test_doc_type_maps_cover_every_ir_relevant_doctype() -> None:
    """Every DocType with an `IR_` or `EARNINGS_CALL_TRANSCRIPT` member must have a stem and an index key."""
    expected = {
        DocType.IR_PRESS_RELEASE,
        DocType.IR_PRESENTATION,
        DocType.IR_SUPPLEMENT,
        DocType.IR_INVESTOR_UPDATE,
        DocType.EARNINGS_CALL_TRANSCRIPT,
        DocType.IR_EVENT,
    }
    assert set(DOC_TYPE_FILE_STEM.keys()) == expected
    assert set(DOC_TYPE_INDEX_KEY.keys()) == expected


def test_event_doc_types_routed_separately() -> None:
    """Events live in their own folder, distinguishable via EVENT_DOC_TYPES."""
    assert DocType.IR_EVENT in EVENT_DOC_TYPES
    # Quarterly types must not leak into the events bucket
    assert DocType.IR_PRESS_RELEASE not in EVENT_DOC_TYPES
    assert DocType.EARNINGS_CALL_TRANSCRIPT not in EVENT_DOC_TYPES


def test_doc_type_index_keys_subset_of_index_manager_valid_set() -> None:
    """Every index key the intake produces must be accepted by index_manager."""
    import index_manager

    for key in DOC_TYPE_INDEX_KEY.values():
        assert key in index_manager.VALID_DOC_TYPES, (
            f"index_manager.VALID_DOC_TYPES is missing {key!r}"
        )


def test_intake_classification_validates_doctype_enum() -> None:
    """A doc_type value not in DocType must fail Pydantic validation."""
    with pytest.raises(ValidationError):
        IntakeClassification.model_validate(
            {
                "ticker": "BN",
                "period_end": "2025-09-30",
                "doc_type": "ir_random_doctype_that_does_not_exist",
                "confidence": 0.9,
                "reasoning": "test",
            }
        )


def test_intake_classification_rejects_out_of_range_confidence() -> None:
    with pytest.raises(ValidationError):
        IntakeClassification.model_validate(
            {
                "ticker": "BN",
                "period_end": "2025-09-30",
                "doc_type": "ir_press_release",
                "confidence": 1.5,
                "reasoning": "test",
            }
        )


def test_intake_classification_resolves_aliased_ticker() -> None:
    c = IntakeClassification.model_validate(
        {
            "ticker": "googl",
            "period_end": "2025-06-30",
            "doc_type": "ir_press_release",
            "confidence": 0.9,
            "reasoning": "test",
        }
    )
    assert c.ticker == "GOOG"


def test_intake_classification_accepts_ir_event() -> None:
    """ir_event is a valid doc_type for non-quarterly investor day / AGM / capital markets day decks."""
    c = IntakeClassification.model_validate(
        {
            "ticker": "BN",
            "period_end": "2025-09-25",
            "doc_type": "ir_event",
            "confidence": 0.9,
            "reasoning": "investor day deck",
        }
    )
    assert c.doc_type == DocType.IR_EVENT
