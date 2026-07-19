"""LLM proposal generator for an unmapped FMP `industry` -> benchmark-ETF
proxy row (docs/design/comparable_sets_bottoms_up.md §4, Phase 3 ratification
flow).

``sector_benchmark_map.SECTOR_BENCHMARK_MAP`` is a plain, owner-ratified dict
-- this module NEVER writes to it. It only proposes a candidate
``{etf, sector_etf, why}`` for a given industry string, caches the proposal to
``data/sector_benchmark_proposals/{industry_key}.json`` for the owner to
review, and stops there. The owner hand-pastes the ratified line into
``sector_benchmark_map.py`` themselves (same governance shape as
``peer_selection`` / ``comparable_set_overrides``: LLM proposes, human
ratifies into a version-controlled file, never auto-applied).

This is "which published index ETF tracks this industry" -- a small,
factual lookup task, not judgment (mirrors ``extract_8k_overrides``'s
reasoning for landing on the Haiku-class ``FAST_CLASSIFIER_MODEL`` in
``llm.cli.LLM_MODELS``). One call per unmapped industry, on demand via
``execution/propose_sector_benchmarks.py`` -- never a standing pipeline
stage (§4's explicit "do not over-engineer this" ruling).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, field_validator

from llm.cli import FAST_CLASSIFIER_MODEL, LLM_MODELS
from llm.structured import StructuredParseError, call_llm_structured

log = logging.getLogger(__name__)

PURPOSE = "sector_benchmark_proposal"


class SectorBenchmarkSuggestion(BaseModel):
    """One LLM-proposed benchmark-ETF proxy for an industry. ``etf`` is the
    tightest-fit published index ETF (``None`` if none exists); ``sector_etf``
    is the coarser GICS-sector fallback ETF; ``why`` is a one-line rationale
    the owner reads while ratifying."""

    etf: str | None = None
    sector_etf: str | None = None
    why: str

    @field_validator("etf", "sector_etf")
    @classmethod
    def _norm_ticker(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().upper()
        return v or None

    @field_validator("why")
    @classmethod
    def _norm_why(cls, v: str) -> str:
        return v.strip()


@dataclass
class SectorBenchmarkProposal:
    industry: str
    industry_key: str
    etf: str | None
    sector_etf: str | None
    why: str
    model: str
    proposed_at: str
    skipped_reason: str | None = None


def industry_key(industry: str) -> str:
    """Filesystem-safe cache key for an industry string (e.g. ``"Banks -
    Regional"`` -> ``"banks_regional"``). Collapses non-alnum runs to a single
    underscore; deterministic and greppable."""
    slug = re.sub(r"[^a-z0-9]+", "_", industry.strip().lower()).strip("_")
    return slug or "unknown"


def _build_prompt(industry: str) -> str:
    return (
        "You are proposing which published, liquid, US-listed index ETF tracks "
        f'the FMP industry classification "{industry}" -- a pure factual lookup, '
        "not a judgment call.\n\n"
        "Return ONLY a JSON object exactly:\n"
        '  {"etf": "<ticker of the tightest-fit dedicated ETF for this industry, '
        'or null if none exists>", '
        '"sector_etf": "<ticker of the coarser GICS-sector ETF this industry '
        'rolls up into, or null if genuinely unclear>", '
        '"why": "<one concise clause citing what the ETF tracks>"}\n'
        "Rules:\n"
        "- Prefer widely-held, liquid ETFs (e.g. SPDR/iShares/VanEck sector and "
        "industry funds) over obscure or thinly-traded wrappers.\n"
        "- `etf` is null when no dedicated industry ETF exists (common for a "
        "narrow or niche industry) -- do not force a loose match.\n"
        "- `sector_etf` should almost always resolve to one of the 11 GICS "
        "sector SPDRs (XLK, XLF, XLV, XLE, XLY, XLP, XLI, XLB, XLU, XLRE, XLC) "
        "or a close equivalent.\n"
        "- Never invent a ticker that does not exist.\n"
        "Return the JSON object and nothing else."
    )


def propose_benchmark(
    industry: str,
    *,
    model: str | None = None,
    backend: str | None = None,
) -> SectorBenchmarkSuggestion:
    """One LLM call -> a validated benchmark-ETF suggestion for ``industry``.

    Raises ``StructuredParseError`` when the model returns unusable JSON on
    both attempts (the caller degrades to a skipped-reason proposal, never
    crashes). Hard stops (budget cap / missing CLI) propagate per
    ``call_llm_structured``.
    """
    prompt = _build_prompt(industry)
    payload = call_llm_structured(
        prompt,
        purpose=PURPOSE,
        ticker=None,
        scope=industry_key(industry),
        model=model,
        backend=backend,
        expect="object",
    )
    if not isinstance(payload, dict):
        raise StructuredParseError(f"expected a JSON object, got {type(payload).__name__}")
    return SectorBenchmarkSuggestion.model_validate(cast("dict[str, object]", payload))


# ---------------------------------------------------------------------------
# cache (the review-queue artifact the owner reads)
# ---------------------------------------------------------------------------


def _cache_path(repo_root: Path, industry: str) -> Path:
    out_dir = repo_root / "data" / "sector_benchmark_proposals"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{industry_key(industry)}.json"


def _stamp(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def extract_for_industry(
    industry: str,
    repo_root: Path,
    *,
    refresh: bool = False,
) -> SectorBenchmarkProposal:
    """Propose + cache one industry's benchmark-ETF row for owner review.

    Idempotent: a cached proposal is reused unless ``refresh=True``. Never
    raises for an LLM/parse failure -- recorded in ``skipped_reason`` so the
    CLI can report a clean per-industry summary and keep going."""
    cache_path = _cache_path(repo_root, industry)
    if not refresh and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cached = None
        if isinstance(cached, dict):
            return SectorBenchmarkProposal(**cast("dict[str, Any]", cached))

    start = datetime.now(UTC)
    model = LLM_MODELS.get(PURPOSE, FAST_CLASSIFIER_MODEL)
    try:
        sug = propose_benchmark(industry, model=model)
    except StructuredParseError as exc:
        result = SectorBenchmarkProposal(
            industry=industry,
            industry_key=industry_key(industry),
            etf=None,
            sector_etf=None,
            why="",
            model=model,
            proposed_at=_stamp(start),
            skipped_reason=f"llm parse failure: {exc}",
        )
        cache_path.write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")
        return result

    result = SectorBenchmarkProposal(
        industry=industry,
        industry_key=industry_key(industry),
        etf=sug.etf,
        sector_etf=sug.sector_etf,
        why=sug.why,
        model=model,
        proposed_at=_stamp(start),
        skipped_reason=None,
    )
    cache_path.write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")
    return result
