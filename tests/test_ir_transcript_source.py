"""Tests for the issuer IR results-center transcript source.

`ir_pipeline.transcript` fetches the company's OWN earnings-call transcript from
its results-center (the timeliest source) and `aggregator_sources` wires it in
as the FIRST link of the fetch chain. The network/Playwright path can't run in
CI, so these tests pin the pure helpers (text normalization, filename-quarter
parsing, Q&A-segment split) and the orchestration logic via monkeypatch — never
hitting the network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import aggregator_sources
from aggregator_sources import AggregatorHit, AggregatorSource
from ir_pipeline import transcript
from ir_pipeline.config import IrConfig
from ir_pipeline.transcript import (
    IrTranscriptHit,
    _normalize,  # pyright: ignore[reportPrivateUsage]  # testing internal seams
    _qa_segment,  # pyright: ignore[reportPrivateUsage]
    _quarter_of,  # pyright: ignore[reportPrivateUsage]
    fetch_ir_transcript,
)

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_normalize_folds_ligatures_zwsp_and_spacing() -> None:
    # NFKC folds the "ﬁ" ligature; the zero-width space is dropped; PDF double
    # spaces collapse; "Operator :" tightens to "Operator:"; wraps flatten.
    raw = "Operator​ :​​Good  ﬁrst\n quarter."
    assert _normalize(raw) == "Operator: Good first quarter."


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("Transcript 1Q26.pdf", (1, 2026)),
        ("NU Q1 2026 transcript.pdf", (1, 2026)),
        ("Transcricao 1T26.pdf", (1, 2026)),  # PT "Trimestre"
        ("Transcript 4Q25.pdf", (4, 2025)),
        ("Earnings Call Transcript Q3 2024.pdf", (3, 2024)),
        ("Privacy Policy.pdf", None),  # no quarter token
    ],
)
def test_quarter_of(filename: str, expected: tuple[int, int] | None) -> None:
    assert _quarter_of(filename) == expected


@pytest.mark.parametrize(
    "marker",
    [
        "We will now start the Q&A session.",
        "We will now begin the Q & A session.",
        "Operator, could you please open the line for Mr. Jorge Kuri.",
        "Our first question comes from Brian Nowak.",
    ],
)
def test_qa_segment_splits_at_marker(marker: str) -> None:
    text = f"Prepared remarks, lots of them. {marker} The actual Q&A follows here."
    seg = _qa_segment(text)
    # Everything before the marker (the prepared remarks) is dropped; everything
    # from the marker onward is kept.
    assert "Prepared remarks" not in seg
    assert "The actual Q&A follows here." in seg


def test_qa_segment_returns_whole_text_without_marker() -> None:
    text = "A transcript with no recognizable Q&A boundary marker at all."
    assert _qa_segment(text) == text


# ---------------------------------------------------------------------------
# fetch_ir_transcript orchestration (monkeypatched — no network)
# ---------------------------------------------------------------------------

_MZ_CFG = IrConfig(ticker="NU", platform="mz", results_center_url="https://ir.example/rc/")


class _FakeResp:
    def __init__(self, content: bytes, status: int = 200) -> None:
        self.content = content
        self.status_code = status


def _cfg_nu(_ticker: str, _repo: Path | None = None) -> IrConfig:
    return _MZ_CFG


def _discover_1q26(_url: str, **_kw: object) -> tuple[str, str]:
    return ("https://files/abc", "Transcript 1Q26.pdf")


def _discover_none(_url: str, **_kw: object) -> tuple[str, str] | None:
    return None


def test_fetch_returns_none_for_unconfigured_ticker() -> None:
    # A ticker with no IR config never launches a browser.
    assert fetch_ir_transcript("ZZZZ_NOT_A_TICKER", 2026, 1) is None


def test_fetch_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    body = "Prepared remarks here, plenty. We will now start the Q&A session. " + ("x " * 1000)

    def _get(*_a: object, **_k: object) -> _FakeResp:
        return _FakeResp(b"%PDF-bytes")

    def _extract(_content: bytes) -> str:
        return body

    monkeypatch.setattr(transcript, "get_config", _cfg_nu)
    monkeypatch.setattr(transcript, "_discover_transcript_url", _discover_1q26)
    monkeypatch.setattr(transcript.requests, "get", _get)
    monkeypatch.setattr(transcript, "_extract_pdf_text", _extract)

    hit = fetch_ir_transcript("NU", 2026, 1)
    assert hit is not None
    assert hit.filename == "Transcript 1Q26.pdf"
    assert hit.page_url == "https://files/abc"
    assert hit.qa_text.startswith("We will now start the Q&A session")
    assert "Prepared remarks" not in hit.qa_text


def test_fetch_quarter_mismatch_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # Results-center shows the latest quarter (1Q26); a request for an OLDER
    # quarter must miss so the caller falls through to the aggregators.
    monkeypatch.setattr(transcript, "get_config", _cfg_nu)
    monkeypatch.setattr(transcript, "_discover_transcript_url", _discover_1q26)
    assert fetch_ir_transcript("NU", 2025, 4) is None


def test_fetch_returns_none_when_no_transcript_discovered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transcript, "get_config", _cfg_nu)
    monkeypatch.setattr(transcript, "_discover_transcript_url", _discover_none)
    assert fetch_ir_transcript("NU", 2026, 1) is None


# ---------------------------------------------------------------------------
# aggregator_sources wiring
# ---------------------------------------------------------------------------


def _issuer_ir_source() -> AggregatorSource:
    """The issuer_ir source object from the registered chain (public access)."""
    return next(s for s in aggregator_sources.SOURCES if s.name == "issuer_ir")


def test_issuer_ir_is_first_in_chain() -> None:
    assert aggregator_sources.SOURCES[0].name == "issuer_ir"


def test_issuer_ir_wrapper_wraps_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    def _hit(*_a: object, **_k: object) -> IrTranscriptHit:
        return IrTranscriptHit(
            page_url="https://files/abc", qa_text="Q&A body text", filename="Transcript 1Q26.pdf"
        )

    monkeypatch.setattr(transcript, "fetch_ir_transcript", _hit)
    hit = _issuer_ir_source().fetch_qa("NU", 2026, 1)
    assert isinstance(hit, AggregatorHit)
    assert hit.source_name == "issuer_ir"
    assert hit.page_url == "https://files/abc"
    assert hit.qa_text == "Q&A body text"


def test_issuer_ir_wrapper_degrades_on_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object, **_k: object) -> IrTranscriptHit:
        raise RuntimeError("playwright exploded")

    monkeypatch.setattr(transcript, "fetch_ir_transcript", _boom)
    # Must swallow and return None so the chain falls through to the aggregators.
    assert _issuer_ir_source().fetch_qa("NU", 2026, 1) is None
