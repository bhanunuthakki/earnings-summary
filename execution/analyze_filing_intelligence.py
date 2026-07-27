"""10-K/10-Q Deep Filing Intelligence Analyzer.

Implements the Focus Algorithm to extract segment footnotes, accounting policies,
commitments, and contingencies from the latest 10-K filing. Runs deep buy-side
LLM analysis to synthesize reporting shifts, metric redefinitions, executive
incentives alignment, and tail risks.

Usage:
    python execution/analyze_filing_intelligence.py --ticker AMZN
    python execution/analyze_filing_intelligence.py --ticker AMZN --year 2025
    python execution/analyze_filing_intelligence.py --ticker AMZN --refresh
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db  # noqa: E402
from llm_client import DEFAULT_MODEL, JSON_FENCE_RE, call_llm  # noqa: E402

log = logging.getLogger("analyze_filing_intelligence")

_KEYWORDS = (
    "description of business",
    "segment",
    "geographic",
    "significant accounting",
    "commitments and contingencies",
    "income taxes",
    "stockholders' equity",
)
_MAX_TEXT_BUDGET = 60000


class SegmentChange(BaseModel):
    has_changes: bool
    description: str | None = None


class MetricRedefinition(BaseModel):
    has_changes: bool
    description: str | None = None


class ExecutiveCompAlignment(BaseModel):
    metrics_used: list[str] = Field(default_factory=list)
    targets_and_thresholds: str | None = None
    alignment_verdict: str | None = None


class InvestmentSignal(BaseModel):
    signal_type: str  # e.g. "Inventory", "Working Capital", "Litigation", "Tail Risk"
    severity: Literal["High", "Medium", "Low"]
    description: str


class FilingIntelligenceSummary(BaseModel):
    ticker: str
    fiscal_year: int
    analyzed_at: str
    segment_changes: SegmentChange
    metric_redefinitions: MetricRedefinition
    executive_comp: ExecutiveCompAlignment
    investment_signals: list[InvestmentSignal] = Field(default_factory=list)
    raw_synthesis_md: str


@dataclass
class FilingIntelligenceResult:
    ticker: str
    fiscal_year: int | None
    source_path: str | None
    source_sha256: str | None
    analyzed_at: str | None = None
    elapsed_ms: int = 0
    model: str = DEFAULT_MODEL
    summary: dict | None = None
    skipped_reason: str | None = None


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


def _flatten(node: object) -> list[str]:
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


def _extract_relevant_text(payload: dict[str, object]) -> str:
    parts: list[str] = []
    total_chars = 0
    for key, value in payload.items():
        key_lower = str(key).lower()
        if not any(kw in key_lower for kw in _KEYWORDS):
            continue
        section_text = "\n".join(_flatten(value))
        if not section_text.strip():
            continue
        if total_chars + len(section_text) > _MAX_TEXT_BUDGET:
            section_text = section_text[: _MAX_TEXT_BUDGET - total_chars]
        parts.append(f"=== SECTION: {key} ===\n{section_text}")
        total_chars += len(section_text)
        if total_chars >= _MAX_TEXT_BUDGET:
            break
    return "\n\n".join(parts)


def _is_s1_anchored(ticker: str, repo_root: Path) -> bool:
    path = repo_root / "micro_thesis" / "holdings" / f"{ticker.upper()}.json"
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    raw = payload.get("data_anchor") if isinstance(payload, dict) else None
    return isinstance(raw, str) and raw.strip().lower() == "s1"


def analyze_for_ticker(
    ticker: str,
    repo_root: Path,
    fiscal_year: int | None = None,
    refresh: bool = False,
) -> FilingIntelligenceResult:
    ticker = ticker.upper()
    out_dir = repo_root / "data" / "filing_intelligence"
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_path = out_dir / f"{ticker}.json"

    # Recently-IPO'd issuers anchor on the S-1; filing-intelligence
    # extraction (segment shifts vs PY, metric redefinitions, exec-comp
    # alignment) fundamentally requires a 10-K + prior-year baseline. Skip
    # cleanly with a descriptive reason rather than crashing on missing
    # FMP JSON.
    if _is_s1_anchored(ticker, repo_root):
        return FilingIntelligenceResult(
            ticker=ticker,
            fiscal_year=None,
            source_path=None,
            source_sha256=None,
            skipped_reason=(
                f"{ticker} is anchored on S-1 (recently IPO'd) — filing-intelligence "
                f"extraction requires a 10-K with a prior-year baseline"
            ),
        )

    source_path, year = _locate_form_10k(repo_root, ticker, fiscal_year)
    if source_path is None:
        return FilingIntelligenceResult(
            ticker=ticker,
            fiscal_year=None,
            source_path=None,
            source_sha256=None,
            skipped_reason=f"no _form_10k_*.json found under data/historical/fmp/ for {ticker}",
        )

    raw_bytes = source_path.read_bytes()
    sha256 = hashlib.sha256(raw_bytes).hexdigest()

    if not refresh and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("source_sha256") == sha256 and cached.get("fiscal_year") == year:
                return FilingIntelligenceResult(**cached)
        except Exception:
            pass

    payload = json.loads(raw_bytes.decode("utf-8"))
    relevant_text = _extract_relevant_text(payload)
    if not relevant_text.strip():
        return FilingIntelligenceResult(
            ticker=ticker,
            fiscal_year=year,
            source_path=str(source_path),
            source_sha256=sha256,
            skipped_reason="form_10k has no footnote section matching keywords",
        )

    start_dt = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    t0 = time.perf_counter()

    prompt = f"""You are a Senior buy-side strategic analyst conducting a deep 10-K filing review for {ticker} (FY {year}).

Below is high-density SEC narrative extracted from Note disclosures (including Description of Business, Segment Information, Income Taxes, Commitments/Contingencies).

Your task is to synthesize a high-fidelity analytical review of segment reporting shifts, metric redefinitions, executive compensation alignment, and critical investment tail signals.

SEC Footnote Disclosures:
\"\"\"
{relevant_text}
\"\"\"

Produce a structured JSON response matching the following JSON schema:
{{
  "ticker": "{ticker}",
  "fiscal_year": {year},
  "analyzed_at": "{start_dt}",
  "segment_changes": {{
    "has_changes": boolean,
    "description": "Buy-side explanation of product shifts, reclassifications, or segment boundary changes. Null if none."
  }},
  "metric_redefinitions": {{
    "has_changes": boolean,
    "description": "Explanation of shifts in how operational/financial metrics are defined or calculated between periods. Null if none."
  }},
  "executive_comp": {{
    "metrics_used": ["List of metrics used for executive bonuses"],
    "targets_and_thresholds": "Bonus targets or performance thresholds management must hit",
    "alignment_verdict": "1-2 sentences assessing whether executive compensation metrics align with long-term margin/FCF expansion."
  }},
  "investment_signals": [
    {{
      "signal_type": "Inventory / Working Capital / Litigation / Tail Risk",
      "severity": "High / Medium / Low",
      "description": "Clear explanation of the signal and its investment implications"
    }}
  ],
  "raw_synthesis_md": "Beautiful, flowing senior-analyst markdown summary (300-600 words) summarizing segment boundaries, structural changes, executive comp, and risk signals."
}}

Return ONLY the valid JSON object. No markdown fence, no commentary, no conversational filler.
"""

    raw = call_llm(prompt, purpose="strategic_analysis").strip()
    if raw.startswith("```"):
        raw = JSON_FENCE_RE.sub("", raw).strip()

    # Parse and validate schema
    parsed = json.loads(raw)
    validated = FilingIntelligenceSummary(**parsed)

    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    result = FilingIntelligenceResult(
        ticker=ticker,
        fiscal_year=year,
        source_path=str(source_path),
        source_sha256=sha256,
        analyzed_at=start_dt,
        elapsed_ms=elapsed_ms,
        model=DEFAULT_MODEL,
        summary=validated.model_dump(),
    )

    cache_path.write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", required=True, help="Single ticker")
    parser.add_argument("--year", type=int, default=None, help="Specific year")
    parser.add_argument("--refresh", action="store_true", help="Ignore cache")
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    args = parser.parse_args(argv)

    # An explicit --repo-root must also reach the governed LLM cost ledger. The
    # deep `strategic_analysis` call resolves the llm_calls DB (plus the budget
    # check and the LLM_MODELS pin) from the `db.DB_PATH` process global, which
    # this script otherwise never syncs — so a run against another checkout's
    # data/ (e.g. --repo-root <MAIN> from a worktree) would silently land the
    # cost row in the default DB. set_db_path also re-points DATA_DIR / FMP_DIR.
    db.set_db_path(args.repo_root / "data" / "portfolio.db")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    t0 = time.perf_counter()
    result = analyze_for_ticker(
        args.ticker,
        args.repo_root,
        fiscal_year=args.year,
        refresh=args.refresh,
    )
    elapsed = time.perf_counter() - t0

    if result.skipped_reason:
        print(f"[skip] {args.ticker}: {result.skipped_reason}")
        return 0

    print(
        json.dumps(
            {
                "ticker": result.ticker,
                "fiscal_year": result.fiscal_year,
                "elapsed_ms": result.elapsed_ms,
                "total_wall_seconds": f"{elapsed:.2f}",
                "signals_found": len(result.summary.get("investment_signals") or [])
                if result.summary
                else 0,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
