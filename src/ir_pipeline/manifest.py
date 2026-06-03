"""Idempotent writer for the IR-document URL manifest.

``execution/fetch_ir_documents.py`` downloads the documents listed in
``.tmp/ir_url_manifest/<TICKER>_urls.json``. This module is what *writes* that
file: the headless discovery orchestrator (``execution/discover_ir_documents.py``)
turns a crawl into ``ManifestEntry`` rows and merges them in.

The merge is **append-only, keyed by URL**: re-running discovery never loses or
clobbers a prior entry (including ones a human curated by hand), and a document
already in the manifest is a no-op. The on-disk shape is a JSON list whose dicts
are byte-compatible with what ``fetch_ir_documents.load_url_manifest`` reads
(``year``/``quarter``/``doc_type``/``url`` + optional ``fiscal_label``/``note``).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import cast


@dataclass(frozen=True)
class ManifestEntry:
    """One discoverable IR document.

    ``year``/``quarter`` are the discovery-time *best guess* (from link text /
    filename); they may be ``None`` when the crawl couldn't attribute a period.
    Authoritative period attribution happens later, from the downloaded file's
    content, in ``categorize_ir_uploads.py`` — so a null here is acceptable, not
    a failure. ``doc_type`` is the legacy alias
    (``press_release`` / ``presentation`` / ``transcript`` / ``supplement`` /
    ``investor_update``).
    """

    ticker: str
    doc_type: str
    url: str
    year: int | None = None
    quarter: str | None = None  # "Q1".."Q4"
    fiscal_label: str | None = None
    note: str | None = None


def manifest_path(repo_root: Path, ticker: str) -> Path:
    """``<repo_root>/.tmp/ir_url_manifest/<TICKER>_urls.json``."""
    return repo_root / ".tmp" / "ir_url_manifest" / f"{ticker.upper()}_urls.json"


def load_manifest(repo_root: Path, ticker: str) -> list[ManifestEntry]:
    """Load the persisted manifest for ``ticker`` (empty list when absent/garbled)."""
    path = manifest_path(repo_root, ticker)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(raw, list):
        return []
    out: list[ManifestEntry] = []
    for item in cast("list[object]", raw):
        if not isinstance(item, dict):
            continue
        d = cast("dict[str, object]", item)
        url = d.get("url")
        if not isinstance(url, str) or not url:
            continue
        out.append(
            ManifestEntry(
                ticker=str(d.get("ticker", ticker.upper())),
                doc_type=str(d.get("doc_type", "")),
                url=url,
                year=_as_int_or_none(d.get("year")),
                quarter=_as_str_or_none(d.get("quarter")),
                fiscal_label=_as_str_or_none(d.get("fiscal_label")),
                note=_as_str_or_none(d.get("note")),
            )
        )
    return out


def merge_write(repo_root: Path, ticker: str, new: list[ManifestEntry]) -> tuple[int, int]:
    """Union the persisted manifest with ``new``, keyed by URL; persist atomically.

    Prior entries win on a URL collision (a curated entry is never overwritten by
    a fresh discovery). Returns ``(added, total)``.
    """
    existing = load_manifest(repo_root, ticker)
    seen = {e.url for e in existing}
    merged = list(existing)
    added = 0
    for e in new:
        if not e.url or e.url in seen:
            continue
        seen.add(e.url)
        merged.append(e)
        added += 1

    path = manifest_path(repo_root, ticker)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps([_to_dict(e) for e in merged], indent=2)
    # Atomic replace so a concurrent reader never sees a half-written file.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)
    return added, len(merged)


def _to_dict(e: ManifestEntry) -> dict[str, object]:
    """Serialize in the directive's documented key order (cosmetic — readers key)."""
    return {
        "ticker": e.ticker,
        "year": e.year,
        "quarter": e.quarter,
        "doc_type": e.doc_type,
        "url": e.url,
        "fiscal_label": e.fiscal_label,
        "note": e.note,
    }


def _as_int_or_none(v: object) -> int | None:
    if isinstance(v, bool):  # bool is an int subclass — reject it explicitly
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None


def _as_str_or_none(v: object) -> str | None:
    return v if isinstance(v, str) and v != "" else None
