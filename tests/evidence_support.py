"""Seed rows that the active schema requires before facts or documents bind."""

from __future__ import annotations

import sqlite3

DEFAULT_STAMP = "2026-02-01T12:00:00+00:00"


def seed_issuer(
    conn: sqlite3.Connection,
    issuer_id: str,
    *,
    entity_kind: str = "operating_company",
    recorded_at: str = DEFAULT_STAMP,
) -> None:
    """Register ``issuer_id`` in the issuer registry."""
    conn.execute(
        "INSERT OR IGNORE INTO issuer_entities "
        "(issuer_id, idempotency_key, entity_kind, created_at) VALUES (?, ?, ?, ?)",
        (issuer_id, issuer_id, entity_kind, recorded_at),
    )


def seed_evidence_document(
    conn: sqlite3.Connection,
    *,
    doc_id: int,
    ticker: str,
    issuer_id: str,
    period_end: str,
    file_path: str,
    sha_char: str,
    recorded_at: str = DEFAULT_STAMP,
) -> None:
    """Insert a legacy document with the evidence chain that fact writes require."""
    sha = sha_char * 64
    url = f"https://example.invalid/{ticker.lower()}/{doc_id}"
    conn.execute(
        "INSERT INTO documents "
        "(id, ticker, source_type, doc_type, period_end, file_path, sha256, fetched_at, "
        "fetch_status, raw_bytes_size, source_quality_tier) VALUES "
        "(?, ?, 'ir_doc', 'ir_press_release', ?, ?, ?, ?, 'ok', 100, 'fmp_normalized')",
        (doc_id, ticker, period_end, file_path, sha, recorded_at),
    )
    conn.execute(
        "INSERT INTO evidence_content_blobs (sha256, byte_size, media_type, storage_uri, recorded_at) "
        "VALUES (?, 100, 'text/html', ?, ?)",
        (sha, url, recorded_at),
    )
    obs_id = f"source-{doc_id}"
    ver_id = f"docver-{doc_id}"
    run_id = f"run-{doc_id}"
    conn.execute(
        "INSERT INTO evidence_source_observations "
        "(observation_id, idempotency_key, source_kind, source_url, blob_sha256, observed_at, "
        "retrieved_at, retrieval_config_sha256, collector_code_version) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            obs_id,
            f"source:{doc_id}",
            "ir_document",
            url,
            sha,
            recorded_at,
            recorded_at,
            "d" * 64,
            "test-v1",
        ),
    )
    conn.execute(
        "INSERT INTO evidence_document_versions "
        "(document_version_id, document_key, version_sequence, observation_id, blob_sha256, "
        "issuer_id, ticker, document_type, form_type, period_end, language, legacy_document_id, "
        "recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            ver_id,
            f"document:{doc_id}",
            1,
            obs_id,
            sha,
            issuer_id,
            ticker,
            "ir_press_release",
            "ir_press_release",
            period_end,
            "en",
            doc_id,
            recorded_at,
        ),
    )
    conn.execute(
        "INSERT INTO evidence_extraction_runs "
        "(extraction_run_id, idempotency_key, document_version_id, input_sha256, "
        "extractor_name, extractor_config_sha256, extractor_code_version, output_sha256, "
        "started_at, completed_at, outcome) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            f"run:{doc_id}",
            ver_id,
            sha,
            "test-extractor",
            "e" * 64,
            "test-v1",
            "f" * 64,
            recorded_at,
            recorded_at,
            "succeeded",
        ),
    )
    conn.execute(
        "INSERT INTO evidence_nodes "
        "(node_id, evidence_key, revision, extraction_run_id, parent_node_id, supersedes_node_id, "
        "node_kind, text, locator_json, locator_sha256, recorded_at) "
        "VALUES (?, ?, 1, ?, NULL, NULL, 'document', 'Earnings release.', NULL, NULL, ?)",
        (f"root-{doc_id}", f"root:{doc_id}", run_id, recorded_at),
    )


def seed_source_obligation(
    conn: sqlite3.Connection,
    issuer_id: str,
    *,
    document_family: str = "operating_company_periodic",
    authority_kind: str = "issuer_publisher",
    completeness_rule: str = "publisher_surface_exhaustion",
    recorded_at: str = DEFAULT_STAMP,
) -> None:
    """Record one active, required source obligation for ``issuer_id``."""
    key = f"{issuer_id}:{authority_kind}:{document_family}"
    conn.execute(
        "INSERT INTO source_obligation_revisions "
        "(obligation_revision_id, idempotency_key, obligation_key, revision, issuer_id, "
        "reporting_entity_id, authority_kind, document_family, obligation_state, "
        "completeness_rule, active_from, active_to, decision_kind, reason_code, "
        "reason_details_json, effective_at, knowledge_at, recorded_at, "
        "supersedes_obligation_revision_id) "
        "VALUES (?, ?, ?, 1, ?, NULL, ?, ?, 'required', ?, ?, NULL, 'deterministic', 'test', "
        "'{}', ?, ?, ?, NULL)",
        (
            f"{key}:v1",
            f"{key}:v1",
            key,
            issuer_id,
            authority_kind,
            document_family,
            completeness_rule,
            recorded_at,
            recorded_at,
            recorded_at,
            recorded_at,
        ),
    )
