"""Unit tests for the shared KPI-label → definition-name resolver.

`compute.kpi_resolver` is the single resolver behind the §3 chart loader, the §2
break-rule ledger, and the break-rule evaluator. It guards the NU-class bug where
a holdings/break-rule label ("Monthly ARPAC") exact-matched a near-empty
*duplicate* definition (2 stray rows) instead of the fully-populated canonical
"Monthly ARPAC (USD)" (12 rows), so the consumer rendered / evaluated an almost
empty series even though the data was present one name-key away.

The resolver matches on a parenthetical-insensitive normalized name and prefers
the definition with the MOST observations, so a fragmented duplicate can never
shadow the canonical series. `period_types` scopes the richness count to the
buckets the caller will actually read (quarterly for the chart, every period for
the break-rule paths).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from compute.kpi_resolver import (  # noqa: E402
    QUARTERLY_FACT_PERIOD_TYPES,
    canonical_metric_name,
    engine_formula_definition,
    normalize_kpi_name,
    resolve_kpi_definition_name,
)
from models.facts import Unit  # noqa: E402

_QUARTER_ENDS = [
    "2023-03-31",
    "2023-06-30",
    "2023-09-30",
    "2023-12-31",
    "2024-03-31",
    "2024-06-30",
    "2024-09-30",
    "2024-12-31",
    "2025-03-31",
    "2025-06-30",
    "2025-09-30",
    "2025-12-31",
]


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE kpi_definitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker VARCHAR NOT NULL,
            name VARCHAR NOT NULL,
            unit VARCHAR,
            definition_origin VARCHAR NOT NULL DEFAULT 'analyst'
        );
        CREATE TABLE kpi_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker VARCHAR NOT NULL,
            period_end VARCHAR NOT NULL,
            value NUMERIC,
            unit VARCHAR,
            kpi_definition_id INTEGER NOT NULL,
            fiscal_period_type VARCHAR NOT NULL,
            source_doc_id INTEGER NOT NULL DEFAULT 1
        );
        """
    )
    return conn


def _add_def(
    conn: sqlite3.Connection,
    ticker: str,
    name: str,
    unit: str = "actual",
    *,
    origin: str = "analyst",
) -> int:
    cur = conn.execute(
        "INSERT INTO kpi_definitions (ticker, name, unit, definition_origin) VALUES (?, ?, ?, ?)",
        (ticker, name, unit, origin),
    )
    row_id = cur.lastrowid
    assert row_id is not None
    return row_id


def _add_facts(
    conn: sqlite3.Connection,
    ticker: str,
    def_id: int,
    ends: list[str],
    *,
    period_type: str | None = None,
    unit: str = "actual",
) -> None:
    """Insert one fact per period_end. `period_type` overrides the derived 'Qn'."""
    for end in ends:
        fpt = period_type or f"Q{(int(end[5:7]) - 1) // 3 + 1}"
        conn.execute(
            "INSERT INTO kpi_facts (ticker, period_end, value, unit, kpi_definition_id, "
            "fiscal_period_type) VALUES (?, ?, ?, ?, ?, ?)",
            (ticker, end, 1.0, unit, def_id, fpt),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# normalize_kpi_name
# ---------------------------------------------------------------------------


def test_normalize_strips_trailing_parenthetical() -> None:
    assert normalize_kpi_name("Monthly ARPAC (USD)") == "monthly arpac"
    assert normalize_kpi_name("ROE (annualized, consolidated)") == "roe"
    assert normalize_kpi_name("Risk-adjusted NIM (NIM minus cost of risk)") == "risk-adjusted nim"


def test_normalize_collapses_whitespace_and_case() -> None:
    assert normalize_kpi_name("  Monthly   ARPAC  ") == "monthly arpac"


def test_normalize_strips_nested_trailing_parentheticals() -> None:
    # Two stacked trailing qualifiers both peel off.
    assert normalize_kpi_name("ROE (annualized) (consolidated)") == "roe"


def test_normalize_keeps_distinct_metrics_distinct() -> None:
    # "NIM" must NOT collapse onto "Risk-adjusted NIM ...".
    assert normalize_kpi_name("NIM") != normalize_kpi_name(
        "Risk-adjusted NIM (NIM minus cost of risk)"
    )


# ---------------------------------------------------------------------------
# resolve_kpi_definition_name
# ---------------------------------------------------------------------------


def test_short_label_resolves_to_richest_canonical_definition() -> None:
    conn = _make_db()
    canonical = _add_def(conn, "NU", "Monthly ARPAC (USD)")
    duplicate = _add_def(conn, "NU", "Monthly ARPAC")
    _add_facts(conn, "NU", canonical, _QUARTER_ENDS)  # 12 obs
    _add_facts(conn, "NU", duplicate, _QUARTER_ENDS[5:7])  # 2 stray obs

    # The short holdings label must reach the 12-obs canonical series.
    assert resolve_kpi_definition_name(conn, "NU", "Monthly ARPAC") == "Monthly ARPAC (USD)"
    # The canonical label resolves to itself.
    assert resolve_kpi_definition_name(conn, "NU", "Monthly ARPAC (USD)") == "Monthly ARPAC (USD)"


def test_richest_wins_even_when_exact_duplicate_present() -> None:
    """Exactness is only a tie-breaker — observation count dominates."""
    conn = _make_db()
    rich = _add_def(conn, "NU", "ROE (annualized, consolidated)")
    sparse_exact = _add_def(conn, "NU", "ROE")
    _add_facts(conn, "NU", rich, _QUARTER_ENDS)  # 12 obs
    _add_facts(conn, "NU", sparse_exact, _QUARTER_ENDS[:1])  # 1 obs, exact name
    assert resolve_kpi_definition_name(conn, "NU", "ROE") == "ROE (annualized, consolidated)"


def test_exactness_breaks_ties_at_equal_obs() -> None:
    conn = _make_db()
    exact = _add_def(conn, "NU", "Activity Rate")
    qualified = _add_def(conn, "NU", "Activity Rate (consolidated)")
    _add_facts(conn, "NU", exact, _QUARTER_ENDS[:3])
    _add_facts(conn, "NU", qualified, _QUARTER_ENDS[3:6])  # same count (3)
    assert resolve_kpi_definition_name(conn, "NU", "Activity Rate") == "Activity Rate"


def test_unrelated_label_does_not_match() -> None:
    conn = _make_db()
    nim = _add_def(conn, "NU", "Risk-adjusted NIM (NIM minus cost of risk)")
    _add_facts(conn, "NU", nim, _QUARTER_ENDS)
    # "NIM" is a different metric — must not borrow the risk-adjusted series.
    assert resolve_kpi_definition_name(conn, "NU", "NIM") is None


def test_resolution_is_ticker_scoped() -> None:
    conn = _make_db()
    other = _add_def(conn, "MELI", "Monthly ARPAC (USD)")
    _add_facts(conn, "MELI", other, _QUARTER_ENDS)
    assert resolve_kpi_definition_name(conn, "NU", "Monthly ARPAC") is None


def test_definition_without_facts_is_not_resolvable() -> None:
    """A label that only matches a factless definition resolves to None — the
    consumer then renders/evaluates an empty series, same as before."""
    conn = _make_db()
    _add_def(conn, "NU", "Monthly ARPAC (USD)")  # no facts inserted
    assert resolve_kpi_definition_name(conn, "NU", "Monthly ARPAC") is None


# ---------------------------------------------------------------------------
# period_types — richness is measured over the rows the caller will read.
# ---------------------------------------------------------------------------


def test_period_types_scopes_richness_count() -> None:
    """The quarterly chart loader and the all-period break-rule paths can pick
    different definitions for the same label when richness differs by cadence.

    "Revenue" has 4 quarterly facts; "Revenue (GAAP)" has 8 annual facts only.
    Counting every period (break-rule paths) picks the annual-rich definition;
    counting quarterly buckets (chart loader) picks the quarterly one — and the
    annual-only definition, having zero quarterly rows, is not even a candidate.
    """
    conn = _make_db()
    quarterly = _add_def(conn, "X", "Revenue")
    annual = _add_def(conn, "X", "Revenue (GAAP)")
    _add_facts(conn, "X", quarterly, _QUARTER_ENDS[:4])  # 4 quarterly
    _add_facts(
        conn,
        "X",
        annual,
        [f"{y}-12-31" for y in range(2016, 2024)],  # 8 annual
        period_type="FY",
    )

    # All-period count (default, break-rule paths): annual-rich def wins.
    assert resolve_kpi_definition_name(conn, "X", "Revenue") == "Revenue (GAAP)"
    # Quarterly-scoped count (chart loader): the quarterly def wins.
    assert (
        resolve_kpi_definition_name(conn, "X", "Revenue", period_types=QUARTERLY_FACT_PERIOD_TYPES)
        == "Revenue"
    )


def test_quarterly_scope_ignores_annual_only_definition() -> None:
    """With the quarterly scope, a definition that has only annual facts is never
    returned (it contributes zero quarterly observations)."""
    conn = _make_db()
    annual = _add_def(conn, "X", "Free Cash Flow (annual)")
    _add_facts(conn, "X", annual, [f"{y}-12-31" for y in range(2018, 2024)], period_type="FY")
    assert (
        resolve_kpi_definition_name(
            conn, "X", "Free Cash Flow", period_types=QUARTERLY_FACT_PERIOD_TYPES
        )
        is None
    )
    # …but the all-period break-rule scope still finds it.
    assert resolve_kpi_definition_name(conn, "X", "Free Cash Flow") == "Free Cash Flow (annual)"


# ---------------------------------------------------------------------------
# canonical_metric_name — the WRITE-path canonicalizer.
#
# Governing invariant: a FALSE MERGE of two distinct metrics is worse than a
# duplicate. So it collapses only unit/casing/whitespace surface variants, keeps
# semantic qualifiers, and refuses to merge across incompatible unit families.
# ---------------------------------------------------------------------------


def test_mints_clean_name_when_no_definition_exists() -> None:
    """An unseen metric mints a whitespace-cleaned name; content is preserved."""
    conn = _make_db()
    assert canonical_metric_name(conn, "X", "  Total   bookings  ", Unit.MILLIONS) == (
        "Total bookings"
    )
    # Newlines/tabs from a sloppy extractor collapse too.
    assert canonical_metric_name(conn, "X", "Net\trevenue\n(GAAP)", Unit.MILLIONS) == (
        "Net revenue (GAAP)"
    )


def test_synonym_collapse_case_whitespace_and_unit_parenthetical() -> None:
    """Casing, whitespace, and a UNIT-ONLY trailing parenthetical all collapse
    onto the one existing definition rather than minting a near-duplicate."""
    conn = _make_db()
    _add_def(conn, "NU", "Monthly ARPAC")
    for variant in (
        "monthly arpac",
        "  Monthly   ARPAC ",
        "Monthly ARPAC (USD)",
        "Monthly ARPAC (US$)",
        "Monthly ARPAC (in USD)",
    ):
        assert canonical_metric_name(conn, "NU", variant, Unit.ACTUAL) == "Monthly ARPAC"


def test_unit_suffix_stripping_bare_and_parenthetical() -> None:
    """A bare or parenthetical unit suffix is stripped from the match key, so
    'Gross margin %' / 'Gross margin (%)' reuse the existing 'Gross margin'."""
    conn = _make_db()
    _add_def(conn, "X", "Gross margin", unit="percent")
    for variant in ("Gross margin %", "Gross margin (%)", "gross margin percent"):
        assert canonical_metric_name(conn, "X", variant, Unit.PERCENT) == "Gross margin"
    # And in the other direction: the stored name carries the unit suffix, the
    # short label still resolves to it.
    conn2 = _make_db()
    _add_def(conn2, "X", "Net interest margin (bps)", unit="bps")
    assert canonical_metric_name(conn2, "X", "Net interest margin", Unit.BPS) == (
        "Net interest margin (bps)"
    )


def test_reuses_factless_definition() -> None:
    """Unlike the read resolver (which ignores factless definitions), the write
    path reuses a not-yet-populated definition so capture-all doesn't re-mint it
    on every encounter."""
    conn = _make_db()
    _add_def(conn, "X", "Total bookings", unit="millions")  # no facts
    assert canonical_metric_name(conn, "X", "total bookings", Unit.MILLIONS) == "Total bookings"


def test_consolidates_onto_richest_canonical_even_over_exact_surface() -> None:
    """When several normalized-equal definitions exist, the value routes to the
    one carrying the MOST facts — even when a sparse duplicate matches the label
    exactly — mirroring the read resolver's defragmentation."""
    conn = _make_db()
    rich = _add_def(conn, "NU", "Monthly ARPAC (USD)", unit="millions")
    sparse = _add_def(conn, "NU", "Monthly ARPAC", unit="millions")
    _add_facts(conn, "NU", rich, _QUARTER_ENDS)  # 12
    _add_facts(conn, "NU", sparse, _QUARTER_ENDS[:2])  # 2, exact-surface to the label
    assert canonical_metric_name(conn, "NU", "Monthly ARPAC", Unit.MILLIONS) == (
        "Monthly ARPAC (USD)"
    )


def test_no_false_merge_distinct_stems() -> None:
    """A different metric name never borrows an existing series."""
    conn = _make_db()
    _add_def(conn, "NU", "NIM", unit="percent")
    assert canonical_metric_name(conn, "NU", "Risk-adjusted NIM", Unit.PERCENT) == (
        "Risk-adjusted NIM"
    )


def test_no_false_merge_semantic_qualifier_kept() -> None:
    """'(gross)' and '(net)' are SEMANTIC qualifiers — kept in the match key — so
    'Loans (net)' is minted distinct from an existing 'Loans (gross)'."""
    conn = _make_db()
    _add_def(conn, "X", "Loans (gross)", unit="millions")
    assert canonical_metric_name(conn, "X", "Loans (net)", Unit.MILLIONS) == "Loans (net)"


def test_no_false_merge_across_unit_families() -> None:
    """Two labels with the SAME match key but incompatible unit families do NOT
    merge — a dollar LEVEL never absorbs a percentage RATE that shares a stem."""
    conn = _make_db()
    _add_def(conn, "X", "Revenue USD", unit="millions")  # key -> 'revenue'
    # Incoming 'Revenue %' keys to 'revenue' too, but PERCENT ⟂ MILLIONS family.
    assert canonical_metric_name(conn, "X", "Revenue %", Unit.PERCENT) == "Revenue %"


def test_same_unit_family_does_merge() -> None:
    """Control for the family guard: a magnitude value joins a magnitude
    definition with the same key even across different scales."""
    conn = _make_db()
    _add_def(conn, "X", "Revenue USD", unit="millions")
    assert canonical_metric_name(conn, "X", "Revenue $", Unit.BILLIONS) == "Revenue USD"


def test_no_false_merge_on_identical_surface_across_unit_families() -> None:
    """The bare-collision seam: when the incoming label is SURFACE-IDENTICAL to an
    existing definition of an incompatible unit family, the unit gate empties the
    candidate set — but returning the bare name would let find_or_create (keyed on
    (ticker, name), unit absent) merge a COUNT metric onto the dollar definition.
    The minted name must be disambiguated so the split survives to persistence.
    The pre-fix test only covered DISTINCT surfaces ('Revenue %' vs 'Revenue USD')
    and was blind to this case."""
    conn = _make_db()
    _add_def(conn, "X", "Deposits", unit="millions")  # a dollar LEVEL
    # Incoming COUNT 'Deposits' shares the exact surface AND match key, but COUNT ⟂
    # MILLIONS — it must NOT come back as the bare 'Deposits' (which would collide).
    out = canonical_metric_name(conn, "X", "Deposits", Unit.COUNT)
    assert out != "Deposits"
    assert out.startswith("Deposits (")  # family-disambiguated, e.g. 'Deposits (count)'


def test_disambiguated_name_is_stable_for_reencounter() -> None:
    """Disambiguation is deterministic: a second capture of the same (label, unit)
    re-derives the SAME unit-safe name, so find_or_create reuses one row rather
    than minting a fresh duplicate each pass."""
    conn = _make_db()
    _add_def(conn, "X", "Deposits", unit="millions")
    first = canonical_metric_name(conn, "X", "Deposits", Unit.COUNT)
    # Register the disambiguated row the first capture would create, then re-run.
    _add_def(conn, "X", first, unit="count", origin="capture")
    second = canonical_metric_name(conn, "X", "Deposits", Unit.COUNT)
    assert second == first


def test_compatible_magnitude_still_merges_on_identical_surface() -> None:
    """The disambiguation is unit-GATED, not surface-gated: a same-family value
    with an identical surface still reuses the existing row (no needless split)."""
    conn = _make_db()
    _add_def(conn, "X", "Deposits", unit="millions")
    assert canonical_metric_name(conn, "X", "Deposits", Unit.BILLIONS) == "Deposits"


def test_canonicalization_is_ticker_scoped() -> None:
    conn = _make_db()
    _add_def(conn, "MELI", "Monthly ARPAC", unit="millions")
    # NU has no such definition — mint, don't borrow MELI's.
    assert canonical_metric_name(conn, "NU", "Monthly ARPAC", Unit.MILLIONS) == "Monthly ARPAC"


def test_analyst_origin_preferred_over_capture_at_equal_richness() -> None:
    """At equal fact counts, the curated analyst definition is chosen as canonical
    over a capture-minted one — even when the capture row matches the label's
    surface exactly."""
    conn = _make_db()
    analyst = _add_def(conn, "NU", "ARPAC", unit="millions", origin="analyst")
    capture = _add_def(conn, "NU", "ARPAC (USD)", unit="millions", origin="capture")
    _add_facts(conn, "NU", analyst, _QUARTER_ENDS[:1])
    _add_facts(conn, "NU", capture, _QUARTER_ENDS[:1])  # same count
    assert canonical_metric_name(conn, "NU", "ARPAC (USD)", Unit.MILLIONS) == "ARPAC"


def test_works_without_origin_column() -> None:
    """Schema-defensive: a pre-0113 DB (no definition_origin) still canonicalizes;
    origin-aware tie-breaking is simply skipped."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE kpi_definitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker VARCHAR NOT NULL, name VARCHAR NOT NULL, unit VARCHAR
        );
        CREATE TABLE kpi_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ticker VARCHAR NOT NULL,
            period_end VARCHAR NOT NULL, value NUMERIC, unit VARCHAR,
            kpi_definition_id INTEGER NOT NULL, fiscal_period_type VARCHAR NOT NULL,
            source_doc_id INTEGER NOT NULL DEFAULT 1
        );
        """
    )
    conn.execute("INSERT INTO kpi_definitions (ticker, name, unit) VALUES ('X', 'GMV', 'millions')")
    assert canonical_metric_name(conn, "X", "GMV (USD)", Unit.MILLIONS) == "GMV"


# ---------------------------------------------------------------------------
# Phase 3 (bottoms-up metrics engine) -- engine_formula_definition
# ---------------------------------------------------------------------------


def test_engine_formula_definition_resolves_a_registry_formula_key() -> None:
    text = engine_formula_definition("pe_ttm")
    assert text is not None
    assert "price / eps_diluted_ttm" in text
    assert "live spot quote" in text


def test_engine_formula_definition_none_for_non_registry_name() -> None:
    assert engine_formula_definition("Monthly ARPAC (USD)") is None
    assert engine_formula_definition("not_a_real_formula") is None
