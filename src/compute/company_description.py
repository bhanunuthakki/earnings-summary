"""Extract the §2 Company description from FMP 10-K + profile.json.

Per-ticker pipeline:
  1. Locate latest `data/historical/fmp/{TICKER}_form_10k_{YEAR}.json`.
  2. Read `data/historical/fmp/{TICKER}_profile.json` for description / sector / industry.
  3. Find 10-K sections matching business-description keywords (Nature of
     Business / Description of Business / Information about Segments).
  4. Pull the segment + geography names this report currently displays from
     `segment_facts` so the LLM only describes things we render.
  5. Single Sonnet call returning {elevator_pitch, business_overview,
     revenue_model, segments, geographies}.
  6. Cache to `data/company_description/{TICKER}.json` keyed by source sha256.

Source-of-truth for SEC narrative is `data/historical/fmp/*_form_10k_*.json`
(see memory: SEC filings source-of-truth scan).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict, cast

from llm_client import DEFAULT_MODEL, JSON_FENCE_RE, generate_company_description


class _ParsedLLMOutput(TypedDict):
    elevator_pitch: str | None
    business_overview: str | None
    revenue_model: str | None
    segments: list[dict[str, str | None]]
    geographies: list[dict[str, str | None]]


_SECTION_KEYWORDS = (
    "nature of business",
    "description of business",
    "segment",
    "geographic",
    "operating segments",
    "business overview",
    "information about segments",
)
_MAX_TEXT_BUDGET = 24000  # chars sent to LLM


@dataclass
class CompanyDescriptionResult:
    ticker: str
    fiscal_year: int | None
    source_path: str | None
    source_sha256: str | None
    extracted_at_start: str | None = None
    extracted_at_end: str | None = None
    elapsed_ms: int = 0
    model: str = DEFAULT_MODEL
    sector: str | None = None
    industry: str | None = None
    segment_names_requested: list[str] = field(default_factory=list)
    geo_names_requested: list[str] = field(default_factory=list)
    elevator_pitch: str | None = None
    business_overview: str | None = None
    revenue_model: str | None = None
    segments: list[dict[str, str | None]] = field(default_factory=list)
    geographies: list[dict[str, str | None]] = field(default_factory=list)
    skipped_reason: str | None = None


def extract_for_ticker(
    ticker: str,
    repo_root: Path,
    db_conn: sqlite3.Connection,
    fiscal_year: int | None = None,
    refresh: bool = False,
) -> CompanyDescriptionResult:
    """End-to-end extract → cache. Idempotent on (ticker, source_sha256) unless refresh=True.

    Two source paths:
      1. 10-K JSON available → richer narrative, segment / geography prose grounded in filings.
      2. Profile-only fallback (no 10-K JSON, but profile.json has a description) →
         LLM synthesizes from the profile blurb + sector/industry + any segment_facts
         already in the DB. Used for tickers FMP can't deliver a 10-K for (legacy-
         endpoint deprecation, non-US issuers, micro-caps with no SEC coverage).
         The cache key in that case hashes the profile description instead.
    """
    ticker = ticker.upper()
    cache_path = _cache_path(repo_root, ticker)

    source_path, year = _locate_form_10k(repo_root, ticker, fiscal_year)
    profile = _load_profile(repo_root, ticker)
    profile_description = _profile_str(profile, "description") or ""
    sector = _profile_str(profile, "sector")
    industry = _profile_str(profile, "industry")

    if source_path is None and not profile_description:
        return CompanyDescriptionResult(
            ticker=ticker,
            fiscal_year=None,
            source_path=None,
            source_sha256=None,
            sector=sector,
            industry=industry,
            skipped_reason=(
                f"no _form_10k_*.json AND no profile.json description for {ticker} — "
                f"nothing to ground the LLM in"
            ),
        )

    if source_path is not None:
        raw_bytes = source_path.read_bytes()
        sha256 = hashlib.sha256(raw_bytes).hexdigest()
        payload = cast("dict[str, object]", json.loads(raw_bytes.decode("utf-8")))
        relevant_text = _extract_relevant_text(payload)
    else:
        # Profile-only fallback: hash the description so refresh-on-change works.
        sha256 = hashlib.sha256(profile_description.encode("utf-8")).hexdigest()
        relevant_text = ""
        year = None

    if not refresh and cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached.get("source_sha256") == sha256 and cached.get("fiscal_year") == year:
            return CompanyDescriptionResult(**cached)

    segment_names = _segment_names_from_db(db_conn, ticker, metric="revenue_by_product")
    geo_names = _segment_names_from_db(db_conn, ticker, metric="revenue_by_geography")

    start_dt = datetime.now(UTC)
    t0 = time.perf_counter()
    parsed = _call_llm(
        ticker=ticker,
        profile_description=profile_description,
        sector=sector,
        industry=industry,
        form_10k_text=relevant_text,
        segment_names=segment_names,
        geo_names=geo_names,
        fiscal_year=year,
    )
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    end_dt = datetime.now(UTC)

    result = CompanyDescriptionResult(
        ticker=ticker,
        fiscal_year=year,
        source_path=str(source_path) if source_path is not None else None,
        source_sha256=sha256,
        extracted_at_start=start_dt.isoformat(timespec="seconds").replace("+00:00", "Z"),
        extracted_at_end=end_dt.isoformat(timespec="seconds").replace("+00:00", "Z"),
        elapsed_ms=elapsed_ms,
        model=DEFAULT_MODEL,
        sector=sector,
        industry=industry,
        segment_names_requested=segment_names,
        geo_names_requested=geo_names,
        elevator_pitch=parsed["elevator_pitch"],
        business_overview=parsed["business_overview"],
        revenue_model=parsed["revenue_model"],
        segments=parsed["segments"],
        geographies=parsed["geographies"],
    )
    _write_cache(cache_path, result)
    return result


def load_description(repo_root: Path, ticker: str) -> CompanyDescriptionResult | None:
    """Read the cached company description for a ticker, or None if absent."""
    cache_path = _cache_path(repo_root, ticker)
    if not cache_path.exists():
        return None
    cached = json.loads(cache_path.read_text(encoding="utf-8"))
    return CompanyDescriptionResult(**cached)


def _cache_path(repo_root: Path, ticker: str) -> Path:
    out_dir = repo_root / "data" / "company_description"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{ticker.upper()}.json"


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


def _profile_str(profile: dict[str, object] | None, key: str) -> str | None:
    """Pull a string field from the FMP profile.json payload safely."""
    if profile is None:
        return None
    v = profile.get(key)
    if isinstance(v, str) and v.strip():
        return v.strip()
    return None


def _load_profile(repo_root: Path, ticker: str) -> dict[str, object] | None:
    """FMP profile.json carries `description` / `sector` / `industry`."""
    path = repo_root / "data" / "historical" / "fmp" / f"{ticker.upper()}_profile.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(payload, list):
        return cast("dict[str, object]", payload[0]) if payload else None
    if isinstance(payload, dict):
        return cast("dict[str, object]", payload)
    return None


def _segment_names_from_db(conn: sqlite3.Connection, ticker: str, metric: str) -> list[str]:
    cur = conn.execute(
        """
        SELECT DISTINCT segment_name FROM segment_facts
        WHERE ticker = ? AND metric = ?
        ORDER BY segment_name
        """,
        (ticker, metric),
    )
    return [str(r["segment_name"]) for r in cur.fetchall() if r["segment_name"]]


def _extract_relevant_text(payload: dict[str, object]) -> str:
    """Concatenate flattened text from every 10-K section whose key matches a keyword."""
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
            stack.extend(cast("dict[str, object]", item).values())
        elif isinstance(item, list):
            stack.extend(cast("list[object]", item))
    return out


def _call_llm(
    ticker: str,
    profile_description: str,
    sector: str | None,
    industry: str | None,
    form_10k_text: str,
    segment_names: list[str],
    geo_names: list[str],
    fiscal_year: int | None,
) -> _ParsedLLMOutput:
    raw = generate_company_description(
        ticker=ticker,
        profile_description=profile_description,
        sector=sector,
        industry=industry,
        form_10k_text=form_10k_text,
        segment_names=segment_names,
        geo_names=geo_names,
        fiscal_year=fiscal_year,
    ).strip()
    if raw.startswith("```"):
        raw = JSON_FENCE_RE.sub("", raw).strip()
    parsed = cast("dict[str, object]", json.loads(raw))
    return _ParsedLLMOutput(
        elevator_pitch=_str_or_none(parsed.get("elevator_pitch")),
        business_overview=_str_or_none(parsed.get("business_overview")),
        revenue_model=_str_or_none(parsed.get("revenue_model")),
        segments=_coerce_named_rows(parsed.get("segments"), allowed=set(segment_names)),
        geographies=_coerce_named_rows(parsed.get("geographies"), allowed=set(geo_names)),
    )


def _str_or_none(v: object) -> str | None:
    if isinstance(v, str) and v.strip():
        return v.strip()
    return None


def _coerce_named_rows(raw: object, allowed: set[str]) -> list[dict[str, str | None]]:
    """Keep only rows whose `name` is in the allowed set; coerce to {name, description}."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str | None]] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        row_d = cast("dict[str, object]", row)
        name = row_d.get("name")
        if not isinstance(name, str) or name not in allowed:
            continue
        desc = row_d.get("description")
        out.append(
            {
                "name": name,
                "description": desc.strip() if isinstance(desc, str) and desc.strip() else None,
            }
        )
    return out


def _write_cache(path: Path, result: CompanyDescriptionResult) -> None:
    path.write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")
