"""Tests for the issuer IR results-center transcript source.

`ir_pipeline.transcript` fetches the company's OWN earnings-call transcript from
its results-center (the timeliest source) and `aggregator_sources` wires it in
as the FIRST link of the fetch chain. It locates the URL through the shared
IR-document discovery (`ir_pipeline.discover` + the persisted URL manifest), so a
single crawl serves both pipelines. The network/Playwright path can't run in CI,
so these tests pin the pure helpers (text normalization, filename-quarter
parsing, Q&A-segment split, transcript selection) and the discovery/orchestration
logic via monkeypatch — never hitting the network.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

import aggregator_sources
from aggregator_sources import AggregatorHit, AggregatorSource
from ir_pipeline import transcript
from ir_pipeline._net import UnsafeURLError
from ir_pipeline.config import IrConfig
from ir_pipeline.discover._docmeta import CandidateDoc
from ir_pipeline.manifest import ManifestEntry
from ir_pipeline.transcript import (
    IrTranscriptHit,
    _locate_transcript,  # pyright: ignore[reportPrivateUsage]  # testing internal seams
    _match_transcript,  # pyright: ignore[reportPrivateUsage]
    _normalize,  # pyright: ignore[reportPrivateUsage]
    _qa_segment,  # pyright: ignore[reportPrivateUsage]
    _quarter_of,  # pyright: ignore[reportPrivateUsage]
    fetch_ir_transcript,
)

_MZ_CFG = IrConfig(ticker="NU", platform="mz", results_center_url="https://ir.example/rc/")


@pytest.fixture(autouse=True)
def public_test_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep transcript tests hermetic while the production guard resolves DNS."""

    monkeypatch.setattr(
        "ir_pipeline._net.socket.getaddrinfo",
        lambda host, port, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))
        ],
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
# _match_transcript — selects the right candidate by host-advertised filename
# ---------------------------------------------------------------------------


def test_match_transcript_picks_requested_quarter() -> None:
    names = {
        "https://ir.example/old": "Transcript 4Q25.pdf",
        "https://ir.example/new": "Transcript 1Q26.pdf",
    }

    def _name(url: str) -> str:
        return names[url]

    assert _match_transcript(list(names), 2026, 1, _name) == (
        "https://ir.example/new",
        "Transcript 1Q26.pdf",
    )


def test_match_transcript_none_when_quarter_absent() -> None:
    def _name(_url: str) -> str:
        return "Transcript 1Q26.pdf"

    assert _match_transcript(["https://ir.example/transcript"], 2024, 2, _name) is None


def test_match_transcript_skips_non_transcript_files() -> None:
    # A spreadsheet for the right quarter is not a transcript → no match.
    def _name(_url: str) -> str:
        return "Historical Data 1Q26.xlsx"

    assert _match_transcript(["https://ir.example/spreadsheet"], 2026, 1, _name) is None


def test_match_transcript_skips_unprobeable_link() -> None:
    # The first link's header probe fails; the second (a real transcript) wins.
    def _name(url: str) -> str:
        if url.endswith("/bad"):
            raise OSError("header probe failed")
        return "Transcript 1Q26.pdf"

    assert _match_transcript(
        ["https://ir.example/bad", "https://ir.example/good"], 2026, 1, _name
    ) == ("https://ir.example/good", "Transcript 1Q26.pdf")


def test_match_transcript_blocks_unsafe_url_before_filename_probe() -> None:
    called = False

    def _name(_url: str) -> str:
        nonlocal called
        called = True
        return "Transcript 1Q26.pdf"

    assert _match_transcript(["http://127.0.0.1/private"], 2026, 1, _name) is None
    assert called is False


# ---------------------------------------------------------------------------
# _locate_transcript — manifest-first, live hybrid crawl only on a miss
# ---------------------------------------------------------------------------


def test_locate_prefers_manifest_over_live_crawl(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _manifest(_root: Path, _ticker: str) -> list[ManifestEntry]:
        return [
            ManifestEntry(
                ticker="NU",
                doc_type="transcript",
                url="https://ir.example/manifest",
            )
        ]

    def _name(_url: str) -> str:
        return "Transcript 1Q26.pdf"

    def _must_not_crawl(**_kw: object) -> list[CandidateDoc]:
        raise AssertionError("hybrid crawl must not run when the manifest already has it")

    monkeypatch.setattr(transcript, "load_manifest", _manifest)
    monkeypatch.setattr(transcript, "filename_for_url", _name)
    monkeypatch.setattr(transcript, "discover_history_hybrid", _must_not_crawl)

    assert _locate_transcript(_MZ_CFG, "NU", 2026, 1, tmp_path) == (
        "https://ir.example/manifest",
        "Transcript 1Q26.pdf",
    )


def test_locate_falls_back_to_hybrid_when_manifest_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _empty(_root: Path, _ticker: str) -> list[ManifestEntry]:
        return []

    def _name(_url: str) -> str:
        return "Transcript 1Q26.pdf"

    def _crawl(**_kw: object) -> list[CandidateDoc]:
        return [
            CandidateDoc(
                url="https://ir.example/hybrid",
                link_text="",
                filename_hint="",
                doc_type_guess="transcript",
                year_guess=2026,
                quarter_guess=1,
                source_page="",
            )
        ]

    monkeypatch.setattr(transcript, "load_manifest", _empty)
    monkeypatch.setattr(transcript, "filename_for_url", _name)
    monkeypatch.setattr(transcript, "discover_history_hybrid", _crawl)

    assert _locate_transcript(_MZ_CFG, "NU", 2026, 1, tmp_path) == (
        "https://ir.example/hybrid",
        "Transcript 1Q26.pdf",
    )


def test_locate_guards_results_center_before_live_crawl(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    unsafe = IrConfig(
        ticker="NU",
        platform="mz",
        results_center_url="http://127.0.0.1/private",
    )

    def _empty_manifest(_root: Path, _ticker: str) -> list[ManifestEntry]:
        return []

    monkeypatch.setattr(transcript, "load_manifest", _empty_manifest)

    def _must_not_crawl(**_kw: object) -> list[CandidateDoc]:
        raise AssertionError("unsafe results-center URL reached the crawler")

    monkeypatch.setattr(transcript, "discover_history_hybrid", _must_not_crawl)
    with pytest.raises(UnsafeURLError):
        _locate_transcript(unsafe, "NU", 2026, 1, tmp_path)


# ---------------------------------------------------------------------------
# fetch_ir_transcript orchestration (monkeypatched — no network)
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, content: bytes, status: int = 200) -> None:
        self.content = content
        self.status_code = status
        self.status = status

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.content

    def geturl(self) -> str:
        return "https://files/abc"


class _FakeOpener:
    def open(self, *_args: object, **_kwargs: object) -> _FakeResp:
        return _FakeResp(b"%PDF-bytes")


def _cfg_nu(_ticker: str, _repo: Path | None = None) -> IrConfig:
    return _MZ_CFG


def test_fetch_returns_none_for_unconfigured_ticker() -> None:
    # A ticker with no IR config never launches a browser.
    assert fetch_ir_transcript("ZZZZ_NOT_A_TICKER", 2026, 1) is None


def test_fetch_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    body = "Prepared remarks here, plenty. We will now start the Q&A session. " + ("x " * 1000)

    def _located(*_a: object, **_k: object) -> tuple[str, str]:
        return ("https://files/abc", "Transcript 1Q26.pdf")

    def _extract(_content: bytes) -> str:
        return body

    monkeypatch.setattr(transcript, "get_config", _cfg_nu)
    monkeypatch.setattr(transcript, "_locate_transcript", _located)
    monkeypatch.setattr(transcript, "build_public_opener", _FakeOpener)
    monkeypatch.setattr(transcript, "_extract_pdf_text", _extract)

    hit = fetch_ir_transcript("NU", 2026, 1)
    assert hit is not None
    assert hit.filename == "Transcript 1Q26.pdf"
    assert hit.page_url == "https://files/abc"
    assert hit.qa_text.startswith("We will now start the Q&A session")
    assert "Prepared remarks" not in hit.qa_text


def test_fetch_returns_none_when_no_transcript_located(monkeypatch: pytest.MonkeyPatch) -> None:
    def _located(*_a: object, **_k: object) -> tuple[str, str] | None:
        return None

    monkeypatch.setattr(transcript, "get_config", _cfg_nu)
    monkeypatch.setattr(transcript, "_locate_transcript", _located)
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
