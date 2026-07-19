# These tests exercise internal extraction seams (_build_manifest, _llm_extract, …).
# pyright: reportPrivateUsage=false
"""Tests for compute.kpi_extract_summaries value parsing.

Regression for the VEEV extraction abort: Haiku returned a non-numeric KPI value
("Q1 2026 ... InvalidOperation: ConversionSyntax"), and `_build_manifest` caught
only (TypeError, ValueError) — but `Decimal("N/A")` raises decimal.InvalidOperation
(an ArithmeticError), so one bad value aborted the whole ticker. `parse_decimal_value`
must degrade at the per-KPI scope (skip the value, keep extracting).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

import compute.kpi_extract_summaries as kes
from compute.kpi_extract_summaries import (
    _build_manifest,
    _canonical_units_from_holdings,
    _ensure_summary_document_row,
    _fact_units_by_name,
    _llm_extract,
    _period_end,
    parse_decimal_value,
)
from models.facts import FiscalPeriodType, Unit
from models.validation import Severity, ValidationRule


def test_parse_decimal_value_parses_clean_numbers() -> None:
    assert parse_decimal_value(12.5) == Decimal("12.5")
    assert parse_decimal_value("17.8") == Decimal("17.8")
    assert parse_decimal_value(0) == Decimal("0")
    assert parse_decimal_value("-3.2") == Decimal("-3.2")


def test_parse_decimal_value_returns_none_for_unparseable() -> None:
    # Each of these raised decimal.InvalidOperation (or TypeError) and used to
    # abort the ticker; they must now skip just that KPI.
    for bad in ("N/A", "~17%", "n.m.", "", "1,200", "TBD", None):
        assert parse_decimal_value(bad) is None


# --- Prompt unit vocabulary (problem 1: out-of-enum unit tokens) --------------


def test_llm_extract_prompt_offers_only_valid_unit_enum_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prompt must not instruct the model to return tokens absent from the
    `Unit` enum. The old prompt offered "usd" and "ratio_per_unit", which
    `Unit(...)` then coerced to ACTUAL — storing dollar KPIs as raw `actual` and
    breaking every break-rule declared in `millions`."""
    captured: dict[str, str] = {}

    def fake_call(prompt: str, model: str | None = None) -> str:
        captured["prompt"] = prompt
        return '{"GMV": {"value": 1200000000, "unit": "actual", "confidence": 0.9}}'

    monkeypatch.setattr(kes, "_call_claude", fake_call)
    out = _llm_extract("MELI", "Q1 2026", ["GMV"], "GMV reached $1.2 billion this quarter.")

    prompt = captured["prompt"]
    # The invalid tokens are gone entirely.
    assert "ratio_per_unit" not in prompt
    assert '"usd"' not in prompt
    # Every offered token is a real Unit enum value.
    for token in ('"percent"', '"actual"', '"count"', '"ratio"', '"bps"'):
        assert token in prompt
        assert token.strip('"') in {u.value for u in Unit}
    # The money convention is spelled out with a full-figure example.
    assert "1200000000" in prompt
    # Sanity: the canned response still parses through the existing path.
    assert out == {"GMV": {"value": 1200000000, "unit": "actual", "confidence": 0.9}}


# --- Canonical-unit mapping from holdings break-rules (problem 2) -------------


def _holdings(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {"break_rules": [], "business_model_rules": []}
    base.update(kw)
    return base


def test_canonical_units_matches_break_rule_unit_across_name_spellings() -> None:
    """The break-rule spells the metric differently from the tier_1 label, but
    both normalize equal — the canonical unit still attaches. This is the RBRK
    net-new-ARR case ("($)" tier_1 label vs "(USD millions)" rule label)."""
    holdings = _holdings(
        break_rules=[
            {
                "rule_id": "r1",
                "kpi_name": "Net New Subscription ARR (USD millions)",
                "comparator": "lt",
                "threshold": 80,
                "unit": "millions",
                "narrative": "x",
            }
        ]
    )
    out = _canonical_units_from_holdings(holdings, ["Net new subscription ARR ($)"])
    assert out == {"Net new subscription ARR ($)": Unit.MILLIONS}


def test_canonical_units_skips_non_enum_rule_units() -> None:
    """A rule unit outside the `Unit` enum (e.g. KVYO's derived "percent_decel")
    has no dimensional meaning for reconciliation and is skipped, leaving the
    metric with no canonical override (LLM unit used as-is)."""
    holdings = _holdings(
        break_rules=[
            {
                "rule_id": "kvyo_revenue_decel",
                "kpi_name": "Revenue YoY Growth",
                "comparator": "lt",
                "threshold": 50,
                "unit": "percent_decel",
                "narrative": "x",
            }
        ]
    )
    out = _canonical_units_from_holdings(holdings, ["Revenue YoY Growth"])
    assert out == {}


def test_canonical_units_reads_both_rule_arrays_and_ignores_unlisted_names() -> None:
    holdings = _holdings(
        break_rules=[
            {
                "rule_id": "a",
                "kpi_name": "Gross margin",
                "comparator": "lt",
                "threshold": 70,
                "unit": "percent",
                "narrative": "x",
            },
        ],
        business_model_rules=[
            {
                "rule_id": "b",
                "kpi_name": "Monthly ARPAC (USD)",
                "comparator": "lt",
                "threshold": 9,
                "unit": "actual",
                "narrative": "x",
            },
        ],
    )
    out = _canonical_units_from_holdings(
        holdings, ["Gross margin", "Monthly ARPAC", "Untracked KPI"]
    )
    assert out == {"Gross margin": Unit.PERCENT, "Monthly ARPAC": Unit.ACTUAL}
    assert "Untracked KPI" not in out


def test_canonical_units_drops_cross_family_rule_unit_vs_facts() -> None:
    """A break-rule on a dollar LEVEL phrased as a YoY-growth `percent` (the AWS
    RPO shape) must NOT become the metric's canonical value unit when the facts
    say it is a `actual` dollar magnitude. The rule unit is a *comparison* unit
    and is dropped; the LLM's extracted unit is used unchanged downstream."""
    holdings = _holdings(
        break_rules=[
            {
                "rule_id": "deposits_growth",
                "kpi_name": "Total deposits",
                "comparator": "lt",
                "threshold": 10,
                "unit": "percent",  # "deposits growth < 10%" — a rate on a $ level
                "narrative": "x",
            }
        ]
    )
    out = _canonical_units_from_holdings(
        holdings, ["Total deposits"], fact_units={"Total deposits": Unit.ACTUAL}
    )
    assert out == {}


def test_canonical_units_keeps_same_family_rule_unit_with_facts() -> None:
    """Within-family rule units are still adopted even when facts are present —
    the #320 normalization (raw dollars -> the rule's `millions`) must survive."""
    holdings = _holdings(
        break_rules=[
            {
                "rule_id": "r1",
                "kpi_name": "Net New Subscription ARR (USD millions)",
                "comparator": "lt",
                "threshold": 80,
                "unit": "millions",
                "narrative": "x",
            }
        ]
    )
    out = _canonical_units_from_holdings(
        holdings,
        ["Net new subscription ARR ($)"],
        fact_units={"Net new subscription ARR ($)": Unit.ACTUAL},
    )
    assert out == {"Net new subscription ARR ($)": Unit.MILLIONS}


def test_canonical_units_without_facts_adopts_rule_unit() -> None:
    """First extraction (no facts yet): the rule unit is adopted as before, so a
    genuine direct-threshold rule still normalizes the metric. The fact-based
    veto only kicks in once a recorded value unit exists."""
    holdings = _holdings(
        break_rules=[
            {
                "rule_id": "deposits_growth",
                "kpi_name": "Total deposits",
                "comparator": "lt",
                "threshold": 10,
                "unit": "percent",
                "narrative": "x",
            }
        ]
    )
    out = _canonical_units_from_holdings(holdings, ["Total deposits"])
    assert out == {"Total deposits": Unit.PERCENT}


def _facts_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE kpi_definitions ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, name TEXT, unit TEXT)"
    )
    conn.execute(
        "CREATE TABLE kpi_facts ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, kpi_definition_id INTEGER, "
        "unit TEXT, value REAL, period_end TEXT)"
    )
    return conn


def test_fact_units_by_name_returns_modal_unit_and_feeds_the_veto() -> None:
    conn = _facts_db()
    conn.execute(
        "INSERT INTO kpi_definitions (id, ticker, name, unit) "
        "VALUES (1, 'NU', 'Total deposits', 'actual')"
    )
    for v in (9.0e10, 1.0e11):
        conn.execute(
            "INSERT INTO kpi_facts (kpi_definition_id, unit, value, period_end) "
            "VALUES (1, 'actual', ?, '2026-03-31')",
            (v,),
        )

    fact_units = _fact_units_by_name(conn, "NU", ["Total deposits", "No facts metric"])
    assert fact_units == {"Total deposits": Unit.ACTUAL}

    holdings = _holdings(
        break_rules=[
            {
                "rule_id": "deposits_growth",
                "kpi_name": "Total deposits",
                "comparator": "lt",
                "threshold": 10,
                "unit": "percent",
                "narrative": "x",
            }
        ]
    )
    out = _canonical_units_from_holdings(holdings, ["Total deposits"], fact_units=fact_units)
    assert out == {}


def test_build_manifest_threads_canonical_units_onto_manifest() -> None:
    """`_build_manifest` carries the canonical-unit map onto the manifest so
    persist_manifest can reconcile; the extracted unit itself is left intact at
    build time (reconciliation happens at persist)."""
    extracted: dict[str, dict[str, object]] = {
        "Net new subscription ARR ($)": {"value": 115000000, "unit": "actual", "confidence": 0.9},
    }
    canonical = {"Net new subscription ARR ($)": Unit.MILLIONS}
    manifest = _build_manifest(
        "RBRK",
        datetime(2027, 1, 31),
        FiscalPeriodType.Q4,
        7140,
        extracted,
        canonical,
    )
    assert manifest.canonical_units == canonical
    # Build time keeps the extracted unit; reconciliation is persist's job.
    assert manifest.values[0].unit is Unit.ACTUAL
    assert manifest.values[0].value == Decimal("115000000")


def test_build_manifest_canonical_units_default_empty() -> None:
    """Called without a canonical map (back-compat), the manifest carries none."""
    extracted: dict[str, dict[str, object]] = {
        "GMV": {"value": 5, "unit": "actual", "confidence": 0.9}
    }
    manifest = _build_manifest("YY", datetime(2026, 3, 31), FiscalPeriodType.Q1, 1, extracted)
    assert manifest.canonical_units == {}


# --- Phase C: anchor-quote verification gate (§3.3) --------------------------


def test_build_manifest_without_source_text_keeps_legacy_locator() -> None:
    """Back-compat: no `source_text` passed (today's every-other-caller shape)
    -> the escape hatch, unchanged from before Phase C."""
    from models.facts import LegacyEscapeHatch

    extracted: dict[str, dict[str, object]] = {
        "GMV": {"value": 5, "source_excerpt": "GMV was $5 this quarter."}
    }
    manifest = _build_manifest("YY", datetime(2026, 3, 31), FiscalPeriodType.Q1, 1, extracted)
    assert isinstance(manifest.values[0].locator, LegacyEscapeHatch)


def test_build_manifest_verified_excerpt_upgrades_to_html_span() -> None:
    """A `source_excerpt` that IS verbatim in the summary text earns a real,
    click-through-able `html_span` locator anchored on the summary doc_id."""
    from models.facts import FactLocator, LocatorKind

    source_text = "Financial Highlights\nGMV reached $1.2 billion this quarter.\n"
    extracted: dict[str, dict[str, object]] = {
        "GMV": {
            "value": 1200000000,
            "source_excerpt": "GMV reached $1.2 billion this quarter.",
        }
    }
    manifest = _build_manifest(
        "MELI",
        datetime(2026, 3, 31),
        FiscalPeriodType.Q1,
        42,
        extracted,
        source_text=source_text,
    )
    loc = manifest.values[0].locator
    assert isinstance(loc, FactLocator)
    assert loc.effective_kind() == LocatorKind.HTML_SPAN
    assert loc.html_span is not None
    assert loc.html_span.doc_id == 42
    assert loc.verbatim_snippet == "GMV reached $1.2 billion this quarter."


def test_build_manifest_fabricated_excerpt_rejected_and_logged() -> None:
    """A `source_excerpt` that does NOT appear verbatim in the summary text
    (the hallucination case) must NEVER produce a renderable locator -- it
    demotes to the escape hatch AND logs a validation_issues row
    (rule=hallucinated_anchor). This is the guard-ratchet's required
    'a fabricated quote MUST be rejected' proof for the on-disk-summary path."""
    from models.facts import LegacyEscapeHatch

    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE validation_issues (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "run_id TEXT NOT NULL, source_doc_id INTEGER, ticker TEXT, "
        "severity TEXT NOT NULL, rule TEXT NOT NULL, raw_value TEXT, expected TEXT, "
        "raised_at TIMESTAMP NOT NULL, resolved_at TIMESTAMP);"
    )
    source_text = "Financial Highlights\nGMV reached $1.2 billion this quarter.\n"
    extracted: dict[str, dict[str, object]] = {
        "GMV": {
            "value": 1200000000,
            # Plausible but NOT a verbatim substring of source_text.
            "source_excerpt": "GMV surged past $1.2 billion, a new all-time record.",
        }
    }
    manifest = _build_manifest(
        "MELI",
        datetime(2026, 3, 31),
        FiscalPeriodType.Q1,
        42,
        extracted,
        conn=conn,
        source_text=source_text,
    )
    # The VALUE is still extracted -- never dropped for a bad anchor alone.
    assert manifest.values[0].value == Decimal("1200000000")
    assert isinstance(manifest.values[0].locator, LegacyEscapeHatch)

    issue = conn.execute(
        "SELECT source_doc_id, ticker, severity, rule, raw_value, expected FROM validation_issues"
    ).fetchone()
    assert issue is not None
    assert issue[0] == 42
    assert issue[1] == "MELI"
    assert issue[2] == Severity.WARN.value
    assert issue[3] == ValidationRule.HALLUCINATED_ANCHOR.value
    assert issue[4] == "GMV surged past $1.2 billion, a new all-time record."
    assert issue[5] == "GMV"
    conn.close()


# --- _ensure_summary_document_row: parent_document_id resolution + guard -----


def _documents_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT, source_type TEXT, doc_type TEXT, period_end TIMESTAMP,
            file_path TEXT, sha256 TEXT, fetched_at TIMESTAMP, fetch_status TEXT,
            raw_bytes_size INTEGER, parent_document_id INTEGER, source_quality_tier TEXT
        );
        CREATE TABLE validation_issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL, source_doc_id INTEGER, ticker TEXT,
            severity TEXT NOT NULL, rule TEXT NOT NULL, raw_value TEXT, expected TEXT,
            raised_at TIMESTAMP NOT NULL, resolved_at TIMESTAMP
        );
        """
    )
    return conn


def test_ensure_summary_document_row_resolves_parent_from_transcript(tmp_path: Path) -> None:
    conn = _documents_db()
    transcript_id = conn.execute(
        "INSERT INTO documents (ticker, source_type, doc_type, period_end, file_path, "
        "sha256, fetched_at, fetch_status, raw_bytes_size) "
        "VALUES ('NU', 'transcript_audio', 'earnings_call_transcript', "
        "'2023-03-31 00:00:00', 'x', 'x', '2026-05-19 01:45:16', 'ok', 1)"
    ).lastrowid
    conn.commit()

    summary_path = tmp_path / "NU_Q1_2023_summary.txt"
    summary_path.write_text("summary text", encoding="utf-8")

    doc_id = _ensure_summary_document_row(
        conn, "NU", datetime(2023, 3, 31), summary_path, "llm_summary"
    )

    row = conn.execute(
        "SELECT parent_document_id FROM documents WHERE id = ?", (doc_id,)
    ).fetchone()
    assert row["parent_document_id"] == transcript_id
    # A resolvable row must not trip the guard.
    assert conn.execute("SELECT COUNT(*) FROM validation_issues").fetchone()[0] == 0


def test_ensure_summary_document_row_flags_unresolvable_parent(tmp_path: Path) -> None:
    """No eligible primary document on file for this (ticker, period) — the row
    is still inserted (kpi_facts needs the FK) but parent_document_id stays NULL
    and the write-path guard logs a validation_issues row instead of silently
    dropping the provenance gap."""
    conn = _documents_db()
    summary_path = tmp_path / "AMAT_Q4_2025_summary.txt"
    summary_path.write_text("summary text", encoding="utf-8")

    doc_id = _ensure_summary_document_row(
        conn, "AMAT", datetime(2025, 12, 31), summary_path, "llm_summary"
    )

    row = conn.execute(
        "SELECT parent_document_id FROM documents WHERE id = ?", (doc_id,)
    ).fetchone()
    assert row["parent_document_id"] is None

    issue = conn.execute(
        "SELECT source_doc_id, ticker, severity, rule FROM validation_issues"
    ).fetchone()
    assert issue is not None
    assert issue["source_doc_id"] == doc_id
    assert issue["ticker"] == "AMAT"
    assert issue["severity"] == Severity.WARN.value
    assert issue["rule"] == ValidationRule.MISSING_FIELD.value


def test_period_end_calendar_fye_unaffected() -> None:
    """Ordinary Dec-FYE tickers use the plain quarter->month/day map."""
    assert _period_end("NVDA", 1, 2026) == datetime(2026, 3, 31)
    assert _period_end("NVDA", 4, 2026) == datetime(2026, 12, 31)


def test_period_end_jan_fye_rolls_only_q4() -> None:
    """RBRK/VEEV (Jan FYE): Q1-Q3 land in the filename's label year; only Q4
    (period-end month January) rolls into label_year + 1. Regression for the
    fiscal-period stamping drift audit — this module's own filename-label
    convention, not transcript_ingest.py's, must be preserved exactly."""
    assert _period_end("RBRK", 1, 2026) == datetime(2026, 4, 30)
    assert _period_end("RBRK", 3, 2026) == datetime(2026, 10, 31)
    assert _period_end("RBRK", 4, 2026) == datetime(2027, 1, 31)
    assert _period_end("VEEV", 1, 2025) == datetime(2025, 4, 30)
    assert _period_end("VEEV", 4, 2025) == datetime(2026, 1, 31)


def test_period_end_oct_fye_never_rolls() -> None:
    """AMAT/TOL (Oct FYE): Q1's period-end month is also January (like Jan-FYE's
    Q4), but must NOT roll over — all four quarters stay in the filename's
    label year. Before this fix, AMAT/TOL were absent from
    `_TICKER_QUARTER_PERIOD_END` entirely, so they fell through to the plain
    calendar-quarter map (stamping e.g. AMAT_Q4_2025 as 2025-12-31 instead of
    its true fiscal Q4 end 2025-10-31) — the root cause of 6 of the 10
    llm_extracted parent_document_id backfill orphans (#765)."""
    assert _period_end("AMAT", 4, 2025) == datetime(2025, 10, 31)
    assert _period_end("AMAT", 1, 2026) == datetime(2026, 1, 31)
    assert _period_end("TOL", 4, 2025) == datetime(2025, 10, 31)
    assert _period_end("TOL", 1, 2026) == datetime(2026, 1, 31)


def test_ensure_summary_document_row_is_idempotent_on_sha256(tmp_path: Path) -> None:
    """A second call for the same bytes returns the existing row and does not
    re-resolve or re-guard (matches the pre-existing sha256-keyed idempotence)."""
    conn = _documents_db()
    summary_path = tmp_path / "NU_Q1_2023_summary.txt"
    summary_path.write_text("summary text", encoding="utf-8")

    first_id = _ensure_summary_document_row(
        conn, "NU", datetime(2023, 3, 31), summary_path, "llm_summary"
    )
    second_id = _ensure_summary_document_row(
        conn, "NU", datetime(2023, 3, 31), summary_path, "llm_summary"
    )
    assert first_id == second_id
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
