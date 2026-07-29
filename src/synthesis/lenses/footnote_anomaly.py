"""footnote_anomaly lens — Item 8 footnote anomaly scan.

Surfaces related-party transactions, contingent liabilities, off-balance
items, lease commitments, restatements, and SBC footnote shifts that
99% of analysts skip.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

from ._shared import (
    Lens,
    LensContext,
    sha8,
    thesis_block,
)

_PROMPT_FOOTNOTE_ANOMALY = """You are scanning {ticker}'s Item 8 financial-statement footnotes for the
signals 99% of analysts ignore: related-party transactions, contingent
liabilities, off-balance-sheet items, lease commitments, restatements,
stock-based-comp footnote shifts.

**Thesis:**
{thesis_block}

**Footnote facts extracted from recent filings:**
{footnotes}

Produce a 250-400 word memo. For each anomaly worth surfacing (up to 5):

### [Fact type] · [Period]
- **The disclosure** (one sentence, with the specific amount + counterparty
  if available)
- **Why it's anomalous** (vs. last year, vs. peer norm, vs. management
  commentary)
- **Read for the thesis** (one sentence: is this a yellow flag, a real
  signal, or normal-course noise?)

If no anomalies surface, say so. If footnote_facts has no data for this
ticker yet, return a one-line "no footnote data ingested — run
`python execution/extract_footnotes.py --ticker {ticker}`."
"""


def _ctx_footnote_anomaly(ticker: str | None, repo_root: Path) -> LensContext | None:
    if not ticker:
        return None
    ticker = ticker.upper()
    db = repo_root / "data" / "portfolio.db"
    if not db.exists():
        return None
    conn = connect_sqlite(db, role=SQLiteConnectionRole.READ_ONLY)
    try:
        conn.row_factory = sqlite3.Row
        if (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='footnote_facts'"
            ).fetchone()
            is None
        ):
            return None
        rows = conn.execute(
            """
            SELECT period_end, fact_type, description_md, amount, currency, counterparty, status
            FROM footnote_facts WHERE ticker = ?
            ORDER BY period_end DESC LIMIT 80
            """,
            (ticker,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return None
    block = "\n".join(
        f"- {str(r['period_end'])[:10]} · {r['fact_type']} · "
        f"{float(r['amount'] or 0):,.0f} {r['currency'] or ''} · "
        f"{r['counterparty'] or '?'} · {r['status'] or '?'} · "
        f"{(r['description_md'] or '')[:200]}"
        for r in rows
    )
    return LensContext(
        ticker=ticker,
        template_kwargs={
            "ticker": ticker,
            "thesis_block": thesis_block(ticker, repo_root),
            "footnotes": block,
        },
        cache_inputs=[ticker, sha8(block)],
        source_doc_ids=[],
        parent_artifact_ids=[],
    )


LENS = Lens(
    name="footnote_anomaly",
    model="claude-sonnet-4-6",
    scope="ticker",
    prompt_template=_PROMPT_FOOTNOTE_ANOMALY,
    build_context=_ctx_footnote_anomaly,
)
