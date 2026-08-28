"""Backfill/enrich ``pdf_slide`` locators on IR-deck-sourced kpi_facts rows.

Provenance click-through Phase B retrofit (docs/design/provenance_clickthrough.md
§5.2, §5.4): now that PDF page rendering exists, existing IR-PDF-sourced facts
are mechanically enrichable from data already on disk — no LLM, no re-fetch:

  * a row with a v1 ``{"pdf_page": N}`` locator gets promoted to the v2
    ``pdf_slide`` shape, attaching its ``source_excerpt`` as the
    ``verbatim_snippet`` and a bounding box when PyMuPDF finds the excerpt on
    the cited page (``page.search_for``);
  * a row with NO locator (escape-hatched at capture time) but a non-empty
    ``source_excerpt`` gets the excerpt located across the source PDF's pages
    (whitespace/case-normalized verbatim match — the same honesty bar as
    ``pipeline.locators.verify_quote_in_source``; no fuzzy matching) and, when
    found, a full v2 ``pdf_slide`` locator (page + bbox where derivable);
  * a row with no excerpt, or whose excerpt can't be found verbatim, stays
    legacy-badged — an honest legacy floor beats a fabricated anchor.

Idempotent: rows already carrying a v2 ``pdf_slide`` locator with a bbox are
skipped; re-running is a no-op (``--force`` re-derives them). Ticker order
follows §5.4 (surface visibility first): portfolio + evaluation tickers before
the rest. Structured JSON-line events go to stderr; the Pydantic result
summary is the only stdout output.

Usage:
    python execution/backfill_ir_deck_locators.py [--ticker T ...] [--force]
        [--dry-run] [--db data/portfolio.db]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from models.facts import FactLocator, LocatorKind  # noqa: E402
from pipeline.locators import pdf_slide_locator  # noqa: E402
from pipeline.pdf_render import (  # noqa: E402
    extract_page_texts,
    find_page_for_quote,
    find_quote_bbox,
)
from pipeline.queries import BRIEFED_LIST_TYPES, open_db, tracked_companies_for_user  # noqa: E402


class TickerBackfill(BaseModel):
    """Per-ticker retrofit accounting."""

    ticker: str
    examined: int = 0
    # NULL-locator rows whose excerpt was located → full v2 locator written.
    backfilled_from_excerpt: int = 0
    # v1 pdf_page rows promoted to v2 (snippet attached; bbox where found).
    upgraded_v1_to_v2: int = 0
    # v2 rows that additionally gained a bbox (counts inside the two above,
    # plus --force re-derivations that found one).
    bbox_added: int = 0
    # Rows already carrying a renderable v2 pdf_slide locator (no-op).
    already_v2: int = 0
    # Rows left legacy: no excerpt to locate, or excerpt not found verbatim.
    legacy_no_excerpt: int = 0
    legacy_excerpt_not_found: int = 0
    # Source PDF missing on disk (worktree without ir_documents/, moved file).
    missing_pdf: int = 0
    # Rows whose locator is a different kind entirely (e.g. capture_xbrl_v1
    # section locators on a 10-K PDF registered under ir_documents/) — not
    # IR-deck extractions, not this retrofit's business.
    skipped_other_kind: int = 0


class BackfillResult(BaseModel):
    """Whole-run summary — the CLI's stdout contract."""

    db: str
    dry_run: bool
    force: bool
    tickers: list[TickerBackfill] = Field(default_factory=list[TickerBackfill])

    @property
    def total_written(self) -> int:
        return sum(t.backfilled_from_excerpt + t.upgraded_v1_to_v2 for t in self.tickers)


def _log(event: str, **fields: object) -> None:
    """One structured JSON event per line to stderr (repo logging contract)."""
    sys.stderr.write(json.dumps({"event": event, **fields}, default=str) + "\n")


def _priority_ordered_tickers(conn: sqlite3.Connection, scoped: list[str]) -> list[str]:
    """Tickers with IR-PDF-sourced kpi_facts, portfolio+evaluation first
    (§5.4: a 200-row improvement on an open name beats 10k rows on one
    nobody is looking at). ``scoped`` (from --ticker) short-circuits."""
    if scoped:
        return [t.upper() for t in scoped]
    rows = conn.execute(
        "SELECT DISTINCT kf.ticker FROM kpi_facts kf "
        "JOIN documents d ON d.id = kf.source_doc_id "
        "WHERE d.source_type = 'ir_doc' AND LOWER(d.file_path) LIKE '%.pdf' "
        "ORDER BY kf.ticker"
    ).fetchall()
    all_tickers = [str(r["ticker"]).upper() for r in rows]
    priority: set[str] = set()
    try:
        for company in tracked_companies_for_user(conn, list_types=BRIEFED_LIST_TYPES):
            priority.add(company.ticker.upper())
    except sqlite3.Error:
        priority = set()
    return sorted(all_tickers, key=lambda t: (t not in priority, t))


def _resolve_pdf(repo_root: Path, file_path: str) -> Path | None:
    path = Path(file_path)
    if not path.is_absolute():
        path = repo_root / path
    return path if path.exists() else None


def _parse_locator(raw: object) -> FactLocator | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return FactLocator.from_json(raw)
    except ValueError:
        return None


def backfill_ticker(
    conn: sqlite3.Connection,
    repo_root: Path,
    ticker: str,
    *,
    force: bool = False,
    dry_run: bool = True,
) -> TickerBackfill:
    """Enrich every IR-PDF-sourced kpi_facts row for one ticker (see module
    docstring for the three enrichment cases and the legacy floor)."""
    if not dry_run:
        raise ValueError("in-place locator backfill is retired; use source-backed supersession")
    out = TickerBackfill(ticker=ticker)
    rows = conn.execute(
        "SELECT kf.id, kf.locator, kf.source_excerpt, d.file_path "
        "FROM kpi_facts kf JOIN documents d ON d.id = kf.source_doc_id "
        "WHERE kf.ticker = ? AND d.source_type = 'ir_doc' "
        "AND LOWER(d.file_path) LIKE '%.pdf' ORDER BY kf.id",
        (ticker,),
    ).fetchall()
    # Per-PDF page-text index, built lazily once per file (a deck backfill
    # makes many lookups against the same PDF).
    page_texts_by_pdf: dict[Path, list[str] | None] = {}
    for row in rows:
        out.examined += 1
        fact_id = int(row["id"])
        excerpt = str(row["source_excerpt"]).strip() if row["source_excerpt"] else ""
        loc = _parse_locator(row["locator"])
        kind = loc.effective_kind() if loc is not None else None
        if loc is not None and kind != LocatorKind.PDF_SLIDE:
            # A non-PDF locator on an IR-PDF-sourced row (e.g. a
            # capture_xbrl_v1 section locator on a 10-K PDF, or a derived
            # locator) is not this retrofit's business.
            out.skipped_other_kind += 1
            continue
        is_v2 = loc is not None and loc.kind == LocatorKind.PDF_SLIDE
        if is_v2 and loc is not None and loc.pdf_bbox is not None and not force:
            out.already_v2 += 1
            continue

        pdf_path = _resolve_pdf(repo_root, str(row["file_path"]))
        if pdf_path is None:
            out.missing_pdf += 1
            _log("backfill_missing_pdf", ticker=ticker, fact_id=fact_id, path=row["file_path"])
            continue

        if loc is not None and loc.pdf_page is not None:
            # v1 pdf_page (or bbox-less v2): promote/enrich on the cited page.
            snippet = excerpt or (loc.verbatim_snippet or "")
            if not snippet:
                out.legacy_no_excerpt += 1
                continue
            bbox = loc.pdf_bbox or find_quote_bbox(pdf_path, loc.pdf_page, snippet)
            _ = pdf_slide_locator(pdf_page=loc.pdf_page, verbatim_snippet=snippet, bbox=bbox)
            if is_v2 and bbox is None:
                # --force re-derivation found nothing new; don't churn the row.
                out.already_v2 += 1
                continue
            if bbox is not None and (loc.pdf_bbox is None):
                out.bbox_added += 1
            out.upgraded_v1_to_v2 += 1 if not is_v2 else 0
        else:
            # No locator at all: locate the excerpt across the PDF.
            if not excerpt:
                out.legacy_no_excerpt += 1
                continue
            if pdf_path not in page_texts_by_pdf:
                page_texts_by_pdf[pdf_path] = extract_page_texts(pdf_path)
            texts = page_texts_by_pdf[pdf_path]
            hit = (
                find_page_for_quote(pdf_path, excerpt, page_texts=texts)
                if texts is not None
                else None
            )
            if hit is None:
                out.legacy_excerpt_not_found += 1
                continue
            page, bbox = hit
            _ = pdf_slide_locator(pdf_page=page, verbatim_snippet=excerpt, bbox=bbox)
            if bbox is not None:
                out.bbox_added += 1
            out.backfilled_from_excerpt += 1

    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ticker",
        action="append",
        default=[],
        help="Restrict to one ticker (repeatable). Default: every ticker with "
        "IR-PDF-sourced kpi_facts, portfolio+evaluation names first.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-derive rows already carrying a v2 pdf_slide locator (e.g. to "
        "retry bbox derivation after a PyMuPDF upgrade).",
    )
    parser.add_argument(
        "--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"), help="Path to portfolio.db"
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Root for resolving relative documents.file_path values. IR PDFs "
        "live in the MAIN checkout, so a worktree run points this at it.",
    )
    args = parser.parse_args()

    conn = open_db(args.db)
    conn.execute("PRAGMA busy_timeout = 30000")
    result = BackfillResult(db=str(args.db), dry_run=True, force=bool(args.force))
    try:
        tickers = _priority_ordered_tickers(conn, list(args.ticker))
        _log("backfill_start", tickers=tickers, dry_run=True, force=args.force)
        for ticker in tickers:
            tb = backfill_ticker(
                conn,
                Path(args.repo_root),
                ticker,
                force=bool(args.force),
                dry_run=True,
            )
            result.tickers.append(tb)
            _log("backfill_ticker_done", **tb.model_dump())
    finally:
        conn.close()
    print(json.dumps(result.model_dump(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
