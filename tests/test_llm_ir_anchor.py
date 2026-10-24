"""Behavioral tests for the IR-narrative anchor in src/llm/anchors.py.

PR #180 added `load_ir_anchor` + the IR slot on `compose_anchor_block` but
shipped only a layout/import test. These lock in the behavior that actually
matters for prompt quality: the company-bias framing header is always present,
the output is capped (downweighted vs the analyst's own thesis/bear blocks),
doctype priority + recency selection work, and missing/empty caches degrade to
"" rather than raising.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from compute.ir_narrative import extract_for_ticker
from llm.anchors import (
    IR_ANCHOR_CHAR_CAP,
    compose_anchor_block,
    load_ir_anchor,
)

# The bias header is an internal constant; assert on its stable public prefix
# rather than importing the private symbol (keeps the test strict-pyright clean).
_HEADER_PREFIX = "## IR ANCHOR (company-provided framing"


def _write_ir(repo_root: Path, ticker: str, filename: str, body: str) -> Path:
    """Build a real envelope from synthetic source text through the producer."""
    doctype, period = Path(filename).stem.split("__", 1)
    source = repo_root / "ir_documents" / ticker / period / f"{doctype}__abcd1234.pdf"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(body if not body.strip() else body * (200 // len(body) + 1))

    def extract(payload: bytes, _source_ref: str) -> str:
        return payload.decode()

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("compute.ir_narrative.project_source_text", extract)
        assert extract_for_ticker(repo_root, ticker)["failed"] == 0
    return repo_root / "data" / "ir_narrative" / ticker / Path(filename).with_suffix(".json")


# --- graceful-empty paths ----------------------------------------------------


def test_no_ir_dir_returns_empty(tmp_path: Path) -> None:
    assert load_ir_anchor(tmp_path, "FOO") == ""


def test_dir_without_matching_files_returns_empty(tmp_path: Path) -> None:
    d = tmp_path / "data" / "ir_narrative" / "FOO"
    d.mkdir(parents=True)
    # Non-.txt and a non-narrative doctype — neither should be picked up.
    (d / "notes.md").write_text("ignored", encoding="utf-8")
    (d / "ir_press_release__2025-Q4.txt").write_text("just numbers", encoding="utf-8")
    assert load_ir_anchor(tmp_path, "FOO") == ""


def test_empty_extraction_returns_explicit_gap(tmp_path: Path) -> None:
    _write_ir(tmp_path, "FOO", "ir_presentation__2025-Q4.txt", "   \n\n  ")
    assert "UNAVAILABLE" in load_ir_anchor(tmp_path, "FOO")


# --- positive path + bias framing -------------------------------------------


def test_positive_path_has_header_source_tag_and_body(tmp_path: Path) -> None:
    _write_ir(tmp_path, "FOO", "ir_presentation__2025-Q4.txt", "Our TAM is $1 trillion.")
    out = load_ir_anchor(tmp_path, "FOO")
    assert out.startswith(_HEADER_PREFIX)
    assert "_Source: ir_presentation__2025-Q4_" in out
    assert "Our TAM is $1 trillion." in out


def test_bias_framing_language_present(tmp_path: Path) -> None:
    # The whole point of #180: company framing must be flagged as biased so the
    # LLM forms a POV instead of parroting it. Guard the distinctive phrases.
    _write_ir(tmp_path, "FOO", "ir_presentation__2025-Q4.txt", "body")
    out = load_ir_anchor(tmp_path, "FOO")
    assert "USE WITH SKEPTICISM" in out
    assert "Form your own POV" in out


def test_ticker_is_case_insensitive(tmp_path: Path) -> None:
    _write_ir(tmp_path, "FOO", "ir_presentation__2025-Q4.txt", "body text here")
    assert load_ir_anchor(tmp_path, "foo") != ""


# --- char cap (downweighting) -----------------------------------------------


def test_long_body_is_truncated_near_cap(tmp_path: Path) -> None:
    _write_ir(tmp_path, "FOO", "ir_presentation__2025-Q4.txt", "x" * 5000)
    out = load_ir_anchor(tmp_path, "FOO")
    assert "[...truncated]" in out
    # The cap is approximate — the truncation marker adds a few chars past it.
    assert len(out) <= IR_ANCHOR_CHAR_CAP + len("\n[...truncated]")


def test_smaller_custom_cap_yields_shorter_output(tmp_path: Path) -> None:
    _write_ir(tmp_path, "FOO", "ir_presentation__2025-Q4.txt", "y" * 5000)
    big = load_ir_anchor(tmp_path, "FOO", char_cap=2000)
    small = load_ir_anchor(tmp_path, "FOO", char_cap=800)
    assert len(small) < len(big)
    # Header framing survives even a tight cap.
    assert "USE WITH SKEPTICISM" in small


# --- selection logic ---------------------------------------------------------


def test_doctype_priority_presentation_beats_event(tmp_path: Path) -> None:
    _write_ir(tmp_path, "FOO", "ir_event__2025-06-01.txt", "EVENT DECK CONTENT")
    _write_ir(tmp_path, "FOO", "ir_presentation__2025-Q1.txt", "PRESENTATION CONTENT")
    out = load_ir_anchor(tmp_path, "FOO")
    assert "PRESENTATION CONTENT" in out
    assert "EVENT DECK CONTENT" not in out
    assert "_Source: ir_presentation__2025-Q1_" in out


def test_within_doctype_latest_period_wins(tmp_path: Path) -> None:
    _write_ir(tmp_path, "FOO", "ir_presentation__2024-Q4.txt", "OLD DECK")
    _write_ir(tmp_path, "FOO", "ir_presentation__2025-Q4.txt", "NEW DECK")
    out = load_ir_anchor(tmp_path, "FOO")
    assert "NEW DECK" in out
    assert "OLD DECK" not in out
    assert "_Source: ir_presentation__2025-Q4_" in out


def test_falls_through_to_lowest_priority_doctype(tmp_path: Path) -> None:
    _write_ir(tmp_path, "FOO", "ir_investor_update__2025-Q4.txt", "LETTER CONTENT")
    out = load_ir_anchor(tmp_path, "FOO")
    assert "LETTER CONTENT" in out
    assert "_Source: ir_investor_update__2025-Q4_" in out


# --- compose_anchor_block (IR slot) -----------------------------------------


def test_compose_all_empty_returns_empty() -> None:
    assert compose_anchor_block("", "", "") == ""


def test_compose_legacy_two_arg_call_still_works() -> None:
    out = compose_anchor_block("THESIS", "BEAR")
    assert "THESIS" in out
    assert "BEAR" in out


def test_compose_orders_ir_last_with_separators() -> None:
    out = compose_anchor_block("THESIS", "BEAR", "IRBLOCK")
    assert out.index("THESIS") < out.index("BEAR") < out.index("IRBLOCK")
    assert "---" in out


def test_compose_with_only_ir_present() -> None:
    out = compose_anchor_block("", "", "IRONLY")
    assert "IRONLY" in out


def test_new_uncached_period_blocks_older_cached_fallback(tmp_path: Path) -> None:
    _write_ir(tmp_path, "FOO", "ir_presentation__2025-Q3.txt", "OLD VALID DECK")
    source = tmp_path / "ir_documents/FOO/2025-Q4/ir_presentation__aaaaaaaa.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"new source not yet extracted")
    output = load_ir_anchor(tmp_path, "FOO")
    assert "UNAVAILABLE" in output
    assert "2025-Q4" in output
    assert "OLD VALID DECK" not in output


def test_invalid_preferred_cache_does_not_fall_back(tmp_path: Path) -> None:
    _write_ir(tmp_path, "FOO", "ir_event__2025-06-01.txt", "VALID EVENT DECK")
    cache = _write_ir(tmp_path, "FOO", "ir_presentation__2025-Q4.txt", "PREFERRED DECK")
    cache.write_text("{invalid json")
    output = load_ir_anchor(tmp_path, "FOO")
    assert "UNAVAILABLE" in output
    assert "ir_presentation__2025-Q4" in output
    assert "VALID EVENT DECK" not in output
    assert "PREFERRED DECK" not in output


def test_unreadable_newer_directory_cannot_select_older_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    from compute.ir_narrative import has_pending_narrative

    _write_ir(tmp_path, "FOO", "ir_presentation__2025-Q3.txt", "OLDER VERIFIED DECK")
    newer = tmp_path / "ir_documents/FOO/2025-Q4"
    newer.mkdir()
    original = os.scandir

    def denied(path: str | Path):
        if Path(path) == newer:
            raise PermissionError("newer period cannot be enumerated")
        return original(path)

    monkeypatch.setattr(os, "scandir", denied)
    output = load_ir_anchor(tmp_path, "FOO")
    assert "UNAVAILABLE" in output
    assert "OLDER VERIFIED DECK" not in output
    assert has_pending_narrative(tmp_path, "FOO")


def test_newer_group_arriving_during_validation_returns_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from provenance import immutable_artifact

    _write_ir(tmp_path, "FOO", "ir_presentation__2025-Q3.txt", "OLDER VERIFIED DECK")
    source = tmp_path / "ir_documents/FOO/2025-Q3/ir_presentation__abcd1234.pdf"
    newer = tmp_path / "ir_documents/FOO/2025-Q4/ir_presentation__bbbbbbbb.pdf"
    original = immutable_artifact.read_stable_artifact

    def read(path: Path, *, allowed_root: Path | None = None, max_bytes: int | None = None):
        result = original(path, allowed_root=allowed_root, max_bytes=max_bytes)
        if path == source and not newer.exists():
            newer.parent.mkdir()
            newer.write_bytes(b"arrived during verification")
        return result

    monkeypatch.setattr(immutable_artifact, "read_stable_artifact", read)
    output = load_ir_anchor(tmp_path, "FOO")
    assert "UNAVAILABLE" in output
    assert "OLDER VERIFIED DECK" not in output
