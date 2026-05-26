"""llm_calibration lens — does the LLM's stated conviction match outcomes?

Audits the calibration of the LLM's analytical voice across recent
decisions: stated conviction vs realized correct%, hit rates by
recommendation kind, prompt-tuning recommendations.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ._shared import (
    Lens,
    LensContext,
    sha8,
)

_PROMPT_LLM_CALIBRATION = """You are auditing the calibration of the LLM's own analytical voice across
the last 180 days of recommendations. The `decisions` table records every
LLM recommendation (kind, stated conviction, realized outcome). Your job:
read the calibration curve and emit a memo on whether the LLM's confidence
language is actually predictive.

**Decisions hit rate by recommendation kind:**
{hit_rate_by_kind}

**Calibration curve (stated conviction → outcome distribution):**
{conviction_curve}

**Recent decisions with outcomes (sample):**
{decision_sample}

Produce a 300-450 word memo with these sections:

## 1. Calibration verdict
Is the LLM's confidence calibrated? "High conviction" should correlate
with higher correct% than "medium" or "low". Compute the realized
correct% for each bucket and state whether the ranking holds. If the LLM
expresses "high conviction" but underperforms its "medium" calls, the
voice is over-confident — name it.

## 2. Recommendation-kind asymmetry
Are ADD calls more often right than TRIM calls, or vice versa? Are HOLDs
systematically getting punished (the stock moved a lot and the user did
nothing)? Identify the WORST-calibrated bucket — that's where the prompt
needs the most discipline.

## 3. What this implies for prompt tuning
ONE paragraph naming 1-2 specific prompt changes that would address the
worst-calibrated pattern. E.g., "the five-min-reread prompt currently
forces a verdict on every artifact; on tickers with no thesis-relevant
data movement, allow an explicit 'no action' response so HOLD isn't
diluted by reflex." Be specific — the goal is an actionable retune, not
generic "be more humble."

Voice: dispassionate auditor. The reader is the prompt-tuner. If the
sample is too thin (graded total < 10), say so plainly — calibration
needs N≥20 before it's meaningful, and 30+ before it's load-bearing.
"""


def _ctx_llm_calibration(ticker: str | None, repo_root: Path) -> LensContext | None:
    # Portfolio-scope lens (ticker is None)
    del ticker
    db = repo_root / "data" / "portfolio.db"
    if not db.exists():
        return None
    conn = sqlite3.connect(str(db))
    try:
        conn.row_factory = sqlite3.Row
        if (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='decisions'"
            ).fetchone()
            is None
        ):
            return None
        # Hit rate by kind, last 180 days
        by_kind = conn.execute(
            """
            SELECT recommendation_kind,
                   COALESCE(outcome_label, 'pending') AS outcome_label,
                   COUNT(*) AS n
            FROM decisions
            WHERE made_at >= date('now', '-180 days')
            GROUP BY recommendation_kind, outcome_label
            """
        ).fetchall()
        by_conv = conn.execute(
            """
            SELECT COALESCE(conviction, 'unstated') AS conv,
                   COALESCE(outcome_label, 'pending') AS outcome_label,
                   COUNT(*) AS n
            FROM decisions
            WHERE made_at >= date('now', '-180 days')
            GROUP BY conv, outcome_label
            """
        ).fetchall()
        sample = conn.execute(
            """
            SELECT ticker, recommendation_kind, recommendation_value, conviction,
                   made_at, outcome_label, outcome_pct, outcome_notes
            FROM decisions
            WHERE made_at >= date('now', '-180 days')
            ORDER BY made_at DESC LIMIT 25
            """
        ).fetchall()
    finally:
        conn.close()
    if not by_kind and not sample:
        return None

    # Pivot the by_kind rows into a scannable table
    kinds: dict[str, dict[str, int]] = {}
    for r in by_kind:
        kinds.setdefault(str(r["recommendation_kind"]), {})[str(r["outcome_label"])] = int(r["n"])
    kind_lines: list[str] = []
    for kind in sorted(kinds.keys()):
        c = kinds[kind]
        graded = c.get("correct", 0) + c.get("wrong", 0) + c.get("mixed", 0)
        hit = (100 * c.get("correct", 0) / graded) if graded > 0 else 0.0
        kind_lines.append(
            f"- **{kind.upper()}** — correct={c.get('correct', 0)} · wrong={c.get('wrong', 0)} · "
            f"mixed={c.get('mixed', 0)} · pending={c.get('pending', 0)} "
            f"(graded={graded}, correct rate={hit:.0f}%)"
        )
    kind_block = "\n".join(kind_lines) if kind_lines else "(no decisions yet)"

    conv_kinds: dict[str, dict[str, int]] = {}
    for r in by_conv:
        conv_kinds.setdefault(str(r["conv"]), {})[str(r["outcome_label"])] = int(r["n"])
    conv_lines: list[str] = []
    for conv in ("high", "medium", "low", "unstated"):
        if conv not in conv_kinds:
            continue
        c = conv_kinds[conv]
        graded = c.get("correct", 0) + c.get("wrong", 0) + c.get("mixed", 0)
        hit = (100 * c.get("correct", 0) / graded) if graded > 0 else 0.0
        conv_lines.append(
            f"- **{conv}** conviction — correct={c.get('correct', 0)} · wrong={c.get('wrong', 0)} · "
            f"mixed={c.get('mixed', 0)} (graded={graded}, correct rate={hit:.0f}%)"
        )
    conv_block = "\n".join(conv_lines) if conv_lines else "(no graded decisions yet)"

    sample_lines: list[str] = []
    for r in sample:
        pct = r["outcome_pct"]
        pct_str = f"{float(pct) * 100:+.1f}%" if pct is not None else "—"
        sample_lines.append(
            f"- {str(r['made_at'])[:10]} · {r['ticker']} · "
            f"**{r['recommendation_kind'].upper()}**"
            + (f" {float(r['recommendation_value'])}%" if r["recommendation_value"] is not None else "")
            + f" · conv={r['conviction'] or '?'} · outcome={r['outcome_label'] or 'pending'} ({pct_str})"
        )
    sample_block = "\n".join(sample_lines) if sample_lines else "(no recent decisions)"

    return LensContext(
        ticker=None,
        template_kwargs={
            "hit_rate_by_kind": kind_block,
            "conviction_curve": conv_block,
            "decision_sample": sample_block,
        },
        cache_inputs=[
            "portfolio",
            sha8(kind_block),
            sha8(conv_block),
            sha8(sample_block),
        ],
        source_doc_ids=[],
        parent_artifact_ids=[],
    )


LENS = Lens(
    name="llm_calibration",
    # Opus is worth it here — calibration auditing is a numeric-reasoning
    # task where Opus's stronger discipline pays off in the analysis. One
    # call per portfolio run, cost is bounded.
    model="claude-opus-4-7",
    scope="portfolio",
    prompt_template=_PROMPT_LLM_CALIBRATION,
    build_context=_ctx_llm_calibration,
)
