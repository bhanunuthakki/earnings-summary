"""Captured MELI quarters stay separate from issuer archive coverage."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from execution import sync_ir_source_inventory as cli
from ir_pipeline.home_authority import IRHomeAuthorityRequest, verify_ir_home_authority
from provenance.issuer_registry import IssuerEntity, IssuerRegistry, LegacyIssuerBindingRevision
from provenance.reporting_entity_registry import (
    DocumentFamily,
    ReportingEntityRegistry,
    SourceObligationRevision,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

STAMP = datetime(2026, 8, 5, tzinfo=UTC)
URL = "https://investor.mercadolibre.com/sec-filings"
LABELS = ("Letter to Shareholders", "Earnings Presentation", "SEC Filing", "Webcast Transcript")


def _page() -> bytes:
    items = [
        {
            "id": f"result-q{quarter}",
            "title": f"Results Q{quarter}'26",
            "links": [
                {"label": label, "href": f"https://files.example.test/q{quarter}-{i}.pdf"}
                for i, label in enumerate(LABELS)
            ],
        }
        for quarter in (1, 2)
    ]
    return (
        '<html>Mercado Libre<script id="__NORDIC_RENDERING_CTX__">_n.ctx.r='
        + json.dumps({"quarterlyResults": {"items": items}})
        + ";_n.ctx.r.assets={};</script></html>"
    ).encode()


def _capture(tmp_path: Path, migrated_db: Callable[..., Path]) -> tuple[Path, Path, str]:
    db = migrated_db(tmp_path / "quarter.db")
    blobs = tmp_path / "blobs"
    conn = sqlite3.connect(db)
    registry = IssuerRegistry(conn)
    registry.persist(
        IssuerEntity(
            issuer_id="issuer-meli",
            idempotency_key="issuer-meli",
            entity_kind="operating_company",
            created_at=STAMP,
        )
    )
    registry.persist(
        LegacyIssuerBindingRevision(
            binding_revision_id="binding-meli",
            idempotency_key="binding-meli",
            recorded_issuer_id="legacy-ticker:MELI",
            revision=1,
            issuer_id="issuer-meli",
            outcome="selected",
            decision_kind="deterministic",
            reason_code="fixture",
            reason_details=(("ticker", "MELI"),),
            material_dissent=False,
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
        )
    )
    families: tuple[DocumentFamily, ...] = (
        "issuer_earnings_materials",
        "issuer_presentations",
        "issuer_financial_statements",
    )
    for family in families:
        ReportingEntityRegistry(conn).persist(
            SourceObligationRevision(
                obligation_revision_id=f"fixture-{family}",
                idempotency_key=f"fixture-{family}",
                obligation_key=f"issuer-meli:{family}",
                revision=1,
                issuer_id="issuer-meli",
                authority_kind="issuer_publisher",
                document_family=family,
                obligation_state="required",
                completeness_rule="publisher_surface_exhaustion",
                active_from=STAMP,
                active_to=None,
                decision_kind="deterministic",
                reason_code="fixture",
                reason_details=(("ticker", "MELI"),),
                effective_at=STAMP,
                knowledge_at=STAMP,
                recorded_at=STAMP,
            )
        )
    conn.commit()
    captured = verify_ir_home_authority(
        conn,
        request=IRHomeAuthorityRequest(
            issuer_id="issuer-meli",
            ticker="MELI",
            requested_url=URL,
            final_url=URL,
            raw_body=_page(),
            media_type="text/html",
            required_marker_groups=(("Mercado Libre",),),
            verification_method="fixture",
            blob_root=blobs,
            apply=True,
            recorded_at=STAMP,
        ),
    )
    conn.close()
    assert captured.source_observation_id is not None
    return db, blobs, captured.source_observation_id


def _argv(db: Path, blobs: Path, observation: str, *, quarter: int = 2) -> list[str]:
    return [
        "--db",
        str(db),
        "--issuer-id",
        "issuer-meli",
        "--ticker",
        "MELI",
        "--ir-url",
        URL,
        "--revision",
        "1",
        "--blob-root",
        str(blobs),
        "--meli-captured-observation",
        observation,
        "--fiscal-year",
        "2026",
        "--fiscal-quarter",
        str(quarter),
        "--publisher-file-endpoint",
        "files.example.test/",
    ]


def test_cli_selected_quarter_uses_original_capture_without_crawl_or_writes(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db, blobs, observation = _capture(tmp_path, migrated_db)

    def no_crawl(**_kwargs: object) -> object:
        pytest.fail("offline quarter must not crawl")

    monkeypatch.setattr(cli, "discover_document_inventory", no_crawl)
    before = {p: p.read_bytes() for p in blobs.rglob("*") if p.is_file()}
    capsys.readouterr()
    assert cli.main(_argv(db, blobs, observation)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["candidate_count"] == 4 and result["complete"] is False
    assert result["selected_quarter"]["inventory"]["result_id"] == "result-q2"
    assert result["selected_quarter"]["raw_sha256"] == hashlib.sha256(_page()).hexdigest()
    assert {p: p.read_bytes() for p in blobs.rglob("*") if p.is_file()} == before
    with closing(connect_sqlite(db, role=SQLiteConnectionRole.WRITER)) as conn:
        conn.row_factory = None
        assert conn.execute("SELECT COUNT(*) FROM source_inventory_snapshots").fetchone() == (0,)


def test_apply_quarters_have_independent_lineage_original_bytes_and_periods(
    tmp_path: Path, migrated_db: Callable[..., Path], capsys: pytest.CaptureFixture[str]
) -> None:
    db, blobs, observation = _capture(tmp_path, migrated_db)
    for quarter in (1, 2):
        assert cli.main([*_argv(db, blobs, observation, quarter=quarter), "--apply"]) == 0
    capsys.readouterr()
    with closing(connect_sqlite(db, role=SQLiteConnectionRole.WRITER)) as conn:
        conn.row_factory = None
        assert conn.execute(
            "SELECT inventory_key,outcome,authoritative FROM source_inventory_snapshots ORDER BY inventory_key"
        ).fetchall() == [
            ("issuer-meli:ir-quarter:2026:Q1", "partial", 0),
            ("issuer-meli:ir-quarter:2026:Q2", "partial", 0),
        ]
        assert conn.execute(
            "SELECT DISTINCT completion_status FROM source_inventory_snapshot_seals"
        ).fetchall() == [("incomplete",)]
        assert conn.execute(
            "SELECT COUNT(*) FROM source_inventory_components WHERE component_key='original-publisher-html' AND source_observation_id=?",
            (observation,),
        ).fetchone() == (2,)
        periods = conn.execute(
            "SELECT period_end,expectation_basis FROM expected_documents"
        ).fetchall()
        assert len(periods) == 8
        assert {str(row[0])[:10] for row in periods} == {"2026-03-31", "2026-06-30"}
        assert {row[1] for row in periods} == {"publisher_candidate"}
        assert not conn.execute(
            "SELECT 1 FROM source_inventory_snapshots WHERE inventory_key='issuer-meli:ir-crawl'"
        ).fetchall()
        artifacts = conn.execute(
            "SELECT b.storage_uri FROM evidence_source_observations o JOIN evidence_content_blobs b ON b.sha256=o.blob_sha256 WHERE o.collector_code_version='sync-ir-source-inventory@2'"
        ).fetchall()
    from urllib.parse import unquote, urlsplit

    derived = [
        json.loads(Path(unquote(urlsplit(str(row[0])).path)).read_bytes()) for row in artifacts
    ]
    envelopes = [item for item in derived if "selected_quarter" in item]
    assert len(envelopes) == 2
    assert all(
        item["selected_quarter"]["source_observation_id"] == observation for item in envelopes
    )
    assert all(
        {doc["label"] for doc in item["selected_quarter"]["inventory"]["documents"]} == set(LABELS)
        for item in envelopes
    )
    digest = hashlib.sha256(_page()).hexdigest()
    assert (blobs / digest[:2] / digest).read_bytes() == _page()


@pytest.mark.parametrize(
    "failure",
    [
        "changed_bytes",
        "missing_observation",
        "unapproved_endpoint",
        "missing_period",
        "wrong_issuer",
        "wrong_source",
    ],
)
def test_cli_rejects_bad_source_before_inventory_writes(
    tmp_path: Path, migrated_db: Callable[..., Path], failure: str
) -> None:
    db, blobs, observation = _capture(tmp_path, migrated_db)
    args = _argv(db, blobs, observation)
    if failure == "changed_bytes":
        digest = hashlib.sha256(_page()).hexdigest()
        (blobs / digest[:2] / digest).write_bytes(_page() + b" ")
    elif failure == "missing_observation":
        args[args.index("--meli-captured-observation") + 1] = "missing"
    elif failure == "unapproved_endpoint":
        args = args[:-2]
    elif failure == "missing_period":
        args[args.index("--fiscal-quarter") + 1] = "3"
    elif failure == "wrong_issuer":
        args[args.index("--issuer-id") + 1] = "another-issuer"
    else:
        args[args.index("--ir-url") + 1] = URL + "/unapproved"
    before = {p for p in blobs.rglob("*") if p.is_file()}
    assert cli.main([*args, "--apply"]) in {1, 2}
    with closing(connect_sqlite(db, role=SQLiteConnectionRole.WRITER)) as conn:
        conn.row_factory = None
        assert conn.execute("SELECT COUNT(*) FROM source_inventory_snapshots").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM evidence_source_observations").fetchone() == (1,)
    assert {p for p in blobs.rglob("*") if p.is_file()} == before


def test_durable_owner_replays_scope_and_rejects_forged_candidates(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    from ir_pipeline.authority import PublisherEndpointRule
    from ir_pipeline.meli_inventory import load_captured_meli_quarter
    from ir_pipeline.source_inventory import (
        meli_quarter_discovery,
        source_inventory_request,
        sync_ir_source_inventory,
    )

    db, blobs, observation = _capture(tmp_path, migrated_db)
    with closing(connect_sqlite(db, role=SQLiteConnectionRole.WRITER)) as conn:
        conn.row_factory = None
        scope = load_captured_meli_quarter(
            conn,
            issuer_id="issuer-meli",
            ticker="MELI",
            ir_url=URL,
            source_observation_id=observation,
            fiscal_year=2026,
            fiscal_quarter=2,
            publisher_file_rules=(PublisherEndpointRule(host="files.example.test"),),
            blob_root=blobs,
            knowledge_at=STAMP,
        )
        request = source_inventory_request(
            issuer_id="issuer-meli",
            ticker="MELI",
            ir_url=URL,
            revision=1,
            inventory=meli_quarter_discovery(scope),
            selected_quarter=scope,
            retrieval_config_sha256="a" * 64,
            collector_code_version="test",
            started_at=STAMP,
            completed_at=STAMP,
            recorded_at=STAMP,
            reconciled_at=STAMP,
            apply=True,
        )
        forged = request.model_copy(
            update={
                "discovery": request.discovery.model_copy(
                    update={"candidates": request.discovery.candidates[:-1]}
                )
            }
        )
        with pytest.raises(ValueError, match="does not replay"):
            sync_ir_source_inventory(conn, forged, blob_root=blobs)
        assert conn.execute("SELECT COUNT(*) FROM source_inventory_snapshots").fetchone() == (0,)
        first = sync_ir_source_inventory(conn, request, blob_root=blobs)
        replay = sync_ir_source_inventory(conn, request, blob_root=blobs)
        assert replay.records_created == 0 and first.snapshot_id == replay.snapshot_id
        assert not first.complete


@pytest.mark.parametrize(
    "extra",
    [
        ["--fiscal-quarter", "2"],
        ["--meli-captured-observation", "x"],
        [
            "--meli-captured-observation",
            "x",
            "--fiscal-year",
            "2026",
            "--fiscal-quarter",
            "2",
            "--authority-evidence",
            "x.json",
        ],
    ],
)
def test_cli_refuses_ambiguous_selection_before_opening_database(
    extra: list[str], tmp_path: Path
) -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(
            [
                "--db",
                str(tmp_path / "absent.db"),
                "--issuer-id",
                "issuer-meli",
                "--ticker",
                "MELI",
                "--ir-url",
                URL,
                "--revision",
                "1",
                *extra,
            ]
        )
    assert error.value.code == 2
    assert not (tmp_path / "absent.db").exists()


@pytest.mark.parametrize("case", ["derived_observation", "different_url", "future_capture"])
def test_original_source_binding_is_required_before_parser(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from ir_pipeline import meli_inventory
    from ir_pipeline.authority import PublisherEndpointRule
    from provenance.evidence_ledger import EvidenceLedger, SourceObservation

    db, blobs, original = _capture(tmp_path, migrated_db)

    def no_parse(*_args: object, **_kwargs: object) -> object:
        pytest.fail("invalid capture must stop before parsing")

    monkeypatch.setattr(meli_inventory, "discover_embedded_quarterly_inventory", no_parse)
    with closing(connect_sqlite(db, role=SQLiteConnectionRole.WRITER)) as conn:
        conn.row_factory = None
        EvidenceLedger(conn).persist(
            SourceObservation(
                observation_id="bad-observation",
                idempotency_key="bad-observation",
                source_kind="ir_crawl"
                if case == "derived_observation"
                else "ir_publisher_home_authority",
                source_url=URL + "/other" if case == "different_url" else URL,
                blob_sha256=hashlib.sha256(_page()).hexdigest(),
                source_published_at=None,
                filing_at=None,
                accepted_at=None,
                observed_at=STAMP.replace(year=2027) if case == "future_capture" else STAMP,
                retrieved_at=STAMP.replace(year=2027) if case == "future_capture" else STAMP,
                retrieval_config_sha256="b" * 64,
                collector_code_version="fixture",
            )
        )
        conn.commit()
        with pytest.raises(ValueError):
            meli_inventory.load_captured_meli_quarter(
                conn,
                issuer_id="issuer-meli",
                ticker="MELI",
                ir_url=URL,
                source_observation_id="bad-observation",
                fiscal_year=2026,
                fiscal_quarter=2,
                publisher_file_rules=(PublisherEndpointRule(host="files.example.test"),),
                blob_root=blobs,
                knowledge_at=STAMP,
            )
        assert conn.execute("SELECT COUNT(*) FROM source_inventory_snapshots").fetchone()[0] == 0
    assert original != "bad-observation"
