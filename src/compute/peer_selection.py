"""LLM-driven peer selection — the *generator* behind the §4 peer-comp panel.

The FMP ``stock_peers`` screen is a sector/market-cap list whose head is often
wrong (the owner flagged NU → Barclays, NOW → Applied Materials): an
industry/cap screen has no notion of *business-model comparability*. NU's real
comps are MELI Credit / Inter / StoneCo / Itaú's digital arm — not "diversified
banks by market cap". S5 made the panel *steerable* (pin/exclude/quality-gate)
but left the auto-selection unchanged; this module fixes the generator.

Pipeline (runs on the ``--enable-llm`` build, NOT on render):
  1. Gather the inputs the LLM needs — company name, a business description
     (company_description cache → thesis → FMP profile blurb), reported
     segments, and the FMP sector/industry as *hints*.
  2. One ``peer_selection`` LLM call → 6-10 business-model comparables, each
     with a one-line ``why`` (schema-validated; degrade to the FMP screen on
     parse failure — never crash the build).
  3. Best-effort fetch of each suggested peer's FMP fundamentals (profile +
     key-metrics-ttm + ratios-ttm + quarterly income) so the panel's multiples
     resolve instead of rendering a wall of em-dashes. Free-tier (stable)
     only, budget-capped, per-file resumable across builds.
  4. Cache the set to ``data/peer_selection/{TICKER}.json`` keyed on an input
     sha256 — stable quarter-to-quarter, re-run only on ``--refresh`` or when
     the inputs change.

The renderer (``report.sections.p3_data.load_peer_comp``) reads the cache
directly (no heavy import on the render path) and merges the suggestions into
the existing scored screen, riding the S5 curation plumbing
(``competitive_watchlist`` pins / ``peer_exclude`` / ``peers_section_override``
all still win). Absent cache → ``load_peer_comp`` behaves exactly as before.
See ``directives/peer_selection_llm.md``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ValidationError, field_validator

from llm.cli import DEFAULT_MODEL, LLM_MODELS
from llm.structured import StructuredParseError, call_llm_structured

log = logging.getLogger(__name__)

PURPOSE = "peer_selection"

# An exchange-ticker-shaped token: leading letter, ≤7 chars, no spaces. Mirrors
# p3_data._TICKER_RX so a hallucinated prose "ticker" ("Nubank Brazil") never
# seeds the FMP pool.
_TICKER_RX = re.compile(r"[A-Z][A-Z0-9.\-]{0,6}")

_MAX_DESC_CHARS = 4000
# Per-build FMP-fetch ceiling: 4 stable calls x up to 10 peers. Free tier is
# 250 calls/day; cap so one build can't starve the daily budget on a long peer
# set. The per-file skip in _fetch_peer_fundamentals makes re-runs resumable:
# each run tops up only the files still missing, so a budget-capped (or 429'd)
# fetch completes over successive builds instead of stalling forever.
_MAX_FETCH_CALLS = 40
_FETCH_ENDPOINTS: tuple[tuple[str, str, dict[str, str]], ...] = (
    # (stable endpoint path, cache-file suffix p3_data reads, extra params)
    ("profile", "profile", {}),
    ("key-metrics-ttm", "key_metrics_ttm", {}),
    ("ratios-ttm", "financial_ratios_ttm", {}),
    # The panel's revenue column sums the 4 newest quarterly income rows; the
    # stable key-metrics-ttm payload carries NO revenueTTM fallback, so without
    # this file a fetched peer still renders revenue as an em-dash.
    ("income-statement", "income_statement_quarterly", {"period": "quarter", "limit": "4"}),
)

# _stable_get sentinel: the FMP daily budget is exhausted (HTTP 429). Abort the
# whole fetch run instead of burning the remaining call budget on more 429s —
# the per-file skip resumes the top-up on the next build.
_RATE_LIMITED = object()


class PeerSuggestion(BaseModel):
    """One LLM-proposed comparable. ``why`` is the business-model rationale that
    becomes the panel's ``match_reasons`` 'why' column."""

    ticker: str
    name: str
    why: str

    @field_validator("ticker")
    @classmethod
    def _norm_ticker(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("name", "why")
    @classmethod
    def _norm_text(cls, v: str) -> str:
        return v.strip()


@dataclass
class PeerSelectionResult:
    ticker: str
    suggestions: list[dict[str, str]]  # [{ticker, name, why}], LLM order preserved
    inputs_sha256: str | None
    model: str = DEFAULT_MODEL
    extracted_at: str | None = None
    # A peer lands here the moment ANY of the 4 _FETCH_ENDPOINTS succeeded for
    # it — kept for backward compatibility with existing cache JSON and the
    # self-heal top-up check (`if result.suggestions` / `if topped`). Does NOT
    # mean the peer's multiples fully resolve: a free-tier-402 peer (e.g.
    # GRAB/INTR/KSPI/PAGS, where only `profile` is accessible) lands here too.
    fetched_peers: list[str] = field(default_factory=list[str])
    # Subset of fetched_peers where EVERY _FETCH_ENDPOINTS call succeeded —
    # the peer's multiples panel is expected to fully resolve, not just avoid
    # an all-dash row. Distinguishes "some data" from "all data" without
    # changing rendering (the renderer doesn't read either field; both are
    # cache-side bookkeeping consulted by tooling/diagnostics).
    fetched_complete: list[str] = field(default_factory=list[str])
    skipped_reason: str | None = None


# ---------------------------------------------------------------------------
# the LLM call (also the eval's production entry point)
# ---------------------------------------------------------------------------


def _build_prompt(
    *,
    ticker: str,
    name: str | None,
    business_description: str,
    segments: list[str],
    sector: str | None,
    industry: str | None,
    max_peers: int,
) -> str:
    label = f"{name} ({ticker})" if name else ticker
    seg_line = ", ".join(segments) if segments else "(not separately reported)"
    return (
        f"You are selecting public-market peer companies for {label} for a "
        "fundamental peer-comparison table used by a hedge-fund-style analyst.\n\n"
        "Choose the 6-10 BEST comparables by BUSINESS MODEL — how the company "
        "actually makes money, who its customers are, its unit economics, and its "
        "real competitive set — NOT merely the same GICS sector or a similar "
        "market cap. The right peers are frequently cross-sector, foreign-listed, "
        "or a different size; pick the true comparable even when the sector screen "
        "would miss it (e.g. a digital bank's peers are other digital-finance "
        "platforms, not diversified money-center banks by market cap).\n\n"
        f"Company: {label}\n"
        f"Sector (hint, not a constraint): {sector or 'unknown'}\n"
        f"Industry (hint, not a constraint): {industry or 'unknown'}\n"
        f"Reported segments: {seg_line}\n"
        "Business description:\n"
        f"{business_description or '(none available — infer from name/sector)'}\n\n"
        f"Return ONLY a JSON array of {max_peers} or fewer objects, each exactly:\n"
        '  {"ticker": "<exchange ticker>", "name": "<company name>", '
        '"why": "<one concise clause naming the shared business mechanic>"}\n'
        "Rules:\n"
        "- Only real, CURRENTLY-listed public companies — never one that was "
        "acquired, merged, or taken private (e.g. Squarespace is private now).\n"
        "- Ticker must be the US-market symbol: the US primary listing or the "
        "US ADR when one exists (SE not SEA for Sea Limited, SAP not SAPS, "
        "ADYEY not ADYEN.AS). Use a foreign-exchange symbol ONLY when the "
        "company has no US listing of any kind.\n"
        "- Rank closest business-model comparables first.\n"
        f"- Never include {ticker} itself.\n"
        "- `why` states the comparability basis, not a generic description.\n"
        "Return the JSON array and nothing else."
    )


def suggest_peers(
    *,
    ticker: str,
    name: str | None,
    business_description: str,
    segments: list[str],
    sector: str | None,
    industry: str | None,
    model: str | None = None,
    backend: str | None = None,
    max_peers: int = 10,
) -> list[PeerSuggestion]:
    """One LLM call → validated business-model comparables.

    Raises ``StructuredParseError`` when the model returns unusable JSON on both
    attempts (the caller degrades to the FMP screen — never crashes the build).
    Hard stops (budget cap / missing CLI) propagate per ``call_llm_structured``.
    Returns ``[]`` only when the LLM legitimately proposes nothing.
    """
    ticker = ticker.upper()
    prompt = _build_prompt(
        ticker=ticker,
        name=name,
        business_description=business_description[:_MAX_DESC_CHARS],
        segments=segments,
        sector=sector,
        industry=industry,
        max_peers=max_peers,
    )
    payload = call_llm_structured(
        prompt, purpose=PURPOSE, ticker=ticker, model=model, backend=backend, expect="array"
    )
    out: list[PeerSuggestion] = []
    seen: set[str] = set()
    for entry in cast("list[object]", payload):
        if not isinstance(entry, dict):
            continue
        try:
            sug = PeerSuggestion.model_validate(entry)
        except ValidationError:
            continue
        if not sug.ticker or not _TICKER_RX.fullmatch(sug.ticker):
            continue  # prose "ticker" / empty — would never resolve FMP metrics
        if sug.ticker == ticker or sug.ticker in seen:
            continue
        seen.add(sug.ticker)
        out.append(sug)
        if len(out) >= max_peers:
            break
    return out


# ---------------------------------------------------------------------------
# extract → cache (the build step)
# ---------------------------------------------------------------------------


def extract_for_ticker(
    ticker: str,
    repo_root: Path,
    db_conn: sqlite3.Connection,
    *,
    refresh: bool = False,
    max_peers: int = 10,
    fetch_fundamentals: bool = True,
) -> PeerSelectionResult:
    """End-to-end suggest → fetch → cache. Idempotent on the input sha256
    unless ``refresh=True``. Never raises for LLM/parse/fetch failure — those
    are recorded in ``skipped_reason`` and the renderer falls back to the FMP
    screen."""
    ticker = ticker.upper()
    cache_path = _cache_path(repo_root, ticker)

    name, sector, industry = _profile_identity(repo_root, ticker)
    business_description = _gather_business_description(repo_root, ticker)
    segments = _segment_names(db_conn, ticker)
    inputs_sha = _inputs_sha(name, business_description, segments, sector, industry)

    if not refresh and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cached = None
        if isinstance(cached, dict) and cached.get("inputs_sha256") == inputs_sha:
            result = PeerSelectionResult(**cast("dict[str, Any]", cached))
            # Self-heal: a cache hit skips the LLM but still tops up missing
            # peer fundamentals (per-file skip → 0 calls once complete). Every
            # pre-fix cache has fetched_peers=[] because the fetch never had an
            # FMP key; without this, those panels stay em-dash forever.
            if fetch_fundamentals and result.suggestions:
                topped = _fetch_peer_fundamentals(
                    [s["ticker"] for s in result.suggestions if s.get("ticker")],
                    repo_root,
                    self_ticker=ticker,
                )
                if topped.fetched_any or topped.fetched_complete:
                    result.fetched_peers = sorted({*result.fetched_peers, *topped.fetched_any})
                    result.fetched_complete = sorted(
                        {*result.fetched_complete, *topped.fetched_complete}
                    )
                    _write_cache(cache_path, result)
            return result

    start = datetime.now(UTC)
    try:
        suggestions = suggest_peers(
            ticker=ticker,
            name=name,
            business_description=business_description,
            segments=segments,
            sector=sector,
            industry=industry,
            max_peers=max_peers,
        )
    except StructuredParseError as exc:
        result = PeerSelectionResult(
            ticker=ticker,
            suggestions=[],
            inputs_sha256=inputs_sha,
            model=LLM_MODELS.get(PURPOSE, DEFAULT_MODEL),
            extracted_at=_stamp(start),
            skipped_reason=f"llm parse failure: {exc}",
        )
        _write_cache(cache_path, result)
        return result

    sug_dicts = [{"ticker": s.ticker, "name": s.name, "why": s.why} for s in suggestions]
    fetch_outcome = PeerFetchOutcome()
    if fetch_fundamentals and suggestions:
        fetch_outcome = _fetch_peer_fundamentals(
            [s.ticker for s in suggestions], repo_root, self_ticker=ticker
        )

    result = PeerSelectionResult(
        ticker=ticker,
        suggestions=sug_dicts,
        inputs_sha256=inputs_sha,
        model=LLM_MODELS.get(PURPOSE, DEFAULT_MODEL),
        extracted_at=_stamp(start),
        fetched_peers=fetch_outcome.fetched_any,
        fetched_complete=fetch_outcome.fetched_complete,
        skipped_reason=None if sug_dicts else "llm returned no peers",
    )
    _write_cache(cache_path, result)
    return result


def load_suggestions(repo_root: Path, ticker: str) -> list[PeerSuggestion]:
    """Read the cached suggestions, or ``[]`` on any miss. Tolerant — a
    malformed cache is treated as absent, never raised."""
    data = _read_json(_cache_path(repo_root, ticker))
    if not isinstance(data, dict):
        return []
    raw = cast("dict[str, object]", data).get("suggestions")
    if not isinstance(raw, list):
        return []
    out: list[PeerSuggestion] = []
    for entry in cast("list[object]", raw):
        if not isinstance(entry, dict):
            continue
        try:
            out.append(PeerSuggestion.model_validate(entry))
        except ValidationError:
            continue
    return out


# ---------------------------------------------------------------------------
# input gathering
# ---------------------------------------------------------------------------


def _profile_identity(repo_root: Path, ticker: str) -> tuple[str | None, str | None, str | None]:
    """(companyName, sector, industry) from the cached FMP profile."""
    path = repo_root / "data" / "historical" / "fmp" / f"{ticker}_profile.json"
    rec = _first_record(path)
    if rec is None:
        return None, None, None
    name = _str_or_none(rec.get("companyName"))
    sector = _str_or_none(rec.get("sector"))
    industry = _str_or_none(rec.get("industry"))
    return name, sector, industry


def _gather_business_description(repo_root: Path, ticker: str) -> str:
    """Best business description we have, in priority order: the
    company_description cache (analytical, grounded in the 10-K) → the owner's
    thesis → the FMP profile blurb. Concatenated up to the char budget."""
    parts: list[str] = []
    try:
        from compute.company_description import load_description

        desc = load_description(repo_root, ticker)
    except Exception:  # best-effort input gathering — never block on a bad cache
        desc = None
    if desc is not None and not desc.skipped_reason:
        for piece in (desc.elevator_pitch, desc.business_overview, desc.revenue_model):
            if isinstance(piece, str) and piece.strip():
                parts.append(piece.strip())
    if parts:
        return "\n\n".join(parts)[:_MAX_DESC_CHARS]

    thesis = _load_thesis(repo_root, ticker)
    if thesis:
        return thesis[:_MAX_DESC_CHARS]

    rec = _first_record(repo_root / "data" / "historical" / "fmp" / f"{ticker}_profile.json")
    if rec is not None:
        blurb = _str_or_none(rec.get("description"))
        if blurb:
            return blurb[:_MAX_DESC_CHARS]
    return ""


def _load_thesis(repo_root: Path, ticker: str) -> str:
    path = repo_root / "micro_thesis" / "holdings" / f"{ticker}.json"
    if not path.exists():
        return ""
    try:
        payload = cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return ""
    for key in ("thesis", "thesis_full", "thesis_one_liner"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _segment_names(conn: sqlite3.Connection, ticker: str) -> list[str]:
    """Distinct product-segment names for the ticker (the junction's
    (product, revenue) pair). Empty on any miss — segments are a hint."""
    try:
        cur = conn.execute(
            """
            SELECT DISTINCT sd.dim_name AS segment_name
            FROM segment_periods sp
            JOIN segment_dimensions sd ON sd.period_id = sp.id
            WHERE sp.ticker = ? AND sd.dim_type = 'product' AND sd.metric = 'revenue'
            ORDER BY sd.dim_name
            """,
            (ticker,),
        )
    except sqlite3.Error:
        return []
    return [str(r[0]) for r in cur.fetchall() if r[0]]


# ---------------------------------------------------------------------------
# FMP fundamentals fetch (best-effort, free-tier stable only)
# ---------------------------------------------------------------------------


def _resolve_fmp_key(repo_root: Path) -> str | None:
    """FMP_API_KEY from the environment, falling back to ``repo_root/.env``.

    The LLM build (``build_artifacts.py`` / ``extract_peer_selection.py``)
    never exports the key, so every pre-fix build silently skipped the whole
    fetch (``fetched_peers=[]`` across prod caches) and thesis peers rendered
    as em-dash walls. Reading .env directly (dotenv_values — no env mutation)
    matches where the rest of the FMP fleet keeps the key."""
    key = os.environ.get("FMP_API_KEY")
    if key:
        return key
    try:
        from dotenv import dotenv_values

        from runtime.secrets import project_env_file
    except ImportError:
        return None
    val = dotenv_values(project_env_file(repo_root)).get("FMP_API_KEY")
    return val.strip() if isinstance(val, str) and val.strip() else None


@dataclass
class PeerFetchOutcome:
    """Which suggested peers got FMP fundamentals this run, split by
    completeness — see ``_fetch_peer_fundamentals``."""

    fetched_any: list[str] = field(default_factory=list[str])
    fetched_complete: list[str] = field(default_factory=list[str])


def _fetch_peer_fundamentals(
    peers: list[str], repo_root: Path, *, self_ticker: str
) -> PeerFetchOutcome:
    """Fetch profile + key-metrics-ttm + ratios-ttm + quarterly income for each
    suggested peer, writing the JSON files ``load_peer_comp`` reads. Per-FILE
    skip: only endpoints whose cache file is missing are called, so a peer left
    half-fetched by an earlier budget hit completes here instead of being
    skipped forever (the old profile-exists check did exactly that), and a
    fully-cached peer costs zero calls.

    Self-contained (a focused stable-endpoint fetcher) rather than reusing
    ``execution/save_fmp_data.py``: that module ``sys.exit``s at import when
    ``FMP_API_KEY`` is unset and computes its cache dir from its own
    ``__file__`` (so it writes to the checkout, not the build's ``--repo-root``).
    Writing straight to ``repo_root`` keeps the fetched files where the renderer
    looks. Never raises — on a missing key or any HTTP error the peer simply
    renders metric-less and the existing all-dash filter drops it (unless it's
    also an owner-pinned named rival, which survives by design). A 429 (daily
    budget exhausted) aborts the remaining fetch outright; the next build
    resumes the top-up.

    Returns both ``fetched_any`` (at least one endpoint resolved — the
    pre-existing behavior, kept for cache/self-heal compatibility) and
    ``fetched_complete`` (every endpoint resolved, so the peer's multiples are
    expected to fully populate). Free-tier accounts get 402 on some `/stable`
    endpoints for certain symbols (observed for GRAB/INTR/KSPI/PAGS: `profile`
    succeeds, the other 3 don't) — those peers were previously indistinguishable
    from a fully-fetched peer in the cache."""
    api_key = _resolve_fmp_key(repo_root)
    if not api_key:
        log.info({"event": "peer_fetch_skipped_no_key", "ticker": self_ticker})
        return PeerFetchOutcome()
    fmp_dir = repo_root / "data" / "historical" / "fmp"
    fmp_dir.mkdir(parents=True, exist_ok=True)

    try:
        import requests  # late — keep the render/import path free of it
    except ImportError:
        return PeerFetchOutcome()

    fetched_any: list[str] = []
    fetched_complete: list[str] = []
    calls = 0
    for peer in peers:
        todo = [
            (endpoint, suffix, params)
            for endpoint, suffix, params in _FETCH_ENDPOINTS
            if not (fmp_dir / f"{peer}_{suffix}.json").exists()
        ]
        if not todo:
            continue  # fully cached (tracked peer or a completed prior run)
        peer_ok = False
        for endpoint, suffix, params in todo:
            if calls >= _MAX_FETCH_CALLS:
                log.warning(
                    {"event": "peer_fetch_budget_hit", "ticker": self_ticker, "calls": calls}
                )
                return PeerFetchOutcome(fetched_any=fetched_any, fetched_complete=fetched_complete)
            calls += 1
            body = _stable_get(requests, endpoint, peer, api_key, params)
            if body is _RATE_LIMITED:
                log.warning(
                    {"event": "peer_fetch_rate_limited", "ticker": self_ticker, "calls": calls}
                )
                return PeerFetchOutcome(fetched_any=fetched_any, fetched_complete=fetched_complete)
            if body is not None:
                (fmp_dir / f"{peer}_{suffix}.json").write_text(
                    json.dumps(body, indent=2), encoding="utf-8"
                )
                peer_ok = True
            time.sleep(0.1)  # gentle on the free-tier rate limit
        if peer_ok:
            fetched_any.append(peer)
            # Complete iff every endpoint missing at loop-start now exists on
            # disk (a 402/other error leaves its file unwritten).
            all_landed = all((fmp_dir / f"{peer}_{suffix}.json").exists() for _, suffix, _ in todo)
            if all_landed:
                fetched_complete.append(peer)
    return PeerFetchOutcome(fetched_any=fetched_any, fetched_complete=fetched_complete)


def _stable_get(
    requests_mod: Any,
    endpoint: str,
    symbol: str,
    api_key: str,
    extra_params: dict[str, str] | None = None,
) -> object | None:
    """One FMP /stable GET. Returns the parsed JSON list; ``_RATE_LIMITED`` on
    HTTP 429 (daily budget gone — the caller aborts the run); None on any other
    non-200 / empty / error. Mirrors save_fmp_data's stable URL shape.

    ``requests_mod`` is the dynamically-imported ``requests`` module typed as
    ``Any`` — the network boundary, kept off the import/render path on purpose.
    """
    url = f"https://financialmodelingprep.com/stable/{endpoint}"
    params = {"symbol": symbol, "apikey": api_key, **(extra_params or {})}
    try:
        resp = requests_mod.get(url, params=params, timeout=20)
    except Exception as exc:  # network best-effort — any failure → skip this file
        from log_redact import redact

        log.info(
            {
                "event": "peer_fetch_http_error",
                "symbol": symbol,
                "endpoint": endpoint,
                "error": redact(str(exc)[:120]),
            }
        )
        return None
    if resp.status_code == 429:
        return _RATE_LIMITED
    if resp.status_code != 200:
        return None
    try:
        body = resp.json()  # Any (requests boundary)
    except ValueError:
        return None
    if isinstance(body, list) and not body:
        return None  # accessible but empty — nothing to cache
    return cast("object", body)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _cache_path(repo_root: Path, ticker: str) -> Path:
    out_dir = repo_root / "data" / "peer_selection"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{ticker.upper()}.json"


def _inputs_sha(
    name: str | None,
    business_description: str,
    segments: list[str],
    sector: str | None,
    industry: str | None,
) -> str:
    blob = "\x00".join(
        [
            name or "",
            business_description,
            "|".join(segments),
            sector or "",
            industry or "",
        ]
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _read_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _first_record(path: Path) -> dict[str, object] | None:
    raw = _read_json(path)
    if isinstance(raw, list) and raw and isinstance(raw[0], dict):
        return cast("dict[str, object]", raw[0])
    if isinstance(raw, dict):
        return cast("dict[str, object]", raw)
    return None


def _str_or_none(v: object) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None


def _stamp(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _write_cache(path: Path, result: PeerSelectionResult) -> None:
    path.write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")
