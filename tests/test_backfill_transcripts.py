"""Tests for execution/backfill_transcripts.py — subprocess phase targeting.

Focuses on the worktree-vs-main-repo path bug: when the script is run from a
worktree with `--repo-root <main>`, the `_run_ingest` and `_run_extract`
subprocesses must invoke current code while binding mutable state and cwd to
the state checkout.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Never

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_module() -> Any:
    src = PROJECT_ROOT / "execution" / "backfill_transcripts.py"
    spec = importlib.util.spec_from_file_location("backfill_transcripts", src)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["backfill_transcripts"] = mod
    spec.loader.exec_module(mod)
    return mod


def _q2_2026(*_args: object) -> list[tuple[int, int]]:
    return [(2026, 2)]


def _always_true(*_args: object) -> bool:
    return True


def _always_false(*_args: object) -> bool:
    return False


class _FakeProc:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode


def test_run_ingest_uses_repo_root_for_cwd_and_script_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_module()
    repo_root = tmp_path / "main-repo"
    repo_root.mkdir()

    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> _FakeProc:
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeProc(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    rc = mod._run_ingest(repo_root, "AAPL", dry_run=False)
    assert rc == 0
    assert captured["kwargs"]["cwd"] == str(repo_root)
    assert captured["cmd"][1] == str(PROJECT_ROOT / "execution" / "sqlite_bootstrap.py")
    assert captured["cmd"][2] == str(PROJECT_ROOT / "execution" / "ingest_transcripts_state.py")
    assert captured["cmd"][captured["cmd"].index("--repo-root") + 1] == str(repo_root)
    assert captured["cmd"][captured["cmd"].index("--ticker") + 1] == "AAPL"
    assert "--no-promote" not in captured["cmd"]


def test_run_extract_uses_repo_root_for_cwd_and_script_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_module()
    repo_root = tmp_path / "main-repo"
    repo_root.mkdir()

    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> _FakeProc:
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeProc(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    rc = mod._run_extract(repo_root, "AAPL", dry_run=False)
    assert rc == 0
    assert captured["kwargs"]["cwd"] == str(repo_root)
    assert captured["cmd"][1] == str(PROJECT_ROOT / "execution" / "sqlite_bootstrap.py")
    assert captured["cmd"][2] == str(
        PROJECT_ROOT / "execution" / "extract_commitments_from_transcript.py"
    )
    assert "--auto" in captured["cmd"]
    assert "--rescan-unreceipted" in captured["cmd"]
    assert "AAPL" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--db") + 1] == str(
        repo_root / "data" / "portfolio.db"
    )


def test_run_ingest_dry_run_skips_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_module()

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("subprocess.run must not be called in dry-run")

    monkeypatch.setattr(mod.subprocess, "run", boom)
    assert mod._run_ingest(Path("/nonexistent"), "AAPL", dry_run=True) == 0


def test_run_extract_dry_run_skips_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_module()

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("subprocess.run must not be called in dry-run")

    monkeypatch.setattr(mod.subprocess, "run", boom)
    assert mod._run_extract(Path("/nonexistent"), "AAPL", dry_run=True) == 0


def test_commitment_extraction_is_limited_to_newly_ingested_tickers() -> None:
    mod = _load_module()
    results = [
        mod.TickerBackfillResult("AAPL", 9, fetched=["Q2_2026"]),
        mod.TickerBackfillResult("MSFT", 6, skipped_existing=["Q2_2026"]),
        mod.TickerBackfillResult("NVDA", 1, aggregator_misses=["Q2_2026"]),
    ]

    assert mod._newly_ingested_tickers(
        results,
        ingest_results=[{"ticker": "AAPL", "rc": 0}],
    ) == ["AAPL"]
    assert (
        mod._newly_ingested_tickers(
            results,
            ingest_results=[{"ticker": "AAPL", "rc": 1}],
        )
        == []
    )


def test_commitment_extraction_continues_for_successful_tickers_after_peer_failure() -> None:
    mod = _load_module()
    results = [
        mod.TickerBackfillResult("AAPL", 9, fetched=["Q2_2026"]),
        mod.TickerBackfillResult("MSFT", 6, fetched=["Q2_2026"]),
    ]

    assert mod._newly_ingested_tickers(
        results,
        ingest_results=[
            {"ticker": "AAPL", "rc": 0},
            {"ticker": "MSFT", "rc": 2},
        ],
    ) == ["AAPL"]


def test_no_new_fetches_produce_no_commitment_extraction_scope() -> None:
    mod = _load_module()
    results = [
        mod.TickerBackfillResult("AAPL", 9, skipped_existing=["Q2_2026"]),
        mod.TickerBackfillResult("MSFT", 6, aggregator_misses=["Q2_2026"]),
    ]

    assert mod._newly_ingested_tickers(results, ingest_results=[]) == []


def test_unreceipted_local_file_is_reacquired_before_ingest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_module()
    calls: list[tuple[object, dict[str, object]]] = []

    def fake_recent_fiscal_quarters(*_args: object) -> list[tuple[int, int]]:
        return [(2026, 2)]

    def fake_has_ingested_evidence(*_args: object) -> bool:
        return False

    def fake_fetch_qa(spec: object, **kwargs: object) -> SimpleNamespace:
        calls.append((spec, kwargs))
        return SimpleNamespace(
            status=mod.FetchQaStatus.ACQUIRED,
            idempotency_key="transcript:" + "a" * 64,
            attempts=(),
            result=SimpleNamespace(acquired_artifact=SimpleNamespace(sha256="b" * 64)),
        )

    monkeypatch.setattr(mod, "recent_fiscal_quarters", fake_recent_fiscal_quarters)
    monkeypatch.setattr(mod, "_has_ingested_evidence", fake_has_ingested_evidence)
    monkeypatch.setattr(mod, "fetch_qa", fake_fetch_qa)
    def persist_satisfied(**_kwargs: object) -> str:
        return "satisfied"

    monkeypatch.setattr(mod, "_persist_coverage_disposition", persist_satisfied)

    result = mod._backfill_one(
        "NU",
        12,
        1,
        mod.date(2026, 9, 4),
        False,
        tmp_path / "portfolio.db",
        False,
    )

    assert result.fetched == ["Q2_2026"]
    assert len(calls) == 1


def test_dry_run_existing_evidence_performs_zero_disposition_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_module()
    monkeypatch.setattr(mod, "recent_fiscal_quarters", _q2_2026)
    monkeypatch.setattr(mod, "_has_ingested_evidence", _always_true)

    def unexpected_disposition(**_kwargs: object) -> Never:
        pytest.fail("dry-run crossed the disposition write boundary")

    def unexpected_fetch(*_args: object, **_kwargs: object) -> Never:
        pytest.fail("dry-run crossed the acquisition boundary")

    monkeypatch.setattr(
        mod,
        "_persist_coverage_disposition",
        unexpected_disposition,
    )
    monkeypatch.setattr(
        mod,
        "fetch_qa",
        unexpected_fetch,
    )

    result = mod._backfill_one(
        "MELI",
        12,
        1,
        mod.date(2026, 9, 5),
        True,
        tmp_path / "portfolio.db",
        False,
    )

    assert result.skipped_existing == ["Q2_2026"]
    assert result.coverage_dispositions == []


@pytest.mark.parametrize(
    ("canonical_row_exists", "expected_status"),
    [
        (False, "source_unavailable"),
        (True, "repair_evidence_missing"),
    ],
)
def test_provider_miss_persists_exact_non_complete_disposition_without_failing_run(
    canonical_row_exists: bool,
    expected_status: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_module()
    persisted: list[Any] = []
    outcome = SimpleNamespace(
        status=(
            mod.FetchQaStatus.DENIED if canonical_row_exists else mod.FetchQaStatus.PROVIDER_MISS
        ),
        idempotency_key="transcript:" + "a" * 64,
        attempts=(
            SimpleNamespace(
                provider="issuer_ir",
                status=mod.FetchQaAttemptStatus.PROVIDER_MISS,
                idempotency_key="transcript:" + "b" * 64,
            ),
            SimpleNamespace(
                provider="roic_ai",
                status=mod.FetchQaAttemptStatus.DENIED,
                idempotency_key="transcript:" + "c" * 64,
            ),
        ),
        result=None,
    )

    def transcript_rows_exist(*_args: object) -> bool:
        return canonical_row_exists

    def fetch_outcome(*_args: object, **_kwargs: object) -> object:
        return outcome

    monkeypatch.setattr(mod, "recent_fiscal_quarters", _q2_2026)
    monkeypatch.setattr(mod, "_has_ingested_evidence", _always_false)
    monkeypatch.setattr(mod, "_transcript_rows_exist", transcript_rows_exist)
    monkeypatch.setattr(mod, "fetch_qa", fetch_outcome)

    def persist(**kwargs: object) -> str:
        persisted.append(kwargs["status"])
        return str(persisted[-1].value)

    monkeypatch.setattr(mod, "_persist_coverage_disposition", persist)
    result = mod._backfill_one(
        "NVO" if not canonical_row_exists else "NOW",
        12,
        1,
        mod.date(2026, 9, 4),
        False,
        tmp_path / "portfolio.db",
        False,
    )

    assert result.errors == []
    assert result.aggregator_misses == ([] if canonical_row_exists else ["Q2_2026"])
    assert result.coverage_dispositions == [f"Q2_2026:{expected_status}"]
    assert [item.value for item in persisted] == [expected_status]


def test_existing_unscanned_transcript_is_selected_even_without_new_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_module()
    result = mod.TickerBackfillResult("BKNG", 12, skipped_existing=["Q2_2026"])
    def exact_evidence(*_args: object) -> object:
        return mod.TranscriptEvidence("transcript-receipt:exact", "a" * 64)

    def no_scan_evidence(*_args: object) -> None:
        return None

    monkeypatch.setattr(mod, "recent_fiscal_quarters", _q2_2026)
    monkeypatch.setattr(mod, "_transcript_rows_exist", _always_true)
    monkeypatch.setattr(
        mod,
        "_ingested_evidence",
        exact_evidence,
    )
    monkeypatch.setattr(mod, "_commitment_scan_evidence", no_scan_evidence)

    assert mod._tickers_requiring_commitment_scan([result], mod.date(2026, 9, 5), 1) == ["BKNG"]


@pytest.mark.parametrize(
    ("transcript_exists", "exact_transcript", "evidence", "expected", "closed"),
    [
        (False, False, None, "source_unavailable", True),
        (
            True,
            True,
            ("commitment-scan:42", "d" * 64),
            "satisfied",
            True,
        ),
        (True, True, None, "operational_error", False),
        (True, False, None, "repair_evidence_missing", True),
    ],
)
def test_commitment_scan_disposition_preserves_missing_prerequisite_and_failures(
    transcript_exists: bool,
    exact_transcript: bool,
    evidence: tuple[str, str] | None,
    expected: str,
    closed: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_module()
    persisted: list[dict[str, Any]] = []
    transcript_evidence = (
        None if evidence is None else mod.TranscriptEvidence(evidence[0], evidence[1])
    )
    def transcript_rows_exist(*_args: object) -> bool:
        return transcript_exists

    def current_evidence(*_args: object) -> object | None:
        return (
            mod.TranscriptEvidence("transcript-receipt:exact", "a" * 64)
            if exact_transcript
            else None
        )

    def scan_evidence(*_args: object) -> object | None:
        return transcript_evidence

    monkeypatch.setattr(mod, "recent_fiscal_quarters", _q2_2026)
    monkeypatch.setattr(mod, "_transcript_rows_exist", transcript_rows_exist)
    monkeypatch.setattr(
        mod,
        "_ingested_evidence",
        current_evidence,
    )
    monkeypatch.setattr(mod, "_commitment_scan_evidence", scan_evidence)

    def persist(**kwargs: Any) -> str:
        persisted.append(kwargs)
        return str(kwargs["status"].value)

    monkeypatch.setattr(mod, "_persist_coverage_disposition", persist)
    result = mod.TickerBackfillResult("NVO", 12)

    assert (
        mod._persist_commitment_scan_coverage(
            result,
            today=mod.date(2026, 9, 5),
            lookback=1,
            extraction_attempted=transcript_exists,
        )
        is closed
    )
    assert persisted[0]["artifact_kind"].value == "commitment_scan"
    assert persisted[0]["status"].value == expected
    assert result.commitment_scan_dispositions == [f"Q2_2026:{expected}"]


def test_ingest_success_requires_exact_evidence_for_every_fetched_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_module()
    result = mod.TickerBackfillResult("NU", 12, fetched=["Q2_2026", "Q1_2026"])
    seen: list[tuple[object, ...]] = []

    def has_evidence(*args: object) -> bool:
        seen.append(args)
        return args[2] == 2

    monkeypatch.setattr(mod, "_has_ingested_evidence", has_evidence)

    assert mod._fetched_evidence_complete(result) is False
    assert seen == [("NU", 2026, 2, 12), ("NU", 2026, 1, 12)]


def test_ingest_child_failure_is_terminal() -> None:
    mod = _load_module()

    assert mod._terminal_exit_code(None, []) == 0
    assert mod._terminal_exit_code(0, []) == 0
    assert mod._terminal_exit_code(7, []) == 7
    assert mod._terminal_exit_code(0, [{"ticker": "NU", "rc": 9}]) == 1
    assert mod._terminal_exit_code(None, [], acquisition_errors=2) == 1
    assert mod._terminal_exit_code(8, [], acquisition_errors=2) == 8


@pytest.mark.parametrize(
    "recorded",
    ["../outside.txt", "transcripts/raw/../outside.txt", "C:/outside.txt"],
)
def test_backfill_rejects_non_relative_recorded_paths(
    recorded: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_module()
    outside = tmp_path / "outside.txt"
    outside.write_text("bound transcript", encoding="utf-8")
    digest = hashlib.sha256(outside.read_bytes()).hexdigest()
    db_path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE documents (id INTEGER PRIMARY KEY, ticker TEXT, file_path TEXT, sha256 TEXT);
        CREATE TABLE transcripts (
            id INTEGER PRIMARY KEY, document_id INTEGER, ticker TEXT,
            fiscal_period_type TEXT, period_end TEXT
        );
        """
    )
    conn.execute("INSERT INTO documents VALUES (1, 'NU', ?, ?)", (recorded, digest))
    conn.execute("INSERT INTO transcripts VALUES (1, 1, 'NU', 'Q1', '2026-03-31')")
    conn.commit()
    conn.close()

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        return connection

    monkeypatch.setattr(mod.db, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(mod.db, "get_connection", connect)
    assert mod._has_ingested_evidence("NU", 2026, 1, 12) is False


def test_backfill_stop_rejects_unreceipted_raw_path_even_with_matching_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_module()
    raw = tmp_path / "transcripts" / "raw" / "NU_Q1_2026.txt"
    raw.parent.mkdir(parents=True)
    raw.write_text("bound transcript", encoding="utf-8")
    digest = hashlib.sha256(raw.read_bytes()).hexdigest()
    db_path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY, ticker TEXT, file_path TEXT, sha256 TEXT
        );
        CREATE TABLE transcripts (
            id INTEGER PRIMARY KEY, document_id INTEGER, ticker TEXT,
            fiscal_period_type TEXT, period_end TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO documents VALUES (1, 'NU', 'transcripts/raw/NU_Q1_2026.txt', ?)",
        (digest,),
    )
    conn.execute("INSERT INTO transcripts VALUES (1, 1, 'NU', 'Q1', '2026-03-31')")
    conn.commit()
    conn.close()

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        return connection

    monkeypatch.setattr(mod.db, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(mod.db, "get_connection", connect)
    assert mod._has_ingested_evidence("NU", 2026, 1, 12) is False


def test_backfill_stop_requires_processed_current_segments_and_authorized_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_module()
    processed = tmp_path / "transcripts" / "processed" / "NU_Q1_2026.txt"
    processed.parent.mkdir(parents=True)
    processed.write_text("authorized bound transcript", encoding="utf-8")
    digest = hashlib.sha256(processed.read_bytes()).hexdigest()
    db_path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE documents (id INTEGER PRIMARY KEY,ticker TEXT,file_path TEXT,sha256 TEXT);
        CREATE TABLE transcripts (
            id INTEGER PRIMARY KEY,document_id INTEGER,ticker TEXT,
            fiscal_period_type TEXT,period_end TEXT,is_current INTEGER
        );
        CREATE TABLE transcript_segments (id INTEGER PRIMARY KEY,transcript_id INTEGER);
        CREATE TABLE transcript_acquisition_receipts (
            receipt_id TEXT,document_id INTEGER,canonical_ticker TEXT,fiscal_year INTEGER,
            fiscal_quarter INTEGER,canonical_document_path TEXT,artifact_sha256 TEXT,
            provider TEXT,source_type TEXT,document_type TEXT
        );
        INSERT INTO documents VALUES (
            1,'NU','transcripts/processed/NU_Q1_2026.txt','DIGEST'
        );
        INSERT INTO transcripts VALUES (2,1,'NU','Q1','2026-03-31',1);
        INSERT INTO transcript_segments VALUES (3,2);
        INSERT INTO transcript_acquisition_receipts VALUES (
            'receipt',NULL,'NU',2026,1,'transcripts/raw/NU_Q1_2026.txt','DIGEST',
            'issuer_ir','ir_doc','earnings_call_transcript'
        );
        """.replace("DIGEST", digest)
    )
    conn.commit()
    conn.close()

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        return connection

    monkeypatch.setattr(mod.db, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(mod.db, "get_connection", connect)
    assert mod._has_ingested_evidence("NU", 2026, 1, 12) is True
    processed.write_text("mutated", encoding="utf-8")
    assert mod._has_ingested_evidence("NU", 2026, 1, 12) is False


def test_scheduled_transcript_scope_is_portfolio_only_but_explicit_evaluation_is_allowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_module()
    db_path = tmp_path / "portfolio.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE tracked_companies (ticker TEXT, list_type TEXT, archived_at TEXT, "
            "fiscal_year_end TEXT)"
        )
        conn.executemany(
            "INSERT INTO tracked_companies VALUES (?, ?, NULL, '12-31')",
            [
                ("PORT", "portfolio"),
                ("EVAL", "evaluation"),
                ("WATCH", "watchlist"),
                ("IDX", "index_member"),
            ],
        )

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        return connection

    monkeypatch.setattr(mod.db, "get_connection", connect)
    assert mod._resolve_tickers(None) == [("PORT", 12)]
    assert mod._resolve_tickers("EVAL") == [("EVAL", 12)]
    assert mod._resolve_tickers("WATCH") == []
    assert mod._resolve_tickers("IDX") == []


def test_transcript_automatic_lookback_defaults_to_five() -> None:
    mod = _load_module()
    assert mod._DEFAULT_LOOKBACK == 5


def test_help_does_not_advertise_retired_audio_fallback() -> None:
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "execution" / "backfill_transcripts.py"), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "--audio-fallback" not in result.stdout
