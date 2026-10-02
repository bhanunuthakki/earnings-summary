"""Mixed SEC inventories must retain metadata without inventing source duties."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import NoReturn

import pytest
import requests
from pydantic import ValidationError

from execution import sync_sec_filing_inventory as sync
from filings.sec_submissions_inventory import parse_sec_submissions_inventory
from provenance.issuer_registry_bootstrap import (
    BootstrapRequest,
    bootstrap_issuer_reporting_registry,
)
from provenance.sec_inventory_scope import SecInventoryScopeManifest

STAMP = datetime(2026, 10, 1, tzinfo=UTC)


class _Clock(datetime):
    @classmethod
    def fromisoformat(cls, date_string: str) -> datetime:
        return datetime.fromisoformat(date_string)

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> datetime:
        return STAMP if tz is not None else STAMP.replace(tzinfo=None)


def _root(forms: list[str]) -> bytes:
    count = len(forms)
    columns = {
        "accessionNumber": [f"0000001001-25-{n:06d}" for n in range(1, count + 1)],
        "filingDate": ["2025-02-01"] * count,
        "reportDate": ["2024-12-31"] * count,
        "acceptanceDateTime": ["20250201120000"] * count,
        "act": ["34"] * count,
        "form": forms,
        "fileNumber": ["001-00001"] * count,
        "filmNumber": ["25000001"] * count,
        "items": [""] * count,
        "size": ["100"] * count,
        "isXBRL": ["1"] * count,
        "isInlineXBRL": ["1"] * count,
        # Missing primaries stay authoritative-unavailable, not silently filtered.
        "primaryDocument": ["current.htm" if form == "8-K" else "" for form in forms],
        "primaryDocDescription": ["Synthetic filing"] * count,
    }
    return json.dumps(
        {
            "cik": "1001",
            "name": "Acme",
            "tickers": ["ACME"],
            "filings": {"recent": columns, "files": []},
        }
    ).encode()


@pytest.mark.parametrize("unknown", [False, True])
@pytest.mark.parametrize("prior_failure", [False, True])
def test_mixed_inventory_applies_real_duty_bindings_and_exact_replay(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    unknown: bool,
    prior_failure: bool,
) -> None:
    database = migrated_db(tmp_path / "inventory.db")
    with sqlite3.connect(database) as conn:
        conn.execute(
            "INSERT INTO tracked_companies (user_id,ticker,name,list_type) VALUES ('bhanu','ACME','Acme','portfolio')"
        )
        conn.commit()
        bootstrap_issuer_reporting_registry(
            conn,
            raw_body=b'{"0":{"cik_str":1001,"ticker":"ACME","title":"Acme"}}',
            request=BootstrapRequest(
                source_url="https://www.sec.gov/files/company_tickers.json",
                blob_root=tmp_path / "blobs",
                apply=True,
                recorded_at=STAMP - timedelta(days=1),
            ),
        )
    forms = ["10-K", "8-K", "UPLOAD", "424B5", "4"] + (["NEW-FORM"] if unknown else [])
    root = _root(forms)
    fetched: list[str] = []

    def fetch(_session: requests.Session, url: str, _agent: str) -> bytes:
        fetched.append(url)
        if url == "https://data.sec.gov/submissions/CIK0000001001.json":
            return root
        prefix = "https://www.sec.gov/Archives/edgar/data/1001/000000100125000002/"
        if url == prefix + "index.json":
            return json.dumps(
                {
                    "directory": {
                        "name": "/Archives/edgar/data/1001/000000100125000002",
                        "parent-dir": "/Archives/edgar/data/1001",
                        "item": [
                            {
                                "name": name,
                                "type": "text.gif",
                                "size": "100",
                                "last-modified": "2025-02-01 12:00:00",
                            }
                            for name in ("current.htm", "release.htm")
                        ],
                    }
                }
            ).encode()
        assert url == prefix + "0000001001-25-000002-index.html"
        return b'<html><table class="tableFile"><tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr><tr><td>1</td><td>Report</td><td><a href="/Archives/edgar/data/1001/000000100125000002/current.htm">current.htm</a></td><td>8-K</td><td>100</td></tr><tr><td>2</td><td>Release</td><td><a href="/Archives/edgar/data/1001/000000100125000002/release.htm">release.htm</a></td><td>EX-99.1</td><td>100</td></tr></table></html>'

    monkeypatch.setattr(sync, "_fetch", fetch)
    monkeypatch.setattr(sync, "_utc_now", lambda: STAMP)
    monkeypatch.setattr(sync, "datetime", _Clock)
    monkeypatch.setattr(sync, "sec_user_agent", lambda: "synthetic contact@example.test")
    monkeypatch.setattr(sync, "PROJECT_ROOT", tmp_path)
    args = [
        "--db",
        str(database),
        "--ticker",
        "ACME",
        "--cik",
        "1001",
        "--revision",
        "1",
        "--blob-root",
        str(tmp_path / "blobs"),
        "--package-checkpoint-root",
        str(tmp_path / "checkpoints"),
        "--apply",
    ]
    retained_receipts: list[tuple[object, ...]] = []
    retained_observations: list[tuple[object, ...]] = []
    if prior_failure:

        def fail_scope(**_kwargs: object) -> NoReturn:
            raise ValueError("SEC form is outside the governed source-duty map: UPLOAD")

        # Recreate the durable boundary of the failed live attempt: raw responses
        # committed, no inventory snapshot, failed terminal attempt retained.
        with monkeypatch.context() as failure:
            failure.setattr(sync, "SecInventoryScopeManifest", fail_scope)
            with pytest.raises(ValueError, match="source-duty map: UPLOAD"):
                sync.main(args)
        with sqlite3.connect(database) as conn:
            assert conn.execute("SELECT COUNT(*) FROM source_inventory_snapshots").fetchone() == (
                0,
            )
            retained_receipts = conn.execute(
                "SELECT * FROM sec_execution_receipts ORDER BY rowid"
            ).fetchall()
            retained_observations = conn.execute(
                "SELECT * FROM evidence_source_observations ORDER BY observation_id"
            ).fetchall()
            assert conn.execute(
                "SELECT COUNT(*) FROM evidence_source_observations WHERE collector_code_version='sync-sec-filing-inventory@5'"
            ).fetchone() == (3,)
            assert conn.execute(
                "SELECT COUNT(*) FROM sec_execution_receipts WHERE json_extract(payload_json,'$.result.reason_code')='inventory_failed'"
            ).fetchone() == (1,)
        capsys.readouterr()
    assert sync.main(args[:-1]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert sync.main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["component_count"] == preview["component_count"]
    assert first["component_count"] == (
        5 if unknown else 4
    )  # root + two HTTP package responses + derived scope (+ failure)

    assert first["filing_count"] == len(forms)
    assert first["inventory_only_filing_count"] == 3
    assert first["package_failure_count"] == 0, first
    assert first["complete"] is (not unknown)
    with sqlite3.connect(database) as conn:
        assert conn.execute(
            "SELECT form_type FROM expected_documents ORDER BY form_type"
        ).fetchall() == [("10-K",), ("8-K",), ("8-K",)]
        assert conn.execute(
            "SELECT document_family FROM expected_document_obligation_bindings ORDER BY document_family"
        ).fetchall() == [
            ("continuous_disclosure",),
            ("continuous_disclosure",),
            ("operating_company_periodic",),
        ]
        assert conn.execute(
            "SELECT coverage_status FROM source_coverage_assessments ORDER BY coverage_status"
        ).fetchall() == [("authority_unavailable",), ("available",), ("available",)]
        assert conn.execute(
            "SELECT completion_status FROM source_inventory_snapshot_seals"
        ).fetchone() == (("incomplete" if unknown else "complete"),)
        observation = conn.execute(
            "SELECT blob_sha256,source_kind,source_url,observed_at,retrieved_at FROM evidence_source_observations WHERE observation_id=?",
            (first["scope_manifest_observation_id"],),
        ).fetchone()
        assert observation is not None and observation[0] == first["scope_manifest_sha256"]
        digest = str(observation[0])
        payload = (tmp_path / "blobs" / digest[:2] / digest).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == digest
        scope = SecInventoryScopeManifest.model_validate_json(payload)
        assert scope.claim_basis == "software_derived"
        assert scope.clock_basis == "first_local_manifest_capture"
        assert scope.policy_version == "governed-reporting-package-scope@4"
        assert scope.source_issuer_id == "sec-cik:0000001001"
        assert all(item.filing.issuer_id == scope.issuer_id for item in scope.filings)
        parsed = parse_sec_submissions_inventory(
            cik="1001", ticker="ACME", primary_body=root, historical=()
        )
        scope.verify_reconstruction(parsed=parsed, source_inputs=scope.source_inputs)
        # Reconstruction refuses omitted accessions or substituted parent hashes.
        with pytest.raises(ValueError, match="complete authoritative inputs"):
            scope.model_copy(update={"filings": scope.filings[:-1]}).verify_reconstruction(
                parsed=parsed, source_inputs=scope.source_inputs
            )
        altered_sources = (scope.source_inputs[0].model_copy(update={"blob_sha256": "a" * 64}),)
        with pytest.raises(ValueError, match="complete authoritative inputs"):
            scope.verify_reconstruction(parsed=parsed, source_inputs=altered_sources)
        payload_fields = scope.model_dump()
        payload_fields["root_source_observation_id"] = "different-observation"
        with pytest.raises(ValidationError, match="primary source component"):
            SecInventoryScopeManifest.model_validate(payload_fields)
        payload_fields = scope.model_dump()
        payload_fields["filings"] = (
            scope.filings[0].model_copy(
                update={"filing": scope.filings[0].filing.model_copy(update={"issuer_id": "other"})}
            ),
            *scope.filings[1:],
        )
        with pytest.raises(ValidationError, match="issuer"):
            SecInventoryScopeManifest.model_validate(payload_fields)
        # Later local generation/recording cannot create another derived observation
        # when the immutable parent inputs are exactly the same. SEC clocks stay put.
        later = STAMP + timedelta(hours=1)
        before_observations = conn.execute(
            "SELECT * FROM evidence_source_observations ORDER BY observation_id"
        ).fetchall()
        reused = sync.capture_scope_manifest(
            conn,
            body=payload,
            url=str(observation[2]),
            blob_root=tmp_path / "blobs",
            config_sha=str(
                conn.execute(
                    "SELECT retrieval_config_sha256 FROM evidence_source_observations WHERE observation_id=?",
                    (first["scope_manifest_observation_id"],),
                ).fetchone()[0]
            ),
            captured_at=later,
        )
        assert reused == first["scope_manifest_observation_id"]
        assert (
            conn.execute(
                "SELECT * FROM evidence_source_observations ORDER BY observation_id"
            ).fetchall()
            == before_observations
        )
        assert [item.filing.form_type for item in scope.filings] == forms
        assert [item.disposition for item in scope.filings] == ["governed_reporting"] * 2 + [
            "inventory_only"
        ] * 3 + (["unclassified"] if unknown else [])
        assert observation[1] == "sec_inventory_scope_derived"
        assert str(observation[2]).startswith("urn:sec-inventory-duty-scope:")
        assert conn.execute(
            "SELECT source_observation_id FROM source_inventory_snapshots"
        ).fetchone() == (scope.root_source_observation_id,)
        for source in scope.source_inputs:
            assert conn.execute(
                "SELECT blob_sha256 FROM evidence_source_observations WHERE observation_id=?",
                (source.source_observation_id,),
            ).fetchone() == (source.blob_sha256,)
        assert conn.execute(
            "SELECT source_observation_id FROM source_inventory_components WHERE component_key='software-derived-duty-scope'"
        ).fetchone() == (first["scope_manifest_observation_id"],)
        assert conn.execute(
            "SELECT COUNT(*) FROM evidence_document_versions WHERE observation_id=?",
            (first["scope_manifest_observation_id"],),
        ).fetchone() == (0,)
    assert sync.main(args) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay["snapshot_id"] == first["snapshot_id"]
    assert replay["records_created"] == 0
    assert len(fetched) == (6 if prior_failure else 5)
    # Root HTTP acquisition remains explicit; package responses replay exact bytes.
    if prior_failure:
        with sqlite3.connect(database) as conn:
            receipts = conn.execute(
                "SELECT * FROM sec_execution_receipts ORDER BY rowid"
            ).fetchall()
            observations = conn.execute(
                "SELECT * FROM evidence_source_observations ORDER BY observation_id"
            ).fetchall()
            assert receipts[: len(retained_receipts)] == retained_receipts
            assert all(row in observations for row in retained_observations)
            assert conn.execute(
                "SELECT COUNT(*) FROM evidence_source_observations WHERE collector_code_version='sync-sec-filing-inventory@5'"
            ).fetchone() == (3,)

    if not unknown and not prior_failure:
        # A real new HTTP acquisition is distinct even when response bytes match.
        # Same revision must refuse; explicit next revision retains both captures.
        class LaterClock(_Clock):
            @classmethod
            def now(cls, tz: tzinfo | None = None) -> datetime:
                value = STAMP + timedelta(hours=2)
                return value if tz is not None else value.replace(tzinfo=None)

        monkeypatch.setattr(sync, "_utc_now", lambda: STAMP + timedelta(hours=2))
        monkeypatch.setattr(sync, "datetime", LaterClock)
        with pytest.raises(ValueError, match="immutable source_inventory_snapshots"):
            sync.main(args)
        capsys.readouterr()
        next_args = args.copy()
        next_args[next_args.index("--revision") + 1] = "2"
        assert sync.main(next_args) == 0
        renewed = json.loads(capsys.readouterr().out)
        assert renewed["snapshot_id"] != first["snapshot_id"]
        assert renewed["scope_manifest_observation_id"] != first["scope_manifest_observation_id"]
        with sqlite3.connect(database) as conn:
            assert conn.execute("SELECT COUNT(*) FROM source_inventory_snapshots").fetchone() == (
                2,
            )
            assert conn.execute(
                "SELECT COUNT(*) FROM evidence_source_observations WHERE source_kind='sec_inventory_scope_derived'"
            ).fetchone() == (2,)
