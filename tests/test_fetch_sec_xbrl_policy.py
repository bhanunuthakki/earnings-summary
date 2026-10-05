from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn, cast

import pytest

from execution import fetch_sec_xbrl
from models.companies import Company, ListType
from pipeline import sec_xbrl


def _companies(rows: list[tuple[str, ListType]]) -> list[Company]:
    return cast(
        "list[Company]",
        [
            SimpleNamespace(ticker=ticker, list_type=role, instrument_type="equity")
            for ticker, role in rows
        ],
    )


def _args(ticker: str | None = None, *, all_mapped: bool = False) -> argparse.Namespace:
    return argparse.Namespace(ticker=ticker, all_mapped=all_mapped)


def test_sec_scheduled_scope_includes_all_research_roles_and_is_priority_ordered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def tracked(_conn: sqlite3.Connection) -> list[Company]:
        return _companies(
            [
                ("WIX", ListType.EVALUATION),
                ("NOW", ListType.WATCHLIST),
                ("META", ListType.PORTFOLIO),
                ("RBRK", ListType.PORTFOLIO),
            ]
        )

    monkeypatch.setattr(fetch_sec_xbrl, "tracked_companies_for_user", tracked)
    with sqlite3.connect(":memory:") as conn:
        assert cast(
            Callable[[argparse.Namespace, sqlite3.Connection], list[str]],
            getattr(fetch_sec_xbrl, "_resolve_tickers"),
        )(_args(all_mapped=True), conn) == [
            "META",
            "RBRK",
            "WIX",
            "NOW",
        ]


def test_sec_explicit_request_uses_stored_role_and_cannot_bypass_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def tracked(_conn: sqlite3.Connection, **_kwargs: object) -> list[Company]:
        return _companies(
            [
                ("WIX", ListType.EVALUATION),
                ("NOW", ListType.WATCHLIST),
                ("IDX", ListType.INDEX_MEMBER),
            ]
        )

    monkeypatch.setattr(fetch_sec_xbrl, "tracked_companies_for_user", tracked)
    with sqlite3.connect(":memory:") as conn:
        assert cast(
            Callable[[argparse.Namespace, sqlite3.Connection], list[str]],
            getattr(fetch_sec_xbrl, "_resolve_tickers"),
        )(_args("WIX"), conn) == ["WIX"]
        assert cast(
            Callable[[argparse.Namespace, sqlite3.Connection], list[str]],
            getattr(fetch_sec_xbrl, "_resolve_tickers"),
        )(_args("NOW"), conn) == ["NOW"]
        assert (
            cast(
                Callable[[argparse.Namespace, sqlite3.Connection], list[str]],
                getattr(fetch_sec_xbrl, "_resolve_tickers"),
            )(_args("IDX"), conn)
            == []
        )
        assert (
            cast(
                Callable[[argparse.Namespace, sqlite3.Connection], list[str]],
                getattr(fetch_sec_xbrl, "_resolve_tickers"),
            )(_args("UNKNOWN"), conn)
            == []
        )


@pytest.mark.parametrize("instrument", ["equity", "adr", "etf", None])
def test_sec_evaluation_scope_requires_corporate_instrument(
    monkeypatch: pytest.MonkeyPatch, instrument: str | None
) -> None:
    def tracked(_conn: sqlite3.Connection) -> list[Company]:
        return cast(
            "list[Company]",
            [
                SimpleNamespace(
                    ticker="WIX", list_type=ListType.EVALUATION, instrument_type=instrument
                )
            ],
        )

    monkeypatch.setattr(fetch_sec_xbrl, "tracked_companies_for_user", tracked)
    with sqlite3.connect(":memory:") as conn:
        assert cast(
            Callable[[argparse.Namespace, sqlite3.Connection], list[str]],
            getattr(fetch_sec_xbrl, "_resolve_tickers"),
        )(_args(), conn) == (["WIX"] if instrument in ("equity", "adr") else [])


def test_sec_documented_foreign_non_filer_emits_an_honest_disposition(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def tracked(_conn: sqlite3.Connection) -> list[Company]:
        return _companies([("NTDOY", ListType.PORTFOLIO)])

    monkeypatch.setattr(fetch_sec_xbrl, "tracked_companies_for_user", tracked)
    with sqlite3.connect(":memory:") as conn:
        assert (
            cast(
                Callable[[argparse.Namespace, sqlite3.Connection], list[str]],
                getattr(fetch_sec_xbrl, "_resolve_tickers"),
            )(_args(), conn)
            == []
        )

    stderr = capsys.readouterr().err
    assert '"event": "sec_no_filer_disposition"' in stderr
    assert '"disposition": "documented_non_filer"' in stderr


def test_sec_explicit_request_keeps_unmapped_authorized_company_for_identity_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def tracked(_conn: sqlite3.Connection, **_kwargs: object) -> list[Company]:
        return _companies([("MBGL", ListType.EVALUATION)])

    monkeypatch.setattr(fetch_sec_xbrl, "tracked_companies_for_user", tracked)
    with sqlite3.connect(":memory:") as conn:
        assert cast(
            Callable[[argparse.Namespace, sqlite3.Connection], list[str]],
            getattr(fetch_sec_xbrl, "_resolve_tickers"),
        )(_args("MBGL"), conn) == ["MBGL"]


@pytest.mark.parametrize("explicit_root", [False, True])
def test_sec_cli_uses_selected_artifact_root_for_ingestion_and_raw_error_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    explicit_root: bool,
) -> None:
    checkout = tmp_path / "checkout"
    state = tmp_path / "configured-state"
    expected = state if explicit_root else checkout
    raw = expected / "data" / "historical" / "sec" / "MBGL_companyfacts.json"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"synthetic retained response")
    received_roots: list[Path] = []
    conn = sqlite3.connect(":memory:")

    def open_test_db(_path: str) -> sqlite3.Connection:
        return conn

    def tickers(_args: argparse.Namespace, _conn: sqlite3.Connection) -> list[str]:
        return ["MBGL"]

    def begin_run(*_args: object, **_kwargs: object) -> str:
        return "synthetic-root-test"

    def ingest(
        _conn: sqlite3.Connection,
        *,
        ticker: str,
        project_root: Path,
        run_id: str,
        timing_sink: Callable[[sec_xbrl.SecIngestTimingReceipt], None],
    ) -> sec_xbrl.IngestStats:
        del ticker, run_id, timing_sink
        received_roots.append(project_root)
        raise ValueError("synthetic schema failure")

    def finish(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(fetch_sec_xbrl, "PROJECT_ROOT", checkout)
    monkeypatch.setattr(fetch_sec_xbrl, "open_db", open_test_db)
    monkeypatch.setattr(fetch_sec_xbrl, "_resolve_tickers", tickers)
    monkeypatch.setattr(fetch_sec_xbrl, "start_run", begin_run)
    monkeypatch.setattr(fetch_sec_xbrl, "end_run", finish)
    monkeypatch.setattr(fetch_sec_xbrl, "ingest_for_ticker", ingest)
    database = tmp_path / "unrelated-database.db"
    database.touch()
    argv = ["fetch_sec_xbrl.py", "--db", str(database)]
    if explicit_root:
        argv.extend(("--project-root", str(state)))
    monkeypatch.setattr(sys, "argv", argv)
    assert fetch_sec_xbrl.main() == 1
    assert received_roots == [expected]
    result = json.loads(capsys.readouterr().out)
    assert result["rows"][0]["raw_response_path"] == str(raw)


def test_sec_cli_without_override_uses_existing_configured_database_for_consumers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import db

    configured = tmp_path / "configured-authority.db"
    configured.touch()
    conn = sqlite3.connect(":memory:")
    opened: list[Path] = []
    consumed: list[str] = []

    def open_test_db(path: Path) -> sqlite3.Connection:
        opened.append(path)
        return conn

    def tickers(_args: argparse.Namespace, _conn: sqlite3.Connection) -> list[str]:
        return ["MBGL"]

    def begin_run(*_args: object, **_kwargs: object) -> str:
        return "configured-database-test"

    def ingest(*_args: object, **_kwargs: object) -> sec_xbrl.IngestStats:
        return sec_xbrl.IngestStats(accessions_inserted=0, facts_inserted=1)

    def finish(*_args: object, **_kwargs: object) -> None:
        return None

    def invalidate(
        _conn: sqlite3.Connection,
        *,
        ticker: str,
        stats: sec_xbrl.IngestStats,
        db_path: str,
    ) -> tuple[bool, int]:
        del ticker, stats
        consumed.append(db_path)
        return False, 0

    monkeypatch.setattr(db, "DB_PATH", str(configured))
    monkeypatch.setattr(fetch_sec_xbrl, "open_db", open_test_db)
    monkeypatch.setattr(fetch_sec_xbrl, "_resolve_tickers", tickers)
    monkeypatch.setattr(fetch_sec_xbrl, "start_run", begin_run)
    monkeypatch.setattr(fetch_sec_xbrl, "end_run", finish)
    monkeypatch.setattr(fetch_sec_xbrl, "ingest_for_ticker", ingest)
    monkeypatch.setattr(fetch_sec_xbrl, "handle_silent_staleness", invalidate)
    monkeypatch.setattr(sys, "argv", ["fetch_sec_xbrl.py"])
    assert fetch_sec_xbrl.main() == 0
    assert opened == [configured.resolve()]
    assert consumed == [str(configured.resolve())]


@pytest.mark.parametrize("missing_override", [False, True])
def test_sec_cli_unavailable_database_refuses_before_writer(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    missing_override: bool,
) -> None:
    import db

    checkout_default = Path(fetch_sec_xbrl.__file__).resolve().parents[1] / "data" / "portfolio.db"
    monkeypatch.setattr(db, "DB_PATH", str(checkout_default))
    opened: list[object] = []

    def refuse_writer(path: object) -> NoReturn:
        opened.append(path)
        raise AssertionError("writer must not open for an unavailable database")

    monkeypatch.setattr(fetch_sec_xbrl, "open_db", refuse_writer)
    argv = ["fetch_sec_xbrl.py"]
    if missing_override:
        argv.extend(("--db", str(tmp_path / "does-not-exist.db")))
    monkeypatch.setattr(sys, "argv", argv)
    assert fetch_sec_xbrl.main() == 3
    assert opened == []
    assert not (tmp_path / "does-not-exist.db").exists()
    assert json.loads(capsys.readouterr().out) == {
        "event": "sec_xbrl_database_unavailable",
        "status": "unavailable",
        "reason_code": "database_authority_unavailable",
    }


@pytest.mark.parametrize("status", [401, 403])
def test_companyfacts_boundary_classifies_auth_denial(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    class AuthResponse(sec_xbrl.requests.Response):
        def raise_for_status(self) -> NoReturn:
            pytest.fail("generic HTTP path was used")

    response = AuthResponse()
    response.status_code = status
    setattr(response, "_content", b"denied")

    def get_response(*_args: object, **_kwargs: object) -> AuthResponse:
        return response

    monkeypatch.setattr(sec_xbrl.requests, "get", get_response)

    with pytest.raises(sec_xbrl.SecCompanyFactsAuthenticationDeniedError) as exc_info:
        sec_xbrl.fetch_companyfacts("0000000001")

    assert exc_info.value.status_code == status


def test_sec_auth_denial_halts_before_the_next_ticker(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    conn = sqlite3.connect(":memory:")
    calls: list[str] = []
    terminal: dict[str, object] = {}

    def open_test_db(_path: Path) -> sqlite3.Connection:
        return conn

    def tickers(_args: argparse.Namespace, _conn: sqlite3.Connection) -> list[str]:
        return ["META", "RBRK"]

    def begin_run(*_args: object, **_kwargs: object) -> str:
        return "run-1"

    monkeypatch.setattr(fetch_sec_xbrl, "open_db", open_test_db)
    monkeypatch.setattr(fetch_sec_xbrl, "_resolve_tickers", tickers)
    monkeypatch.setattr(fetch_sec_xbrl, "start_run", begin_run)

    def ingest(
        _conn: sqlite3.Connection,
        *,
        ticker: str,
        project_root: object,
        run_id: str,
        timing_sink: Callable[[sec_xbrl.SecIngestTimingReceipt], None],
    ) -> object:
        del project_root, run_id
        calls.append(ticker)
        timing_sink(
            sec_xbrl.SecIngestTimingReceipt(
                ticker=ticker,
                outcome="failed",
                failed_phase="http_fetch",
                total_ms=2.0,
                phases=sec_xbrl.SecIngestPhaseDurations(
                    http_fetch_ms=2.0,
                    payload_parse_ms=0.0,
                    snapshot_registration_ms=0.0,
                    accession_mapping_ms=0.0,
                    document_evidence_capture_ms=0.0,
                    tag_selection_ms=0.0,
                    fact_persistence_restatement_ms=0.0,
                    evidence_capture_resolution_ms=0.0,
                    commit_ms=0.0,
                    latest_cache_publish_ms=0.0,
                ),
            )
        )
        raise sec_xbrl.SecCompanyFactsAuthenticationDeniedError(403)

    def finish(
        _conn: sqlite3.Connection,
        run_id: str,
        status: object,
        *,
        error_summary: str | None,
    ) -> None:
        terminal.update(run_id=run_id, status=status, error_summary=error_summary)

    monkeypatch.setattr(fetch_sec_xbrl, "ingest_for_ticker", ingest)
    monkeypatch.setattr(fetch_sec_xbrl, "end_run", finish)
    database = tmp_path / "authentication-policy.db"
    database.touch()
    monkeypatch.setattr(sys, "argv", ["fetch_sec_xbrl.py", "--db", str(database)])

    assert fetch_sec_xbrl.main() == 1
    assert calls == ["META"]
    assert terminal["error_summary"] == "1 tickers failed"
    events = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert events == [
        {
            "event": "sec_xbrl_ingest_timing",
            "failed_phase": "http_fetch",
            "outcome": "failed",
            "phases": {
                "accession_mapping_ms": 0.0,
                "commit_ms": 0.0,
                "document_evidence_capture_ms": 0.0,
                "evidence_capture_resolution_ms": 0.0,
                "fact_persistence_restatement_ms": 0.0,
                "http_fetch_ms": 2.0,
                "latest_cache_publish_ms": 0.0,
                "payload_parse_ms": 0.0,
                "snapshot_registration_ms": 0.0,
                "tag_selection_ms": 0.0,
            },
            "run_id": "run-1",
            "schema_version": "sec_companyfacts_ingest_timing.v1",
            "ticker": "META",
            "total_ms": 2.0,
        }
    ]
