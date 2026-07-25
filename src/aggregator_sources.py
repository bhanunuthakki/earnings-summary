"""
src/aggregator_sources.py
-------------------------
Free, no-auth earnings-transcript aggregator sources, ranked by coverage.

Probed against the 11-name portfolio (NOW, NVO, NU, MELI, META, GOOG, AMZN,
WIX, RBRK, VEEV, BN) on 2026-05-06. Coverage:

  issuer_ir      latest quarter only, but EARLIEST — the company's own results-
                 center PDF is posted before any aggregator indexes the call
                 (config-gated to MZ-platform issuers; see ir_pipeline.transcript)
  roic.ai        10/10  including foreign ADRs (NVO, NU, MELI) and Brookfield (BN)
  stockanalysis  7/10   misses NVO, GOOG, BN
  tickertrends   recent only; aggressive rate-limit on bursty access

Used by `execution/fetch_qa_transcript.py`. Each source is a `AggregatorSource`
with a `fetch_qa(ticker, year, quarter) -> AggregatorHit | None` callable.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Callable

import requests

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HTTP_TIMEOUT = (10, 30)

# Operator-transition phrases that mark Q&A start. These are templated by the
# call-hosting service (Q4 Inc, Notified, etc.) and reproduced verbatim by the
# aggregator. NOT keyword classification — boundary detection on a known
# protocol cue.
QA_BOUNDARY_RE = re.compile(
    r"(?:"
    # "first question {today,now,...0-4 filler words} {comes|coming|come|is|will come} from ..."
    # Catches:
    #   first question comes from              (most US large-caps)
    #   first question is from                 (variant)
    #   first question today comes from        (LLY, SOFI, LMND)
    #   first question today is coming from    (LLY)
    #   first question will come from          (JPM)
    #   first question coming from the line of (WIX)
    r"first\s+question(?:\s+\w+){0,4}\s+(?:comes?|come|coming|is|will\s+come)\s+from|"
    # Operator-cue: open up the call/line/floor for/to questions.
    r"(?:we['’]?ll|let['’]?s|i['’]?ll)\s+(?:now\s+)?(?:open|begin)\s+(?:the\s+|up\s+)*(?:line|floor|call)?\s*(?:up\s+)?(?:for|to)\s+question|"
    r"open\s+(?:up\s+)?(?:the\s+)?(?:line|floor|call)?\s*(?:up\s+)?(?:to|for)\s+questions|"
    # "begin the Q&A" / "start the question-and-answer session"
    r"(?:begin|start|let['’]?s\s+(?:now\s+)?begin)\s+(?:our\s+|the\s+)?(?:question[-\s]and[-\s]answer|q\s*&\s*a)(?:\s+session)?|"
    # Hand-off cue: "I'll turn it over to the operator"
    r"i['’]?ll\s+(?:now\s+)?turn\s+(?:it|the\s+call)\s+over\s+to\s+the\s+operator"
    r")",
    re.IGNORECASE,
)

PAYWALL_MARKERS = (
    "this article is reserved",
    "subscribe to read",
    "for subscribers only",
    "create a free account",
    "log in to read",
    "sign in to continue",
    "premium content",
)

# Templated end-of-call cues. Used to trim aggregator-page footers/nav from the
# Q&A capture. Like the boundary regex above, these are operator-script artefacts,
# not content classification.
QA_TAIL_RE = re.compile(
    r"(?:"
    r"that\s+concludes\s+(?:today['’]?s\s+)?(?:conference\s+)?call|"
    r"this\s+concludes\s+(?:today['’]?s\s+)?(?:conference\s+)?call|"
    r"you\s+may\s+now\s+disconnect|"
    r"thank\s+you\s+for\s+(?:your\s+)?participation\s*\.?\s*you\s+may"
    r")",
    re.IGNORECASE,
)

# Some IR officers (not the operator) run the analyst queue themselves —
# "Operator, could you please open the line for Mr. Jorge Kuri from Morgan
# Stanley?" (NU's convention). This is the same kind of templated protocol cue
# as QA_BOUNDARY_RE above (call-hosting-service script, not content
# classification); it is checked per-turn against `_TranscriptMessageParser`
# output, never against the flattened whole-page text, so it cannot cross a
# real speaker-turn boundary.
_QA_HANDOFF_RE = re.compile(
    r"open\s+(?:the\s+|up\s+the\s+)?line\s+for\s+(?:mr\.?|ms\.?|mrs\.?|dr\.?)\s*",
    re.IGNORECASE,
)


def _split_into_speaker_paragraphs(qa_text: str) -> str:
    """Insert blank lines between speaker turns so the file is readable.

    Aggregators tend to flatten everything to one long line. We re-paragraph
    on the prefix shape `<single letter><space><Capitalised Name>` (e.g.
    `B Bipul Sinha`, `O Operator`, `M Matthew Martino`) which roic.ai uses,
    plus the standalone `Operator` cue.
    """
    # Insert newline before "<Letter> <Capitalised>...":
    paragraphed = re.sub(r"\s+(?=[A-Z]\s[A-Z][a-zA-Z]+\s)", "\n\n", qa_text)
    # Also break before standalone "Operator " occurrences (when no letter prefix).
    paragraphed = re.sub(r"\s+(?=Operator\s+(?:Your|Thank|Ladies|We|Next))", "\n\n", paragraphed)
    return paragraphed.strip()


@dataclass(frozen=True)
class AggregatorHit:
    source_name: str
    page_url: str
    qa_text: str
    full_text_chars: int  # length of stripped page text, for diagnostics


@dataclass(frozen=True)
class AggregatorSource:
    name: str
    fetch_qa: Callable[[str, int, int], AggregatorHit | None]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _VisibleTextStripper(HTMLParser):
    """HTMLParser that yields the visible text of a page (drops script/style/svg)."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style", "noscript", "svg"):
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript", "svg") and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def _strip_html(html: str) -> str:
    s = _VisibleTextStripper()
    s.feed(html)
    return " ".join(p.strip() for p in s.parts if p.strip())


def _has_paywall(html: str) -> bool:
    body_lower = html.lower()
    return any(m in body_lower for m in PAYWALL_MARKERS)


def _extract_qa(visible_text: str) -> str | None:
    """Return the substring of `visible_text` from the first Q&A boundary
    cue onwards (with page footer trimmed and speaker turns paragraphed),
    or None if no boundary detected."""
    head = QA_BOUNDARY_RE.search(visible_text)
    if not head:
        return None
    qa = visible_text[head.start() :]
    tail = QA_TAIL_RE.search(qa)
    if tail:
        # Keep the cue itself; drop everything after it (footer/nav).
        qa = qa[: tail.end()]
    qa = _split_into_speaker_paragraphs(qa)
    # Sanity floor: a real Q&A is at least a few exchanges (~500 chars).
    return qa if len(qa) >= 500 else None


def _http_get(url: str) -> requests.Response | None:
    try:
        return requests.get(url, headers={"User-Agent": UA}, timeout=HTTP_TIMEOUT)
    except requests.RequestException:
        return None


# ---------------------------------------------------------------------------
# Source: roic.ai — DOM-structured per-turn extraction
# ---------------------------------------------------------------------------
#
# roic.ai renders each call message as its own block:
#   <div data-cy="transcripts_call_message">
#     <div data-transcript-avatar="true"><span>B</span></div>   <!-- initial only -->
#     <p data-transcript-speaker-name="true">Bipul Sinha</p>
#     <span>...body paragraphs...</span>
#   </div>
#
# The page's *flattened visible text* (what `_strip_html` produces) runs the
# avatar initial straight into the speaker name with no delimiter — "B Bipul
# Sinha" — which is what the old `_split_into_speaker_paragraphs` heuristic
# below was reverse-engineering, and why it silently produced ZERO turn
# boundaries whenever a call didn't spell a name in that exact shape (verified
# 2026-07-25: NU_Q1_2026 collapsed to one 55k-char "Operator" turn). The
# `data-transcript-speaker-name="true"` attribute is an unambiguous, real
# structural signal for who is speaking at each turn — parsing the DOM instead
# of the flattened text recovers genuine per-turn speaker attribution instead
# of guessing it back from a rendering artifact.


class _TranscriptMessageParser(HTMLParser):
    """Extract (speaker_name, body_text) per `data-cy="transcripts_call_message"` block.

    Tracks two independent nested regions inside each message block: the
    speaker-name `<p data-transcript-speaker-name="true">` (isolated so its
    text never leaks into the body) and the avatar `<div
    data-transcript-avatar="true">` (whose single-letter initial must be
    dropped, not treated as body text). Everything else inside the message
    block is body text, joined with single spaces (only the delimiter BETWEEN
    turns matters for the downstream `segment_by_speaker` regex, not
    within-turn paragraph breaks).
    """

    def __init__(self) -> None:
        super().__init__()
        self.turns: list[tuple[str, str]] = []
        self._in_msg = False
        self._msg_depth = 0
        self._in_name = False
        self._name_depth = 0
        self._in_avatar = False
        self._avatar_depth = 0
        self._name_parts: list[str] = []
        self._body_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_d = dict(attrs)
        if not self._in_msg:
            if tag == "div" and attrs_d.get("data-cy") == "transcripts_call_message":
                self._in_msg = True
                self._msg_depth = 1
                self._name_parts = []
                self._body_parts = []
            return
        if tag == "div":
            self._msg_depth += 1
            if not self._in_avatar and attrs_d.get("data-transcript-avatar") == "true":
                self._in_avatar = True
                self._avatar_depth = 1
            elif self._in_avatar:
                self._avatar_depth += 1
        if (
            not self._in_name
            and tag == "p"
            and attrs_d.get("data-transcript-speaker-name") == "true"
        ):
            self._in_name = True
            self._name_depth = 1
        elif self._in_name and tag == "p":
            self._name_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if not self._in_msg:
            return
        if self._in_name and tag == "p":
            self._name_depth -= 1
            if self._name_depth == 0:
                self._in_name = False
        if tag == "div":
            if self._in_avatar:
                self._avatar_depth -= 1
                if self._avatar_depth == 0:
                    self._in_avatar = False
            self._msg_depth -= 1
            if self._msg_depth == 0:
                name = " ".join("".join(self._name_parts).split())
                body = " ".join("".join(self._body_parts).split())
                if name and body:
                    self.turns.append((name, body))
                self._in_msg = False

    def handle_data(self, data: str) -> None:
        if not self._in_msg or self._in_avatar:
            return
        if self._in_name:
            self._name_parts.append(data)
        else:
            self._body_parts.append(data)


def _parse_roic_messages(html: str) -> list[tuple[str, str]]:
    """Return [(speaker, body), ...] in call order, or [] if the DOM shape changed."""
    p = _TranscriptMessageParser()
    p.feed(html)
    return p.turns


def _slice_qa_turns(turns: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], bool]:
    """Return (qa_turns, boundary_found).

    Searches each turn's body in call order for a start cue (the standard
    operator-script phrasing, or the IR-officer handoff variant); slices from
    the first match onward, then trims at the first end cue found from there.
    When no start cue matches any turn, `boundary_found` is False and the
    WHOLE call (prepared remarks + Q&A) is returned rather than silently
    dropping the transcript — a visible, logged degrade, not a silent one
    (see `_roic_fetch`).
    """
    start_idx: int | None = None
    for i, (_name, body) in enumerate(turns):
        if QA_BOUNDARY_RE.search(body) or _QA_HANDOFF_RE.search(body):
            start_idx = i
            break
    boundary_found = start_idx is not None
    scoped = turns[start_idx:] if start_idx is not None else list(turns)

    end_idx: int | None = None
    for i, (_name, body) in enumerate(scoped):
        m = QA_TAIL_RE.search(body)
        if m:
            end_idx = i
            scoped[i] = (scoped[i][0], scoped[i][1][: m.end()])
            break
    if end_idx is not None:
        scoped = scoped[: end_idx + 1]
    return scoped, boundary_found


def _serialize_turns(turns: list[tuple[str, str]]) -> str:
    """Render turns so the existing `\\n<Name>: <text>` speaker regex (shared with
    the PDF ingest path — never reinvented here) recovers every turn boundary."""
    return "\n\n".join(f"{name}: {body}" for name, body in turns)


def _roic_fetch(ticker: str, year: int, quarter: int) -> AggregatorHit | None:
    url = f"https://www.roic.ai/quote/{ticker.upper()}/transcripts/{year}-year/{quarter}-quarter"
    r = _http_get(url)
    if r is None or r.status_code != 200 or _has_paywall(r.text):
        return None

    turns = _parse_roic_messages(r.text)
    if turns:
        qa_turns, boundary_found = _slice_qa_turns(turns)
        qa = _serialize_turns(qa_turns)
        if len(qa) < 500:
            return None
        if not boundary_found:
            log.warning(
                "roic %s Q%s %s: no Q&A boundary cue matched any turn; "
                "keeping the whole call (prepared remarks + Q&A) rather than "
                "dropping it — %d turns, %d chars",
                ticker,
                quarter,
                year,
                len(qa_turns),
                len(qa),
            )
        return AggregatorHit(source_name="roic", page_url=url, qa_text=qa, full_text_chars=len(qa))

    # DOM shape changed / didn't match at all — degrade to the legacy
    # flatten-and-guess path rather than failing outright. Logged distinctly
    # so this fallback is visible, not a silent quality drop.
    log.warning(
        "roic %s Q%s %s: no data-cy=transcripts_call_message blocks found; "
        "falling back to flattened-text extraction (degraded speaker attribution)",
        ticker,
        quarter,
        year,
    )
    text = _strip_html(r.text)
    qa = _extract_qa(text)
    if qa is None:
        return None
    return AggregatorHit(source_name="roic", page_url=url, qa_text=qa, full_text_chars=len(text))


# ---------------------------------------------------------------------------
# Source: stockanalysis.com (list-then-fetch)
# ---------------------------------------------------------------------------


def _stockanalysis_fetch(ticker: str, year: int, quarter: int) -> AggregatorHit | None:
    list_url = f"https://stockanalysis.com/stocks/{ticker.lower()}/transcripts/"
    r = _http_get(list_url)
    if r is None or r.status_code != 200:
        return None
    # Each entry: /stocks/<ticker>/transcripts/<NUMERIC_ID>-q<N>-<YEAR>/
    pattern = re.compile(
        rf"/stocks/{re.escape(ticker.lower())}/transcripts/(\d+)-q{quarter}-{year}/"
    )
    m = pattern.search(r.text)
    if not m:
        return None
    transcript_url = (
        f"https://stockanalysis.com/stocks/{ticker.lower()}/transcripts/"
        f"{m.group(1)}-q{quarter}-{year}/"
    )
    r2 = _http_get(transcript_url)
    if r2 is None or r2.status_code != 200 or _has_paywall(r2.text):
        return None
    text = _strip_html(r2.text)
    qa = _extract_qa(text)
    if qa is None:
        return None
    return AggregatorHit(
        source_name="stockanalysis",
        page_url=transcript_url,
        qa_text=qa,
        full_text_chars=len(text),
    )


# ---------------------------------------------------------------------------
# Source: tickertrends.io
# ---------------------------------------------------------------------------


def _tickertrends_fetch(ticker: str, year: int, quarter: int) -> AggregatorHit | None:
    url = f"https://tickertrends.io/transcripts/{ticker.upper()}/Q{quarter}-earnings-transcript-{year}"
    r = _http_get(url)
    if r is None or r.status_code != 200 or _has_paywall(r.text):
        return None
    text = _strip_html(r.text)
    qa = _extract_qa(text)
    if qa is None:
        return None
    return AggregatorHit(
        source_name="tickertrends", page_url=url, qa_text=qa, full_text_chars=len(text)
    )


# ---------------------------------------------------------------------------
# Source: issuer IR results-center (the timeliest — published before aggregators)
# ---------------------------------------------------------------------------


def _issuer_ir_fetch(ticker: str, year: int, quarter: int) -> AggregatorHit | None:
    """The issuer's own IR results-center transcript (see `ir_pipeline.transcript`).

    Best-effort and config-gated: tickers without an MZ IR config short-circuit
    before any browser launch, and any discovery/render/download failure (incl. a
    missing optional `ir`/Playwright extra) degrades to None so the chain falls
    through to the free aggregators.
    """
    try:
        from ir_pipeline.transcript import fetch_ir_transcript

        hit = fetch_ir_transcript(ticker, year, quarter)
    except Exception as exc:  # first source must never break the chain
        log.debug("issuer_ir fetch failed for %s Q%s %s: %s", ticker, quarter, year, exc)
        return None
    if hit is None:
        return None
    return AggregatorHit(
        source_name="issuer_ir",
        page_url=hit.page_url,
        qa_text=hit.qa_text,
        full_text_chars=len(hit.qa_text),
    )


# ---------------------------------------------------------------------------
# Registered fallback chain — order is the priority order
# ---------------------------------------------------------------------------

SOURCES: list[AggregatorSource] = [
    AggregatorSource("issuer_ir", _issuer_ir_fetch),
    AggregatorSource("roic", _roic_fetch),
    AggregatorSource("stockanalysis", _stockanalysis_fetch),
    AggregatorSource("tickertrends", _tickertrends_fetch),
]


def fetch_qa_with_fallback(
    ticker: str,
    year: int,
    quarter: int,
    sources: list[AggregatorSource] | None = None,
) -> tuple[AggregatorHit | None, list[str]]:
    """Walk `sources` (default SOURCES) in order; return the first hit and
    the names of sources that were tried (for logging)."""
    chain = sources if sources is not None else SOURCES
    tried: list[str] = []
    for source in chain:
        tried.append(source.name)
        hit = source.fetch_qa(ticker, year, quarter)
        if hit is not None:
            return hit, tried
    return None, tried
