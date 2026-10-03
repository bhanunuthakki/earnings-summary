"""Replay one MELI quarter from an existing, exact-byte HTML observation.

This is an offline discovery adapter, not capture or publisher archive authority.
It reads captured HTML response bytes; it does not claim a rendered DOM.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from ir_pipeline.authority import PublisherEndpointRule
from ir_pipeline.discover.meli import (
    MeliEmbeddedQuarterlyInventory,
    discover_embedded_quarterly_inventory,
)
from provenance.evidence_native_candidates import resolve_local_storage_uri
from provenance.immutable_artifact import read_stable_artifact
from provenance.inventory_identity import resolve_ir_inventory_subject


class CapturedMeliQuarter(BaseModel):
    """Derived quarter and its original publisher HTML observation commitment."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    scope_kind: Literal["meli_selected_quarter"] = "meli_selected_quarter"
    source_representation: Literal["captured_html"] = "captured_html"
    source_observation_id: str = Field(min_length=1, max_length=128)
    raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    inventory: MeliEmbeddedQuarterlyInventory
    publisher_file_rules: tuple[PublisherEndpointRule, ...] = ()


def load_captured_meli_quarter(
    conn: sqlite3.Connection,
    *,
    issuer_id: str,
    ticker: str,
    ir_url: str,
    source_observation_id: str,
    fiscal_year: int,
    fiscal_quarter: int,
    publisher_file_rules: tuple[PublisherEndpointRule, ...],
    blob_root: Path,
    knowledge_at: datetime,
) -> CapturedMeliQuarter:
    """Verify source identity, current local bytes and endpoints before deriving links."""

    if ticker.strip().upper() != "MELI":
        raise ValueError("selected-quarter adapter requires MELI")
    resolve_ir_inventory_subject(
        conn, issuer_id=issuer_id, ticker=ticker, ir_url=ir_url, knowledge_at=knowledge_at
    )
    row = conn.execute(
        "SELECT o.source_url,o.blob_sha256,o.source_kind,o.observed_at,o.retrieved_at,"
        "b.byte_size,b.media_type,b.recorded_at "
        "FROM evidence_source_observations o JOIN evidence_content_blobs b "
        "ON b.sha256=o.blob_sha256 WHERE o.observation_id=?",
        (source_observation_id,),
    ).fetchone()
    if row is None or row[0] != ir_url:
        raise ValueError("captured publisher observation URL mismatch or missing")
    if row[2] not in {"ir_publisher_home_authority", "ir_publisher_authority"}:
        raise ValueError("original publisher HTML capture required")
    if str(row[6]).partition(";")[0].strip().lower() not in {"text/html", "application/xhtml+xml"}:
        raise ValueError("captured HTML media type required")
    if not 0 < int(row[5]) <= 25_000_000:
        raise ValueError("captured HTML size exceeds discovery bound")
    cutoff = knowledge_at.replace(tzinfo=UTC) if knowledge_at.tzinfo is None else knowledge_at
    for value in (row[3], row[4], row[7]):
        if value is None:
            raise ValueError("captured HTML evidence clock missing")
        stamp = datetime.fromisoformat(str(value))
        if (stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp) > cutoff:
            raise ValueError("captured HTML is newer than inventory cutoff")
    locations = conn.execute(
        "SELECT storage_uri FROM v_evidence_blob_locations_current "
        "WHERE blob_sha256=? AND availability_state='present' AND location_kind='local' "
        "AND verified_sha256=? AND verified_byte_size=? ORDER BY storage_uri",
        (row[1], row[1], row[5]),
    ).fetchall()
    paths = [
        resolve_local_storage_uri(str(item[0]), allowed_roots=(blob_root,)) for item in locations
    ]
    path = next((item for item in paths if item is not None), None)
    if path is None:
        raise ValueError("captured HTML has no verified location within blob root")
    if path.stat().st_size != int(row[5]):
        raise ValueError("captured HTML byte size mismatch")
    snapshot, raw = read_stable_artifact(path)
    if snapshot.file_sha256 != row[1] or snapshot.size_bytes != row[5]:
        raise ValueError("captured HTML byte commitment mismatch")
    inventory = discover_embedded_quarterly_inventory(
        raw.decode("utf-8", errors="strict"),
        source_page=ir_url,
        fiscal_year=fiscal_year,
        fiscal_quarter=fiscal_quarter,
    )
    hostname = urlsplit(ir_url).hostname
    if hostname is None:
        raise ValueError("publisher source host missing")
    rules = (PublisherEndpointRule(host=hostname), *publisher_file_rules)
    if any(not any(rule.allows(doc.source_url) for rule in rules) for doc in inventory.documents):
        raise ValueError("quarterly document endpoint is not authorized")
    return CapturedMeliQuarter(
        source_observation_id=source_observation_id,
        raw_sha256=str(row[1]),
        inventory=inventory,
        publisher_file_rules=publisher_file_rules,
    )
