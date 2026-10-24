"""
src/compute/ir_narrative.py
---------------------------
Extract narrative (qualitative) text from per-ticker IR PDFs and cache it for
LLM-prompt consumption.

Source: ir_documents/<TICKER>/<period>/ir_<doctype>__<sha8>.pdf and
        ir_documents/_events/<TICKER>/<event_date>/ir_event__<sha8>.pdf.
Cache:  data/ir_narrative/<TICKER>/<doctype>__<period>.json
Legacy text caches are preserved, but are not verified prompt inputs.

This is the SOURCE-TEXT companion to ``src/table_extractors/investor_decks.py``:
that module extracts numeric strategic targets from the same decks; this one
preserves the narrative framing (TAM language, strategic priorities phrasing,
competitive positioning) so the prompt context can engage with how management
chooses to position the business — not just the numbers they commit to.

Doctypes processed (also the priority order ``load_ir_anchor`` walks):

  - ir_presentation    — earnings decks; richest source of strategic framing
  - ir_event           — investor days, capital-markets days, conference decks
  - ir_investor_update — letters / quarterly updates with narrative framing

Skipped on purpose:

  - ir_press_release   — pure quarterly numbers; condensed elsewhere
  - ir_transcript      — already fed directly into the transcript prompt

When more than one source PDF exists for the same (doctype, period) pair
(e.g. META frequently publishes BOTH a financial-highlights deck and a
slide deck for the same quarter), the extracted texts are concatenated into
a single cache file with a ``=== SOURCE BREAK ===`` delimiter so the loader
sees the full narrative for that period in one read.

CLI:
    python execution/sqlite_bootstrap.py src/compute/ir_narrative.py --ticker META
    python execution/sqlite_bootstrap.py src/compute/ir_narrative.py --all
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import stat
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from provenance.fulltext_backfill import project_source_text, source_text_projection_identity

if TYPE_CHECKING:
    from provenance.immutable_artifact import ImmutableArtifactSnapshot

log = logging.getLogger(__name__)

# Subset of intake's DocType prefixes that carry narrative content worth
# caching. Sorted in the loader's preference order — the first prefix the
# ticker has wins when ``load_ir_anchor`` picks "the most relevant deck".
_NARRATIVE_DOCTYPES: tuple[str, ...] = (
    "ir_presentation",
    "ir_event",
    "ir_investor_update",
)

_FILENAME_RX = re.compile(
    r"^(?P<doctype>ir_(?:presentation|event|investor_update))"
    r"__(?P<sha8>[0-9a-f]{8})\.pdf$"
)

# pypdf returns text with whitespace artifacts and per-slide footers. Normalize
# so the cached text is what the LLM actually reads, not raw pypdf output.
_WS_RUN = re.compile(r"[ \t]+")
_BLANK_LINES = re.compile(r"\n{3,}")
_NOISE_LINE = re.compile(
    r"^(?:"
    r"page\s+\d+(?:\s+of\s+\d+)?|"  # "Page 4 of 18"
    r"\d+\s*/\s*\d+|"  # "4 / 18"
    r"©.*|"  # "© 2025 Meta Platforms, Inc."
    r"copyright.*|"
    r"confidential.*|"
    r"all rights reserved.*"
    r")$",
    re.IGNORECASE,
)

_MIN_CACHE_BYTES = 200  # below this the deck has no usable narrative
_SOURCE_BREAK = "\n\n=== SOURCE BREAK ===\n\n"


def _normalize(text: str) -> str:
    """Collapse pypdf whitespace + drop page-number / copyright noise lines."""
    out_lines: list[str] = []
    for raw_line in text.split("\n"):
        line = _WS_RUN.sub(" ", raw_line).strip()
        if not line:
            out_lines.append("")
            continue
        if _NOISE_LINE.match(line):
            continue
        out_lines.append(line)
    return _BLANK_LINES.sub("\n\n", "\n".join(out_lines)).strip()


def _cache_path_for(repo_root: Path, ticker: str, doctype: str, period: str) -> Path:
    """One current text-and-manifest envelope per narrative group."""
    out_dir = repo_root / "data" / "ir_narrative" / ticker.upper()
    return out_dir / f"{doctype}__{period}.json"


def _entries(directory: Path, *, optional: bool = False) -> list[Path]:
    """Only absent optional roots are empty; unreadable directories are gaps."""
    try:
        metadata = directory.stat()
    except FileNotFoundError:
        if optional:
            return []
        raise
    if not stat.S_ISDIR(metadata.st_mode):
        raise OSError("expected a narrative directory")
    return sorted(directory.iterdir())


def _discover_sources(repo_root: Path, ticker: str) -> dict[tuple[str, str], list[Path]]:
    """Discover every matching source in the established period/event order."""
    base = repo_root / "ir_documents"
    out: dict[tuple[str, str], list[Path]] = {}
    for root, events_only in (
        (base / ticker.upper(), False),
        (base / "_events" / ticker.upper(), True),
    ):
        for period_dir in _entries(root, optional=True):
            if not stat.S_ISDIR(period_dir.stat().st_mode):
                continue
            for pdf in _entries(period_dir):
                match = _FILENAME_RX.match(pdf.name)
                if match is None:
                    continue
                doctype = match.group("doctype")
                if events_only and doctype != "ir_event":
                    continue
                out.setdefault((doctype, period_dir.name), []).append(pdf)
    return out


class _Source(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str
    sha256: str
    size_bytes: int = Field(ge=0)


class _Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[3] = 3
    ticker: str
    doctype: str
    period: str
    method: str
    sources: list[_Source]
    text: str
    text_sha256: str


@dataclass(frozen=True)
class NarrativeCache:
    """Local extraction validity; never financial admission or archive coverage."""

    status: Literal["valid", "unavailable"]
    source_tag: str
    reason: str = ""
    body: str = ""


def _method_identity() -> str:
    payload = {
        "revision": 3,
        "source_text_identity": source_text_projection_identity("source.pdf"),
        "normalizer_code": sha256(Path(__file__).read_bytes()).hexdigest(),
        "minimum_chars": _MIN_CACHE_BYTES,
        "source_break": _SOURCE_BREAK,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _read_sources(
    repo_root: Path, pdfs: list[Path]
) -> Iterator[tuple[ImmutableArtifactSnapshot, bytes]]:
    # Project imports stay lazy for this module's standalone CLI entrypoint.
    from provenance.immutable_artifact import read_stable_artifact

    for pdf in pdfs:
        yield read_stable_artifact(pdf, allowed_root=repo_root)


def _assert_population_unchanged(
    repo_root: Path,
    ticker: str,
    key: tuple[str, str],
    pdfs: list[Path],
    snapshots: list[ImmutableArtifactSnapshot],
) -> None:
    from provenance.immutable_artifact import assert_artifact_unchanged

    if _discover_sources(repo_root, ticker).get(key, []) != pdfs:
        raise ValueError("source population changed")
    for snapshot in snapshots:
        assert_artifact_unchanged(snapshot)


def _read_cache(
    repo_root: Path, ticker: str, key: tuple[str, str], pdfs: list[Path]
) -> NarrativeCache:
    from provenance.immutable_artifact import (
        ImmutableArtifactConflictError,
        read_stable_artifact,
    )

    doctype, period = key
    tag = f"{doctype}__{period}"
    if not pdfs:
        return NarrativeCache("unavailable", tag, "missing_sources")
    cache = _cache_path_for(repo_root, ticker, doctype, period)
    try:
        _, raw = read_stable_artifact(cache, allowed_root=repo_root)
        envelope = _Envelope.model_validate_json(raw)
        if (envelope.ticker, envelope.doctype, envelope.period) != (ticker.upper(), *key):
            return NarrativeCache("unavailable", tag, "identity_mismatch")
        if envelope.method != _method_identity():
            return NarrativeCache("unavailable", tag, "method_changed")
        if sha256(envelope.text.encode("utf-8")).hexdigest() != envelope.text_sha256:
            return NarrativeCache("unavailable", tag, "output_changed")
        snapshots: list[ImmutableArtifactSnapshot] = []
        actual: list[tuple[str, str, int]] = []
        for pdf, (snapshot, _) in zip(pdfs, _read_sources(repo_root, pdfs), strict=True):
            snapshots.append(snapshot)
            actual.append(
                (str(pdf.relative_to(repo_root)), snapshot.file_sha256, snapshot.size_bytes)
            )
        recorded = [(s.path, s.sha256, s.size_bytes) for s in envelope.sources]
        if actual != recorded:
            return NarrativeCache("unavailable", tag, "sources_changed")
        _assert_population_unchanged(repo_root, ticker, key, pdfs, snapshots)
        if not envelope.text.strip() or not envelope.sources:
            return NarrativeCache("unavailable", tag, "invalid_disposition")
        return NarrativeCache("valid", tag, body=envelope.text)
    except FileNotFoundError:
        return NarrativeCache("unavailable", tag, "missing_cache_or_source")
    except (OSError, ValueError, ImmutableArtifactConflictError):
        return NarrativeCache("unavailable", tag, "unreadable_invalid_or_unstable")


def _selected_key(
    repo_root: Path, ticker: str, sources: dict[tuple[str, str], list[Path]]
) -> tuple[str, str] | None:
    keys = set(sources)
    base = repo_root / "data" / "ir_narrative" / ticker.upper()
    for path in _entries(base, optional=True):
        doctype, sep, period = path.stem.partition("__")
        if sep and period and doctype in _NARRATIVE_DOCTYPES and path.suffix in {".json", ".txt"}:
            keys.add((doctype, period))
    for doctype in _NARRATIVE_DOCTYPES:
        periods = [period for dt, period in keys if dt == doctype]
        if periods:
            return doctype, max(periods)
    return None


def load_latest_narrative(repo_root: Path, ticker: str) -> NarrativeCache | None:
    """Select once across sources and caches; never fall back after a gap."""
    try:
        sources = _discover_sources(repo_root, ticker)
        key = _selected_key(repo_root, ticker, sources)
        if key is None:
            return None
        result = _read_cache(repo_root, ticker, key, sources.get(key, []))
        current = _selected_key(repo_root, ticker, _discover_sources(repo_root, ticker))
        if current != key:
            return NarrativeCache("unavailable", "__".join(current or key), "selection_changed")
        return result
    except OSError:
        return NarrativeCache("unavailable", "IR materials", "discovery_unavailable")


def has_pending_narrative(repo_root: Path, ticker: str) -> bool:
    """Retry available local source groups; source-less caches are reader gaps."""
    try:
        return any(
            _read_cache(repo_root, ticker, key, pdfs).status == "unavailable"
            for key, pdfs in _discover_sources(repo_root, ticker).items()
        )
    except OSError:
        return True


def _extract_group(repo_root: Path, ticker: str, key: tuple[str, str], pdfs: list[Path]) -> bool:
    # Parse the exact stable bytes that were hashed, never a second file read.
    from materialized_cache import write_payload_atomically

    method = _method_identity()
    snapshots: list[ImmutableArtifactSnapshot] = []
    sources: list[_Source] = []
    chunks: list[str] = []
    empty_sources = 0
    for pdf, (snapshot, payload) in zip(pdfs, _read_sources(repo_root, pdfs), strict=True):
        snapshots.append(snapshot)
        cleaned = _normalize(project_source_text(payload, str(pdf)))
        if len(cleaned) < _MIN_CACHE_BYTES:
            empty_sources += 1
        else:
            chunks.append(cleaned)
        sources.append(
            _Source(
                path=str(pdf.relative_to(repo_root)),
                sha256=snapshot.file_sha256,
                size_bytes=snapshot.size_bytes,
            )
        )
    if empty_sources:
        if not chunks:
            return False
        raise ValueError("narrative source population contains empty documents")
    merged = _SOURCE_BREAK.join(chunks)
    envelope = _Envelope(
        ticker=ticker.upper(),
        doctype=key[0],
        period=key[1],
        method=method,
        sources=sources,
        text=merged,
        text_sha256=sha256(merged.encode("utf-8")).hexdigest(),
    )
    _assert_population_unchanged(repo_root, ticker, key, pdfs, snapshots)
    if method != _method_identity():
        raise ValueError("extraction method changed")
    write_payload_atomically(
        _cache_path_for(repo_root, ticker, *key),
        envelope.model_dump(mode="json"),
        prefix=".ir-narrative-",
    )
    return bool(chunks)


def extract_for_ticker(
    repo_root: Path,
    ticker: str,
    refresh: bool = False,
) -> dict[str, int]:
    """Build one atomic text+manifest envelope per source group, with one writer."""
    src_dir = Path(__file__).resolve().parents[1]
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))
    from run_lock import hold_run_lock

    counts = {"processed": 0, "cached": 0, "failed": 0, "skipped_empty": 0}
    # The adjacent-file lock guards this cache write set; no database is opened.
    authority = repo_root.resolve() / "data" / "ir_narrative" / ticker.upper()
    with hold_run_lock(authority, owner="ir-narrative"):
        for key, pdfs in _discover_sources(repo_root, ticker).items():
            if not refresh and _read_cache(repo_root, ticker, key, pdfs).status != "unavailable":
                counts["cached"] += 1
                continue
            try:
                included = _extract_group(repo_root, ticker, key, pdfs)
            except Exception as exc:  # pypdf has many extraction failure modes
                log.warning(
                    {
                        "event": "ir_narrative_group_failed",
                        "ticker": ticker,
                        "source_tag": "__".join(key),
                        "error_type": type(exc).__name__,
                    }
                )
                counts["failed"] += 1
                continue
            counts["processed" if included else "skipped_empty"] += 1
    return counts


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--ticker", help="Single ticker")
    g.add_argument(
        "--all",
        action="store_true",
        help="Every ticker with content under ir_documents/",
    )
    p.add_argument("--refresh", action="store_true", help="Re-extract even if cache exists")
    p.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="Project root (default: derived from this file's path)",
    )
    return p.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parse_args()
    repo_root = args.repo_root.resolve()
    if args.ticker:
        tickers = [args.ticker.upper()]
    else:
        base = repo_root / "ir_documents"
        if not base.is_dir():
            print("[]")
            return 0
        tickers = sorted(
            p.name for p in base.iterdir() if p.is_dir() and not p.name.startswith("_")
        )
    summary: list[dict[str, object]] = []
    failures = 0
    for ticker in tickers:
        counts = extract_for_ticker(repo_root, ticker, refresh=args.refresh)
        failures += counts["failed"] + counts["skipped_empty"]
        row: dict[str, object] = dict(counts)
        row["ticker"] = ticker
        summary.append(row)
        print(
            f"[{ticker}] processed={counts['processed']} cached={counts['cached']}"
            f" failed={counts['failed']} skipped_empty={counts['skipped_empty']}",
            file=sys.stderr,
        )
    print(json.dumps(summary, indent=2))
    return int(failures > 0)


if __name__ == "__main__":
    raise SystemExit(main())
