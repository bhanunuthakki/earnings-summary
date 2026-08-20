"""Resolve a requested KPI label to the canonical ``kpi_definitions.name``.

Shared by every consumer that joins a human-supplied KPI label — a holdings
``chart_priorities`` entry, a ``break_rules`` ``kpi_name``, a tier-N ledger name
— to the stored ``kpi_definitions`` row. KPI names fragment over time: the
issuer's IR spreadsheet ingests "Monthly ARPAC (USD)" while an older LLM brief
stored a bare "Monthly ARPAC", leaving two definitions for the same metric where
one is fully populated and the other near-empty.

Exact-name matching picks whichever spelling the label happens to use, so a
short holdings label can resolve to the sparse duplicate and the consumer then
charts / evaluates an almost-empty series. This module instead matches on a
parenthetical-insensitive normalized name and prefers the definition carrying
the MOST observations, so a fragmented duplicate can never shadow the canonical
series.

Originally ``financials._resolve_kpi_definition_name`` / ``_normalize_kpi_name``
(PR #195); extracted here so the §3 chart loader, the §2 break-rule ledger, and
the break-rule evaluator all share one resolver instead of three exact-match
lookups that drift apart.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence

from models.facts import Unit
from models.kpis import DefinitionOrigin
from models.unit_convert import same_family

# kpi_facts.fiscal_period_type values that denote a quarterly observation. The
# §3 chart cadence is quarterly, so the chart loader measures richness over
# these buckets only; the break-rule paths query every period type and pass
# period_types=None so "most observations" is measured over the rows they read.
QUARTERLY_FACT_PERIOD_TYPES: tuple[str, ...] = ("Q1", "Q2", "Q3", "Q4")

# kpi_facts.fiscal_period_type values that denote an annual (fiscal-year-end)
# observation — the cadence-aware twin of QUARTERLY_FACT_PERIOD_TYPES. Matches the
# financials annual set (report.sections._common.ANNUAL_PERIOD_TYPES) so an annual
# KPI series aligns with the annual line-item axis. Consumers select these rows
# when a definition's reporting_cadence is 'annual' (see reporting_cadence_for).
ANNUAL_FACT_PERIOD_TYPES: tuple[str, ...] = ("FY", "annual")

# Trailing "(...)" qualifier on a stored KPI name, e.g. "Monthly ARPAC (USD)" or
# "ROE (annualized, consolidated)". Stripped when matching a requested label to a
# stored definition.
_KPI_NAME_PAREN_TAIL_RX = re.compile(r"\s*\([^()]*\)\s*$")


def normalize_kpi_name(name: str) -> str:
    """Lowercase, collapse whitespace, drop trailing parenthetical qualifiers.

    "Monthly ARPAC (USD)" and "Monthly ARPAC" both normalize to "monthly arpac"
    so a short label still resolves to the canonical definition. Only *trailing*
    parentheticals are stripped; an interior qualifier is kept so genuinely
    distinct metrics ("NIM" vs "Risk-adjusted NIM (...)") don't collide.
    """
    s = name.strip()
    while True:
        stripped = _KPI_NAME_PAREN_TAIL_RX.sub("", s).strip()
        if stripped == s:
            break
        s = stripped
    return " ".join(s.split()).lower()


# The capture-all extractor (table_extractors.generic_xbrl_capture._build_name)
# qualifies a captured metric as ``section — axis — leaf`` joined by this exact
# separator; the LEAF after the last one is the metric itself. Mirrors
# ask.grounding._KPI_QUALIFIER_SEP so the picker (kpi_group_key) and the ask
# name-match agree on what "the same metric" is.
_KPI_QUALIFIER_SEP = " — "


def kpi_group_key(name: str) -> str:
    """The de-fragmentation key: surface variants of ONE metric share it.

    Mirrors the ask leaf logic (``ask.grounding._label_match_keys``) then folds
    with :func:`normalize_kpi_name`. Peel a ``section — axis —`` qualifier to
    its LEAF when that leaf is a distinct ≥2-word phrase (a generic single-word
    leaf — "Total" / "Net" — is NOT peeled, so distinct metrics that merely
    share it can't false-merge), otherwise key on the whole name.

    Conservative by construction (the §7a.4 invariant: a duplicate token is
    acceptable, a false merge is not): only unit / casing / whitespace /
    qualifier-prefix variants collapse; true synonyms ("NIM" vs "Net interest
    margin") never do, because their normalized leaves differ.
    """
    full = normalize_kpi_name(name)
    if _KPI_QUALIFIER_SEP in name:
        leaf = normalize_kpi_name(name.rsplit(_KPI_QUALIFIER_SEP, 1)[-1])
        if leaf and leaf != full and len(leaf.split()) >= 2:
            return leaf
    return full


def resolve_kpi_definition_name(
    conn: sqlite3.Connection,
    ticker: str,
    requested: str,
    *,
    period_types: Sequence[str] | None = None,
) -> str | None:
    """Choose which stored ``kpi_definitions.name`` to use for a requested label.

    Among this ticker's definitions that carry facts (optionally restricted to
    ``period_types``), accept an exact name match or a normalized-equal
    (parenthetical-insensitive) one, then pick the candidate with the MOST
    observations — exactness only breaks ties. This keeps a near-empty
    fragmented duplicate (e.g. a stray "Monthly ARPAC" with 2 rows) from
    shadowing the fully-populated canonical "Monthly ARPAC (USD)".

    ``period_types`` filters the observation COUNT to those fiscal_period_type
    buckets so richness is measured over the rows the caller will actually read:
    the quarterly chart loader passes ``QUARTERLY_FACT_PERIOD_TYPES``; the
    break-rule paths (which query every period type) pass None. Returns None when
    no stored definition — among those carrying facts — matches the label.

    Requires ``conn.row_factory = sqlite3.Row`` (every consumer's connection
    already sets it).
    """
    cur = conn.cursor()
    if period_types:
        placeholders = ",".join("?" * len(period_types))
        cur.execute(
            f"""
            SELECT kd.name AS name, COUNT(*) AS n
            FROM kpi_facts kf
            JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id
            WHERE kf.ticker = ?
              AND kf.fiscal_period_type IN ({placeholders})
            GROUP BY kd.name
            """,
            (ticker.upper(), *period_types),
        )
    else:
        cur.execute(
            """
            SELECT kd.name AS name, COUNT(*) AS n
            FROM kpi_facts kf
            JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id
            WHERE kf.ticker = ?
            GROUP BY kd.name
            """,
            (ticker.upper(),),
        )
    want = normalize_kpi_name(requested)
    best_name: str | None = None
    best_rank: tuple[int, int] = (-1, -1)  # (obs_count, exactness)
    for r in cur.fetchall():
        stored = str(r["name"])
        if stored == requested:
            exactness = 1
        elif normalize_kpi_name(stored) == want:
            exactness = 0
        else:
            continue
        rank = (int(r["n"]), exactness)
        if rank > best_rank:
            best_rank = rank
            best_name = stored
    return best_name


def matching_kpi_definition_ids(
    conn: sqlite3.Connection, ticker: str, requested: str
) -> tuple[int, ...]:
    """All definition IDs in the read-side normalized KPI family.

    Readers must reconcile aliases *before* filtering and window partitioning;
    choosing a single rich definition first leaves a same-period fact under a
    less-populated alias invisible.  This is intentionally read-only and uses
    the existing conservative normalized-name contract.
    """
    want = normalize_kpi_name(requested)
    rows = conn.execute(
        "SELECT id, name, unit FROM kpi_definitions WHERE ticker = ? ORDER BY id",
        (ticker.upper(),),
    ).fetchall()
    family = [row for row in rows if normalize_kpi_name(str(row["name"])) == want]
    if not family:
        return ()
    exact = [row for row in family if str(row["name"]) == requested]
    anchor = exact[0] if exact else family[0]
    anchor_unit = str(anchor["unit"])
    # Names alone are not a fact identity: readers cannot merge an actual-dollar
    # row with a millions-scaled alias and silently alter a displayed value. Nor
    # may a USD alias pull a BRL observation into the same report cell. Currency
    # is a fact (not definition) attribute, so compare each definition's observed
    # currency set; a definition with no observations is harmless to retain.
    fact_columns = {
        str(column["name"]) for column in conn.execute("PRAGMA table_info(kpi_facts)").fetchall()
    }
    if "currency" not in fact_columns:
        return tuple(int(row["id"]) for row in family if str(row["unit"]) == anchor_unit)
    fact_currency_rows = conn.execute(
        "SELECT kpi_definition_id, currency FROM kpi_facts WHERE ticker = ?",
        (ticker.upper(),),
    ).fetchall()
    currencies_by_definition: dict[int, set[str | None]] = {}
    for fact in fact_currency_rows:
        definition_id = int(fact["kpi_definition_id"])
        currencies_by_definition.setdefault(definition_id, set()).add(
            str(fact["currency"]) if fact["currency"] is not None else None
        )
    anchor_currencies = currencies_by_definition.get(int(anchor["id"]), set())
    return tuple(
        int(row["id"])
        for row in family
        if str(row["unit"]) == anchor_unit
        and (
            not anchor_currencies
            or not currencies_by_definition.get(int(row["id"]))
            or currencies_by_definition[int(row["id"])] == anchor_currencies
        )
    )


def engine_formula_definition(name: str) -> str | None:
    """If ``name`` is a ``metrics_engine`` REGISTRY ``formula_key``, return a
    rich "display_formula — method_notes" tooltip string; ``None`` otherwise
    (the caller falls back to the generic kpi_definitions-derived tooltip).

    Phase 3 wiring (docs/design/bottoms_up_metrics_engine.md §6): every
    ``metrics_engine`` computed value is persisted with ``kpi_definitions.name
    == formula.formula_key`` verbatim (``metrics_engine.io._persist_attempt``
    calls ``find_or_create_kpi_definition(..., name=formula.formula_key,
    ...)``), so a ``formula_key`` like ``"pe_ttm"`` or ``"gross_margin"`` IS a
    KPI name the moment the engine has run for a ticker. Without this
    hookup, the DIY picker / Ask tooltip would fall back to the generic
    "Company KPI 'pe_ttm'" placeholder instead of surfacing the registry's
    own documented formula + method notes — the read-side half of the
    engine's provenance contract (§3: "what formula produced this number").
    A lazy, function-local import avoids a module-load-time dependency from
    this low-level resolver onto the higher-level metrics_engine package.
    """
    from compute.metrics_engine.registry import latest as latest_formula

    formula = latest_formula(name)
    if formula is None:
        return None
    return f"{formula.display_formula} — {formula.method_notes}"


def reporting_cadence_for(conn: sqlite3.Connection, ticker: str, requested: str) -> str:
    """Return the reporting_cadence ('quarterly' | 'annual' | 'ttm') for the
    definition best matching ``requested``, defaulting to ``'quarterly'``.

    Matching is *fact-independent* (a tracked-but-empty annual KPI still
    resolves): exact name, then a parenthetical-insensitive normalized name. When
    several definitions normalize-match, an ``'annual'`` marker wins over
    ``'quarterly'`` so a sparse fragmented duplicate can't mask the annual
    cadence. Defensive: returns ``'quarterly'`` when the column is absent
    (pre-0072 / minimal test DBs) or no definition matches — so every caller can
    treat the result as authoritative without re-checking the schema.

    Requires ``conn.row_factory = sqlite3.Row`` (every consumer sets it).
    """
    cols = {c["name"] for c in conn.execute("PRAGMA table_info(kpi_definitions)").fetchall()}
    if "reporting_cadence" not in cols:
        return "quarterly"
    want = normalize_kpi_name(requested)
    best = "quarterly"
    found = False
    for row in conn.execute(
        "SELECT name, reporting_cadence FROM kpi_definitions WHERE ticker = ?",
        (ticker.upper(),),
    ):
        stored = str(row["name"])
        if stored == requested or normalize_kpi_name(stored) == want:
            found = True
            cadence = str(row["reporting_cadence"] or "quarterly")
            if cadence == "annual":
                return "annual"
            best = cadence
    return best if found else "quarterly"


# ---------------------------------------------------------------------------
# Write-path canonicalizer — canonical_metric_name
#
# `resolve_kpi_definition_name` (above) is the READ side: it unifies already-
# fragmented duplicates at query time and is deliberately LENIENT (it strips
# every trailing parenthetical). `canonical_metric_name` is the WRITE side: the
# "capture every reported number" program (directives/capture_every_number_program.md)
# routes every freshly-extracted label through it BEFORE persisting, to decide
# whether the value joins an existing kpi_definitions row or mints a new one.
#
# The two paths are intentionally asymmetric — split cautiously on write, unify
# generously on read — because the governing invariant on the write side is:
#
#     A FALSE MERGE of two genuinely distinct metrics is worse than a duplicate.
#
# A duplicate is recoverable (the read resolver defragments it; a later pass can
# consolidate). A false merge silently routes two different metrics' facts into
# one series and is effectively unrecoverable. So the write-side match key only
# collapses UNIT / CASING / WHITESPACE surface variants and KEEPS semantic
# qualifiers, and a normalized match is additionally gated by unit-family
# compatibility.
# ---------------------------------------------------------------------------

# Tokens that denote a UNIT rather than the metric itself, used to recognize a
# "unit-only" trailing parenthetical — "(USD)", "($ in millions)", "(%)" — that is
# safe to strip from a match key. A parenthetical is unit-only iff EVERY non-filler
# token in it is here; a single non-unit token ("gross", "annualized", "of total")
# keeps the whole parenthetical, so distinct metrics that merely share a stem never
# collapse. Symbols ($/%/# and the major currency glyphs) are tokenized standalone.
_UNIT_PAREN_TOKENS: frozenset[str] = frozenset(
    {
        # currency codes / glyphs (us$, r$ split to 'us'/'r' + '$'; 'us' is filler)
        "usd",
        "eur",
        "gbp",
        "dkk",
        "brl",
        "cad",
        "inr",
        "aud",
        "krw",
        "jpy",
        "chf",
        "$",
        "€",
        "£",
        "¥",
        "₹",
        # magnitudes
        "thousands",
        "thousand",
        "millions",
        "million",
        "billions",
        "billion",
        "mm",
        "mn",
        "mln",
        "bn",
        "bln",
        "k",
        "m",
        "b",
        # proportions
        "percent",
        "pct",
        "%",
        "bps",
        "bp",
        "ratio",
        "x",
        # counts
        "count",
        "number",
        "#",
        "units",
        "unit",
    }
)
# Connective words admitted inside a unit parenthetical without disqualifying it:
# "(in millions)" / "(per share)" are unit-only, but "(% of revenue)" is NOT
# (because 'revenue' survives), so the rate-of-X case never merges into the level.
_UNIT_PAREN_FILLER: frozenset[str] = frozenset({"in", "of", "per", "us", "the", "a"})

# A trailing "( ... )" group, capturing its inner text (no nested parens).
_PAREN_TAIL_GROUP_RX = re.compile(r"\s*\(([^()]*)\)\s*$")
# Tokenizer for parenthetical content: alphanumeric runs OR a standalone unit glyph.
_PAREN_TOKEN_RX = re.compile(r"[a-z0-9]+|[$%#€£¥₹]")

# Bare (non-parenthetical) trailing unit suffixes that are safe to strip from a
# match key — "Gross margin %" / "NIM bps" / "Revenue USD". DELIBERATELY tiny: the
# risky single letters (m / b / k / x) are excluded so a real trailing word is
# never truncated. Word tokens require a preceding space; glyphs may be glued.
_BARE_SUFFIX_WORDS: tuple[str, ...] = ("percent", "pct", "bps", "usd")
_BARE_SUFFIX_GLYPHS: tuple[str, ...] = ("%", "$")


def _is_unit_only_parenthetical(inner: str) -> bool:
    """True iff every non-filler token in a parenthetical's content is a unit."""
    tokens = [t for t in _PAREN_TOKEN_RX.findall(inner.lower()) if t not in _UNIT_PAREN_FILLER]
    return bool(tokens) and all(t in _UNIT_PAREN_TOKENS for t in tokens)


def _canonical_match_key(name: str) -> str:
    """Normalize ``name`` to the conservative write-path match key.

    Three passes: (1) peel trailing UNIT-ONLY parentheticals while keeping
    semantic ones; (2) casefold + collapse whitespace; (3) peel a trailing bare
    unit suffix from a tiny safe set. The result is the key two labels must share
    EXACTLY (plus unit-family compatibility) to be treated as the same metric on
    write — no edit-distance / fuzzy similarity is used, because an approximate
    match is exactly how a false merge happens.
    """
    s = name.strip()
    while True:
        m = _PAREN_TAIL_GROUP_RX.search(s)
        if m is None or not _is_unit_only_parenthetical(m.group(1)):
            break
        s = s[: m.start()].rstrip()
    s = " ".join(s.split()).lower()
    changed = True
    while changed and s:
        changed = False
        for glyph in _BARE_SUFFIX_GLYPHS:
            if s.endswith(glyph) and len(s) > len(glyph):
                s = s[: -len(glyph)].rstrip()
                changed = True
        for word in _BARE_SUFFIX_WORDS:
            suffix = " " + word
            if s.endswith(suffix):
                s = s[: -len(suffix)].rstrip()
                changed = True
    return s


def _clean_new_name(raw_label: str) -> str:
    """The name a never-before-seen metric is minted under: the raw label with
    whitespace collapsed/trimmed, content otherwise preserved.

    Deliberately minimal — case, parentheticals, and unit tokens are KEPT so the
    first surface form an extractor emits becomes a readable canonical name and so
    a minted name can never collide (via over-stripping) with a different existing
    definition. Later variants collapse onto it through ``_canonical_match_key``,
    not by mangling this name.
    """
    return " ".join(raw_label.split())


def _parse_unit(raw: object) -> Unit | None:
    """Best-effort parse of a stored ``kpi_definitions.unit`` string to ``Unit``;
    None when absent or out-of-vocabulary (a junk unit must not block a merge)."""
    if raw is None:
        return None
    try:
        return Unit(str(raw).strip().lower())
    except ValueError:
        return None


def _units_mergeable(incoming: Unit, stored_raw: object) -> bool:
    """True iff a value in ``incoming`` may join a definition stored in
    ``stored_raw``'s unit — i.e. they share a unit family (a dollar LEVEL never
    absorbs a percentage RATE that shares a stem). An unparseable stored unit is
    treated as compatible: don't split a series over a garbage unit string."""
    stored = _parse_unit(stored_raw)
    if stored is None:
        return True
    return same_family(incoming, stored)


def _unit_family_token(unit: Unit) -> str:
    """A short, stable tag for ``unit``'s dimensional FAMILY — used to disambiguate
    a minted name from a surface-identical definition of an incompatible family.

    Family-level (not the specific unit) so every member of one family shares a
    single disambiguated name: a later capture in a different magnitude of the same
    family (``count``→``thousands`` say) re-derives the SAME tag and reuses the row
    rather than minting a per-magnitude duplicate. Magnitudes → ``amount``,
    proportions → ``rate``, everything else (raw counts, out-of-vocab) → its own
    unit value, which is its own singleton family under ``same_family``."""
    if same_family(unit, Unit.ACTUAL):
        return "amount"
    if same_family(unit, Unit.RATIO):
        return "rate"
    return unit.value


def _mint_unit_safe_name(cleaned: str, unit: Unit, rows: list[sqlite3.Row]) -> str:
    """The name to mint for a captured ``(cleaned, unit)`` that found no
    family-compatible normalized match — GUARANTEED not to collide on
    ``(ticker, name)`` with an existing definition of an INCOMPATIBLE unit family.

    ``find_or_create_kpi_definition`` keys only on ``(ticker, name)`` with the unit
    absent from its identity (``UNIQUE(ticker, name)``), so returning ``cleaned``
    bare when a surface-identical row of an incompatible family already exists
    would silently route this distinct metric's facts onto that row — the exact
    false merge the candidate-step unit gate exists to prevent, and the governing
    invariant says a false merge is worse than a duplicate. Append a family tag (and
    in the pathological case that the tagged name ALSO collides incompatibly, a
    numeric suffix) until the name is unit-safe. Deterministic in the common
    single-collision case, so a re-encounter of the same ``(label, unit)`` reuses
    the disambiguated row instead of minting another."""

    def _collides(name: str) -> bool:
        return any(str(r["name"]) == name and not _units_mergeable(unit, r["unit"]) for r in rows)

    if not _collides(cleaned):
        return cleaned
    tagged = f"{cleaned} ({_unit_family_token(unit)})"
    if not _collides(tagged):
        return tagged
    suffix = 2
    while _collides(f"{tagged} #{suffix}"):
        suffix += 1
    return f"{tagged} #{suffix}"


def _kpi_definitions_has_origin(conn: sqlite3.Connection) -> bool:
    """True iff kpi_definitions carries ``definition_origin`` (migration 0113).
    Schema-defensive: a pre-0113 / minimal test DB simply skips origin-aware
    tie-breaking."""
    return any(
        str(r["name"] if hasattr(r, "keys") else r[1]) == "definition_origin"
        for r in conn.execute("PRAGMA table_info(kpi_definitions)").fetchall()
    )


def _definition_fact_counts(conn: sqlite3.Connection, ticker: str) -> dict[int, int]:
    """``{kpi_definition_id: fact_count}`` for ``ticker`` — INCLUDING factless
    definitions (LEFT JOIN → 0), unlike the read resolver which counts only
    definitions that carry facts. The write path must see every existing
    definition so a not-yet-populated capture row is reused, not re-minted.
    Tolerates a missing kpi_facts table ({} → all counts treated as 0)."""
    try:
        rows = conn.execute(
            "SELECT kd.id AS id, COUNT(kf.id) AS n "
            "FROM kpi_definitions kd "
            "LEFT JOIN kpi_facts kf ON kf.kpi_definition_id = kd.id "
            "WHERE kd.ticker = ? GROUP BY kd.id",
            (ticker.upper(),),
        ).fetchall()
    except sqlite3.Error:
        return {}
    return {int(r["id"]): int(r["n"]) for r in rows}


def canonical_metric_name(
    conn: sqlite3.Connection,
    ticker: str,
    raw_label: str,
    unit: Unit,
) -> str:
    """Return the ``kpi_definitions.name`` a captured ``(raw_label, unit)`` should
    be stored under for ``ticker`` — the write-path canonicalizer for the
    capture-every-number program.

    Resolution:

    1. Compute the conservative match key of ``raw_label`` (``_canonical_match_key``
       — unit/casing/whitespace folded, semantic qualifiers kept).
    2. Among this ticker's existing definitions, keep those whose own match key
       equals it AND whose stored unit is family-compatible with ``unit``
       (``_units_mergeable``). The unit gate is what stops a dollar *level* from
       absorbing a *rate* that happens to share a stem.
    3. If any match, return the canonical one: most facts wins (mirrors the read
       resolver's most-observations defragmentation so capture reuses the rich
       series), then analyst-origin over capture-origin, then an exact surface
       match, then lowest id — a fully deterministic order.
    4. Otherwise mint a clean new name (``_clean_new_name``) — but if that bare
       name is surface-identical to an existing definition of an INCOMPATIBLE unit
       family, disambiguate it with a family tag (``_mint_unit_safe_name``) so the
       unit gate's split is not silently undone downstream.

    The returned name is fed straight to ``find_or_create_kpi_definition``, which
    keys on ``(ticker, name)`` with the unit ABSENT from its identity. So when no
    family-compatible normalized match exists, returning a bare name that collides
    with a surface-identical row of a DIFFERENT unit family would false-merge two
    distinct metrics; step 4 prevents that by minting a unit-safe name instead.
    Requires ``conn.row_factory = sqlite3.Row``.
    """
    cleaned = _clean_new_name(raw_label)
    if not cleaned:
        return cleaned
    want = _canonical_match_key(cleaned)
    has_origin = _kpi_definitions_has_origin(conn)
    select_cols = "id, name, unit" + (", definition_origin" if has_origin else "")
    try:
        rows = conn.execute(
            f"SELECT {select_cols} FROM kpi_definitions WHERE ticker = ?",
            (ticker.upper(),),
        ).fetchall()
    except sqlite3.Error:
        return cleaned

    candidates = [
        r
        for r in rows
        if _canonical_match_key(str(r["name"])) == want and _units_mergeable(unit, r["unit"])
    ]
    if not candidates:
        # No family-compatible normalized match. A bare ``cleaned`` here would let
        # find_or_create_kpi_definition — keyed on (ticker, name), unit ABSENT —
        # silently merge this value onto a surface-identical definition of an
        # incompatible unit family (a COUNT "Deposits" onto the dollar "Deposits").
        # Mint a unit-disambiguated name so the unit gate's split survives to
        # persistence.
        return _mint_unit_safe_name(cleaned, unit, rows)

    counts = _definition_fact_counts(conn, ticker)

    def _rank(r: sqlite3.Row) -> tuple[int, int, int, int]:
        obs = counts.get(int(r["id"]), 0)
        analyst = int(has_origin and str(r["definition_origin"]) == DefinitionOrigin.ANALYST.value)
        exact_surface = int(str(r["name"]) == cleaned)
        return (obs, analyst, exact_surface, -int(r["id"]))

    return str(max(candidates, key=_rank)["name"])
