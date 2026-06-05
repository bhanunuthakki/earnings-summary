"""Populate the entity graph from `src/entity_seed.py` + observed data.

This is the workhorse that fixes the matching problem the user flagged
("AWS metrics should be attributable to Amazon"). Steps:

  1. For every ticker in PORTFOLIO_HOLDINGS + WATCHLIST_BIZ_MODELS, create
     or reuse a company-kind entity. Register aliases.
  2. For every segment under a portfolio holding, create a segment-kind
     entity with parent_entity_id pointing to the company. Register aliases.
     Wire `segment_of` relationship.
  3. For every product under a segment, create a product-kind entity with
     parent_entity_id pointing to the segment. Register `product_of`.
  4. For every competitor mentioned, create a competitor-kind entity (or
     reuse the company entity if it's a tracked ticker). Wire `competes_with`.
  5. Backfill `segment_dimensions.segment_entity_id` for every existing row
     by resolving dim_name against entity_aliases (deterministic match).
  6. For unmapped segment_dimensions rows, log them as `mapping_proposals`
     for LLM canonicalization (separate cron).

Idempotent: re-running applies new aliases but skips existing rows.

Usage:
    python execution/seed_entity_graph.py
    python execution/seed_entity_graph.py --verbose
    python execution/seed_entity_graph.py --backfill-only   # skip seeding
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db  # noqa: E402

from entity_seed import (  # noqa: E402
    PORTFOLIO_HOLDINGS,
    WATCHLIST_BIZ_MODELS,
    CompanySeed,
    SegmentSeed,
)
from entity_store import (  # noqa: E402
    propose_mapping,
    record_alias,
    resolve_entity,
    upsert_entity,
    upsert_relationship,
)

log = logging.getLogger("seed_entity_graph")


def _sync_db(repo_root: Path) -> None:
    db.PROJECT_ROOT = str(repo_root)
    db.DATA_DIR = str(repo_root / "data")
    db.DB_PATH = str(repo_root / "data" / "portfolio.db")
    db.FMP_DIR = str(repo_root / "data" / "historical" / "fmp")


def seed_company(seed: CompanySeed) -> int | None:
    """Create or reuse company entity. Returns entity_id."""
    eid = upsert_entity(
        kind="company",
        canonical_name=seed.canonical_name,
        display_name=seed.display_name,
        external_ids={"ticker": seed.ticker},
        meta={"sector": seed.sector} if seed.sector else None,
    )
    if eid is None:
        return None
    # Register all aliases (ticker + display name + other surface forms)
    for alias in {seed.ticker, seed.display_name, *seed.aliases}:
        record_alias(entity_id=eid, alias_text=alias, alias_kind="manual")
    return eid


def seed_segment(seed: SegmentSeed, *, company_entity_id: int, ticker: str) -> int | None:
    """Create or reuse segment entity under a company."""
    # We use `ticker:canonical_name` as the global canonical_name to avoid
    # collisions when two companies have segments of the same name (e.g.
    # both Amazon and Google have "Other Bets" / "Cloud" type segments).
    # The display name stays as the raw canonical_name for UX.
    canonical = f"{ticker}:{seed.canonical_name}"
    eid = upsert_entity(
        kind="segment",
        canonical_name=canonical,
        display_name=seed.canonical_name,
        parent_entity_id=company_entity_id,
    )
    if eid is None:
        return None
    # Aliases: the bare canonical_name + every surface form
    aliases: set[str] = {seed.canonical_name, *seed.aliases}
    for alias in aliases:
        record_alias(
            entity_id=eid,
            alias_text=alias,
            alias_kind="manual",
        )
    # Wire the segment_of relationship
    upsert_relationship(
        from_entity_id=eid,
        relationship_kind="segment_of",
        to_entity_id=company_entity_id,
    )
    return eid


def seed_products(
    *, segment_entity_id: int, products: list[str], ticker: str, segment_name: str
) -> None:
    """Seed product entities under a segment."""
    for product in products:
        # Canonical name includes the parent to disambiguate (e.g.
        # both NVO and LLY may have GLP-1 products with overlapping names)
        canonical = f"{ticker}:{segment_name}:{product}"
        pid = upsert_entity(
            kind="product",
            canonical_name=canonical,
            display_name=product,
            parent_entity_id=segment_entity_id,
        )
        if pid is None:
            continue
        record_alias(entity_id=pid, alias_text=product, alias_kind="manual")
        upsert_relationship(
            from_entity_id=pid,
            relationship_kind="product_of",
            to_entity_id=segment_entity_id,
        )


def seed_competitors(*, company_entity_id: int, competitor_names: list[str]) -> None:
    """Seed competitor entities and the `competes_with` relationship.

    If the competitor is itself a tracked ticker (e.g. Microsoft for Amazon
    AWS), reuse that company entity. Otherwise create a `kind='competitor'`
    entity with the name as canonical_name.
    """
    for name in competitor_names:
        # Try to resolve to an existing company entity first
        existing = resolve_entity(name, kind="company")
        if existing is not None:
            target = existing
        else:
            target = upsert_entity(
                kind="competitor", canonical_name=name, display_name=name
            )
            if target is None:
                continue
            record_alias(entity_id=target, alias_text=name, alias_kind="manual")
        upsert_relationship(
            from_entity_id=company_entity_id,
            relationship_kind="competes_with",
            to_entity_id=target,
        )


def seed_all_portfolio() -> dict[str, int]:
    """Seed all portfolio + watchlist tickers. Returns a {ticker: entity_id} map."""
    company_ids: dict[str, int] = {}

    for ticker, seed in PORTFOLIO_HOLDINGS.items():
        cid = seed_company(seed)
        if cid is None:
            log.warning({"event": "company_seed_failed", "ticker": ticker})
            continue
        company_ids[ticker] = cid
        for seg in seed.segments:
            sid = seed_segment(seg, company_entity_id=cid, ticker=ticker)
            if sid is None:
                continue
            if seg.products:
                seed_products(
                    segment_entity_id=sid,
                    products=seg.products,
                    ticker=ticker,
                    segment_name=seg.canonical_name,
                )
        seed_competitors(company_entity_id=cid, competitor_names=seed.competitors)
        log.info(
            {
                "event": "portfolio_seeded",
                "ticker": ticker,
                "segments": len(seed.segments),
                "competitors": len(seed.competitors),
            }
        )

    # Watchlist: company-only
    for ticker, meta in WATCHLIST_BIZ_MODELS.items():
        cid = upsert_entity(
            kind="company",
            canonical_name=meta["name"],
            display_name=meta["display"],
            external_ids={"ticker": ticker},
            meta={"sector": meta.get("sector")} if meta.get("sector") else None,
        )
        if cid is None:
            continue
        company_ids[ticker] = cid
        record_alias(entity_id=cid, alias_text=ticker, alias_kind="manual")
        record_alias(entity_id=cid, alias_text=meta["display"], alias_kind="manual")

    return company_ids


def backfill_segment_facts_entity_id(repo_root: Path) -> tuple[int, int]:
    """For every unmapped segment_dimensions row, resolve dim_name against
    entity_aliases (scoped to the period's ticker) and set
    segment_entity_id. Returns (resolved_count, unresolved_count).

    Function name retained for backwards compatibility with the CLI driver
    and any external scripts referencing it; the underlying table is now
    segment_dimensions.
    """
    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        return (0, 0)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        # For each (ticker, dim_name) with NULL segment_entity_id → look up
        # entity_id of the matching segment.
        rows_to_resolve = conn.execute(
            """
            SELECT DISTINCT sp.ticker AS ticker, sd.dim_name AS segment_name
            FROM segment_periods sp
            JOIN segment_dimensions sd ON sd.period_id = sp.id
            WHERE sd.segment_entity_id IS NULL
            """
        ).fetchall()
        resolved = 0
        unresolved = 0
        unresolved_pairs: list[tuple[str, str]] = []
        for r in rows_to_resolve:
            ticker = r["ticker"]
            segment_name = r["segment_name"]
            if not segment_name:
                unresolved += 1
                continue
            seg_id = _resolve_segment_for_ticker(conn, ticker, segment_name)
            if seg_id is None:
                unresolved += 1
                unresolved_pairs.append((ticker, segment_name))
                continue
            # Backfill every dim row matching this (ticker, dim_name).
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
                (seg_id, ticker, segment_name),
            )
            resolved += cur.rowcount
        conn.commit()
        # Log unresolved pairs as mapping_proposals so an LLM canonicalization
        # cron can pick them up. confidence=0.4 means "needs LLM resolution"
        # but doesn't auto-apply.
        for ticker, segment_name in unresolved_pairs:
            propose_mapping(
                kind="new_alias",
                payload={
                    "ticker": ticker,
                    "unmapped_segment_name": segment_name,
                    "needs_canonical_match": True,
                },
                confidence=0.45,  # pending_review
                proposed_by="seed_backfill",
                ticker=ticker,
                source_excerpt=(
                    f"segment_dimensions row(s) for ticker={ticker} have "
                    f"dim_name='{segment_name}' which doesn't match any "
                    f"seeded segment alias."
                ),
            )
        return (resolved, unresolved)
    finally:
        conn.close()


def _resolve_segment_for_ticker(
    conn: sqlite3.Connection, ticker: str, segment_name: str
) -> int | None:
    """Resolve segment_name → segment entity_id, scoped to the given ticker.

    The same surface name can appear under multiple companies' segments
    (e.g. "Cloud" exists under both AMZN and GOOG). We disambiguate by
    finding the company entity for the ticker first, then looking up
    segment-kind entities whose parent is the company.
    """
    # 1. Find the company entity for the ticker.
    company_row = conn.execute(
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
    if company_row is None:
        return None
    company_id = int(company_row[0])

    # 2. Find a segment entity under this company whose aliases include
    #    segment_name (case-insensitive). Prefer exact match over substring.
    seg_row = conn.execute(
        """
        SELECT ea.entity_id FROM entity_aliases ea
        INNER JOIN entities e ON e.id = ea.entity_id
        WHERE e.kind = 'segment'
          AND e.parent_entity_id = ?
          AND LOWER(ea.alias_text) = LOWER(?)
        ORDER BY ea.confidence DESC, ea.observation_count DESC
        LIMIT 1
        """,
        (company_id, segment_name),
    ).fetchone()
    if seg_row is not None:
        return int(seg_row[0])

    # 3. Substring/normalized fallback: strip "Segment" suffix, lowercase
    normalized = segment_name.lower().replace(" segment", "").strip()
    seg_row2 = conn.execute(
        """
        SELECT ea.entity_id FROM entity_aliases ea
        INNER JOIN entities e ON e.id = ea.entity_id
        WHERE e.kind = 'segment'
          AND e.parent_entity_id = ?
          AND LOWER(REPLACE(ea.alias_text, ' Segment', '')) = ?
        ORDER BY ea.confidence DESC, ea.observation_count DESC
        LIMIT 1
        """,
        (company_id, normalized),
    ).fetchone()
    if seg_row2 is not None:
        return int(seg_row2[0])

    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root", type=Path, default=PROJECT_ROOT, help="Repo root."
    )
    parser.add_argument(
        "--backfill-only",
        action="store_true",
        help="Skip the seed step; just rerun segment_dimensions backfill.",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    _sync_db(args.repo_root)

    if not args.backfill_only:
        company_ids = seed_all_portfolio()
        log.info({"event": "seed_done", "n_companies": len(company_ids)})

    resolved, unresolved = backfill_segment_facts_entity_id(args.repo_root)
    log.info(
        {
            "event": "backfill_done",
            "resolved_rows": resolved,
            "unresolved_pairs": unresolved,
        }
    )

    # Final state
    conn = sqlite3.connect(str(args.repo_root / "data" / "portfolio.db"))
    try:
        ent = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        aliases = conn.execute("SELECT COUNT(*) FROM entity_aliases").fetchone()[0]
        rels = conn.execute("SELECT COUNT(*) FROM entity_relationships").fetchone()[0]
        nulled = conn.execute(
            "SELECT COUNT(*) FROM segment_dimensions WHERE segment_entity_id IS NULL"
        ).fetchone()[0]
        mapped = conn.execute(
            "SELECT COUNT(*) FROM segment_dimensions WHERE segment_entity_id IS NOT NULL"
        ).fetchone()[0]
        proposals = conn.execute(
            "SELECT COUNT(*) FROM mapping_proposals WHERE status = 'pending_review'"
        ).fetchone()[0]
    finally:
        conn.close()

    print(
        f"\nEntity graph state:\n"
        f"  entities:              {ent}\n"
        f"  aliases:               {aliases}\n"
        f"  relationships:         {rels}\n"
        f"  segment_dimensions mapped:  {mapped}\n"
        f"  segment_dimensions unmapped:{nulled}\n"
        f"  pending mapping proposals: {proposals}\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
