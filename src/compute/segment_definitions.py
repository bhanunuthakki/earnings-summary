"""Extract segment definitions from FMP form_10k JSONs.

Per-ticker pipeline:
  1. Locate latest `data/historical/fmp/{TICKER}_form_10k_{YEAR}.json`.
  2. Find sections whose key matches segment / geographic / description keywords.
  3. Flatten the nested arrays into a single text blob.
  4. Pull the actual segment names rendered in §4 from `segment_facts` so we ask
     Claude only for definitions of segments we display.
  5. Single Haiku call with a strict-JSON contract returning {name: definition}.
  6. Cache to `data/segment_definitions/{ticker}.json` keyed by source sha256.

The cache file carries `extracted_at_start` / `extracted_at_end` (ISO Z) so
the user can see when each ticker was last touched and how long it took.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from llm_client import FAST_CLASSIFIER_MODEL, JSON_FENCE_RE, _call_claude

_SECTION_KEYWORDS = ("segment", "geographic", "description of business", "operating segments")
_MAX_TEXT_BUDGET = 18000  # chars sent to LLM — caps prompt size on chatty filings


@dataclass
class SegmentDefinitionsResult:
    ticker: str
    fiscal_year: int | None
    source_path: str | None
    source_sha256: str | None
    extracted_at_start: str | None = None
    extracted_at_end: str | None = None
    elapsed_ms: int = 0
    model: str = FAST_CLASSIFIER_MODEL
    segment_names_requested: list[str] = field(default_factory=list)
    definitions: dict[str, str | None] = field(default_factory=dict)
    skipped_reason: str | None = None


def extract_for_ticker(
    ticker: str,
    repo_root: Path,
    db_conn: sqlite3.Connection,
    fiscal_year: int | None = None,
    refresh: bool = False,
) -> SegmentDefinitionsResult:
    """End-to-end extract → cache. Idempotent on (ticker, source_sha256) unless refresh=True."""
    ticker = ticker.upper()
    cache_path = _cache_path(repo_root, ticker)

    source_path, year = _locate_form_10k(repo_root, ticker, fiscal_year)
    if source_path is None:
        return SegmentDefinitionsResult(
            ticker=ticker,
            fiscal_year=None,
            source_path=None,
            source_sha256=None,
            skipped_reason=f"no _form_10k_*.json under data/historical/fmp/ for {ticker}",
        )

    raw_bytes = source_path.read_bytes()
    sha256 = hashlib.sha256(raw_bytes).hexdigest()

    if not refresh and cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached.get("source_sha256") == sha256 and cached.get("fiscal_year") == year:
            return SegmentDefinitionsResult(**cached)

    segment_names = _segment_names_from_db(db_conn, ticker)
    if not segment_names:
        result = SegmentDefinitionsResult(
            ticker=ticker,
            fiscal_year=year,
            source_path=str(source_path),
            source_sha256=sha256,
            skipped_reason="segment_facts has no rows for this ticker — nothing to define",
        )
        _write_cache(cache_path, result)
        return result

    payload = json.loads(raw_bytes.decode("utf-8"))
    relevant_text = _extract_relevant_text(payload)
    if not relevant_text.strip():
        result = SegmentDefinitionsResult(
            ticker=ticker,
            fiscal_year=year,
            source_path=str(source_path),
            source_sha256=sha256,
            segment_names_requested=segment_names,
            skipped_reason="form_10k has no section matching segment/geographic/description keywords",
        )
        _write_cache(cache_path, result)
        return result

    start_dt = datetime.now(timezone.utc)
    t0 = time.perf_counter()
    definitions = _ask_claude(ticker, year, segment_names, relevant_text)
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    end_dt = datetime.now(timezone.utc)

    result = SegmentDefinitionsResult(
        ticker=ticker,
        fiscal_year=year,
        source_path=str(source_path),
        source_sha256=sha256,
        extracted_at_start=start_dt.isoformat(timespec="seconds").replace("+00:00", "Z"),
        extracted_at_end=end_dt.isoformat(timespec="seconds").replace("+00:00", "Z"),
        elapsed_ms=elapsed_ms,
        model=FAST_CLASSIFIER_MODEL,
        segment_names_requested=segment_names,
        definitions=definitions,
    )
    _write_cache(cache_path, result)
    return result


def load_definitions(repo_root: Path, ticker: str) -> dict[str, str | None]:
    """Read the cached {segment_name: definition} for a ticker, or {} if absent."""
    cache_path = _cache_path(repo_root, ticker)
    if not cache_path.exists():
        return {}
    cached = json.loads(cache_path.read_text(encoding="utf-8"))
    defs = cached.get("definitions") or {}
    return {str(k): (str(v) if v is not None else None) for k, v in defs.items()}


def _cache_path(repo_root: Path, ticker: str) -> Path:
    out_dir = repo_root / "data" / "segment_definitions"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{ticker}.json"


def _locate_form_10k(
    repo_root: Path, ticker: str, fiscal_year: int | None
) -> tuple[Path | None, int | None]:
    fmp_dir = repo_root / "data" / "historical" / "fmp"
    if not fmp_dir.exists():
        return (None, None)
    candidates: list[tuple[int, Path]] = []
    pattern = re.compile(rf"^{re.escape(ticker)}_form_10k_(\d{{4}})\.json$")
    for p in fmp_dir.iterdir():
        m = pattern.match(p.name)
        if not m:
            continue
        candidates.append((int(m.group(1)), p))
    if not candidates:
        return (None, None)
    if fiscal_year is not None:
        for y, p in candidates:
            if y == fiscal_year:
                return (p, y)
        return (None, None)
    candidates.sort()
    y, p = candidates[-1]
    return (p, y)


def _segment_names_from_db(conn: sqlite3.Connection, ticker: str) -> list[str]:
    cur = conn.execute(
        """
        SELECT DISTINCT segment_name FROM segment_facts
        WHERE ticker = ?
          AND metric IN ('revenue_by_product', 'revenue_by_geography', 'operating_income')
        ORDER BY segment_name
        """,
        (ticker,),
    )
    return [str(r["segment_name"]) for r in cur.fetchall() if r["segment_name"]]


def _extract_relevant_text(payload: dict[str, object]) -> str:
    """Concatenate flattened text from every section whose key matches a keyword."""
    parts: list[str] = []
    total_chars = 0
    for key, value in payload.items():
        key_lower = str(key).lower()
        if not any(kw in key_lower for kw in _SECTION_KEYWORDS):
            continue
        section_text = "\n".join(_flatten(value))
        if not section_text.strip():
            continue
        if total_chars + len(section_text) > _MAX_TEXT_BUDGET:
            section_text = section_text[: _MAX_TEXT_BUDGET - total_chars]
        parts.append(f"=== {key} ===\n{section_text}")
        total_chars += len(section_text)
        if total_chars >= _MAX_TEXT_BUDGET:
            break
    return "\n\n".join(parts)


def _flatten(node: object) -> list[str]:
    """Walk a nested list/dict structure yielding text leaves longer than 80 chars."""
    out: list[str] = []
    stack: list[object] = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            s = item.strip().replace("\xa0", " ")
            if len(s) > 80:
                out.append(s)
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return out


def _ask_claude(
    ticker: str, year: int | None, segment_names: list[str], text: str
) -> dict[str, str | None]:
    names_block = "\n".join(f"- {n}" for n in segment_names)
    prompt = f"""You are extracting segment definitions from a 10-K filing for {ticker} (fiscal year {year}).

Here are the segment names this report displays. For EACH name, find the definition in the 10-K text below and return it verbatim where possible (companies typically open the segment section with a short bulleted definition like "Segment X includes products and services such as A, B, C…").

Segments to define:
{names_block}

10-K text:
\"\"\"
{text}
\"\"\"

Return STRICT JSON with one key per segment name above. Value is the definition string (1–4 sentences, copied verbatim from the 10-K where possible) or `null` if you cannot find it. Do NOT invent a definition.

Example:
{{"Google Services": "Includes products and services such as ads, Android…", "Other Bets": null}}

Return ONLY the JSON object — no markdown fence, no commentary."""

    raw = _call_claude(prompt, model=FAST_CLASSIFIER_MODEL).strip()
    if raw.startswith("```"):
        raw = JSON_FENCE_RE.sub("", raw).strip()
    parsed = json.loads(raw)
    out: dict[str, str | None] = {}
    for name in segment_names:
        v = parsed.get(name)
        if isinstance(v, str) and v.strip():
            out[name] = v.strip()
        else:
            out[name] = None
    return out


def _write_cache(path: Path, result: SegmentDefinitionsResult) -> None:
    path.write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")
