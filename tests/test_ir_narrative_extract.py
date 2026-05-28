"""Tests for src/compute/ir_narrative.py — the IR-deck narrative extractor that
feeds load_ir_anchor.

Covers the pure helpers (_normalize, _discover_sources) and the
extract_for_ticker orchestration with a stubbed PDF text extractor, so the test
path doesn't depend on pypdf or on real binary PDFs. PR #180 shipped this module
with no behavioral tests.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from compute.ir_narrative import (
    _discover_sources,  # pyright: ignore[reportPrivateUsage]
    _normalize,  # pyright: ignore[reportPrivateUsage]
    extract_for_ticker,
)

# --- _normalize --------------------------------------------------------------


def test_normalize_collapses_whitespace_runs() -> None:
    assert _normalize("foo     bar\t\tbaz") == "foo bar baz"


def test_normalize_drops_noise_lines() -> None:
    raw = (
        "Real content line\n"
        "Page 4 of 18\n"
        "© 2025 Meta Platforms, Inc.\n"
        "CONFIDENTIAL\n"
        "All Rights Reserved\n"
        "More content"
    )
    out = _normalize(raw)
    assert "Real content line" in out
    assert "More content" in out
    assert "Page 4 of 18" not in out
    assert "Meta Platforms" not in out
    assert "CONFIDENTIAL" not in out
    assert "All Rights Reserved" not in out


def test_normalize_collapses_blank_line_runs() -> None:
    assert _normalize("a\n\n\n\n\nb") == "a\n\nb"


# --- _discover_sources -------------------------------------------------------


def _touch_pdf(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"%PDF-1.4 stub")


def test_discover_periodized_sources(tmp_path: Path) -> None:
    _touch_pdf(tmp_path / "ir_documents" / "FOO" / "2025-Q4" / "ir_presentation__abcd1234.pdf")
    found = _discover_sources(tmp_path, "FOO")
    assert ("ir_presentation", "2025-Q4") in found
    assert len(found[("ir_presentation", "2025-Q4")]) == 1


def test_discover_filters_non_narrative_doctypes(tmp_path: Path) -> None:
    _touch_pdf(tmp_path / "ir_documents" / "FOO" / "2025-Q4" / "ir_press_release__abcd1234.pdf")
    assert _discover_sources(tmp_path, "FOO") == {}


def test_discover_event_sources(tmp_path: Path) -> None:
    _touch_pdf(
        tmp_path / "ir_documents" / "_events" / "FOO" / "2025-06-01" / "ir_event__abcd1234.pdf"
    )
    found = _discover_sources(tmp_path, "FOO")
    assert ("ir_event", "2025-06-01") in found


def test_discover_groups_multiple_pdfs_per_period(tmp_path: Path) -> None:
    base = tmp_path / "ir_documents" / "FOO" / "2025-Q4"
    _touch_pdf(base / "ir_presentation__aaaaaaaa.pdf")
    _touch_pdf(base / "ir_presentation__bbbbbbbb.pdf")
    found = _discover_sources(tmp_path, "FOO")
    assert len(found[("ir_presentation", "2025-Q4")]) == 2


def test_discover_no_tree_returns_empty(tmp_path: Path) -> None:
    assert _discover_sources(tmp_path, "FOO") == {}


# --- extract_for_ticker (stubbed PDF extractor) ------------------------------


def _install_fake_parser(monkeypatch: pytest.MonkeyPatch, text_by_name: dict[str, str]) -> None:
    """Inject a fake `parser` module so extract_for_ticker's late
    `from parser import extract_text_from_pdf` resolves to our stub — no pypdf."""
    fake = types.ModuleType("parser")

    def _fake_extract(path: str) -> str:
        return text_by_name.get(Path(path).name, "")

    fake.extract_text_from_pdf = _fake_extract  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "parser", fake)


def test_extract_writes_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _touch_pdf(tmp_path / "ir_documents" / "FOO" / "2025-Q4" / "ir_presentation__abcd1234.pdf")
    _install_fake_parser(
        monkeypatch,
        {"ir_presentation__abcd1234.pdf": "Strategic priority: scale the platform. " * 20},
    )
    counts = extract_for_ticker(tmp_path, "FOO")
    assert counts["processed"] == 1
    cache = tmp_path / "data" / "ir_narrative" / "FOO" / "ir_presentation__2025-Q4.txt"
    assert cache.exists()
    assert "Strategic priority" in cache.read_text(encoding="utf-8")


def test_extract_skips_empty_pdf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _touch_pdf(tmp_path / "ir_documents" / "FOO" / "2025-Q4" / "ir_presentation__abcd1234.pdf")
    # Below _MIN_CACHE_BYTES (200) → counted as skipped_empty, no cache written.
    _install_fake_parser(monkeypatch, {"ir_presentation__abcd1234.pdf": "tiny"})
    counts = extract_for_ticker(tmp_path, "FOO")
    assert counts["skipped_empty"] == 1
    assert counts["processed"] == 0
    cache = tmp_path / "data" / "ir_narrative" / "FOO" / "ir_presentation__2025-Q4.txt"
    assert not cache.exists()


def test_extract_merges_multiple_pdfs_with_source_break(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "ir_documents" / "FOO" / "2025-Q4"
    _touch_pdf(base / "ir_presentation__aaaaaaaa.pdf")
    _touch_pdf(base / "ir_presentation__bbbbbbbb.pdf")
    _install_fake_parser(
        monkeypatch,
        {
            "ir_presentation__aaaaaaaa.pdf": "DECK ALPHA narrative content. " * 10,
            "ir_presentation__bbbbbbbb.pdf": "DECK BRAVO narrative content. " * 10,
        },
    )
    counts = extract_for_ticker(tmp_path, "FOO")
    assert counts["processed"] == 1
    cache = tmp_path / "data" / "ir_narrative" / "FOO" / "ir_presentation__2025-Q4.txt"
    text = cache.read_text(encoding="utf-8")
    assert "DECK ALPHA" in text
    assert "DECK BRAVO" in text
    assert "=== SOURCE BREAK ===" in text


def test_extract_idempotent_cache_hit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _touch_pdf(tmp_path / "ir_documents" / "FOO" / "2025-Q4" / "ir_presentation__abcd1234.pdf")
    _install_fake_parser(monkeypatch, {"ir_presentation__abcd1234.pdf": "content " * 50})
    extract_for_ticker(tmp_path, "FOO")
    counts2 = extract_for_ticker(tmp_path, "FOO")
    assert counts2["cached"] == 1
    assert counts2["processed"] == 0


def test_extract_refresh_reprocesses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _touch_pdf(tmp_path / "ir_documents" / "FOO" / "2025-Q4" / "ir_presentation__abcd1234.pdf")
    _install_fake_parser(monkeypatch, {"ir_presentation__abcd1234.pdf": "content " * 50})
    extract_for_ticker(tmp_path, "FOO")
    counts2 = extract_for_ticker(tmp_path, "FOO", refresh=True)
    assert counts2["processed"] == 1
    assert counts2["cached"] == 0


def test_extract_no_sources_returns_zero_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_parser(monkeypatch, {})
    counts = extract_for_ticker(tmp_path, "FOO")
    assert counts == {"processed": 0, "cached": 0, "failed": 0, "skipped_empty": 0}
