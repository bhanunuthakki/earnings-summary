"""LLM canonicalizer for unmapped segment names.

After `execution/seed_entity_graph.py` lands its deterministic mappings,
this script walks the remaining `segment_dimensions` rows with
segment_entity_id IS NULL and asks Haiku, per ticker, to:

  1. Identify which observed segment names refer to the same business unit.
  2. Pick a canonical name for each group.
  3. Return a JSON mapping that the script applies.

For each canonical group the script either:
  - Reuses an existing segment entity if one already exists under the ticker
    (e.g., a watchlist company's "AI Software" segment that's a clean addition)
  - Creates a new segment-kind entity with parent_entity_id = company's entity_id
  - Registers each observed surface form as an alias
  - Backfills segment_dimensions.segment_entity_id

Skip cases (these stay unmapped — they're not real segments):
  - "Operating Segments", "Reconciling items", "Other Segments", "Other"
  - "Total", "Consolidated"
  - Pure geographic rows when fiscal data already has geography breakouts

Idempotent: rerunning skips already-mapped segments.

Usage:
    python execution/canonicalize_segments.py
    python execution/canonicalize_segments.py --tickers AMD,NVDA,MSFT
    python execution/canonicalize_segments.py --dry-run  # preview only
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, Field, TypeAdapter

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db  # noqa: E402
from entity_store import (  # noqa: E402
    record_alias,
    upsert_entity,
    upsert_relationship,
)
from llm.structured import StructuredParseError, call_llm_structured  # noqa: E402
from llm.untrusted import spotlight  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

log = logging.getLogger("canonicalize_segments")


class _CanonicalGroup(BaseModel):
    canonical_name: str
    kind: Literal["segment", "product_category", "geography"]
    aliases: list[str] = Field(default_factory=list[str])


class _Canonicalization(BaseModel):
    groups: list[_CanonicalGroup] = Field(default_factory=list[_CanonicalGroup])
    skip: list[str] = Field(default_factory=list[str])


# Names that should NEVER be mapped — they're aggregation buckets or noise.
SKIP_TERMS = {
    "operating segments",
    "reconciling items",
    "other segments",
    "other",
    "total",
    "consolidated",
    "elimination",
    "eliminations",
    "corporate",
    "unallocated",
    "intersegment",
    "intercompany",
}


def _sync_db(repo_root: Path) -> None:
    db.PROJECT_ROOT = str(repo_root)
    db.DATA_DIR = str(repo_root / "data")
    db.DB_PATH = str(repo_root / "data" / "portfolio.db")
    db.FMP_DIR = str(repo_root / "data" / "historical" / "fmp")


def _unmapped_by_ticker(repo_root: Path, tickers: list[str] | None) -> dict[str, list[str]]:
    """Return {ticker: [segment_name, ...]} for unmapped segment_dimensions rows.

    Reads from segment_periods + segment_dimensions; the segment_entity_id
    column on segment_dimensions was carried over from segment_facts in
    migration 0056. dim_name plays the role of the old segment_name.
    """
    conn = connect_sqlite(
        str(repo_root / "data" / "portfolio.db"), role=SQLiteConnectionRole.READ_ONLY
    )
    try:
        conn.row_factory = sqlite3.Row
        if tickers:
            placeholders = ",".join("?" * len(tickers))
            rows = conn.execute(
                f"""
                SELECT DISTINCT sp.ticker AS ticker, sd.dim_name AS segment_name
                FROM segment_periods sp
                JOIN segment_dimensions sd ON sd.period_id = sp.id
                WHERE sd.segment_entity_id IS NULL AND sp.ticker IN ({placeholders})
                """,
                tickers,
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT DISTINCT sp.ticker AS ticker, sd.dim_name AS segment_name
                FROM segment_periods sp
                JOIN segment_dimensions sd ON sd.period_id = sp.id
                WHERE sd.segment_entity_id IS NULL
                """
            ).fetchall()
    finally:
        conn.close()

    by_ticker: dict[str, list[str]] = {}
    for r in rows:
        sn = (r["segment_name"] or "").strip()
        if not sn:
            continue
        if sn.lower() in SKIP_TERMS:
            continue
        by_ticker.setdefault(r["ticker"], []).append(sn)
    return by_ticker


def _company_entity_id(conn: sqlite3.Connection, ticker: str) -> int | None:
    row = conn.execute(
        """
        SELECT e.id FROM entities e
        WHERE e.kind = 'company'
          AND (
            json_extract(e.external_ids, '$.ticker') = ?
            OR EXISTS (
              SELECT 1 FROM entity_aliases ea
              WHERE ea.entity_id = e.id AND UPPER(ea.alias_text) = ?
            )
          )
        LIMIT 1
        """,
        (ticker, ticker.upper()),
    ).fetchone()
    return int(row[0]) if row else None


_PROMPT = """You are canonicalizing observed segment names for {ticker} ({company_name}).
The following surface forms are unmapped — they need to be grouped under
canonical segment names with consistent display labels.

OBSERVED SEGMENT NAMES (one per line):
{segment_list}

Your task: produce a JSON object grouping these into canonical segments
that match how {ticker} ACTUALLY reports segments in their 10-K (not your
guess — match to the disclosed reportable segments / product categories).

Output schema (strict JSON, no commentary):
{{
  "groups": [
    {{
      "canonical_name": "<the display label as it appears in 10-K>",
      "kind": "segment" | "product_category" | "geography",
      "aliases": ["<every observed form that should map here>"]
    }}
  ],
  "skip": ["<observed names that are pseudo-segments or noise>"]
}}

Rules:
  - If you cannot confidently group a name, put it in "skip" — better to
    leave it unmapped than to mismap.
  - "geography" kind for country / region rows (UNITED STATES, EMEA, etc.).
  - "product_category" for product-level breakdowns (Electronics, Media, etc.).
  - "segment" for reportable business segments.
  - Each observed name appears in EXACTLY ONE group OR in "skip".
"""


def _canonicalize_one_ticker(
    *, ticker: str, segment_names: list[str], company_name: str | None
) -> dict[str, object] | None:
    """LLM call — returns parsed JSON or None on failure."""
    if not segment_names:
        return None
    segment_list = "\n".join(f"- {n}" for n in sorted(set(segment_names)))
    prompt = _PROMPT.format(
        ticker=ticker,
        company_name=company_name or ticker,
        segment_list=spotlight(segment_list, source="observed SEC segment labels"),
    )
    try:
        validated = cast(
            "_Canonicalization",
            call_llm_structured(
                prompt,
                purpose="canonicalize_segments",
                ticker=ticker,
                scope="segment_canonicalization",
                schema=TypeAdapter(_Canonicalization),
            ),
        )
    except StructuredParseError as exc:
        log.warning({"event": "canon_structured_failed", "ticker": ticker, "error": str(exc)})
        return None
    except Exception as exc:
        log.warning({"event": "canon_llm_failed", "ticker": ticker, "error": str(exc)})
        return None
    return cast("dict[str, object]", validated.model_dump())


def _apply_groups(
    *,
    ticker: str,
    company_entity_id: int,
    parsed: dict[str, object],
    repo_root: Path,
    dry_run: bool,
) -> int:
    """Materialize the LLM's canonical groups into entities + aliases +
    segment_facts.segment_entity_id. Returns count of rows newly mapped."""
    groups_raw = parsed.get("groups")
    if not isinstance(groups_raw, list):
        return 0

    db_path = repo_root / "data" / "portfolio.db"
    rows_mapped = 0
    for g in cast("list[object]", groups_raw):
        if not isinstance(g, dict):
            continue
        gd = cast("dict[str, object]", g)
        canonical_name = gd.get("canonical_name")
        kind = gd.get("kind") or "segment"
        aliases_raw = gd.get("aliases")
        if not isinstance(canonical_name, str) or not canonical_name.strip():
            continue
        if kind not in ("segment", "product_category", "geography"):
            kind = "segment"
        aliases = (
            [a for a in cast("list[object]", aliases_raw) if isinstance(a, str)]
            if isinstance(aliases_raw, list)
            else []
        )
        if not aliases:
            continue

        global_canonical = f"{ticker}:{canonical_name}"
        if dry_run:
            log.info(
                {
                    "event": "dry_run_group",
                    "ticker": ticker,
                    "kind": kind,
                    "canonical": canonical_name,
                    "aliases": aliases,
                }
            )
            continue

        seg_id = upsert_entity(
            kind=kind,
            canonical_name=global_canonical,
            display_name=canonical_name,
            parent_entity_id=company_entity_id,
        )
        if seg_id is None:
            continue
        # Register canonical + all observed aliases
        for alias in {canonical_name, *aliases}:
            record_alias(
                entity_id=seg_id,
                alias_text=alias,
                alias_kind="llm_canonicalized",
                confidence=0.9,
            )
        upsert_relationship(
            from_entity_id=seg_id,
            relationship_kind=(
                "segment_of"
                if kind == "segment"
                else "product_of"
                if kind == "product_category"
                else "geography_of"
            ),
            to_entity_id=company_entity_id,
        )

        # Backfill segment_dimensions for each alias (via segment_periods to
        # filter on ticker — segment_dimensions itself has no ticker column).
        conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.WRITER, schema_preflight=True)
        try:
            for alias in aliases:
                cur = conn.execute(
                    """
                    UPDATE segment_dimensions
                    SET segment_entity_id = ?
                    WHERE id IN (
                        SELECT sd.id
                        FROM segment_dimensions sd
                        JOIN segment_periods sp ON sp.id = sd.period_id
                        WHERE sp.ticker = ?
                          AND sd.dim_name = ?
                          AND sd.segment_entity_id IS NULL
                    )
                    """,
                    (seg_id, ticker, alias),
                )
                rows_mapped += cur.rowcount
            conn.commit()
        finally:
            conn.close()
    return rows_mapped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT, help="Repo root.")
    parser.add_argument("--tickers", help="Comma-separated tickers (default: all unmapped).")
    parser.add_argument(
        "--max-tickers",
        type=int,
        default=0,
        help="Cap the number of tickers processed in this run (0 = no cap).",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Show LLM proposals without writing."
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    _sync_db(args.repo_root)

    tickers = [t.strip().upper() for t in args.tickers.split(",")] if args.tickers else None
    by_ticker = _unmapped_by_ticker(args.repo_root, tickers)
    if args.max_tickers:
        by_ticker = dict(list(by_ticker.items())[: args.max_tickers])
    log.info({"event": "canon_start", "n_tickers": len(by_ticker)})

    total_mapped = 0
    conn = connect_sqlite(
        str(args.repo_root / "data" / "portfolio.db"), role=SQLiteConnectionRole.READ_ONLY
    )
    try:
        for ticker, segments in by_ticker.items():
            company_entity_id = _company_entity_id(conn, ticker)
            if company_entity_id is None:
                log.warning(
                    {"event": "no_company_entity", "ticker": ticker, "n_segments": len(segments)}
                )
                continue
            row = conn.execute(
                "SELECT display_name FROM entities WHERE id = ?", (company_entity_id,)
            ).fetchone()
            company_name = row[0] if row else None

            log.info(
                {
                    "event": "canon_ticker_start",
                    "ticker": ticker,
                    "n_segments": len(segments),
                }
            )
            parsed = _canonicalize_one_ticker(
                ticker=ticker, segment_names=segments, company_name=company_name
            )
            if parsed is None:
                continue
            n = _apply_groups(
                ticker=ticker,
                company_entity_id=company_entity_id,
                parsed=parsed,
                repo_root=args.repo_root,
                dry_run=args.dry_run,
            )
            total_mapped += n
            log.info(
                {
                    "event": "canon_ticker_done",
                    "ticker": ticker,
                    "rows_mapped": n,
                }
            )
    finally:
        conn.close()

    # Final state
    conn = connect_sqlite(
        str(args.repo_root / "data" / "portfolio.db"), role=SQLiteConnectionRole.READ_ONLY
    )
    try:
        mapped = conn.execute(
            "SELECT COUNT(*) FROM segment_dimensions WHERE segment_entity_id IS NOT NULL"
        ).fetchone()[0]
        unmapped = conn.execute(
            "SELECT COUNT(*) FROM segment_dimensions WHERE segment_entity_id IS NULL"
        ).fetchone()[0]
    finally:
        conn.close()
    print(
        f"\nCanonicalization done · {total_mapped} new rows mapped this run.\n"
        f"  segment_dimensions mapped:   {mapped}\n"
        f"  segment_dimensions unmapped: {unmapped}\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
