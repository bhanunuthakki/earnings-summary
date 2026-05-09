"""
src/memo_generator.py
---------------------
Generate an investor-grade HTML memo per portfolio name. Designed to work
with whatever inputs are available — the only HARD requirement is at least
one cached transcript summary; everything else (thesis JSON, tracker,
IR-doc presence, FMP news) is folded in opportunistically.

Inputs (all optional except summaries):
  - .tmp/<TICKER>_<Q>_<YEAR>_summary.txt        (per-quarter LLM summary)
  - .tmp/SayDo_<TICKER>_<...>.txt               (pairwise Say-Do verdicts)
  - micro_thesis/holdings/<TICKER>.json         (thesis schema, KPI breaks)
  - micro_thesis/thesis-tracker-<TICKER>-*.md   (latest tracker file)
  - micro_thesis/sources/<TICKER>/              (IR PDFs — listed, not parsed)
  - FMP /stable/news/stock                      (last N business days of news)

Output:
  transcripts/memos/<TICKER>_memo_<YYYY-MM-DD>.html

The LLM step (Claude CLI primary, Gemini fallback via llm_client) produces
a markdown body that this module wraps in a minimal styled HTML shell.

Public API:
  generate_memo(ticker: str, *, news_days: int = 14) -> Path
"""

from __future__ import annotations

import datetime as _dt
import glob
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import markdown as md
import requests
from dotenv import load_dotenv

from llm_client import _call_claude as call_llm  # use the merged llm_client's CLI+Gemini path
from llm_client import call_llm_with_web

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TMP_DIR = PROJECT_ROOT / ".tmp"
THESIS_DIR = PROJECT_ROOT / "micro_thesis"
HOLDINGS_DIR = THESIS_DIR / "holdings"
SOURCES_DIR = THESIS_DIR / "sources"
MEMOS_DIR = PROJECT_ROOT / "transcripts" / "memos"

load_dotenv(PROJECT_ROOT / ".env")
FMP_API_KEY = os.environ.get("FMP_API_KEY", "")
FMP_BASE = "https://financialmodelingprep.com/stable"

# How much of each per-quarter summary to ship to the LLM. Cap so the prompt
# stays well within context even with 8 quarters + tracker + news.
SUMMARY_CHARS_PER_QUARTER = 4000
TRACKER_CHARS = 6000
NEWS_HEADLINE_LIMIT = 12

# Lightweight HTML shell. No external CSS — single style block, neutral palette.
HTML_SHELL = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{title}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
            max-width: 880px; margin: 2em auto; padding: 0 1.5em;
            color: #1a1a1a; line-height: 1.55; }}
    h1, h2, h3 {{ line-height: 1.25; }}
    h1 {{ border-bottom: 2px solid #ccc; padding-bottom: .3em; }}
    h2 {{ margin-top: 2em; border-bottom: 1px solid #e5e5e5; padding-bottom: .2em; }}
    code, pre {{ background: #f5f5f7; padding: .15em .4em; border-radius: 4px;
                 font-family: "SF Mono", Consolas, monospace; font-size: .95em; }}
    pre {{ padding: 1em; overflow-x: auto; }}
    table {{ border-collapse: collapse; margin: 1em 0; width: 100%; }}
    th, td {{ border: 1px solid #ddd; padding: .5em .75em; text-align: left; }}
    th {{ background: #f7f7f8; }}
    blockquote {{ border-left: 4px solid #d0d0d0; margin-left: 0; padding-left: 1em; color: #555; }}
    .meta {{ color: #888; font-size: .9em; margin-bottom: 1.5em; }}
    .badge-intact   {{ display: inline-block; padding: .2em .6em; border-radius: 4px;
                       background: #d1f4d1; color: #1a6f1a; font-weight: 600; }}
    .badge-monitor  {{ display: inline-block; padding: .2em .6em; border-radius: 4px;
                       background: #fff3c2; color: #7a5b00; font-weight: 600; }}
    .badge-broken   {{ display: inline-block; padding: .2em .6em; border-radius: 4px;
                       background: #f9d6d6; color: #8a1c1c; font-weight: 600; }}
  </style>
</head>
<body>
{body}
<hr>
<p class="meta">{footer}</p>
</body>
</html>
"""


@dataclass(frozen=True)
class QuarterSummary:
    quarter: str  # "Q1"
    year: int
    text: str


@dataclass(frozen=True)
class SayDoEntry:
    prev_label: str
    curr_label: str
    text: str


@dataclass(frozen=True)
class NewsItem:
    title: str
    url: str
    site: str
    published_date: str


@dataclass
class MemoInputs:
    ticker: str
    summaries: list[QuarterSummary]                 # chronological, oldest first
    saydo_entries: list[SayDoEntry]                 # chronological pairs
    thesis_schema: dict[str, Any] | None
    thesis_tracker_md: str | None
    thesis_tracker_path: Path | None
    ir_doc_filenames: list[str]
    news: list[NewsItem]


# ---------------------------------------------------------------------------
# Input gathering — graceful degradation; missing pieces become None / [].
# ---------------------------------------------------------------------------


def _quarter_sort_key(name: str) -> tuple[int, int]:
    # name like "NOW_Q3_2025_summary.txt"
    parts = name.split("_")
    # ['NOW','Q3','2025','summary.txt']
    return (int(parts[2]), int(parts[1][1:]))


def _load_summaries(ticker: str) -> list[QuarterSummary]:
    paths = sorted(
        glob.glob(str(TMP_DIR / f"{ticker}_Q*_*_summary.txt")),
        key=lambda p: _quarter_sort_key(Path(p).name),
    )
    out: list[QuarterSummary] = []
    for p in paths:
        name = Path(p).name
        parts = name.split("_")
        out.append(QuarterSummary(
            quarter=parts[1], year=int(parts[2]),
            text=Path(p).read_text(encoding="utf-8", errors="replace"),
        ))
    return out


def _saydo_sort_key(name: str) -> tuple[int, int]:
    # SayDo_NOW_Q3_2024_Q4_2024.txt → sort by current-quarter (year, q)
    parts = name.replace(".txt", "").split("_")
    # ['SayDo','NOW','Q3','2024','Q4','2024']
    return (int(parts[5]), int(parts[4][1:]))


def _load_saydo(ticker: str) -> list[SayDoEntry]:
    paths = sorted(
        glob.glob(str(TMP_DIR / f"SayDo_{ticker}_*.txt")),
        key=lambda p: _saydo_sort_key(Path(p).name),
    )
    out: list[SayDoEntry] = []
    for p in paths:
        parts = Path(p).name.replace(".txt", "").split("_")
        prev = f"{parts[2]} {parts[3]}"
        curr = f"{parts[4]} {parts[5]}"
        out.append(SayDoEntry(
            prev_label=prev, curr_label=curr,
            text=Path(p).read_text(encoding="utf-8", errors="replace"),
        ))
    return out


def _load_thesis_schema(ticker: str) -> dict[str, Any] | None:
    p = HOLDINGS_DIR / f"{ticker}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_latest_tracker(ticker: str) -> tuple[str | None, Path | None]:
    paths = sorted(THESIS_DIR.glob(f"thesis-tracker-{ticker}-*.md"))
    if not paths:
        return (None, None)
    latest = paths[-1]
    return (latest.read_text(encoding="utf-8", errors="replace"), latest)


def _load_ir_doc_listing(ticker: str) -> list[str]:
    folder = SOURCES_DIR / ticker
    if not folder.is_dir():
        return []
    return sorted(p.name for p in folder.iterdir() if p.is_file())


def _fetch_news(ticker: str, days: int) -> list[NewsItem]:
    if not FMP_API_KEY:
        return []
    try:
        r = requests.get(
            f"{FMP_BASE}/news/stock",
            params={"symbols": ticker, "limit": NEWS_HEADLINE_LIMIT, "apikey": FMP_API_KEY},
            timeout=20,
        )
        if r.status_code != 200:
            return []
        payload = r.json()
        if not isinstance(payload, list):
            return []
    except requests.RequestException:
        return []

    cutoff = _dt.datetime.now() - _dt.timedelta(days=days)
    out: list[NewsItem] = []
    for row in payload:
        date_str = (row.get("publishedDate") or "")[:10]
        try:
            d = _dt.datetime.fromisoformat(date_str)
        except ValueError:
            continue
        if d < cutoff:
            continue
        out.append(NewsItem(
            title=str(row.get("title", ""))[:240],
            url=str(row.get("url", "")),
            site=str(row.get("site", row.get("publisher", ""))),
            published_date=date_str,
        ))
    return out


def gather_inputs(ticker: str, news_days: int = 14) -> MemoInputs:
    canonical = ticker.strip().upper()
    summaries = _load_summaries(canonical)
    if not summaries:
        raise FileNotFoundError(
            f"No transcript summaries for {canonical} in {TMP_DIR.relative_to(PROJECT_ROOT)} — "
            f"run src/main.py first."
        )
    saydo = _load_saydo(canonical)
    schema = _load_thesis_schema(canonical)
    tracker_md, tracker_path = _load_latest_tracker(canonical)
    ir_docs = _load_ir_doc_listing(canonical)
    news = _fetch_news(canonical, news_days)
    return MemoInputs(
        ticker=canonical, summaries=summaries, saydo_entries=saydo,
        thesis_schema=schema, thesis_tracker_md=tracker_md,
        thesis_tracker_path=tracker_path, ir_doc_filenames=ir_docs, news=news,
    )


# ---------------------------------------------------------------------------
# Prompt assembly — concise, structurally tagged so the LLM's output is
# predictable.
# ---------------------------------------------------------------------------


def _build_prompt(inputs: MemoInputs) -> str:
    parts: list[str] = []
    parts.append(
        "You are a senior fundamental equity analyst writing a portfolio memo. "
        "The reader is the portfolio manager and already knows the basic business — "
        "skip generic background, focus on what matters this period and what is changing.\n\n"
        f"Holding: {inputs.ticker}\n"
        f"Memo date: {_dt.date.today().isoformat()}\n\n"
    )

    if inputs.thesis_schema is not None:
        parts.append("=== THESIS SCHEMA (from holdings JSON) ===\n")
        parts.append(json.dumps(inputs.thesis_schema, indent=2)[:3000])
        parts.append("\n\n")
    else:
        parts.append(
            "=== THESIS SCHEMA ===\n(none on file — derive a working thesis snapshot from the transcript evidence.)\n\n"
        )

    if inputs.thesis_tracker_md:
        parts.append(f"=== LATEST THESIS TRACKER ({inputs.thesis_tracker_path.name if inputs.thesis_tracker_path else '?'}) ===\n")
        parts.append(inputs.thesis_tracker_md[:TRACKER_CHARS])
        parts.append("\n\n")

    parts.append(f"=== PER-QUARTER SUMMARIES (last {len(inputs.summaries)}, oldest first) ===\n")
    for q in inputs.summaries:
        parts.append(f"\n--- {q.quarter} {q.year} ---\n")
        parts.append(q.text[:SUMMARY_CHARS_PER_QUARTER])
    parts.append("\n\n")

    if inputs.saydo_entries:
        parts.append("=== SAY-DO PAIRWISE VERDICTS (chronological) ===\n")
        for s in inputs.saydo_entries[-6:]:  # last 6 pairs, plenty
            parts.append(f"\n--- {s.prev_label} → {s.curr_label} ---\n")
            parts.append(s.text[:1500])
        parts.append("\n\n")

    if inputs.ir_doc_filenames:
        parts.append("=== IR DOCS ON FILE (filenames only, not contents) ===\n")
        parts.extend(f"  - {n}\n" for n in inputs.ir_doc_filenames)
        parts.append("\n")
    else:
        parts.append("=== IR DOCS ===\n(none on disk — base the memo on the transcript + news evidence.)\n\n")

    # FMP news is included only as a fallback hint when Claude's WebSearch
    # tool is unavailable (e.g. router has fallen through to Gemini). With
    # Claude CLI + WebSearch (the default path via call_llm_with_web()),
    # Claude is asked to do its own primary research below.
    if inputs.news:
        parts.append("=== FMP-PROVIDED NEWS HEADLINES (fallback hints, last 14 days) ===\n")
        for n in inputs.news:
            parts.append(f"  - [{n.published_date}] {n.title} ({n.site})\n")
        parts.append("\n")

    parts.append(_WEB_RESEARCH_INSTRUCTIONS.format(ticker=inputs.ticker))
    parts.append(_OUTPUT_SPEC)
    return "".join(parts)


_WEB_RESEARCH_INSTRUCTIONS = """
=== EXTERNAL RESEARCH INSTRUCTIONS ===
You have access to the WebSearch and WebFetch tools. For the "Recent
Developments (External)" section below:

  1. Run 1-2 WebSearch queries to find material news for {ticker} in the
     last 14 days. Suggested queries:
       - "{ticker} news May 2026"
       - "{ticker} guidance OR product OR M&A site:bloomberg.com OR site:reuters.com OR site:cnbc.com"
  2. Cite each item with a markdown link to the source URL. Distinguish
     "material" (affects the thesis) from "noise" (sponsor pieces, generic
     market commentary).
  3. If WebSearch returns nothing material, write "Nothing material in the
     last 14 days." — do NOT pad with the FMP-provided hints unless they
     reflect a genuine catalyst.

"""





_OUTPUT_SPEC = """
=== OUTPUT SPEC — STRICT MARKDOWN ===
Produce a memo in the following exact section order. No preamble, no signoff.
Use specific numbers from the evidence wherever possible. If a fact isn't
supported by the inputs above, omit it rather than guessing.

# {{TICKER}} — Memo {{YYYY-MM-DD}}

## Thesis Status
One line containing one of: 🟢 INTACT / 🟡 MONITORING / 🔴 UNDER PRESSURE,
followed by a one-sentence verdict.

## Latest Quarter Snapshot
3-5 bullets on the most recent reported quarter — the headline metric, the
KPI delta vs prior, the most important guidance change, the sharpest
analyst question (and the answer).

## Quarterly Trajectory
A markdown table covering the last 4-8 quarters. Choose the 3-5 metrics
that matter most for THIS company (not a generic template). Include period
labels in the leftmost column.

## Say-Do Track Record
Short table: prior-period guidance, what happened, MET / MISSED / EXCEEDED
verdict. One row per pair (last 4-6 pairs only).

## Recent Developments (External)
Bulleted list of any news from the headlines above that materially affects
the thesis. If headlines are noise, write "Nothing material."

## Risks & Watchpoints
3-5 bullets — the things that would flip thesis status to 🟡 or 🔴. Be
specific: KPI thresholds, dates, competitor moves.

## Bottom Line
One paragraph. The portfolio manager's takeaway. End with one of:
**Action: HOLD** / **Action: ADD** / **Action: TRIM** / **Action: REVIEW**.
"""


# ---------------------------------------------------------------------------
# Render + write
# ---------------------------------------------------------------------------


def _render_html(ticker: str, memo_md: str) -> str:
    body_html = md.markdown(memo_md, extensions=["tables", "fenced_code"])
    title = f"{ticker} Memo — {_dt.date.today().isoformat()}"
    footer = (
        f"Generated by execution/generate_memo.py on {_dt.datetime.now().isoformat(timespec='seconds')}. "
        f"Inputs: per-quarter summaries (Claude CLI), Say-Do verdicts, optional thesis tracker, "
        f"FMP news (14d window)."
    )
    return HTML_SHELL.format(title=title, body=body_html, footer=footer)


def generate_memo(ticker: str, news_days: int = 14) -> Path:
    inputs = gather_inputs(ticker, news_days=news_days)
    prompt = _build_prompt(inputs)
    # Web-search-enabled call by default — Claude does its own news lookup.
    # Falls through to plain call_llm() (no web) only if Claude CLI is unavailable.
    memo_md = call_llm_with_web(prompt)
    html = _render_html(inputs.ticker, memo_md)
    MEMOS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = MEMOS_DIR / f"{inputs.ticker}_memo_{_dt.date.today().isoformat()}.html"
    out_path.write_text(html, encoding="utf-8")
    return out_path
