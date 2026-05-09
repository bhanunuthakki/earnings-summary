"""Context-aware chat backend for the dashboard.

MVP shape: detect the ticker the user is asking about (or trust an explicit
selection), pull a fixed set of structured rows from the DB and the latest
quarter's LLM summary off disk, hand it all to Claude via llm_client. Returns
the answer plus the list of sources that fed the prompt so the UI can show
"answered using X, Y, Z."

This is deliberately not RAG. The context budget is small and the data is
already structured per ticker — keyword matching and SQL beat embeddings here.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from llm_client import _call_claude

# Per-call ceilings — keeps prompt budget bounded regardless of data size.
_MAX_SUMMARY_CHARS = 6000
_MAX_TICKERS_IN_CONTEXT = 3  # if user mentions multiple, cap to keep prompt readable


@dataclass
class ChatContext:
    """Everything the LLM sees about a single ticker."""

    ticker: str
    name: str | None = None
    list_type: str | None = None
    thesis: str | None = None
    key_driver: str | None = None
    overall_breach_status: str | None = None
    last_evaluated_at: str | None = None
    break_rules: list[dict[str, object]] = field(default_factory=list)
    quarterly_financials: list[dict[str, object]] = field(default_factory=list)
    latest_summary: str | None = None
    latest_summary_quarter: str | None = None
    sources: list[str] = field(default_factory=list)


def detect_ticker(query: str, conn: sqlite3.Connection) -> str | None:
    """Word-boundary match against tracked_companies tickers + names.

    Word-boundary so `NU` in "Nubank" doesn't fire on "nuance" and `BN` doesn't
    fire on every word containing those letters.
    """
    q_upper = " " + query.upper() + " "
    cursor = conn.cursor()
    rows = cursor.execute(
        "SELECT ticker, name FROM tracked_companies WHERE list_type IN ('portfolio','watchlist')"
    ).fetchall()
    for r in rows:
        ticker = str(r["ticker"])
        name = str(r["name"] or "")
        if f" {ticker} " in q_upper:
            return ticker
        if name and name.upper() in q_upper:
            return ticker
    return None


def build_context(
    ticker: str,
    conn: sqlite3.Connection,
    repo_root: Path,
) -> ChatContext:
    """Gather the structured context for one ticker."""
    ctx = ChatContext(ticker=ticker.upper())
    cursor = conn.cursor()

    row = cursor.execute(
        "SELECT name, list_type FROM tracked_companies WHERE ticker = ?", (ctx.ticker,)
    ).fetchone()
    if row:
        ctx.name = row["name"]
        ctx.list_type = row["list_type"]
        ctx.sources.append("DB:tracked_companies")

    holdings = _read_holdings(repo_root, ctx.ticker)
    if holdings is not None:
        ctx.thesis = holdings.get("thesis")
        ctx.key_driver = holdings.get("key_driver")
        ctx.sources.append(f"file:micro_thesis/holdings/{ctx.ticker}.json")

    eval_row = cursor.execute(
        """
        SELECT evaluated_at, overall_status, rule_evaluations_json
        FROM thesis_evaluations
        WHERE ticker = ?
        ORDER BY evaluated_at DESC LIMIT 1
        """,
        (ctx.ticker,),
    ).fetchone()
    if eval_row:
        ctx.overall_breach_status = str(eval_row["overall_status"])
        ctx.last_evaluated_at = str(eval_row["evaluated_at"])[:19]
        payload = json.loads(eval_row["rule_evaluations_json"])
        ctx.break_rules = [
            {
                "rule": str(p.get("kpi_name", "")),
                "status": str(p.get("status", "")),
                "detail": str(p.get("detail", "")),
            }
            for p in payload
        ]
        ctx.sources.append("DB:thesis_evaluations")

    fin_rows = cursor.execute(
        """
        SELECT period_end, revenue, operating_income, net_income,
               operating_cash_flow, free_cash_flow
        FROM metrics
        WHERE ticker = ? AND fiscal_period_type LIKE 'Q%'
        ORDER BY period_end DESC LIMIT 4
        """,
        (ctx.ticker,),
    ).fetchall()
    if fin_rows:
        ctx.quarterly_financials = [
            {
                "period_end": str(r["period_end"])[:10],
                "revenue_usd_m": _to_millions(r["revenue"]),
                "operating_income_usd_m": _to_millions(r["operating_income"]),
                "net_income_usd_m": _to_millions(r["net_income"]),
                "ocf_usd_m": _to_millions(r["operating_cash_flow"]),
                "fcf_usd_m": _to_millions(r["free_cash_flow"]),
            }
            for r in fin_rows
        ]
        ctx.sources.append("DB:metrics")

    summary_path = _latest_summary_path(repo_root, ctx.ticker)
    if summary_path is not None:
        with open(summary_path, encoding="utf-8") as f:
            text = f.read()
        ctx.latest_summary = text[:_MAX_SUMMARY_CHARS]
        # Filename: GOOG_Q1_2026_summary.txt → quarter label "Q1 2026"
        parts = summary_path.stem.split("_")
        if len(parts) >= 3:
            ctx.latest_summary_quarter = f"{parts[1]} {parts[2]}"
        ctx.sources.append(f"file:.tmp/{summary_path.name}")

    return ctx


def answer(query: str, contexts: list[ChatContext]) -> dict[str, object]:
    """Call Claude with the assembled context and return {answer, ticker, sources, elapsed_ms}."""
    if not contexts:
        return {
            "answer": (
                "I couldn't tell which ticker you're asking about. "
                "Mention a ticker (e.g. GOOG, NU, MELI) or company name in your question, "
                "or pick one from the sidebar."
            ),
            "tickers": [],
            "sources": [],
            "elapsed_ms": 0,
        }

    prompt = _format_prompt(query, contexts)
    t0 = time.perf_counter()
    response = _call_claude(prompt)
    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    sources_combined: list[str] = []
    for ctx in contexts:
        for s in ctx.sources:
            tagged = f"{ctx.ticker}: {s}"
            if tagged not in sources_combined:
                sources_combined.append(tagged)

    return {
        "answer": response,
        "tickers": [c.ticker for c in contexts],
        "sources": sources_combined,
        "elapsed_ms": elapsed_ms,
    }


def _format_prompt(query: str, contexts: list[ChatContext]) -> str:
    """Pack the contexts into a Claude prompt grounded in the supplied data."""
    blocks: list[str] = []
    for ctx in contexts:
        sect = [f"## Context: {ctx.ticker} ({ctx.name or '—'}; {ctx.list_type or 'untracked'})"]
        if ctx.thesis:
            sect.append(f"\n**Investment thesis:** {ctx.thesis}")
        if ctx.key_driver:
            sect.append(f"\n**Key driver:** {ctx.key_driver}")
        if ctx.overall_breach_status:
            sect.append(
                f"\n**Universal break-rule status (evaluated {ctx.last_evaluated_at}):** "
                f"{ctx.overall_breach_status}"
            )
            for br in ctx.break_rules:
                sect.append(f"  - [{br['status']}] {br['rule']} — {br['detail']}")
        if ctx.quarterly_financials:
            sect.append("\n**Last 4 quarters (USD millions, Q→ newest at top):**")
            sect.append("| Period | Revenue | Op Inc | Net Inc | OCF | FCF |")
            sect.append("|---|---|---|---|---|---|")
            for q in ctx.quarterly_financials:
                sect.append(
                    f"| {q['period_end']} | {_fmt_cell(q['revenue_usd_m'])} | "
                    f"{_fmt_cell(q['operating_income_usd_m'])} | {_fmt_cell(q['net_income_usd_m'])} | "
                    f"{_fmt_cell(q['ocf_usd_m'])} | {_fmt_cell(q['fcf_usd_m'])} |"
                )
        if ctx.latest_summary:
            sect.append(
                f"\n**Latest LLM summary ({ctx.latest_summary_quarter or 'most recent'}):**\n{ctx.latest_summary}"
            )
        blocks.append("\n".join(sect))

    context_block = "\n\n---\n\n".join(blocks)

    return f"""You are an investment-research assistant embedded in the user's portfolio tracking app.

Answer the user's question using ONLY the context below. If the context doesn't have what's needed, say so — point to the specific ingestion step that would fill the gap (e.g., "no kpi_facts row for ARPAC; run python execution/extract_kpis_from_ir.py --ticker NU"). Don't invent figures.

Be concise: 3–8 sentences typical, with a short bulleted list when comparing multiple things. Cite specific numbers from the context. If a number you'd want isn't present, say "not in the data I have."

# Context

{context_block}

# User question

{query}

# Answer
"""


def _read_holdings(repo_root: Path, ticker: str) -> dict[str, object] | None:
    path = repo_root / "micro_thesis" / "holdings" / f"{ticker}.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _latest_summary_path(repo_root: Path, ticker: str) -> Path | None:
    """Pick the chronologically latest summary, not the alphabetically latest.

    Filename: `{TICKER}_Q{n}_{YEAR}_summary.txt` — sort by (year, quarter)
    tuple so Q1 2026 beats Q4 2025.
    """
    tmp_dir = repo_root / ".tmp"
    if not tmp_dir.exists():
        return None
    candidates: list[tuple[tuple[int, int], Path]] = []
    for p in tmp_dir.glob(f"{ticker}_Q*_summary.txt"):
        parts = p.stem.split("_")
        if len(parts) < 4:
            continue
        try:
            quarter = int(parts[1].lstrip("Q"))
            year = int(parts[2])
        except ValueError:
            continue
        candidates.append(((year, quarter), p))
    if not candidates:
        return None
    candidates.sort()
    return candidates[-1][1]


def _to_millions(v: object) -> float | None:
    if v is None:
        return None
    try:
        return round(float(v) / 1_000_000, 1)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _fmt_cell(v: float | None) -> str:
    return "—" if v is None else f"{v:,.0f}"
