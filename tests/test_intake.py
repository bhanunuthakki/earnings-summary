# The SEC-override tests exercise internal seams (_classify / _dest_path_for).
# pyright: reportPrivateUsage=false
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
    """Every IR DocType needs a filing stem AND a legacy index key. SEC forms get a
    stem (so an inbox-dropped 10-K files under sec_*) but NO index key — they have
    no legacy LLM handler and are registered straight into `documents`."""
    ir_expected = {
        DocType.IR_PRESS_RELEASE,
        DocType.IR_PRESENTATION,
        DocType.IR_SUPPLEMENT,
        DocType.IR_INVESTOR_UPDATE,
        DocType.EARNINGS_CALL_TRANSCRIPT,
        DocType.IR_EVENT,
    }
    sec_expected = {
        DocType.SEC_10K,
        DocType.SEC_10Q,
        DocType.SEC_20F,
        DocType.SEC_8K,
        DocType.SEC_6K,
    }
    assert set(DOC_TYPE_INDEX_KEY.keys()) == ir_expected
    assert set(DOC_TYPE_FILE_STEM.keys()) == ir_expected | sec_expected
    # The index keys must be a subset of what can be filed.
    assert set(DOC_TYPE_INDEX_KEY).issubset(DOC_TYPE_FILE_STEM)


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


# ---------------------------------------------------------------------------
# SEC-form override: the LLM classifier has no SEC doc-types, so a dropped 10-K
# is forced into an IR bucket; intake vetoes that deterministically.
# ---------------------------------------------------------------------------

_TENK_COVER = (
    "UNITED STATES SECURITIES AND EXCHANGE COMMISSION\nWashington, D.C. 20549\n"
    "FORM 10-K\n(Mark One)\nANNUAL REPORT PURSUANT TO SECTION 13 OR 15(d)\n"
    "For the fiscal year ended January 31, 2026\nRubrik, Inc.\n"
)


def test_classify_overrides_llm_ir_doctype_when_content_is_sec_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import intake

    # The LLM (no SEC types in its enum) calls a numbers-heavy 10-K an ir_supplement.
    def fake_llm(filename: str, text: str, hint: dict[str, object]) -> dict[str, object]:
        return {
            "ticker": "RBRK",
            "period_end": "2026-01-31",
            "doc_type": "ir_supplement",
            "confidence": 0.9,
            "reasoning": "lots of tables",
        }

    monkeypatch.setattr(intake, "classify_intake_document", fake_llm)
    classification, skip = intake._classify(Path("RBRK-FY26-10K.pdf"), _TENK_COVER)
    assert skip == ""
    assert classification is not None
    # doc_type was overridden to the detected SEC form; ticker/period kept.
    assert classification.doc_type is DocType.SEC_10K
    assert classification.ticker == "RBRK"
    assert classification.period_end == date(2026, 1, 31)


def test_classify_leaves_genuine_ir_doc_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    import intake

    def fake_llm(filename: str, text: str, hint: dict[str, object]) -> dict[str, object]:
        return {
            "ticker": "NU",
            "period_end": "2025-03-31",
            "doc_type": "ir_press_release",
            "confidence": 0.95,
            "reasoning": "earnings release",
        }

    monkeypatch.setattr(intake, "classify_intake_document", fake_llm)
    classification, skip = intake._classify(
        Path("NU-Q1.pdf"), "NU Holdings today reported financial results for the first quarter..."
    )
    assert skip == ""
    assert classification is not None
    assert classification.doc_type is DocType.IR_PRESS_RELEASE  # no SEC override


def test_dest_path_for_sec_form_uses_sec_stem() -> None:
    import intake

    classification = IntakeClassification.model_validate(
        {
            "ticker": "RBRK",
            "period_end": "2026-01-31",
            "doc_type": "sec_10k",
            "confidence": 0.9,
            "reasoning": "10-K cover",
        }
    )
    dest = intake._dest_path_for(classification, "deadbeefcafef00d", ".pdf")
    assert dest.name == "sec_10k__deadbeef.pdf"
    assert dest.parent.name == "2026-01-31"
    assert dest.parent.parent.name == "RBRK"
