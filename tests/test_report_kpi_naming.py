"""Pure KPI-name presentation helpers (`report.kpi_naming`).

These back the §2 ledger's clean-name-plus-definition display: the renderer
shows ``clean_kpi_name`` and the section builder composes the definition from
``kpi_qualifier``, so the parenthetical never appears in both the name and the
definition line. Pinning both halves here keeps that split from drifting.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from report.kpi_naming import clean_kpi_name, kpi_qualifier  # noqa: E402


def test_clean_name_strips_trailing_parenthetical() -> None:
    assert clean_kpi_name("ROE (annualized, consolidated)") == "ROE"


def test_clean_name_strips_interior_parenthetical_and_collapses_ws() -> None:
    assert clean_kpi_name("Operating margin (GAAP) consolidated") == "Operating margin consolidated"


def test_clean_name_keeps_interior_separator() -> None:
    # The parenthetical is removed; the em-dash separating the two clauses is
    # interior, so it stays.
    assert (
        clean_kpi_name("NPL by product (cc vs pl) — early warning")
        == "NPL by product — early warning"
    )


def test_clean_name_trims_dangling_trailing_dash() -> None:
    assert clean_kpi_name("Risk metric — (legacy)") == "Risk metric"


def test_clean_name_passthrough_when_no_parenthetical() -> None:
    assert clean_kpi_name("Cost-to-serve trajectory") == "Cost-to-serve trajectory"


def test_clean_name_falls_back_when_only_a_parenthetical() -> None:
    # Removal would empty the string — keep the original rather than render blank.
    assert clean_kpi_name("(USD)") == "(USD)"


def test_qualifier_extracts_parenthetical() -> None:
    assert kpi_qualifier("Risk-adjusted NIM (NIM minus cost of risk)") == "NIM minus cost of risk"


def test_qualifier_joins_multiple_parentheticals() -> None:
    assert kpi_qualifier("Operating margin (GAAP) (consolidated)") == "GAAP · consolidated"


def test_qualifier_none_when_absent() -> None:
    assert kpi_qualifier("Total customers") is None


def test_qualifier_ignores_empty_parens() -> None:
    assert kpi_qualifier("Foo ()") is None
