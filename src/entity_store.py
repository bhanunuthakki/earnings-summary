"""Read/write API for the entity + concept + extraction + mapping-proposal tables.

Single module by design: entities and concepts are siblings of the same
"semantic backbone," and bundling the access pattern keeps the API surface
small. Read paths are common: resolve("Active Customers", ticker='NU') →
concept_id; resolve("AI Mode") → entity_id (kind=product).

All writers are idempotent / dedup-safe so re-running an extractor over the
same document doesn't multiply rows. observation_count + last_observed_at
on aliases captures "we've seen this name N times".

The mapping-proposal queue is the self-updating piece: when an extractor
sees a name it can't resolve, it writes a proposal with confidence. The
``auto_apply_threshold=0.85`` default mirrors the spec; raise the floor in
production once the resolver's calibration is known.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import cast

from clock import now_iso, to_naive_utc

log = logging.getLogger(__name__)

AUTO_APPLY_THRESHOLD = 0.85
PENDING_REVIEW_FLOOR = 0.50


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Entity:
    id: int
    kind: str
    canonical_name: str
    display_name: str | None
    external_ids: dict[str, object] = field(default_factory=dict)
    parent_entity_id: int | None = None
    meta: dict[str, object] = field(default_factory=dict)
    effective_from: datetime | None = None
    effective_to: datetime | None = None


@dataclass(slots=True)
class Concept:
    id: int
    kind: str
    canonical_name: str
    unit_kind: str | None = None
    taxonomy_xbrl_tag: str | None = None
    generic_definition_md: str | None = None
    computation_kind: str | None = None
    computation_formula_md: str | None = None


@dataclass(slots=True)
class MappingProposal:
    id: int
    kind: str
    ticker: str | None
    proposed_by: str
    payload: dict[str, object]
    confidence: float
    status: str
    source_doc_id: int | None = None
    source_excerpt: str | None = None
    applied_to_entity_id: int | None = None
    applied_to_concept_id: int | None = None


def _open(db_path: Path | str | None) -> sqlite3.Connection | None:
    """Open or return None if DB / tables unavailable (graceful degrade)."""
    try:
        path = _resolve_path(db_path)
        if path is None or not Path(path).exists():
            return None
        conn = sqlite3.connect(str(path), timeout=5.0)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.row_factory = sqlite3.Row
        # Verify the spine tables exist — fail gracefully if not
        for t in ("entities", "concepts"):
            r = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (t,)
            ).fetchone()
            if r is None:
                conn.close()
                return None
        return conn
    except (sqlite3.Error, OSError) as exc:
        log.debug({"event": "entity_store_open_failed", "error": str(exc)})
        return None


def _resolve_path(override: Path | str | None) -> Path | None:
    if override is not None:
        return Path(override)
    try:
        from db import DB_PATH

        return Path(cast("str", DB_PATH))
    except ImportError:
        return None


def _now_iso() -> str:
    return now_iso()


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


def upsert_entity(
    *,
    kind: str,
    canonical_name: str,
    display_name: str | None = None,
    external_ids: dict[str, object] | None = None,
    parent_entity_id: int | None = None,
    meta: dict[str, object] | None = None,
    effective_from: datetime | None = None,
    db_path: Path | str | None = None,
) -> int | None:
    """Insert if (kind, canonical_name) is new; update meta+aliases on dup.
    Returns the entity_id. None on DB unavailable."""
    conn = _open(db_path)
    if conn is None:
        return None
    try:
        existing = conn.execute(
            "SELECT id FROM entities WHERE kind = ? AND canonical_name = ?",
            (kind, canonical_name),
        ).fetchone()
        now = _now_iso()
        if existing is not None:
            eid = int(existing["id"])
            # Refresh last_observed_at and optionally extend meta
            conn.execute(
                "UPDATE entities SET last_observed_at = ? WHERE id = ?",
                (now, eid),
            )
            conn.commit()
            return eid
        cur = conn.execute(
            """
            INSERT INTO entities(
                kind, canonical_name, display_name, external_ids,
                parent_entity_id, meta_json, effective_from, created_at, last_observed_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                kind,
                canonical_name,
                display_name,
                json.dumps(external_ids) if external_ids else None,
                parent_entity_id,
                json.dumps(meta) if meta else None,
                effective_from.isoformat() if effective_from else None,
                now,
                now,
            ),
        )
        new_id = int(cur.lastrowid or 0)
        # Self-alias so resolve(canonical_name) works immediately
        conn.execute(
            """
            INSERT OR IGNORE INTO entity_aliases(
                entity_id, alias_text, alias_kind,
                first_observed_at, last_observed_at, observation_count, confidence
            ) VALUES (?,?,?,?,?,1,1.0)
            """,
            (new_id, canonical_name, "manual", now, now),
        )
        conn.commit()
        return new_id
    except sqlite3.Error as exc:
        log.warning({"event": "entity_upsert_failed", "error": str(exc)})
        return None
    finally:
        conn.close()


def record_alias(
    *,
    entity_id: int,
    alias_text: str,
    alias_kind: str = "reported_in_10k",
    confidence: float = 1.0,
    source_doc_id: int | None = None,
    excerpt: str | None = None,
    db_path: Path | str | None = None,
) -> int | None:
    """Insert alias or increment observation_count on dup. Returns alias id."""
    conn = _open(db_path)
    if conn is None:
        return None
    try:
        now = _now_iso()
        existing = conn.execute(
            "SELECT id, observation_count FROM entity_aliases WHERE entity_id = ? AND alias_text = ?",
            (entity_id, alias_text),
        ).fetchone()
        if existing is not None:
            conn.execute(
                """
                UPDATE entity_aliases
                SET observation_count = observation_count + 1,
                    last_observed_at = ?
                WHERE id = ?
                """,
                (now, int(existing["id"])),
            )
            conn.commit()
            return int(existing["id"])
        cur = conn.execute(
            """
            INSERT INTO entity_aliases(
                entity_id, alias_text, alias_kind,
                first_observed_at, last_observed_at, observation_count, confidence,
                exemplar_source_doc_id, exemplar_excerpt
            ) VALUES (?,?,?,?,?,1,?,?,?)
            """,
            (entity_id, alias_text, alias_kind, now, now, confidence, source_doc_id, excerpt),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    except sqlite3.Error as exc:
        log.warning({"event": "entity_alias_failed", "error": str(exc)})
        return None
    finally:
        conn.close()


def resolve_entity(
    alias_text: str,
    *,
    kind: str | None = None,
    db_path: Path | str | None = None,
) -> int | None:
    """Look up entity_id by alias. Case-insensitive. Returns highest-confidence
    match when multiple exist (rare with unique constraints, but possible
    across kinds). Returns None if unresolved."""
    conn = _open(db_path)
    if conn is None:
        return None
    try:
        params: tuple[object, ...]
        if kind is None:
            sql = """
                SELECT a.entity_id, a.confidence
                FROM entity_aliases a
                WHERE LOWER(a.alias_text) = LOWER(?)
                ORDER BY a.confidence DESC, a.observation_count DESC
                LIMIT 1
            """
            params = (alias_text,)
        else:
            sql = """
                SELECT a.entity_id, a.confidence
                FROM entity_aliases a
                JOIN entities e ON e.id = a.entity_id
                WHERE LOWER(a.alias_text) = LOWER(?) AND e.kind = ?
                ORDER BY a.confidence DESC, a.observation_count DESC
                LIMIT 1
            """
            params = (alias_text, kind)
        row = conn.execute(sql, params).fetchone()
        return int(row["entity_id"]) if row else None
    finally:
        conn.close()


def get_entity(entity_id: int, *, db_path: Path | str | None = None) -> Entity | None:
    conn = _open(db_path)
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()
        if row is None:
            return None
        return Entity(
            id=int(row["id"]),
            kind=row["kind"],
            canonical_name=row["canonical_name"],
            display_name=row["display_name"],
            external_ids=_loads_dict(row["external_ids"]),
            parent_entity_id=row["parent_entity_id"],
            meta=_loads_dict(row["meta_json"]),
            effective_from=_parse_dt(row["effective_from"]),
            effective_to=_parse_dt(row["effective_to"]),
        )
    finally:
        conn.close()


def record_mention(
    *,
    source_doc_id: int,
    entity_id: int | None,
    mention_text: str,
    extractor_id: str,
    extractor_version: str,
    char_offset_start: int | None = None,
    char_offset_end: int | None = None,
    surrounding_context: str | None = None,
    confidence: float = 1.0,
    unresolved_alias_text: str | None = None,
    db_path: Path | str | None = None,
) -> int | None:
    conn = _open(db_path)
    if conn is None:
        return None
    try:
        cur = conn.execute(
            """
            INSERT INTO entity_mentions(
                source_doc_id, entity_id, char_offset_start, char_offset_end,
                mention_text, surrounding_context,
                extractor_id, extractor_version, confidence, unresolved_alias_text
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                source_doc_id,
                entity_id,
                char_offset_start,
                char_offset_end,
                mention_text,
                surrounding_context,
                extractor_id,
                extractor_version,
                confidence,
                unresolved_alias_text,
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    except sqlite3.Error as exc:
        log.warning({"event": "mention_record_failed", "error": str(exc)})
        return None
    finally:
        conn.close()


def upsert_relationship(
    *,
    from_entity_id: int,
    relationship_kind: str,
    to_entity_id: int,
    effective_from: datetime | None = None,
    effective_to: datetime | None = None,
    evidence_doc_id: int | None = None,
    evidence_excerpt: str | None = None,
    confidence: float = 1.0,
    meta: dict[str, object] | None = None,
    db_path: Path | str | None = None,
) -> int | None:
    conn = _open(db_path)
    if conn is None:
        return None
    try:
        ef_iso = effective_from.isoformat() if effective_from else None
        et_iso = effective_to.isoformat() if effective_to else None
        # Check existing
        existing = conn.execute(
            """
            SELECT id FROM entity_relationships
            WHERE from_entity_id = ? AND relationship_kind = ? AND to_entity_id = ?
              AND COALESCE(effective_from, '') = COALESCE(?, '')
            """,
            (from_entity_id, relationship_kind, to_entity_id, ef_iso),
        ).fetchone()
        if existing is not None:
            return int(existing["id"])
        cur = conn.execute(
            """
            INSERT INTO entity_relationships(
                from_entity_id, relationship_kind, to_entity_id,
                effective_from, effective_to, evidence_doc_id, evidence_excerpt,
                confidence, meta_json
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                from_entity_id,
                relationship_kind,
                to_entity_id,
                ef_iso,
                et_iso,
                evidence_doc_id,
                evidence_excerpt,
                confidence,
                json.dumps(meta) if meta else None,
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    except sqlite3.Error as exc:
        log.warning({"event": "relationship_upsert_failed", "error": str(exc)})
        return None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Concepts
# ---------------------------------------------------------------------------


def upsert_concept(
    *,
    canonical_name: str,
    kind: str = "kpi",
    unit_kind: str | None = None,
    taxonomy_xbrl_tag: str | None = None,
    generic_definition_md: str | None = None,
    computation_kind: str | None = None,
    computation_formula_md: str | None = None,
    db_path: Path | str | None = None,
) -> int | None:
    conn = _open(db_path)
    if conn is None:
        return None
    try:
        now = _now_iso()
        existing = conn.execute(
            "SELECT id FROM concepts WHERE canonical_name = ?", (canonical_name,)
        ).fetchone()
        if existing is not None:
            return int(existing["id"])
        cur = conn.execute(
            """
            INSERT INTO concepts(
                kind, canonical_name, unit_kind, taxonomy_xbrl_tag,
                generic_definition_md, computation_kind, computation_formula_md,
                created_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                kind,
                canonical_name,
                unit_kind,
                taxonomy_xbrl_tag,
                generic_definition_md,
                computation_kind,
                computation_formula_md,
                now,
            ),
        )
        new_id = int(cur.lastrowid or 0)
        # Self-alias
        conn.execute(
            """
            INSERT OR IGNORE INTO concept_aliases(
                concept_id, ticker, alias_text, alias_kind,
                first_observed_at, last_observed_at, confidence
            ) VALUES (?,NULL,?,?,?,?,1.0)
            """,
            (new_id, canonical_name, "manual", now, now),
        )
        conn.commit()
        return new_id
    except sqlite3.Error as exc:
        log.warning({"event": "concept_upsert_failed", "error": str(exc)})
        return None
    finally:
        conn.close()


def resolve_concept(
    alias_text: str,
    *,
    ticker: str | None = None,
    db_path: Path | str | None = None,
) -> int | None:
    """Resolve alias → concept_id. Ticker-specific aliases win over universal."""
    conn = _open(db_path)
    if conn is None:
        return None
    try:
        # Prefer ticker-specific alias; fall back to universal (ticker IS NULL)
        row = conn.execute(
            """
            SELECT concept_id, ticker, confidence
            FROM concept_aliases
            WHERE LOWER(alias_text) = LOWER(?)
              AND (ticker = ? OR ticker IS NULL)
            ORDER BY (ticker IS NOT NULL) DESC, confidence DESC
            LIMIT 1
            """,
            (alias_text, ticker),
        ).fetchone()
        return int(row["concept_id"]) if row else None
    finally:
        conn.close()


def record_concept_alias(
    *,
    concept_id: int,
    alias_text: str,
    ticker: str | None = None,
    alias_kind: str = "reported",
    confidence: float = 1.0,
    db_path: Path | str | None = None,
) -> int | None:
    conn = _open(db_path)
    if conn is None:
        return None
    try:
        now = _now_iso()
        existing = conn.execute(
            """
            SELECT id FROM concept_aliases
            WHERE concept_id = ? AND COALESCE(ticker,'') = COALESCE(?, '') AND alias_text = ?
            """,
            (concept_id, ticker, alias_text),
        ).fetchone()
        if existing is not None:
            conn.execute(
                "UPDATE concept_aliases SET last_observed_at = ? WHERE id = ?",
                (now, int(existing["id"])),
            )
            conn.commit()
            return int(existing["id"])
        cur = conn.execute(
            """
            INSERT INTO concept_aliases(
                concept_id, ticker, alias_text, alias_kind,
                first_observed_at, last_observed_at, confidence
            ) VALUES (?,?,?,?,?,?,?)
            """,
            (concept_id, ticker, alias_text, alias_kind, now, now, confidence),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    except sqlite3.Error as exc:
        log.warning({"event": "concept_alias_failed", "error": str(exc)})
        return None
    finally:
        conn.close()


def record_concept_definition(
    *,
    concept_id: int,
    ticker: str,
    effective_from: datetime,
    definition_md: str,
    evidence_doc_id: int | None = None,
    evidence_excerpt: str | None = None,
    computation_change_md: str | None = None,
    db_path: Path | str | None = None,
) -> int | None:
    """Record a new definition. Supersedes any prior current definition for
    (concept, ticker) so the history is queryable."""
    conn = _open(db_path)
    if conn is None:
        return None
    try:
        cur = conn.execute(
            """
            INSERT INTO concept_definitions(
                concept_id, ticker, effective_from, effective_to,
                definition_md, evidence_doc_id, evidence_excerpt,
                computation_change_md, superseded_by_id
            ) VALUES (?,?,?,?,?,?,?,?,NULL)
            """,
            (
                concept_id,
                ticker,
                effective_from.isoformat(),
                None,
                definition_md,
                evidence_doc_id,
                evidence_excerpt,
                computation_change_md,
            ),
        )
        new_id = int(cur.lastrowid or 0)
        # Supersede any prior current row
        conn.execute(
            """
            UPDATE concept_definitions
            SET superseded_by_id = ?,
                effective_to = ?
            WHERE concept_id = ? AND ticker = ? AND id != ?
              AND superseded_by_id IS NULL
            """,
            (new_id, effective_from.isoformat(), concept_id, ticker, new_id),
        )
        conn.commit()
        return new_id
    except sqlite3.Error as exc:
        log.warning({"event": "concept_def_failed", "error": str(exc)})
        return None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Extractions
# ---------------------------------------------------------------------------


def record_extraction(
    *,
    source_doc_id: int,
    extraction_kind: str,
    extractor_id: str,
    extractor_version: str,
    payload: dict[str, object] | list[object],
    confidence: float = 1.0,
    char_offset_start: int | None = None,
    char_offset_end: int | None = None,
    links_to: dict[str, int] | None = None,
    db_path: Path | str | None = None,
) -> int | None:
    conn = _open(db_path)
    if conn is None:
        return None
    try:
        cur = conn.execute(
            """
            INSERT INTO extractions(
                source_doc_id, char_offset_start, char_offset_end,
                extraction_kind, extractor_id, extractor_version,
                payload_json, confidence, extracted_at, links_to_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                source_doc_id,
                char_offset_start,
                char_offset_end,
                extraction_kind,
                extractor_id,
                extractor_version,
                json.dumps(payload, ensure_ascii=False),
                confidence,
                _now_iso(),
                json.dumps(links_to) if links_to else None,
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    except sqlite3.Error as exc:
        log.warning({"event": "extraction_record_failed", "error": str(exc)})
        return None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Mapping proposals — self-updating queue
# ---------------------------------------------------------------------------


def propose_mapping(
    *,
    kind: str,
    payload: dict[str, object],
    confidence: float,
    proposed_by: str = "llm_resolver_v1",
    ticker: str | None = None,
    source_doc_id: int | None = None,
    source_excerpt: str | None = None,
    db_path: Path | str | None = None,
) -> tuple[int | None, str]:
    """Write a mapping proposal. Returns (proposal_id, status).

    Confidence routing:
      >= AUTO_APPLY_THRESHOLD (0.85) → status='auto_applied', mapping immediately applied
      >= PENDING_REVIEW_FLOOR (0.50) → status='pending_review', queued for human
      <  PENDING_REVIEW_FLOOR        → status='rejected' (logged only, no action)
    """
    conn = _open(db_path)
    if conn is None:
        return (None, "db_unavailable")
    try:
        now = _now_iso()
        if confidence >= AUTO_APPLY_THRESHOLD:
            status = "auto_applied"
        elif confidence >= PENDING_REVIEW_FLOOR:
            status = "pending_review"
        else:
            status = "rejected"
        cur = conn.execute(
            """
            INSERT INTO mapping_proposals(
                kind, ticker, proposed_by, payload_json, confidence,
                source_doc_id, source_excerpt, status, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                kind,
                ticker,
                proposed_by,
                json.dumps(payload, ensure_ascii=False),
                confidence,
                source_doc_id,
                source_excerpt,
                status,
                now,
            ),
        )
        pid = int(cur.lastrowid or 0)
        if status == "auto_applied":
            applied_entity_id, applied_concept_id = _apply_proposal_payload(
                conn, kind=kind, payload=payload, ticker=ticker
            )
            conn.execute(
                """
                UPDATE mapping_proposals
                SET applied_at = ?, applied_to_entity_id = ?, applied_to_concept_id = ?,
                    decided_at = ?, decided_by = ?
                WHERE id = ?
                """,
                (now, applied_entity_id, applied_concept_id, now, "auto", pid),
            )
        conn.commit()
        return (pid, status)
    except sqlite3.Error as exc:
        log.warning({"event": "proposal_write_failed", "error": str(exc)})
        return (None, "error")
    finally:
        conn.close()


def pending_proposals(
    *,
    limit: int = 50,
    db_path: Path | str | None = None,
) -> list[MappingProposal]:
    conn = _open(db_path)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            """
            SELECT * FROM mapping_proposals
            WHERE status = 'pending_review'
            ORDER BY confidence DESC, created_at DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        out: list[MappingProposal] = []
        for r in rows:
            try:
                payload = json.loads(r["payload_json"])
            except (json.JSONDecodeError, TypeError):
                payload = {}
            payload_dict: dict[str, object] = (
                payload if isinstance(payload, dict) else {"raw": payload}
            )
            out.append(
                MappingProposal(
                    id=int(r["id"]),
                    kind=r["kind"],
                    ticker=r["ticker"],
                    proposed_by=r["proposed_by"],
                    payload=payload_dict,
                    confidence=float(r["confidence"]),
                    status=r["status"],
                    source_doc_id=r["source_doc_id"],
                    source_excerpt=r["source_excerpt"],
                    applied_to_entity_id=r["applied_to_entity_id"],
                    applied_to_concept_id=r["applied_to_concept_id"],
                )
            )
        return out
    finally:
        conn.close()


def decide_proposal(
    *,
    proposal_id: int,
    decision: str,  # 'apply' | 'reject'
    decided_by: str,
    notes: str | None = None,
    db_path: Path | str | None = None,
) -> bool:
    """Human review action. Apply: execute the proposal's mapping. Reject:
    mark for audit, no schema effect."""
    conn = _open(db_path)
    if conn is None:
        return False
    try:
        row = conn.execute(
            "SELECT kind, ticker, payload_json, status FROM mapping_proposals WHERE id = ?",
            (proposal_id,),
        ).fetchone()
        if row is None or row["status"] not in ("pending_review",):
            return False
        now = _now_iso()
        if decision == "apply":
            try:
                payload = json.loads(row["payload_json"])
            except (json.JSONDecodeError, TypeError):
                payload = {}
            payload_dict: dict[str, object] = (
                payload if isinstance(payload, dict) else {"raw": payload}
            )
            ent_id, con_id = _apply_proposal_payload(
                conn, kind=row["kind"], payload=payload_dict, ticker=row["ticker"]
            )
            conn.execute(
                """
                UPDATE mapping_proposals
                SET status = 'manually_revised', applied_at = ?,
                    applied_to_entity_id = ?, applied_to_concept_id = ?,
                    decided_at = ?, decided_by = ?, decision_notes = ?
                WHERE id = ?
                """,
                (now, ent_id, con_id, now, decided_by, notes, proposal_id),
            )
        else:
            conn.execute(
                """
                UPDATE mapping_proposals
                SET status = 'rejected', decided_at = ?, decided_by = ?, decision_notes = ?
                WHERE id = ?
                """,
                (now, decided_by, notes, proposal_id),
            )
        conn.commit()
        return True
    except sqlite3.Error as exc:
        log.warning({"event": "proposal_decide_failed", "error": str(exc)})
        return False
    finally:
        conn.close()


def _apply_proposal_payload(
    conn: sqlite3.Connection,
    *,
    kind: str,
    payload: dict[str, object],
    ticker: str | None,
) -> tuple[int | None, int | None]:
    """Execute a proposal's payload against the schema. Returns
    (applied_entity_id, applied_concept_id) — at most one is non-None.

    Payload schema per kind:
      new_alias            — {"entity_id": int, "alias_text": str, "alias_kind": str}
      new_entity           — {"kind": str, "canonical_name": str, ...all entity fields}
      new_concept          — {"canonical_name": str, "kind": str, ...all concept fields}
      new_concept_alias    — {"concept_id": int, "ticker": str?, "alias_text": str}
      new_relationship     — {"from_entity_id": int, "to_entity_id": int,
                              "relationship_kind": str, "effective_from": str?}
      concept_redefinition — {"concept_id": int, "ticker": str, "effective_from": str,
                              "definition_md": str, "change_md": str?}
    """
    now = _now_iso()
    if kind == "new_alias":
        eid = payload.get("entity_id")
        text = payload.get("alias_text")
        akind = str(payload.get("alias_kind") or "reported_in_10k")
        if isinstance(eid, (int, float)) and isinstance(text, str):
            conn.execute(
                """
                INSERT OR IGNORE INTO entity_aliases(
                    entity_id, alias_text, alias_kind,
                    first_observed_at, last_observed_at, observation_count, confidence
                ) VALUES (?,?,?,?,?,1,1.0)
                """,
                (int(eid), text, akind, now, now),
            )
            return (int(eid), None)
    elif kind == "new_entity":
        ekind = str(payload.get("kind") or "company")
        cname = str(payload.get("canonical_name") or "")
        if cname:
            cur = conn.execute(
                "SELECT id FROM entities WHERE kind = ? AND canonical_name = ?",
                (ekind, cname),
            ).fetchone()
            if cur is not None:
                return (int(cur["id"]), None)
            insert = conn.execute(
                """
                INSERT INTO entities(
                    kind, canonical_name, display_name, external_ids,
                    parent_entity_id, meta_json, created_at, last_observed_at
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    ekind,
                    cname,
                    payload.get("display_name")
                    if isinstance(payload.get("display_name"), str)
                    else None,
                    json.dumps(payload.get("external_ids"))
                    if payload.get("external_ids")
                    else None,
                    payload.get("parent_entity_id")
                    if isinstance(payload.get("parent_entity_id"), int)
                    else None,
                    json.dumps(payload.get("meta")) if payload.get("meta") else None,
                    now,
                    now,
                ),
            )
            new_id = int(insert.lastrowid or 0)
            conn.execute(
                """
                INSERT OR IGNORE INTO entity_aliases(
                    entity_id, alias_text, alias_kind,
                    first_observed_at, last_observed_at, observation_count, confidence
                ) VALUES (?,?,?,?,?,1,1.0)
                """,
                (new_id, cname, "manual", now, now),
            )
            return (new_id, None)
    elif kind == "new_concept":
        cname = str(payload.get("canonical_name") or "")
        ckind = str(payload.get("kind") or "kpi")
        if cname:
            cur = conn.execute(
                "SELECT id FROM concepts WHERE canonical_name = ?", (cname,)
            ).fetchone()
            if cur is not None:
                return (None, int(cur["id"]))
            insert = conn.execute(
                """
                INSERT INTO concepts(kind, canonical_name, unit_kind, taxonomy_xbrl_tag, generic_definition_md, created_at)
                VALUES (?,?,?,?,?,?)
                """,
                (
                    ckind,
                    cname,
                    payload.get("unit_kind") if isinstance(payload.get("unit_kind"), str) else None,
                    payload.get("taxonomy_xbrl_tag")
                    if isinstance(payload.get("taxonomy_xbrl_tag"), str)
                    else None,
                    payload.get("generic_definition_md")
                    if isinstance(payload.get("generic_definition_md"), str)
                    else None,
                    now,
                ),
            )
            new_id = int(insert.lastrowid or 0)
            conn.execute(
                """
                INSERT OR IGNORE INTO concept_aliases(
                    concept_id, ticker, alias_text, alias_kind,
                    first_observed_at, last_observed_at, confidence
                ) VALUES (?, NULL, ?, 'manual', ?, ?, 1.0)
                """,
                (new_id, cname, now, now),
            )
            return (None, new_id)
    elif kind == "new_concept_alias":
        cid = payload.get("concept_id")
        text = payload.get("alias_text")
        if isinstance(cid, (int, float)) and isinstance(text, str):
            conn.execute(
                """
                INSERT OR IGNORE INTO concept_aliases(
                    concept_id, ticker, alias_text, alias_kind,
                    first_observed_at, last_observed_at, confidence
                ) VALUES (?,?,?,?,?,?,1.0)
                """,
                (
                    int(cid),
                    ticker,
                    text,
                    str(payload.get("alias_kind") or "reported"),
                    now,
                    now,
                ),
            )
            return (None, int(cid))
    elif kind == "new_relationship":
        f = payload.get("from_entity_id")
        t = payload.get("to_entity_id")
        rk = payload.get("relationship_kind")
        if isinstance(f, (int, float)) and isinstance(t, (int, float)) and isinstance(rk, str):
            conn.execute(
                """
                INSERT OR IGNORE INTO entity_relationships(
                    from_entity_id, relationship_kind, to_entity_id, effective_from, confidence
                ) VALUES (?,?,?,?,1.0)
                """,
                (
                    int(f),
                    rk,
                    int(t),
                    payload.get("effective_from")
                    if isinstance(payload.get("effective_from"), str)
                    else None,
                ),
            )
            return (int(f), None)
    elif kind == "concept_redefinition":
        cid = payload.get("concept_id")
        tk = payload.get("ticker") or ticker
        ef = payload.get("effective_from")
        df = payload.get("definition_md")
        if (
            isinstance(cid, (int, float))
            and isinstance(tk, str)
            and isinstance(ef, str)
            and isinstance(df, str)
        ):
            insert = conn.execute(
                """
                INSERT INTO concept_definitions(
                    concept_id, ticker, effective_from, effective_to,
                    definition_md, computation_change_md
                ) VALUES (?,?,?,NULL,?,?)
                """,
                (
                    int(cid),
                    tk,
                    ef,
                    df,
                    payload.get("change_md") if isinstance(payload.get("change_md"), str) else None,
                ),
            )
            new_id = int(insert.lastrowid or 0)
            conn.execute(
                """
                UPDATE concept_definitions
                SET superseded_by_id = ?, effective_to = ?
                WHERE concept_id = ? AND ticker = ? AND id != ? AND superseded_by_id IS NULL
                """,
                (new_id, ef, int(cid), tk, new_id),
            )
            return (None, int(cid))
    return (None, None)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _loads_dict(raw: object) -> dict[str, object]:
    if raw is None:
        return {}
    try:
        decoded = json.loads(str(raw))
        return decoded if isinstance(decoded, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _parse_dt(raw: object) -> datetime | None:
    if raw is None:
        return None
    # Normalize to naive-UTC: rows written before the naive-UTC migration carry
    # an aware +00:00 offset; strip it so old (aware) and new (naive) effective-
    # dating stamps compare without TypeError.
    if isinstance(raw, datetime):
        return to_naive_utc(raw)
    try:
        return to_naive_utc(datetime.fromisoformat(str(raw)))
    except ValueError:
        return None
