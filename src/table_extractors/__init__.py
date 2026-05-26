"""Per-table-kind extractors for SEC filing tables.

Each module exports a single `extract(...) -> ExtractionOutcome` function
that conforms to the contract in `base.py`. The top-level orchestrator
at `src/document_table_extractor.py` dispatches to these modules.

See `directives/document_tables_design.md` for the full table-kind
taxonomy, schema proposals, and extraction pipeline architecture.

MVP-shipped extractors:
    customer_concentration  — LLM (Haiku) over narrative; populates
                              `customer_concentrations` (created in 0040).
    lease_commitments       — deterministic FMP XBRL-section parser;
                              populates `lease_commitments` (created in 0047).

Stub modules (raise NotImplementedError; documented contract is the spec
for the next PR):
    goodwill_rollforward, debt_maturities, stock_award_activity,
    geographic_revenue, geographic_assets, supplier_concentration,
    related_party_transactions, contractual_obligations,
    restructuring_programs, pipeline_drug_candidates,
    outstanding_equity_awards.
"""

from __future__ import annotations

from table_extractors.base import (
    ExtractionOutcome,
    ExtractorContract,
    XbrlRow,
    iter_xbrl_table,
    parse_units,
)

__all__ = [
    "ExtractionOutcome",
    "ExtractorContract",
    "XbrlRow",
    "iter_xbrl_table",
    "parse_units",
]
