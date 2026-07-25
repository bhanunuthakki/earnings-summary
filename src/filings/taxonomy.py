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

3. **A filing states its item structure more than once, and only one of them
   is the disclosure.** The contents page lists every item; the body prints
   them; the prose then cites them by name for hundreds of paragraphs. A naive
   "first match wins" or "last match wins" both fail. So candidates are filtered
   before they are scored — ``_is_citation`` drops in-sentence references and
   ``_drop_contents_block`` drops the contents listing — and only then does
   ``locate_items`` score whole *chains* by how much text sits between them.

   Chain scoring alone was tried first and is not sufficient, which is worth
   recording because it looked sufficient: contents entries are packed a few
   hundred characters apart so they earn no gap, but they DO earn the per-item
   bonus, so the winning chain simply took the contents page AND the body. The
   damage was invisible in the output (contents rows slice to nothing) and
   severe in effect — the item numbers were spent, so real body items became
   unreachable and their disclosure was absorbed by a neighbour.

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
#: hence its own line, which ``_drop_contents_block`` handles structurally)
#: but cheap to guard against for a renderer that keeps them on one line.
_TRAILING_PAGE_NUMBER_RX = re.compile(r"\s+\d{1,4}$")
#: Gap contributions are capped so one enormous section can't outweigh a chain
#: that locates many well-separated headers.
_GAP_CAP = 20_000
#: Per-located-item bonus: prefer a chain that finds more items, all else equal.
_ITEM_BONUS = 1_500
#: Below this, two headers are adjacent (TOC-like) and contribute no gap score.
_MIN_SECTION_CHARS = 240
#: Longest structural lead-in tolerated before a header on its own line
#: ("PART I", a page number, a table pipe). Longer means prose.
_MAX_HEADER_PREFIX_LEN = 40
#: An item reference opening with one of these is a citation ("see \"Item 5.
#: Operating and Financial Review\""), never a heading.
_QUOTE_OPENERS = "\"'“‘(«"  # noqa: RUF001 — curly quotes are what _strip_html emits
#: A contents block lists essentially the whole form. Both bars must clear:
#: the absolute one stops a four-item cluster of "Not applicable." bodies from
#: reading as a TOC, the relative one keeps the test meaningful for the forms
#: with fewer items. The same absolute bar doubles as the "is there a body
#: after this block?" test — see ``_drop_contents_block``.
_TOC_MIN_ITEMS = 8
_TOC_MIN_SHARE = 0.6


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
    # A filer may answer two adjacent items under ONE heading, and writes it as
    # a plural with a bridge: FCX's 10-K heads its MD&A "Items 7. and 7A.
    # Management's Discussion and Analysis…" and its business section "Items 1.
    # and 2. Business and Properties". Without the plural and the bridge, the
    # title keyword is unreachable past the second item number and the heading
    # matches nothing — FCX had no `mdna` section at all for this reason. The
    # bridge is optional and bounded to one item designator, so an ordinary
    # "Item 7. Management's Discussion" is unaffected.
    bridge = r"(?:and\s+\d{1,2}\s*[a-c]?\s*[.:\-–—]?\s*)?"  # noqa: RUF001 — EN/EM dash are real SEC item separators
    sep = rf"\s*[.:\-–—]?\s*{bridge}"  # noqa: RUF001 — EN/EM dash are real SEC item separators
    title = re.compile(
        rf"items?\s*{number}{sep}(?:{title_alternatives})",
        re.IGNORECASE,
    )
    bare = re.compile(rf"^[ \t]*items?\s*{number}[ \t.:\-–—]*$", re.IGNORECASE | re.MULTILINE)  # noqa: RUF001 — EN/EM dash are real SEC separators
    return title, bare


def _spec(key: str, concept: str, number: str, titles: str, part: str | None = None) -> ItemSpec:
    title_rx, bare_rx = _item_patterns(number, titles)
    return ItemSpec(key=key, concept=concept, title_rx=title_rx, bare_rx=bare_rx, part=part)


#: Filers write the market-risk title in BOTH word orders — ServiceNow's 10-K
#: says "Qualitative and Quantitative Disclosures About Market Risk". Accepting
#: only the SEC's order left Item 7A unmatched, and an unmatched Item 7A is not
#: a missing section: it removes the boundary, so Item 7 runs on through 7A and
#: 8 and MD&A ends up holding the financial-statement footnotes.
_MARKET_RISK_TITLES = r"(?:quantitative|qualitative)\s+and\s+(?:qualitative|quantitative)"
#: Likewise "Consolidated Financial Statements and Supplementary Data" — the
#: qualifier is optional, the noun is not.
_FINANCIAL_STATEMENTS_TITLES = r"(?:consolidated\s+)?financial\s+statements"


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
    _spec("Item 7A", "market_risk", r"7\s*a", _MARKET_RISK_TITLES),
    _spec("Item 8", "financial_statements", r"8(?![0-9])", _FINANCIAL_STATEMENTS_TITLES),
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
    _spec(
        "Part I Item 1", "financial_statements", r"1(?![0-9a])", _FINANCIAL_STATEMENTS_TITLES, "I"
    ),
    _spec("Part I Item 2", "mdna", r"2(?![0-9])", r"management.{0,3}s\s+discussion", "I"),
    _spec("Part I Item 3", "market_risk", r"3(?![0-9])", _MARKET_RISK_TITLES, "I"),
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
    _spec("Item 11", "market_risk", r"11(?![0-9])", _MARKET_RISK_TITLES),
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
    _spec("Item 17", "financial_statements", r"17(?![0-9])", _FINANCIAL_STATEMENTS_TITLES),
    _spec("Item 18", "financial_statements", r"18(?![0-9])", _FINANCIAL_STATEMENTS_TITLES),
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

    Which letter is written does NOT have to be the SEC's. NU's 20-F prints its
    risk factors under "A.Risk Factors" while its own table of contents and
    every cross-reference in the document call that section 3.D; keying on the
    letter found no risk-factors sub-item at all and left Item 3 whole — 240KB
    of risk factors plus everything the missing boundary let in. So the title
    keyword identifies the disclosure and the letter only has to be A LETTER,
    standing alone as its own token. Requiring one still matters: dropping it
    entirely makes ``risk\\s+factors?`` match the opening words of an ordinary
    sentence ("Risk factor disclosure prose…"), turning body text into rival
    headers. ``key``/``concept`` keep recording the letter the SEC assigns, so
    the cross-form timeline is unaffected by what the filer chose to print.
    """
    number = key.split()[-1].split(".")[0]
    rx = re.compile(
        rf"(?:item\s*)?(?:{number}\s*\.?\s*)?(?<![a-z])[a-k](?![a-z])\s*[.:\-–—)]?\s*(?:{titles})",  # noqa: RUF001 — EN/EM dash are real SEC item separators
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


def _is_citation(text: str, line_start: int, match_start: int) -> bool:
    """True when the item reference at ``match_start`` is prose, not a heading.

    A heading owns its line. A cross-reference is embedded in a sentence, and
    the two are told apart by what sits to the LEFT of the reference on the same
    line — the one place the difference always shows:

    * lowercase words before it ("as described in Item 4 below") — prose;
    * an opening quote immediately before it ("Item 5. Operating…") — a
      citation, even when the quote starts the line, which is the shape a
      hard-wrapped filing produces constantly;
    * a long lead-in — prose that happens to avoid both tells.

    A structural lead-in ("PART I", a page number, a table pipe) is none of
    those and passes. Without this test a filing whose stripped text wraps at
    ~135 chars — NU's 20-F does — offers HUNDREDS of in-prose citations as
    candidate headers, all of them under the line-length limit, and the chain
    scorer happily builds a partition out of them.
    """
    prefix = text[line_start:match_start]
    if prefix.rstrip().endswith(tuple(_QUOTE_OPENERS)):
        return True
    stripped = prefix.strip()
    if not stripped:
        return False
    return len(stripped) > _MAX_HEADER_PREFIX_LEN or any(c.islower() for c in stripped)


def _candidate_positions(text: str, spec: ItemSpec) -> list[tuple[int, int, str]]:
    """All plausible header positions for one item as (header_start, body_start, line).

    Filters out three things that match an item pattern without being a
    header: table-of-contents rows in the dotted-leader shape ("Item 1A.
    Risk Factors....9") or with a trailing page number ("Item 1A. Risk
    Factors 9"); long prose lines that merely mention the item; and
    in-sentence cross-references (``_is_citation``). The dotless contents
    shape — each title and page number in its own block element, so each on
    its own line — is NOT handled here; it is a property of the block, not
    of any one row, and ``_drop_contents_block`` removes it as a region.
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
            if _is_citation(text, line_start, m.start()):
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


def _drop_contents_block(pool: list[tuple[int, int, int, str]]) -> list[tuple[int, int, int, str]]:
    """Remove the table-of-contents entries from a candidate pool.

    A TOC writes the form's item structure a SECOND time, and chain scoring
    cannot arbitrate between two copies of the same structure — it takes both.
    Contents entries are packed a few hundred characters apart, so each one
    earns its ``_ITEM_BONUS`` while contributing no gap: prefixing the real body
    chain with the entire TOC is free profit, and the winning chain on NU's 20-F
    did exactly that, walking all 33 contents rows before reaching page one.
    The damage was not the junk rows (they slice to nothing) but the item
    numbers they consumed — with Items 1, 2 and 3 already spent on the TOC, the
    body's Item 3 was unreachable and NU's entire risk-factors disclosure was
    absorbed into the neighbouring slice.

    The block is identified by a density no body achieves — nearly every item
    in the taxonomy inside a few thousand characters — rather than by position,
    because a long front matter can push the contents a quarter of the way into
    the document. When no run qualifies (a filing with no TOC) the pool is
    returned unchanged, and when removing the run would leave almost nothing
    the pool is also returned unchanged: a thin 20-F whose only item headers
    ARE that dense block is better partitioned badly than not at all.

    **A contents block must be removed whole, or not at all.** #1009 added a
    per-row test (``_looks_like_toc_entry``: ≥3 bare page-number lines plus an
    item title starting a line, within 400 chars of lookahead) that was correct
    row by row and still net-negative here, because a lookahead cannot see the
    block's TAIL — its last rows have no following entry to supply the
    signature. On NU it stripped 29 of 32 contents rows and left the final two,
    which is strictly worse than leaving all 32: the density that identifies the
    block was gone, so this function no longer fired, and the survivors went on
    to consume item numbers exactly as before. Measured over the 125 cached
    documents, adding it back drops risk-factors coverage 116 → 113 (all three
    NU years) and improves nothing, so it was removed when the two changes were
    reconciled. The trailing-page-number row test it also added is orthogonal
    — it rejects one row on its own evidence, without thinning a block — and is
    kept above.
    """
    if not pool:
        return pool
    total_distinct = len({spec_idx for _, spec_idx, _, _ in pool})
    threshold = max(_TOC_MIN_ITEMS, int(total_distinct * _TOC_MIN_SHARE))

    window = _densest_window(pool, threshold)
    if window is None:
        return pool
    lo, hi = window  # inclusive indices
    if (pool[hi][0] - pool[lo][0]) / max(hi - lo, 1) >= _MIN_SECTION_CHARS:
        # Spread this thin, the "block" is just the document listing its items
        # once, in the body. There is no second copy to drop.
        return pool

    # The block runs from the first candidate through the end of that window.
    # Growing outward from the window by spacing was tried and is not sound:
    # a contents page's own spacing is uneven (NU's Item 16A-16K rows sit 83
    # chars apart, its Item 1-10 rows 130-359), so a reach calibrated on the
    # densest part stops short of the rest and leaves exactly the rows that do
    # the damage. Anchoring at the start of the pool needs no tolerance at all
    # and is what the document guarantees: a contents page precedes the body it
    # points at, so nothing before the listing is a body header either.
    after_block = {pool[i][1] for i in range(hi + 1, len(pool))}
    if len(after_block) < _TOC_MIN_ITEMS:
        # Nothing substantial follows, so this dense listing is not a contents
        # page with a body after it — it is the body, or an exhibit index at the
        # end of the filing. Excising it would delete the disclosure itself.
        return pool

    # Never delete an item's ONLY evidence: an item is cleared out of the block
    # only when it still has a candidate AFTER it. Otherwise its last in-block
    # candidate survives — last, not first, because a contents row always
    # precedes the body header it points at, so if both fell inside the block
    # the later one is the real heading.
    last_in_block: dict[int, int] = {}
    for i in range(hi + 1):
        last_in_block[pool[i][1]] = i
    return [
        row
        for i, row in enumerate(pool)
        if i > hi or (row[1] not in after_block and last_in_block[row[1]] == i)
    ]


def _densest_window(
    pool: list[tuple[int, int, int, str]], threshold: int
) -> tuple[int, int] | None:
    """Narrowest span of ``pool`` holding ``threshold`` distinct items, inclusive.

    The anchor for contents detection: whatever else a document contains, the
    place where most of the form's items appear in the fewest characters is its
    contents page. Returns None when no span reaches the threshold.
    """
    counts: dict[int, int] = {}
    best: tuple[int, int, int] | None = None  # (span, lo, hi)
    lo = 0
    for hi, (_, spec_idx, _, _) in enumerate(pool):
        counts[spec_idx] = counts.get(spec_idx, 0) + 1
        while len(counts) >= threshold:
            span = pool[hi][0] - pool[lo][0]
            if best is None or span < best[0]:
                best = (span, lo, hi)
            head = pool[lo][1]
            counts[head] -= 1
            if not counts[head]:
                del counts[head]
            lo += 1
    return None if best is None else (best[1], best[2])


def _best_chain(pool: list[tuple[int, int, int, str]], *, allow_reorder: bool) -> list[int]:
    """Highest-scoring chain of candidate indices, as described in ``locate_items``.

    ``allow_reorder=False`` forbids backward taxonomy steps, which makes the
    chain a strictly increasing subsequence of item indices and therefore unique
    by construction — the fallback for when the unordered chain repeats an item.
    """
    n = len(pool)
    best_score = [float(_ITEM_BONUS)] * n
    prev: list[int | None] = [None] * n
    for i in range(n):
        pos_i, spec_i, _, _ = pool[i]
        for j in range(i):
            pos_j, spec_j, _, _ = pool[j]
            if pos_j >= pos_i:
                continue
            if spec_j == spec_i or (not allow_reorder and spec_j > spec_i):
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
    return chain


def locate_items(text: str, specs: tuple[ItemSpec, ...]) -> list[LocatedItem]:
    """Resolve each item to its body position, choosing the best header chain.

    Candidates from every item are pooled and a chain is selected by dynamic
    programming: a chain visits each item at most once at strictly increasing
    positions, and scores as the sum of capped inter-header gaps plus a bonus
    per item located. Because a table of contents packs its entries a few
    hundred characters apart while a real body separates them by thousands,
    the body chain outscores the TOC chain without any positional guesswork —
    and a filing that genuinely omits items (a 10-Q's abbreviated Part II) just
    yields a shorter chain rather than mismatched slices.

    Taxonomy order is deliberately NOT scored. Filers reorder items — NU's 20-F
    prints Item 4, then Item 4A, then Item 3 — and requiring taxonomy order did
    not mislabel those items so much as swallow them: with Items 1–3 unreachable
    once Item 4 was taken, NU's entire risk-factors disclosure was absorbed into
    the Item 4A slice. Scoring order as evidence was measured across the 125
    cached filings and changed the outcome for exactly one issuer — NU, for the
    worse — because what actually separates a real header from a false one is
    ``_is_citation`` and ``_drop_contents_block``, not the order it appears in.
    What order still buys is uniqueness, and that is enforced directly below.

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
    pool = _drop_contents_block(pool)

    chain = _best_chain(pool, allow_reorder=True)
    # An unordered chain can return to an item it already used (3 → 7 → 3),
    # which would emit that item TWICE and hand the store two rival slices under
    # one key. Uniqueness cannot be expressed as a constraint on this DP, so it
    # is verified afterwards and a violating chain is rejected outright for the
    # ordered one, which is unique by construction. This is a live path, not a
    # formality: across the cached corpus the unordered chain repeats an item on
    # DASH, NVO, RGEN and VEEV, and is a clean permutation only on NU.
    used = [pool[i][1] for i in chain]
    if len(set(used)) != len(used):
        chain = _best_chain(pool, allow_reorder=False)

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
