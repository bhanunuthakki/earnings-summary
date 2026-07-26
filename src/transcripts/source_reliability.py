"""Reliability ranking for earnings-transcript sources.

`documents.source_quality_tier` (models/documents.py) already ranks source
reliability for financial-statement data, but every transcript document maps
to the same constant tier (`SourceQualityTier.FMP_NORMALIZED` via
`_SOURCE_TYPE_TO_TIER[SourceType.TRANSCRIPT_AUDIO]`) — that enum answers a
different question (statement provenance) and can't distinguish a verbatim
official transcript from a third-party aggregator's Q&A scrape. This module
answers the transcript-specific question: when two ingested rows claim the
same (ticker, fiscal_period_type, period_end), which one should win.

Ranking is grounded in concrete evidence, not guesswork:
  - Nothing in the automated fetch paths (`fetch_qa_transcript.py`,
    `fetch_audio_transcripts.py`) ever writes a `.pdf` — both only write
    `.txt`. A `.pdf` found by `execution/ingest_transcripts.py`'s directory
    walk is therefore a manual drop (directives/fetch_transcripts.md's
    documented fallback: "ask the user to drop a transcript PDF").
  - `doc_type == ir_transcript` is an explicitly-typed official issuer
    document (see `ingest_existing_ir_transcript`).
  - `aggregator_sources.SOURCES` already encodes a maintainer-judged priority
    order (issuer_ir tried first, as the company's own primary source) —
    reused directly here rather than re-derived.
  - roic.ai (2026-07-25 fix) and, as of the same-day D1.1-D1.5 filings work,
    stockanalysis.com and tickertrends.io too, all read real per-turn DOM
    structure instead of the old flatten-and-guess heuristic (see
    `src/aggregator_sources.py`'s module docstring) — all three are now
    equally reliable parses; none is still on the old heuristic.
  - `fetch_audio_transcripts.py`'s curated-URL/manifest modes are a verified
    link chosen in advance; smart-search auto-selects and scores a YouTube
    candidate, which is inherently less certain to be the right video.
  - Every aggregator source is Q&A-only by design (directives/
    fetch_qa_transcript.md, "Why Q&A only") — structurally less complete
    than a full-call transcript (manual/IR PDF, Whisper audio), regardless
    of parse fidelity.
"""

from __future__ import annotations

import re
from pathlib import Path

SYNTHESIZED_BANNER = "=== SYNTHESIZED QUARTERLY UPDATE"
_SYNTH_SOURCE_RE = re.compile(r"^Source:\s+aggregator_(\S+)", re.MULTILINE)

IR_TRANSCRIPT_DOC = "ir_transcript_doc"
MANUAL_PDF = "manual_pdf"
UNKNOWN_LEGACY = "unknown_legacy"

# Higher wins a period conflict. See module docstring for the evidence behind
# each rank. Gaps are deliberate spacing, not missing tiers.
_RANK: dict[str, int] = {
    IR_TRANSCRIPT_DOC: 100,
    MANUAL_PDF: 90,
    "yt_dlp_whisper_url": 70,
    "yt_dlp_whisper_links": 70,
    "yt_dlp_whisper_search": 60,
    "aggregator_issuer_ir": 50,
    # roic/stockanalysis/tickertrends are all DOM-verified per-turn parses as
    # of 2026-07-25 (see src/aggregator_sources.py) — one tier, not ranked
    # against each other.
    "aggregator_roic": 40,
    "aggregator_stockanalysis": 40,
    "aggregator_tickertrends": 40,
    UNKNOWN_LEGACY: 10,
}
_DEFAULT_RANK = 0


def reliability_rank(source: str | None) -> int:
    """Numeric rank for a source label; unrecognized/None sorts lowest."""
    if source is None:
        return _DEFAULT_RANK
    return _RANK.get(source, _DEFAULT_RANK)


def classify_transcript_source(
    file_path: Path,
    text: str,
    *,
    is_ir_transcript_doc: bool = False,
    index_hint: str | None = None,
) -> str:
    """Best-effort provenance label for an ingested transcript file.

    Priority order (most to least certain signal):
      1. `is_ir_transcript_doc` — the document row is explicitly typed
         `ir_transcript`; ground truth, not inferred.
      2. The synthesized-fetch banner's own `Source:` line — ground truth
         for anything `fetch_qa_transcript.py` produced.
      3. A `.pdf` extension — nothing automated ever writes one.
      4. `index_hint` — a best-effort label recovered from
         `.tmp/transcript_index.json`, which is a singleton per
         (ticker, year, quarter) and may describe a *different* fetch than
         the file at hand; lowest-confidence signal, used only as a
         last resort before giving up.
      5. `unknown_legacy` — provenance unrecoverable.
    """
    if is_ir_transcript_doc:
        return IR_TRANSCRIPT_DOC
    if SYNTHESIZED_BANNER in text[:200]:
        m = _SYNTH_SOURCE_RE.search(text[:2000])
        if m:
            return f"aggregator_{m.group(1)}"
    if file_path.suffix.lower() == ".pdf":
        return MANUAL_PDF
    if index_hint:
        return index_hint
    return UNKNOWN_LEGACY


def choose_winner(
    *,
    new_source: str,
    new_segment_count: int,
    old_source: str | None,
    old_segment_count: int,
) -> bool:
    """True if the new row should replace the old one at a period conflict.

    Reliability tier wins first; a tie falls back to richer segment
    structure (more real speaker turns) as a corroborating signal, since two
    fetches from the same tier can still differ in parse quality.
    """
    new_rank = reliability_rank(new_source)
    old_rank = reliability_rank(old_source)
    if new_rank != old_rank:
        return new_rank > old_rank
    return new_segment_count > old_segment_count
