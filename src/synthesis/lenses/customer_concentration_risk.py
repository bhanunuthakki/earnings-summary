"""customer_concentration_risk lens — portfolio-wide single-customer exposure.

Reads customer_concentrations for every portfolio holding, separates
named-customer disclosures from anonymized labels ("Customer A"),
ranks by exposure, and flags cross-holding overlap.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ._shared import (
    Lens,
    LensContext,
    sha8,
)

_PROMPT_CUSTOMER_CONCENTRATION_RISK = """You are writing the portfolio's customer-concentration risk memo. The
analyst wants to see, in one read, which holdings have material revenue
concentration in a single named customer, where those concentrations
overlap across the book, and how each holding's concentration is
trending.

**Portfolio customer-concentration disclosures (extracted from 10-Ks):**
{concentrations_block}

**Anonymized disclosures (issuer-scoped labels like 'Customer A'):**
{anonymized_block}

Write a 400-600 word memo, three sections, with explicit tickers and
percentages — no hand-waving.

## 1. Highest exposures right now
Rank-ordered list (descending pct_of_revenue) of the top single-customer
exposures across the portfolio. For each: ticker, customer, pct, the
fiscal period, and one sentence on what the dependency means
strategically (is the customer also a competitor? a hyperscaler the
holding's product runs on? a regulator? a single distributor?). Skip
exposures below 5%.

## 2. Correlated risk (shared customers)
Customers that appear in more than one holding's disclosures. These are
the cross-portfolio dependencies the analyst needs to flag — a single
customer disappointment hits multiple positions. If none, say so.

## 3. Trend + watchlist
For each holding with multi-year disclosures, is the concentration
increasing or decreasing? Anonymized disclosures: which ones look like
they should be tracked through subsequent filings (e.g., a label that
showed up new this year, or one whose share is climbing)?

Voice: senior analyst writing for themselves. Terse. Cite numbers. If
the data is sparse (few holdings disclose), say so plainly — don't pad.
"""


def _ctx_customer_concentration_risk(ticker: str | None, repo_root: Path) -> LensContext | None:
    """Read every customer_concentrations row for portfolio holdings and
    bucket into named vs anonymized. Returns None when there are no rows
    at all (lens skipped until the extractor has populated something)."""
    del ticker  # portfolio scope
    db = repo_root / "data" / "portfolio.db"
    if not db.exists():
        return None
    conn = sqlite3.connect(str(db))
    try:
        present = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='customer_concentrations'"
        ).fetchone()
        if present is None:
            return None
        rows = conn.execute(
            """
            SELECT cc.ticker, cc.fiscal_period, cc.customer_label,
                   cc.pct_of_revenue, cc.revenue_amount,
                   cc.customer_entity_id, cc.source_excerpt
            FROM customer_concentrations cc
            INNER JOIN tracked_companies tc ON tc.ticker = cc.ticker
            WHERE tc.list_type = 'portfolio' AND tc.archived_at IS NULL
            ORDER BY cc.pct_of_revenue DESC, cc.ticker
            """
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return None
    named_lines: list[str] = []
    anon_lines: list[str] = []
    for r in rows:
        ticker_, fiscal_period, label, pct, rev_amt, ent_id, excerpt = r
        pct_str = f"{float(pct) * 100:.1f}%" if pct is not None else "?"
        rev_str = (
            f"${float(rev_amt) / 1e3:.1f}B"
            if rev_amt and float(rev_amt) >= 1e3
            else f"${float(rev_amt):.0f}M"
            if rev_amt
            else "?"
        )
        line = f"- {ticker_} FY{fiscal_period} · {label} · {pct_str} of revenue · {rev_str}"
        if excerpt:
            line += f"\n    > {str(excerpt)[:200]}"
        # Anonymized when the entity row has the meta flag, or the label
        # matches the well-known anonymized patterns we tag at extraction.
        is_anon = bool(
            ent_id is None
            and any(
                s in str(label).lower() for s in ("customer ", "a major", "a single", "undisclosed")
            )
        )
        (anon_lines if is_anon else named_lines).append(line)
    return LensContext(
        ticker=None,
        template_kwargs={
            "concentrations_block": (
                "\n".join(named_lines) if named_lines else "(no named-customer disclosures yet)"
            ),
            "anonymized_block": (
                "\n".join(anon_lines) if anon_lines else "(no anonymized-customer disclosures yet)"
            ),
        },
        cache_inputs=[
            "portfolio",
            sha8("\n".join(named_lines)),
            sha8("\n".join(anon_lines)),
        ],
        source_doc_ids=[],
        parent_artifact_ids=[],
    )


LENS = Lens(
    name="customer_concentration_risk",
    model="claude-sonnet-4-6",
    scope="portfolio",
    prompt_template=_PROMPT_CUSTOMER_CONCENTRATION_RISK,
    build_context=_ctx_customer_concentration_risk,
)
