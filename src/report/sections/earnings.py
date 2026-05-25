"""§6 Earnings analysis — beat-rate header + per-quarter LLM summaries.

Most recent N quarters render in full; older ones collapse to a 1-paragraph
digest. Pairwise Say-Do lives in §7; full transcripts in §12.

When the `earnings_surprises` table has rows for the ticker, a leading
beat-rate scorecard renders before the per-quarter cards.

Sources:
  - .tmp/{TICKER}_{Q}_{YEAR}_summary.txt          per-quarter LLM summary (written by execution/process_ir_documents.py)
  - transcripts/processed/ + transcripts/raw/     path provenance (processed wins on collision)
  - earnings_surprises table                       beat-rate header (FMP primary, yfinance fallback)
"""

from __future__ import annotations

import re
import sys
from decimal import Decimal
from pathlib import Path

from report.models import (
    EarningsSection,
    QuarterlyEarningsCard,
    SectionStatus,
    SurpriseScorecardCard,
)
from report.sections._common import missing, open_repo_db

# The compute module lives in src/compute/, accessible because src/ is on the
# sys.path (per pyproject.toml `pythonpath = ["src"]`).
_PROJECT_SRC = str(Path(__file__).resolve().parents[2])
if _PROJECT_SRC not in sys.path:
    sys.path.insert(0, _PROJECT_SRC)
from compute.earnings_surprise import surprise_scorecard_for  # noqa: E402

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
    tr_root = repo_root / "transcripts"

    summaries = _scan_summaries(tmp_dir, ticker)
    transcripts = _scan_transcripts(tr_root, ticker)
    surprise_card = _build_surprise_card(ticker, repo_root)

    if not summaries and not transcripts:
        return EarningsSection(
            status=SectionStatus.MISSING_DATA,
            missing=missing(
                stage="SYNTHESIZE(process_ir_documents)",
                fix_command=f"python execution/process_ir_documents.py --ticker {ticker.upper()}",
                detail="No per-quarter summaries in .tmp/ and no transcripts in transcripts/{processed,raw}/.",
            ),
            surprise_scorecard=surprise_card,
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
        surprise_scorecard=surprise_card,
        full_quarters=list(reversed(full_old_to_new)),
        digest_quarters=list(reversed(digest_old_to_new)),
    )


def _dec_to_float(v: Decimal | None) -> float | None:
    """Decimal → float at the Pydantic boundary. None passes through."""
    return None if v is None else float(v)


def _build_surprise_card(ticker: str, repo_root: Path) -> SurpriseScorecardCard | None:
    """Build the §6 header beat-rate card from the `earnings_surprises` table.

    Returns None when:
      - the DB isn't reachable (open_repo_db returns None — fresh checkout)
      - there are no rows for the ticker (backfill_earnings_surprises hasn't
        run yet, or the ticker is brand new)

    Decimal-to-float conversion happens here at the compute → Pydantic
    boundary; the compute layer keeps full Decimal precision internally.
    """
    conn = open_repo_db(repo_root)
    if conn is None:
        return None
    try:
        sc = surprise_scorecard_for(conn, ticker)
    finally:
        conn.close()
    if sc.total_quarters == 0:
        return None
    return SurpriseScorecardCard(
        total_quarters=sc.total_quarters,
        eps_beats=sc.eps.beats,
        eps_misses=sc.eps.misses,
        eps_no_data=sc.eps.no_data,
        eps_beat_rate_pct=_dec_to_float(sc.eps.beat_rate_pct),
        eps_avg_surprise_pct=_dec_to_float(sc.eps.avg_surprise_pct),
        eps_latest_surprise_pct=_dec_to_float(sc.eps.latest_surprise_pct),
        revenue_beats=sc.revenue.beats,
        revenue_misses=sc.revenue.misses,
        revenue_no_data=sc.revenue.no_data,
        revenue_beat_rate_pct=_dec_to_float(sc.revenue.beat_rate_pct),
        revenue_avg_surprise_pct=_dec_to_float(sc.revenue.avg_surprise_pct),
        revenue_latest_surprise_pct=_dec_to_float(sc.revenue.latest_surprise_pct),
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


def _scan_transcripts(tr_root: Path, ticker: str) -> dict[tuple[int, int], str]:
    """Scan both transcripts/processed/ and transcripts/raw/.

    `processed/` is the canonical promoted location (see index_manager.py); a
    file living there wins over the same (quarter, year) in `raw/`. We scan
    `raw/` second and only fill slots that processed/ left empty, matching the
    dual-dir convention `ingest_transcripts.py` already uses.
    """
    out: dict[tuple[int, int], str] = {}
    upper = ticker.upper()
    for subdir in ("processed", "raw"):
        d = tr_root / subdir
        if not d.exists():
            continue
        for path in d.iterdir():
            if not path.is_file():
                continue
            m = _TRANSCRIPT_RX.match(path.name)
            if not m or m.group("ticker").upper() != upper:
                continue
            key = (int(m.group("q")), int(m.group("y")))
            if key not in out:
                out[key] = str(path)
    return out
