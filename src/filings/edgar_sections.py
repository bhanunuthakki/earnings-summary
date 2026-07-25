"""Partition EDGAR narrative text into sections, per form.

This is the ``edgar_text`` half of the store. It is the only source of MD&A and
Risk Factors in the platform — FMP's ``financial-reports-json`` product carries
statements and footnotes but no Item 1 / 1A / 7 narrative at all, so anything
tracking *reporting language* has to come through here.

Every splitter returns a ``SplitResult`` carrying both slices and
``warnings``. The warnings are load-bearing, not decoration: the ingest layer
turns a non-empty warning list into ``PARTIAL`` coverage with a reason code, so
"we split this 10-Q but never found the Part I/II boundary" is recorded as a
qualified success rather than passing as a clean one.

HTML stripping reuses ``filing_text_fetcher._strip_html`` rather than
reimplementing entity/tag handling — that function is already the platform's
answer for EDGAR HTML and its behavior (block tags to newlines, entity table,
whitespace collapse) is what the taxonomy's line-oriented header patterns are
tuned against. A second stripper would drift from it silently.
"""

from __future__ import annotations

import itertools
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from filing_text_fetcher import _strip_html
from filings.models import FilingForm
from filings.taxonomy import (
    FORM_10K_ITEMS,
    FORM_10Q_PART1_ITEMS,
    FORM_10Q_PART2_ITEMS,
    FORM_20F_ITEM3_SUBITEMS,
    FORM_20F_ITEM5_SUBITEMS,
    FORM_20F_ITEMS,
    ItemSpec,
    locate_items,
)

log = logging.getLogger(__name__)

EXTRACTOR_VERSION = "edgar_sections_v1"

#: A slice shorter than this is a header with no body — a false-positive match,
#: not a section. Dropped rather than persisted as an empty partition.
MIN_SECTION_CHARS = 120
#: Guards against a pathological single "section" swallowing a whole 4MB filing
#: because no downstream header matched. Truncation is recorded as a warning.
MAX_SECTION_CHARS = 800_000


@dataclass(slots=True)
class SectionSlice:
    """One partitioned section, pre-persistence."""

    key: str
    text: str
    ordinal: int
    concept: str | None = None
    title: str | None = None


@dataclass(slots=True)
class SplitResult:
    slices: list[SectionSlice] = field(default_factory=list[SectionSlice])
    warnings: list[str] = field(default_factory=list[str])

    @property
    def ok(self) -> bool:
        return bool(self.slices)


_PART1_SPEC = ItemSpec(
    key="Part I",
    concept="part_i",
    title_rx=re.compile(r"part\s+i(?![ivx])\s*[.:\-–—]?\s*financial\s+information", re.IGNORECASE),  # noqa: RUF001 — EN/EM dash are real SEC separators
    bare_rx=re.compile(r"^[ \t]*part\s+i(?![ivx])[ \t.:\-–—]*$", re.IGNORECASE | re.MULTILINE),  # noqa: RUF001 — EN/EM dash are real SEC separators
)
_PART2_SPEC = ItemSpec(
    key="Part II",
    concept="part_ii",
    title_rx=re.compile(r"part\s+ii(?![ivx])\s*[.:\-–—]?\s*other\s+information", re.IGNORECASE),  # noqa: RUF001 — EN/EM dash are real SEC separators
    bare_rx=re.compile(r"^[ \t]*part\s+ii(?![ivx])[ \t.:\-–—]*$", re.IGNORECASE | re.MULTILINE),  # noqa: RUF001 — EN/EM dash are real SEC separators
)


def _slices_from_items(
    text: str, specs: tuple[ItemSpec, ...], start_ordinal: int = 0
) -> list[SectionSlice]:
    """Apply a taxonomy to text and materialize non-empty slices."""
    out: list[SectionSlice] = []
    for located in locate_items(text, specs):
        body = text[located.body_start : located.body_end].strip()
        if len(body) < MIN_SECTION_CHARS:
            continue
        out.append(
            SectionSlice(
                key=located.spec.key,
                text=body[:MAX_SECTION_CHARS],
                ordinal=start_ordinal + len(out),
                concept=located.spec.concept,
                title=located.header_text,
            )
        )
    return out


#: A sub-split must account for at least this share of its parent's text to
#: replace it. Below the threshold the located sub-items are an unreliable read
#: of the parent's structure and the parent is kept whole (with a warning).
_SUBSPLIT_MIN_COVERAGE = 0.35


def _subsplit(
    body: str, subspecs: tuple[ItemSpec, ...], *, start_ordinal: int
) -> tuple[list[SectionSlice], str]:
    """Try to sub-split a parent item; return (sub-slices, preamble).

    Returns ``([], "")`` when the sub-items explain too little of the parent to
    be trusted. A single located sub-item IS enough when it covers most of the
    parent — which is the common real shape: a 20-F's Item 3 sub-items 3.A/3.B
    are frequently one line of "Not applicable" while 3.D Risk Factors runs for
    two hundred kilobytes. Requiring two surviving sub-items (an earlier,
    tighter rule) silently dropped every FPI risk-factors section on the book.
    """
    located = locate_items(body, subspecs)
    if not located:
        return [], ""
    slices: list[SectionSlice] = []
    for item in located:
        chunk = body[item.body_start : item.body_end].strip()
        if len(chunk) < MIN_SECTION_CHARS:
            continue
        slices.append(
            SectionSlice(
                key=item.spec.key,
                text=chunk[:MAX_SECTION_CHARS],
                ordinal=start_ordinal + len(slices),
                concept=item.spec.concept,
                title=item.header_text,
            )
        )
    if not slices:
        return [], ""
    covered = sum(len(s.text) for s in slices)
    if covered < len(body) * _SUBSPLIT_MIN_COVERAGE:
        return [], ""
    preamble = body[: located[0].header_start].strip()
    return slices, preamble


#: A real section header sits on a short line; anything longer is prose that
#: merely mentions the item ("as described in Item 4 below").
_BARE_HEADER_MAX_LEN = 80
#: How many slices may contain their OWN SUCCESSOR's header before the
#: partition is called shifted. One can happen innocently (a running page
#: header caught at a boundary); two in a document is the off-by-one signature.
_MAX_SUCCESSOR_LEAKS = 1


def _contains_bare_header(body: str, spec: ItemSpec) -> bool:
    """True when ``spec``'s header appears in ``body`` as a standalone line."""
    match = spec.bare_rx.search(body) or spec.title_rx.search(body)
    if match is None:
        return False
    line_start = body.rfind("\n", 0, match.start()) + 1
    line_end = body.find("\n", match.start())
    line = body[line_start : line_end if line_end != -1 else len(body)].strip()
    return len(line) <= _BARE_HEADER_MAX_LEN


def _boundaries_look_suspect(slices: list[SectionSlice], specs: tuple[ItemSpec, ...]) -> bool:
    """True when the slices are shifted relative to the real item boundaries.

    A correctly-cut partition holds each item's prose and NOT the next item's
    header — that header IS the boundary. So the tell for an off-by-one chain
    is specific: slice *i* contains the bare header of slice *i+1*. Counting
    foreign headers generally is too blunt (a backward cross-reference is
    normal and a shifted partition may only skew its first few slices); the
    successor relationship is what actually distinguishes a shift.

    Detecting it does not fix it, and that is the point. The alternative is a
    partition that looks clean while attributing one item's disclosure to
    another — far worse for a language diff than a flagged gap. Real case:
    NVO's thin incorporate-by-reference 20-F repeats "ITEM 3 KEY INFORMATION",
    and its Item 1, 2 and 3 slices each swallowed the following item's heading.
    """
    if len(slices) < 3:
        return False
    by_key = {spec.key: spec for spec in specs}
    leaks = 0
    for current, following in itertools.pairwise(slices):
        spec = by_key.get(following.key)
        if spec is not None and _contains_bare_header(current.text, spec):
            leaks += 1
    return leaks > _MAX_SUCCESSOR_LEAKS


#: Concepts that are near-universally a one-line "Not applicable." / "None."
#: answer across the whole book. A slice filed under one of these labels that
#: runs to thousands of characters did not suddenly gain real disclosure — a
#: boundary swallowed a NEIGHBORING item's prose instead.
_TRIVIAL_CONCEPTS = frozenset({"unresolved_staff_comments", "mine_safety", "offer_statistics"})
#: Calibrated against the real corpus (``data/sec_text/``): genuine mine-safety
#: prose (FCX, NSP, DHR, NVO all actually operate mines) tops out around 5,400
#: chars; the smallest CONFIRMED-corrupted case (NVO's unresolved_staff_comments,
#: a boundary artifact unrelated to the TOC fix below) is 6,231, and NU's and
#: FCX's worst TOC-latched cases ran 87K-298K. 6,000 sits in that gap.
_TRIVIAL_SECTION_MAX_CHARS = 6_000


def _trivial_section_oversized(slices: list[SectionSlice]) -> bool:
    """True when a near-always-trivial concept's slice is implausibly large.

    Unlike ``_boundaries_look_suspect`` (which needs the successor's own
    header text to still be present in the prior slice), this catches the
    shape where the swallowed content came from an item the chain skipped
    over entirely — NU's 20-F reorders Items 1/2/3/4/4A in its body relative
    to their canonical numbering, so Item 4A's real "Not applicable." answer
    ends up bundled with Item 3's full risk-factors disclosure with no leaked
    header text to detect, because Item 3 was never a chain node to begin
    with. Detecting it does not fix the cut; it is the same trade as
    ``_boundaries_look_suspect`` — a flagged gap beats a clean-looking
    mislabel.
    """
    return any(
        s.concept in _TRIVIAL_CONCEPTS and len(s.text) > _TRIVIAL_SECTION_MAX_CHARS for s in slices
    )


#: A leading legal-drafting enumerator ("(a)", "(b)(1)", "1.", "A.") is a
#: normal, correct way for a real section to open — it is not evidence of a
#: cut landing mid-sentence, so it is stripped before the case of the first
#: real word is inspected.
_ENUMERATOR_PREFIX_RX = re.compile(r"^\(?[a-zA-Z0-9]{1,3}\)?[.:)]\s*")
#: Share of a filing's OWN slices that may legitimately open on a lowercase
#: letter (a genuine cross-reference like "10b5-1 Trading Plans...") before
#: the pattern stops looking like noise and starts looking like a partition
#: whose cuts are landing inside sentences. Calibrated against the corpus:
#: every clean filing's worst case is 1 slice out of >=15 (<=7%); NU's and
#: NVO's shifted partitions run 15-27%.
_MID_SENTENCE_SHARE = 0.15
#: A single mid-sentence slice happens even in clean filings; only a genuine
#: SHARE (not one occurrence) indicates the partition itself is the problem.
_MIN_MID_SENTENCE_SLICES = 2


def _starts_mid_sentence(text: str) -> bool:
    """True when a slice's body opens on a lowercase letter.

    A real header hands off to a capitalized sentence or an enumerated list
    item ("(a) Evaluation of disclosure controls..."); stripping ordinary
    enumerators first is what keeps those common, legitimate openings from
    registering as false positives here.
    """
    body = text.lstrip()
    for _ in range(3):  # a chained enumerator like "(a) 1." needs 2 passes
        stripped = _ENUMERATOR_PREFIX_RX.sub("", body, count=1)
        if stripped == body:
            break
        body = stripped.lstrip()
    first_alpha = next((c for c in body if c.isalpha()), None)
    return bool(first_alpha and first_alpha.islower())


def _mid_sentence_share_suspect(slices: list[SectionSlice]) -> bool:
    """True when enough of ONE filing's slices open mid-sentence that the
    partition — not any single section — is what needs flagging.

    Real case: NU's 20-F, after the TOC-rejection fix, still lands several
    cuts inside a sentence because its item order in the body does not match
    the canonical numbering the chain has to assume (see
    ``_trivial_section_oversized``), so multiple slices in the SAME filing
    show the symptom at once. A share threshold is what lets this catch that
    filing-wide pattern without firing on the rare, legitimate lowercase
    cross-reference any single clean filing can have.
    """
    if not slices:
        return False
    count = sum(1 for s in slices if _starts_mid_sentence(s.text))
    return count >= _MIN_MID_SENTENCE_SLICES and count / len(slices) >= _MID_SENTENCE_SHARE


def _integrity_warnings(slices: list[SectionSlice]) -> list[str]:
    """Post-split checks shared by every taxonomy-based splitter (10-K, 10-Q,
    20-F) — the safety net for boundary defects ``_boundaries_look_suspect``
    cannot see because the swallowed item was never a chain node at all."""
    warnings: list[str] = []
    if _trivial_section_oversized(slices):
        warnings.append("trivial_section_oversized")
    if _mid_sentence_share_suspect(slices):
        warnings.append("slice_starts_mid_sentence")
    return warnings


def split_10k(text: str) -> SplitResult:
    """Partition a 10-K into its Items 1–16."""
    slices = _slices_from_items(text, FORM_10K_ITEMS)
    warnings: list[str] = []
    if not slices:
        return SplitResult([], ["no_item_headers_located"])
    if _boundaries_look_suspect(slices, FORM_10K_ITEMS):
        warnings.append("slice_boundaries_suspect")
    warnings.extend(_integrity_warnings(slices))
    concepts = {s.concept for s in slices}
    # Item 1A and Item 7 are the whole point of the narrative partition; a
    # filing that yields sections but not these two is a partition worth
    # flagging, because a downstream language diff would silently have nothing
    # to compare.
    for required in ("risk_factors", "mdna"):
        if required not in concepts:
            warnings.append(f"missing_{required}")
    return SplitResult(slices, warnings)


def split_10q(text: str) -> SplitResult:
    """Partition a 10-Q, respecting the Part I / Part II boundary.

    A 10-Q reuses item numbers across parts ("Item 1" is Financial Statements
    in Part I and Legal Proceedings in Part II), so the parts are resolved
    first and each part's taxonomy applied only within its own slice. When the
    boundary cannot be located, Part I's taxonomy is applied to the whole
    document and a ``part_boundary_unresolved`` warning downgrades the period
    to PARTIAL coverage — mislabeling Part II's Legal Proceedings as Part I's
    Financial Statements would be worse than an incomplete partition.
    """
    parts = locate_items(text, (_PART1_SPEC, _PART2_SPEC))
    by_key = {p.spec.key: p for p in parts}
    part1, part2 = by_key.get("Part I"), by_key.get("Part II")

    if part1 is None or part2 is None:
        slices = _slices_from_items(text, FORM_10Q_PART1_ITEMS)
        if not slices:
            return SplitResult([], ["no_item_headers_located", "part_boundary_unresolved"])
        return SplitResult(slices, ["part_boundary_unresolved"])

    part1_text = text[part1.body_start : part1.body_end]
    part2_text = text[part2.body_start : part2.body_end]
    slices = _slices_from_items(part1_text, FORM_10Q_PART1_ITEMS)
    slices.extend(_slices_from_items(part2_text, FORM_10Q_PART2_ITEMS, start_ordinal=len(slices)))
    if not slices:
        return SplitResult([], ["no_item_headers_located"])

    warnings: list[str] = _integrity_warnings(slices)
    if "mdna" not in {s.concept for s in slices}:
        warnings.append("missing_mdna")
    return SplitResult(slices, warnings)


def split_20f(text: str) -> SplitResult:
    """Partition a 20-F into Items 1–19, sub-splitting Items 3 and 5.

    Items 3 and 5 carry the two disclosures this feature most needs from a
    foreign private issuer: 3.D is the FPI risk-factors section (the 10-K Item
    1A analog) and Item 5 is the MD&A analog. When a parent yields two or more
    sub-items, the sub-items REPLACE the parent rather than being stored
    alongside it — storing both would double-count the same prose in every
    downstream diff and inflate any change metric computed over the store.
    """
    # A thin incorporate-by-reference 20-F (NVO) can reprint an item's own
    # title as a running page header well inside that item's real body — see
    # ``taxonomy._drop_running_header_repeats``. FORM_20F_ITEMS is large
    # enough (29 items) for ``_drop_contents_block`` to have already resolved
    # any genuine contents page, so a surviving duplicate at this point is
    # safe to treat as the running-header defect rather than a second TOC.
    top = locate_items(text, FORM_20F_ITEMS, drop_running_header_repeats=True)
    if not top:
        return SplitResult([], ["no_item_headers_located"])

    slices: list[SectionSlice] = []
    warnings: list[str] = []
    for located in top:
        body = text[located.body_start : located.body_end].strip()
        if len(body) < MIN_SECTION_CHARS:
            continue
        subspecs: tuple[ItemSpec, ...] | None = None
        if located.spec.key == "Item 3":
            subspecs = FORM_20F_ITEM3_SUBITEMS
        elif located.spec.key == "Item 5":
            subspecs = FORM_20F_ITEM5_SUBITEMS

        if subspecs is not None:
            sub_slices, preamble = _subsplit(body, subspecs, start_ordinal=len(slices))
            if sub_slices:
                # The preamble (everything before the first located sub-item)
                # is emitted separately rather than dropped: sub-items rarely
                # start at character zero, and silently discarding the lead-in
                # would lose real disclosure. It carries the parent's concept
                # and does not overlap any sub-item, so nothing is double-counted.
                if len(preamble) >= MIN_SECTION_CHARS:
                    slices.append(
                        SectionSlice(
                            key=f"{located.spec.key} (preamble)",
                            text=preamble[:MAX_SECTION_CHARS],
                            ordinal=len(slices),
                            concept=located.spec.concept,
                            title=located.header_text,
                        )
                    )
                    for offset, sub in enumerate(sub_slices):
                        sub.ordinal = len(slices) + offset
                slices.extend(sub_slices)
                continue
            warnings.append(f"subsplit_incomplete_{located.spec.key.replace(' ', '_').lower()}")

        slices.append(
            SectionSlice(
                key=located.spec.key,
                text=body[:MAX_SECTION_CHARS],
                ordinal=len(slices),
                concept=located.spec.concept,
                title=located.header_text,
            )
        )

    if not slices:
        return SplitResult([], ["no_item_headers_located"])
    if _boundaries_look_suspect(slices, FORM_20F_ITEMS):
        warnings.append("slice_boundaries_suspect")
    warnings.extend(_integrity_warnings(slices))
    if "risk_factors" not in {s.concept for s in slices}:
        warnings.append("missing_risk_factors")
    return SplitResult(slices, warnings)


_DATE_ONLY_RX = re.compile(r"^[\d\W]+$")


def _looks_like_heading(line: str) -> bool:
    """Heuristic header test for free-form exhibit text.

    Mirrors ``filing_text_fetcher._looks_like_risk_heading``'s shape (short,
    capitalized, header-ish) with two additions for 6-K exhibits: pure
    number/punctuation lines (table rows, page numbers, dates) are rejected,
    and ALL-CAPS lines are accepted outright since press-release exhibits
    headline that way.
    """
    stripped = line.strip()
    if not (4 < len(stripped) < 160):
        return False
    if _DATE_ONLY_RX.match(stripped):
        return False
    if stripped.endswith((",", ";", ":")) or stripped.endswith(".."):
        return False
    if not stripped[0].isalpha() or not stripped[0].isupper():
        return False
    words = stripped.split()
    if not (1 < len(words) <= 20):
        return False
    letters = [c for c in stripped if c.isalpha()]
    if letters and all(c.isupper() for c in letters):
        return True
    capitalized = sum(1 for w in words if w[:1].isupper())
    return capitalized >= len(words) * 0.6


def split_freeform(text: str, *, label: str = "Exhibit") -> SplitResult:
    """Partition free-form text (a 6-K exhibit) by detected headings.

    6-K has no mandated item structure, so no taxonomy is applied and every
    slice carries ``concept=None`` — an honest "we found headings" rather than
    a fabricated mapping onto 10-K items. When no headings are detected the
    whole document is returned as ONE section (with a warning) instead of
    nothing: a whole-exhibit slice still diffs usefully period over period,
    whereas an empty result would look like a missing filing.
    """
    body = text.strip()
    if not body:
        return SplitResult([], ["empty_text"])

    lines = body.splitlines()
    sections: list[tuple[str, list[str]]] = []
    heading: str | None = None
    buffer: list[str] = []
    for line in lines:
        if _looks_like_heading(line):
            if heading is not None and buffer:
                sections.append((heading, buffer))
            heading = line.strip()
            buffer = []
        elif heading is not None:
            buffer.append(line)
    if heading is not None and buffer:
        sections.append((heading, buffer))

    slices: list[SectionSlice] = []
    for head, body_lines in sections:
        chunk = "\n".join(body_lines).strip()
        if len(chunk) < MIN_SECTION_CHARS:
            continue
        slices.append(
            SectionSlice(
                key=head[:255],
                text=chunk[:MAX_SECTION_CHARS],
                ordinal=len(slices),
                concept=None,
                title=head,
            )
        )

    if not slices:
        return SplitResult(
            [
                SectionSlice(
                    key=f"{label} (full document)",
                    text=body[:MAX_SECTION_CHARS],
                    ordinal=0,
                    concept=None,
                    title=label,
                )
            ],
            ["no_headings_detected"],
        )
    return SplitResult(slices, [])


def split_document(form: FilingForm, text: str, *, is_html: bool = False) -> SplitResult:
    """Dispatch to the right splitter for ``form``.

    ``40-F`` returns empty with ``unsupported_form``: MJDS filers incorporate
    their disclosure by reference to Canadian AIF/MD&A documents instead of
    numbering items, so there is nothing to partition here and the caller must
    record UNSUPPORTED_FORM rather than an empty success.
    """
    body = _strip_html(text) if is_html else text
    if not body.strip():
        return SplitResult([], ["empty_text"])
    if form is FilingForm.FORM_10K or form is FilingForm.FORM_S1:
        return split_10k(body)
    if form is FilingForm.FORM_10Q:
        return split_10q(body)
    if form is FilingForm.FORM_20F:
        return split_20f(body)
    if form is FilingForm.FORM_6K:
        return split_freeform(body)
    return SplitResult([], ["unsupported_form"])
