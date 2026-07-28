"""mgmt_credibility_score lens — per-ticker mgmt SayDo track record.

Reads predictions.outcome for source_kind='mgmt_commitment' and emits a
calibration-aware track-record analysis.
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

_PROMPT_MGMT_CREDIBILITY = """You are writing a management credibility memo for {ticker}. Read the
ticker's prediction outcome history (drawn from `predictions.outcome` for
source_kind='mgmt_commitment' — i.e., the SayDo ledger) and emit a
calibration-aware track-record analysis.

**Thesis (for framing the commitments analytically):**
{thesis_block}

**Predictions outcomes histogram (mgmt commitments only):**
{outcome_histogram}

**Recent commitments with outcomes (newest first):**
{recent_commitments}
{citation_rule}
Produce a 300-450 word memo with these sections:

## 1. Hit rate — what the ledger says
Lead with the headline ratio: graded {{total_graded}} commitments, {{met}}
met ({{hit_rate_pct}}%), {{missed}} missed, {{mixed}} partially. State
which KPIs management has been MOST reliable on, and which they have
systematically over-promised on. Name 1-2 specific commitments by paraphrased
language + outcome (cite each with its [n] when it is one of the numbered
commitments above).

## 2. The pattern behind the misses
Is there a category that explains most failures? (margin guidance, segment
growth, capex discipline, M&A integration). What does the pattern reveal
about management's incentive structure or analytical blind spots?

## 3. What to discount in the current guide
ONE paragraph. Given the track record, what current management language
should the analyst discount or stress-test? Quote a specific recent commitment
(if one exists in the "recent commitments" block — cite its [n]) and rate it
Low / Medium / High credibility based on the historical pattern.

Voice: senior analyst writing for themselves. Numbers-first, no fluff. If
the ledger is too thin to support a credibility read (graded < 5), say so
plainly and recommend re-running grade_bear_cases / grade_decisions to
densify the record before relying on this lens.
"""


def _ctx_mgmt_credibility(ticker: str | None, repo_root: Path) -> LensContext | None:
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
                "SELECT name FROM sqlite_master WHERE type='table' AND name='predictions'"
            ).fetchone()
            is None
        ):
            return None
        histo_rows = conn.execute(
            """
            SELECT outcome, COUNT(*) AS n FROM predictions
            WHERE ticker = ? AND source_kind = 'mgmt_commitment'
            GROUP BY outcome
            """,
            (ticker,),
        ).fetchall()
        recent_rows = conn.execute(
            """
            SELECT made_at, target_period, prediction_md, outcome,
                   realized_value, target_value, notes, source_doc_id
            FROM predictions
            WHERE ticker = ? AND source_kind = 'mgmt_commitment'
            ORDER BY made_at DESC LIMIT 25
            """,
            (ticker,),
        ).fetchall()
    finally:
        conn.close()
    if not histo_rows and not recent_rows:
        return None
    histo = {str(r["outcome"]): int(r["n"]) for r in histo_rows}
    total_graded = sum(v for k, v in histo.items() if k != "pending")
    met = histo.get("met", 0)
    hit_rate = (100 * met / total_graded) if total_graded > 0 else 0.0
    histo_block = (
        f"met={met} · missed={histo.get('missed', 0)} · mixed={histo.get('mixed', 0)} · "
        f"unfalsifiable={histo.get('unfalsifiable', 0)} · pending={histo.get('pending', 0)} "
        f"(graded total: {total_graded}, hit rate: {hit_rate:.1f}%)"
    )
    # L12 static-prose citations: number ONLY the commitments backed by a source
    # document, in render order, and store those doc ids as ordered
    # source_doc_ids. The memo's ``[n]`` markers (report.sections.synthesis
    # resolves them against this same list) therefore open the filing the
    # commitment came from. A commitment with no source doc renders without a
    # marker, so the LLM can't cite a number that wouldn't resolve.
    recent_block, source_doc_ids = _format_commitments(recent_rows)
    citation_rule = _CITATION_RULE if source_doc_ids else ""
    return LensContext(
        ticker=ticker,
        template_kwargs={
            "ticker": ticker,
            "thesis_block": thesis_block(ticker, repo_root),
            "outcome_histogram": histo_block,
            "recent_commitments": recent_block,
            "citation_rule": citation_rule,
        },
        cache_inputs=[ticker, sha8(histo_block), sha8(recent_block)],
        source_doc_ids=source_doc_ids,
        parent_artifact_ids=[],
    )


# The prompt rule that turns numbered commitments into cited claims. Only
# injected when at least one commitment carries a source document.
_CITATION_RULE = """
**Citing your evidence:** commitments prefixed with a bracketed number
([1], [2], …) are backed by a source filing. Whenever you reference one of those
specific commitments, append its bracketed number inline so the reader can open
that filing — e.g. "guided to 25% margins [1], then missed". Cite only those
numbers; never invent one.
"""


def _format_commitments(
    recent_rows: list[sqlite3.Row],
) -> tuple[str, list[int]]:
    """Render the recent-commitments block and the ordered citeable doc ids.

    Commitments with a ``source_doc_id`` are prefixed ``[n]`` (n ascending in
    render order) and their doc ids collected in the SAME order, so the memo's
    inline markers line up 1:1 with ``source_doc_ids`` at render time.
    """
    if not recent_rows:
        return "(no recent commitments)", []
    lines: list[str] = []
    source_doc_ids: list[int] = []
    for r in recent_rows:
        raw_doc = r["source_doc_id"]
        marker = ""
        if isinstance(raw_doc, int) and not isinstance(raw_doc, bool):
            source_doc_ids.append(raw_doc)
            marker = f"[{len(source_doc_ids)}] "
        lines.append(
            f"- {marker}{str(r['made_at'])[:10]} → {str(r['target_period'] or '?')[:10]} · "
            f"**{(r['outcome'] or 'pending').upper()}** · "
            f"{(r['prediction_md'] or '')[:160]}"
        )
    return "\n".join(lines), source_doc_ids


LENS = Lens(
    name="mgmt_credibility_score",
    model="claude-sonnet-4-6",
    scope="ticker",
    prompt_template=_PROMPT_MGMT_CREDIBILITY,
    build_context=_ctx_mgmt_credibility,
)
