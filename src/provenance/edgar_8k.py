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

The orchestrator (``extract_8k_segment_override``) returns an
:class:`ExtractedSegmentOverride` (the proposal); it never writes to the DB.
``execution/extract_8k_overrides.py --apply`` records it via
``provenance.overrides.record_override``, after first calling
``register_8k_exhibit_document`` (the one function in this module that DOES touch
the DB — it needs a real ``documents.id`` for the proposal's ``html_span`` locator).

Anchor-quote verification (docs/design/provenance_clickthrough.md §3.3, Phase C):
the LLM must return a verbatim ``anchor_quote`` alongside each segment's value, and
``pipeline.locators.verify_quote_in_source`` checks it against the ACTUAL fetched
exhibit text before a ``FactLocator`` is ever built. Every segment's quote verifying
earns an ``html_span`` locator (the render-side ``doc_id`` is filled in later by
the CLI, once the exhibit is registered as a ``documents`` row); any segment whose
returned quote does NOT verify demotes the whole override's locator to a
``LegacyEscapeHatch`` — never a fabricated anchor, per the "never persist a
hallucinated locator" rule. The override's VALUE is persisted either way (a bad
anchor is a provenance-quality problem, not a reason to drop a real number).
"""

from __future__ import annotations

import hashlib
import html as _htmlmod
import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import cast

from models.facts import FactLocator, LegacyEscapeHatch
from pipeline.locators import html_span_locator, locate_char_span, verify_quote_in_source

# SEC requires a descriptive User-Agent on every request.
SEC_USER_AGENT = "earnings-summary provenance-override (contact: bhanunuthakthi@gmail.com)"

# LLM purpose key — register the cheapest at-parity model in llm.cli.LLM_MODELS.
EIGHT_K_PURPOSE = "extract_8k_overrides"

GetJson = Callable[[str], object]
GetText = Callable[[str], str]
StructuredCall = Callable[..., object]

# Max chars of the verified-quote snippet carried on the override's locator
# (mirrors FactLocator.verbatim_snippet's own cap).
_MAX_LOCATOR_SNIPPET = 2000


@dataclass(frozen=True, slots=True)
class SegmentExtraction:
    """One segment's LLM-returned value plus its grounding anchor quote.

    ``anchor_quote`` is ``None`` when the LLM followed the prompt's "omit
    rather than fabricate" instruction — an honest gap, not a hallucination;
    it only becomes a hallucination signal once a NON-None quote fails
    ``verify_quote_in_source`` against the real exhibit text.
    """

    value: float
    anchor_quote: str | None


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
    # The full stripped exhibit text the LLM read (and every anchor_quote was
    # verified against) -- carried so the CLI can register a `documents` row
    # for it (register_8k_exhibit_document) without a second HTTP fetch.
    exhibit_text: str = ""
    # Phase C (§3.3): the anchor-verified locator, or an explicit opt-out.
    # ``html_span.doc_id`` is None here -- edgar_8k.py never touches the DB;
    # execution/extract_8k_overrides.py --apply patches in the real
    # documents.id once the exhibit text is registered.
    locator: FactLocator | LegacyEscapeHatch = field(
        default_factory=lambda: LegacyEscapeHatch(
            reason="extract_8k_overrides: no anchor_quote evaluated for this proposal"
        )
    )
    # Segment names whose anchor_quote was present but failed verification
    # (the hallucination signal) -- empty when every returned quote verified
    # or none were returned at all.
    failed_segments: tuple[str, ...] = ()


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

Return ONLY a JSON object mapping each segment name to an object with TWO fields:
  - "value": the segment's revenue for THIS reporting period, expressed in FULL US \
DOLLARS (e.g. $17,664 million -> 17664000000). Use the issuer's exact segment names. \
Include negative eliminations/"Other" lines as given. Do NOT include totals, \
year-over-year %s, prior-period figures, or any non-revenue line.
  - "anchor_quote": the VERBATIM snippet (under 200 characters) copied \
character-for-character from the exhibit text that contains this segment's reported \
value — the exact sentence or table row you read it from. Never paraphrase or \
reformat; if you cannot quote it exactly, omit this field for that segment rather \
than guessing.

Example shape: {{"Google Cloud": {{"value": 17664000000, "anchor_quote": "Google Cloud $17,664"}}}}

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
) -> dict[str, SegmentExtraction]:
    """LLM-extract ``{segment_name: SegmentExtraction(value, anchor_quote)}``.

    Returns ``{}`` when no breakdown is present. ``call`` defaults to
    ``llm.structured.call_llm_structured`` (routed to the cheapest at-parity model
    via the ``extract_8k_overrides`` purpose); inject it in tests to avoid spend.

    Anchor-quote grounding (docs/design/provenance_clickthrough.md §3.3, Phase C):
    the schema requires a per-segment ``anchor_quote`` alongside ``value`` — this
    function only PARSES what the LLM returned; ``extract_8k_segment_override``
    is where each quote is actually verified against the real exhibit text via
    ``pipeline.locators.verify_quote_in_source`` before any locator is built.
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
    out: dict[str, SegmentExtraction] = {}
    for name, entry in cast("dict[str, object]", raw).items():
        # Tolerate a model that ignores the nested-object instruction and
        # returns a flat {name: value} scalar anyway -- still parseable as a
        # value with no anchor quote, rather than dropping the whole segment.
        if isinstance(entry, dict):
            entry_map = cast("dict[str, object]", entry)
            value_raw = entry_map.get("value")
            quote_raw = entry_map.get("anchor_quote")
        else:
            value_raw = entry
            quote_raw = None
        try:
            value = float(str(value_raw))
        except (TypeError, ValueError):
            continue
        anchor_quote = (
            quote_raw.strip() if isinstance(quote_raw, str) and quote_raw.strip() else None
        )
        out[str(name)] = SegmentExtraction(value=value, anchor_quote=anchor_quote)
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
    extractions = extract_segment_map(
        text=text,
        ticker=ticker,
        period_end=period_end,
        fiscal_period_type=fiscal_period_type,
        dim_type=dim_type,
        call=call,
    )
    if not extractions:
        return None
    segments = {name: sx.value for name, sx in extractions.items()}
    locator, failed_segments = _build_verified_locator(extractions, source_text=text)
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
        exhibit_text=text,
        locator=locator,
        failed_segments=failed_segments,
    )


def _build_verified_locator(
    extractions: Mapping[str, SegmentExtraction], *, source_text: str
) -> tuple[FactLocator | LegacyEscapeHatch, tuple[str, ...]]:
    """Verify every returned ``anchor_quote`` against the real exhibit text and
    build the record-level locator (docs/design/provenance_clickthrough.md §3.3).

    A single override row covers every segment in the table (record-level
    ``replace``), so there is one locator slot for potentially many segments'
    quotes. Rule:

    * ANY segment whose ``anchor_quote`` was given but fails
      ``verify_quote_in_source`` demotes the WHOLE locator to a
      ``LegacyEscapeHatch`` -- one hallucinated quote is enough to make the
      aggregate untrustworthy, and the failing segment names are returned so
      the caller can log a ``validation_issues`` row (``rule=hallucinated_anchor``).
      The extracted VALUES themselves are unaffected -- they are still
      returned in ``segments`` and still get recorded.
    * Otherwise, every VERIFIED quote (segments that omitted a quote simply
      don't contribute one -- an honest gap, not a failure) is folded into one
      ``html_span`` locator: the first verified quote anchors ``html_span``
      itself (with a best-effort char span via
      ``pipeline.locators.locate_char_span``), and the concatenation of every
      verified quote becomes ``verbatim_snippet`` so the peek popover shows
      coverage across the whole table, not just one segment.
    * With zero verified quotes and zero failures (every segment omitted its
      quote), there is nothing to anchor -- a ``LegacyEscapeHatch``.

    ``html_span.doc_id`` is left ``None`` here; the CLI patches in the real
    ``documents.id`` once it registers the fetched exhibit text.
    """
    verified_quotes: list[str] = []
    failed_segments: list[str] = []
    for name, sx in extractions.items():
        if sx.anchor_quote is None:
            continue
        if verify_quote_in_source(sx.anchor_quote, source_text):
            verified_quotes.append(sx.anchor_quote)
        else:
            failed_segments.append(name)

    if failed_segments:
        reason = (
            "anchor_verification_failed: anchor_quote for "
            f"{', '.join(sorted(failed_segments))} not found verbatim in the exhibit text"
        )
        return LegacyEscapeHatch(reason=reason[:500]), tuple(failed_segments)

    if not verified_quotes:
        return (
            LegacyEscapeHatch(
                reason="no anchor_quote returned for any segment in this 8-K extraction"
            ),
            (),
        )

    primary = verified_quotes[0]
    span = locate_char_span(primary, source_text)
    locator = html_span_locator(
        doc_id=None,
        quote=primary,
        char_start=span[0] if span is not None else None,
        char_end=span[1] if span is not None else None,
    )
    combined_snippet = "\n".join(verified_quotes)[:_MAX_LOCATOR_SNIPPET]
    locator = locator.model_copy(update={"verbatim_snippet": combined_snippet})
    return locator, ()


# ---------------------------------------------------------------------------
# Document registration (the ONE function in this module that touches the DB)
# ---------------------------------------------------------------------------
#
# Every other function above is a pure fetch/extract/verify step, injectable
# and DB-free per the module's own design contract. An `html_span` locator's
# `doc_id` must resolve to a real `documents.id` (docs/design/
# provenance_clickthrough.md §1.4), so the fetched exhibit text itself needs a
# documents row -- this mirrors `pipeline.sec_6k_fetch.register_6k_document`
# exactly (same source_type='sec_xbrl' / tier=SEC_OFFICIAL convention: this IS
# a bona fide SEC-official primary filing; the LLM-extraction method lives on
# the fact_overrides row's own created_by tag, not on a different document
# source_type). Persisted under data/historical/sec/ (a deliverable, not a
# `.tmp/` cache) since the exhibit is durable evidence, not a regenerable
# artifact.


def register_8k_exhibit_document(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    accession: str,
    exhibit_name: str,
    exhibit_text: str,
    source_url: str,
    period_end: str,
    repo_root: Path,
) -> int:
    """Persist the fetched (stripped-text) exhibit and insert one ``documents``
    row; returns ``documents.id``. Idempotent on sha256 -- re-running against
    the same exhibit content returns the existing row's id rather than
    duplicating it.
    """
    sha256 = hashlib.sha256(exhibit_text.encode("utf-8")).hexdigest()
    existing = conn.execute(
        "SELECT id FROM documents WHERE sha256 = ? LIMIT 1", (sha256,)
    ).fetchone()
    if existing is not None:
        return int(existing[0])

    out_dir = repo_root / "data" / "historical" / "sec"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{ticker.upper()}_8k_{_acc_nodash(accession)}_{exhibit_name}.txt"
    out_path.write_text(exhibit_text, encoding="utf-8")
    rel_path = str(out_path.relative_to(repo_root)).replace("\\", "/")

    cur = conn.execute(
        "INSERT INTO documents "
        "(ticker, source_type, doc_type, period_start, period_end, file_path, "
        " sha256, fetched_at, fetch_status, http_code, raw_bytes_size, source_url, "
        " parent_document_id, accession_number) "
        "VALUES (?, 'sec_xbrl', 'sec_8k', NULL, ?, ?, ?, ?, 'ok', 200, ?, ?, NULL, ?)",
        (
            ticker.upper(),
            period_end[:10],
            rel_path,
            sha256,
            datetime.now(),
            len(exhibit_text.encode("utf-8")),
            source_url,
            accession,
        ),
    )
    return int(cur.lastrowid) if cur.lastrowid is not None else 0
