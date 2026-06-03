"""Tests for src/ir_pipeline/manifest.py + ir_url_overrides.py (PR1 foundation).

The manifest writer is the bridge between headless discovery and
``execution/fetch_ir_documents.py``: it must merge new discoveries into the
per-ticker URL manifest idempotently (URL-keyed, prior wins) and write the exact
JSON shape the downloader reads.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from ir_pipeline.ir_url_overrides import IR_URL_OVERRIDES, resolve_ir_url  # noqa: E402
from ir_pipeline.manifest import (  # noqa: E402
    ManifestEntry,
    load_manifest,
    manifest_path,
    merge_write,
)


def _entry(
    url: str,
    doc_type: str = "press_release",
    *,
    year: int | None = None,
    quarter: str | None = None,
    note: str | None = None,
) -> ManifestEntry:
    return ManifestEntry(
        ticker="NU", doc_type=doc_type, url=url, year=year, quarter=quarter, note=note
    )


def test_merge_write_creates_manifest_and_returns_counts(tmp_path: Path) -> None:
    added, total = merge_write(
        tmp_path,
        "NU",
        [_entry("https://x/a.pdf", year=2025, quarter="Q3"), _entry("https://x/b.pdf")],
    )
    assert (added, total) == (2, 2)
    path = manifest_path(tmp_path, "NU")
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert [d["url"] for d in data] == ["https://x/a.pdf", "https://x/b.pdf"]
    # Shape the downloader reads: year/quarter/doc_type + optional fiscal_label/note.
    assert data[0]["year"] == 2025
    assert data[0]["quarter"] == "Q3"
    assert data[0]["doc_type"] == "press_release"
    assert "fiscal_label" in data[0]
    assert "note" in data[0]


def test_merge_write_is_idempotent_on_repeat(tmp_path: Path) -> None:
    merge_write(tmp_path, "NU", [_entry("https://x/a.pdf")])
    added, total = merge_write(tmp_path, "NU", [_entry("https://x/a.pdf")])
    assert added == 0
    assert total == 1


def test_merge_write_unions_and_prior_wins(tmp_path: Path) -> None:
    merge_write(tmp_path, "NU", [_entry("https://x/a.pdf", note="curated")])
    # Same URL with a different note must NOT overwrite the prior (curated) entry.
    added, total = merge_write(
        tmp_path,
        "NU",
        [_entry("https://x/a.pdf", note="rediscovered"), _entry("https://x/c.pdf")],
    )
    assert added == 1
    assert total == 2
    by_url = {e.url: e for e in load_manifest(tmp_path, "NU")}
    assert by_url["https://x/a.pdf"].note == "curated"


def test_merge_write_skips_blank_urls(tmp_path: Path) -> None:
    added, total = merge_write(tmp_path, "NU", [_entry(""), _entry("https://x/a.pdf")])
    assert added == 1
    assert total == 1


def test_load_manifest_absent_is_empty(tmp_path: Path) -> None:
    assert load_manifest(tmp_path, "ZZ") == []


def test_load_manifest_tolerates_garbage(tmp_path: Path) -> None:
    path = manifest_path(tmp_path, "NU")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert load_manifest(tmp_path, "NU") == []


def test_load_manifest_skips_entries_without_url(tmp_path: Path) -> None:
    path = manifest_path(tmp_path, "NU")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([{"doc_type": "x"}, {"url": "https://x/a.pdf", "doc_type": "deck"}]),
        encoding="utf-8",
    )
    assert [e.url for e in load_manifest(tmp_path, "NU")] == ["https://x/a.pdf"]


def test_resolve_ir_url_precedence() -> None:
    # Override wins over config + DB.
    assert resolve_ir_url("NU", "https://db", "https://cfg") == IR_URL_OVERRIDES["NU"]
    # No override: config URL beats DB URL.
    assert resolve_ir_url("ZZZ", "https://db", "https://cfg") == "https://cfg"
    # No override / no config: DB URL.
    assert resolve_ir_url("ZZZ", "https://db") == "https://db"
    # Nothing usable → None.
    assert resolve_ir_url("ZZZ", None) is None
    assert resolve_ir_url("ZZZ", "   ") is None


def test_resolve_ir_url_is_case_insensitive_on_ticker() -> None:
    assert resolve_ir_url("nu", None) == IR_URL_OVERRIDES["NU"]
