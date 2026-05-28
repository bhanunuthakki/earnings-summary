"""
src/compute/ir_narrative.py
---------------------------
Extract narrative (qualitative) text from per-ticker IR PDFs and cache it for
LLM-prompt consumption.

Source: ir_documents/<TICKER>/<period>/ir_<doctype>__<sha8>.pdf and
        ir_documents/_events/<TICKER>/<event_date>/ir_event__<sha8>.pdf.
Cache:  data/ir_narrative/<TICKER>/<doctype>__<period>.txt

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
    python src/compute/ir_narrative.py --ticker META
    python src/compute/ir_narrative.py --all
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

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
_MAX_CHARS_PER_DOC = 24000  # post-normalize per-PDF cap; loader trims further
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
    """data/ir_narrative/<TICKER>/<doctype>__<period>.txt"""
    out_dir = repo_root / "data" / "ir_narrative" / ticker.upper()
    return out_dir / f"{doctype}__{period}.txt"


def _discover_sources(repo_root: Path, ticker: str) -> dict[tuple[str, str], list[Path]]:
    """Map (doctype, period) → list of source PDFs.

    Walks both the periodized layout (``ir_documents/<T>/<period>/``) and the
    event layout (``ir_documents/_events/<T>/<event_date>/``). One ticker can
    have multiple PDFs per (doctype, period) — they all flow into one cache
    file with the SOURCE BREAK delimiter.
    """
    ticker_u = ticker.upper()
    base = repo_root / "ir_documents"
    out: dict[tuple[str, str], list[Path]] = {}

    periodized = base / ticker_u
    if periodized.is_dir():
        for period_dir in sorted(periodized.iterdir()):
            if not period_dir.is_dir():
                continue
            period = period_dir.name
            for pdf in sorted(period_dir.glob("*.pdf")):
                m = _FILENAME_RX.match(pdf.name)
                if not m:
                    continue
                doctype = m.group("doctype")
                if doctype not in _NARRATIVE_DOCTYPES:
                    continue
                out.setdefault((doctype, period), []).append(pdf)

    events_root = base / "_events" / ticker_u
    if events_root.is_dir():
        for event_dir in sorted(events_root.iterdir()):
            if not event_dir.is_dir():
                continue
            event_date = event_dir.name
            for pdf in sorted(event_dir.glob("*.pdf")):
                m = _FILENAME_RX.match(pdf.name)
                if not m or m.group("doctype") != "ir_event":
                    continue
                out.setdefault(("ir_event", event_date), []).append(pdf)

    return out


def extract_for_ticker(
    repo_root: Path,
    ticker: str,
    refresh: bool = False,
) -> dict[str, int]:
    """Extract narrative text for every IR PDF in the ticker's tree. Idempotent.

    Returns a count dict: ``{processed, cached, failed, skipped_empty}``.
    Failures are logged and counted but don't abort the run — a single
    corrupted PDF can't block the rest of a ticker's narrative cache.
    """
    # Late import: pypdf is a heavy dep and tests that don't touch the
    # extractor shouldn't pay for it. Also keeps this module importable in
    # environments where pypdf isn't installed (the cache may already exist).
    src_dir = Path(__file__).resolve().parents[1]
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))
    from parser import extract_text_from_pdf

    sources = _discover_sources(repo_root, ticker)
    counts = {"processed": 0, "cached": 0, "failed": 0, "skipped_empty": 0}
    if not sources:
        return counts

    for (doctype, period), pdfs in sources.items():
        cache = _cache_path_for(repo_root, ticker, doctype, period)
        if cache.exists() and not refresh:
            counts["cached"] += 1
            continue

        chunks: list[str] = []
        had_failure = False
        for pdf in pdfs:
            try:
                raw = extract_text_from_pdf(str(pdf))
            except Exception as exc:  # pypdf has many failure modes
                log.warning(
                    {
                        "event": "pdf_extract_failed",
                        "pdf": str(pdf),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                had_failure = True
                continue
            cleaned = _normalize(raw)
            if len(cleaned) < _MIN_CACHE_BYTES:
                continue
            if len(cleaned) > _MAX_CHARS_PER_DOC:
                cleaned = cleaned[:_MAX_CHARS_PER_DOC].rstrip() + "\n[...truncated]"
            chunks.append(cleaned)

        if not chunks:
            if had_failure:
                counts["failed"] += 1
            else:
                counts["skipped_empty"] += 1
            continue

        merged = _SOURCE_BREAK.join(chunks)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(merged, encoding="utf-8")
        counts["processed"] += 1

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
    for ticker in tickers:
        counts = extract_for_ticker(repo_root, ticker, refresh=args.refresh)
        row: dict[str, object] = dict(counts)
        row["ticker"] = ticker
        summary.append(row)
        print(
            f"[{ticker}] processed={counts['processed']} cached={counts['cached']}"
            f" failed={counts['failed']} skipped_empty={counts['skipped_empty']}",
            file=sys.stderr,
        )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
