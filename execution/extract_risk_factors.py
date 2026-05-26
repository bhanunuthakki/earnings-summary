"""Extract 10-K Item 1A risk factors → risk_factors table.

Pipeline:
  1. fetch_latest_10k_text(ticker) — pulls 10-K from SEC EDGAR, caches text.
  2. split_risk_factors(item_1a_text) — splits into [(heading, body), ...]
  3. For each risk: deterministic categorization (regex) + LLM classification
     (Haiku, batched per ticker — one prompt per 10-K, not per risk).
  4. Year-over-year diff: compare against prior fiscal year's rows.
     - same body_sha256 → unchanged
     - new heading → new
     - heading exists prior year but missing now → removed
     - heading present both years but body differs → reworded; LLM produces
       a brief diff_md describing the change.
  5. Write rows to risk_factors. Idempotent on (ticker, fiscal_year, ordinal).

Usage:
    python execution/extract_risk_factors.py --ticker GOOG
    python execution/extract_risk_factors.py --ticker GOOG --fiscal-year 2024
    python execution/extract_risk_factors.py --all-portfolio
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db  # noqa: E402

from filing_text_fetcher import (  # noqa: E402
    fetch_latest_10k_text,
    split_risk_factors,
)
from llm_client import JSON_FENCE_RE, call_llm  # noqa: E402

log = logging.getLogger("extract_risk_factors")


_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "regulatory": ["regulation", "regulator", "antitrust", "compliance", "DOJ", "FTC", "EU", "GDPR", "ESG", "tax law"],
    "operational": ["operations", "infrastructure", "supply chain", "datacenter", "workforce", "talent"],
    "technology": ["cybersecurity", "AI", "data", "intellectual property", "patent", "cyber", "security breach"],
    "financial": ["liquidity", "interest rate", "credit", "debt", "covenant", "currency", "foreign exchange"],
    "macro": ["macroeconomic", "recession", "inflation", "geopolitical", "war", "trade", "tariff"],
    "litigation": ["litigation", "lawsuit", "claim", "court", "settlement", "judgment"],
    "competition": ["competitor", "competition", "market share", "pricing pressure"],
    "product": ["product", "launch", "platform", "feature", "demand"],
}


def _categorize_deterministic(heading: str, body: str) -> str | None:
    """Cheap regex categorization — assigns the first matching category by
    keyword presence in heading + body. Returns None if no match (LLM fallback
    will classify)."""
    text = f"{heading} {body}".lower()
    for category, keywords in _CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in text:
                return category
    return None


def _llm_classify_risks(
    *, ticker: str, fiscal_year: int, risks: list[tuple[str, str]]
) -> dict[int, str]:
    """Batch LLM call: send all uncategorized risks at once and get back
    a {ordinal: category} mapping. Returns {} on parse failure."""
    if not risks:
        return {}
    payload = [
        {"id": i, "heading": h[:300], "body_head": b[:600]} for i, (h, b) in enumerate(risks)
    ]
    prompt = f"""You are categorizing risk factors from {ticker}'s 10-K (fiscal year {fiscal_year}).
For each risk factor, return ONE category from this fixed set:
- regulatory
- operational
- technology
- financial
- macro
- litigation
- competition
- product
- other

Risks (JSON):
{json.dumps(payload, ensure_ascii=False)}

Return ONLY a JSON object {{"<id>": "<category>", ...}}.
Use "other" only when no category fits."""
    try:
        raw = call_llm(
            prompt,
            purpose="risk_factor_classify",
            ticker=ticker,
            model="claude-haiku-4-5-20251001",
        ).strip()
    except Exception as exc:  # noqa: BLE001
        log.warning({"event": "risk_classify_failed", "ticker": ticker, "error": str(exc)})
        return {}
    if raw.startswith("```"):
        raw = JSON_FENCE_RE.sub("", raw).strip()
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        log.warning({"event": "risk_classify_parse_failed", "ticker": ticker})
        return {}
    out: dict[int, str] = {}
    if isinstance(decoded, dict):
        for k, v in decoded.items():
            try:
                out[int(k)] = str(v)
            except (ValueError, TypeError):
                continue
    return out


def _prior_year_index(
    conn: sqlite3.Connection, *, ticker: str, fiscal_year: int
) -> dict[str, dict[str, object]]:
    """Index prior-year risk_factors by normalized heading for diffing."""
    rows = conn.execute(
        """
        SELECT heading, body_sha256, body_md
        FROM risk_factors
        WHERE ticker = ? AND fiscal_year = ?
        """,
        (ticker, fiscal_year - 1),
    ).fetchall()
    out: dict[str, dict[str, object]] = {}
    for r in rows:
        heading = (r[0] or "").lower().strip()
        out[heading] = {"body_sha256": r[1], "body_md": r[2] or ""}
    return out


def _llm_diff_one(*, ticker: str, heading: str, old: str, new: str) -> str | None:
    """One-call diff narrative — summarizes what changed in 1-2 sentences."""
    prompt = f"""You are summarizing how a single risk factor was reworded between
two fiscal years of {ticker}'s 10-K.

HEADING: {heading[:200]}

PRIOR YEAR BODY (truncated):
{old[:2500]}

CURRENT YEAR BODY (truncated):
{new[:2500]}

In 1-3 sentences, summarize what materially changed. Focus on:
- Severity language changes ("could" → "may materially adversely")
- New mechanisms / risks named
- Removed mechanisms / risks
- Quantitative changes (specific numbers, percentages)

If the change is purely cosmetic (whitespace, capitalization, immaterial), say
"no material rewording."

Return only the summary text, no JSON."""
    try:
        out = call_llm(
            prompt,
            purpose="risk_factor_diff",
            ticker=ticker,
            model="claude-haiku-4-5-20251001",
        ).strip()
        if "no material rewording" in out.lower():
            return None
        return out[:1500]
    except Exception as exc:  # noqa: BLE001
        log.warning({"event": "risk_diff_failed", "ticker": ticker, "error": str(exc)})
        return None


def extract_for_ticker(
    *,
    ticker: str,
    repo_root: Path,
    user_agent: str,
    fiscal_year: int | None = None,
    do_diff: bool = True,
) -> dict[str, object]:
    """Extract risk factors for one ticker, classify, diff, write to DB."""
    cache_dir = repo_root / "data" / "sec_text"
    result = fetch_latest_10k_text(
        ticker=ticker, user_agent=user_agent, cache_dir=cache_dir, fiscal_year=fiscal_year
    )
    if result is None or not result.item_1a_text:
        return {"ticker": ticker, "status": "no_item_1a", "n": 0}

    risks = split_risk_factors(result.item_1a_text)
    if not risks:
        return {"ticker": ticker, "status": "no_risks_parsed", "n": 0}

    # Deterministic categorization first; LLM fills the gaps
    categories: dict[int, str] = {}
    uncategorized: list[tuple[str, str]] = []
    uncategorized_indices: list[int] = []
    for i, (heading, body) in enumerate(risks):
        cat = _categorize_deterministic(heading, body)
        if cat:
            categories[i] = cat
        else:
            uncategorized.append((heading, body))
            uncategorized_indices.append(i)

    if uncategorized:
        # Batch LLM classification (single call for all uncategorized risks)
        llm_map = _llm_classify_risks(
            ticker=ticker, fiscal_year=result.fiscal_year, risks=uncategorized
        )
        for local_idx, original_idx in enumerate(uncategorized_indices):
            categories[original_idx] = llm_map.get(local_idx, "other")

    # Write to DB
    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        return {"ticker": ticker, "status": "no_db", "n": 0}

    conn = sqlite3.connect(str(db_path))
    try:
        prior = _prior_year_index(conn, ticker=ticker, fiscal_year=result.fiscal_year) if do_diff else {}
        inserted = 0
        for ordinal, (heading, body) in enumerate(risks):
            body_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
            heading_key = heading.lower().strip()
            vs_prior = None
            reword_diff = None
            if prior:
                if heading_key not in prior:
                    vs_prior = "new"
                elif prior[heading_key].get("body_sha256") == body_sha:
                    vs_prior = "unchanged"
                else:
                    vs_prior = "reworded"
                    if do_diff:
                        reword_diff = _llm_diff_one(
                            ticker=ticker,
                            heading=heading,
                            old=str(prior[heading_key].get("body_md") or ""),
                            new=body,
                        )
            try:
                conn.execute(
                    """
                    INSERT INTO risk_factors(
                        ticker, fiscal_year, ordinal, heading, body_md,
                        category, vs_prior_year, reword_diff_md, body_sha256,
                        extracted_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        ticker,
                        result.fiscal_year,
                        ordinal,
                        heading[:512],
                        body,
                        categories.get(ordinal, "other"),
                        vs_prior,
                        reword_diff,
                        body_sha,
                        datetime.now(UTC).isoformat(),
                    ),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                continue  # idempotent on (ticker, fiscal_year, ordinal)

        # Mark prior-year-only headings as "removed" rows ordinal-shifted
        if prior:
            current_headings = {h.lower().strip() for h, _ in risks}
            for prior_heading_key in prior.keys() - current_headings:
                # Insert a placeholder row with vs_prior_year='removed'
                try:
                    conn.execute(
                        """
                        INSERT INTO risk_factors(
                            ticker, fiscal_year, ordinal, heading, body_md,
                            category, vs_prior_year, body_sha256, extracted_at
                        ) VALUES (?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            ticker,
                            result.fiscal_year,
                            10_000 + len(risks),  # synthetic ordinal at end
                            prior_heading_key[:512],
                            "(removed in current year — see prior FY for body)",
                            "other",
                            "removed",
                            hashlib.sha256(b"removed").hexdigest(),
                            datetime.now(UTC).isoformat(),
                        ),
                    )
                    inserted += 1
                except sqlite3.IntegrityError:
                    continue

        conn.commit()
    finally:
        conn.close()

    return {
        "ticker": ticker,
        "fiscal_year": result.fiscal_year,
        "n_risks_parsed": len(risks),
        "n_inserted": inserted,
        "status": "ok",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--ticker", help="Single ticker.")
    g.add_argument("--all-portfolio", action="store_true", help="Run for all portfolio holdings.")
    g.add_argument("--all-watchlist", action="store_true", help="Run for all watchlist tickers too.")
    parser.add_argument("--fiscal-year", type=int, default=None, help="Specific fiscal year (default: latest).")
    parser.add_argument("--no-diff", action="store_true", help="Skip year-over-year diff (no LLM diff calls).")
    parser.add_argument(
        "--repo-root", type=Path, default=PROJECT_ROOT, help="Repo root."
    )
    parser.add_argument(
        "--user-agent",
        default="EarningsSummary/1.0 (bhanumufcpraneeth@gmail.com)",
        help="SEC EDGAR User-Agent.",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # Sync db.DB_PATH for the ledger writer
    db.PROJECT_ROOT = str(args.repo_root)
    db.DATA_DIR = str(args.repo_root / "data")
    db.DB_PATH = str(args.repo_root / "data" / "portfolio.db")
    db.FMP_DIR = str(args.repo_root / "data" / "historical" / "fmp")

    if args.ticker:
        tickers = [args.ticker.upper()]
    else:
        conn = sqlite3.connect(str(args.repo_root / "data" / "portfolio.db"))
        list_types = ["portfolio"] if args.all_portfolio else ["portfolio", "watchlist"]
        placeholders = ",".join("?" * len(list_types))
        tickers = [
            r[0]
            for r in conn.execute(
                f"""SELECT ticker FROM tracked_companies
                WHERE archived_at IS NULL AND list_type IN ({placeholders}) ORDER BY ticker""",
                list_types,
            )
        ]
        conn.close()

    total_inserted = 0
    for ticker in tickers:
        outcome = extract_for_ticker(
            ticker=ticker,
            repo_root=args.repo_root,
            user_agent=args.user_agent,
            fiscal_year=args.fiscal_year,
            do_diff=not args.no_diff,
        )
        log.info(outcome)
        total_inserted += int(outcome.get("n_inserted", 0))
    print(f"\nDone · {total_inserted} risk factor rows inserted across {len(tickers)} ticker(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
