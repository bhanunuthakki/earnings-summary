"""New-company onboarding uses the primary SEC lane without FMP or a thesis."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from argparse import Namespace
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Protocol, TypeVar, cast

import pytest

import db
from execution import onboard_pending_tickers, onboard_ticker
from pipeline import sec_xbrl
from pipeline.quarterly_refresh import StageName, StageResult, StageStatus
from pipeline.run_accounting import JsonValue, make_pipeline_key
from pipeline.sec_onboarding_identity import IdentityStatus, SecOnboardingIdentityResult
from provenance.issuer_registry_bootstrap import (
    BootstrapRequest,
    bootstrap_issuer_reporting_registry,
)


class SecIngestion(Protocol):
    def __call__(
        self,
        conn: sqlite3.Connection,
        *,
        ticker: str,
        project_root: Path,
        run_id: str,
        skip: bool,
    ) -> StageResult: ...


class OnboardInputs(Protocol):
    def __call__(
        self,
        args: Namespace,
        ticker: str,
        *,
        instrument: str | None,
        conn: sqlite3.Connection | None = None,
        acquisition_at: datetime | None = None,
    ) -> dict[str, JsonValue]: ...


class TranscriptBackfill(Protocol):
    def __call__(self, ticker: str, *, skip_llm: bool = False) -> int: ...


onboard = cast("Callable[[Namespace], int]", getattr(onboard_ticker, "_onboard"))
sec_ingestion = cast("SecIngestion", getattr(onboard_ticker, "_run_sec_ingestion"))
onboard_inputs = cast("OnboardInputs", getattr(onboard_ticker, "_onboard_invocation_inputs"))
transcript_backfill = cast(
    "TranscriptBackfill", getattr(onboard_ticker, "_run_transcript_backfill")
)
remaining_fmp_budget = cast(
    "Callable[[], int]", getattr(onboard_pending_tickers, "_remaining_fmp_budget")
)


def runtime_path(owner: ModuleType, name: str) -> Path:
    value: object = getattr(owner, name)
    assert isinstance(value, Path)
    return value


_Result = TypeVar("_Result")


def _constant_result(value: _Result) -> Callable[..., _Result]:
    def call(*_args: object, **_kwargs: object) -> _Result:
        return value

    return call


@pytest.mark.parametrize("skip_fmp", [False, True])
@pytest.mark.parametrize("skip_llm", [False, True])
def test_onboard_equity_attempts_sec_after_failed_or_skipped_fmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skip_fmp: bool, skip_llm: bool
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE tracked_companies (ticker TEXT, instrument_type TEXT)")
    conn.execute("INSERT INTO tracked_companies VALUES ('NEW','equity')")
    monkeypatch.setattr(onboard_ticker, "open_db", _constant_result(conn))
    monkeypatch.setattr(onboard_ticker, "_STATE_ROOT", tmp_path)
    monkeypatch.setattr(onboard_ticker, "_HOLDINGS_DIR", tmp_path / "holdings")
    monkeypatch.setattr(onboard_ticker, "_run_fmp_fetch", _constant_result(1))
    monkeypatch.setattr(
        onboard_ticker,
        "ensure_sec_onboarding_identity",
        _constant_result(SecOnboardingIdentityResult(IdentityStatus.READY, "fixture identity")),
    )
    monkeypatch.setattr(onboard_ticker, "index_fmp_files_for_ticker", _constant_result(0))
    monkeypatch.setattr(onboard_ticker, "set_fiscal_year_end_from_fmp", _constant_result(None))
    monkeypatch.setattr(onboard_ticker, "set_instrument_type_from_fmp", _constant_result(None))
    monkeypatch.setattr(onboard_ticker, "set_filing_regime_from_profile", _constant_result(None))
    monkeypatch.setattr(
        onboard_ticker, "stage_pending_issuer_transcripts", _constant_result(dict[str, object]())
    )
    monkeypatch.setattr(onboard_ticker, "start_run", _constant_result("attempt"))
    monkeypatch.setattr(onboard_ticker, "end_run", _constant_result(None))
    monkeypatch.setattr(
        onboard_ticker, "refresh_ticker", _constant_result(SimpleNamespace(stages=[]))
    )
    calls: list[bool] = []

    def sec(_conn: sqlite3.Connection, **kwargs: object) -> StageResult:
        calls.append(bool(kwargs["skip"]))
        return StageResult(StageName.FETCH_SEC_XBRL, StageStatus.OK, 1, "one admitted fact")

    monkeypatch.setattr(onboard_ticker, "_run_sec_ingestion", sec)
    source_calls: list[tuple[str, bool]] = []

    def transcripts(_ticker: str, *, skip_llm: bool = False) -> int:
        source_calls.append(("transcripts", skip_llm))
        return 0

    def ir(_ticker: str) -> int:
        source_calls.append(("ir", False))
        return 0

    def saydo(_ticker: str) -> int:
        pytest.fail("--skip-llm must suppress Say-Do even with --force-saydo")

    monkeypatch.setattr(onboard_ticker, "_run_transcript_backfill", transcripts)
    monkeypatch.setattr(onboard_ticker, "_run_ir_documents", ir)
    monkeypatch.setattr(onboard_ticker, "_run_saydo", saydo)
    args = Namespace(
        ticker="NEW",
        industry_template=None,
        instrument=None,
        skip_fmp=skip_fmp,
        skip_sec=False,
        skip_llm=skip_llm,
        skip_transcripts=not skip_llm,
        skip_ir=not skip_llm,
        skip_saydo=not skip_llm,
        force_saydo=True,
    )
    assert onboard(args) == 0
    assert calls == [False]
    assert source_calls == ([("transcripts", True), ("ir", False)] if skip_llm else [])
    assert not (tmp_path / "holdings" / "NEW.json").exists()


def test_pending_children_preserve_separate_state_and_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    database = tmp_path / "authority.sqlite"
    holdings = state / "micro_thesis" / "holdings"
    holdings.mkdir(parents=True)
    (holdings / "NEW.json").write_text('{"wacc":0.09}')
    monkeypatch.setattr(onboard_pending_tickers, "_STATE_ROOT", state)
    monkeypatch.setattr(onboard_pending_tickers, "_DB_PATH", database)
    monkeypatch.setattr(onboard_pending_tickers, "_HOLDINGS_DIR", holdings)
    calls: dict[str, list[str]] = {}

    def child(cmd: list[str], stage: str, _log: Path) -> onboard_pending_tickers.StageResult:
        calls[stage] = cmd
        return onboard_pending_tickers.StageResult(
            stage, onboard_pending_tickers.StageOutcome.OK, 0, ""
        )

    monkeypatch.setattr(onboard_pending_tickers, "_run_subprocess", child)
    onboard_pending_tickers.onboard_one(
        "NEW",
        "no_financial_facts",
        skip_fmp=True,
        skip_commitments=False,
        log_path=tmp_path / "log",
    )
    for stage in ("onboard_ticker", "run_thesis_evaluator", "extract_commitments"):
        assert calls[stage][calls[stage].index("--db") + 1] == str(database)
    cmd = calls["onboard_ticker"]
    assert cmd[cmd.index("--project-root") + 1] == str(state)
    assert "--skip-fmp" in cmd and "--skip-sec" not in cmd
    assert calls["refresh_dcf"][calls["refresh_dcf"].index("--repo-root") + 1] == str(state)


def test_existing_dcf_input_never_replays_source_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(onboard_pending_tickers, "_has_valuation_inputs", _constant_result(True))
    monkeypatch.setattr(onboard_pending_tickers, "_HOLDINGS_DIR", tmp_path)
    calls: list[str] = []

    def child(_cmd: list[str], stage: str, _log: Path) -> onboard_pending_tickers.StageResult:
        calls.append(stage)
        return onboard_pending_tickers.StageResult(
            stage, onboard_pending_tickers.StageOutcome.OK, 0, ""
        )

    monkeypatch.setattr(onboard_pending_tickers, "_run_subprocess", child)
    onboard_pending_tickers.onboard_one(
        "NEW",
        "no_dcf_run",
        skip_fmp=False,
        skip_commitments=True,
        log_path=tmp_path / "log",
    )
    assert calls == ["refresh_dcf"]


def test_sec_discovery_window_replays_daily_and_tracks_identity_and_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(onboard_ticker, "_STATE_ROOT", tmp_path)
    monkeypatch.setattr(onboard_ticker, "_HOLDINGS_DIR", tmp_path / "holdings")
    cik = ["0001234567"]

    def resolve(*_args: object, **_kwargs: object) -> str:
        return cik[0]

    monkeypatch.setattr(onboard_ticker, "resolve_companyfacts_cik", resolve)
    args = Namespace(
        skip_fmp=True,
        skip_sec=False,
        skip_transcripts=True,
        skip_ir=True,
        skip_saydo=True,
        force_saydo=False,
        industry_template=None,
        instrument=None,
    )
    conn = sqlite3.connect(":memory:")

    def key(day: int, hour: int = 0) -> str:
        inputs = onboard_inputs(
            args,
            "NEW",
            instrument="equity",
            conn=conn,
            acquisition_at=datetime(2026, 10, day, hour, tzinfo=UTC),
        )
        return make_pipeline_key("onboard_ticker", ["NEW"], inputs)

    try:
        baseline = key(1)
        args.skip_llm = True
        assert key(1) != baseline
        args.skip_llm = False
        assert key(1, 23) == baseline
        assert key(2) != baseline
        cik[0] = "0007654321"
        changed_identity = key(1)
        assert changed_identity != baseline
        path = tmp_path / "data/historical/sec/NEW_companyfacts.json"
        path.parent.mkdir(parents=True)
        path.write_bytes(b'{"cik":7654321}')
        assert key(1) != changed_identity
        args.skip_sec = True
        assert key(1) == key(2)
    finally:
        conn.close()


@pytest.mark.parametrize("skip_llm", [False, True])
def test_transcript_child_collects_without_llm_and_preserves_state_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skip_llm: bool
) -> None:
    state = tmp_path / "state"
    database = tmp_path / "isolated.sqlite"
    monkeypatch.setattr(onboard_ticker, "_STATE_ROOT", state)
    monkeypatch.setattr(onboard_ticker, "_DB_PATH", database)
    calls: list[list[str]] = []

    def child(cmd: list[str], *, cwd: str) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        assert cwd == str(onboard_ticker.PROJECT_ROOT)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(onboard_ticker.subprocess, "run", child)
    assert transcript_backfill("NEW", skip_llm=skip_llm) == 0
    command = calls[0]
    assert command[command.index("--db") + 1] == str(database)
    assert command[command.index("--repo-root") + 1] == str(state)
    assert ("--skip-extract" in command) is skip_llm


def test_fmp_budget_uses_retained_state_without_changing_code_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from execution import refresh_cache

    monkeypatch.setattr(onboard_pending_tickers, "_STATE_ROOT", tmp_path)
    monkeypatch.setenv("FMP_TIER", "basic")
    ledger = tmp_path / ".tmp" / "cacher" / f"budget_{datetime.now().date().isoformat()}.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text('{"calls_made":249}')
    original_cache = refresh_cache.CACHE_DIR
    assert remaining_fmp_budget() == 1
    assert original_cache == refresh_cache.CACHE_DIR


def test_onboard_runtime_separates_code_state_and_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    database = tmp_path / "database" / "research.db"
    database.parent.mkdir()
    sqlite3.connect(database).close()
    loaded: list[Path] = []
    monkeypatch.setattr(onboard_ticker, "load_project_env", loaded.append)
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(database))
    for owner, names in (
        (onboard_ticker, ("_STATE_ROOT", "_DB_PATH", "_HOLDINGS_DIR")),
        (db, ("DB_PATH", "STATE_ROOT", "DATA_DIR", "FMP_DIR", "PROJECT_ROOT")),
    ):
        for name in names:
            monkeypatch.setattr(owner, name, getattr(owner, name))
    code_root = onboard_ticker.PROJECT_ROOT

    onboard_ticker.configure_onboarding_runtime(state, database)

    assert loaded == [state.resolve()]
    assert code_root == onboard_ticker.PROJECT_ROOT
    assert state.resolve() == runtime_path(onboard_ticker, "_STATE_ROOT")
    assert database.resolve() == runtime_path(onboard_ticker, "_DB_PATH")
    assert state.resolve() / "micro_thesis" / "holdings" == runtime_path(
        onboard_ticker, "_HOLDINGS_DIR"
    )


@pytest.mark.parametrize("entrypoint", ["single", "pending"])
@pytest.mark.parametrize("configured", [None, "", "  "])
def test_onboard_runtime_refuses_shadow_database_without_approved_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
    configured: str | None,
) -> None:
    owner = onboard_ticker if entrypoint == "single" else onboard_pending_tickers
    configure = (
        onboard_ticker.configure_onboarding_runtime
        if entrypoint == "single"
        else onboard_pending_tickers.configure_runtime
    )
    state = tmp_path / "state"
    shadow = state / "data" / "portfolio.db"
    shadow.parent.mkdir(parents=True)
    sqlite3.connect(shadow).close()
    # Neither an existing state-root shadow nor a stale process binding grants authority.
    monkeypatch.setattr(db, "DB_PATH", shadow)
    monkeypatch.setattr(owner, "load_project_env", _constant_result(None))
    if configured is None:
        monkeypatch.delenv("EARNINGS_SUMMARY_DB_PATH", raising=False)
    else:
        monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", configured)
    before = (
        runtime_path(owner, "_STATE_ROOT"),
        runtime_path(owner, "_DB_PATH"),
        runtime_path(owner, "_HOLDINGS_DIR"),
    )
    for name in ("_STATE_ROOT", "_DB_PATH", "_HOLDINGS_DIR"):
        monkeypatch.setattr(owner, name, getattr(owner, name))
    if entrypoint == "pending":
        monkeypatch.setattr(
            onboard_pending_tickers, "_LOG_DIR", runtime_path(onboard_pending_tickers, "_LOG_DIR")
        )

    with pytest.raises(RuntimeError, match="explicit or configured portfolio database"):
        configure(state, None)

    assert before == (
        runtime_path(owner, "_STATE_ROOT"),
        runtime_path(owner, "_DB_PATH"),
        runtime_path(owner, "_HOLDINGS_DIR"),
    )
    assert not (state / ".tmp").exists()


@pytest.mark.parametrize("entrypoint", ["single", "pending"])
@pytest.mark.parametrize("explicit", [False, True])
def test_onboard_runtime_uses_approved_database_after_environment_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entrypoint: str, explicit: bool
) -> None:
    owner = onboard_ticker if entrypoint == "single" else onboard_pending_tickers
    configure = (
        onboard_ticker.configure_onboarding_runtime
        if entrypoint == "single"
        else onboard_pending_tickers.configure_runtime
    )
    state = tmp_path / "state"
    authority = tmp_path / "approved.sqlite"
    sqlite3.connect(authority).close()
    for name in ("_STATE_ROOT", "_DB_PATH", "_HOLDINGS_DIR"):
        monkeypatch.setattr(owner, name, getattr(owner, name))
    if entrypoint == "pending":
        monkeypatch.setattr(
            onboard_pending_tickers, "_LOG_DIR", runtime_path(onboard_pending_tickers, "_LOG_DIR")
        )
    for name in ("DB_PATH", "STATE_ROOT", "DATA_DIR", "FMP_DIR", "PROJECT_ROOT"):
        monkeypatch.setattr(db, name, getattr(db, name))

    def load(root: Path) -> None:
        assert root == state.resolve()
        monkeypatch.setenv(
            "EARNINGS_SUMMARY_DB_PATH", str(tmp_path / "missing.sqlite" if explicit else authority)
        )

    monkeypatch.setattr(owner, "load_project_env", load)
    configure(state, authority if explicit else None)
    assert authority.resolve() == runtime_path(owner, "_DB_PATH")
    assert state.resolve() == runtime_path(owner, "_STATE_ROOT")


@pytest.mark.parametrize("skip", [False, True])
def test_sec_skip_is_independent_of_fmp_flag(skip: bool, tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    try:
        if skip:
            receipt = sec_ingestion(
                conn, ticker="NEW", project_root=tmp_path, run_id="test", skip=True
            )
            assert receipt.status is StageStatus.SKIPPED
        else:
            receipt = sec_ingestion(
                conn, ticker="NEW", project_root=tmp_path, run_id="test", skip=False
            )
            assert receipt.status is StageStatus.FAILED
            assert receipt.rows_processed == 0
    finally:
        conn.close()


def test_new_evaluation_uses_native_sec_admission_without_static_pin_or_thesis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, migrated_db: Callable[..., Path]
) -> None:
    state = tmp_path / "state"
    database = tmp_path / "research.db"
    migrated_db(database)
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    stamp = datetime(2026, 10, 1, tzinfo=UTC)
    conn.execute(
        "INSERT INTO tracked_companies (user_id,ticker,name,list_type,instrument_type,"
        "filing_regime,fiscal_year_end) VALUES ('bhanu','NEW','New Issuer','evaluation',"
        "'equity','10-K','12-31')"
    )
    bootstrap_issuer_reporting_registry(
        conn,
        raw_body=json.dumps(
            {"0": {"cik_str": 1234567, "ticker": "NEW", "title": "New Issuer"}}
        ).encode(),
        request=BootstrapRequest(
            source_url="https://www.sec.gov/files/company_tickers.json",
            blob_root=state / "data" / "evidence" / "blobs",
            apply=True,
            recorded_at=stamp,
        ),
    )
    raw = json.dumps(
        {
            "cik": 1234567,
            "entityName": "New Issuer",
            "facts": {
                "us-gaap": {
                    "RevenueFromContractWithCustomerExcludingAssessedTax": {
                        "label": "Revenue",
                        "description": "Revenue",
                        "units": {
                            "USD": [
                                {
                                    "start": "2026-04-01",
                                    "end": "2026-06-30",
                                    "val": 100000000,
                                    "accn": "0001234567-26-000001",
                                    "fy": 2026,
                                    "fp": "Q2",
                                    "form": "10-Q",
                                    "filed": "2026-08-01",
                                    "frame": "CY2026Q2",
                                }
                            ]
                        },
                    }
                }
            },
        }
    ).encode()
    calls: list[str] = []

    def fetch(cik: str) -> sec_xbrl.FetchedCompanyFacts:
        calls.append(cik)
        return sec_xbrl.FetchedCompanyFacts(
            source_url=f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json",
            raw_body=raw,
            observed_at=stamp,
            retrieved_at=stamp,
        )

    monkeypatch.setattr(sec_xbrl, "fetch_companyfacts", fetch)
    assert "NEW" not in sec_xbrl.CIK_MAP
    try:
        receipt = sec_ingestion(
            conn, ticker="NEW", project_root=state, run_id="onboard-test", skip=False
        )
        assert receipt.status is StageStatus.OK
        assert receipt.rows_processed == 1
        assert calls == ["0001234567"]
        document = conn.execute("SELECT sha256,file_path FROM documents").fetchone()
        assert document is not None
        assert document[0] == hashlib.sha256(raw).hexdigest()
        assert Path(document[1]).is_relative_to(state)
        assert Path(document[1]).read_bytes() == raw
        assert conn.execute("SELECT COUNT(*) FROM fact_observation_revisions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM fact_observation_match_proofs").fetchone()[0] == 1
        assert (
            conn.execute("SELECT COUNT(*) FROM v_financial_facts_resolved_current").fetchone()[0]
            == 1
        )
        assert not (state / "micro_thesis" / "holdings" / "NEW.json").exists()
        replay = sec_ingestion(
            conn, ticker="NEW", project_root=state, run_id="onboard-replay", skip=False
        )
        assert replay.status is StageStatus.OK
        assert replay.rows_processed == 0
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    finally:
        conn.close()
