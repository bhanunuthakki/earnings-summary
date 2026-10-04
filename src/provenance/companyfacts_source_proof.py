"""Reconstruct an exact raw entry from a retained first-party SEC snapshot."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

from provenance.issuer_registry import IssuerRegistry
from provenance.legacy_fact_evidence_match import CompanyFactsRelocatedLocator
from provenance.metric_ontology import canonical_json
from provenance.sec_companyfacts_capture import CompanyFactEntry, parse_companyfacts_body


def read_companyfacts_snapshot(
    conn: sqlite3.Connection, document_version_id: str, *, issuer_id: str, cutoff: datetime
) -> bytes:
    row = conn.execute(
        "SELECT document.issuer_id,document.blob_sha256,blob.storage_uri,blob.byte_size,document.recorded_at,observation.source_url,observation.observed_at,observation.retrieved_at FROM evidence_document_versions document JOIN evidence_content_blobs blob ON blob.sha256=document.blob_sha256 JOIN evidence_source_observations observation ON observation.observation_id=document.observation_id WHERE document.document_version_id=? AND document.document_type='companyfacts_snapshot'",
        (document_version_id,),
    ).fetchone()
    if row is None or str(row[0]) != issuer_id:
        raise ValueError("CompanyFacts raw snapshot issuer/document mismatch")
    for index in (4, 6, 7):
        clock = datetime.fromisoformat(str(row[index]))
        clock = clock.replace(tzinfo=UTC) if clock.tzinfo is None else clock.astimezone(UTC)
        if clock > cutoff:
            raise ValueError("CompanyFacts raw snapshot is after cutoff")
    url, uri = urlparse(str(row[5])), urlparse(str(row[2]))
    if (
        url.scheme != "https"
        or url.hostname != "data.sec.gov"
        or not url.path.startswith("/api/xbrl/companyfacts/CIK")
        or url.query
        or url.fragment
    ):
        raise ValueError("CompanyFacts raw snapshot is not the first-party SEC source")
    if uri.scheme != "file" or uri.netloc not in {"", "localhost"} or uri.query or uri.fragment:
        raise ValueError("CompanyFacts immutable blob requires an exact local file URI")
    path = Path(url2pathname(uri.path))
    if not path.is_absolute():
        raise ValueError("CompanyFacts immutable blob path must be absolute")
    try:
        body = path.read_bytes()
    except OSError as exc:
        raise ValueError("CompanyFacts immutable source bytes are unavailable") from exc
    if len(body) != int(row[3]) or hashlib.sha256(body).hexdigest() != str(row[1]):
        raise ValueError("CompanyFacts immutable bytes/hash conflict")
    raw = json.loads(body)
    cik = str(raw["cik"]).zfill(10)
    issuer = IssuerRegistry(conn).resolve_identifier("sec_cik", cik, knowledge_at=cutoff)
    if (
        issuer.issuer_id != issuer_id
        or issuer.material_dissent
        or url.path != f"/api/xbrl/companyfacts/CIK{cik}.json"
    ):
        raise ValueError("CompanyFacts raw CIK does not establish the exact reviewed issuer")
    return body


def exact_companyfacts_entry(
    body: bytes, locator: CompanyFactsRelocatedLocator, *, entry_sha256: str
) -> CompanyFactEntry:
    expected_path = (
        f"facts.{locator.namespace}.{locator.concept}.units.{locator.unit}[{locator.entry_index}]"
    )
    if locator.json_path != expected_path:
        raise ValueError("CompanyFacts raw JSON path does not match exact coordinates")
    raw_payload = json.loads(body)
    try:
        raw_value = raw_payload["facts"][locator.namespace][locator.concept]["units"][locator.unit][
            locator.entry_index
        ]["val"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("CompanyFacts raw reviewed value is absent") from exc
    if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
        raise ValueError("CompanyFacts reported value must be a raw number")
    payload = parse_companyfacts_body(body, expected_cik=str(json.loads(body)["cik"]).zfill(10))
    try:
        entry = payload.facts[locator.namespace][locator.concept].units[locator.unit][
            locator.entry_index
        ]
    except (KeyError, IndexError) as exc:
        raise ValueError("CompanyFacts raw reviewed entry is absent") from exc
    entry_json = canonical_json(entry.model_dump(mode="json", exclude_none=False))
    if (
        hashlib.sha256(entry_json.encode()).hexdigest() != entry_sha256
        or entry.accn != locator.accession_number
    ):
        raise ValueError("CompanyFacts raw entry conflicts with exact hash/accession proof")
    return entry
