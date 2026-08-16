"""Extract the §2 "Platform overview" ASCII diagram from 10-K + profile + recent transcripts.

Per-ticker pipeline (mirrors `compute/company_description.py`):
  1. Locate latest `data/historical/fmp/{TICKER}_form_10k_{YEAR}.json` and
     `{TICKER}_profile.json` for the canonical narrative + sector/industry.
  2. Pull the two most recent processed transcripts (`transcripts/processed/
     {TICKER}_Q*_*.txt`) for how management currently frames the platform.
  3. Pull the segment names this report displays from `segment_dimensions`
     so the diagram labels can lean on real product lines.
  4. Single Claude call returning {diagram, caption}.
  5. Cache to `data/platform_diagram/{TICKER}.json` keyed by a sha256 over
     the combined inputs (10-K bytes + profile description + transcript text).
     Re-runs are no-ops unless any source changed or refresh=True.

Source-of-truth for SEC narrative is `data/historical/fmp/*_form_10k_*.json`
(see memory: SEC filings source-of-truth scan).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict, cast

from compute.company_description import (
    _extract_relevant_text,  # 10-K section-keyed text extractor
    _load_profile,
    _locate_form_10k,
    _profile_str,
    _segment_names_from_db,
)
from llm_client import DEFAULT_MODEL, JSON_FENCE_RE, LLM_MODELS, generate_platform_diagram

_PURPOSE = "platform_diagram"
_MODEL_USED = LLM_MODELS.get(_PURPOSE, DEFAULT_MODEL)


class _ParsedLLMOutput(TypedDict):
    diagram: str | None
    caption: str | None


# Two transcripts at ~6 KB each leaves room alongside the 24 KB 10-K budget
# under any Claude model's context. Pick the latest by (year, quarter).
_TRANSCRIPTS_TO_USE = 2
_TRANSCRIPT_BUDGET_CHARS = 6000
_TRANSCRIPT_FILENAME_RE = re.compile(
    r"^(?P<ticker>[A-Z][A-Z0-9.\-]*)_Q(?P<q>[1-4])_(?P<y>\d{4})\.txt$"
)


@dataclass
class PlatformDiagramResult:
    ticker: str
    fiscal_year: int | None
    source_path: str | None  # 10-K path (or None when profile-only)
    source_sha256: str | None  # hash over combined inputs
    transcript_paths: list[str]
    extracted_at_start: str | None = None
    extracted_at_end: str | None = None
    elapsed_ms: int = 0
    model: str = _MODEL_USED
    diagram: str | None = None
    caption: str | None = None
    skipped_reason: str | None = None


def extract_for_ticker(
    ticker: str,
    repo_root: Path,
    db_conn: sqlite3.Connection,
    fiscal_year: int | None = None,
    refresh: bool = False,
) -> PlatformDiagramResult:
    """End-to-end extract → cache. Idempotent on combined-source sha256."""
    ticker = ticker.upper()
    cache_path = _cache_path(repo_root, ticker)

    source_path, year = _locate_form_10k(repo_root, ticker, fiscal_year)
    profile = _load_profile(repo_root, ticker)
    profile_description = _profile_str(profile, "description") or ""
    sector = _profile_str(profile, "sector")
    industry = _profile_str(profile, "industry")

    transcripts = _latest_transcripts(repo_root, ticker, _TRANSCRIPTS_TO_USE)
    transcript_excerpts, used_transcript_paths = _build_transcript_excerpts(transcripts)

    if source_path is None and not profile_description and not transcript_excerpts:
        return PlatformDiagramResult(
            ticker=ticker,
            fiscal_year=None,
            source_path=None,
            source_sha256=None,
            transcript_paths=[],
            skipped_reason=(
                f"no _form_10k_*.json, no profile.json description, and no transcripts "
                f"for {ticker} — nothing to ground the diagram in"
            ),
        )

    # Build combined-source hash so refresh-on-change works even when one
    # input updates (new 10-K filing, new transcript) but the others don't.
    hasher = hashlib.sha256()
    if source_path is not None:
        raw_bytes = source_path.read_bytes()
        payload = cast("dict[str, object]", json.loads(raw_bytes.decode("utf-8")))
        relevant_text = _extract_relevant_text(payload)
        hasher.update(b"10K:")
        hasher.update(raw_bytes)
    else:
        relevant_text = ""
        year = None
    if profile_description:
        hasher.update(b"PROFILE:")
        hasher.update(profile_description.encode("utf-8"))
    for path, text in transcripts:
        hasher.update(b"TX:")
        hasher.update(path.name.encode("utf-8"))
        hasher.update(text.encode("utf-8"))
    sha256 = hasher.hexdigest()

    if not refresh and cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached.get("source_sha256") == sha256 and cached.get("fiscal_year") == year:
            return PlatformDiagramResult(**cached)

    segment_names = _segment_names_from_db(db_conn, ticker, metric="revenue_by_product")

    start_dt = datetime.now(UTC)
    t0 = time.perf_counter()
    parsed = _call_llm(
        ticker=ticker,
        profile_description=profile_description,
        sector=sector,
        industry=industry,
        form_10k_text=relevant_text,
        transcript_excerpts=transcript_excerpts,
        segment_names=segment_names,
        fiscal_year=year,
    )
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    end_dt = datetime.now(UTC)

    result = PlatformDiagramResult(
        ticker=ticker,
        fiscal_year=year,
        source_path=str(source_path) if source_path is not None else None,
        source_sha256=sha256,
        transcript_paths=[str(p) for p in used_transcript_paths],
        extracted_at_start=start_dt.isoformat(timespec="seconds").replace("+00:00", "Z"),
        extracted_at_end=end_dt.isoformat(timespec="seconds").replace("+00:00", "Z"),
        elapsed_ms=elapsed_ms,
        model=_MODEL_USED,
        diagram=parsed["diagram"],
        caption=parsed["caption"],
    )
    _write_cache(cache_path, result)
    return result


def load_diagram(repo_root: Path, ticker: str) -> PlatformDiagramResult | None:
    """Read the cached platform diagram for a ticker, or None if absent."""
    cache_path = _cache_path(repo_root, ticker)
    if not cache_path.exists():
        return None
    cached = json.loads(cache_path.read_text(encoding="utf-8"))
    return PlatformDiagramResult(**cached)


def _cache_path(repo_root: Path, ticker: str) -> Path:
    return repo_root / "data" / "platform_diagram" / f"{ticker.upper()}.json"


def _latest_transcripts(repo_root: Path, ticker: str, n: int) -> list[tuple[Path, str]]:
    """Return up to `n` most recent transcript (path, text) pairs.

    Sorts by (year, quarter) descending. Reads from both `transcripts/processed/`
    and `transcripts/raw/`; if the same (quarter, year) exists in both, `processed/`
    wins (it's the promoted canonical location). Silently skips files that can't
    be read so a single bad file doesn't block the rest.
    """
    upper = ticker.upper()
    by_key: dict[tuple[int, int], Path] = {}
    for subdir in ("processed", "raw"):
        d = repo_root / "transcripts" / subdir
        if not d.exists():
            continue
        for p in d.iterdir():
            m = _TRANSCRIPT_FILENAME_RE.match(p.name)
            if not m or m.group("ticker").upper() != upper:
                continue
            key = (int(m.group("y")), int(m.group("q")))
            if key not in by_key:
                by_key[key] = p
    candidates: list[tuple[int, int, Path]] = [(y, q, path) for (y, q), path in by_key.items()]
    if not candidates:
        return []
    candidates.sort(reverse=True)
    out: list[tuple[Path, str]] = []
    for _y, _q, path in candidates[:n]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        out.append((path, text))
    return out


def _build_transcript_excerpts(
    transcripts: list[tuple[Path, str]],
) -> tuple[str, list[Path]]:
    """Concatenate truncated transcript bodies into a single LLM-bound blob.

    Strips the synthesized-update header (everything before `=== Q&A SEGMENT ===`)
    so the model sees mgmt prose instead of file metadata, then truncates to
    `_TRANSCRIPT_BUDGET_CHARS` per transcript. Returns the joined blob plus
    the list of paths actually used (in case any were empty after stripping).
    """
    parts: list[str] = []
    used: list[Path] = []
    for path, text in transcripts:
        body = _strip_header(text)
        if not body.strip():
            continue
        excerpt = body[:_TRANSCRIPT_BUDGET_CHARS]
        parts.append(f"=== {path.name} ===\n{excerpt}")
        used.append(path)
    return ("\n\n".join(parts), used)


def _strip_header(text: str) -> str:
    """Trim the synthesized-update preamble so the model sees Q&A prose only."""
    marker = "=== Q&A SEGMENT ==="
    idx = text.find(marker)
    if idx == -1:
        return text
    return text[idx + len(marker) :]


def _call_llm(
    ticker: str,
    profile_description: str,
    sector: str | None,
    industry: str | None,
    form_10k_text: str,
    transcript_excerpts: str,
    segment_names: list[str],
    fiscal_year: int | None,
) -> _ParsedLLMOutput:
    raw = generate_platform_diagram(
        ticker=ticker,
        profile_description=profile_description,
        sector=sector,
        industry=industry,
        form_10k_text=form_10k_text,
        transcript_excerpts=transcript_excerpts,
        segment_names=segment_names,
        fiscal_year=fiscal_year,
    ).strip()
    if raw.startswith("```"):
        raw = JSON_FENCE_RE.sub("", raw).strip()
    parsed = cast("dict[str, object]", json.loads(raw))
    return _ParsedLLMOutput(
        diagram=_str_or_none(parsed.get("diagram")),
        caption=_str_or_none(parsed.get("caption")),
    )


def _str_or_none(v: object) -> str | None:
    if isinstance(v, str) and v.strip():
        return v.strip()
    return None


def _write_cache(path: Path, result: PlatformDiagramResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")
