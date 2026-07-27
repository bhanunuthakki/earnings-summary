"""Generic headless IR-document discovery — the universal fallback adapter.

The ``mz`` / ``q4cdn`` adapters are precise but per-platform; most issuers (US
large-caps, the evaluation list) aren't on either. This adapter renders ANY
issuer's IR page with Playwright, harvests every document link (.pdf / .xlsx /
CDN / filemanager), follows obvious "quarterly results / financial reports /
archive" sub-links one hop to reach historical quarters, and returns up to
``max_quarters`` of :class:`CandidateDoc` rows.

It is **best-effort**: a login wall, a JS-only download widget, or a site with no
standard links simply yields fewer (or zero) candidates — never an exception. The
period / doc-type on each candidate is a cheap link-text *guess*; authoritative
attribution happens later from the downloaded file's content
(``categorize_ir_uploads.py``). robots.txt is honored and requests are
rate-limited.

The Playwright call is isolated behind the injectable ``render`` seam so the
link-harvest / attribution / truncation logic is unit-testable without a browser.
"""

from __future__ import annotations

import re
import time
import urllib.parse
import urllib.request
import urllib.robotparser
from collections.abc import Callable

from ir_pipeline._net import (
    PLAYWRIGHT_NETWORK_LOCKDOWN_ARG,
    PLAYWRIGHT_NO_PROXY_ARG,
    UnsafeURLError,
    build_public_opener,
    ensure_safe_public_url,
    install_public_only_playwright_routing,
)
from ir_pipeline.discover._docmeta import CandidateDoc, classify, filename_for_url

# (href, visible anchor text) for one page; the unit a renderer returns.
Anchor = tuple[str, str]
Renderer = Callable[[str, int], list[Anchor]]
FilenameFetcher = Callable[[str], str]

# A "document" link: a PDF/XLSX(/XLS), or a known IR file-host path. A trailing
# query/fragment after the extension is tolerated (...e.pdf?x=1).
_DOC_HREF_RX = re.compile(
    r"(?:\.pdf|\.xlsx|\.xls)(?:$|[?#])|mzfilemanager|q4cdn|/files/",
    re.IGNORECASE,
)

# Nav links worth following to reach the doc-bearing page (bare "Financials",
# "Quarterly Earnings", "SEC filings", "Investor news", "Events & presentations",
# etc.). Generous on purpose — same-host + page cap + period-stop bound the walk.
_HISTORY_NAV_RX = re.compile(
    r"quarter|\bfinanc(?:e|ial|ials)\b|\bearnings\b|\bresults\b|press[-\s]?releas|"
    r"presentation|\barchive\b|annual[-\s]?report|webcast|sec[-\s]?filing|"
    r"\binvestors?\b|investor[-\s]?relation|financial[-\s]?(?:information|results|reports?)",
    re.IGNORECASE,
)

# Tier-1 period guesses (hint only — content classification is authoritative).
# Q (English) and T (Portuguese "trimestre" — NU/MELI/DLO use 1T26 etc.).
_RX_FY_Q = re.compile(r"\bFY\s*((?:20)?\d{2})[\s_/-]*Q([1-4])\b", re.IGNORECASE)
_RX_YEAR_Q = re.compile(r"\b(20\d{2})[\s_/-]*Q([1-4])\b", re.IGNORECASE)
_RX_Q_YEAR = re.compile(r"\bQ([1-4])[\s'/_-]*((?:20)?\d{2})\b", re.IGNORECASE)
_RX_NQ_YEAR = re.compile(r"\b([1-4])[QT][\s'/_-]*((?:20)?\d{2})\b", re.IGNORECASE)

# ``_docmeta.classify`` speaks spreadsheet/deck/press_release/transcript; the
# manifest + categorizer speak the legacy aliases. Map the two that differ.
_DOCTYPE_ALIAS: dict[str, str] = {
    "spreadsheet": "supplement",
    "deck": "presentation",
    "press_release": "press_release",
    "transcript": "transcript",
}

_DEFAULT_RATE_LIMIT_S = 0.5
_MAX_PAGES = 8  # total pages rendered per crawl (seed + nav hops)
_MAX_DEPTH = 2  # homepage → IR landing → doc-bearing page
_DEFAULT_TIME_BUDGET_S = 240.0  # wall-clock cap; return partials before the subprocess timeout


def discover_document_history(
    *,
    ir_url: str,
    max_quarters: int = 8,
    timeout_ms: int = 60_000,
    render: Renderer | None = None,
    fetch_filename: FilenameFetcher | None = None,
    rate_limit_s: float = _DEFAULT_RATE_LIMIT_S,
    check_robots: bool = True,
    time_budget_s: float = _DEFAULT_TIME_BUDGET_S,
) -> list[CandidateDoc]:
    """Crawl ``ir_url`` (+ one hop of history links); return up to ``max_quarters`` of docs.

    ``render`` is injectable for tests (default: the lazy-Playwright renderer).
    Never raises on a dead page / empty site — returns whatever it found (``[]``
    is a valid, expected result). Candidates are deduped by URL (first win). Stops
    following nav links once ``time_budget_s`` elapses, so a slow site returns the
    docs found so far instead of timing the subprocess out to a null result.
    """
    if not ir_url:
        return []
    t0 = time.monotonic()
    do_render = render or _playwright_render
    do_fetch = fetch_filename or _safe_filename_for_url
    allows, robots_delay = _robots_policy(ir_url) if check_robots else (_allow_all, 0.0)
    effective_delay = max(rate_limit_s, robots_delay)  # honor robots Crawl-delay

    visited: set[str] = set()
    candidates: dict[str, CandidateDoc] = {}

    def harvest(page_url: str) -> list[Anchor]:
        if page_url in visited or not allows(page_url):
            return []
        visited.add(page_url)
        try:
            anchors = do_render(page_url, timeout_ms)
        except Exception:  # one page's browser/network failure is non-fatal
            return []
        for href, text in anchors:
            if href and _DOC_HREF_RX.search(href) and href not in candidates:
                candidates[href] = _to_candidate(href, text, page_url, do_fetch)
        if effective_delay > 0:
            time.sleep(effective_delay)
        return anchors

    def _distinct_periods() -> int:
        return len({(c.year_guess, c.quarter_guess) for c in candidates.values() if c.year_guess})

    # Bounded breadth-first walk: render the seed, then follow same-host nav links
    # (Financials / Quarterly / Earnings / Investor Relations / …) up to _MAX_DEPTH
    # hops to reach the doc-bearing page — handles IR landing pages AND company
    # homepages (so a homepage seed can navigate Home → Investors → Financials).
    # Stops early once enough history is found, the page budget is hit, or the time
    # budget elapses (a slow site returns partials instead of timing out to null).
    base_host = _host(ir_url)
    frontier: list[tuple[str, int]] = [(ir_url, 0)]
    rendered = 0
    while frontier:
        if (
            rendered >= _MAX_PAGES
            or _distinct_periods() >= max_quarters
            or (time.monotonic() - t0) > time_budget_s
        ):
            break
        page_url, depth = frontier.pop(0)
        if page_url in visited or not allows(page_url):
            continue
        anchors = harvest(page_url)
        rendered += 1
        if depth >= _MAX_DEPTH:
            continue
        for href, text in anchors:
            if not href or _DOC_HREF_RX.search(href):
                continue
            if not _HISTORY_NAV_RX.search(f"{href} {text}"):
                continue
            target = urllib.parse.urljoin(page_url, href)
            if _host(target) == base_host and target not in visited:
                frontier.append((target, depth + 1))

    return _truncate_to_quarters(list(candidates.values()), max_quarters)


def precise_to_candidates(precise: dict[str, str], source_page: str) -> list[CandidateDoc]:
    """Wrap a precise adapter's ``{doc_type: url}`` (latest quarter) as candidates.

    Used by the hybrid path to fold the mz adapter's high-fidelity current-quarter
    links into the generic history. ``doc_type`` keys are ``_docmeta.classify``
    values; period is left unknown (the mz page only exposes the latest quarter).
    """
    out: list[CandidateDoc] = []
    for raw_type, url in precise.items():
        if not url:
            continue
        out.append(
            CandidateDoc(
                url=url,
                link_text="",
                filename_hint=_filename_from_href(url),
                doc_type_guess=_DOCTYPE_ALIAS.get(raw_type, raw_type),
                year_guess=None,
                quarter_guess=None,
                source_page=source_page,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested directly)
# ---------------------------------------------------------------------------


def _to_candidate(
    href: str, text: str, source_page: str, fetch_filename: FilenameFetcher
) -> CandidateDoc:
    filename = _filename_from_href(href)
    # Opaque host (mzfilemanager / extension-less) → ranged GET for the real name.
    if not _has_doc_ext(filename):
        fetched = fetch_filename(href)
        if fetched:
            filename = fetched
    doc_type = _guess_doc_type(filename, text)
    year, quarter = _guess_period(f"{filename} {text}")
    return CandidateDoc(
        url=href,
        link_text=text,
        filename_hint=filename,
        doc_type_guess=doc_type,
        year_guess=year,
        quarter_guess=quarter,
        source_page=source_page,
    )


def _guess_doc_type(filename: str, text: str) -> str | None:
    raw = classify(f"{filename} {text}")
    return _DOCTYPE_ALIAS.get(raw) if raw else None


def _guess_period(text: str) -> tuple[int | None, int | None]:
    """Best-effort (year, quarter) from link text / filename. Hint only."""
    m = _RX_FY_Q.search(text)
    if m:
        return _norm_year(m.group(1)), int(m.group(2))
    m = _RX_YEAR_Q.search(text)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = _RX_Q_YEAR.search(text)
    if m:
        return _norm_year(m.group(2)), int(m.group(1))
    m = _RX_NQ_YEAR.search(text)
    if m:
        return _norm_year(m.group(2)), int(m.group(1))
    return None, None


def _truncate_to_quarters(cands: list[CandidateDoc], max_quarters: int) -> list[CandidateDoc]:
    """Keep period-dated candidates in the newest ``max_quarters`` distinct quarters.

    Candidates the crawler couldn't date (``year_guess is None``) are kept too —
    the content classifier dates them post-download, and the natural crawl bound
    (one landing page + ≤8 nav pages) keeps the set small. ``max_quarters`` caps
    only the dated set so a deep archive doesn't pull years of history.
    """
    dated = [c for c in cands if c.year_guess is not None]
    undated = [c for c in cands if c.year_guess is None]
    periods = sorted({(c.year_guess, c.quarter_guess) for c in dated}, reverse=True)
    keep = set(periods[:max_quarters])
    kept = [c for c in dated if (c.year_guess, c.quarter_guess) in keep]
    return kept + undated


def _filename_from_href(href: str) -> str:
    tail = urllib.parse.urlparse(href).path.rsplit("/", 1)[-1]
    return urllib.parse.unquote(tail)


def _has_doc_ext(name: str) -> bool:
    return name.lower().endswith((".pdf", ".xlsx", ".xls"))


def _norm_year(s: str) -> int:
    n = int(s)
    return 2000 + n if n < 100 else n


def _host(url: str) -> str:
    return urllib.parse.urlparse(url).netloc.lower()


def _allow_all(_url: str) -> bool:
    return True


def _deny_all(_url: str) -> bool:
    return False


_ROBOTS_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
_MAX_CRAWL_DELAY_S = 12.0


def _robots_policy(url: str) -> tuple[Callable[[str], bool], float]:
    """Return ``(can_fetch predicate, crawl_delay seconds)`` for ``url``'s origin.

    robots.txt is fetched with a browser User-Agent on purpose: many corporate
    IR sites 403 a bare ``urllib`` request, and ``RobotFileParser.read()`` treats
    a 403 on robots.txt as *disallow-all* — which would falsely block sites that
    actually permit crawling (e.g. Visa/Oracle publish ``Allow: /``). A missing /
    unreachable robots.txt means "no rules" → allow. We honor a published
    ``Crawl-delay`` (capped) so the crawl stays polite.
    """
    parsed = urllib.parse.urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = urllib.robotparser.RobotFileParser()
    try:
        ensure_safe_public_url(robots_url)
        req = urllib.request.Request(robots_url, headers={"User-Agent": _ROBOTS_UA})
        with build_public_opener().open(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", "replace")
        rp.parse(raw.splitlines())
    except UnsafeURLError:
        return _deny_all, 0.0
    except Exception:  # missing / unreachable robots.txt → no rules → allow
        return _allow_all, 0.0

    raw_delay = rp.crawl_delay("*")
    delay_s = min(float(raw_delay), _MAX_CRAWL_DELAY_S) if raw_delay else 0.0

    def _can(u: str) -> bool:
        try:
            return rp.can_fetch("*", u)
        except Exception:
            return True

    return _can, delay_s


def _safe_filename_for_url(href: str) -> str:
    """``filename_for_url`` (ranged GET for Content-Disposition), guarded.

    A network error / odd host returns "" so the candidate just carries an empty
    filename hint — the content classifier resolves its type post-download.
    """
    try:
        return filename_for_url(href)
    except Exception:
        return ""


# CSS selector for the document-link patterns — used to wait for JS to inject
# the earnings docs (q4cdn / Investis / mz file hosts) before harvesting.
_DOC_LINK_SELECTOR = (
    "a[href*='q4cdn'], a[href*='mzfilemanager'], a[href*='/files/'], "
    "a[href$='.pdf'], a[href*='.pdf?'], a[href$='.xlsx'], a[href$='.xls']"
)
_DOC_WAIT_MS = 6000  # wait up to this for JS-injected doc links to attach
_SETTLE_FALLBACK_MS = 2000  # no doc link appeared (nav/landing page) — settle, then harvest anyway


def _playwright_render(url: str, timeout_ms: int) -> list[Anchor]:
    """Render ``url`` headlessly; return [(href, text)] for every anchor on the page.

    Lazy import of the optional ``ir`` extra (Playwright). Two hard-won choices:

    * ``wait_until="domcontentloaded"`` (NOT ``networkidle``) — ad/tracker/chat-heavy
      commercial IR sites never reach network-idle, so ``networkidle`` times out and
      yields nothing. After the DOM loads we explicitly wait for the JS-injected
      document links to attach (capped), falling back to a short settle delay.
    * Harvest **all** anchors, not just visible ones — earnings PDFs on q4cdn /
      Investis sites are collapsed inside year tabs/accordions (``offsetParent`` is
      null) until clicked; ``_DOC_HREF_RX`` filters out the non-document links.
    """
    from playwright.sync_api import sync_playwright

    js = "els => els.map(a => [a.href, (a.textContent || '').trim()])"
    with sync_playwright() as pw:
        # --disable-http2: some IR servers mis-negotiate HTTP/2 with headless
        # chromium (net::ERR_HTTP2_PROTOCOL_ERROR); forcing HTTP/1.1 is robust.
        browser = pw.chromium.launch(
            headless=True,
            args=["--disable-http2", PLAYWRIGHT_NETWORK_LOCKDOWN_ARG, PLAYWRIGHT_NO_PROXY_ARG],
        )
        try:
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                service_workers="block",
            )
            install_public_only_playwright_routing(context, timeout_s=timeout_ms / 1000)
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                page.wait_for_selector(_DOC_LINK_SELECTOR, timeout=_DOC_WAIT_MS, state="attached")
            except Exception:  # no doc link surfaced — a nav/landing page; harvest its links
                page.wait_for_timeout(_SETTLE_FALLBACK_MS)
            raw = page.eval_on_selector_all("a[href]", js)
        finally:
            browser.close()

    out: list[Anchor] = []
    for item in raw:  # item: Any (playwright eval result) — unpack defensively
        try:
            href, text = item
        except (TypeError, ValueError):
            continue
        out.append((str(href), str(text)))
    return out
