"""Retained SEC submissions prove current issuer identity without a ticker feed."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from provenance.inventory_identity import resolve_sec_inventory_subject
from provenance.issuer_registry_bootstrap import (
    SecCompanyTickerContractError,
    SecSubmissionsIssuerRequest,
    bootstrap_sec_submissions_issuer,
)
from provenance.reporting_entity_registry import (
    EvidenceSubjectBindingRevision,
    ReportingEntityRegistry,
)

CIK = "0001691493"
URL = f"https://data.sec.gov/submissions/CIK{CIK}.json"
OBSERVED = datetime(2026, 10, 18, 4, 39, 34, tzinfo=UTC)
RETRIEVED = OBSERVED + timedelta(seconds=1)
RECORDED = datetime(2026, 10, 19, 12, tzinfo=UTC)


def _body(**updates: object) -> bytes:
    values: dict[str, object] = {
        "cik": 1691493,
        "name": "Nu Holdings Ltd.",
        "entityType": "operating",
        "tickers": ["NU"],
        "exchanges": ["NYSE"],
    }
    values.update(updates)
    return json.dumps(values, separators=(",", ":")).encode()


def _request(
    tmp_path: Path, *, apply: bool = False, **updates: object
) -> SecSubmissionsIssuerRequest:
    values: dict[str, object] = {
        "ticker": "NU",
        "normalized_cik": CIK,
        "source_url": URL,
        "raw_body": _body(),
        "blob_root": tmp_path / "blobs",
        "observed_at": OBSERVED,
        "retrieved_at": RETRIEVED,
        "recorded_at": RECORDED,
        "apply": apply,
    }
    values.update(updates)
    return SecSubmissionsIssuerRequest.model_validate(values)


def _database(tmp_path: Path, migrated_db: Callable[..., Path]) -> sqlite3.Connection:
    path = tmp_path / "submissions-identity.db"
    migrated_db(path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def test_submissions_dry_run_does_not_write_bytes_or_records(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _database(tmp_path, migrated_db)
    conn.execute("PRAGMA query_only=ON")
    result = bootstrap_sec_submissions_issuer(conn, request=_request(tmp_path))
    assert result.mode == "dry_run"
    assert result.legal_name == "Nu Holdings Ltd."
    assert result.security_id is None
    assert result.security_status == "unavailable_from_submissions"
    assert result.records_created == 0
    assert conn.execute("SELECT COUNT(*) FROM issuer_entities").fetchone() == (0,)
    assert not (tmp_path / "blobs").exists()
    conn.close()


def test_retained_submissions_apply_preserves_clocks_bytes_and_replays(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _database(tmp_path, migrated_db)
    first = bootstrap_sec_submissions_issuer(conn, request=_request(tmp_path, apply=True))
    replay = bootstrap_sec_submissions_issuer(
        conn,
        request=_request(tmp_path, apply=True, recorded_at=RECORDED + timedelta(hours=1)),
    )
    assert first.records_created > 0
    assert replay.records_created == 0
    assert replay.source_observation_id == first.source_observation_id
    source = conn.execute(
        "SELECT source_kind, source_url, observed_at, retrieved_at FROM evidence_source_observations"
    ).fetchone()
    assert source is not None and source[:2] == ("sec_submissions", URL)
    assert datetime.fromisoformat(source[2]) == OBSERVED
    assert datetime.fromisoformat(source[3]) == RETRIEVED
    binding = conn.execute(
        "SELECT issuer_id,reporting_entity_id,security_id,recorded_at "
        "FROM v_recorded_subject_bindings_current WHERE recorded_issuer_id='legacy-ticker:NU'"
    ).fetchone()
    assert binding is not None
    assert binding[:3] == (first.canonical_issuer_id, f"reporting:sec:{CIK}", None)
    assert datetime.fromisoformat(binding[3]) == RECORDED
    assert conn.execute("SELECT COUNT(*) FROM securities").fetchone() == (0,)
    assert conn.execute("SELECT COUNT(*) FROM security_listing_assertions").fetchone() == (0,)
    assert conn.execute("SELECT COUNT(*) FROM source_obligation_revisions").fetchone() == (3,)
    assert conn.execute("SELECT COUNT(*) FROM issuer_reporting_scope_revisions").fetchone() == (0,)
    digest = hashlib.sha256(_body()).hexdigest()
    assert (tmp_path / "blobs" / digest[:2] / digest).read_bytes() == _body()
    subject = resolve_sec_inventory_subject(conn, ticker="NU", cik=CIK, knowledge_at=RECORDED)
    assert subject.issuer_id == first.canonical_issuer_id
    registry = ReportingEntityRegistry(conn)
    for recorded_key in (first.canonical_issuer_id, "legacy-ticker:NU"):
        canonical = registry.canonicalize_recorded_subject(recorded_key, knowledge_at=RECORDED)
        assert canonical.issuer_id == first.canonical_issuer_id
        assert canonical.reporting_entity_id == first.reporting_entity_id
        assert canonical.security_id is None
        assert not canonical.material_dissent
    conn.close()


@pytest.mark.parametrize("material_dissent", [False, True])
def test_existing_canonical_subject_conflict_is_rejected_before_capture(
    tmp_path: Path, migrated_db: Callable[..., Path], material_dissent: bool
) -> None:
    conn = _database(tmp_path, migrated_db)
    first = bootstrap_sec_submissions_issuer(conn, request=_request(tmp_path, apply=True))
    previous = conn.execute(
        "SELECT binding_revision_id,revision FROM recorded_subject_binding_revisions "
        "WHERE recorded_issuer_id=? ORDER BY revision DESC LIMIT 1",
        (first.canonical_issuer_id,),
    ).fetchone()
    revision = 1 if previous is None else previous[1] + 1
    registry = ReportingEntityRegistry(conn)
    registry.persist(
        EvidenceSubjectBindingRevision(
            binding_revision_id="conflicting-canonical-subject",
            idempotency_key="conflicting-canonical-subject",
            recorded_issuer_id=first.canonical_issuer_id,
            revision=revision,
            issuer_id=first.canonical_issuer_id if material_dissent else None,
            reporting_entity_id=first.reporting_entity_id if material_dissent else None,
            outcome="selected" if material_dissent else "unresolved",
            decision_kind="deterministic",
            reason_code="synthetic_conflict",
            reason_details=(("fixture", "canonical subject conflict"),),
            material_dissent=material_dissent,
            effective_at=RECORDED + timedelta(hours=1),
            knowledge_at=RECORDED + timedelta(hours=1),
            recorded_at=RECORDED + timedelta(hours=1),
            supersedes_binding_revision_id=None if previous is None else previous[0],
        )
    )
    conn.commit()
    before = conn.total_changes
    with pytest.raises(SecCompanyTickerContractError, match="existing reporting subject"):
        bootstrap_sec_submissions_issuer(
            conn,
            request=_request(tmp_path, apply=True, recorded_at=RECORDED + timedelta(hours=2)),
        )
    assert conn.total_changes == before
    conn.close()


@pytest.mark.parametrize(
    "updates",
    [
        {"cik": True},
        {"cik": 1099590},
        {"entityType": "investment"},
        {"tickers": ["OTHER"]},
        {"tickers": ["NU", "NU"]},
        {"name": ""},
    ],
)
def test_source_identity_mismatch_fails_before_any_write(
    tmp_path: Path, migrated_db: Callable[..., Path], updates: dict[str, object]
) -> None:
    conn = _database(tmp_path, migrated_db)
    with pytest.raises(SecCompanyTickerContractError):
        bootstrap_sec_submissions_issuer(
            conn, request=_request(tmp_path, apply=True, raw_body=_body(**updates))
        )
    assert conn.execute("SELECT COUNT(*) FROM issuer_entities").fetchone() == (0,)
    assert not (tmp_path / "blobs").exists()
    conn.close()


def test_duplicate_keys_source_url_and_capture_clock_mismatches_are_rejected(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _database(tmp_path, migrated_db)
    duplicate = _body().replace(b'"cik":1691493', b'"cik":1691493,"cik":1691493')
    for raw in (duplicate,):
        with pytest.raises(SecCompanyTickerContractError):
            bootstrap_sec_submissions_issuer(
                conn, request=_request(tmp_path, apply=True, raw_body=raw)
            )
    with pytest.raises(ValueError):
        _request(tmp_path, source_url="https://www.sec.gov/files/company_tickers.json")
    with pytest.raises(ValueError):
        _request(tmp_path, recorded_at=OBSERVED)
    conn.close()


def test_existing_ticker_binding_cannot_be_silently_reassigned(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _database(tmp_path, migrated_db)
    original = bootstrap_sec_submissions_issuer(
        conn,
        request=_request(
            tmp_path,
            apply=True,
            normalized_cik="0000000123",
            source_url="https://data.sec.gov/submissions/CIK0000000123.json",
            raw_body=_body(cik=123),
        ),
    )
    before = conn.total_changes
    with pytest.raises(SecCompanyTickerContractError, match="existing ticker identity"):
        bootstrap_sec_submissions_issuer(conn, request=_request(tmp_path, apply=True))
    assert conn.total_changes == before
    assert conn.execute("SELECT issuer_id FROM issuer_entities").fetchone() == (
        original.canonical_issuer_id,
    )
    conn.close()


def test_replay_rejects_changed_original_capture_witness(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _database(tmp_path, migrated_db)
    bootstrap_sec_submissions_issuer(conn, request=_request(tmp_path, apply=True))
    with pytest.raises(ValueError, match="capture clocks conflict"):
        bootstrap_sec_submissions_issuer(
            conn,
            request=_request(tmp_path, apply=True, observed_at=OBSERVED - timedelta(seconds=1)),
        )
    conn.close()


def test_nu_other_sec_registrant_uses_20f_reporting_identity(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _database(tmp_path, migrated_db)
    raw = _body(
        entityType="other",
        sic="6199",
        ownerOrg="02 Finance",
        filings={"recent": {"form": ["6-K", "20-F"]}},
    )
    result = bootstrap_sec_submissions_issuer(
        conn, request=_request(tmp_path, apply=True, raw_body=raw)
    )
    assert result.sec_entity_type == "other"
    assert result.legal_name == "Nu Holdings Ltd."
    assert conn.execute("SELECT reporting_entity_kind FROM reporting_entities").fetchone() == (
        "legal_registrant",
    )
    assert (
        resolve_sec_inventory_subject(conn, ticker="NU", cik=CIK, knowledge_at=RECORDED).issuer_id
        == result.canonical_issuer_id
    )
    conn.close()


def test_other_registrant_without_reporting_proof_or_with_fund_series_is_rejected(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _database(tmp_path, migrated_db)
    for updates in (
        {"entityType": "other"},
        {"seriesAndClassesInformation": [{"seriesId": "S000000001"}]},
    ):
        with pytest.raises(SecCompanyTickerContractError):
            bootstrap_sec_submissions_issuer(
                conn, request=_request(tmp_path, apply=True, raw_body=_body(**updates))
            )
    assert conn.execute("SELECT COUNT(*) FROM issuer_entities").fetchone() == (0,)
    conn.close()


@pytest.mark.parametrize("invalid", [None, "", "0132", "1301", "01-31", 1231, "0000"])
def test_explicit_invalid_fiscal_year_end_fails_before_capture(
    tmp_path: Path, migrated_db: Callable[..., Path], invalid: object
) -> None:
    conn = _database(tmp_path, migrated_db)
    before = conn.total_changes
    with pytest.raises(SecCompanyTickerContractError, match="fiscalYearEnd"):
        bootstrap_sec_submissions_issuer(
            conn, request=_request(tmp_path, apply=True, raw_body=_body(fiscalYearEnd=invalid))
        )
    assert conn.total_changes == before
    assert not (tmp_path / "blobs").exists()
    conn.close()


def test_source_backed_fiscal_profile_enrichment_preserves_history_and_replays(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _database(tmp_path, migrated_db)
    bootstrap_sec_submissions_issuer(conn, request=_request(tmp_path, apply=True))
    admitted = RECORDED + timedelta(days=2)
    request = _request(
        tmp_path, apply=True, recorded_at=admitted, raw_body=_body(fiscalYearEnd="0131")
    )
    result = bootstrap_sec_submissions_issuer(conn, request=request)
    rows = conn.execute(
        "SELECT fiscal_year_end,knowledge_at,recorded_at,reason_details_json "
        "FROM issuer_profile_revisions ORDER BY revision"
    ).fetchall()
    assert len(rows) == 2 and rows[0][0] is None
    assert rows[1][0] == "01-31"
    assert datetime.fromisoformat(rows[1][1]) == admitted
    assert datetime.fromisoformat(rows[1][2]) == admitted
    assert "/fiscalYearEnd" in rows[1][3]
    assert result.source_sha256 in rows[1][3]
    assert (
        bootstrap_sec_submissions_issuer(
            conn, request=request.model_copy(update={"recorded_at": admitted + timedelta(days=1)})
        ).records_created
        == 0
    )
    assert conn.execute("SELECT COUNT(*) FROM issuer_profile_revisions").fetchone() == (2,)
    conn.close()


def test_fiscal_inventory_witness_uses_existing_sec_profile_and_document_owners(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    from datetime import date

    from pipeline.issuer_document_inventory import (
        ExpectedIssuerDocument,
        FiscalPeriodWitness,
        IssuerDocumentInventoryError,
        IssuerDocumentInventoryRequest,
        validate_fiscal_period_witness,
    )
    from provenance.evidence_backfill import ensure_legacy_document_evidence

    conn = _database(tmp_path, migrated_db)
    result = bootstrap_sec_submissions_issuer(
        conn, request=_request(tmp_path, apply=True, raw_body=_body(fiscalYearEnd="0131"))
    )
    raw = b"Synthetic reported Q2 FY2027 release"
    relative = "ir_documents/NU/release.pdf"
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    conn.execute(
        "INSERT INTO documents (id,ticker,source_type,doc_type,period_end,file_path,sha256,"
        "fetched_at,fetch_status,raw_bytes_size,source_url) VALUES (1,?,?,?,?,?,?,?,?,?,?)",
        (
            "NU",
            "ir_doc",
            "ir_press_release",
            "2026-07-31",
            relative,
            digest,
            RECORDED.isoformat(),
            "ok",
            len(raw),
            "https://issuer.example/release.pdf",
        ),
    )
    conn.row_factory = sqlite3.Row
    ensure_legacy_document_evidence(conn, repo_root=tmp_path, document_id=1)
    profile = conn.execute("SELECT profile_revision_id FROM issuer_profile_revisions").fetchone()
    version = conn.execute("SELECT document_version_id FROM evidence_document_versions").fetchone()
    assert profile is not None and version is not None
    witness = FiscalPeriodWitness(
        issuer_id=result.canonical_issuer_id,
        profile_revision_id=profile[0],
        fiscal_year_end="01-31",
        document_version_id=version[0],
        document_sha256=digest,
        knowledge_cutoff=RECORDED + timedelta(days=1),
        observed_through=RECORDED + timedelta(days=1),
    )
    request = IssuerDocumentInventoryRequest(
        ticker="NU",
        fiscal_year=2027,
        fiscal_quarter=2,
        period_end=date(2026, 7, 31),
        expected_documents=(
            ExpectedIssuerDocument(
                source_url="https://issuer.example/release.pdf", document_type="ir_press_release"
            ),
        ),
        fiscal_period_witness=witness,
    )
    validate_fiscal_period_witness(conn, request=request)
    from pipeline.issuer_document_inventory import (
        ExpectedIssuerDocumentAlias,
        IssuerDocumentAliasBinding,
        IssuerDocumentAliasInventoryRequest,
        build_issuer_document_inventory,
        inspect_issuer_document_alias,
    )

    subject = conn.execute(
        "SELECT binding_revision_id FROM recorded_subject_binding_revisions "
        "WHERE recorded_issuer_id='legacy-ticker:NU' ORDER BY revision DESC LIMIT 1"
    ).fetchone()
    assert subject is not None
    alias = IssuerDocumentAliasBinding(
        document_id=1,
        document_version_id=version[0],
        blob_sha256=digest,
        source_observation_id="legacy-obs-1",
        document_link_id="legacy-primary-link:1",
        recorded_issuer_id="legacy-ticker:NU",
        issuer_id=result.canonical_issuer_id,
        reporting_entity_id=result.reporting_entity_id,
        subject_binding_revision_id=subject[0],
        registered_source_url="https://issuer.example/release.pdf",
        lineage_sha256="0" * 64,
    )
    expected = ExpectedIssuerDocumentAlias(
        source_url="https://issuer.example/release.pdf",
        document_type="ir_press_release",
        alias=alias,
    )
    alias_request = IssuerDocumentAliasInventoryRequest(
        ticker="NU",
        fiscal_year=2027,
        fiscal_quarter=2,
        period_end=date(2026, 7, 31),
        fiscal_period_witness=witness,
        knowledge_cutoff=witness.knowledge_cutoff,
        observed_through=witness.observed_through,
        expected_documents=(expected,),
    )
    lineage = inspect_issuer_document_alias(conn, expected=expected, request=alias_request)
    lineage_sha = hashlib.sha256(
        json.dumps(lineage, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    alias_request = alias_request.model_copy(
        update={
            "expected_documents": (
                expected.model_copy(
                    update={"alias": alias.model_copy(update={"lineage_sha256": lineage_sha})}
                ),
            )
        }
    )
    conn.commit()
    receipt = build_issuer_document_inventory(
        conn,
        database_path=tmp_path / "submissions-identity.db",
        repo_root=tmp_path,
        request=alias_request,
    )
    assert receipt.request.period_end == date(2026, 7, 31)
    assert receipt.request.fiscal_period_witness == witness
    assert receipt.records[0].document_id == 1 and receipt.records[0].sha256 == digest
    for field, bad in [
        ("document_sha256", "0" * 64),
        ("issuer_id", "wrong-issuer"),
        ("fiscal_year_end", "12-31"),
    ]:
        bad_witness = witness.model_copy(update={field: bad})
        # model_copy is deliberately adversarial: the database boundary must reject it.
        with pytest.raises(IssuerDocumentInventoryError):
            validate_fiscal_period_witness(
                conn, request=request.model_copy(update={"fiscal_period_witness": bad_witness})
            )
    with pytest.raises(IssuerDocumentInventoryError, match="after_cutoff"):
        validate_fiscal_period_witness(
            conn,
            request=request.model_copy(
                update={
                    "fiscal_period_witness": witness.model_copy(
                        update={"knowledge_cutoff": OBSERVED - timedelta(days=1)}
                    )
                }
            ),
        )
    bootstrap_sec_submissions_issuer(
        conn,
        request=_request(
            tmp_path,
            apply=True,
            raw_body=_body(fiscalYearEnd="1231"),
            recorded_at=RECORDED + timedelta(hours=1),
        ),
    )
    with pytest.raises(IssuerDocumentInventoryError, match="superseded"):
        validate_fiscal_period_witness(conn, request=request)
    conn.close()


def test_cached_submissions_profile_enrichment_uses_new_admission_clock(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from provenance import issuer_registry_bootstrap as owner
    from provenance.issuer_registry import IssuerRegistry

    conn = _database(tmp_path, migrated_db)
    request = _request(tmp_path, apply=True, raw_body=_body(fiscalYearEnd="0131"))
    original = getattr(owner, "_persist_profile")

    def predecessor(
        conn: sqlite3.Connection,
        registry: IssuerRegistry,
        *,
        issuer_id: str,
        legal_name: str,
        observation_id: str,
        source_sha: str,
        recorded_at: datetime,
        reason_code: str,
        explicit_fiscal_year_end: str | None = None,
        enrichment_recorded_at: datetime | None = None,
    ) -> int:
        return original(
            conn,
            registry,
            issuer_id=issuer_id,
            legal_name=legal_name,
            observation_id=observation_id,
            source_sha=source_sha,
            recorded_at=recorded_at,
            reason_code=reason_code,
        )

    with monkeypatch.context() as scoped:
        scoped.setattr(owner, "_persist_profile", predecessor)
        bootstrap_sec_submissions_issuer(conn, request=request)
    later = RECORDED + timedelta(days=3)
    bootstrap_sec_submissions_issuer(
        conn, request=request.model_copy(update={"recorded_at": later})
    )
    rows = conn.execute(
        "SELECT fiscal_year_end,knowledge_at FROM issuer_profile_revisions ORDER BY revision"
    ).fetchall()
    assert rows[0][0] is None and rows[1][0] == "01-31"
    assert datetime.fromisoformat(rows[1][1]) == later
    assert conn.execute("SELECT COUNT(*) FROM evidence_source_observations").fetchone() == (1,)
    conn.close()
