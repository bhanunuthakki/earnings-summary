"""§6 Say-Do analysis — pairwise prior-quarter guidance vs current-quarter results.

Sourced from .tmp/SayDo_{TICKER}_Q{prev}_{prev_yr}_Q{curr}_{curr_yr}.txt files
written by src/main.py during legacy transcript processing. Newest first.

Each card is parsed for the LLM's verdict line so the renderer can show a
summary table (rating + thesis view) before the per-quarter breakdown.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, cast

from report.models import (
    SayDoCard,
    SayDoSection,
    SectionStatus,
)
from report.sections._common import missing

_SAYDO_FILENAME_RX = re.compile(
    r"^SayDo_(?P<t>[A-Z][A-Z0-9.]*)_Q(?P<pq>[1-4])_(?P<py>\d{4})_Q(?P<cq>[1-4])_(?P<cy>\d{4})\.txt$"
)
# LLM puts the rating on a line like "**Performance Rating:** **EXCEEDED**" — match flexibly.
_RATING_RX = re.compile(
    r"Performance Rating:?\s*\**\s*\**\s*(MET|MISSED|EXCEEDED|MIXED)\b",
    re.IGNORECASE,
)
# Capture the rest of the "Thesis View:" / "Attribution:" line until newline.
_THESIS_VIEW_RX = re.compile(
    r"Thesis View:?\s*\**\s*([^\n]+?)(?:\*\*)?\s*$", re.IGNORECASE | re.MULTILINE
)
_ATTRIBUTION_RX = re.compile(
    r"Attribution:?\s*\**\s*([^\n]+?)(?:\*\*)?\s*$", re.IGNORECASE | re.MULTILINE
)
_RatingValue = Literal["MET", "MISSED", "EXCEEDED", "MIXED", "unknown"]


def build(ticker: str, repo_root: Path) -> SayDoSection:
    tmp_dir = repo_root / ".tmp"
    cards = _scan(tmp_dir, ticker)
    if not cards:
        return SayDoSection(
            status=SectionStatus.MISSING_DATA,
            missing=missing(
                stage="SYNTHESIZE(main.py)",
                fix_command=f"python src/main.py --company {ticker.upper()}",
                detail="No SayDo_*.txt files found under .tmp/.",
            ),
        )
    return SayDoSection(status=SectionStatus.OK, cards=cards)


def _scan(tmp_dir: Path, ticker: str) -> list[SayDoCard]:
    if not tmp_dir.exists():
        return []
    upper = ticker.upper()
    found: list[SayDoCard] = []
    for path in tmp_dir.iterdir():
        if not path.is_file():
            continue
        m = _SAYDO_FILENAME_RX.match(path.name)
        if not m or m.group("t").upper() != upper:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        rating = _parse_rating(text)
        thesis_view = _parse_one_line(text, _THESIS_VIEW_RX)
        attribution = _parse_one_line(text, _ATTRIBUTION_RX)
        found.append(
            SayDoCard(
                current_quarter=f"Q{m.group('cq')}",
                current_year=int(m.group("cy")),
                prior_quarter=f"Q{m.group('pq')}",
                prior_year=int(m.group("py")),
                saydo_md=text,
                rating=rating,
                thesis_view=thesis_view,
                attribution=attribution,
            )
        )
    found.sort(key=lambda c: (c.current_year, c.current_quarter), reverse=True)
    return found


def _parse_rating(text: str) -> _RatingValue:
    m = _RATING_RX.search(text)
    if m is None:
        return "unknown"
    return cast(_RatingValue, m.group(1).upper())


def _parse_one_line(text: str, rx: re.Pattern[str]) -> str | None:
    m = rx.search(text)
    if m is None:
        return None
    extracted = m.group(1).strip()
    # Strip all markdown-bold markers (`**`) anywhere in the captured fragment.
    extracted = extracted.replace("**", "").strip()
    # Trim trailing punctuation noise commonly left by the LLM (`.` after a
    # bolded label) and collapse any internal whitespace runs.
    extracted = re.sub(r"\s+", " ", extracted).strip()
    return extracted or None
