from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from alembic.config import Config

from alembic import command
from filings.inline_xbrl_processor import FILING_XBRL_PROTOCOL_SQL
from provenance.integrity_audit import AuditOptions, audit_connection

ROOT = Path(__file__).resolve().parents[1]
PARENT = "0053_reviewed_financial_derivations"
REVISION = "0054_filing_xbrl_unit_protocol"


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _trigger(path: Path) -> str:
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='trg_filing_xbrl_input_seal_exact'"
        ).fetchone()
    assert row is not None
    return str(row[0])


def test_current_schema_retains_v1_and_admits_only_committed_v2_unit_helper(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "unit-protocol.db")
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='trg_filing_xbrl_input_seal_exact'"
        ).fetchone()
        assert row is not None
        assert FILING_XBRL_PROTOCOL_SQL in row[0]


def test_migration_preserves_every_other_seal_predicate_and_round_trips(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "migration.db", target=PARENT)
    before = _trigger(path)
    config = _config(path)
    migrated_db(path, target=REVISION, upgrade_existing=True)
    upgraded = _trigger(path)
    assert (
        upgraded.replace(
            FILING_XBRL_PROTOCOL_SQL, "artifact.bridge_protocol_version='filing-xbrl-bridge.v1'", 1
        )
        == before
    )
    command.downgrade(config, PARENT)
    assert _trigger(path) == before


def test_migration_refuses_an_unknown_predecessor_guard_without_dropping_it(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "unexpected.db", target=PARENT)
    unexpected = _trigger(path).replace("filing-xbrl-bridge.v1", "unreviewed-bridge.v9")
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER trg_filing_xbrl_input_seal_exact")
        conn.execute(unexpected)
    with pytest.raises(RuntimeError, match="differs from its predecessor"):
        migrated_db(path, target=REVISION, upgrade_existing=True)
    assert _trigger(path) == unexpected


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("valid", True),
        ("legacy", True),
        ("missing", False),
        ("mismatch", False),
        ("duplicate", False),
        ("invalid_hash", False),
        ("negative_size", False),
        ("scalar_member", False),
        ("object_members", False),
        ("invalid_json", False),
        ("unknown_protocol", False),
    ],
)
def test_durable_processor_subgate_checks_exact_unit_closure(
    tmp_path: Path, migrated_db: Callable[..., Path], mutation: str, expected: bool
) -> None:
    path = migrated_db(tmp_path / "gate.db")
    trigger = _trigger(path)
    # Exercise the actual retained trigger's processor subgate. Other source,
    # clock, census and identity predicates remain covered by the full trigger.
    predicate = trigger.split(
        "OR NOT EXISTS (SELECT 1 FROM filing_xbrl_processor_artifacts artifact ", 1
    )[1].split(" OR fact_sha256(NEW.canonical_member_set_json)", 1)[0]
    query = (
        "SELECT EXISTS (SELECT 1 FROM filing_xbrl_processor_artifacts artifact " + predicate
    ).replace("NEW.processor_artifact_id", "'synthetic'")
    manifest = json.loads((ROOT / "config" / "filing_xbrl_processor_bundle.json").read_text())
    manifest["bridge_protocol_version"] = "filing-xbrl-bridge.v2"
    manifest["build_provenance"]["unit_source_sha256"] = "a" * 64
    helper = {
        "relative_path": "earnings_summary_xbrl_units.py",
        "blob_sha256": "a" * 64,
        "byte_size": 1,
    }
    manifest["execution"]["runtime_members"] = [helper]
    protocol = "filing-xbrl-bridge.v2"
    if mutation == "legacy":
        protocol = "filing-xbrl-bridge.v1"
        manifest = {"qualification": manifest["qualification"]}
    elif mutation == "missing":
        del manifest["build_provenance"]["unit_source_sha256"]
    elif mutation == "mismatch":
        helper["blob_sha256"] = "b" * 64
    elif mutation == "duplicate":
        manifest["execution"]["runtime_members"].append(dict(helper))
    elif mutation == "invalid_hash":
        manifest["build_provenance"]["unit_source_sha256"] = "z" * 64
        helper["blob_sha256"] = "z" * 64
    elif mutation == "negative_size":
        helper["byte_size"] = -1
    elif mutation == "scalar_member":
        manifest["execution"]["runtime_members"] = ["not an object"]
    elif mutation == "object_members":
        manifest["execution"]["runtime_members"] = {"helper": helper}
    elif mutation == "unknown_protocol":
        protocol = "unreviewed-bridge.v9"
    body = "not-json" if mutation == "invalid_json" else json.dumps(manifest)
    with sqlite3.connect(":memory:") as conn:
        conn.execute(
            "CREATE TABLE filing_xbrl_processor_artifacts (processor_artifact_id TEXT,"
            "arelle_version TEXT, edgar_version TEXT,xule_version TEXT,"
            "bridge_protocol_version TEXT,canonical_manifest_json TEXT "
            "CHECK(json_valid(canonical_manifest_json)))"
        )
        if mutation == "invalid_json":
            with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
                conn.execute(
                    "INSERT INTO filing_xbrl_processor_artifacts VALUES (?,?,?,?,?,?)",
                    ("synthetic", "2.39.8", "26.1", "30052", protocol, body),
                )
            return
        conn.execute(
            "INSERT INTO filing_xbrl_processor_artifacts VALUES (?,?,?,?,?,?)",
            ("synthetic", "2.39.8", "26.1", "30052", protocol, body),
        )
        assert bool(conn.execute(query).fetchone()[0]) is expected


@pytest.mark.parametrize("missing_helper", [False, True])
def test_integrity_audit_reconstructs_v2_helper_contract_and_downgrade_holds(
    tmp_path: Path, migrated_db: Callable[..., Path], missing_helper: bool
) -> None:
    path = migrated_db(tmp_path / "audit.db")
    manifest = cast(
        dict[str, object],
        json.loads((ROOT / "config" / "filing_xbrl_processor_bundle.json").read_text()),
    )
    execution = cast(dict[str, object], manifest["execution"])
    provenance = cast(dict[str, object], manifest["build_provenance"])
    manifest["bridge_protocol_version"] = "filing-xbrl-bridge.v2"
    provenance["unit_source_sha256"] = "a" * 64
    members = [
        {"relative_path": "Scripts/python.exe", "blob_sha256": "b" * 64, "byte_size": 1},
        {
            "relative_path": "earnings_summary_xbrl_units.py",
            "blob_sha256": "a" * 64,
            "byte_size": 1,
        },
    ]
    if missing_helper:
        members.pop()
    execution["runtime_members"] = members
    execution["bundle_python_sha256"] = "b" * 64
    canonical_members = json.dumps(members, sort_keys=True, separators=(",", ":"))
    runtime_sha = hashlib.sha256(canonical_members.encode()).hexdigest()
    execution["runtime_artifact_sha256"] = runtime_sha
    body = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    manifest_sha = hashlib.sha256(body.encode()).hexdigest()
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO filing_xbrl_processor_artifacts "
            "(processor_artifact_id,idempotency_key,bundle_name,arelle_version,edgar_version,"
            "xule_version,bridge_protocol_version,artifact_sha256,sandbox_launcher_sha256,"
            "bundle_python_sha256,canonical_manifest_json,manifest_sha256,recorded_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "synthetic",
                "synthetic",
                manifest["bundle_name"],
                "2.39.8",
                "26.1",
                "30052",
                "filing-xbrl-bridge.v2",
                runtime_sha,
                execution["sandbox_launcher_sha256"],
                "b" * 64,
                body,
                manifest_sha,
                "2026-10-04T00:00:00+00:00",
            ),
        )
        codes = {finding.code for finding in audit_connection(conn, AuditOptions()).findings}
        assert ("FILING_XBRL_PROCESSOR_COORDINATES_UNQUALIFIED" in codes) is missing_helper
        assert ("FILING_XBRL_RESULT_COMMITMENT_DIGEST_MISMATCH" in codes) is missing_helper
    before = _trigger(path)
    with pytest.raises(RuntimeError, match="v2 artifacts are retained"):
        command.downgrade(_config(path), PARENT)
    assert _trigger(path) == before
