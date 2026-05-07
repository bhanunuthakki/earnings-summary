"""
scratch/probe_aggregators.py
----------------------------
One-off probe: which earnings-transcript aggregators are reachable
without auth, and which actually carry Q&A content?

Method: hit one known-good URL per aggregator with a real-browser UA,
record HTTP status, content length, and whether the body contains a
templated Q&A boundary marker. Print a table.

Run:  python scratch/probe_aggregators.py
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
TIMEOUT = (10, 25)

# Markers Q&A sections almost always carry. Operator scripts are templated.
QA_MARKERS = (
    "question-and-answer",
    "question and answer",
    "q&a session",
    "q-and-a",
    "we'll now open the line for questions",
    "we'll now begin the question",
    "first question comes from",
    "first question from",
)

# Paywall / login-wall fingerprints (none implies the body is real content).
PAYWALL_MARKERS = (
    "subscribe to read",
    "create a free account",
    "premium content",
    "this article is reserved",
    "for subscribers only",
    "log in to read",
    "sign in to continue",
)


@dataclass
class Probe:
    name: str
    url: str
    note: str = ""


PROBES: list[Probe] = [
    # Already proven during the RBRK Q4 FY25 synthesis — sanity baselines:
    Probe("tickertrends",   "https://tickertrends.io/transcripts/RBRK/Q4-earnings-transcript-2025"),
    Probe("marketscreener", "https://www.marketscreener.com/quote/stock/RUBRIK-INC-168916373/news/Transcript-Rubrik-Inc-Q4-2025-Earnings-Call-Mar-13-2025-49331248/"),
    Probe("stocktitan",     "https://www.stocktitan.net/news/RBRK/rubrik-to-report-fourth-quarter-and-fiscal-year-2025-financial-ofknwk16gj3p.html"),

    # Free-tier sources we've seen referenced but haven't load-tested:
    Probe("fool",           "https://www.fool.com/earnings/call-transcripts/2024/04/24/servicenow-now-q1-2024-earnings-call-transcript/"),
    Probe("fool_nvo_q4_24", "https://www.fool.com/earnings/call-transcripts/2025/02/05/novo-nordisk-nvo-q4-2024-earnings-call-transcript/"),
    Probe("insidermonkey",  "https://www.insidermonkey.com/blog/servicenow-inc-nysenow-q2-2024-earnings-call-transcript-1326087/"),
    Probe("yahoo",          "https://finance.yahoo.com/news/servicenow-inc-now-q3-2024-071122176.html"),

    # Common second-tier transcript hubs (predictable URLs):
    Probe("stockanalysis",  "https://stockanalysis.com/stocks/now/financials/transcripts/q1-2024/"),
    Probe("roic",           "https://www.roic.ai/quote/NOW/transcripts"),
    Probe("wallstreetzen",  "https://www.wallstreetzen.com/stocks/us/nyse/now/earnings-call-transcript"),
    Probe("public",         "https://public.com/stocks/now/earnings"),
    Probe("nasdaq",         "https://www.nasdaq.com/market-activity/stocks/now/earnings"),

    # Paywalled — included to confirm the wall actually fires:
    Probe("seekingalpha",   "https://seekingalpha.com/article/4753179-servicenow-inc-now-q4-2024-earnings-call-transcript",
                            note="(expected paywall)"),
    Probe("gurufocus",      "https://www.gurufocus.com/news/2738692/q4-2025-rubrik-inc-earnings-call-transcript",
                            note="(403'd in earlier probe; retry)"),
]


def probe_one(p: Probe) -> dict:
    headers = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*"}
    try:
        r = requests.get(p.url, headers=headers, timeout=TIMEOUT, allow_redirects=True)
    except Exception as e:
        return {"status": "ERR", "len": 0, "qa_hits": 0, "paywall": False, "err": str(e)[:80]}
    body = r.text
    body_lower = body.lower()
    qa_hits = sum(1 for m in QA_MARKERS if m in body_lower)
    paywall = any(m in body_lower for m in PAYWALL_MARKERS)
    return {
        "status": str(r.status_code),
        "len": len(body),
        "qa_hits": qa_hits,
        "paywall": paywall,
        "err": "",
    }


def main() -> None:
    print(
        f"{'NAME':16s} {'STATUS':6s} {'BODY_LEN':>8s} {'QA':>3s} {'PAY':>4s}  URL"
    )
    print("-" * 130)
    for p in PROBES:
        r = probe_one(p)
        marker = "OK   " if (r["status"] == "200" and r["qa_hits"] > 0 and not r["paywall"]) else \
                 "PAY  " if r["paywall"] else \
                 "NO_QA" if r["status"] == "200" and r["qa_hits"] == 0 else \
                 "BLOCK"
        print(
            f"{p.name:16s} {r['status']:6s} {r['len']:>8} "
            f"{r['qa_hits']:>3} {('Y' if r['paywall'] else 'n'):>4}  "
            f"[{marker}] {p.url[:60]}{'…' if len(p.url) > 60 else ''}"
            + (f"  {p.note}" if p.note else "")
        )
        if r["err"]:
            print(f"{'':16s} ERR  {r['err']}")


if __name__ == "__main__":
    main()
