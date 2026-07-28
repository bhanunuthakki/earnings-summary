"""Fail-closed identity and source-authority guard for inventory writers."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from provenance.issuer_registry import (
    IssuerRegistry,
    UnresolvedIssuerIdentityError,
    normalize_identifier,
)


class InventoryIdentityError(RuntimeError):
    """The requested ticker, issuer, regulator ID, and source do not cohere."""


class InventorySubject(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    issuer_id: str
    ticker: str
    source_url: str
    registry_enforced: bool = True


def issuer_registry_available(conn: sqlite3.Connection) -> bool:
    required = {
        "issuer_entities",
        "issuer_identifier_resolution_outcomes",
        "issuer_authority_surface_revisions",
        "legacy_issuer_binding_revisions",
    }
    tables = {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    return required <= tables


def resolve_sec_inventory_subject(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    cik: str,
    knowledge_at: datetime,
) -> InventorySubject:
    """Require ticker, CIK, canonical issuer, and SEC endpoint to agree."""

    registry = IssuerRegistry(conn)
    normalized_ticker = ticker.strip().upper()
    normalized_cik = normalize_identifier("sec_cik", cik)
    source_url = f"https://data.sec.gov/submissions/CIK{normalized_cik}.json"
    try:
        cik_issuer = registry.resolve_identifier(
            "sec_cik",
            normalized_cik,
            knowledge_at=knowledge_at,
        )
        ticker_issuer = registry.canonicalize_recorded_issuer(
            f"legacy-ticker:{normalized_ticker}",
            knowledge_at=knowledge_at,
        )
    except UnresolvedIssuerIdentityError as exc:
        raise InventoryIdentityError(
            f"ticker or CIK has no canonical issuer: {type(exc).__name__}"
        ) from exc
    if cik_issuer.issuer_id != ticker_issuer.issuer_id:
        raise InventoryIdentityError("ticker and SEC CIK resolve to different canonical issuers")
    surfaces = registry.source_authority(
        cik_issuer.issuer_id,
        "sec_submissions",
        knowledge_at=knowledge_at,
    )
    if not any(surface.source_url == source_url for surface in surfaces):
        raise InventoryIdentityError(
            "SEC submissions URL is not a verified authority surface for issuer"
        )
    return InventorySubject(
        issuer_id=cik_issuer.issuer_id,
        ticker=normalized_ticker,
        source_url=source_url,
    )


def resolve_ir_inventory_subject(
    conn: sqlite3.Connection,
    *,
    issuer_id: str,
    ticker: str,
    ir_url: str,
    knowledge_at: datetime,
) -> InventorySubject:
    """Require caller issuer, ticker binding, and IR home authority to agree."""

    registry = IssuerRegistry(conn)
    normalized_ticker = ticker.strip().upper()
    try:
        ticker_issuer = registry.canonicalize_recorded_issuer(
            f"legacy-ticker:{normalized_ticker}",
            knowledge_at=knowledge_at,
        )
    except UnresolvedIssuerIdentityError as exc:
        raise InventoryIdentityError(
            f"ticker has no canonical issuer: {type(exc).__name__}"
        ) from exc
    if issuer_id != ticker_issuer.issuer_id:
        raise InventoryIdentityError(
            "caller issuer and ticker resolve to different canonical issuers"
        )
    surfaces = registry.source_authority(
        issuer_id,
        "ir_home",
        knowledge_at=knowledge_at,
    )
    if not any(surface.source_url == ir_url for surface in surfaces):
        raise InventoryIdentityError("IR URL is not a verified authority surface for issuer")
    return InventorySubject(
        issuer_id=issuer_id,
        ticker=normalized_ticker,
        source_url=ir_url,
    )
