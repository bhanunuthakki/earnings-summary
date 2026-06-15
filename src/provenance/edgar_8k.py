"""Automated SEC 8-K exhibit extraction → ``fact_overrides`` proposals.

Fetches a company's earnings press-release exhibit (EX-99.1) from EDGAR by
accession number, LLM-extracts the product/geographic segment revenue table into a
structured dict, and builds a record-level ``segment`` override the company doc can
authoritatively assert over FMP. The override is the durable mechanism
(``directives/provenance_override_2026_06.md``) — this module is the *populating*
front-end the owner asked for (vs. hand-seeding via ``record_fact_override.py``).

Design: every side-effecting seam is injectable so the whole pipeline is testable
with no network and no LLM spend —

* ``get_json`` / ``get_text`` — HTTP GET (default: ``requests`` with the SEC UA);
* ``call`` — the structured-LLM transport (default: ``llm.structured.call_llm_structured``,
  routed to the cheapest at-parity model via the ``extract_8k_overrides`` purpose).

The orchestrator returns an :class:`ExtractedSegmentOverride` (the proposal); it
never writes to the DB. ``execution/extract_8k_overrides.py --apply`` records it via
``provenance.overrides.record_override``.
"""

from __future__ import annotations

import html as _htmlmod
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import cast

# SEC requires a descriptive User-Agent on every request.
SEC_USER_AGENT = "earnings-summary provenance-override (contact: bhanunuthakthi@gmail.com)"

# LLM purpose key — register the cheapest at-parity model in llm.cli.LLM_MODELS.
EIGHT_K_PURPOSE = "extract_8k_overrides"

GetJson = Callable[[str], object]
GetText = Callable[[str], str]
StructuredCall = Callable[..., object]


@dataclass(frozen=True, slots=True)
class ExtractedSegmentOverride:
    """A proposed record-level segment override extracted from an 8-K exhibit."""

    ticker: str
    period_end: str
    fiscal_period_type: str
    dim_type: str  # 'product' | 'geography'
    segments: dict[str, float]  # {segment_name: value in actual USD}
    source_accession: str
    source_exhibit: str
    source_url: str
    source_excerpt: str


# ---------------------------------------------------------------------------
# Default HTTP transport (injectable)
# ---------------------------------------------------------------------------


def _default_get_json(url: str) -> object:
    import requests

    resp = requests.get(url, headers={"User-Agent": SEC_USER_AGENT}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _default_get_text(url: str) -> str:
    import requests

    resp = requests.get(url, headers={"User-Agent": SEC_USER_AGENT}, timeout=60)
    resp.raise_for_status()
    return resp.text


_TAG_RX = re.compile(r"<[^>]+>")
_WS_RX = re.compile(r"[ \t]+")
_BLANK_RX = re.compile(r"\n\s*\n\s*\n+")


def strip_html(raw: str) -> str:
    """Compact HTML → readable text: drop script/style, tags → space, unescape entities."""
    raw = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)</(p|div|tr|table|h[1-6]|li|br)\s*>", "\n", raw)
    text = _TAG_RX.sub(" ", raw)
    text = _htmlmod.unescape(text)
    text = _WS_RX.sub(" ", text)
    return _BLANK_RX.sub("\n\n", text).strip()


# ---------------------------------------------------------------------------
# EDGAR resolution + fetch
# ---------------------------------------------------------------------------


def _acc_nodash(accession: str) -> str:
    return accession.replace("-", "")


def resolve_cik(ticker: str, *, get_json: GetJson = _default_get_json) -> str | None:
    """Resolve a ticker to its zero-padded 10-digit CIK via EDGAR's index."""
    try:
        payload = get_json("https://www.sec.gov/files/company_tickers.json")
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    want = ticker.upper()
    for row in cast("dict[str, object]", payload).values():
        if isinstance(row, dict):
            r = cast("dict[str, object]", row)
            if str(r.get("ticker", "")).upper() == want:
                return str(int(str(r.get("cik_str") or r.get("cik") or "0"))).zfill(10)
    return None


def discover_exhibit(
    cik: str, accession: str, *, get_json: GetJson = _default_get_json
) -> str | None:
    """Find the press-release exhibit filename (prefer EX-99.1) in a filing's index.

    Reads ``.../{cik}/{accNoDashes}/index.json`` and returns the first ``.htm``
    item whose type starts with ``EX-99``; falls back to any ``ex99*.htm`` name.
    """
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{_acc_nodash(accession)}"
    try:
        payload = get_json(f"{base}/index.json")
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    directory_raw = cast("dict[str, object]", payload).get("directory")
    if not isinstance(directory_raw, dict):
        return None
    files_raw = cast("dict[str, object]", directory_raw).get("item")
    if not isinstance(files_raw, list):
        return None
    fallback: str | None = None
    for f in cast("list[object]", files_raw):
        if not isinstance(f, dict):
            continue
        fd = cast("dict[str, object]", f)
        name = str(fd.get("name") or "")
        ftype = str(fd.get("type") or "")
        if not name.lower().endswith((".htm", ".html")):
            continue
        if ftype.upper().startswith("EX-99"):
            return name
        if fallback is None and "ex99" in name.lower().replace("-", "").replace("_", ""):
            fallback = name
    return fallback


def fetch_exhibit_text(
    *,
    ticker: str,
    accession: str,
    exhibit: str | None = None,
    cik: str | None = None,
    get_json: GetJson = _default_get_json,
    get_text: GetText = _default_get_text,
) -> tuple[str, str, str] | None:
    """Fetch one 8-K exhibit and return ``(plain_text, exhibit_filename, url)`` or None.

    Resolves the CIK (unless provided) and discovers the EX-99.1 exhibit (unless an
    explicit ``exhibit`` filename is given).
    """
    resolved_cik = cik or resolve_cik(ticker, get_json=get_json)
    if resolved_cik is None:
        return None
    name = exhibit or discover_exhibit(resolved_cik, accession, get_json=get_json)
    if not name:
        return None
    url = f"https://www.sec.gov/Archives/edgar/data/{int(resolved_cik)}/{_acc_nodash(accession)}/{name}"
    try:
        raw = get_text(url)
    except Exception:
        return None
    return strip_html(raw), name, url


# ---------------------------------------------------------------------------
# LLM extraction
# ---------------------------------------------------------------------------

_DIM_LABEL = {"product": "product / business-segment", "geography": "geographic"}

_PROMPT = """\
You are extracting the {dim_label} revenue table for {ticker} {fiscal_period_type} \
(period ending {period_end}) from the text of an SEC 8-K earnings press-release exhibit.

Return ONLY a JSON object mapping each segment name to its revenue for THIS reporting \
period, expressed in FULL US DOLLARS (e.g. $17,664 million -> 17664000000). Use the \
issuer's exact segment names. Include negative eliminations/"Other" lines as given. \
Do NOT include totals, year-over-year %s, prior-period figures, or any non-revenue line.

If the exhibit does not contain a {dim_label} revenue breakdown for this period, return \
an empty object {{}}.

EXHIBIT TEXT:
<<<
{excerpt}
>>>
"""

_MAX_EXCERPT_CHARS = 24000


def extract_segment_map(
    *,
    text: str,
    ticker: str,
    period_end: str,
    fiscal_period_type: str,
    dim_type: str,
    call: StructuredCall | None = None,
) -> dict[str, float]:
    """LLM-extract ``{segment_name: value_in_usd}`` from exhibit text.

    Returns ``{}`` when no breakdown is present. ``call`` defaults to
    ``llm.structured.call_llm_structured`` (routed to the cheapest at-parity model
    via the ``extract_8k_overrides`` purpose); inject it in tests to avoid spend.
    """
    prompt = _PROMPT.format(
        dim_label=_DIM_LABEL.get(dim_type, dim_type),
        ticker=ticker.upper(),
        fiscal_period_type=fiscal_period_type,
        period_end=period_end,
        excerpt=text[:_MAX_EXCERPT_CHARS],
    )
    if call is not None:
        raw = call(prompt, purpose=EIGHT_K_PURPOSE, ticker=ticker.upper(), expect="object")
    else:
        from llm.structured import call_llm_structured

        raw = call_llm_structured(
            prompt, purpose=EIGHT_K_PURPOSE, ticker=ticker.upper(), expect="object"
        )
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for name, value in cast("dict[str, object]", raw).items():
        try:
            out[str(name)] = float(str(value))
        except (TypeError, ValueError):
            continue
    return out


def _norm_segment(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def score_segment_extraction(
    extracted: Mapping[str, float], expected: Mapping[str, float], *, rel_tol: float = 0.01
) -> float:
    """0..1 accuracy of an extracted segment map vs the golden expected map.

    A segment counts as matched when its normalized name is present and the value
    is within ``rel_tol`` (default 1%). The score divides matches by the larger of
    the two maps so BOTH misses and spurious extra segments lower it — exactly the
    failure modes of the FMP contamination this extractor defends against.
    """
    exp = {_norm_segment(k): v for k, v in expected.items()}
    got = {_norm_segment(k): v for k, v in extracted.items()}
    if not exp:
        return 1.0 if not got else 0.0
    matched = 0
    for key, want in exp.items():
        have = got.get(key)
        if have is None:
            continue
        if want == 0:
            if have == 0:
                matched += 1
        elif abs(have - want) / abs(want) <= rel_tol:
            matched += 1
    denom = max(len(exp), len(got))
    return matched / denom if denom else 1.0


def extract_8k_segment_override(
    *,
    ticker: str,
    accession: str,
    period_end: str,
    fiscal_period_type: str,
    dim_type: str = "product",
    exhibit: str | None = None,
    cik: str | None = None,
    get_json: GetJson = _default_get_json,
    get_text: GetText = _default_get_text,
    call: StructuredCall | None = None,
) -> ExtractedSegmentOverride | None:
    """Fetch the 8-K exhibit, extract the segment table, and build an override proposal.

    Returns None when the exhibit can't be fetched or carries no segment breakdown.
    Does NOT write to the DB — the CLI records it (``--apply``).
    """
    fetched = fetch_exhibit_text(
        ticker=ticker,
        accession=accession,
        exhibit=exhibit,
        cik=cik,
        get_json=get_json,
        get_text=get_text,
    )
    if fetched is None:
        return None
    text, exhibit_name, url = fetched
    segments = extract_segment_map(
        text=text,
        ticker=ticker,
        period_end=period_end,
        fiscal_period_type=fiscal_period_type,
        dim_type=dim_type,
        call=call,
    )
    if not segments:
        return None
    excerpt = " ".join(f"{k}: {v:,.0f}" for k, v in list(segments.items())[:8])
    return ExtractedSegmentOverride(
        ticker=ticker.upper(),
        period_end=period_end[:10],
        fiscal_period_type=fiscal_period_type,
        dim_type=dim_type,
        segments=segments,
        source_accession=accession,
        source_exhibit=exhibit_name,
        source_url=url,
        source_excerpt=excerpt[:1024],
    )
