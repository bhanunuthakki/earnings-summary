"""§5 Earnings analysis — LLM summaries only, newest first.

Most recent N quarters render in full; older ones collapse to a 1-paragraph
digest. Pairwise Say-Do lives in §6; full transcripts in §9.

Sources:
  - .tmp/{TICKER}_{Q}_{YEAR}_summary.txt          per-quarter LLM summary (written by execution/process_ir_documents.py)
  - transcripts/processed/{TICKER}_Q{N}_{YEAR}.txt path provenance
"""

from __future__ import annotations

import re
from pathlib import Path

from report.models import (
    EarningsSection,
    QuarterlyEarningsCard,
    SectionStatus,
)
from report.sections._common import missing

# `_summary.txt` is the canonical per-quarter LLM summary; `_investor_update_summary.txt`
# is the MELI/NU variant (companies that publish investor-update letters in lieu of
# traditional press-release-plus-call). Both feed §5 the same way.
_SUMMARY_RX = re.compile(
    r"^(?P<ticker>[A-Z][A-Z0-9.]*)_Q(?P<q>[1-4])_(?P<y>\d{4})_(?:investor_update_)?summary\.txt$"
)
_TRANSCRIPT_RX = re.compile(r"^(?P<ticker>[A-Z][A-Z0-9.]*)_Q(?P<q>[1-4])_(?P<y>\d{4})\.(?:txt|pdf)$")

MAX_CARDS = 8
RECENT_FULL_COUNT = 3  # most recent N quarters get full content in §5


def build(ticker: str, repo_root: Path) -> EarningsSection:
    tmp_dir = repo_root / ".tmp"
    tr_dir = repo_root / "transcripts" / "processed"

    summaries = _scan_summaries(tmp_dir, ticker)
    transcripts = _scan_transcripts(tr_dir, ticker)

    if not summaries and not transcripts:
        return EarningsSection(
            status=SectionStatus.MISSING_DATA,
            missing=missing(
                stage="SYNTHESIZE(process_ir_documents)",
                fix_command=f"python execution/process_ir_documents.py --ticker {ticker.upper()}",
                detail="No per-quarter summaries in .tmp/ and no transcripts in transcripts/processed/.",
            ),
        )

    # Oldest → newest first, then take last MAX_CARDS, then reverse for display.
    keys_old_to_new = sorted(set(summaries.keys()) | set(transcripts.keys()), key=lambda k: (k[1], k[0]))[-MAX_CARDS:]
    cards_old_to_new = [_make_card(q, y, summaries, transcripts) for q, y in keys_old_to_new]

    full_old_to_new = cards_old_to_new[-RECENT_FULL_COUNT:]
    digest_old_to_new = cards_old_to_new[:-RECENT_FULL_COUNT]
    for c in full_old_to_new:
        c.is_recent = True

    has_any_llm = any(c.summary_md for c in cards_old_to_new)
    return EarningsSection(
        status=SectionStatus.OK if has_any_llm else SectionStatus.PARTIAL,
        full_quarters=list(reversed(full_old_to_new)),
        digest_quarters=list(reversed(digest_old_to_new)),
    )


def _make_card(
    q: int,
    y: int,
    summaries: dict[tuple[int, int], str],
    transcripts: dict[tuple[int, int], str],
) -> QuarterlyEarningsCard:
    summary = summaries.get((q, y))
    return QuarterlyEarningsCard(
        quarter=f"Q{q}",
        year=y,
        summary_md=summary,
        digest_md=_extract_digest(summary) if summary else None,
        transcript_path=transcripts.get((q, y)),
    )


def _extract_digest(summary_md: str) -> str:
    """Pull just the Executive Summary block from the per-quarter summary.

    Per-quarter summaries follow a stable structure starting with
    `## 1. Executive Summary` and ending at the next H2 header. We lift that
    block verbatim — no LLM call. Falls back to the first 600 chars on miss.
    """
    lines = summary_md.splitlines()
    in_block = False
    block: list[str] = []
    for line in lines:
        if line.lstrip().startswith("## ") and "Executive Summary" in line:
            in_block = True
            continue
        if in_block and line.lstrip().startswith("## "):
            break
        if in_block:
            block.append(line)
    extracted = "\n".join(block).strip()
    if extracted:
        return extracted
    return summary_md.strip()[:600]


def _scan_summaries(tmp_dir: Path, ticker: str) -> dict[tuple[int, int], str]:
    out: dict[tuple[int, int], str] = {}
    if not tmp_dir.exists():
        return out
    upper = ticker.upper()
    for path in tmp_dir.iterdir():
        if not path.is_file():
            continue
        m = _SUMMARY_RX.match(path.name)
        if not m or m.group("ticker").upper() != upper:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                out[(int(m.group("q")), int(m.group("y")))] = f.read()
        except OSError:
            continue
    return out


def _scan_transcripts(processed_dir: Path, ticker: str) -> dict[tuple[int, int], str]:
    out: dict[tuple[int, int], str] = {}
    if not processed_dir.exists():
        return out
    upper = ticker.upper()
    for path in processed_dir.iterdir():
        if not path.is_file():
            continue
        m = _TRANSCRIPT_RX.match(path.name)
        if not m or m.group("ticker").upper() != upper:
            continue
        out[(int(m.group("q")), int(m.group("y")))] = str(path)
    return out
