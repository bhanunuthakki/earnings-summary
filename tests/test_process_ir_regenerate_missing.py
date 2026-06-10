"""Tests for `process_ir_documents.py --regenerate-missing` (run_for_ticker).

Regression coverage for the gap that leaves §6 Say-Do empty: transcripts are
registered processed=True at ingest (index_manager.register_transcript, "legacy
flow already processed") without a `_summary.txt` ever being written, so the
default get_unprocessed_documents path skips them forever — no summary, no
build_saydo_pairs input, no §6 cards.

`--regenerate-missing` re-includes registered quarterly docs whose summary cache
file is ABSENT (keyed on cache-file existence, not the processed flag), while
the existing process_document cache-hit early-return skips docs that already
have a summary (no re-billing).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import cast

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import process_ir_documents as pir  # noqa: E402

# --- typed test doubles (pyright-strict friendly; no bare untyped lambdas) ----


def _noop(*_args: object, **_kwargs: object) -> None:
    return None


def _empty_anchor(*_args: object, **_kwargs: object) -> str:
    return ""


def _fixed_transcript_text(*_args: object, **_kwargs: object) -> str:
    return "transcript body"


def _upper(ticker: str) -> str:
    return ticker.upper()


class _Patched:
    """What the `patched` fixture exposes: the isolated cache dir + a recorder
    of every transcript body handed to the (faked) summary function."""

    def __init__(self, cache_dir: Path, llm_calls: list[str]) -> None:
        self.cache_dir = cache_dir
        self.llm_calls = llm_calls


@pytest.fixture
def patched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Patched:
    """Isolate CACHE_DIR and stub the LLM call, anchor IO, rate-limit sleep,
    index writes, and ticker resolution so run_for_ticker is hermetic."""
    cache = tmp_path / ".tmp"
    cache.mkdir()
    llm_calls: list[str] = []

    def fake_summary(text: str, anchor_block: str = "") -> str:
        llm_calls.append(text)
        return "SUMMARY: " + text[:20]

    monkeypatch.setattr(pir, "CACHE_DIR", cache)
    monkeypatch.setattr(pir, "resolve_ticker", _upper)
    monkeypatch.setattr(pir.time, "sleep", _noop)
    monkeypatch.setattr(pir, "compose_anchor_block", _empty_anchor)
    monkeypatch.setattr(pir, "load_thesis_anchor", _empty_anchor)
    monkeypatch.setattr(pir, "load_bear_anchor", _empty_anchor)
    monkeypatch.setattr(pir, "load_ir_anchor", _empty_anchor)
    monkeypatch.setattr(pir, "_mark_processed", _noop)
    monkeypatch.setattr(pir, "extract_text", _fixed_transcript_text)
    monkeypatch.setitem(
        cast("dict[str, object]", pir.DOC_TYPE_CONFIG["transcript"]), "llm_fn", fake_summary
    )
    return _Patched(cache, llm_calls)


def _transcript_doc(
    tmp_path: Path, ticker: str = "UBER", year: int = 2026, quarter: str = "Q1"
) -> dict[str, object]:
    """A registered transcript doc dict (processed=True) with a real local_path."""
    f = tmp_path / f"{ticker}_{quarter}_{year}.txt"
    f.write_text("raw transcript", encoding="utf-8")
    return {
        "ticker": ticker,
        "year": year,
        "quarter": quarter,
        "doc_type": "transcript",
        "local_path": str(f),
        "processed": True,
    }


def test_regenerate_missing_summarizes_processed_doc_without_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patched: _Patched
) -> None:
    doc = _transcript_doc(tmp_path)

    def fake_all(_ticker: str) -> list[dict[str, object]]:
        return [doc]

    def fake_unprocessed(_ticker: str | None = None) -> list[dict[str, object]]:
        return []

    monkeypatch.setattr(pir.index_manager, "get_documents_for_ticker", fake_all)
    # Prove the default path would have skipped it (processed=True).
    monkeypatch.setattr(pir.index_manager, "get_unprocessed_documents", fake_unprocessed)

    pir.run_for_ticker("UBER", regenerate_missing=True)

    out = patched.cache_dir / "UBER_Q1_2026_summary.txt"
    assert out.exists(), "a processed transcript with no summary cache should be regenerated"
    assert out.read_text(encoding="utf-8").startswith("SUMMARY:")
    assert patched.llm_calls == ["transcript body"]


def test_regenerate_missing_skips_doc_with_existing_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patched: _Patched
) -> None:
    doc = _transcript_doc(tmp_path)
    # Pre-existing summary → must be skipped, never re-billed.
    (patched.cache_dir / "UBER_Q1_2026_summary.txt").write_text("PRIOR SUMMARY", encoding="utf-8")

    def fake_all(_ticker: str) -> list[dict[str, object]]:
        return [doc]

    monkeypatch.setattr(pir.index_manager, "get_documents_for_ticker", fake_all)

    pir.run_for_ticker("UBER", regenerate_missing=True)

    assert patched.llm_calls == [], "a doc that already has a summary must not be re-summarized"
    assert (patched.cache_dir / "UBER_Q1_2026_summary.txt").read_text(
        encoding="utf-8"
    ) == "PRIOR SUMMARY"


def test_default_path_skips_processed_doc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patched: _Patched
) -> None:
    """Without --regenerate-missing, a processed doc is never summarized — the
    bug this whole change exists to fix, pinned so a future refactor can't
    silently reintroduce it."""

    def fake_unprocessed(_ticker: str | None = None) -> list[dict[str, object]]:
        return []

    monkeypatch.setattr(pir.index_manager, "get_unprocessed_documents", fake_unprocessed)
    monkeypatch.setattr(pir.index_manager, "get_unprocessed_events", fake_unprocessed)

    pir.run_for_ticker("UBER", regenerate_missing=False)

    assert patched.llm_calls == []
    assert not (patched.cache_dir / "UBER_Q1_2026_summary.txt").exists()


def test_regenerate_missing_excludes_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patched: _Patched
) -> None:
    """The regenerate path is scoped to quarterly docs (the Say-Do inputs); an
    `event` doc (separate keyspace, no quarter) must not be pulled in."""
    transcript = _transcript_doc(tmp_path)
    event: dict[str, object] = {
        "ticker": "UBER",
        "doc_type": "event",
        "event_date": "2026-05-01",
        "local_path": str(tmp_path / "evt.txt"),
        "processed": True,
    }

    def fake_all(_ticker: str) -> list[dict[str, object]]:
        return [transcript, event]

    monkeypatch.setattr(pir.index_manager, "get_documents_for_ticker", fake_all)

    pir.run_for_ticker("UBER", regenerate_missing=True)

    # Only the transcript was summarized; the event was filtered out (no KeyError
    # on its missing quarter/year).
    assert patched.llm_calls == ["transcript body"]
    assert (patched.cache_dir / "UBER_Q1_2026_summary.txt").exists()
