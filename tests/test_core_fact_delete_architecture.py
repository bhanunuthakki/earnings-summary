"""Ratchet destructive writes away from canonical evidence and fact records.

Alembic migrations are intentionally excluded: their historical destructive
steps are reviewable, versioned compatibility work.  Production source is not.
The three source exceptions below predate the append-only lifecycle and are
explicit debt; execution scripts have an empty allowlist.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_DELETE = re.compile(r"DELETE\s+FROM\s+([^\s;]+)", re.IGNORECASE)
_CORE_TABLES = frozenset(
    {
        "documents",
        "transcripts",
        "filing_sections",
        "financial_facts",
        "kpi_facts",
        "kpi_definitions",
    }
)
_DYNAMIC_CORE_TABLES = {
    "{SECTIONS_TABLE}": "filing_sections",
    "{ITEMS_TABLE}": "filing_sections",
}

# These are documented pre-0214 compatibility debt.  New source deletes must
# not be added here; their removal is tracked by the data-infrastructure plan.
_SRC_AUDITED_DELETE_DEBT = {
    ("src/compute/transcript_ingest.py", "documents"),
    ("src/compute/transcript_ingest.py", "transcripts"),
    ("src/pipeline/kpi_persistence.py", "kpi_facts"),
    ("src/ir_pipeline/ingest.py", "kpi_facts"),
    ("src/filings/store.py", "filing_sections"),
    ("src/filings/section_items.py", "filing_sections"),
}
_EXECUTION_AUDITED_DELETE_DEBT: frozenset[tuple[str, str]] = frozenset()


def _core_deletes(directory: Path) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in directory.rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        for match in _DELETE.finditer(path.read_text(encoding="utf-8")):
            token = match.group(1).strip('"`[]').upper()
            table = _DYNAMIC_CORE_TABLES.get(token, token.lower())
            if table in _CORE_TABLES:
                found.add((relative, table))
    return found


def test_production_core_fact_and_evidence_deletes_are_an_explicit_ratchet() -> None:
    source_deletes = _core_deletes(ROOT / "src")
    execution_deletes = _core_deletes(ROOT / "execution")
    assert execution_deletes == _EXECUTION_AUDITED_DELETE_DEBT
    assert source_deletes == _SRC_AUDITED_DELETE_DEBT
