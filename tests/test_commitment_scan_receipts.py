# pyright: reportPrivateUsage=false

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config

from alembic import command
from compute.management_indicators import ManagementIndicatorExtractionManifest, persist_indicator
from compute.say_do import CommitmentInput
from compute.say_do_extractor import (
    TranscriptScanResult,
    extract_for_transcript,
    record_scan,
    transcripts_without_scan_receipt,
)
from llm.prompt_versions import prompt_version_for
from llm_client import LLMBudgetExceeded, LLMSetupError
from pipeline.commitment_scan_receipts import (
    CommitmentScanCoverageState,
    _parse_observed_segments,
    append_commitment_scan_receipt,
    commitment_scan_coverage,
    current_commitment_scan_receipt,
)


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _legacy_zero_receipt_id(
    *,
    transcript_id: int,
    document_id: int,
    acquisition_receipt_id: str,
    transcript_sha256: str,
    prompt_version: str = "v1",
) -> str:
    return _sha(
        _canonical(
            {
                "schema_version": "commitment-scan-receipt@1",
                "transcript_id": transcript_id,
                "document_id": document_id,
                "transcript_acquisition_receipt_id": acquisition_receipt_id,
                "transcript_sha256": transcript_sha256,
                "prompt_version": prompt_version,
                "n_extracted": 0,
                "output_manifest_sha256": _sha("[]"),
            }
        )
    )


def _config(path: Path) -> Config:
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def _seed_transcript(path: Path, *, segments: list[str]) -> tuple[int, tuple[int, ...]]:
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        transcript_sha = hashlib.sha256("\n".join(segments).encode()).hexdigest()
        conn.execute(
            "INSERT INTO documents "
            "(ticker,source_type,doc_type,file_path,sha256,fetched_at,fetch_status,raw_bytes_size) "
            "VALUES ('ACME','ir_doc','ir_transcript',?,?,?,?,?)",
            (
                "transcripts/processed/ACME_Q2_2026.txt",
                transcript_sha,
                "2026-07-01",
                "ok",
                sum(len(item.encode()) for item in segments),
            ),
        )
        document_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            "INSERT INTO transcripts "
            "(document_id,ticker,call_date,fiscal_period_type,period_end,source,is_active,"
            "is_current,recorded_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                document_id,
                "ACME",
                "2026-07-01",
                "Q2",
                "2026-06-30",
                "issuer_ir",
                1,
                1,
                "2026-07-01",
            ),
        )
        transcript_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        segment_ids: list[int] = []
        for sequence, text in enumerate(segments):
            conn.execute(
                "INSERT INTO transcript_segments "
                "(transcript_id,seq,speaker,time_code_start,time_code_end,text) "
                "VALUES (?,?,?,?,?,?)",
                (transcript_id, sequence, "CEO", f"00:0{sequence}:00", None, text),
            )
            segment_ids.append(int(conn.execute("SELECT last_insert_rowid()").fetchone()[0]))

        key = "transcript:" + "1" * 64
        contract_sha = "2" * 64
        authorization = {
            "idempotency_key": key,
            "request": {
                "canonical_ticker": "ACME",
                "document_type": "earnings_call_transcript",
                "fiscal_quarter": 2,
                "fiscal_year": 2026,
                "provider": "issuer_ir",
                "source_regime_identity": {
                    "contract_sha256": contract_sha,
                    "regime": "combined",
                },
                "source_type": "ir_doc",
            },
            "schema_version": "transcript-acquisition-authorization@1",
            "status": "authorized",
            "stored_target": {"coverage_role": "holdings", "fiscal_year_end_month": 12},
        }
        artifact = {
            "authorization": authorization,
            "canonical_document_path": "transcripts/raw/ACME_Q2_2026.txt",
            "document_id": document_id,
            "schema_version": "authorized-transcript-artifact@1",
            "source_url": None,
            "staged": {
                "sha256": transcript_sha,
                "size_bytes": sum(len(item.encode()) for item in segments),
            },
        }
        artifact_json = _canonical(artifact)
        for trigger in (
            "trg_transcript_acquisition_receipts_validate",
            "trg_transcript_acquisition_receipts_stored_target_binding",
            "trg_transcript_acquisition_receipts_document_binding",
        ):
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")  # nosec B608 -- test-owned constants
        conn.execute(
            "INSERT INTO transcript_acquisition_receipts "
            "(receipt_id,idempotency_key,document_id,canonical_ticker,fiscal_year,fiscal_quarter,"
            "canonical_document_path,artifact_sha256,artifact_size_bytes,source_url,provider,"
            "source_type,document_type,source_regime,source_regime_contract_sha256,"
            "authorization_json,artifact_json,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                _sha(artifact_json),
                key,
                document_id,
                "ACME",
                2026,
                2,
                "transcripts/raw/ACME_Q2_2026.txt",
                transcript_sha,
                sum(len(item.encode()) for item in segments),
                None,
                "issuer_ir",
                "ir_doc",
                "earnings_call_transcript",
                "combined",
                contract_sha,
                _canonical(authorization),
                artifact_json,
                "2026-09-05T00:00:00Z",
            ),
        )
        conn.commit()
    return transcript_id, tuple(segment_ids)


def _zero_scan(conn: sqlite3.Connection, transcript_id: int) -> TranscriptScanResult:
    return extract_for_transcript(
        conn,
        transcript_id,
        llm_call=lambda _prompt: '{"commitments":[],"novel_indicators":[]}',
    )


def _add_changed_acquisition(
    conn: sqlite3.Connection,
    *,
    document_id: int,
    changed_text: str,
    key_digit: str,
) -> str:
    changed_sha = _sha(changed_text)
    changed_key = "transcript:" + key_digit * 64
    contract_sha = "2" * 64
    authorization = {
        "idempotency_key": changed_key,
        "request": {
            "canonical_ticker": "ACME",
            "document_type": "earnings_call_transcript",
            "fiscal_quarter": 2,
            "fiscal_year": 2026,
            "provider": "issuer_ir",
            "source_regime_identity": {"contract_sha256": contract_sha, "regime": "combined"},
            "source_type": "ir_doc",
        },
        "schema_version": "transcript-acquisition-authorization@1",
        "status": "authorized",
    }
    artifact = {
        "authorization": authorization,
        "canonical_document_path": "transcripts/raw/ACME_Q2_2026.txt",
        "document_id": document_id,
        "schema_version": "authorized-transcript-artifact@1",
        "source_url": None,
        "staged": {"sha256": changed_sha, "size_bytes": len(changed_text.encode())},
    }
    artifact_json = _canonical(artifact)
    conn.execute("DROP TRIGGER trg_transcript_documents_immutable")
    conn.execute(
        "UPDATE documents SET sha256=?,raw_bytes_size=? WHERE id=?",
        (changed_sha, len(changed_text.encode()), document_id),
    )
    receipt_id = _sha(artifact_json)
    conn.execute(
        "INSERT INTO transcript_acquisition_receipts "
        "(receipt_id,idempotency_key,document_id,canonical_ticker,fiscal_year,fiscal_quarter,"
        "canonical_document_path,artifact_sha256,artifact_size_bytes,source_url,provider,"
        "source_type,document_type,source_regime,source_regime_contract_sha256,"
        "authorization_json,artifact_json,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            receipt_id,
            changed_key,
            document_id,
            "ACME",
            2026,
            2,
            "transcripts/raw/ACME_Q2_2026.txt",
            changed_sha,
            len(changed_text.encode()),
            None,
            "issuer_ir",
            "ir_doc",
            "earnings_call_transcript",
            "combined",
            contract_sha,
            _canonical(authorization),
            artifact_json,
            "2026-09-06T00:00:00Z",
        ),
    )
    return receipt_id


def _load_script() -> Any:
    import importlib.util

    source = (
        Path(__file__).resolve().parents[1] / "execution" / "extract_commitments_from_transcript.py"
    )
    spec = importlib.util.spec_from_file_location("bha140_extract_commitments", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_backfill_script() -> Any:
    import importlib.util

    source = Path(__file__).resolve().parents[1] / "execution" / "backfill_transcripts.py"
    spec = importlib.util.spec_from_file_location("bha140_backfill_transcripts", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_onboard_script() -> Any:
    import importlib.util

    source = Path(__file__).resolve().parents[1] / "execution" / "onboard_pending_tickers.py"
    spec = importlib.util.spec_from_file_location("bha140_onboard_pending_tickers", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _seed_kpi(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO kpi_definitions (ticker,name,unit,primary_source) "
            "VALUES ('ACME','Revenue Growth','percent','transcript')"
        )
        conn.commit()


def _commitment_response(narrative: str) -> str:
    return _canonical(
        {
            "commitments": [
                {
                    "comparator": "ge",
                    "kpi_name": "Revenue Growth",
                    "narrative": narrative,
                    "period_target": "2026-09-30",
                    "target_value": "20",
                    "unit": "percent",
                }
            ],
            "novel_indicators": [],
        }
    )


def _constant_llm(response: str) -> Callable[..., str]:
    def respond(_prompt: str, **_kwargs: object) -> str:
        return response

    return respond


def test_exact_segment_receipt_is_current_and_exact_replay_is_idempotent(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "receipt.db")
    transcript_id, _ = _seed_transcript(path, segments=["first", "second"])
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        result = _zero_scan(conn, transcript_id)
        first = append_commitment_scan_receipt(
            conn,
            transcript_id=transcript_id,
            prompt_version="v1",
            observed_segments=result.observed_segments,
            recorded_at=datetime(2026, 9, 5, tzinfo=UTC),
        )
        conn.commit()
        replay = append_commitment_scan_receipt(
            conn,
            transcript_id=transcript_id,
            prompt_version="v1",
            observed_segments=result.observed_segments,
            recorded_at=datetime(2026, 9, 6, tzinfo=UTC),
        )
        conn.commit()
        current = current_commitment_scan_receipt(
            conn, transcript_id=transcript_id, prompt_version="v1"
        )
        count = conn.execute("SELECT COUNT(*) FROM commitment_scan_receipts").fetchone()[0]

    assert replay.receipt_id == first.receipt_id
    assert replay.recorded_at == first.recorded_at
    assert count == 1
    assert current is not None
    assert current.observed_segments == result.observed_segments
    assert all(item.disposition == "parsed_no_output" for item in current.observed_segments)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("text", "changed text"),
        ("seq", 7),
        ("speaker", "CFO"),
        ("time_code_start", "00:07:00"),
        ("time_code_end", "00:08:00"),
    ],
)
def test_segment_text_or_locator_change_invalidates_receipt(
    column: str,
    value: object,
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    path = migrated_db(tmp_path / "mutation.db")
    transcript_id, segment_ids = _seed_transcript(path, segments=["exact text"])
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        result = _zero_scan(conn, transcript_id)
        append_commitment_scan_receipt(
            conn,
            transcript_id=transcript_id,
            prompt_version="v1",
            observed_segments=result.observed_segments,
        )
        conn.commit()
        conn.execute("DROP TRIGGER trg_transcript_segments_immutable")
        conn.execute(f"UPDATE transcript_segments SET {column}=? WHERE id=?", (value, *segment_ids))
        assert (
            current_commitment_scan_receipt(conn, transcript_id=transcript_id, prompt_version="v1")
            is None
        )
        assert (
            commitment_scan_coverage(conn, transcript_id=transcript_id, prompt_version="v1").state
            is CommitmentScanCoverageState.INVALID_REAUDIT_REQUIRED
        )


@pytest.mark.parametrize("change", ["insert", "delete"])
def test_segment_set_change_invalidates_receipt(
    change: str, tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / f"segment-{change}.db")
    transcript_id, segment_ids = _seed_transcript(path, segments=["exact text"])
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        result = _zero_scan(conn, transcript_id)
        append_commitment_scan_receipt(
            conn,
            transcript_id=transcript_id,
            prompt_version="v1",
            observed_segments=result.observed_segments,
        )
        conn.commit()
        if change == "insert":
            conn.execute(
                "INSERT INTO transcript_segments "
                "(transcript_id,seq,speaker,time_code_start,time_code_end,text) "
                "VALUES (?,1,'CEO','00:01:00','00:02:00','new segment')",
                (transcript_id,),
            )
        else:
            conn.execute("DROP TRIGGER trg_transcript_segments_no_delete")
            conn.execute("DELETE FROM transcript_segments WHERE id=?", segment_ids)
        assert (
            current_commitment_scan_receipt(conn, transcript_id=transcript_id, prompt_version="v1")
            is None
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("segment_id", True),
        ("segment_id", "1"),
        ("sequence", 1.0),
        ("text_size_bytes", -1),
    ],
)
def test_observed_manifest_rejects_coercible_integer_tampering(field: str, value: object) -> None:
    source: dict[str, object] = {
        "period_end": "2026-06-30",
        "segment_id": 1,
        "sequence": 0,
        "source_document_id": 1,
        "speaker": None,
        "text_sha256": "a" * 64,
        "text_size_bytes": 4,
        "time_code_end": None,
        "time_code_start": None,
        "transcript_id": 1,
    }
    source[field] = value
    with pytest.raises(ValueError):
        _parse_observed_segments(
            {
                "schema_version": "commitment-segment-observations@1",
                "segments": [
                    {
                        "commitment_count": 0,
                        "disposition": "parsed_no_output",
                        "indicator_count": 0,
                        "source": source,
                    }
                ],
            }
        )


def test_pre_0037_writer_fails_without_scan_marker(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = migrated_db(tmp_path / "legacy-schema.db", target="0036_add_data_coverage_dispositions")
    transcript_id, _ = _seed_transcript(path, segments=["text"])
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        result = _zero_scan(conn, transcript_id)
        with pytest.raises(RuntimeError, match="segment manifest schema"):
            record_scan(
                conn,
                transcript_id,
                n_extracted=0,
                prompt_version="v1",
                observed_segments=result.observed_segments,
            )
        conn.rollback()
        assert conn.execute("SELECT COUNT(*) FROM commitment_scan_log").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM commitment_scan_receipts").fetchone()[0] == 0

        module = _load_script()

        def unexpected_call(*_args: object, **_kwargs: object) -> str:
            raise AssertionError("schema preflight must fail before a model call")

        monkeypatch.setattr(module, "call_llm", unexpected_call)
        report = module._run_auto(
            conn, ticker=None, transcript_id=transcript_id, max_n=0, dry_run=False
        )
        assert report["failed_targets"] == 1
        assert conn.execute("SELECT COUNT(*) FROM commitment_scan_log").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("target", "drop_scan_log"),
    [
        ("0035_add_report_kpi_reference_resolution_states", False),
        ("0035_add_report_kpi_reference_resolution_states", True),
        ("head", True),
    ],
)
def test_auto_requires_exact_receipt_and_scan_log_schema_before_model_call(
    target: str,
    drop_scan_log: bool,
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = migrated_db(tmp_path / f"schema-preflight-{target}-{drop_scan_log}.db", target=target)
    transcript_id, _ = _seed_transcript(path, segments=["text"])
    if drop_scan_log:
        with sqlite3.connect(path) as setup:
            setup.execute("DROP TABLE commitment_scan_log")
    module = _load_script()
    calls = 0

    def unexpected_call(*_args: object, **_kwargs: object) -> str:
        nonlocal calls
        calls += 1
        return _commitment_response("must not run")

    monkeypatch.setattr(module, "call_llm", unexpected_call)
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        report = module._run_auto(
            conn, ticker=None, transcript_id=transcript_id, max_n=0, dry_run=False
        )
        assert report["failed_targets"] == 1
        assert calls == 0
        assert conn.execute("SELECT COUNT(*) FROM management_commitments").fetchone()[0] == 0
        if not drop_scan_log:
            assert conn.execute("SELECT COUNT(*) FROM commitment_scan_log").fetchone()[0] == 0
        if target == "head":
            assert conn.execute("SELECT COUNT(*) FROM commitment_scan_receipts").fetchone()[0] == 0


@pytest.mark.parametrize(
    "drift",
    [
        "partial_receipt_columns",
        "partial_scan_log_columns",
        "missing_scan_log_unique_target",
        "partial_scan_log_unique_target",
        "missing_receipt_update_guard",
        "missing_receipt_exact_index",
        "wrong_receipt_exact_predicate",
    ],
)
def test_auto_rejects_partial_or_unguarded_writer_schema_before_model_call(
    drift: str,
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = migrated_db(tmp_path / f"writer-drift-{drift}.db")
    transcript_id, _ = _seed_transcript(path, segments=["text"])
    with sqlite3.connect(path) as setup:
        if drift == "partial_receipt_columns":
            setup.execute("DROP TABLE commitment_scan_receipts")
            setup.execute(
                "CREATE TABLE commitment_scan_receipts ("
                "receipt_id TEXT PRIMARY KEY,transcript_id INTEGER,document_id INTEGER,"
                "transcript_acquisition_receipt_id TEXT,transcript_sha256 TEXT,"
                "prompt_version TEXT,n_extracted INTEGER,output_manifest_sha256 TEXT,"
                "observed_segments_json TEXT,observed_segments_sha256 TEXT,"
                "observed_source_sha256 TEXT,recorded_at TEXT)"
            )
        elif drift in {
            "partial_scan_log_columns",
            "missing_scan_log_unique_target",
            "partial_scan_log_unique_target",
        }:
            setup.execute("DROP TABLE commitment_scan_log")
            id_column = "" if drift == "partial_scan_log_columns" else "id INTEGER PRIMARY KEY,"
            setup.execute(
                "CREATE TABLE commitment_scan_log ("
                + id_column
                + "transcript_id INTEGER NOT NULL,scanned_at TEXT NOT NULL,"
                "n_extracted INTEGER NOT NULL,prompt_version TEXT)"
            )
            if drift == "partial_scan_log_unique_target":
                setup.execute(
                    "CREATE UNIQUE INDEX partial_scan_log_target "
                    "ON commitment_scan_log(transcript_id) WHERE prompt_version IS NOT NULL"
                )
        elif drift == "missing_receipt_update_guard":
            setup.execute("DROP TRIGGER trg_commitment_scan_receipts_no_update")
        elif drift == "wrong_receipt_exact_predicate":
            setup.execute("DROP INDEX ux_commitment_scan_receipts_exact_scan")
            setup.execute(
                "CREATE UNIQUE INDEX ux_commitment_scan_receipts_exact_scan "
                "ON commitment_scan_receipts("
                "transcript_acquisition_receipt_id,prompt_version,observed_source_sha256) "
                "WHERE prompt_version IS NOT NULL"
            )
        else:
            setup.execute("DROP INDEX ux_commitment_scan_receipts_exact_scan")
        setup.commit()
    module = _load_script()
    calls = 0

    def unexpected_call(*_args: object, **_kwargs: object) -> str:
        nonlocal calls
        calls += 1
        return '{"commitments":[],"novel_indicators":[]}'

    monkeypatch.setattr(module, "call_llm", unexpected_call)
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        report = module._run_auto(
            conn, ticker=None, transcript_id=transcript_id, max_n=0, dry_run=False
        )
        assert report["failed_targets"] == 1
        assert calls == 0
        assert conn.execute("SELECT COUNT(*) FROM management_commitments").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM commitment_scan_log").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM commitment_scan_receipts").fetchone()[0] == 0


def test_migration_retains_legacy_receipt_but_does_not_admit_it_as_complete(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = migrated_db(tmp_path / "legacy-row.db", target="0036_add_data_coverage_dispositions")
    transcript_id, _ = _seed_transcript(path, segments=["text"])
    prompt_version = prompt_version_for("saydo_commitment_extract")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO tracked_companies (ticker,name,list_type,instrument_type,added_at) "
            "VALUES ('ACME','Acme','portfolio','etf','2026-01-01')"
        )
        conn.execute(
            "INSERT INTO kpi_definitions (ticker,name,unit,primary_source) "
            "VALUES ('ACME','Revenue','USD','transcript')"
        )
        binding = conn.execute(
            "SELECT t.document_id,d.sha256,r.receipt_id FROM transcripts t JOIN documents d "
            "ON d.id=t.document_id JOIN transcript_acquisition_receipts r ON r.document_id=d.id "
            "WHERE t.id=?",
            (transcript_id,),
        ).fetchone()
        conn.execute(
            "INSERT INTO commitment_scan_receipts VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "3" * 64,
                transcript_id,
                int(binding[0]),
                str(binding[2]),
                str(binding[1]),
                prompt_version,
                0,
                "[]",
                _sha("[]"),
                "2026-09-05T00:00:00Z",
            ),
        )
        conn.execute(
            "INSERT INTO commitment_scan_receipts VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "4" * 64,
                transcript_id,
                int(binding[0]),
                str(binding[2]),
                str(binding[1]),
                prompt_version,
                0,
                "[ ]",
                _sha("[ ]"),
                "2026-09-05T00:00:01Z",
            ),
        )
        conn.commit()

    migrated_db(path, target="head", upgrade_existing=True)
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT receipt_id,observed_segments_json FROM commitment_scan_receipts ORDER BY receipt_id"
        ).fetchall()
        legacy_manifest = '{"schema_version":"legacy-unobserved@0","segments":[]}'
        assert [tuple(row) for row in rows] == [
            ("3" * 64, legacy_manifest),
            ("4" * 64, legacy_manifest),
        ]
        assert (
            current_commitment_scan_receipt(
                conn, transcript_id=transcript_id, prompt_version=prompt_version
            )
            is None
        )
        assert (
            commitment_scan_coverage(
                conn, transcript_id=transcript_id, prompt_version=prompt_version
            ).state
            is CommitmentScanCoverageState.INVALID_REAUDIT_REQUIRED
        )
        assert transcripts_without_scan_receipt(conn) == []
        module = _load_script()
        calls = 0

        def unexpected_call(*_args: object, **_kwargs: object) -> str:
            nonlocal calls
            calls += 1
            return '{"commitments":[],"novel_indicators":[]}'

        monkeypatch.setattr(module, "call_llm", unexpected_call)
        assert (
            module._resolve_auto_targets(
                conn,
                ticker=None,
                transcript_id=None,
                max_n=0,
                rescan_unreceipted=False,
            )
            == []
        )
        report = module._run_auto(conn, ticker=None, transcript_id=None, max_n=0, dry_run=False)
        assert report["targets"] == 0
        assert calls == 0
        assert _load_onboard_script().find_pending_tickers(path) == []
    command.downgrade(_config(path), "0036_add_data_coverage_dispositions")
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM commitment_scan_receipts").fetchone()[0] == 2
        trigger_names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'trg_commitment_scan_receipts_%'"
            )
        }
        assert trigger_names == {
            "trg_commitment_scan_receipts_no_delete",
            "trg_commitment_scan_receipts_no_update",
        }


def test_valid_legacy_receipt_requires_reaudit_and_is_not_automatically_queued(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = migrated_db(tmp_path / "valid-legacy.db", target="0036_add_data_coverage_dispositions")
    transcript_id, _ = _seed_transcript(path, segments=["text"])
    prompt_version = prompt_version_for("saydo_commitment_extract")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO tracked_companies (ticker,name,list_type,instrument_type,added_at) "
            "VALUES ('ACME','Acme','portfolio','etf','2026-01-01')"
        )
        conn.execute(
            "INSERT INTO kpi_definitions (ticker,name,unit,primary_source) "
            "VALUES ('ACME','Revenue','USD','transcript')"
        )
        binding = conn.execute(
            "SELECT t.document_id,d.sha256,r.receipt_id FROM transcripts t JOIN documents d "
            "ON d.id=t.document_id JOIN transcript_acquisition_receipts r ON r.document_id=d.id "
            "WHERE t.id=?",
            (transcript_id,),
        ).fetchone()
        receipt_id = _legacy_zero_receipt_id(
            transcript_id=transcript_id,
            document_id=int(binding[0]),
            acquisition_receipt_id=str(binding[2]),
            transcript_sha256=str(binding[1]),
            prompt_version=prompt_version,
        )
        conn.execute(
            "INSERT INTO commitment_scan_receipts VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                receipt_id,
                transcript_id,
                int(binding[0]),
                str(binding[2]),
                str(binding[1]),
                prompt_version,
                0,
                "[]",
                _sha("[]"),
                "2026-09-05T00:00:00Z",
            ),
        )
        conn.commit()
    migrated_db(path, target="head", upgrade_existing=True)
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        coverage = commitment_scan_coverage(
            conn, transcript_id=transcript_id, prompt_version=prompt_version
        )
        assert coverage.state is CommitmentScanCoverageState.LEGACY_UNOBSERVED_REAUDIT_REQUIRED
        assert coverage.legacy_receipt_id == receipt_id
        assert (
            current_commitment_scan_receipt(
                conn, transcript_id=transcript_id, prompt_version=prompt_version
            )
            is None
        )
        assert transcripts_without_scan_receipt(conn) == []
        extract_module = _load_script()
        assert (
            extract_module._resolve_auto_targets(
                conn,
                ticker=None,
                transcript_id=None,
                max_n=0,
                rescan_unreceipted=False,
            )
            == []
        )
        assert extract_module._resolve_auto_targets(
            conn,
            ticker=None,
            transcript_id=transcript_id,
            max_n=0,
            rescan_unreceipted=False,
        ) == [(transcript_id, "ACME")]
        assert _load_onboard_script().find_pending_tickers(path) == []

    processed = tmp_path / "transcripts" / "processed" / "ACME_Q2_2026.txt"
    processed.parent.mkdir(parents=True)
    processed.write_text("text", encoding="utf-8")
    module = _load_backfill_script()

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        return connection

    def current_quarter(*_args: object) -> list[tuple[int, int]]:
        return [(2026, 2)]

    def persist_disposition(**kwargs: object) -> str:
        persisted.append(dict(kwargs))
        return "repair_evidence_missing"

    persisted: list[dict[str, object]] = []
    monkeypatch.setattr(module.db, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(module.db, "get_connection", connect)
    monkeypatch.setattr(module, "recent_fiscal_quarters", current_quarter)
    monkeypatch.setattr(module, "_persist_coverage_disposition", persist_disposition)
    result = module.TickerBackfillResult("ACME", 12)
    assert module._commitment_scan_targets([result], module.date(2026, 9, 5), 1) == []
    assert module._persist_commitment_scan_coverage(
        result,
        today=module.date(2026, 9, 5),
        lookback=1,
        extraction_attempted=False,
    )
    assert persisted[0]["status"] is module.CoverageDispositionStatus.REPAIR_EVIDENCE_MISSING
    assert persisted[0]["reason_code"] == "legacy_unobserved_reaudit_required"
    assert persisted[0]["retry_after"] is None
    assert persisted[0]["attempts"] == (
        module.CoverageAttempt(
            provider="legacy_commitment_scan",
            status=module.CoverageAttemptStatus.FAILED,
        ),
    )

    changed_text = "newly acquired source bytes"
    processed.write_text(changed_text, encoding="utf-8")
    changed_sha = _sha(changed_text)
    changed_key = "transcript:" + "9" * 64
    contract_sha = "2" * 64
    authorization = {
        "idempotency_key": changed_key,
        "request": {
            "canonical_ticker": "ACME",
            "document_type": "earnings_call_transcript",
            "fiscal_quarter": 2,
            "fiscal_year": 2026,
            "provider": "issuer_ir",
            "source_regime_identity": {"contract_sha256": contract_sha, "regime": "combined"},
            "source_type": "ir_doc",
        },
        "schema_version": "transcript-acquisition-authorization@1",
        "status": "authorized",
    }
    artifact = {
        "authorization": authorization,
        "canonical_document_path": "transcripts/raw/ACME_Q2_2026.txt",
        "document_id": 1,
        "schema_version": "authorized-transcript-artifact@1",
        "source_url": None,
        "staged": {"sha256": changed_sha, "size_bytes": len(changed_text.encode())},
    }
    artifact_json = _canonical(artifact)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER trg_transcript_documents_immutable")
        conn.execute(
            "UPDATE documents SET sha256=?,raw_bytes_size=? WHERE id=1",
            (
                changed_sha,
                len(changed_text.encode()),
            ),
        )
        conn.execute(
            "INSERT INTO transcript_acquisition_receipts "
            "(receipt_id,idempotency_key,document_id,canonical_ticker,fiscal_year,fiscal_quarter,"
            "canonical_document_path,artifact_sha256,artifact_size_bytes,source_url,provider,"
            "source_type,document_type,source_regime,source_regime_contract_sha256,"
            "authorization_json,artifact_json,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                _sha(artifact_json),
                changed_key,
                1,
                "ACME",
                2026,
                2,
                "transcripts/raw/ACME_Q2_2026.txt",
                changed_sha,
                len(changed_text.encode()),
                None,
                "issuer_ir",
                "ir_doc",
                "earnings_call_transcript",
                "combined",
                contract_sha,
                _canonical(authorization),
                artifact_json,
                "2026-09-06T00:00:00Z",
            ),
        )
        conn.commit()
        conn.row_factory = sqlite3.Row
        assert (
            commitment_scan_coverage(
                conn, transcript_id=transcript_id, prompt_version=prompt_version
            ).state
            is CommitmentScanCoverageState.SOURCE_CHANGED_MISSING
        )
        assert [item[0] for item in transcripts_without_scan_receipt(conn)] == [transcript_id]
    assert module._commitment_scan_targets(
        [module.TickerBackfillResult("ACME", 12)], module.date(2026, 9, 6), 1
    ) == [module.CommitmentScanTarget("ACME", 12, 2026, 2, transcript_id)]
    assert _load_onboard_script().find_pending_tickers(path) == [("ACME", "no_commitments")]


@pytest.mark.parametrize("corruption", ["missing", "tampered"])
def test_unsealed_legacy_acquisition_is_invalid_and_excluded_from_automatic_queues(
    corruption: str,
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = migrated_db(
        tmp_path / f"unsealed-legacy-{corruption}.db",
        target="0036_add_data_coverage_dispositions",
    )
    transcript_id, _ = _seed_transcript(path, segments=["old source text"])
    prompt_version = prompt_version_for("saydo_commitment_extract")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO tracked_companies (ticker,name,list_type,instrument_type,added_at) "
            "VALUES ('ACME','Acme','portfolio','etf','2026-01-01')"
        )
        binding = conn.execute(
            "SELECT t.document_id,d.sha256,r.receipt_id FROM transcripts t JOIN documents d "
            "ON d.id=t.document_id JOIN transcript_acquisition_receipts r ON r.document_id=d.id "
            "WHERE t.id=?",
            (transcript_id,),
        ).fetchone()
        document_id = int(binding[0])
        old_acquisition_receipt_id = str(binding[2])
        receipt_id = _legacy_zero_receipt_id(
            transcript_id=transcript_id,
            document_id=document_id,
            acquisition_receipt_id=old_acquisition_receipt_id,
            transcript_sha256=str(binding[1]),
            prompt_version=prompt_version,
        )
        conn.execute(
            "INSERT INTO commitment_scan_receipts VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                receipt_id,
                transcript_id,
                document_id,
                old_acquisition_receipt_id,
                str(binding[1]),
                prompt_version,
                0,
                "[]",
                _sha("[]"),
                "2026-09-05T00:00:00Z",
            ),
        )
        conn.commit()
    migrated_db(path, target="head", upgrade_existing=True)

    changed_text = "new authorized source text"
    processed = tmp_path / "transcripts" / "processed" / "ACME_Q2_2026.txt"
    processed.parent.mkdir(parents=True)
    processed.write_text(changed_text, encoding="utf-8")
    with sqlite3.connect(path) as conn:
        _add_changed_acquisition(
            conn,
            document_id=document_id,
            changed_text=changed_text,
            key_digit="7",
        )
        if corruption == "missing":
            conn.execute("DROP TRIGGER trg_transcript_acquisition_receipts_no_delete")
            conn.execute(
                "DELETE FROM transcript_acquisition_receipts WHERE receipt_id=?",
                (old_acquisition_receipt_id,),
            )
        else:
            conn.execute("DROP TRIGGER trg_transcript_acquisition_receipts_no_update")
            old_artifact = json.loads(
                str(
                    conn.execute(
                        "SELECT artifact_json FROM transcript_acquisition_receipts "
                        "WHERE receipt_id=?",
                        (old_acquisition_receipt_id,),
                    ).fetchone()[0]
                )
            )
            old_artifact["tampered"] = True
            conn.execute(
                "UPDATE transcript_acquisition_receipts SET artifact_json=? WHERE receipt_id=?",
                (_canonical(old_artifact), old_acquisition_receipt_id),
            )
        conn.commit()
        conn.row_factory = sqlite3.Row
        coverage = commitment_scan_coverage(
            conn, transcript_id=transcript_id, prompt_version=prompt_version
        )
        assert coverage.state is CommitmentScanCoverageState.INVALID_REAUDIT_REQUIRED
        assert transcripts_without_scan_receipt(conn) == []

        extract_module = _load_script()
        calls = 0

        def unexpected_call(*_args: object, **_kwargs: object) -> str:
            nonlocal calls
            calls += 1
            return '{"commitments":[],"novel_indicators":[]}'

        monkeypatch.setattr(extract_module, "call_llm", unexpected_call)
        report = extract_module._run_auto(
            conn, ticker=None, transcript_id=None, max_n=0, dry_run=False
        )
        assert report["targets"] == 0
        assert calls == 0
    assert _load_onboard_script().find_pending_tickers(path) == []

    backfill_module = _load_backfill_script()

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        return connection

    def current_quarter(*_args: object) -> list[tuple[int, int]]:
        return [(2026, 2)]

    monkeypatch.setattr(backfill_module.db, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(backfill_module.db, "get_connection", connect)
    monkeypatch.setattr(backfill_module, "recent_fiscal_quarters", current_quarter)
    assert (
        backfill_module._commitment_scan_targets(
            [backfill_module.TickerBackfillResult("ACME", 12)],
            backfill_module.date(2026, 9, 6),
            1,
        )
        == []
    )


def test_valid_prior_v2_receipt_with_new_acquisition_is_pending_for_auto_and_backfill(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = migrated_db(tmp_path / "v2-source-change.db")
    transcript_id, segment_ids = _seed_transcript(path, segments=["old source text"])
    prompt_version = prompt_version_for("saydo_commitment_extract")
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        old_scan = _zero_scan(conn, transcript_id)
        old_receipt = append_commitment_scan_receipt(
            conn,
            transcript_id=transcript_id,
            prompt_version=prompt_version,
            observed_segments=old_scan.observed_segments,
        )
        conn.commit()
        document_id = old_receipt.binding.document_id

        changed_text = "new authorized source text"
        changed_sha = _sha(changed_text)
        changed_key = "transcript:" + "8" * 64
        contract_sha = "2" * 64
        authorization = {
            "idempotency_key": changed_key,
            "request": {
                "canonical_ticker": "ACME",
                "document_type": "earnings_call_transcript",
                "fiscal_quarter": 2,
                "fiscal_year": 2026,
                "provider": "issuer_ir",
                "source_regime_identity": {
                    "contract_sha256": contract_sha,
                    "regime": "combined",
                },
                "source_type": "ir_doc",
            },
            "schema_version": "transcript-acquisition-authorization@1",
            "status": "authorized",
        }
        artifact = {
            "authorization": authorization,
            "canonical_document_path": "transcripts/raw/ACME_Q2_2026.txt",
            "document_id": document_id,
            "schema_version": "authorized-transcript-artifact@1",
            "source_url": None,
            "staged": {"sha256": changed_sha, "size_bytes": len(changed_text.encode())},
        }
        artifact_json = _canonical(artifact)
        conn.execute("DROP TRIGGER trg_transcript_documents_immutable")
        conn.execute("DROP TRIGGER trg_transcript_segments_immutable")
        conn.execute(
            "UPDATE documents SET sha256=?,raw_bytes_size=? WHERE id=?",
            (changed_sha, len(changed_text.encode()), document_id),
        )
        conn.execute(
            "UPDATE transcript_segments SET text=? WHERE id=?",
            (changed_text, segment_ids[0]),
        )
        conn.execute(
            "INSERT INTO transcript_acquisition_receipts "
            "(receipt_id,idempotency_key,document_id,canonical_ticker,fiscal_year,fiscal_quarter,"
            "canonical_document_path,artifact_sha256,artifact_size_bytes,source_url,provider,"
            "source_type,document_type,source_regime,source_regime_contract_sha256,"
            "authorization_json,artifact_json,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                _sha(artifact_json),
                changed_key,
                document_id,
                "ACME",
                2026,
                2,
                "transcripts/raw/ACME_Q2_2026.txt",
                changed_sha,
                len(changed_text.encode()),
                None,
                "issuer_ir",
                "ir_doc",
                "earnings_call_transcript",
                "combined",
                contract_sha,
                _canonical(authorization),
                artifact_json,
                "2026-09-06T00:00:00Z",
            ),
        )
        conn.commit()
        coverage = commitment_scan_coverage(
            conn, transcript_id=transcript_id, prompt_version=prompt_version
        )
        assert coverage.state is CommitmentScanCoverageState.SOURCE_CHANGED_MISSING

        extract_module = _load_script()
        assert extract_module._resolve_auto_targets(
            conn,
            ticker=None,
            transcript_id=None,
            max_n=0,
            rescan_unreceipted=False,
        ) == [(transcript_id, "ACME")]

    processed = tmp_path / "transcripts" / "processed" / "ACME_Q2_2026.txt"
    processed.parent.mkdir(parents=True)
    processed.write_text(changed_text, encoding="utf-8")
    backfill_module = _load_backfill_script()

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        return connection

    def current_quarter(*_args: object) -> list[tuple[int, int]]:
        return [(2026, 2)]

    monkeypatch.setattr(backfill_module.db, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(backfill_module.db, "get_connection", connect)
    monkeypatch.setattr(backfill_module, "recent_fiscal_quarters", current_quarter)
    assert backfill_module._commitment_scan_targets(
        [backfill_module.TickerBackfillResult("ACME", 12)],
        backfill_module.date(2026, 9, 6),
        1,
    ) == [backfill_module.CommitmentScanTarget("ACME", 12, 2026, 2, transcript_id)]

    calls = 0

    def zero_response(*_args: object, **_kwargs: object) -> str:
        nonlocal calls
        calls += 1
        return '{"commitments":[],"novel_indicators":[]}'

    monkeypatch.setattr(extract_module, "call_llm", zero_response)
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        report = extract_module._run_auto(
            conn, ticker=None, transcript_id=None, max_n=0, dry_run=False
        )
        assert report["failed_targets"] == 0
        assert calls == 1
        assert conn.execute("SELECT COUNT(*) FROM commitment_scan_receipts").fetchone()[0] == 2


def test_downgrade_refuses_after_exact_segment_receipt(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "downgrade-loss.db")
    transcript_id, _ = _seed_transcript(path, segments=["text"])
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        result = _zero_scan(conn, transcript_id)
        append_commitment_scan_receipt(
            conn,
            transcript_id=transcript_id,
            prompt_version="v1",
            observed_segments=result.observed_segments,
        )
        conn.commit()
    with pytest.raises(RuntimeError, match="cannot downgrade"):
        command.downgrade(_config(path), "0036_add_data_coverage_dispositions")


def test_missing_or_incomplete_extraction_coverage_cannot_be_sealed(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "missing-coverage.db")
    transcript_id, _ = _seed_transcript(path, segments=["first", "second"])
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        result = _zero_scan(conn, transcript_id)
        with pytest.raises(ValueError, match="current selected segment bytes"):
            append_commitment_scan_receipt(
                conn,
                transcript_id=transcript_id,
                prompt_version="v1",
                observed_segments=result.observed_segments[:1],
            )
        conn.rollback()
        with pytest.raises(ValueError, match="typed observed"):
            append_commitment_scan_receipt(
                conn,
                transcript_id=transcript_id,
                prompt_version="v1",
                observed_segments=None,
            )


def test_receipt_rejects_reordered_duplicate_missing_extra_and_content_tampering(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path = migrated_db(tmp_path / "manifest-tampering.db")
    transcript_id, _ = _seed_transcript(path, segments=["first", "second"])
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        result = _zero_scan(conn, transcript_id)
        receipt = append_commitment_scan_receipt(
            conn,
            transcript_id=transcript_id,
            prompt_version="v1",
            observed_segments=result.observed_segments,
        )
        conn.commit()
        original = json.loads(receipt.output_manifest_json or "[]")
        assert original == []
        stored_manifest = json.loads(
            conn.execute("SELECT observed_segments_json FROM commitment_scan_receipts").fetchone()[
                0
            ]
        )
        stored = stored_manifest["segments"]
        variants = [
            list(reversed(stored)),
            [stored[0], stored[0]],
            stored[:1],
            [*stored, stored[0]],
            [
                {
                    **stored[0],
                    "source": {**stored[0]["source"], "text_sha256": "b" * 64},
                },
                stored[1],
            ],
        ]
        conn.execute("DROP TRIGGER trg_commitment_scan_receipts_no_update")
        for variant in variants:
            manifest_json = _canonical(
                {"schema_version": "commitment-segment-observations@1", "segments": variant}
            )
            conn.execute(
                "UPDATE commitment_scan_receipts SET observed_segments_json=?,"
                "observed_segments_sha256=? WHERE receipt_id=?",
                (manifest_json, _sha(manifest_json), receipt.receipt_id),
            )
            assert (
                current_commitment_scan_receipt(
                    conn, transcript_id=transcript_id, prompt_version="v1"
                )
                is None
            )


def test_source_mutation_during_model_call_rejects_outputs_and_completion(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = migrated_db(tmp_path / "call-mutation.db")
    transcript_id, segment_ids = _seed_transcript(path, segments=["first", "second"])
    _seed_kpi(path)
    module = _load_script()
    calls = 0

    def mutate_between_calls(_prompt: str, **_kwargs: object) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            with sqlite3.connect(path) as writer:
                writer.execute("DROP TRIGGER trg_transcript_segments_immutable")
                writer.execute(
                    "UPDATE transcript_segments SET text='changed' WHERE id=?", (segment_ids[1],)
                )
                writer.commit()
        return _commitment_response(f"commitment {calls}")

    monkeypatch.setattr(module, "call_llm", mutate_between_calls)
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        report = module._run_auto(
            conn, ticker=None, transcript_id=transcript_id, max_n=0, dry_run=False
        )
        assert report["failed_targets"] == 1
        assert conn.execute("SELECT COUNT(*) FROM management_commitments").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM commitment_scan_log").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM commitment_scan_receipts").fetchone()[0] == 0


def test_source_mutation_during_persistence_rolls_back_outputs_marker_and_receipt(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = migrated_db(tmp_path / "persist-mutation.db")
    transcript_id, segment_ids = _seed_transcript(path, segments=["source text"])
    _seed_kpi(path)
    module = _load_script()
    monkeypatch.setattr(module, "call_llm", _constant_llm(_commitment_response("commitment")))
    original = module._persist_commitment_idempotently

    def mutate_after_staging(conn: sqlite3.Connection, commitment: CommitmentInput) -> int:
        item_id = original(conn, commitment)
        conn.execute("DROP TRIGGER trg_transcript_segments_immutable")
        conn.execute("UPDATE transcript_segments SET text='changed' WHERE id=?", segment_ids)
        return item_id

    monkeypatch.setattr(module, "_persist_commitment_idempotently", mutate_after_staging)
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        report = module._run_auto(
            conn, ticker=None, transcript_id=transcript_id, max_n=0, dry_run=False
        )
        assert report["failed_targets"] == 1
        assert conn.execute("SELECT COUNT(*) FROM management_commitments").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM commitment_scan_log").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM commitment_scan_receipts").fetchone()[0] == 0
        assert conn.execute("SELECT text FROM transcript_segments").fetchone()[0] == "source text"


def test_write_lock_blocks_second_connection_source_mutation(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = migrated_db(tmp_path / "write-lock.db")
    transcript_id, segment_ids = _seed_transcript(path, segments=["source text"])
    _seed_kpi(path)
    module = _load_script()
    monkeypatch.setattr(module, "call_llm", _constant_llm(_commitment_response("commitment")))
    original = module._persist_commitment_idempotently
    lock_error: list[str] = []
    with sqlite3.connect(path) as setup:
        setup.execute("DROP TRIGGER trg_transcript_segments_immutable")
        setup.commit()

    def contend_after_staging(conn: sqlite3.Connection, commitment: CommitmentInput) -> int:
        item_id = original(conn, commitment)
        with sqlite3.connect(path, timeout=0) as contender:
            try:
                contender.execute(
                    "UPDATE transcript_segments SET text='changed' WHERE id=?", segment_ids
                )
            except sqlite3.OperationalError as exc:
                lock_error.append(str(exc))
        return item_id

    monkeypatch.setattr(module, "_persist_commitment_idempotently", contend_after_staging)
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        report = module._run_auto(
            conn, ticker=None, transcript_id=transcript_id, max_n=0, dry_run=False
        )
        assert report["failed_targets"] == 0
        assert conn.execute("SELECT COUNT(*) FROM commitment_scan_receipts").fetchone()[0] == 1
    assert lock_error and "locked" in lock_error[0]


def test_receipt_insert_failure_after_output_and_scan_log_staging_rolls_back_all(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = migrated_db(tmp_path / "receipt-failure.db")
    transcript_id, _ = _seed_transcript(path, segments=["source text"])
    _seed_kpi(path)
    with sqlite3.connect(path) as setup:
        setup.execute(
            "CREATE TRIGGER reject_commitment_scan_receipt BEFORE INSERT "
            "ON commitment_scan_receipts BEGIN SELECT RAISE(ABORT,'forced receipt failure'); END"
        )
        setup.commit()
    module = _load_script()
    monkeypatch.setattr(module, "call_llm", _constant_llm(_commitment_response("commitment")))
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        report = module._run_auto(
            conn, ticker=None, transcript_id=transcript_id, max_n=0, dry_run=False
        )
        assert report["failed_targets"] == 1
        assert conn.execute("SELECT COUNT(*) FROM management_commitments").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM commitment_scan_log").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM commitment_scan_receipts").fetchone()[0] == 0


@pytest.mark.parametrize("response", ['{"commitments":[]}', '{"novel_indicators":[]}'])
def test_missing_output_category_cannot_persist_zero_scan_completion(
    response: str,
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = migrated_db(tmp_path / "partial-zero.db")
    transcript_id, _ = _seed_transcript(path, segments=["source text"])
    module = _load_script()
    calls = 0

    def partial_response(_prompt: str, **_kwargs: object) -> str:
        nonlocal calls
        calls += 1
        return response

    monkeypatch.setattr(module, "call_llm", partial_response)
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        report = module._run_auto(
            conn, ticker=None, transcript_id=transcript_id, max_n=0, dry_run=False
        )
        assert report["failed_targets"] == 1
        assert calls == 2
        assert conn.execute("SELECT COUNT(*) FROM management_commitments").fetchone()[0] == 0
        assert (
            conn.execute("SELECT COUNT(*) FROM management_indicator_observations").fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM commitment_scan_log").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM commitment_scan_receipts").fetchone()[0] == 0


def test_commitment_failure_after_one_staged_output_rolls_back_all(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = migrated_db(tmp_path / "commitment-failure.db")
    transcript_id, _ = _seed_transcript(path, segments=["source text"])
    _seed_kpi(path)
    module = _load_script()
    payload = json.loads(_commitment_response("first"))
    second = dict(payload["commitments"][0])
    second["narrative"] = "second"
    payload["commitments"].append(second)
    monkeypatch.setattr(module, "call_llm", _constant_llm(_canonical(payload)))
    original = module._persist_commitment_idempotently
    calls = 0

    def fail_second(conn: sqlite3.Connection, commitment: CommitmentInput) -> int:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.OperationalError("forced commitment failure")
        return original(conn, commitment)

    monkeypatch.setattr(module, "_persist_commitment_idempotently", fail_second)
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        report = module._run_auto(
            conn, ticker=None, transcript_id=transcript_id, max_n=0, dry_run=False
        )
        assert report["failed_targets"] == 1
        assert conn.execute("SELECT COUNT(*) FROM management_commitments").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM commitment_scan_log").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM commitment_scan_receipts").fetchone()[0] == 0


def test_indicator_failure_after_one_staged_output_rolls_back_all(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = migrated_db(tmp_path / "indicator-failure.db")
    transcript_id, _ = _seed_transcript(path, segments=["We launched 42 pilots."])
    module = _load_script()
    indicator = {
        "raw_label": "Pilots",
        "recurrence": "one_off",
        "scope": "product",
        "source_excerpt": "We launched 42 pilots.",
        "unit": "count",
        "value": "42",
    }
    response = _canonical(
        {"commitments": [], "novel_indicators": [indicator, {**indicator, "raw_label": "Deals"}]}
    )
    monkeypatch.setattr(module, "call_llm", _constant_llm(response))

    def fail_after_one(
        conn: sqlite3.Connection, manifest: ManagementIndicatorExtractionManifest
    ) -> list[int]:
        first = manifest.indicators[0]
        persist_indicator(conn, indicator=first)
        raise sqlite3.OperationalError("forced indicator failure")

    monkeypatch.setattr(module, "persist_indicators", fail_after_one)
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        report = module._run_auto(
            conn, ticker=None, transcript_id=transcript_id, max_n=0, dry_run=False
        )
        assert report["failed_targets"] == 1
        assert (
            conn.execute("SELECT COUNT(*) FROM management_indicator_observations").fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM commitment_scan_log").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM commitment_scan_receipts").fetchone()[0] == 0


@pytest.mark.parametrize("hard_stop", [LLMBudgetExceeded("budget"), LLMSetupError("auth")])
def test_later_segment_budget_or_setup_hard_stop_propagates_without_writes(
    hard_stop: Exception,
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = migrated_db(tmp_path / f"hard-stop-{type(hard_stop).__name__}.db")
    transcript_id, _ = _seed_transcript(path, segments=["first", "second"])
    module = _load_script()
    calls = 0

    def fail_second(_prompt: str, **_kwargs: object) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise hard_stop
        return '{"commitments":[],"novel_indicators":[]}'

    monkeypatch.setattr(module, "call_llm", fail_second)
    with sqlite3.connect(path) as conn, pytest.raises(type(hard_stop)):
        conn.row_factory = sqlite3.Row
        module._run_auto(conn, ticker=None, transcript_id=transcript_id, max_n=0, dry_run=False)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM commitment_scan_log").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM commitment_scan_receipts").fetchone()[0] == 0


def test_exact_auto_replay_is_idempotent_and_conflicting_replay_is_rejected(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = migrated_db(tmp_path / "auto-replay.db")
    transcript_id, _ = _seed_transcript(path, segments=["source text"])
    _seed_kpi(path)
    module = _load_script()
    response = _commitment_response("first commitment")
    monkeypatch.setattr(module, "call_llm", _constant_llm(response))
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        first = module._run_auto(
            conn, ticker=None, transcript_id=transcript_id, max_n=0, dry_run=False
        )
        replay = module._run_auto(
            conn, ticker=None, transcript_id=transcript_id, max_n=0, dry_run=False
        )
        assert first["failed_targets"] == replay["failed_targets"] == 0
        assert conn.execute("SELECT COUNT(*) FROM management_commitments").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM commitment_scan_receipts").fetchone()[0] == 1

        conflicting_payload = json.loads(_commitment_response("conflicting commitment"))
        second_commitment = dict(conflicting_payload["commitments"][0])
        second_commitment["narrative"] = "second conflicting commitment"
        conflicting_payload["commitments"].append(second_commitment)
        monkeypatch.setattr(module, "call_llm", _constant_llm(_canonical(conflicting_payload)))
        conflict = module._run_auto(
            conn, ticker=None, transcript_id=transcript_id, max_n=0, dry_run=False
        )
        assert conflict["failed_targets"] == 1
        assert "already sealed" in str(conflict["results"])
        assert conn.execute("SELECT COUNT(*) FROM management_commitments").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM commitment_scan_receipts").fetchone()[0] == 1
