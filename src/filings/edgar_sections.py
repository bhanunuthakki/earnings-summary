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


def split_10k(text: str) -> SplitResult:
    """Partition a 10-K into its Items 1–16."""
    slices = _slices_from_items(text, FORM_10K_ITEMS)
    warnings: list[str] = []
    if not slices:
        return SplitResult([], ["no_item_headers_located"])
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

    warnings: list[str] = []
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
    top = locate_items(text, FORM_20F_ITEMS)
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
