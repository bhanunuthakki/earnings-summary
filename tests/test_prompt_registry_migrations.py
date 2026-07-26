"""Byte-identity gates for P0 prompt-registry migrations
(directives/llm_quality_program_2026_07.md — "registry-rendered output
byte-identical to the current inline f-string for each migrated purpose").

Each test rebuilds the LEGACY prompt independently, from the exact literal
text the pre-migration code used, and asserts the registered template renders
the same bytes. That independence is the point: comparing the template to
itself would prove nothing. When a prompt is INTENTIONALLY changed later, the
legacy literal here is updated in the same commit — which is exactly the
review signal we want (a prompt edit becomes a visible diff in a test whose
whole job is to notice).

No LLM calls: pure string assembly.
"""

from __future__ import annotations

import pytest

from llm.prompt_registry import RenderedPrompt
from llm.style import NUMBER_FORMATTING_BLOCK
from llm.untrusted import WEB_CONTENT_NOTICE

# --------------------------------------------------------------------------
# news_structuring — the platform's #1 spender
# --------------------------------------------------------------------------


def _legacy_news_structuring(
    ticker: str, anchor_clause: str, news_days: int, max_web_results: int
) -> str:
    """Verbatim reconstruction of the pre-migration concatenation."""
    return (
        f"You are sourcing recent news for a long-term investor in {ticker} and "
        f"returning it as STRUCTURED DATA (not prose).\n\n"
        f"{anchor_clause}"
        f"Search the web for {ticker} news from the last {news_days} days. "
        f"Prioritize Bloomberg, Reuters, CNBC, FT, WSJ, and company press "
        f"releases. Skip blog spam, opinion pieces with no new information, and "
        f"pure stock-price chatter.\n\n"
        f"WEB BUDGET (HARD CAPS): issue AT MOST 2 web_search queries; open AT "
        f"MOST {max_web_results} URLs via web_fetch.\n\n"
        f"{WEB_CONTENT_NOTICE}\n\n"
        "Return ONLY a JSON array, one object per distinct story, EXACTLY:\n"
        '[{"headline": "<title>", "url": "<canonical article url>", '
        '"published_at": "YYYY-MM-DD HH:MM:SS", "published_tz": "UTC", '
        '"snippet": "<one-sentence gloss>", "source": "<outlet, e.g. Reuters>"}]\n\n'
        "HARD RULES:\n"
        "- published_at: the publication timestamp where you can determine it, "
        "formatted 'YYYY-MM-DD HH:MM:SS' (24-hour, a space not 'T', no zone "
        "suffix). Give it in UTC and set published_tz to 'UTC'. If you only know "
        "the US/Eastern wall-clock time, return that and set published_tz to 'ET'.\n"
        "- If you CANNOT determine a publication date for a story from its "
        "source, OMIT that story entirely. Never guess or fabricate a date.\n"
        "- url must be the real article URL (it is the dedup key). Omit any item "
        "without one.\n"
        "- Output the JSON array and nothing else: no markdown fences, no prose."
    )


@pytest.mark.parametrize(
    ("ticker", "anchor", "days", "maxr"),
    [
        ("NU", "", 7, 5),
        ("MELI", "=== THESIS ANCHOR ===\nbraces {like} this\n\n", 14, 3),
        # A ticker/anchor carrying format-significant characters — the case a
        # naive .format migration would corrupt.
        ("BRK.B", "{not_a_slot} 100% {{doubled}}\n\n", 1, 1),
    ],
)
def test_news_structuring_byte_identity(ticker: str, anchor: str, days: int, maxr: int) -> None:
    from llm_client import NEWS_STRUCTURING_TEMPLATE

    rendered = NEWS_STRUCTURING_TEMPLATE.render(
        ticker=ticker,
        anchor_clause=anchor,
        news_days=days,
        max_web_results=maxr,
        WEB_CONTENT_NOTICE=WEB_CONTENT_NOTICE,
    )
    assert rendered == _legacy_news_structuring(ticker, anchor, days, maxr)
    assert isinstance(rendered, RenderedPrompt)
    assert rendered.template_id == "news_structuring.items"


# --------------------------------------------------------------------------
# recent_developments
# --------------------------------------------------------------------------


def _legacy_recent_developments(
    ticker: str, anchor_block: str, news_days: int, max_web_results: int, max_excerpt_chars: int
) -> str:
    return f"""You are a senior equity analyst preparing a recent-developments
brief for {ticker} for an analyst-grade research memo. Bar: every item
must move the thesis or be tracking a specific known catalyst — pure news
recap earns an automatic rewrite.

{anchor_block}Search the web for {ticker} news from the last {news_days} days. Prioritize
Bloomberg, Reuters, CNBC, FT, WSJ, and company press releases. Skip blog
spam, opinion pieces with no new information, recapitulation of older news,
and analyst initiation reports unless they include a non-obvious data point.

WEB BUDGET (HARD CAPS — do not exceed):
- Issue AT MOST 2 web_search queries total.
- Open AT MOST {max_web_results} URLs via web_fetch across the entire call.
- For each fetched article, quote AT MOST {max_excerpt_chars} characters
  inline. Paraphrase the rest. Long verbatim quotes do not improve the
  memo and burn input tokens with no marginal value.
- If the search returns more candidates than {max_web_results}, pick the
  highest-thesis-impact subset (see ranking rules below) and discard the
  rest before fetching. Don't fetch URLs you won't cite.

{WEB_CONTENT_NOTICE}

RANKING + filtering rules:
- Order each section by THESIS IMPACT (highest first), NOT chronologically.
  When a THESIS ANCHOR is provided above, "thesis impact" means: which
  named tier-1 KPI does this item move, and in what direction relative to
  its break condition? Items that touch a tier-1 KPI rank above items
  that touch a tier-2 KPI; items that touch nothing in the anchor rank
  last (or are dropped).
- For each item: the gloss must explain the IMPLICATION for the investor,
  not just restate the headline. "X happened" is wrong; "X happened, which
  shortens the runway for Y by ~Z months" is right. When the anchor names
  a relevant KPI or failure mode, cite it explicitly in the implication
  clause (e.g., "tightens the GCP-margin trajectory KPI", "partially
  confirms the AI-Mode query-dilution failure mode").
- Skip items that are purely stock-price commentary, sell-side rating
  changes without a new data point, or pure recapitulation of prior news.
- Skip ANY item that's older than {news_days} days. Don't pad.

{NUMBER_FORMATTING_BLOCK}

**Output Format (Strict Markdown):**

### Material news
- **[Headline]** — [1-2 sentences: what happened AND specific implication for the thesis / valuation / KPI trajectory. Quantify the implication where the news supports it.] [Source: outlet, YYYY-MM-DD, URL]
- ... (3-7 items, ranked by thesis impact)

### Sector / regulatory context
- [optional 1-3 items: peer earnings prints, FDA / antitrust decisions affecting peers, sector ETF flows, macro shifts that hit this ticker's specific exposures. Each item must have a "why this matters for {ticker}" clause.]

### Watch this week
- [1-3 items: upcoming earnings calls (this ticker or named peers), scheduled disclosures, investor days, regulatory dockets within the next ~7 days. Format: `**Date · Event** — what to watch for`]

If no material news found in the window, write `*No material news in the last
{news_days} days.*` under "Material news" and skip the other two sections.
Do not pad with stale or low-signal items just to fill the section.
"""


@pytest.mark.parametrize(
    ("ticker", "anchor", "days", "maxr", "maxc"),
    [
        ("NU", "", 7, 7, 500),
        ("GOOGL", "=== THESIS ANCHOR ===\nKPI {tier1}\n\n", 30, 4, 250),
    ],
)
def test_recent_developments_byte_identity(
    ticker: str, anchor: str, days: int, maxr: int, maxc: int
) -> None:
    from llm_client import RECENT_DEVELOPMENTS_TEMPLATE

    rendered = RECENT_DEVELOPMENTS_TEMPLATE.render(
        ticker=ticker,
        anchor_block=anchor,
        news_days=days,
        max_web_results=maxr,
        max_excerpt_chars=maxc,
        WEB_CONTENT_NOTICE=WEB_CONTENT_NOTICE,
        NUMBER_FORMATTING_BLOCK=NUMBER_FORMATTING_BLOCK,
    )
    assert rendered == _legacy_recent_developments(ticker, anchor, days, maxr, maxc)
    assert rendered.template_id == "recent_developments.brief"


# --------------------------------------------------------------------------
# Coverage accounting — the directive's kill criterion needs a live number
# --------------------------------------------------------------------------


def test_migrated_templates_are_registered() -> None:
    import llm_client  # noqa: F401  (registers on import)
    from dcf.scenario_prior import SCENARIO_PRIOR_TEMPLATE  # noqa: F401
    from llm.prompt_registry import REGISTRY

    for tid in (
        "news_structuring.items",
        "recent_developments.brief",
        "scenario_prior.weights",
    ):
        assert tid in REGISTRY, f"{tid} not registered"
        # Every registered template must render its declared variables — a
        # template that cannot render is worse than an unmigrated one.
        t = REGISTRY[tid]
        assert t.variables
        assert t.render(**{v: "x" for v in t.variables})


# --------------------------------------------------------------------------
# valuation_basis — migrated with pre-computed expression slots
# --------------------------------------------------------------------------


def _legacy_valuation_basis(
    ticker: str,
    sector: str | None,
    industry: str | None,
    thesis_text: str,
    financial_profile_md: str,
    available_estimates_md: str,
    choices_block: str,
) -> str:
    return f"""You are a senior buy-side analyst picking THE SINGLE multiple
that best frames {ticker} for an investment memo. Not 2-3 multiples. ONE.
The reader will see this number prominently on the report's Valuation tab;
your pick must answer the question "is this stock rich or cheap?" in the
lens that's most diagnostic for THIS specific business.

TICKER: {ticker}
SECTOR / INDUSTRY: {sector or "?"} / {industry or "?"}

THESIS (the analyst's investment case):
\"\"\"
{thesis_text.strip()[:2000] or "(no thesis on file)"}
\"\"\"

FINANCIAL PROFILE (recent quarterly + TTM shape):
\"\"\"
{financial_profile_md.strip()[:2500]}
\"\"\"

AVAILABLE ANALYST ESTIMATES (use to know which NTM multiples are computable):
\"\"\"
{available_estimates_md.strip()[:1200]}
\"\"\"

PICK FROM EXACTLY THESE OPTIONS (verbatim string match):
{choices_block}

Selection guidance:
- Banks / fintech-with-balance-sheet (NU, MELI's credit book, SOFI): P/B or P/TBV is the canonical lens. P/E only if earnings power is the bet.
- SaaS / high-growth software with negative or thin GAAP earnings: EV/NTM Revenue, EV/LTM Revenue as fallback.
- Profitable platforms / GARP (GOOG, META, NOW at scale): EV/NTM EBITDA or P/E (NTM) when consensus EBITDA / EPS is available.
- Capital-intensive / industrial / commodity (BHP, FCX, CGEH): EV/LTM EBITDA — through-cycle.
- FCF-thesis names (mature compounders, royalty/lease businesses): P/FCF or EV/FCF.
- If the thesis is explicitly about FCF inflection or capex moderation, prefer the FCF multiples regardless of sector default.
- Only pick NTM multiples when the AVAILABLE ANALYST ESTIMATES block lists the relevant NTM line.
- Note: choosing P/E (NTM) additionally surfaces a PEG ratio (forward P/E divided by forward EPS growth) on the Valuation tab — the diagnostic lens when the thesis is about earnings compounding and "cheap or rich vs growth" is the question. PEG is auto-omitted for every other multiple and for unprofitable / negative-growth names, so weigh it as a point in P/E (NTM)'s favor only when forward EPS growth is genuinely the bet.

{NUMBER_FORMATTING_BLOCK}

Return ONLY a JSON object (no markdown fence, no prose):

{{
  "multiple": "<one of the options above, exact string>",
  "rationale": "1-2 sentence why THIS multiple is the diagnostic lens for THIS ticker's thesis. Reference the specific thesis pillar or business-model mechanic that makes it the right pick. Generic 'standard SaaS lens' earns a rewrite.",
  "target_band": "Optional 1-sentence qualitative read of where the multiple SHOULD trade (e.g. 'historical 10-15x range; deserves the upper half if margin expansion sustains', or 'currently in a re-rating window — base-case 4-6x P/TBV'). Pass empty string if no view.",
  "notes": "Optional 1-line caveat, e.g. 'NTM not available, fell back to LTM' or 'historical P/B distorted by 2022 IPO multiple compression'. Empty string if none."
}}
"""


@pytest.mark.parametrize(
    ("sector", "industry", "thesis"),
    [
        ("Financials", "Banks", "FCF inflection thesis"),
        (None, None, ""),  # the or-"?" and or-"(no thesis on file)" branches
    ],
)
def test_valuation_basis_byte_identity(
    sector: str | None, industry: str | None, thesis: str
) -> None:
    from llm_client import VALUATION_BASIS_TEMPLATE, VALUATION_MULTIPLE_CHOICES

    choices_block = "\n".join(f"- {m}" for m in VALUATION_MULTIPLE_CHOICES)
    fin, est = "  rev 1.2B  ", "  EPS NTM 3.10  "
    rendered = VALUATION_BASIS_TEMPLATE.render(
        ticker="NU",
        sector_label=sector or "?",
        industry_label=industry or "?",
        thesis_block=thesis.strip()[:2000] or "(no thesis on file)",
        financial_profile_block=fin.strip()[:2500],
        available_estimates_block=est.strip()[:1200],
        choices_block=choices_block,
        NUMBER_FORMATTING_BLOCK=NUMBER_FORMATTING_BLOCK,
    )
    assert rendered == _legacy_valuation_basis(
        "NU", sector, industry, thesis, fin, est, choices_block
    )
    assert rendered.template_id == "valuation_basis.pick"


def test_valuation_basis_matches_real_production_captures() -> None:
    """The gate a source-text migration can silently fail: Python ESCAPES.

    The body was lifted from source, where the author wrote \\" to embed triple
    quotes inside an f-string. Taken literally, the template stopped matching
    what production actually sent — invisible to a self-consistent byte test,
    caught immediately by replaying real captures. Skips when the corpus is
    absent (CI has no capture dir)."""
    import pathlib

    import llm_client  # noqa: F401  (registers)
    from evals.sampler import load_frame
    from llm.prompt_registry import REGISTRY

    cap = pathlib.Path(__file__).resolve().parents[1] / "data" / "llm_capture"
    if not cap.exists():
        pytest.skip("no capture corpus available")
    frame = load_frame(sorted(cap.glob("capture_*.jsonl")), "valuation_basis")
    if not frame:
        pytest.skip("no valuation_basis captures")
    template = REGISTRY["valuation_basis.pick"]
    head = template.body.split("{ticker}")[0]
    for rec in frame.values():
        assert rec.prompt.startswith(head)
        # every long literal segment of the template must appear verbatim
        import re as _re

        for seg in _re.split(r"\{[a-zA-Z_]+\}", template.body):
            if len(seg.strip()) > 40:
                assert seg in rec.prompt, f"template drifted from production: {seg[:60]!r}"
