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

    receipt_ids = ["a" * 64, "b" * 64]
    rc = mod._run_ingest(
        repo_root,
        "AAPL",
        receipt_ids,
        dry_run=False,
        owner_requested=False,
    )
    assert rc == 0
    assert captured["kwargs"]["cwd"] == str(repo_root)
    assert captured["cmd"][1] == str(PROJECT_ROOT / "execution" / "sqlite_bootstrap.py")
    assert captured["cmd"][2] == str(PROJECT_ROOT / "execution" / "ingest_transcripts_state.py")
    assert captured["cmd"][captured["cmd"].index("--repo-root") + 1] == str(repo_root)
    assert captured["cmd"][captured["cmd"].index("--ticker") + 1] == "AAPL"
    assert "--owner-requested" not in captured["cmd"]
    assert "--no-promote" not in captured["cmd"]
    assert [
        captured["cmd"][index + 1]
        for index, value in enumerate(captured["cmd"])
        if value == "--receipt-id"
    ] == receipt_ids


def test_run_ingest_preserves_explicit_per_ticker_owner_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_module()
    repo_root = tmp_path / "main-repo"
    repo_root.mkdir()
    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **_kwargs: Any) -> _FakeProc:
        captured["cmd"] = cmd
        return _FakeProc(returncode=0)

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    rc = mod._run_ingest(
        repo_root,
        "NU",
        ["c" * 64],
        dry_run=False,
        owner_requested=True,
    )

    assert rc == 0
    assert captured["cmd"][captured["cmd"].index("--ticker") + 1] == "NU"
    assert "--owner-requested" in captured["cmd"]


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

    rc = mod._run_extract(repo_root, "AAPL", 42, dry_run=False)
    assert rc == 0
    assert captured["kwargs"]["cwd"] == str(repo_root)
    assert captured["cmd"][1] == str(PROJECT_ROOT / "execution" / "sqlite_bootstrap.py")
    assert captured["cmd"][2] == str(
        PROJECT_ROOT / "execution" / "extract_commitments_from_transcript.py"
    )
    assert "--auto" in captured["cmd"]
    assert "--rescan-unreceipted" not in captured["cmd"]
    assert "--ticker" not in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--transcript-id") + 1] == "42"
    assert captured["cmd"][captured["cmd"].index("--db") + 1] == str(
        repo_root / "data" / "portfolio.db"
    )


def test_run_ingest_dry_run_skips_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_module()

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("subprocess.run must not be called in dry-run")

    monkeypatch.setattr(mod.subprocess, "run", boom)
    assert (
        mod._run_ingest(
            Path("/nonexistent"),
            "AAPL",
            ["a" * 64],
            dry_run=True,
            owner_requested=False,
        )
        == 0
    )


def test_run_extract_dry_run_skips_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_module()

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("subprocess.run must not be called in dry-run")

    monkeypatch.setattr(mod.subprocess, "run", boom)
    assert mod._run_extract(Path("/nonexistent"), "AAPL", 42, dry_run=True) == 0


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
            result=SimpleNamespace(
                receipt_id="c" * 64,
                acquired_artifact=SimpleNamespace(
                    canonical_document_path=Path("transcripts/raw/NU_Q2_2026.txt"),
                    sha256="b" * 64,
                    size_bytes=123,
                ),
            ),
        )

    monkeypatch.setattr(mod, "recent_fiscal_quarters", fake_recent_fiscal_quarters)
    monkeypatch.setattr(mod, "_has_ingested_evidence", fake_has_ingested_evidence)
    monkeypatch.setattr(mod, "fetch_qa", fake_fetch_qa)
    monkeypatch.setattr(mod, "_canonical_processed_path_conflicts", _always_false)

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
    assert result.fetched_artifacts == [
        mod.FetchedTranscriptIdentity(
            label="Q2_2026",
            receipt_id="c" * 64,
            canonical_document_path="transcripts/raw/NU_Q2_2026.txt",
            sha256="b" * 64,
            size_bytes=123,
        )
    ]
    assert len(calls) == 1


def test_reacquired_q1_collision_is_actionable_while_q2_remains_ingestible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_module()
    persisted: list[dict[str, object]] = []

    def two_quarters(*_args: object) -> list[tuple[int, int]]:
        return [(2026, 2), (2026, 1)]

    monkeypatch.setattr(mod, "recent_fiscal_quarters", two_quarters)
    monkeypatch.setattr(mod, "_has_ingested_evidence", _always_false)

    def fake_fetch(spec: Any, **_kwargs: object) -> SimpleNamespace:
        label = f"Q{spec.quarter}_{spec.year}"
        digest = str(spec.quarter) * 64
        return SimpleNamespace(
            status=mod.FetchQaStatus.ACQUIRED,
            attempts=(
                SimpleNamespace(
                    provider="issuer_ir",
                    status=mod.FetchQaAttemptStatus.ACQUIRED,
                    idempotency_key="transcript:" + digest,
                ),
            ),
            result=SimpleNamespace(
                receipt_id=digest,
                acquired_artifact=SimpleNamespace(
                    canonical_document_path=Path(f"transcripts/raw/NU_{label}.txt"),
                    sha256=digest,
                    size_bytes=123,
                ),
            ),
        )

    def persist(**kwargs: object) -> str:
        persisted.append(kwargs)
        return "operational_error"

    monkeypatch.setattr(mod, "fetch_qa", fake_fetch)

    def conflicts(identity: Any) -> bool:
        return bool(identity.label == "Q1_2026")

    monkeypatch.setattr(mod, "_canonical_processed_path_conflicts", conflicts)
    monkeypatch.setattr(mod, "_persist_coverage_disposition", persist)

    result = mod._backfill_one(
        "NU",
        12,
        2,
        mod.date(2026, 9, 5),
        False,
        tmp_path / "portfolio.db",
        False,
    )

    assert result.errors == []
    assert result.fetched == ["Q2_2026"]
    assert [item.label for item in result.fetched_artifacts] == ["Q2_2026"]
    assert result.artifact_conflicts == [
        mod.TranscriptArtifactConflict(
            label="Q1_2026",
            receipt_id="1" * 64,
            reason_code="reacquired_transcript_conflicts_with_canonical_bytes",
        )
    ]
    conflict = persisted[0]
    assert conflict["status"] is mod.CoverageDispositionStatus.OPERATIONAL_ERROR
    assert conflict["reason_code"] == "reacquired_transcript_conflicts_with_canonical_bytes"
    assert mod._terminal_exit_code(0, [], acquisition_errors=len(result.errors)) == 0


def test_canonical_processed_collision_compares_db_and_live_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_module()
    repo_root = tmp_path / "repo"
    processed = repo_root / "transcripts" / "processed" / "NU_Q1_2026.txt"
    processed.parent.mkdir(parents=True)
    processed.write_bytes(b"immutable legacy bytes")
    legacy_sha = hashlib.sha256(processed.read_bytes()).hexdigest()
    db_path = tmp_path / "portfolio.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE documents (file_path TEXT, sha256 TEXT)")
        conn.execute(
            "INSERT INTO documents VALUES ('transcripts/processed/NU_Q1_2026.txt', ?)",
            (legacy_sha,),
        )

    def connection() -> sqlite3.Connection:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(mod.db, "get_connection", connection)
    monkeypatch.setattr(mod.db, "PROJECT_ROOT", str(repo_root))
    assert mod._canonical_processed_path_conflicts(  # pyright: ignore[reportPrivateUsage]
        mod.FetchedTranscriptIdentity(
            label="Q1_2026",
            receipt_id="a" * 64,
            canonical_document_path="transcripts/raw/NU_Q1_2026.txt",
            sha256="b" * 64,
            size_bytes=1,
        )
    )
    assert not mod._canonical_processed_path_conflicts(  # pyright: ignore[reportPrivateUsage]
        mod.FetchedTranscriptIdentity(
            label="Q1_2026",
            receipt_id="a" * 64,
            canonical_document_path="transcripts/raw/NU_Q1_2026.txt",
            sha256=legacy_sha,
            size_bytes=1,
        )
    )


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


def test_acquisition_denial_is_persisted_as_policy_blocked_not_operational_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_module()
    persisted: list[dict[str, Any]] = []

    def denied_fetch(*_args: object, **_kwargs: object) -> Never:
        raise mod.TranscriptAcquisitionDeniedError(
            "returned issuer source URL is outside configured authority"
        )

    def persist(**kwargs: Any) -> str:
        persisted.append(kwargs)
        return str(kwargs["status"].value)

    monkeypatch.setattr(mod, "recent_fiscal_quarters", _q2_2026)
    monkeypatch.setattr(mod, "_has_ingested_evidence", _always_false)
    monkeypatch.setattr(mod, "fetch_qa", denied_fetch)
    monkeypatch.setattr(mod, "_persist_coverage_disposition", persist)

    result = mod._backfill_one(
        "NU",
        12,
        1,
        mod.date(2026, 9, 5),
        False,
        tmp_path / "portfolio.db",
        False,
    )

    assert result.coverage_dispositions == ["Q2_2026:policy_blocked"]
    assert "TranscriptAcquisitionDeniedError" in result.errors[0]
    assert persisted[0]["status"] is mod.CoverageDispositionStatus.POLICY_BLOCKED
    assert persisted[0]["reason_code"] == "transcript_source_policy_denied"
    assert persisted[0]["attempts"] == (
        mod.CoverageAttempt(
            provider="transcript_chain",
            status=mod.CoverageAttemptStatus.POLICY_DENIED,
        ),
    )


def test_existing_unscanned_transcript_is_selected_even_without_new_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_module()
    result = mod.TickerBackfillResult("BKNG", 12, skipped_existing=["Q2_2026"])

    def exact_evidence(*_args: object) -> object:
        return mod.TranscriptEvidence("transcript-receipt:exact", "a" * 64)

    def no_scan_evidence(*_args: object) -> None:
        return None

    def transcript_id(*_args: object) -> int:
        return 42

    monkeypatch.setattr(mod, "recent_fiscal_quarters", _q2_2026)
    monkeypatch.setattr(mod, "_transcript_id_for_period", transcript_id)
    monkeypatch.setattr(
        mod,
        "_ingested_evidence",
        exact_evidence,
    )
    monkeypatch.setattr(mod, "_commitment_scan_evidence", no_scan_evidence)

    assert mod._commitment_scan_targets([result], mod.date(2026, 9, 5), 1) == [
        mod.CommitmentScanTarget(
            ticker="BKNG",
            fye_month=12,
            fiscal_year=2026,
            fiscal_quarter=2,
            transcript_id=42,
        )
    ]


def test_commitment_scan_targets_exclude_out_of_window_unreceipted_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_module()
    result = mod.TickerBackfillResult("BN", 12)
    historical_ids = {(2026, 2): 1135, (2025, 4): 1075}
    selected_periods: list[tuple[int, int]] = []

    def in_window(*_args: object) -> list[tuple[int, int]]:
        return [(2026, 2)]

    def transcript_id(
        _ticker: str,
        year: int,
        quarter: int,
        _fye_month: int,
    ) -> int | None:
        selected_periods.append((year, quarter))
        return historical_ids.get((year, quarter))

    def exact_evidence(*_args: object) -> object:
        return mod.TranscriptEvidence("transcript-receipt:exact", "a" * 64)

    def no_scan_evidence(*_args: object) -> None:
        return None

    monkeypatch.setattr(mod, "recent_fiscal_quarters", in_window)
    monkeypatch.setattr(mod, "_transcript_id_for_period", transcript_id)
    monkeypatch.setattr(mod, "_ingested_evidence", exact_evidence)
    monkeypatch.setattr(mod, "_commitment_scan_evidence", no_scan_evidence)

    targets = mod._commitment_scan_targets([result], mod.date(2026, 9, 5), 1)

    assert [
        (target.fiscal_year, target.fiscal_quarter, target.transcript_id) for target in targets
    ] == [(2026, 2, 1135)]
    assert selected_periods == [(2026, 2)]
    assert all(target.transcript_id != 1075 for target in targets)


def test_exact_commitment_scan_targets_continue_after_peer_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_module()
    targets = [
        mod.CommitmentScanTarget("BN", 12, 2026, 2, 1135),
        mod.CommitmentScanTarget("NU", 12, 2026, 2, 1136),
    ]
    invoked: list[tuple[str, int]] = []

    def run_extract(
        _repo_root: Path,
        ticker: str,
        transcript_id: int,
        _dry_run: bool,
    ) -> int:
        invoked.append((ticker, transcript_id))
        return 7 if transcript_id == 1135 else 0

    def scan_evidence(ticker: str, *_args: object) -> object | None:
        if ticker == "NU":
            return mod.TranscriptEvidence("commitment-scan-receipt:nu", "a" * 64)
        return None

    monkeypatch.setattr(mod, "_run_extract", run_extract)
    monkeypatch.setattr(mod, "_commitment_scan_evidence", scan_evidence)

    results = mod._run_commitment_scan_targets(tmp_path, targets, dry_run=False)

    assert invoked == [("BN", 1135), ("NU", 1136)]
    assert [item["rc"] for item in results] == [7, 0]
    assert mod._terminal_exit_code(None, results) == 1


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
    artifact_json = '{"artifact":"nu-q1-2026"}'
    receipt_id = hashlib.sha256(artifact_json.encode()).hexdigest()
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
            provider TEXT,source_type TEXT,document_type TEXT,artifact_json TEXT,recorded_at TEXT
        );
        INSERT INTO documents VALUES (
            1,'NU','transcripts/processed/NU_Q1_2026.txt','DIGEST'
        );
        INSERT INTO transcripts VALUES (2,1,'NU','Q1','2026-03-31',1);
        INSERT INTO transcript_segments VALUES (3,2);
        INSERT INTO transcript_acquisition_receipts VALUES (
            'RECEIPT',NULL,'NU',2026,1,'transcripts/raw/NU_Q1_2026.txt','DIGEST',
            'issuer_ir','ir_doc','earnings_call_transcript','ARTIFACT',
            '2026-09-05T01:00:00Z'
        );
        """.replace("DIGEST", digest)
        .replace("RECEIPT", receipt_id)
        .replace("ARTIFACT", artifact_json)
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


def test_ingested_and_scan_evidence_share_latest_valid_receipt_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_module()
    from pipeline.commitment_scan_receipts import append_commitment_scan_receipt

    processed = tmp_path / "transcripts" / "processed" / "BN_Q2_2026.txt"
    processed.parent.mkdir(parents=True)
    processed.write_text("authorized bound transcript", encoding="utf-8")
    digest = hashlib.sha256(processed.read_bytes()).hexdigest()
    valid_artifact_json = '{"artifact":"bn-q2-2026-valid"}'
    valid_receipt_id = hashlib.sha256(valid_artifact_json.encode()).hexdigest()
    db_path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
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
            provider TEXT,source_type TEXT,document_type TEXT,artifact_json TEXT,recorded_at TEXT
        );
        CREATE TABLE commitment_scan_receipts (
            receipt_id TEXT PRIMARY KEY,transcript_id INTEGER,document_id INTEGER,
            transcript_acquisition_receipt_id TEXT,transcript_sha256 TEXT,
            prompt_version TEXT,n_extracted INTEGER,output_manifest_json TEXT,
            output_manifest_sha256 TEXT,recorded_at TEXT
        );
        INSERT INTO documents VALUES (
            7,'BN','transcripts/processed/BN_Q2_2026.txt','DIGEST'
        );
        INSERT INTO transcripts VALUES (1135,7,'BN','Q2','2026-06-30',1);
        INSERT INTO transcript_segments VALUES (9,1135);
        INSERT INTO transcript_acquisition_receipts VALUES (
            'VALID_RECEIPT',NULL,'BN',2026,2,'transcripts/raw/BN_Q2_2026.txt','DIGEST',
            'issuer_ir','ir_doc','earnings_call_transcript','VALID_ARTIFACT',
            '2026-09-05T01:00:00Z'
        );
        INSERT INTO transcript_acquisition_receipts VALUES (
            'ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff',
            NULL,'BN',2026,2,'transcripts/raw/BN_Q2_2026.txt','DIGEST',
            'issuer_ir','ir_doc','earnings_call_transcript','{}',
            '2026-09-05T02:00:00Z'
        );
        """.replace("DIGEST", digest)
        .replace("VALID_RECEIPT", valid_receipt_id)
        .replace("VALID_ARTIFACT", valid_artifact_json)
    )
    scan_receipt = append_commitment_scan_receipt(
        conn,
        transcript_id=1135,
        prompt_version=mod.prompt_version_for("saydo_commitment_extract"),
    )
    conn.commit()
    conn.close()

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        return connection

    monkeypatch.setattr(mod.db, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(mod.db, "get_connection", connect)

    evidence = mod._ingested_evidence("BN", 2026, 2, 12)

    assert evidence is not None
    assert evidence.reference == f"transcript-receipt:{valid_receipt_id}"
    assert evidence.sha256 == digest

    scan_evidence = mod._commitment_scan_evidence("BN", 2026, 2, 12)
    assert scan_evidence == mod.TranscriptEvidence(
        reference=f"commitment-scan-receipt:{scan_receipt.receipt_id}",
        sha256=scan_receipt.receipt_id,
    )


def test_ambiguous_selected_transcript_is_explicit_terminal_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_module()
    result = mod.TickerBackfillResult("BN", 12)
    db_path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE transcripts (
            id INTEGER PRIMARY KEY,document_id INTEGER,ticker TEXT,
            fiscal_period_type TEXT,period_end TEXT,is_current INTEGER
        );
        INSERT INTO transcripts VALUES (1135,7,'BN','Q2','2026-06-30',1);
        INSERT INTO transcripts VALUES (1136,8,'BN','Q2','2026-06-30',1);
        """
    )
    conn.commit()
    conn.close()

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        return connection

    monkeypatch.setattr(mod, "recent_fiscal_quarters", _q2_2026)
    monkeypatch.setattr(mod.db, "get_connection", connect)

    targets = mod._commitment_scan_targets([result], mod.date(2026, 9, 5), 1)

    assert targets == []
    assert result.errors == [
        "Q2_2026: commitment scan selection failed: "
        "ambiguous_selected_transcript: transcript_ids=1135,1136"
    ]
    assert mod._terminal_exit_code(None, [], acquisition_errors=len(result.errors)) == 1


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
