"""VAL-SEARCH-002/003: sealed citation hrefs resolve to their provenance chain.

Everything here is hermetic and synthetic. The fixture corpus defines the
documents, observations, evidence nodes, search chunks, canonical fact
projection entries, and the persisted claim-to-citation mapping; the cockpit
route ``/source/version/<document_version_id>?node=<node_id>`` must resolve each
fixture citation to HTTP 200 and expose the issuer, fiscal period, source
document identity, cited locator, observation version, and reported fact value
matching the fixture's immutable observation version. An unknown document
version, an unknown node, or a node from a different document version must 404
honestly.

No live sealed Ask turn, real Claude call, SEC/source request, portfolio or bench
database access, or sealed-retrieval reachability claim is made. Setting
``SEARCH_VALIDATION_EVIDENCE_DIR`` records the contract's evidence artifacts
under that directory during the same pytest run; ordinary runs write nothing
outside pytest tmp.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import cast

import comments_server
import pytest
from flask.testing import FlaskClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SEALED_RETRIEVAL_SOURCE = PROJECT_ROOT / "src" / "ask" / "sealed_retrieval.py"

_EVIDENCE_DIR_ENV = "SEARCH_VALIDATION_EVIDENCE_DIR"

# The exact href template the sealed lane emits for BOTH narrative and fact
# citations (src/ask/sealed_retrieval.py ``_narrative_item`` / ``_fact_item``).
# The route must serve this shape; the coupling test below fails if the sealed
# lane changes it without the route.
_SEALED_HREF_TEMPLATE = (
    "/source/version/{quote(version_id, safe='')}?node={quote(node_id, safe='')}"
)

_SCOPE_NOTE = (
    "Hermetic fixture evidence only: no live sealed Ask turn, no real Claude call, "
    "no SEC or source request, no portfolio or bench database access, and no claim "
    "that sealed retrieval is reachable or ready in this or any deployment."
)

# --- fixture corpus -----------------------------------------------------------------

_ISSUER_ID = "issuer-acme"
_TICKER = "ACME"
_OBSERVATION_ID = "obs-acme-10k-fy2025"
_DOCUMENT_VERSION_ID = "dv-acme-10k-fy2025-v1"
_DOCUMENT_REF = "acme-10k-fy2025"
_DOCUMENT_TYPE = "annual_report"
_FORM_TYPE = "10-K"
_ACCESSION_NUMBER = "0001234567-25-000123"
_PERIOD_START = "2025-01-01"
_PERIOD_END = "2025-12-31"
_LANGUAGE = "en"
_SOURCE_KIND = "sec_filing"
_SOURCE_URL = "https://www.sec.gov/Archives/edgar/data/1234567/000123456725000123/acme-10k.htm"
_BLOB_SHA256 = "a" * 64
_OBSERVED_AT = "2026-01-15 14:00:00"
_RETRIEVED_AT = "2026-01-15 14:00:05"
_RECORDED_AT = "2026-01-15 14:05:00"
_EXTRACTION_RUN_ID = "run-acme-10k-fy2025"
_MANIFEST_ID = "manifest-acme-1"

_NARRATIVE_NODE_ID = "node-acme-risk-factors"
_NARRATIVE_NODE_KIND = "section"
_NARRATIVE_NODE_TEXT = "Credit quality deteriorated in the youngest vintages during fiscal 2025."
_NARRATIVE_CHUNK_ID = "chunk-acme-risk-factors"
_NARRATIVE_LOCATOR = {"section": "Item 1A"}

_FACT_NODE_ID = "node-acme-total-revenue"
_FACT_NODE_KIND = "table_cell"
_FACT_NODE_TEXT = "Total revenue 1,234.5"
_FACT_CHUNK_ID = "chunk-acme-total-revenue"
_FACT_LOCATOR = {"page": 42, "table": "Consolidated Statements of Operations", "cell": "B7"}
_METRIC_CELL_ID = "cell-acme-total-revenue-fy2025"
_METRIC_NAME = "Total revenue"
_METRIC_VALUE = "1234.5"
_METRIC_UNIT = "USD_millions"
_METRIC_CURRENCY = "USD"
_GENERATION_ID = "generation-acme-1"

# A second, independent document version whose node must NOT resolve under the
# first version (the cross-version mismatch 404 case).
_OTHER_OBSERVATION_ID = "obs-acme-8k-2026-01"
_OTHER_DOCUMENT_VERSION_ID = "dv-acme-8k-2026-01-v1"
_OTHER_DOCUMENT_REF = "acme-8k-2026-01"
_OTHER_NODE_ID = "node-acme-8k-item-2-02"
_OTHER_EXTRACTION_RUN_ID = "run-acme-8k-2026-01"
_OTHER_BLOB_SHA256 = "b" * 64
_OTHER_FORM_TYPE = "8-K"
_OTHER_DOCUMENT_TYPE = "current_report"
_OTHER_PERIOD_END = "2026-01-10"

# The persisted claim-to-citation mapping (VAL-SEARCH-003).
_ANSWER_ID = "answer-fixture-1"
_CLAIM_TEXT = "ACME reported total revenue of 1234.5 for fiscal 2025 [1]."
_CLAIM_ORDINAL = 0
_CITATION_NUMBER = 1
# The sealed item's rendered value appends the currency/unit token; the stored
# observation value is the bare canonical value.
_CITATION_VALUE = "1234.5 USD"
_DISPLAY_NORMALIZATION = (
    "rendered fact value equals the stored canonical value exactly; the citation "
    "record's '1234.5 USD' value is the bare canonical value plus its currency "
    "token (no rounding, scaling, or reformatting)"
)


def _sealed_source_version_href(document_version_id: str, node_id: str) -> str:
    """Build the href exactly as the sealed lane's f-string template does."""
    return (
        f"/source/version/{urllib.parse.quote(document_version_id, safe='')}"
        f"?node={urllib.parse.quote(node_id, safe='')}"
    )


def _record_artifact(name: str, payload: dict[str, object]) -> None:
    """Record one VAL-SEARCH evidence artifact when the run pins the directory."""
    root = os.environ.get(_EVIDENCE_DIR_ENV, "").strip()
    if not root:
        return
    path = Path(root) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _fixture_corpus_definition() -> dict[str, object]:
    return {
        "issuer": {"issuer_id": _ISSUER_ID, "ticker": _TICKER},
        "observation_version": {
            "document_version_id": _DOCUMENT_VERSION_ID,
            "version_sequence": 1,
            "observation_id": _OBSERVATION_ID,
            "form_type": _FORM_TYPE,
            "document_type": _DOCUMENT_TYPE,
            "accession_number": _ACCESSION_NUMBER,
            "period_start": _PERIOD_START,
            "period_end": _PERIOD_END,
            "blob_sha256": _BLOB_SHA256,
            "source_url": _SOURCE_URL,
            "observed_at": _OBSERVED_AT,
            "retrieved_at": _RETRIEVED_AT,
            "recorded_at": _RECORDED_AT,
        },
        "nodes": [
            {
                "node_id": _NARRATIVE_NODE_ID,
                "node_kind": _NARRATIVE_NODE_KIND,
                "locator": _NARRATIVE_LOCATOR,
                "chunk_id": _NARRATIVE_CHUNK_ID,
            },
            {
                "node_id": _FACT_NODE_ID,
                "node_kind": _FACT_NODE_KIND,
                "locator": _FACT_LOCATOR,
                "chunk_id": _FACT_CHUNK_ID,
            },
        ],
        "fact_projection_entry": {
            "canonical_metric_cell_id": _METRIC_CELL_ID,
            "canonical_metric_name": _METRIC_NAME,
            "canonical_value": _METRIC_VALUE,
            "unit_key": _METRIC_UNIT,
            "currency": _METRIC_CURRENCY,
            "period_end": _PERIOD_END,
            "evidence_node_id": _FACT_NODE_ID,
        },
        "persisted_claim_mapping": {
            "answer_id": _ANSWER_ID,
            "claim_ordinal": _CLAIM_ORDINAL,
            "citation_number": _CITATION_NUMBER,
        },
        "href_template": _SEALED_HREF_TEMPLATE,
    }


# --- hermetic fixture -----------------------------------------------------------------


def _drop_fixture_triggers(conn: sqlite3.Connection) -> None:
    """Drop the append-only/provenance triggers on the disposable fixture copy.

    The route and the reconciliation are strictly read-only. The triggers police
    the write path of a full sealed turn (verified traces, sealed projections,
    audit seals), which M4 excludes; retaining them would require fabricating an
    entire sealed retrieval substrate rather than a citation fixture. The real
    head schema, its column names, and its CHECK constraints are retained, so the
    route's SQL is still validated against production column names.
    """
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
        conn.execute(f'DROP TRIGGER "{name}"')


def _seed_fixture(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        _drop_fixture_triggers(conn)
        conn.execute(
            "INSERT INTO evidence_source_observations (observation_id, idempotency_key,"
            "source_kind, source_url, blob_sha256, source_published_at, filing_at,"
            "observed_at, retrieved_at, retrieval_config_sha256, collector_code_version)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                _OBSERVATION_ID,
                "idem-obs-acme-10k",
                _SOURCE_KIND,
                _SOURCE_URL,
                _BLOB_SHA256,
                _PERIOD_END,
                _PERIOD_END,
                _OBSERVED_AT,
                _RETRIEVED_AT,
                "c" * 64,
                "fixture-collector.v1",
            ),
        )
        conn.execute(
            "INSERT INTO evidence_source_observations (observation_id, idempotency_key,"
            "source_kind, source_url, blob_sha256, source_published_at, filing_at,"
            "observed_at, retrieved_at, retrieval_config_sha256, collector_code_version)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                _OTHER_OBSERVATION_ID,
                "idem-obs-acme-8k",
                _SOURCE_KIND,
                "https://www.sec.gov/Archives/edgar/data/1234567/000123456725000999/acme-8k.htm",
                _OTHER_BLOB_SHA256,
                _OTHER_PERIOD_END,
                _OTHER_PERIOD_END,
                _OBSERVED_AT,
                _RETRIEVED_AT,
                "d" * 64,
                "fixture-collector.v1",
            ),
        )
        conn.execute(
            "INSERT INTO evidence_document_versions (document_version_id, document_key,"
            "version_sequence, observation_id, blob_sha256, issuer_id, ticker,"
            "document_type, form_type, accession_number, period_start, period_end,"
            "as_of_at, language, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                _DOCUMENT_VERSION_ID,
                _DOCUMENT_REF,
                1,
                _OBSERVATION_ID,
                _BLOB_SHA256,
                _ISSUER_ID,
                _TICKER,
                _DOCUMENT_TYPE,
                _FORM_TYPE,
                _ACCESSION_NUMBER,
                _PERIOD_START,
                _PERIOD_END,
                _PERIOD_END,
                _LANGUAGE,
                _RECORDED_AT,
            ),
        )
        conn.execute(
            "INSERT INTO evidence_document_versions (document_version_id, document_key,"
            "version_sequence, observation_id, blob_sha256, issuer_id, ticker,"
            "document_type, form_type, accession_number, period_end, as_of_at, language,"
            "recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                _OTHER_DOCUMENT_VERSION_ID,
                _OTHER_DOCUMENT_REF,
                1,
                _OTHER_OBSERVATION_ID,
                _OTHER_BLOB_SHA256,
                _ISSUER_ID,
                _TICKER,
                _OTHER_DOCUMENT_TYPE,
                _OTHER_FORM_TYPE,
                None,
                _OTHER_PERIOD_END,
                _OTHER_PERIOD_END,
                _LANGUAGE,
                _RECORDED_AT,
            ),
        )
        conn.execute(
            "INSERT INTO evidence_extraction_runs (extraction_run_id, idempotency_key,"
            "document_version_id, input_sha256, extractor_name, extractor_config_sha256,"
            "extractor_code_version, output_sha256, started_at, completed_at, outcome)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                _EXTRACTION_RUN_ID,
                "idem-run-acme-10k",
                _DOCUMENT_VERSION_ID,
                _BLOB_SHA256,
                "fixture-extractor",
                "e" * 64,
                "fixture-extractor.v1",
                "f" * 64,
                _OBSERVED_AT,
                _RETRIEVED_AT,
                "succeeded",
            ),
        )
        conn.execute(
            "INSERT INTO evidence_extraction_runs (extraction_run_id, idempotency_key,"
            "document_version_id, input_sha256, extractor_name, extractor_config_sha256,"
            "extractor_code_version, output_sha256, started_at, completed_at, outcome)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                _OTHER_EXTRACTION_RUN_ID,
                "idem-run-acme-8k",
                _OTHER_DOCUMENT_VERSION_ID,
                _OTHER_BLOB_SHA256,
                "fixture-extractor",
                "e" * 64,
                "fixture-extractor.v1",
                "0" * 64,
                _OBSERVED_AT,
                _RETRIEVED_AT,
                "succeeded",
            ),
        )
        for node_id, node_kind, text, locator, run_id in (
            (
                _NARRATIVE_NODE_ID,
                _NARRATIVE_NODE_KIND,
                _NARRATIVE_NODE_TEXT,
                _NARRATIVE_LOCATOR,
                _EXTRACTION_RUN_ID,
            ),
            (_FACT_NODE_ID, _FACT_NODE_KIND, _FACT_NODE_TEXT, _FACT_LOCATOR, _EXTRACTION_RUN_ID),
            (
                _OTHER_NODE_ID,
                "section",
                "Item 2.02 Results of Operations.",
                {"section": "Item 2.02"},
                _OTHER_EXTRACTION_RUN_ID,
            ),
        ):
            conn.execute(
                "INSERT INTO evidence_nodes (node_id, evidence_key, revision,"
                "extraction_run_id, node_kind, text, locator_json, recorded_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    node_id,
                    f"key-{node_id}",
                    1,
                    run_id,
                    node_kind,
                    text,
                    json.dumps(locator, sort_keys=True),
                    _RECORDED_AT,
                ),
            )
        conn.execute(
            "INSERT INTO search_corpus_manifests (manifest_id, idempotency_key, corpus_key,"
            "revision, selection_config_sha256, selector_code_version, recorded_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                _MANIFEST_ID,
                "idem-manifest-acme-1",
                "acme-corpus",
                1,
                "1" * 64,
                "fixture.v1",
                _RECORDED_AT,
            ),
        )
        conn.execute(
            "INSERT INTO search_corpus_document_memberships (membership_id, manifest_id,"
            "expected_document_key, document_version_id, membership_status, reason,"
            "recorded_at) VALUES (?,?,?,?,?,?,?)",
            (
                "membership-acme-10k",
                _MANIFEST_ID,
                _DOCUMENT_REF,
                _DOCUMENT_VERSION_ID,
                "included",
                "fixture corpus membership",
                _RECORDED_AT,
            ),
        )
        for chunk_id, node_id, text in (
            (_NARRATIVE_CHUNK_ID, _NARRATIVE_NODE_ID, _NARRATIVE_NODE_TEXT),
            (_FACT_CHUNK_ID, _FACT_NODE_ID, _FACT_NODE_TEXT),
        ):
            conn.execute(
                "INSERT INTO search_chunks (chunk_id, idempotency_key, manifest_id,"
                "evidence_node_id, chunk_key, chunk_revision, text, content_sha256,"
                "char_start, char_end, chunker_config_sha256, chunker_code_version,"
                "available_at, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    chunk_id,
                    f"idem-{chunk_id}",
                    _MANIFEST_ID,
                    node_id,
                    f"key-{chunk_id}",
                    1,
                    text,
                    _sha256_text(text),
                    0,
                    len(text),
                    "2" * 64,
                    "fixture-chunker.v1",
                    _OBSERVED_AT,
                    _RECORDED_AT,
                ),
            )
        conn.execute(
            "INSERT INTO canonical_fact_projection_generations (generation_id,"
            "idempotency_key, generation_kind, delta_depth, resolution_snapshot_id,"
            "resolution_snapshot_sha256, resolution_watermark_sha256, ontology_snapshot_id,"
            "ontology_snapshot_sha256, cutoff_at, config_json, config_sha256,"
            "digest_bucket_count, max_batch_facts, max_batch_bytes, max_batch_milliseconds,"
            "generation_json, generation_sha256, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                _GENERATION_ID,
                "idem-generation-acme-1",
                "checkpoint",
                0,
                "resolution-snapshot-acme-1",
                "3" * 64,
                "4" * 64,
                "ontology-snapshot-acme-1",
                "5" * 64,
                _RECORDED_AT,
                "{}",
                "6" * 64,
                4096,
                100,
                1048576,
                1000,
                "{}",
                "7" * 64,
                _RECORDED_AT,
            ),
        )
        conn.execute(
            "INSERT INTO canonical_fact_projection_entries (generation_id, entry_ordinal,"
            "change_kind, digest_bucket, canonical_metric_cell_id,"
            "canonical_resolution_revision_id, selected_observation_id, source_fact_cell_id,"
            "source_publication_id, source_publication_seal_id, source_publication_member_id,"
            "source_publication_member_sha256, source_record_commitment_sha256,"
            "binding_revision_id, binding_commitment_sha256, mapping_revision_id,"
            "mapping_commitment_sha256, metric_definition_revision_id,"
            "metric_definition_commitment_sha256, canonical_metric_name, period_kind,"
            "period_start, period_end, unit_key, currency, value_kind, canonical_value,"
            "evidence_document_version_id, evidence_node_id, evidence_locator_json,"
            "evidence_locator_sha256, canonical_search_text, entry_json, entry_sha256)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                _GENERATION_ID,
                0,
                "upsert",
                1,
                _METRIC_CELL_ID,
                "resolution-revision-acme-1",
                "fact-observation-acme-1",
                "fact-cell-acme-1",
                "publication-acme-1",
                "publication-seal-acme-1",
                "publication-member-acme-1",
                "8" * 64,
                "9" * 64,
                "binding-revision-acme-1",
                "a" * 64,
                "mapping-revision-acme-1",
                "b" * 64,
                "definition-revision-acme-1",
                "c" * 64,
                _METRIC_NAME,
                "FY",
                _PERIOD_START,
                _PERIOD_END,
                _METRIC_UNIT,
                _METRIC_CURRENCY,
                "numeric",
                _METRIC_VALUE,
                _DOCUMENT_VERSION_ID,
                _FACT_NODE_ID,
                json.dumps(_FACT_LOCATOR, sort_keys=True),
                _sha256_text(json.dumps(_FACT_LOCATOR, sort_keys=True)),
                f"{_METRIC_NAME} {_METRIC_VALUE}",
                "{}",
                "d" * 64,
            ),
        )
        _seed_persisted_claim_mapping(conn)
        conn.commit()
    finally:
        conn.close()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _seed_persisted_claim_mapping(conn: sqlite3.Connection) -> None:
    """Persist the fixture's claim-to-citation mapping in the real audit tables.

    The rows use the sealed lane's canonical JSON + digest helpers so the
    persisted citation payload has the exact shape the audit store writes.
    """
    from ask.audit_store import canonical_json, digest_text

    citation_payload = {
        "n": _CITATION_NUMBER,
        "kind": "fact",
        "label": f"{_TICKER} · {_METRIC_NAME} · {_PERIOD_END}",
        "href": _sealed_source_version_href(_DOCUMENT_VERSION_ID, _FACT_NODE_ID),
        "source_url": _SOURCE_URL,
        "confidence": None,
        "ticker": _TICKER,
        "doc_type": _FORM_TYPE,
        "period": _PERIOD_END,
        "value": _CITATION_VALUE,
        "trace_id": "trace-fixture-1",
        "result_ordinal": 0,
        "candidate_id": _METRIC_CELL_ID,
        "source_commitment_sha256": "d" * 64,
        "research_snapshot_id": "snapshot-fixture-1",
        "as_of_at": "2026-01-15T14:05:00+00:00",
        "document_version_id": _DOCUMENT_VERSION_ID,
        "evidence_node_id": _FACT_NODE_ID,
        "node_kind": "fact",
        "locator": _FACT_LOCATOR,
    }
    citation_json = canonical_json(citation_payload)
    conn.execute(
        "INSERT INTO ask_answer_audit_citations (answer_id, citation_number, trace_id,"
        "result_ordinal, candidate_kind, candidate_id, source_commitment_sha256,"
        "citation_json, citation_sha256, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            _ANSWER_ID,
            _CITATION_NUMBER,
            "trace-fixture-1",
            0,
            "fact",
            _METRIC_CELL_ID,
            "d" * 64,
            citation_json,
            digest_text(citation_json),
            _RECORDED_AT,
        ),
    )
    claim_json = canonical_json(
        {
            "char_end": len(_CLAIM_TEXT),
            "char_start": 0,
            "claim_ordinal": _CLAIM_ORDINAL,
            "claim_sha256": digest_text(_CLAIM_TEXT),
            "supported": True,
        }
    )
    conn.execute(
        "INSERT INTO ask_answer_audit_claims (answer_id, claim_ordinal, char_start,"
        "char_end, claim_text, claim_sha256, supported, claim_json, claim_json_sha256,"
        "recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            _ANSWER_ID,
            _CLAIM_ORDINAL,
            0,
            len(_CLAIM_TEXT),
            _CLAIM_TEXT,
            digest_text(_CLAIM_TEXT),
            1,
            claim_json,
            digest_text(claim_json),
            _RECORDED_AT,
        ),
    )
    conn.execute(
        "INSERT INTO ask_answer_audit_claim_citations (answer_id, claim_ordinal,"
        "citation_number, recorded_at) VALUES (?,?,?,?)",
        (_ANSWER_ID, _CLAIM_ORDINAL, _CITATION_NUMBER, _RECORDED_AT),
    )


@pytest.fixture
def fixture_db(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    db_path = tmp_path / "data" / "portfolio.db"
    migrated_db(db_path)
    _seed_fixture(db_path)
    return db_path


@pytest.fixture
def client(fixture_db: Path) -> FlaskClient:
    return comments_server.create_app(fixture_db.parents[1]).test_client()


# --- rendered-field parsing -----------------------------------------------------------


_ATTR_RE = re.compile(r'data-([a-z0-9-]+)="([^"]*)"')


def _attrs(segment: str) -> dict[str, str]:
    found = _ATTR_RE.findall(segment)
    return {str(name): unescape(str(value)) for name, value in found}


@dataclass(slots=True)
class _RenderedProvenance:
    root: dict[str, str]
    rows: list[dict[str, str]]
    node_rows: list[dict[str, str]]
    chunk_rows: list[dict[str, str]]
    fact_rows: list[dict[str, str]]


def _parse_provenance(html: str) -> _RenderedProvenance:
    _, marker, remainder = html.partition('<div class="sv-fallback"')
    assert marker, "provenance root is missing from the rendered page"
    root_tag = remainder.partition(">")[0]
    segments = [
        segment.partition(">")[0] for segment in remainder.split('<div class="sv-sec-row"')[1:]
    ]
    rows: list[dict[str, str]] = [_attrs(segment) for segment in segments]
    return _RenderedProvenance(
        root=_attrs(root_tag),
        rows=rows,
        node_rows=[row for row in rows if "node-id" in row],
        chunk_rows=[row for row in rows if "chunk-id" in row],
        fact_rows=[row for row in rows if "fact-metric" in row],
    )


@dataclass(slots=True)
class _PersistedClaim:
    claim_ordinal: int
    citation_number: int
    claim_text: str
    citation: dict[str, object]


def _load_persisted_mapping(db_path: Path) -> list[_PersistedClaim]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT edge.claim_ordinal, edge.citation_number, claim.claim_text,"
            "citation.citation_json "
            "FROM ask_answer_audit_claim_citations edge "
            "JOIN ask_answer_audit_claims claim ON claim.answer_id=edge.answer_id "
            "AND claim.claim_ordinal=edge.claim_ordinal "
            "JOIN ask_answer_audit_citations citation ON citation.answer_id=edge.answer_id "
            "AND citation.citation_number=edge.citation_number "
            "WHERE edge.answer_id=? ORDER BY edge.claim_ordinal, edge.citation_number",
            (_ANSWER_ID,),
        ).fetchall()
    finally:
        conn.close()
    return [
        _PersistedClaim(
            claim_ordinal=int(row["claim_ordinal"]),
            citation_number=int(row["citation_number"]),
            claim_text=str(row["claim_text"]),
            citation=cast("dict[str, object]", json.loads(str(row["citation_json"]))),
        )
        for row in rows
    ]


# --- tests ----------------------------------------------------------------------------


def test_sealed_href_template_matches_the_route_contract() -> None:
    """The fixture hrefs are the sealed lane's emitted shape, not a paraphrase."""
    source = SEALED_RETRIEVAL_SOURCE.read_text(encoding="utf-8")
    assert source.count(_SEALED_HREF_TEMPLATE) == 2, (
        "sealed_retrieval.py no longer emits the /source/version/<id>?node=<node> "
        "href template exactly twice; the route contract must be re-checked"
    )


def test_narrative_citation_resolves_to_the_full_provenance_chain(
    client: FlaskClient,
) -> None:
    href = _sealed_source_version_href(_DOCUMENT_VERSION_ID, _NARRATIVE_NODE_ID)
    response = client.get(href)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    parsed = _parse_provenance(html)
    root = parsed.root
    assert root["issuer-id"] == _ISSUER_ID
    assert root["ticker"] == _TICKER
    assert root["document-version-id"] == _DOCUMENT_VERSION_ID
    assert root["version-sequence"] == "1"
    assert root["form-type"] == _FORM_TYPE
    assert root["document-type"] == _DOCUMENT_TYPE
    assert root["period-start"] == _PERIOD_START
    assert root["period-end"] == _PERIOD_END
    assert root["observation-id"] == _OBSERVATION_ID
    assert root["blob-sha256"] == _BLOB_SHA256
    assert root["observed-at"] == _OBSERVED_AT
    assert root["retrieved-at"] == _RETRIEVED_AT
    assert root["recorded-at"] == _RECORDED_AT

    node_rows = parsed.node_rows
    assert len(node_rows) == 1
    node_row = node_rows[0]
    assert node_row["node-id"] == _NARRATIVE_NODE_ID
    assert node_row["node-kind"] == _NARRATIVE_NODE_KIND
    assert json.loads(node_row["node-locator"]) == _NARRATIVE_LOCATOR
    chunk_rows = parsed.chunk_rows
    assert len(chunk_rows) == 1
    assert chunk_rows[0]["chunk-id"] == _NARRATIVE_CHUNK_ID
    assert chunk_rows[0]["char-start"] == "0"
    assert chunk_rows[0]["char-end"] == str(len(_NARRATIVE_NODE_TEXT))
    assert _NARRATIVE_NODE_TEXT in html

    _record_artifact(
        "source-version-val-search-002-reconciliation.json",
        {
            "assertion": "VAL-SEARCH-002",
            "feature": "source-version-routes",
            "scope_note": _SCOPE_NOTE,
            "fixture_corpus": _fixture_corpus_definition(),
            "citations": [
                {
                    "href": href,
                    "status": response.status_code,
                    "kind": "narrative",
                    "rendered": {
                        "issuer_id": root["issuer-id"],
                        "ticker": root["ticker"],
                        "document_version_id": root["document-version-id"],
                        "form_type": root["form-type"],
                        "period_start": root["period-start"],
                        "period_end": root["period-end"],
                        "observation_id": root["observation-id"],
                        "blob_sha256": root["blob-sha256"],
                        "node_id": node_row["node-id"],
                        "node_kind": node_row["node-kind"],
                        "node_locator": json.loads(node_row["node-locator"]),
                        "chunk_id": chunk_rows[0]["chunk-id"],
                        "char_start": int(chunk_rows[0]["char-start"]),
                        "char_end": int(chunk_rows[0]["char-end"]),
                    },
                    "expected": {
                        "issuer_id": _ISSUER_ID,
                        "ticker": _TICKER,
                        "document_version_id": _DOCUMENT_VERSION_ID,
                        "form_type": _FORM_TYPE,
                        "period_start": _PERIOD_START,
                        "period_end": _PERIOD_END,
                        "observation_id": _OBSERVATION_ID,
                        "blob_sha256": _BLOB_SHA256,
                        "node_id": _NARRATIVE_NODE_ID,
                        "node_kind": _NARRATIVE_NODE_KIND,
                        "node_locator": _NARRATIVE_LOCATOR,
                        "chunk_id": _NARRATIVE_CHUNK_ID,
                        "char_start": 0,
                        "char_end": len(_NARRATIVE_NODE_TEXT),
                    },
                    "matched": True,
                }
            ],
        },
    )


def test_cited_fact_claim_reconciles_to_the_immutable_observation(
    client: FlaskClient,
    fixture_db: Path,
) -> None:
    mapping = _load_persisted_mapping(fixture_db)
    assert len(mapping) == 1
    entry = mapping[0]
    citation = entry.citation
    assert citation["document_version_id"] == _DOCUMENT_VERSION_ID
    assert citation["evidence_node_id"] == _FACT_NODE_ID

    href = str(citation["href"])
    response = client.get(href)
    assert response.status_code == 200
    parsed = _parse_provenance(response.get_data(as_text=True))
    root = parsed.root
    fact_rows = parsed.fact_rows
    assert len(fact_rows) == 1
    fact_row = fact_rows[0]

    # The rendered chain agrees with the fixture's immutable observation version.
    assert root["issuer-id"] == _ISSUER_ID
    assert root["ticker"] == _TICKER
    assert root["document-version-id"] == _DOCUMENT_VERSION_ID
    assert root["observation-id"] == _OBSERVATION_ID
    assert root["period-end"] == _PERIOD_END
    assert fact_row["fact-node-id"] == _FACT_NODE_ID
    assert fact_row["fact-metric"] == _METRIC_NAME
    assert fact_row["fact-unit"] == _METRIC_UNIT
    assert fact_row["fact-currency"] == _METRIC_CURRENCY
    assert fact_row["fact-period-end"] == _PERIOD_END
    assert json.loads(fact_row["fact-locator"]) == _FACT_LOCATOR

    # The stored reported value equals the cited value after only the documented
    # display normalization (the citation appends the currency token).
    assert fact_row["fact-value"] == _METRIC_VALUE
    assert citation["value"] == _CITATION_VALUE
    assert f"{fact_row['fact-value']} {fact_row['fact-currency']}" == _CITATION_VALUE
    assert _METRIC_VALUE in entry.claim_text

    _record_artifact(
        "source-version-val-search-003-reconciliation.json",
        {
            "assertion": "VAL-SEARCH-003",
            "feature": "source-version-routes",
            "scope_note": _SCOPE_NOTE,
            "display_normalization": _DISPLAY_NORMALIZATION,
            "claims": [
                {
                    "claim_ordinal": entry.claim_ordinal,
                    "claim_text": entry.claim_text,
                    "citation_number": entry.citation_number,
                    "citation_href": href,
                    "status": response.status_code,
                    "rendered": {
                        "issuer_id": root["issuer-id"],
                        "ticker": root["ticker"],
                        "document_version_id": root["document-version-id"],
                        "observation_id": root["observation-id"],
                        "period_end": root["period-end"],
                        "metric": fact_row["fact-metric"],
                        "value": fact_row["fact-value"],
                        "unit": fact_row["fact-unit"],
                        "currency": fact_row["fact-currency"],
                        "node_id": fact_row["fact-node-id"],
                        "locator": json.loads(fact_row["fact-locator"]),
                    },
                    "expected_observation": {
                        "issuer_id": _ISSUER_ID,
                        "ticker": _TICKER,
                        "document_version_id": _DOCUMENT_VERSION_ID,
                        "observation_id": _OBSERVATION_ID,
                        "period_end": _PERIOD_END,
                        "metric": _METRIC_NAME,
                        "value": _METRIC_VALUE,
                        "unit": _METRIC_UNIT,
                        "currency": _METRIC_CURRENCY,
                        "node_id": _FACT_NODE_ID,
                        "locator": _FACT_LOCATOR,
                    },
                    "matched": True,
                }
            ],
        },
    )


def test_unknown_document_version_returns_an_honest_404(client: FlaskClient) -> None:
    response = client.get("/source/version/dv-does-not-exist")
    assert response.status_code == 404


def test_unknown_node_returns_an_honest_404(client: FlaskClient) -> None:
    href = _sealed_source_version_href(_DOCUMENT_VERSION_ID, "node-does-not-exist")
    assert client.get(href).status_code == 404


def test_node_from_another_document_version_returns_an_honest_404(
    client: FlaskClient,
) -> None:
    href = _sealed_source_version_href(_DOCUMENT_VERSION_ID, _OTHER_NODE_ID)
    assert client.get(href).status_code == 404
    # ...and the same node resolves under its own document version.
    own = _sealed_source_version_href(_OTHER_DOCUMENT_VERSION_ID, _OTHER_NODE_ID)
    assert client.get(own).status_code == 200


def test_fixture_corpus_and_inventory_artifacts(client: FlaskClient) -> None:
    """Record the fixture definition and the zero-live-input inventory note."""
    assert (
        client.get(_sealed_source_version_href(_DOCUMENT_VERSION_ID, _FACT_NODE_ID)).status_code
        == 200
    )
    _record_artifact(
        "source-version-val-search-002-fixture-corpus.json",
        {
            "assertion": "VAL-SEARCH-002/003",
            "feature": "source-version-routes",
            "scope_note": _SCOPE_NOTE,
            "fixture_corpus": _fixture_corpus_definition(),
        },
    )
    _record_artifact(
        "source-version-test-inventory.json",
        {
            "assertion": "VAL-SEARCH-002/003",
            "feature": "source-version-routes",
            "scope_note": _SCOPE_NOTE,
            "test_file": "tests/test_source_version_routes.py",
            "live_sealed_turns": 0,
            "real_claude_calls": 0,
            "sec_or_source_requests": 0,
            "portfolio_data_inputs": 0,
            "bench_database_accesses": 0,
            "database_engine": "hermetic pytest migrated_db copy (triggers dropped)",
            "note": (
                "Citation hrefs and the claim-to-citation mapping are fixture-defined "
                "and synthetic; no live sealed answer or portfolio citation is used."
            ),
        },
    )
