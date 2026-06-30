"""Piece 2 — competitive-mention extractor over RBRK earnings-call transcripts.

Deterministic (no LLM, no spend) counting of three thesis signals per quarter:

  (a) competitive displacement of legacy — a sentence pairing a displacement
      verb (displace / rip-and-replace / migrate off / take out / switch from)
      with a legacy/incumbent/named-competitor object, plus explicit phrases
      ("competitive displacement", "competitive takeout").
  (b) named >$1M / large-logo wins — a sentence with a large-deal marker:
      "$N million" (N>=1), seven/eight-figure, multimillion, or a marquee-logo
      cue (Fortune 500, Global 2000, largest/biggest deal, marquee win).
  (c) Cohesity / Veeam / Dell mentions — count of named-competitor references
      (the RBRK competitive watchlist: Cohesity[+Veritas], Veeam, Dell
      [PowerProtect / Data Domain], Commvault, Druva).

The per-quarter counts are written to ``kpi_facts`` (unit=count) via the single
``persist_manifest`` interface, so the matching tier-2 KPIs in ``RBRK.json`` read
real stored values and chart over time like any other quarterly metric.

The counter is a pure function over transcript text (``count_mentions``) so it is
fully unit-testable on synthetic fixtures; ``extract_for_ticker`` is the on-disk
scan + persist wrapper that reuses ``compute.transcript_ingest`` for filename ->
fiscal-period mapping (RBRK has a Jan fiscal year-end: ``RBRK_Q4_2026`` ends
2026-01-31).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from competitive import (
    KPI_MENTIONS_DISPLACEMENT,
    KPI_MENTIONS_LARGE_WIN,
    KPI_MENTIONS_NAMED_COMPETITOR,
)
from competitive._docs import ensure_synthetic_document
from compute.transcript_ingest import (
    ParsedFilename,
    map_to_period,
    parse_transcript_filename,
    read_transcript_text,
)
from models.documents import SourceType
from models.facts import Unit
from pipeline.kpi_persistence import KpiExtractionManifest, KpiValue, persist_manifest
from pipeline.run_accounting import start_run

_DOC_TYPE = "competitive_transcript_mentions"
_EXTRACTED_BY = "competitive_transcript_mentions"
_MAX_EXAMPLES = 3  # example snippets kept per signal for the fact's source_excerpt

# --- Named competitors (canonical -> aliases, each alias list LONGEST-FIRST so
# "Dell PowerProtect" counts as one Dell mention, not Dell + PowerProtect). ---- #
_VENDOR_ALIASES: dict[str, tuple[str, ...]] = {
    "Cohesity": ("Cohesity", "Veritas"),
    "Veeam": ("Veeam",),
    "Dell": ("Dell PowerProtect", "Dell Data Domain", "PowerProtect", "Data Domain", "Dell"),
    "Commvault": ("Commvault", "Metallic"),
    "Druva": ("Druva",),
}


def _vendor_regex(aliases: tuple[str, ...]) -> re.Pattern[str]:
    # Aliases are pre-ordered longest-first; join with \s+ tolerance for the
    # multi-word product names so "Dell  PowerProtect" still matches as one.
    alts = "|".join(a.replace(" ", r"\s+") for a in aliases)
    return re.compile(rf"\b(?:{alts})\b", re.IGNORECASE)


_VENDOR_PATTERNS: dict[str, re.Pattern[str]] = {
    name: _vendor_regex(aliases) for name, aliases in _VENDOR_ALIASES.items()
}

# Object of a displacement: legacy/incumbent language or any named competitor.
_LEGACY_OBJECT = (
    r"(?:legacy|incumbent|competitor|competitive|"
    r"existing\s+(?:backup|vendor|solution|environment|tool|infrastructure)|"
    r"old\s+(?:backup|vendor|solution|tool)|"
    r"Cohesity|Veritas|Veeam|Dell|PowerProtect|Data\s+Domain|Commvault|Druva|NetBackup|Avamar)"
)
_DISPLACE_VERB = (
    r"(?:displac\w*|rip(?:ped|ping)?[\s-]+and[\s-]+replac\w*|replac\w*|"
    r"migrat\w*\s+(?:off|away\s+from)|switch\w*\s+(?:off|from|away\s+from)|"
    r"consolidat\w*\s+(?:off|away\s+from)|mov\w*\s+(?:off|away\s+from)|"
    r"took?\s+out|tore?\s+out|displaced)"
)
# Explicit displacement phrases that count on their own.
_DISPLACE_PHRASE = re.compile(
    r"competitive\s+(?:displacement|takeout|take[\s-]?out|win|replacement)|"
    r"rip(?:ped|ping)?[\s-]+and[\s-]+replac\w*|rip[\s-]?and[\s-]?replace",
    re.IGNORECASE,
)
_DISPLACE_VERB_RX = re.compile(_DISPLACE_VERB, re.IGNORECASE)
_LEGACY_OBJECT_RX = re.compile(_LEGACY_OBJECT, re.IGNORECASE)

# Large-deal markers.
_MONEY_MILLIONS_RX = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s?(?:million|mn\b|m\b)", re.IGNORECASE)
_LARGE_DEAL_PHRASE = re.compile(
    r"seven[\s-]?figure|eight[\s-]?figure|multi[\s-]?million|million[\s-]?dollar|"
    r"fortune\s+\d+|global\s+\d{3,4}|"
    r"(?:largest|biggest|record)\s+(?:ever\s+)?(?:deal|win|order|transaction|contract)|"
    r"marquee\s+(?:win|logo|customer|deal|account)|"
    r"(?:large|major|landmark)\s+(?:logo|win|deal)",
    re.IGNORECASE,
)

# Sentence splitter — split on terminal punctuation followed by whitespace.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


@dataclass(slots=True)
class MentionCounts:
    """Per-transcript signal counts plus example snippets and a vendor breakdown."""

    displacement: int = 0
    large_win: int = 0
    named_competitor: int = 0
    vendor_breakdown: dict[str, int] = field(default_factory=dict[str, int])
    displacement_examples: list[str] = field(default_factory=list[str])
    large_win_examples: list[str] = field(default_factory=list[str])


def _sentences(text: str) -> list[str]:
    # Flatten newlines so a speaker-label line break doesn't split a sentence.
    flat = re.sub(r"\s+", " ", text).strip()
    return [s.strip() for s in _SENTENCE_SPLIT.split(flat) if s.strip()]


def _money_over_1m(sentence: str) -> bool:
    for m in _MONEY_MILLIONS_RX.finditer(sentence):
        try:
            if float(m.group(1).replace(",", "")) >= 1.0:
                return True
        except ValueError:
            continue
    return False


def count_mentions(text: str) -> MentionCounts:
    """Count the three competitive signals in one transcript's text.

    Pure and deterministic: displacement and large-win are counted per SENTENCE
    (a sentence with multiple trigger words counts once), named-competitor is the
    total of per-vendor reference counts over the whole text.
    """
    counts = MentionCounts()

    for sentence in _sentences(text):
        is_displacement = bool(_DISPLACE_PHRASE.search(sentence)) or (
            bool(_DISPLACE_VERB_RX.search(sentence)) and bool(_LEGACY_OBJECT_RX.search(sentence))
        )
        if is_displacement:
            counts.displacement += 1
            if len(counts.displacement_examples) < _MAX_EXAMPLES:
                counts.displacement_examples.append(sentence[:240])

        if _money_over_1m(sentence) or _LARGE_DEAL_PHRASE.search(sentence):
            counts.large_win += 1
            if len(counts.large_win_examples) < _MAX_EXAMPLES:
                counts.large_win_examples.append(sentence[:240])

    for vendor, pattern in _VENDOR_PATTERNS.items():
        n = len(pattern.findall(text))
        if n:
            counts.vendor_breakdown[vendor] = n
            counts.named_competitor += n

    return counts


# --------------------------------------------------------------------------- #
# On-disk scan + persist
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class QuarterExtract:
    """One transcript's extraction outcome."""

    quarter: str  # e.g. "Q4"
    fiscal_year_label: int  # e.g. 2026
    period_end: str  # ISO date of the calendar period end
    counts: MentionCounts
    inserted: int
    source_path: str


@dataclass(slots=True)
class ExtractResult:
    ticker: str
    quarters: list[QuarterExtract] = field(default_factory=list[QuarterExtract])


def _discover_transcripts(transcripts_root: Path, ticker: str) -> dict[tuple[int, int], Path]:
    """Map (quarter_idx, fiscal_year_label) -> newest transcript path for ticker.

    ``processed/`` is the canonical promoted location and wins over ``raw/`` for
    the same (quarter, year) — same precedence as the report's transcript scan.
    """
    found: dict[tuple[int, int], Path] = {}
    for subdir in ("processed", "raw"):  # processed first so it wins via setdefault
        base = transcripts_root / subdir
        if not base.exists():
            continue
        for path in sorted(base.iterdir()):
            if path.suffix.lower() not in (".txt", ".pdf"):
                continue
            parsed = parse_transcript_filename(path)
            if parsed is None or parsed.ticker.upper() != ticker.upper():
                continue
            found.setdefault((parsed.quarter_idx, parsed.fiscal_year_label), path)
    return found


def _excerpt_for(metric: str, counts: MentionCounts) -> str:
    if metric == KPI_MENTIONS_DISPLACEMENT:
        body = " | ".join(counts.displacement_examples)
        return f"displacement={counts.displacement}" + (f": {body}" if body else "")
    if metric == KPI_MENTIONS_LARGE_WIN:
        body = " | ".join(counts.large_win_examples)
        return f"large_win={counts.large_win}" + (f": {body}" if body else "")
    # named competitor
    breakdown = ", ".join(f"{v}:{n}" for v, n in sorted(counts.vendor_breakdown.items()))
    return f"named_competitor={counts.named_competitor}" + (f" ({breakdown})" if breakdown else "")


def extract_for_ticker(
    conn: sqlite3.Connection,
    repo_root: Path,
    ticker: str,
    *,
    transcripts_root: Path | None = None,
) -> ExtractResult:
    """Scan ``transcripts/`` for the ticker, count signals per quarter, and write
    per-quarter count facts to ``kpi_facts``. Returns a per-quarter tally."""
    root = transcripts_root or (repo_root / "transcripts")
    result = ExtractResult(ticker=ticker.upper())
    discovered = _discover_transcripts(root, ticker)
    if not discovered:
        return result

    run_id = start_run(
        conn, directive="extract_competitive_mentions", ticker_scope=[ticker.upper()]
    )

    for (q_idx, fy_label), path in sorted(discovered.items()):
        try:
            text = read_transcript_text(path)
        except (ValueError, OSError):
            continue
        counts = count_mentions(text)
        mapping = map_to_period(ParsedFilename(ticker.upper(), q_idx, fy_label))
        doc_id = ensure_synthetic_document(
            conn,
            ticker=ticker,
            source_type=SourceType.TRANSCRIPT_AUDIO,
            doc_type=_DOC_TYPE,
            source_key=f"{_DOC_TYPE}:{str(path).replace(chr(92), '/')}",
            period_end=mapping.period_end,
        )
        metric_values = {
            KPI_MENTIONS_DISPLACEMENT: counts.displacement,
            KPI_MENTIONS_LARGE_WIN: counts.large_win,
            KPI_MENTIONS_NAMED_COMPETITOR: counts.named_competitor,
        }
        values = [
            KpiValue(
                name=metric,
                value=Decimal(count),
                unit=Unit.COUNT,
                confidence=1.0,
                source_excerpt=_excerpt_for(metric, counts),
            )
            for metric, count in metric_values.items()
        ]
        manifest = KpiExtractionManifest(
            ticker=ticker.upper(),
            period_end=mapping.period_end,
            fiscal_period_type=mapping.fiscal_period_type,
            source_doc_id=doc_id,
            primary_source=SourceType.TRANSCRIPT_AUDIO,
            extracted_by=_EXTRACTED_BY,
            canonical_units={metric: Unit.COUNT for metric in metric_values},
            values=values,
        )
        outcome = persist_manifest(conn, run_id=run_id, manifest=manifest)
        result.quarters.append(
            QuarterExtract(
                quarter=f"Q{q_idx}",
                fiscal_year_label=fy_label,
                period_end=mapping.period_end.date().isoformat(),
                counts=counts,
                inserted=outcome.inserted,
                source_path=str(path).replace("\\", "/"),
            )
        )
    return result
