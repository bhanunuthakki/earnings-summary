"""Split ``filing_sections`` rows into atomic ``filing_section_items`` rows.

Why item-level at all: the literature review (``docs/design/
disclosure_change_signals.md`` §1.2) and the migration 0200 docstring are both
explicit that additions and removals of INDIVIDUAL disclosure items — one risk
factor, one MD&A sub-heading — carry the signal, and that a section-level or
document-level similarity score misses it. This module is the splitter half of
that: it turns one stored section's prose into the atomic units a filer
actually adds and drops.

Heading detection is REUSED, not reinvented, for risk factors:
``filing_text_fetcher.split_risk_factors`` already carries a validated heading
heuristic (short, capitalized, header-shaped line) tuned against the platform's
HTML-stripping behavior. MD&A sub-headings get their own splitter
(``_split_mdna_items``) because MD&A sub-heads run to different conventions
("Results of Operations", "Liquidity and Capital Resources") than risk-factor
headings, but it reuses the same underlying line-heuristic
(``filing_text_fetcher._looks_like_risk_heading``) rather than duplicating the
regex — see that function's docstring for why splitting instead of reuse would
have been the wrong call there.

Every section gets at least one item. A section with no detectable
sub-structure emits ONE item covering the whole section (mirrors
``edgar_sections.split_freeform``'s honest whole-document fallback) rather than
zero — an empty result reads as "this section doesn't exist", which is false
and would make it silently invisible to the diff engine.

No DB-level foreign key on ``section_id``, per the repo's FK-poisoning
invariant: it is a code-validated INTEGER pointer to ``filing_sections.id``,
checked in one batched query before insert (mirrors
``store._validate_doc_ids``).
"""

from __future__ import annotations

import logging
import re
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from filing_text_fetcher import _looks_like_risk_heading, split_risk_factors
from filings.models import (
    FilingForm,
    FilingSection,
    FiscalPeriod,
    HardStopError,
    sha256_text,
)

log = logging.getLogger(__name__)

ITEMS_TABLE = "filing_section_items"
EXTRACTOR_VERSION = "section_items_v1"

#: Mirrors ``split_risk_factors``'s own floor: a heading with fewer body chars
#: than this is a false-positive match (a bolded cross-reference, not a real
#: item), not a section worth keeping as its own row.
MIN_ITEM_CHARS = 60

#: Canonical concepts this module knows a sub-heading heuristic for. Anything
#: else (financial_statements, an FMP note stem, a free-form 6-K heading that
#: edgar_sections already split) falls through to the whole-section fallback —
#: an honest "no further structure detected" rather than a fabricated split.
_RISK_FACTOR_CONCEPTS = frozenset({"risk_factors"})
_MDNA_CONCEPTS = frozenset({"mdna"})


@dataclass(slots=True)
class SectionItem:
    """One atomic disclosure item, ready to persist to ``filing_section_items``."""

    ticker: str
    section_id: int
    source_ref: str
    doc_id: int | None
    form: FilingForm
    fiscal_year: int | None
    fiscal_period: FiscalPeriod
    canonical_id: str | None
    item_ordinal: int
    heading: str | None
    match_key: str
    body: str
    body_sha256: str
    char_len: int
    extractor_version: str = EXTRACTOR_VERSION


@dataclass(slots=True)
class ItemSplitResult:
    """Pure split output for one section — no DB, no ids assigned yet."""

    items: list[tuple[str | None, str]] = field(default_factory=list[tuple[str | None, str]])
    warnings: list[str] = field(default_factory=list[str])

    @property
    def ok(self) -> bool:
        return bool(self.items)


# ---------------------------------------------------------------------------
# match_key normalization
# ---------------------------------------------------------------------------

#: Leading enumerators filers prepend to a heading: "1.", "(a)", "iii.",
#: bullets. Each enumerator token (roman numeral / letter / digits) may be
#: paren-wrapped ("(a)") OR period-terminated ("iii.", "1.") — both real
#: shapes, so both terminators are accepted for every token kind rather than
#: only digits getting the period form. Repeated application (see the caller's
#: loop) handles stacked enumerators ("1. (a) Foo").
_LEADING_ENUM_RX = re.compile(
    r"^\s*(?:[-*•–—]+|\(\s*(?:[ivxlcdm]{1,6}|[a-z]|\d+)\s*\)|(?:[ivxlcdm]{1,6}|[a-z]|\d+)\.)\s*",  # noqa: RUF001 — EN/EM dash are real bullet-list markers
    re.IGNORECASE,
)
_PUNCT_RX = re.compile(r"[^\w\s]")
_WS_RX = re.compile(r"\s+")
_MATCH_KEY_MAX_LEN = 255


def normalize_heading(heading: str) -> str:
    """Reduce a raw heading to its cross-period match key.

    Deliberately a SIBLING of ``filings.models.normalize_stem``, not a reuse:
    that function strips FMP-specific artifacts (the ``_N`` order-assigned
    disambiguator, ``(Tables)``/``(Details)`` qualifiers) from short XBRL note
    keys. A risk-factor or MD&A heading is free prose with a different failure
    mode — leading enumerators ("1.", "(a)", bullets) and punctuation that
    varies between filings of the same disclosure — so it needs its own strip
    order: enumerators first (applied to a fixed point, since real headings
    stack them), then punctuation, then whitespace collapse and casefold.
    """
    stem = heading.strip()
    prev = None
    while prev != stem:
        prev = stem
        stem = _LEADING_ENUM_RX.sub("", stem)
    stem = _PUNCT_RX.sub(" ", stem)
    stem = _WS_RX.sub(" ", stem).strip().lower()
    return stem[:_MATCH_KEY_MAX_LEN] or heading.strip().lower()[:_MATCH_KEY_MAX_LEN]


# ---------------------------------------------------------------------------
# Splitters
# ---------------------------------------------------------------------------


#: MD&A prose is loaded with financial tables whose row labels butt directly
#: against a number blob with no separating whitespace after HTML-table
#: flattening (e.g. "Interest expense(1,165)(715)(446)(63)%(60)%") — a real
#: line pulled from META's MD&A during calibration against the live book.
#: That reads as exactly TWO "words" to ``_looks_like_risk_heading`` (one
#: capitalized label token, one digit-leading token), which is enough to pass
#: its >=40%-capitalized-words test and get misread as a sub-heading. Risk
#: factors don't have this failure mode (no tables), which is why
#: ``split_risk_factors`` doesn't need this guard and MD&A does: reject any
#: heading candidate whose digit density says "table row", not prose.
_DIGIT_RX = re.compile(r"\d")
_MAX_HEADING_DIGITS = 3


def _looks_tabular(line: str) -> bool:
    return "%" in line or len(_DIGIT_RX.findall(line)) > _MAX_HEADING_DIGITS


def _split_mdna_items(text: str) -> ItemSplitResult:
    """Split MD&A prose on sub-headings.

    Mirrors ``split_risk_factors``'s algorithm (line-oriented heading
    detection, body accumulated between headers) but is its own function
    because MD&A sub-heads ("Results of Operations", "Liquidity and Capital
    Resources", "Critical Accounting Estimates") are a different vocabulary
    than risk-factor headings, and the two disclosures should stay
    independently tunable. Reuses ``_looks_like_risk_heading`` for the actual
    per-line test rather than re-deriving a second heading regex, since the
    shape of a header line (short, capitalized, non-sentence) is identical
    across both disclosures — only the vocabulary differs, and this function
    doesn't hard-code any. Adds ``_looks_tabular`` on top, for the
    table-row false positive that heuristic alone lets through in MD&A.
    """
    if not text:
        return ItemSplitResult([], ["empty_text"])
    lines = text.splitlines()
    sections: list[tuple[str, list[str]]] = []
    heading: str | None = None
    body: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if heading is not None:
                body.append("")
            continue
        if _looks_like_risk_heading(stripped) and not _looks_tabular(stripped):
            if heading is not None and body:
                sections.append((heading, body))
            heading = stripped.rstrip(".:—-")
            body = []
        elif heading is not None:
            body.append(stripped)
    if heading is not None and body:
        sections.append((heading, body))

    out: list[tuple[str | None, str]] = []
    for head, body_lines in sections:
        joined = "\n".join(body_lines).strip()
        if len(joined) < MIN_ITEM_CHARS:
            continue
        out.append((head, joined))

    if not out:
        return ItemSplitResult([], ["no_subheadings_detected"])
    return ItemSplitResult(out, [])


def _split_risk_factor_items(text: str) -> ItemSplitResult:
    """Split Item 1A / Item 3.D prose into individual risk factors.

    REUSES ``filing_text_fetcher.split_risk_factors`` rather than reinventing
    its heading heuristic, per the P0 directive.
    """
    if not text:
        return ItemSplitResult([], ["empty_text"])
    pairs = split_risk_factors(text)
    if not pairs:
        return ItemSplitResult([], ["no_subheadings_detected"])
    return ItemSplitResult([(heading, body) for heading, body in pairs], [])


def split_section(section: FilingSection) -> ItemSplitResult:
    """Dispatch to the right sub-splitter for ``section.canonical_id``.

    Falls through to the whole-section fallback for any concept this module
    does not have a heuristic for — including a genuine miss inside a known
    concept (e.g. a risk-factors section whose headings failed detection):
    ONE item covering the whole section, never zero.
    """
    concept = section.canonical_id
    if concept in _RISK_FACTOR_CONCEPTS:
        result = _split_risk_factor_items(section.text)
    elif concept in _MDNA_CONCEPTS:
        result = _split_mdna_items(section.text)
    else:
        result = ItemSplitResult([], ["no_heuristic_for_concept"])

    if result.ok:
        return result

    # Whole-section fallback: the section IS one item. Recorded so a caller
    # can tell "we found one real item" from "we gave up and kept the
    # warnings from the sub-splitter" apart.
    whole = section.text.strip()
    if not whole:
        return ItemSplitResult([], [*result.warnings, "empty_section_no_fallback"])
    return ItemSplitResult(
        [(section.title, whole)],
        [*result.warnings, "whole_section_fallback"],
    )


def to_section_items(section: FilingSection, result: ItemSplitResult) -> list[SectionItem]:
    """Materialize a split result into persistable, hashed ``SectionItem`` rows.

    Requires ``section.id`` — the row identity ``filing_section_items.section_id``
    points at — which is only populated when ``section`` was rehydrated from the
    DB (``store.get_sections`` / ``store.section_timeline``), never on a
    freshly-``FilingSection.build``-ed instance.
    """
    if section.id is None:
        raise HardStopError(
            "to_section_items requires a persisted FilingSection (section.id is None) — "
            "fetch it via store.get_sections/section_timeline, not FilingSection.build"
        )
    out: list[SectionItem] = []
    for ordinal, (heading, body) in enumerate(result.items):
        out.append(
            SectionItem(
                ticker=section.ticker,
                section_id=section.id,
                source_ref=section.source_ref,
                doc_id=section.doc_id,
                form=section.form,
                fiscal_year=section.fiscal_year,
                fiscal_period=section.fiscal_period,
                canonical_id=section.canonical_id,
                item_ordinal=ordinal,
                heading=heading,
                match_key=normalize_heading(heading) if heading else f"__ordinal_{ordinal}",
                body=body,
                body_sha256=sha256_text(body),
                char_len=len(body),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _now() -> datetime:
    """Naive-UTC stamp, per the repo's datetime convention."""
    return datetime.now(UTC).replace(tzinfo=None)


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _require_table(conn: sqlite3.Connection, *, missing_ok: bool) -> bool:
    if table_exists(conn, ITEMS_TABLE):
        return True
    if missing_ok:
        log.info({"event": "filing_section_items_table_absent", "degraded": True})
        return False
    raise HardStopError(
        f"{ITEMS_TABLE} missing — run `alembic upgrade head` (migration 0200) "
        "before splitting or querying section items"
    )


def _validate_section_ids(conn: sqlite3.Connection, section_ids: set[int]) -> None:
    """Code-validated pointer check for ``filing_sections.id`` (no DB-level FK)."""
    if not section_ids:
        return
    if not table_exists(conn, "filing_sections"):
        raise HardStopError("filing_sections table missing — cannot validate section_id pointers")
    placeholders = ",".join("?" * len(section_ids))
    found = {
        int(r[0])
        for r in conn.execute(
            f"SELECT id FROM filing_sections WHERE id IN ({placeholders})", tuple(section_ids)
        ).fetchall()
    }
    dangling = section_ids - found
    if dangling:
        raise HardStopError(f"section_id(s) not present in filing_sections: {sorted(dangling)}")


def _prune_superseded(conn: sqlite3.Connection, items: Sequence[SectionItem]) -> int:
    """Delete rows for these sections that the new split no longer contains.

    Mirrors ``store._prune_superseded``'s reasoning exactly, one level down:
    if a splitter fix (or a heuristic tweak) makes a section yield FEWER
    items than a previous run did, the old run's higher-ordinal rows would
    otherwise never get cleaned up — a stale item sitting alongside the
    current split, invisible to ``get_items`` filters that scope by period
    but silently corrupting any query that scans the table directly. One
    section holds exactly one split — the newest.
    """
    by_section: dict[int, set[int]] = {}
    for i in items:
        by_section.setdefault(i.section_id, set()).add(i.item_ordinal)
    deleted = 0
    for section_id, keep in by_section.items():
        rows = conn.execute(
            f"SELECT id, item_ordinal FROM {ITEMS_TABLE} WHERE section_id = ?", (section_id,)
        ).fetchall()
        stale = [int(r[0]) for r in rows if int(r[1]) not in keep]
        if not stale:
            continue
        conn.executemany(f"DELETE FROM {ITEMS_TABLE} WHERE id = ?", [(i,) for i in stale])
        deleted += len(stale)
    if deleted:
        log.info({"event": "filing_section_items_superseded_rows_pruned", "count": deleted})
    return deleted


def write_items(conn: sqlite3.Connection, items: Sequence[SectionItem]) -> int:
    """Upsert items on (section_id, item_ordinal) — idempotent per section.

    Re-splitting the same section with the same extractor reproduces the same
    ordinals and bodies, so this is a no-op in content terms on rerun. Prunes
    stale ordinals first (see ``_prune_superseded``) so a splitter change that
    yields fewer items doesn't leave the excess behind.
    """
    if not items:
        return 0
    _require_table(conn, missing_ok=False)
    _validate_section_ids(conn, {i.section_id for i in items})
    _prune_superseded(conn, items)

    now = _now()
    rows = [
        (
            i.ticker,
            i.section_id,
            i.source_ref,
            i.form.value,
            i.fiscal_year,
            i.fiscal_period.value,
            i.canonical_id,
            i.item_ordinal,
            i.heading,
            i.match_key,
            i.body,
            i.body_sha256,
            i.char_len,
            i.extractor_version,
            now,
        )
        for i in items
    ]
    conn.executemany(
        f"""
        INSERT INTO {ITEMS_TABLE}
            (ticker, section_id, source_ref, form, fiscal_year, fiscal_period,
             canonical_id, item_ordinal, heading, match_key, body, body_sha256,
             char_len, extractor_version, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(section_id, item_ordinal) DO UPDATE SET
            ticker=excluded.ticker,
            source_ref=excluded.source_ref,
            form=excluded.form,
            fiscal_year=excluded.fiscal_year,
            fiscal_period=excluded.fiscal_period,
            canonical_id=excluded.canonical_id,
            heading=excluded.heading,
            match_key=excluded.match_key,
            body=excluded.body,
            body_sha256=excluded.body_sha256,
            char_len=excluded.char_len,
            extractor_version=excluded.extractor_version,
            created_at=excluded.created_at
        """,
        rows,
    )
    return len(rows)


def _row_to_item(row: sqlite3.Row) -> SectionItem | None:
    try:
        return SectionItem(
            ticker=row["ticker"],
            section_id=row["section_id"],
            source_ref=row["source_ref"],
            # doc_id is not a filing_section_items column (see migration
            # 0200 — only section_id is stored); a caller that needs it for
            # provenance re-derives it from filing_sections via section_id.
            doc_id=None,
            form=FilingForm(row["form"]),
            fiscal_year=row["fiscal_year"],
            fiscal_period=FiscalPeriod(row["fiscal_period"]),
            canonical_id=row["canonical_id"],
            item_ordinal=row["item_ordinal"],
            heading=row["heading"],
            match_key=row["match_key"],
            body=row["body"],
            body_sha256=row["body_sha256"],
            char_len=row["char_len"],
            extractor_version=row["extractor_version"],
        )
    except (ValueError, KeyError, TypeError) as exc:
        log.warning({"event": "filing_section_item_row_invalid", "error": str(exc)})
        return None


def get_items(
    conn: sqlite3.Connection,
    ticker: str,
    *,
    canonical_id: str | None = None,
    fiscal_year: int | None = None,
    fiscal_period: FiscalPeriod | None = None,
    section_id: int | None = None,
    missing_ok: bool = False,
) -> list[SectionItem]:
    """Fetch persisted items for a ticker under any combination of filters."""
    if not _require_table(conn, missing_ok=missing_ok):
        return []
    clauses = ["ticker = ?"]
    params: list[object] = [ticker.strip().upper()]
    if canonical_id is not None:
        clauses.append("canonical_id = ?")
        params.append(canonical_id)
    if fiscal_year is not None:
        clauses.append("fiscal_year = ?")
        params.append(fiscal_year)
    if fiscal_period is not None:
        clauses.append("fiscal_period = ?")
        params.append(fiscal_period.value)
    if section_id is not None:
        clauses.append("section_id = ?")
        params.append(section_id)

    prior_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"SELECT * FROM {ITEMS_TABLE} WHERE {' AND '.join(clauses)} "
            "ORDER BY fiscal_year ASC, fiscal_period ASC, section_id, item_ordinal ASC",
            tuple(params),
        ).fetchall()
    finally:
        conn.row_factory = prior_factory

    out: list[SectionItem] = []
    dropped = 0
    for row in rows:
        item = _row_to_item(row)
        if item is None:
            dropped += 1
            continue
        out.append(item)
    if dropped:
        log.warning({"event": "filing_section_items_rows_dropped", "count": dropped})
    return out
