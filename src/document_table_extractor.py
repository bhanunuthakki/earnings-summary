"""Top-level orchestrator for document table extraction.

Dispatches to per-table-kind extractors under `src/table_extractors/`.
Each extractor follows the contract documented in
`directives/document_tables_design.md` §4.2.

This module's responsibilities:
  1. Resolve the FMP form_10k JSON path + the cached SEC text path for a
     (ticker, fiscal_year) pair.
  2. Look up the source `documents.id` (used as `source_doc_id` in every
     typed row + extractions row).
  3. Load each input once, so two extractors can share it without re-reading.
  4. Dispatch to the requested extractor(s).
  5. Aggregate ExtractionOutcome objects for the CLI / caller.

CLI: see `execution/extract_document_tables.py`.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from table_extractors import customer_concentration as cc_module
from table_extractors import lease_commitments as lc_module
from table_extractors.base import ExtractionOutcome

log = logging.getLogger(__name__)


@dataclass(slots=True)
class _ExtractorEntry:
    table_kind: str
    extract_fn: Callable[..., ExtractionOutcome]
    needs_fmp: bool = True
    needs_sec_text: bool = False


# Registry of implemented extractors. Stubs from `_stubs.py` are NOT
# registered here — they aren't dispatch targets until they ship.
_REGISTRY: dict[str, _ExtractorEntry] = {
    "customer_concentration": _ExtractorEntry(
        table_kind="customer_concentration",
        extract_fn=cc_module.extract,
        needs_fmp=True,
        needs_sec_text=False,
    ),
    "lease_commitments": _ExtractorEntry(
        table_kind="lease_commitments_ladder",
        extract_fn=lc_module.extract,
        needs_fmp=True,
        needs_sec_text=False,
    ),
}


def registered_table_kinds() -> list[str]:
    """Names that --table-kind on the CLI accepts."""
    return sorted(_REGISTRY.keys())


def extract_for_ticker(
    *,
    ticker: str,
    fiscal_year: int | None = None,
    table_kinds: Sequence[str] | None = None,
    repo_root: Path,
    db_path: Path | None = None,
) -> list[ExtractionOutcome]:
    """Run one or more extractors for a (ticker, fiscal_year) pair.

    Args:
        ticker: Equity ticker symbol (case-insensitive; upcased internally).
        fiscal_year: Specific FY to extract; None = latest available in FMP cache.
        table_kinds: Which extractors to run; None = all registered.
        repo_root: Repo root; defaults are derived from it.
        db_path: Override for portfolio.db location (default: repo_root/data/portfolio.db).

    Returns:
        One ExtractionOutcome per (table_kind, ticker, fiscal_year) attempted.
    """
    ticker_u = ticker.upper()
    db = db_path or (repo_root / "data" / "portfolio.db")
    fmp_dir = repo_root / "data" / "historical" / "fmp"

    requested = list(table_kinds) if table_kinds else list(_REGISTRY.keys())
    bad = [tk for tk in requested if tk not in _REGISTRY]
    if bad:
        raise ValueError(f"unknown table_kind(s): {bad}; known={registered_table_kinds()}")

    fmp_payload, resolved_fy = _load_fmp_payload(
        fmp_dir=fmp_dir, ticker=ticker_u, fiscal_year=fiscal_year
    )
    if fmp_payload is None:
        return [
            ExtractionOutcome(
                table_kind=_REGISTRY[tk].table_kind,
                ticker=ticker_u,
                fiscal_year=fiscal_year,
                status="no_data",
                notes="no FMP 10-K JSON cached for this ticker",
            )
            for tk in requested
        ]

    fy_for_dispatch = resolved_fy if resolved_fy is not None else fiscal_year
    filing_doc_id = _lookup_doc_id(db=db, ticker=ticker_u, fiscal_year=fy_for_dispatch)

    outcomes: list[ExtractionOutcome] = []
    for tk in requested:
        entry = _REGISTRY[tk]
        try:
            outcome = entry.extract_fn(
                ticker=ticker_u,
                fiscal_year=fy_for_dispatch,
                fmp_payload=fmp_payload if entry.needs_fmp else None,
                sec_text=None,  # MVP doesn't wire SEC text yet — all extractors use FMP
                db_path=db,
                repo_root=repo_root,
                filing_doc_id=filing_doc_id,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                {
                    "event": "extractor_raised",
                    "table_kind": tk,
                    "ticker": ticker_u,
                    "error": str(exc),
                }
            )
            outcome = ExtractionOutcome(
                table_kind=entry.table_kind,
                ticker=ticker_u,
                fiscal_year=fy_for_dispatch,
                status="parse_failed",
                notes=f"{type(exc).__name__}: {str(exc)[:200]}",
            )
        outcomes.append(outcome)
    return outcomes


def _load_fmp_payload(
    *, fmp_dir: Path, ticker: str, fiscal_year: int | None
) -> tuple[dict[str, object] | None, int | None]:
    """Load the FMP form_10k JSON for (ticker, fiscal_year). When fiscal_year
    is None, picks the latest year present on disk. Returns (payload, fy)."""
    if not fmp_dir.exists():
        return None, fiscal_year
    candidates: list[tuple[int, Path]] = []
    prefix = f"{ticker.upper()}_form_10k_"
    for p in fmp_dir.glob(f"{prefix}*.json"):
        stem = p.stem  # 'GOOG_form_10k_2024'
        try:
            year = int(stem.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            continue
        candidates.append((year, p))
    if not candidates:
        return None, fiscal_year
    if fiscal_year is not None:
        for year, path in candidates:
            if year == fiscal_year:
                return _read_json(path), year
        # Requested FY not present; signal no_data without falling back.
        return None, fiscal_year
    # No fiscal_year specified — pick latest.
    candidates.sort(key=lambda t: t[0], reverse=True)
    year, path = candidates[0]
    return _read_json(path), year


def _read_json(path: Path) -> dict[str, object] | None:
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return cast("dict[str, object]", data)
        return None
    except (OSError, json.JSONDecodeError) as exc:
        log.warning({"event": "fmp_payload_read_failed", "path": str(path), "error": str(exc)})
        return None


def _lookup_doc_id(*, db: Path, ticker: str, fiscal_year: int | None) -> int | None:
    """Find the documents.id for the 10-K matching (ticker, fiscal_year).

    Best-effort: returns None when the documents table is missing, when no
    row matches, or when the schema is unexpectedly shaped. Extractors
    handle None gracefully (the extractions audit row is skipped but the
    typed row still lands).
    """
    if fiscal_year is None or not db.exists():
        return None
    conn = sqlite3.connect(str(db))
    try:
        present = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='documents'"
        ).fetchone()
        if present is None:
            return None
        # Prefer fmp_10k_json with period_end's year matching fiscal_year;
        # fall back to sec_10k.
        for doc_type in ("fmp_10k_json", "sec_10k"):
            row = conn.execute(
                """
                SELECT id FROM documents
                WHERE ticker = ? AND doc_type = ?
                  AND (
                    strftime('%Y', period_end) = ? OR
                    substr(period_end, 1, 4) = ?
                  )
                ORDER BY period_end DESC
                LIMIT 1
                """,
                (ticker, doc_type, str(fiscal_year), str(fiscal_year)),
            ).fetchone()
            if row is not None:
                return int(row[0])
        return None
    except sqlite3.Error as exc:
        log.warning({"event": "doc_id_lookup_failed", "ticker": ticker, "error": str(exc)})
        return None
    finally:
        conn.close()
