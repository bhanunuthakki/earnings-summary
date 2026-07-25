"""Per-form section taxonomies + the header matcher that applies them.

Three facts drive this module's shape:

1. **Every form numbers its items differently, but the DISCLOSURES recur.** A
   10-K's Item 1A, a 10-Q's Part II Item 1A and a 20-F's Item 3.D are all
   "risk factors". Longitudinal tracking has to survive a company changing
   form (and has to work at all for the 20-F names on the book: NU, NVO, WIX),
   so each item spec carries a cross-form ``concept`` and it is the concept
   that lands in ``filing_sections.canonical_id``. The form-specific label
   ("Item 3.D") is preserved verbatim in ``section_key_raw``, so nothing is
   lost.

2. **A 10-Q reuses item numbers across Part I and Part II.** "Item 1" is
   Financial Statements in Part I and Legal Proceedings in Part II. Matching
   items without first splitting on the part boundary silently mislabels half
   the document, so ``split_10q`` is part-aware by construction.

3. **The table of contents matches every item pattern, in order.** A naive
   "first match wins" or "last match wins" both fail (TOC first, cross-
   references last). ``locate_items`` instead scores whole *chains* of
   candidate headers by how much text sits between them: a TOC's entries are
   ~100 chars apart, a body's are thousands, so the body chain wins on score
   without any hard-coded "skip the first N chars" guess.

6-K exhibits have no mandated structure at all and are deliberately absent
here — ``edgar_sections.split_freeform`` handles them by heading detection
with ``canonical_id=None``, which is the honest answer rather than forcing a
taxonomy onto free-form text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from filings.models import FilingForm

#: A header line longer than this is prose that mentions the item, not a header.
_MAX_HEADER_LINE_LEN = 200
#: Dotted page-number leader — the table-of-contents tell.
_TOC_LEADER = ".."
#: A header line that ends with its own trailing page number ("Item 5. MD&A
#: 60") — the same TOC tell as the dotted leader, just without the dots. Rare
#: in this corpus (most filers' HTML puts the page number in its own <p>,
#: hence its own line — see ``_BARE_PAGE_NUMBER_RX`` below) but cheap to guard
#: against for filers whose renderer keeps title and number on one line.
_TRAILING_PAGE_NUMBER_RX = re.compile(r"\s+\d{1,4}$")
#: A standalone page-number line — one whole line containing nothing but a
#: 1-4 digit number. This is the dotless-TOC tell that actually fires in this
#: corpus: NU's 20-F and FCX's 10-K both render their table of contents as one
#: block element per title and one per page number (so each ends up on its own
#: line with no dotted leader at all), while a real body header is preceded and
#: followed by prose, not a number.
_BARE_PAGE_NUMBER_RX = re.compile(r"^[ \t]*\d{1,4}[ \t]*$", re.MULTILINE)
#: How far past a header to look for that bare-page-number tell. Wide enough to
#: span a TOC parent entry's own sub-item bullets (each with its own page
#: number, e.g. a 20-F's "A. Offer Statistics / 155 / B. Method... / 155")
#: before the next ITEM line, narrow enough that it won't reach past a real
#: section's own body into an unrelated later item.
_TOC_LOOKAHEAD_CHARS = 400
#: A single incidental page number turning up near a genuinely short answer
#: ("Not applicable.", two lines below a real header, from the page footer) is
#: normal and must not be flagged. Even two real, back-to-back terse items
#: (each its own one-line answer) can each contribute a lone footer number —
#: see ``_looks_like_toc_entry`` for the real corpus case that forced this to
#: 3 rather than 2. A genuine TOC cluster always has more than two page
#: numbers in play within the window.
_MIN_TOC_PAGE_NUMBERS = 3
#: Another item header STARTING a line — required, IN ADDITION to the
#: page-number density above, before a candidate is rejected as TOC. Item 8
#: (Financial Statements) and Item 15 (Exhibits) routinely open their REAL
#: body with their own inline index ("Index to Consolidated Financial
#: Statements ... The following ... are included in Item 8 of this Annual
#: Report ... Report of Independent Registered Public Accounting Firm ... 65
#: ... Consolidated Balance Sheets ... 67 ..."), which is just as
#: page-number-dense as a real TOC AND can even cross-reference another item
#: by number in passing prose ("included in Item 8 of this..."). The
#: anchor to a LINE START is what tells the two apart: a real table of
#: contents marches through consecutive item numbers each headlining their
#: own line, while a prose cross-reference has "Item N" in the middle of a
#: sentence, never at the start of a line. Requiring both signals is what
#: keeps this from mistaking a financial-statement/exhibit index for a table
#: of contents and dropping the one real header for Item 8 or Item 15.
_ITEM_LINE_START_RX = re.compile(r"^[ \t]*item\s*\d", re.IGNORECASE | re.MULTILINE)
#: Gap contributions are capped so one enormous section can't outweigh a chain
#: that locates many well-separated headers.
_GAP_CAP = 20_000
#: Per-located-item bonus: prefer a chain that finds more items, all else equal.
_ITEM_BONUS = 1_500
#: Below this, two headers are adjacent (TOC-like) and contribute no gap score.
_MIN_SECTION_CHARS = 240


@dataclass(frozen=True, slots=True)
class ItemSpec:
    """One mandated item within a form.

    ``key`` is the stable, form-specific label written to
    ``filing_sections.section_key_raw`` ("Item 1A"). ``concept`` is the
    cross-form disclosure identity written to ``canonical_id``.
    """

    key: str
    concept: str
    title_rx: re.Pattern[str]
    #: Matches the item number alone on its own line — the fallback for
    #: filings that put the title on the following line.
    bare_rx: re.Pattern[str]
    part: str | None = None


def _item_patterns(number: str, title_alternatives: str) -> tuple[re.Pattern[str], re.Pattern[str]]:
    """Build the (title, bare) pattern pair for one item number.

    ``number`` is the literal item designator with its own trailing guard baked
    in by the caller (e.g. ``r"1a"`` vs ``r"1(?![0-9abc])"``) so "Item 1" never
    swallows "Item 1A".
    """
    sep = r"\s*[.:\-–—]?\s*"  # noqa: RUF001 — EN/EM dash are real SEC item separators
    title = re.compile(
        rf"item\s*{number}{sep}(?:{title_alternatives})",
        re.IGNORECASE,
    )
    bare = re.compile(rf"^[ \t]*item\s*{number}[ \t.:\-–—]*$", re.IGNORECASE | re.MULTILINE)  # noqa: RUF001 — EN/EM dash are real SEC separators
    return title, bare


def _spec(key: str, concept: str, number: str, titles: str, part: str | None = None) -> ItemSpec:
    title_rx, bare_rx = _item_patterns(number, titles)
    return ItemSpec(key=key, concept=concept, title_rx=title_rx, bare_rx=bare_rx, part=part)


# --- 10-K -------------------------------------------------------------------
# Item numbers carry explicit guards: "1" must not match "1A"/"1B"/"1C", "9"
# must not match "9A"/"9B"/"9C", "1" must not match "10".

FORM_10K_ITEMS: tuple[ItemSpec, ...] = (
    _spec("Item 1", "business", r"1(?![0-9abc])", r"business"),
    _spec("Item 1A", "risk_factors", r"1\s*a", r"risk\s+factors?"),
    _spec("Item 1B", "unresolved_staff_comments", r"1\s*b", r"unresolved\s+staff"),
    _spec("Item 1C", "cybersecurity", r"1\s*c", r"cybersecurity"),
    _spec("Item 2", "properties", r"2(?![0-9])", r"propert"),
    _spec("Item 3", "legal_proceedings", r"3(?![0-9])", r"legal\s+proceedings?"),
    _spec("Item 4", "mine_safety", r"4(?![0-9])", r"mine\s+safety|submission\s+of\s+matters"),
    _spec("Item 5", "market_for_equity", r"5(?![0-9])", r"market\s+for"),
    _spec(
        "Item 6", "selected_financial_data", r"6(?![0-9])", r"\[?reserved\]?|selected\s+financial"
    ),
    _spec("Item 7", "mdna", r"7(?![0-9a])", r"management.{0,3}s\s+discussion"),
    _spec("Item 7A", "market_risk", r"7\s*a", r"quantitative\s+and\s+qualitative"),
    _spec("Item 8", "financial_statements", r"8(?![0-9])", r"financial\s+statements"),
    _spec("Item 9", "auditor_changes", r"9(?![0-9abc])", r"changes\s+in\s+and\s+disagreements"),
    _spec("Item 9A", "controls_procedures", r"9\s*a", r"controls\s+and\s+procedures"),
    _spec("Item 9B", "other_information", r"9\s*b", r"other\s+information"),
    _spec("Item 9C", "foreign_jurisdictions", r"9\s*c", r"disclosure\s+regarding\s+foreign"),
    _spec("Item 10", "directors_officers", r"10(?![0-9])", r"directors,?\s+executive"),
    _spec("Item 11", "executive_compensation", r"11(?![0-9])", r"executive\s+compensation"),
    _spec("Item 12", "security_ownership", r"12(?![0-9])", r"security\s+ownership"),
    _spec("Item 13", "related_party_transactions", r"13(?![0-9])", r"certain\s+relationships"),
    _spec("Item 14", "accountant_fees", r"14(?![0-9])", r"principal\s+account"),
    _spec("Item 15", "exhibits", r"15(?![0-9])", r"exhibits?"),
    _spec("Item 16", "form_summary", r"16(?![0-9])", r"form\s+10-k\s+summary"),
)

# --- 10-Q -------------------------------------------------------------------
# Part-scoped: the same numbers mean different disclosures in each part, so
# these two tuples are only ever applied to their own part's slice.

FORM_10Q_PART1_ITEMS: tuple[ItemSpec, ...] = (
    _spec("Part I Item 1", "financial_statements", r"1(?![0-9a])", r"financial\s+statements", "I"),
    _spec("Part I Item 2", "mdna", r"2(?![0-9])", r"management.{0,3}s\s+discussion", "I"),
    _spec("Part I Item 3", "market_risk", r"3(?![0-9])", r"quantitative\s+and\s+qualitative", "I"),
    _spec(
        "Part I Item 4", "controls_procedures", r"4(?![0-9])", r"controls\s+and\s+procedures", "I"
    ),
)

FORM_10Q_PART2_ITEMS: tuple[ItemSpec, ...] = (
    _spec("Part II Item 1", "legal_proceedings", r"1(?![0-9a])", r"legal\s+proceedings?", "II"),
    _spec("Part II Item 1A", "risk_factors", r"1\s*a", r"risk\s+factors?", "II"),
    _spec("Part II Item 2", "unregistered_sales", r"2(?![0-9])", r"unregistered\s+sales", "II"),
    _spec("Part II Item 3", "defaults", r"3(?![0-9])", r"defaults?\s+upon", "II"),
    _spec("Part II Item 4", "mine_safety", r"4(?![0-9])", r"mine\s+safety", "II"),
    _spec("Part II Item 5", "other_information", r"5(?![0-9])", r"other\s+information", "II"),
    _spec("Part II Item 6", "exhibits", r"6(?![0-9])", r"exhibits?", "II"),
)

# --- 20-F -------------------------------------------------------------------
# Top-level items only; Items 3 and 5 get sub-split afterwards (see
# FORM_20F_ITEM3_SUBITEMS / FORM_20F_ITEM5_SUBITEMS) because 3.D is the
# FPI risk-factors disclosure and Item 5 is the MD&A analog — the two sections
# this whole feature most needs for NU / NVO / WIX.

FORM_20F_ITEMS: tuple[ItemSpec, ...] = (
    _spec("Item 1", "identity_of_directors", r"1(?![0-9ab])", r"identity\s+of\s+directors"),
    _spec("Item 2", "offer_statistics", r"2(?![0-9])", r"offer\s+statistics"),
    _spec("Item 3", "key_information", r"3(?![0-9])", r"key\s+information"),
    _spec("Item 4", "company_information", r"4(?![0-9a])", r"information\s+on\s+the\s+company"),
    _spec("Item 4A", "unresolved_staff_comments", r"4\s*a", r"unresolved\s+staff"),
    _spec("Item 5", "mdna", r"5(?![0-9])", r"operating\s+and\s+financial\s+review"),
    _spec("Item 6", "directors_officers", r"6(?![0-9])", r"directors,?\s+senior\s+management"),
    _spec("Item 7", "major_shareholders", r"7(?![0-9])", r"major\s+shareholders"),
    _spec("Item 8", "financial_information", r"8(?![0-9])", r"financial\s+information"),
    _spec("Item 9", "offer_listing", r"9(?![0-9])", r"the\s+offer\s+and\s+listing"),
    _spec("Item 10", "additional_information", r"10(?![0-9])", r"additional\s+information"),
    _spec("Item 11", "market_risk", r"11(?![0-9])", r"quantitative\s+and\s+qualitative"),
    _spec("Item 12", "securities_description", r"12(?![0-9])", r"description\s+of\s+securities"),
    _spec("Item 13", "defaults", r"13(?![0-9])", r"defaults,?\s+dividend"),
    _spec("Item 14", "material_modifications", r"14(?![0-9])", r"material\s+modifications"),
    _spec("Item 15", "controls_procedures", r"15(?![0-9])", r"controls\s+and\s+procedures"),
    _spec("Item 16", "corporate_governance", r"16(?![0-9a-k])", r"\[?reserved\]?"),
    _spec("Item 16A", "audit_committee_expert", r"16\s*a", r"audit\s+committee\s+financial"),
    _spec("Item 16B", "code_of_ethics", r"16\s*b", r"code\s+of\s+ethics"),
    _spec("Item 16C", "accountant_fees", r"16\s*c", r"principal\s+account"),
    _spec("Item 16D", "listing_exemptions", r"16\s*d", r"exemptions\s+from"),
    _spec("Item 16E", "equity_purchases", r"16\s*e", r"purchases\s+of\s+equity"),
    _spec(
        "Item 16F", "auditor_changes", r"16\s*f", r"change\s+in\s+registrant.{0,3}s\s+certifying"
    ),
    _spec("Item 16G", "corporate_governance", r"16\s*g", r"corporate\s+governance"),
    _spec("Item 16H", "mine_safety", r"16\s*h", r"mine\s+safety"),
    _spec("Item 16I", "foreign_jurisdictions", r"16\s*i", r"disclosure\s+regarding\s+foreign"),
    _spec("Item 16J", "insider_trading_policy", r"16\s*j", r"insider\s+trading"),
    _spec("Item 16K", "cybersecurity", r"16\s*k", r"cybersecurity"),
    _spec("Item 17", "financial_statements", r"17(?![0-9])", r"financial\s+statements"),
    _spec("Item 18", "financial_statements", r"18(?![0-9])", r"financial\s+statements"),
    _spec("Item 19", "exhibits", r"19(?![0-9])", r"exhibits?"),
)


def _subitem(key: str, concept: str, letter: str, titles: str) -> ItemSpec:
    """Sub-item spec ("3.D", "5.B") — matched inside its parent's slice only.

    Real 20-Fs overwhelmingly write the sub-item as a BARE letter under the
    parent heading ("ITEM 3. KEY INFORMATION" … "D. Risk Factors"), not as
    "3.D". The parent number is therefore optional in the title pattern. That
    is safe because these patterns are only ever applied inside the parent
    item's own slice, and because the title keyword is required — a bare "D."
    alone never matches. The numbered form stays accepted for the filers that
    do use it.
    """
    number = key.split()[-1].split(".")[0]
    rx = re.compile(
        rf"(?:item\s*)?(?:{number}\s*\.?\s*)?{letter}\s*[.:\-–—)]?\s*(?:{titles})",  # noqa: RUF001 — EN/EM dash are real SEC item separators
        re.IGNORECASE,
    )
    bare = re.compile(
        rf"^[ \t]*(?:item\s*)?{number}\s*\.?\s*{letter}[ \t.:\-–—]*$",  # noqa: RUF001 — EN/EM dash are real SEC item separators
        re.IGNORECASE | re.MULTILINE,
    )
    return ItemSpec(key=key, concept=concept, title_rx=rx, bare_rx=bare)


FORM_20F_ITEM3_SUBITEMS: tuple[ItemSpec, ...] = (
    _subitem("Item 3.A", "selected_financial_data", "a", r"selected\s+financial"),
    _subitem("Item 3.B", "capitalization", "b", r"capitali[sz]ation"),
    _subitem("Item 3.C", "reasons_for_offer", "c", r"reasons\s+for\s+the\s+offer"),
    _subitem("Item 3.D", "risk_factors", "d", r"risk\s+factors?"),
)

FORM_20F_ITEM5_SUBITEMS: tuple[ItemSpec, ...] = (
    _subitem("Item 5.A", "operating_results", "a", r"operating\s+results"),
    _subitem("Item 5.B", "liquidity", "b", r"liquidity\s+and\s+capital"),
    _subitem("Item 5.C", "rd_patents", "c", r"research\s+and\s+development"),
    _subitem("Item 5.D", "trend_information", "d", r"trend\s+information"),
    _subitem("Item 5.E", "critical_accounting_estimates", "e", r"critical\s+accounting"),
)

#: 40-F (MJDS) incorporates its disclosure by reference to the Canadian AIF/MD&A
#: rather than numbering items the way the other forms do, so there is no item
#: taxonomy to apply. Callers get UNSUPPORTED_FORM coverage instead of a
#: fabricated partition.
ITEMS_BY_FORM: dict[FilingForm, tuple[ItemSpec, ...]] = {
    FilingForm.FORM_10K: FORM_10K_ITEMS,
    FilingForm.FORM_20F: FORM_20F_ITEMS,
}


@dataclass(slots=True)
class LocatedItem:
    """One item's resolved position in a document."""

    spec: ItemSpec
    header_start: int
    body_start: int
    header_text: str
    body_end: int = field(default=-1)


def _looks_like_toc_entry(text: str, line_end: int) -> bool:
    """True when the text just past a header line carries the TOC's page-number
    signature rather than a real section body.

    A real body header is followed by prose; a table-of-contents entry is
    followed by (at most) its own page number and then straight into the NEXT
    item's title and page number. Two consecutive REAL items that both happen
    to be one-line answers ("Not applicable." / "None.") each still contribute
    their own lone page-footer number, so a 2-number threshold isn't quite
    enough to rule that shape out (observed: AVGO's Item 3 "incorporated by
    reference" answer sits right before Item 4's "None.", each with its own
    footer number, two numbers total). Requiring THREE keeps that real,
    if terse, pair of items from being mistaken for a TOC run while still
    catching every genuine TOC cluster in this corpus, which always has more
    than two entries in play within the window.
    """
    lookahead = text[line_end : line_end + _TOC_LOOKAHEAD_CHARS]
    if len(_BARE_PAGE_NUMBER_RX.findall(lookahead)) < _MIN_TOC_PAGE_NUMBERS:
        return False
    return _ITEM_LINE_START_RX.search(lookahead) is not None


def _candidate_positions(text: str, spec: ItemSpec) -> list[tuple[int, int, str]]:
    """All plausible header positions for one item as (header_start, body_start, line).

    Filters out table-of-contents rows — both the dotted-leader shape
    ("Item 1A. Risk Factors....9") and the dotless shape used by filers whose
    HTML renders each TOC title and page number as its own block element
    ("Item 1A. Risk Factors" / "9" on separate lines, or "Item 1A. Risk
    Factors 9" on one line when a renderer keeps them together) — and long
    prose lines that merely mention the item.
    """
    out: list[tuple[int, int, str]] = []
    seen: set[int] = set()
    for rx in (spec.title_rx, spec.bare_rx):
        for m in rx.finditer(text):
            line_start = text.rfind("\n", 0, m.start()) + 1
            if line_start in seen:
                continue
            line_end = text.find("\n", m.start())
            if line_end == -1:
                line_end = len(text)
            line = text[line_start:line_end]
            stripped = line.strip()
            if _TOC_LEADER in stripped or len(stripped) > _MAX_HEADER_LINE_LEN:
                continue
            if _TRAILING_PAGE_NUMBER_RX.search(stripped):
                continue
            if _looks_like_toc_entry(text, line_end):
                continue
            seen.add(line_start)
            # The title match can itself wrap onto a second physical line (a
            # header rendered as "Item 4A. Unresolved" / "staff comments" from
            # an inline <br> or span break) — \s in the title pattern matches
            # that newline same as a plain space. When it does, m.end() lands
            # mid-word on the SECOND line, not at the end of the header's own
            # text, so the body must start at the end of whichever line
            # actually holds m.end() rather than at m.end() itself. Real case:
            # NU's 20-F wraps "Item 4A. Unresolved\nstaff comments" this way,
            # and taking m.end() literally cut the body open mid-word
            # ("comments\n\nNot applicable...") instead of after the header.
            body_start = text.find("\n", max(m.end(), line_start))
            if body_start == -1:
                body_start = len(text)
            out.append((line_start, max(body_start, line_end), stripped))
    out.sort(key=lambda t: t[0])
    return out


def locate_items(text: str, specs: tuple[ItemSpec, ...]) -> list[LocatedItem]:
    """Resolve each item to its body position, choosing the best header chain.

    Candidates from every item are pooled and a chain is selected by dynamic
    programming: a chain must visit items in taxonomy order at increasing
    positions, and scores as the sum of capped inter-header gaps plus a bonus
    per item located. Because a table of contents packs its entries a few
    hundred characters apart while a real body separates them by thousands,
    the body chain outscores the TOC chain without any positional guesswork —
    and a filing that genuinely omits items (a 10-Q's abbreviated Part II) just
    yields a shorter chain rather than mismatched slices.

    Returns items in document order with ``body_end`` filled in. Empty list
    when nothing matched, which callers must treat as ``NO_SECTIONS_FOUND``
    rather than an empty-but-valid partition.
    """
    pool: list[tuple[int, int, int, str]] = []  # (pos, spec_index, body_start, line)
    for idx, spec in enumerate(specs):
        for header_start, body_start, line in _candidate_positions(text, spec):
            pool.append((header_start, idx, body_start, line))
    if not pool:
        return []
    pool.sort(key=lambda t: (t[0], t[1]))

    n = len(pool)
    best_score = [float(_ITEM_BONUS)] * n
    prev: list[int | None] = [None] * n
    for i in range(n):
        pos_i, spec_i, _, _ = pool[i]
        for j in range(i):
            pos_j, spec_j, _, _ = pool[j]
            if spec_j >= spec_i or pos_j >= pos_i:
                continue
            gap = pos_i - pos_j
            contribution = float(min(gap, _GAP_CAP)) if gap >= _MIN_SECTION_CHARS else 0.0
            candidate = best_score[j] + contribution + _ITEM_BONUS
            if candidate > best_score[i]:
                best_score[i] = candidate
                prev[i] = j

    tail = max(range(n), key=lambda i: best_score[i])
    chain: list[int] = []
    cursor: int | None = tail
    while cursor is not None:
        chain.append(cursor)
        cursor = prev[cursor]
    chain.reverse()

    located: list[LocatedItem] = []
    for slot, i in enumerate(chain):
        pos, spec_idx, body_start, line = pool[i]
        end = pool[chain[slot + 1]][0] if slot + 1 < len(chain) else len(text)
        located.append(
            LocatedItem(
                spec=specs[spec_idx],
                header_start=pos,
                body_start=body_start,
                header_text=line,
                body_end=end,
            )
        )
    return located


def concept_for(form: FilingForm, item_key: str) -> str | None:
    """Look up the cross-form concept for a form-specific item key."""
    pools: list[tuple[ItemSpec, ...]] = [
        FORM_10Q_PART1_ITEMS,
        FORM_10Q_PART2_ITEMS,
        FORM_20F_ITEM3_SUBITEMS,
        FORM_20F_ITEM5_SUBITEMS,
    ]
    known = ITEMS_BY_FORM.get(form)
    if known is not None:
        pools.insert(0, known)
    for pool in pools:
        for spec in pool:
            if spec.key.lower() == item_key.lower():
                return spec.concept
    return None
