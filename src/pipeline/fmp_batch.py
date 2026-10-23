"""Ordered extraction of registered FMP documents with per-document outcomes.

Extractors retain their document commit boundaries. This operation rolls back
pending writes before reporting a failed document; it is not a batch transaction.
The supplied-record, commit=False refresh-cache path remains caller-owned.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from compute.as_reported import extract_as_reported_facts
from compute.balance_sheet import extract_balance_sheet_facts
from compute.cashflow import extract_cashflow_facts
from compute.income_statement import extract_income_statement_facts
from compute.segment_oi_10k import extract_segment_oi_facts
from compute.segments import extract_segment_facts
from compute.segments_nu import extract_nu_segment_facts

Extractor = Callable[[sqlite3.Connection, int, Path], int]
Document = tuple[int, str, str]

_DISPATCH: dict[str, Extractor] = {
    "fmp_income_statement": extract_income_statement_facts,
    "fmp_balance_sheet": extract_balance_sheet_facts,
    "fmp_cashflow": extract_cashflow_facts,
    "fmp_segment_product": extract_segment_facts,
    "fmp_segment_geographic": extract_segment_facts,
    "fmp_as_reported_income": extract_as_reported_facts,
    "fmp_as_reported_balance": extract_as_reported_facts,
    "fmp_as_reported_cashflow": extract_as_reported_facts,
    "fmp_as_reported_financial": extract_as_reported_facts,
    "fmp_10k_json": extract_segment_oi_facts,
    "fmp_10q_json": extract_segment_oi_facts,
}
# NU's IFRS segment note needs its own labeler.
_TICKER_OVERRIDE: dict[tuple[str, str], Extractor] = {
    ("NU", "fmp_10k_json"): extract_nu_segment_facts,
    ("NU", "fmp_10q_json"): extract_nu_segment_facts,
}
SUPPORTED_DOC_TYPES = tuple(_DISPATCH)


@dataclass(frozen=True)
class DocumentOutcome:
    document_id: int
    ticker: str
    doc_type: str
    rows_inserted: int = 0
    error: Exception | None = None


@dataclass(frozen=True)
class BatchResult:
    documents: int
    rows_inserted: int
    failed: int

    @property
    def succeeded(self) -> int:
        return self.documents - self.failed


def select_documents(
    conn: sqlite3.Connection,
    tickers: Sequence[str],
    doc_types: Sequence[str] = SUPPORTED_DOC_TYPES,
) -> list[Document]:
    """Select registered sources; commit revenue before dependent segments."""
    if not tickers or not doc_types:
        return []
    placeholders_t = ",".join("?" for _ in tickers)
    placeholders_d = ",".join("?" for _ in doc_types)
    rows = conn.execute(
        f"SELECT id, ticker, doc_type FROM documents "
        f"WHERE ticker IN ({placeholders_t}) AND doc_type IN ({placeholders_d}) "
        "ORDER BY ticker, CASE doc_type "
        "WHEN 'fmp_income_statement' THEN 0 "
        "WHEN 'fmp_as_reported_income' THEN 1 ELSE 2 END, id",
        (*(ticker.upper() for ticker in tickers), *doc_types),
    )
    return [(int(row["id"]), str(row["ticker"]), str(row["doc_type"])) for row in rows]


def _resolve_extractor(ticker: str, doc_type: str) -> Extractor:
    return _TICKER_OVERRIDE.get((ticker.upper(), doc_type)) or _DISPATCH[doc_type]


def extract_documents(
    conn: sqlite3.Connection,
    documents: Sequence[Document],
    project_root: Path,
    *,
    report: Callable[[DocumentOutcome], None] | None = None,
) -> BatchResult:
    """Extract the selected order; input failures continue, database failures halt.

    A report callback may commit accounting. It only runs after extraction has
    committed or pending failed writes have been rolled back. Its own database
    failure halts the batch, and must not replace an original extraction error.
    """
    inserted = failed = 0
    for doc_id, ticker, doc_type in documents:
        try:
            count = _resolve_extractor(ticker, doc_type)(conn, doc_id, project_root)
            if count < 0:
                raise ValueError("extractor returned a negative row count")
            conn.commit()
        except sqlite3.Error as exc:
            try:
                conn.rollback()
                if report is not None:
                    report(DocumentOutcome(doc_id, ticker, doc_type, error=exc))
            except sqlite3.Error:
                # Preserve the extraction error even if accounting/rollback fails.
                with suppress(sqlite3.Error):
                    conn.rollback()
            raise
        except (ValueError, OSError, KeyError) as exc:
            conn.rollback()
            failed += 1
            if report is not None:
                report(DocumentOutcome(doc_id, ticker, doc_type, error=exc))
        else:
            inserted += count
            if report is not None:
                report(DocumentOutcome(doc_id, ticker, doc_type, rows_inserted=count))
    return BatchResult(len(documents), inserted, failed)
