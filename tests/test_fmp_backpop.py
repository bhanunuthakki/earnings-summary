"""Tests for execution/fmp_backpop.py — diff-aware EDGAR-vs-FMP job selection."""

from __future__ import annotations

import sqlite3
import sys
from datetime import date
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "execution"))

from fmp_backpop import SEC_OVERLAP_KEYS, build_manifest, sec_covers_well


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE financial_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            period_end TIMESTAMP NOT NULL,
            line_item TEXT NOT NULL,
            value NUMERIC(24,6) NOT NULL,
            extracted_by TEXT
        );
        CREATE TABLE validation_issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            ticker TEXT,
            severity TEXT NOT NULL,
            rule TEXT NOT NULL,
            raised_at TIMESTAMP NOT NULL,
            resolved_at TIMESTAMP
        );
        """
    )
    conn.commit()


def _insert_fact(conn: sqlite3.Connection, ticker: str, period_end: str, extracted_by: str) -> None:
    conn.execute(
        "INSERT INTO financial_facts (ticker, period_end, line_item, value, extracted_by) "
        "VALUES (?, ?, 'revenue', 100, ?)",
        (ticker, period_end, extracted_by),
    )
    conn.commit()


def _insert_issue(conn: sqlite3.Connection, ticker: str, *, resolved: bool) -> None:
    conn.execute(
        "INSERT INTO validation_issues (run_id, ticker, severity, rule, raised_at, resolved_at) "
        "VALUES ('run1', ?, 'warn', 'source_disagreement', '2026-01-01', ?)",
        (ticker, "2026-01-02" if resolved else None),
    )
    conn.commit()


def test_sec_covers_well_true_when_recent_fact_no_disagreement() -> None:
    conn = sqlite3.connect(":memory:")
    _create_schema(conn)
    _insert_fact(conn, "AAPL", "2026-06-30", "sec_xbrl")
    covered, count, has_disagreement = sec_covers_well(conn, "AAPL", today=date(2026, 7, 2))
    assert covered is True
    assert count == 1
    assert has_disagreement is False


def test_sec_covers_well_false_when_no_sec_facts() -> None:
    conn = sqlite3.connect(":memory:")
    _create_schema(conn)
    _insert_fact(conn, "FLKR", "2026-06-30", "fmp")
    covered, count, _has_disagreement = sec_covers_well(conn, "FLKR", today=date(2026, 7, 2))
    assert covered is False
    assert count == 0


def test_sec_covers_well_false_when_facts_stale() -> None:
    """fact_count is itself freshness-filtered, so a stale-only fact yields 0 —
    covered=False for the right reason (no recent fact), not an off-by-one."""
    conn = sqlite3.connect(":memory:")
    _create_schema(conn)
    _insert_fact(conn, "AAPL", "2024-01-01", "sec_xbrl")
    covered, count, _ = sec_covers_well(conn, "AAPL", today=date(2026, 7, 2))
    assert covered is False
    assert count == 0


def test_sec_covers_well_false_when_unresolved_disagreement() -> None:
    conn = sqlite3.connect(":memory:")
    _create_schema(conn)
    _insert_fact(conn, "AAPL", "2026-06-30", "sec_xbrl")
    _insert_issue(conn, "AAPL", resolved=False)
    covered, _, has_disagreement = sec_covers_well(conn, "AAPL", today=date(2026, 7, 2))
    assert covered is False
    assert has_disagreement is True


def test_sec_covers_well_true_when_disagreement_resolved() -> None:
    conn = sqlite3.connect(":memory:")
    _create_schema(conn)
    _insert_fact(conn, "AAPL", "2026-06-30", "sec_xbrl")
    _insert_issue(conn, "AAPL", resolved=True)
    covered, _, has_disagreement = sec_covers_well(conn, "AAPL", today=date(2026, 7, 2))
    assert covered is True
    assert has_disagreement is False


def test_build_manifest_skips_overlap_jobs_when_covered() -> None:
    conn = sqlite3.connect(":memory:")
    _create_schema(conn)
    _insert_fact(conn, "AAPL", "2026-06-30", "sec_xbrl")
    manifest, decisions = build_manifest(conn, [("AAPL", "portfolio")], today=date(2026, 7, 2))
    assert decisions[0].sec_covered is True
    assert decisions[0].jobs_skipped_via_edgar == len(SEC_OVERLAP_KEYS)
    manifest_keys = {(m["endpoint"], m["period"]) for m in manifest}
    assert manifest_keys.isdisjoint(SEC_OVERLAP_KEYS)


def test_build_manifest_includes_overlap_jobs_when_not_covered() -> None:
    conn = sqlite3.connect(":memory:")
    _create_schema(conn)
    manifest, decisions = build_manifest(conn, [("FLKR", "etf")], today=date(2026, 7, 2))
    assert decisions[0].sec_covered is False
    assert decisions[0].jobs_skipped_via_edgar == 0
    manifest_keys = {(m["endpoint"], m["period"]) for m in manifest}
    assert SEC_OVERLAP_KEYS.issubset(manifest_keys)


def test_build_manifest_always_includes_non_overlap_jobs() -> None:
    """Segments/growth/TTM/etc. are never skipped, regardless of EDGAR coverage —
    EDGAR structurally cannot provide them."""
    conn = sqlite3.connect(":memory:")
    _create_schema(conn)
    _insert_fact(conn, "AAPL", "2026-06-30", "sec_xbrl")
    manifest, decisions = build_manifest(conn, [("AAPL", "portfolio")], today=date(2026, 7, 2))
    manifest_keys = {(m["endpoint"], m["period"]) for m in manifest}
    assert ("stock-peers", "") in manifest_keys
    assert ("profile", "") in manifest_keys
    assert decisions[0].jobs_included < decisions[0].jobs_total
    assert decisions[0].jobs_included == decisions[0].jobs_total - len(SEC_OVERLAP_KEYS)


def test_explicit_scope_rejects_ineligible_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    import fmp_backpop as bulk
    import pytest

    monkeypatch.setattr(
        bulk,
        "_tracked_for_user",
        Mock(
            return_value=[
                SimpleNamespace(ticker="AAPL", list_type=SimpleNamespace(value="portfolio"))
            ]
        ),
    )
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE tracked_companies(ticker TEXT, list_type TEXT)")
    conn.execute("INSERT INTO tracked_companies VALUES ('OTHER', 'portfolio')")
    with pytest.raises(ValueError, match="eligible"):
        getattr(bulk, "_resolve_tickers")(conn, ["OTHER"])


def test_child_commands_keep_database_root_and_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import fmp_backpop as bulk

    commands: list[list[str]] = []

    def run(cmd: list[str], **kwargs: object) -> object:
        commands.append(cmd)
        return Mock(returncode=0)

    monkeypatch.setattr(
        bulk.subprocess,
        "run",
        run,
    )
    database = tmp_path / "retained.db"
    manifest = tmp_path / "manifest.json"
    getattr(bulk, "_run_fetch")(
        manifest,
        7,
        tmp_path,
        database,
        [("AAPL", "portfolio")],
        tmp_path / "receipts.json",
        "opaque-proof",
    )
    getattr(bulk, "_run_extract")(tmp_path, database, [("AAPL", "portfolio")])
    for cmd in commands:
        assert cmd[cmd.index("--db") + 1] == str(database)
        assert cmd[cmd.index("--repo-root") + 1] == str(tmp_path)
        assert cmd[cmd.index("--tickers") + 1] == "AAPL"
        assert "--all" not in cmd


def test_nonpositive_budget_fails_before_database_or_artifacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import fmp_backpop as bulk
    import pytest

    monkeypatch.setattr(
        sys, "argv", ["fmp_backpop", "--max-calls", "0", "--repo-root", str(tmp_path)]
    )
    monkeypatch.setattr(
        bulk, "require_db_path", Mock(side_effect=AssertionError("database resolved"))
    )
    with pytest.raises(SystemExit) as exc:
        bulk.main()
    assert exc.value.code == 2
    assert not (tmp_path / ".tmp").exists()


def test_missing_database_fails_before_artifacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import fmp_backpop as bulk
    import pytest

    monkeypatch.setattr(
        sys,
        "argv",
        ["fmp_backpop", "--db", str(tmp_path / "missing.db"), "--repo-root", str(tmp_path)],
    )
    with pytest.raises(SystemExit) as exc:
        bulk.main()
    assert exc.value.code == 2
    assert not (tmp_path / ".tmp").exists()
    assert not (tmp_path / "missing.db").exists()


def test_apply_stops_after_fetch_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from contextlib import nullcontext

    import fmp_backpop as bulk

    conn = sqlite3.connect(":memory:")
    _create_schema(conn)
    monkeypatch.setattr(
        sys, "argv", ["fmp_backpop", "--apply", "--max-calls", "3", "--repo-root", str(tmp_path)]
    )
    monkeypatch.setattr(bulk, "require_db_path", Mock(return_value=tmp_path / "retained.db"))
    roles: list[object] = []

    def read(path: Path, *, role: object) -> sqlite3.Connection:
        roles.append(role)
        return conn

    monkeypatch.setattr(bulk, "connect_sqlite", read)
    monkeypatch.setattr(bulk, "_resolve_tickers", Mock(return_value=[("AAPL", "portfolio")]))
    locks: list[Path] = []

    def lock(path: Path, **kwargs: object) -> object:
        locks.append(path)
        return nullcontext()

    monkeypatch.setattr(bulk, "hold_run_lock", lock)
    monkeypatch.setattr(bulk, "_run_fetch", Mock(return_value=7))
    monkeypatch.setattr(
        bulk, "open_db", Mock(side_effect=AssertionError("writer opened after failed fetch"))
    )
    monkeypatch.setattr(
        bulk, "_run_extract", Mock(side_effect=AssertionError("extract started after failed fetch"))
    )
    assert bulk.main() == 7
    assert locks == []
    assert roles == [bulk.SQLiteConnectionRole.READ_ONLY]
    assert len(list((tmp_path / ".tmp/fmp_backpop").glob("manifest_*.json"))) == 1


def test_receipts_verify_bytes_and_keep_empty_as_gap(tmp_path: Path) -> None:
    import hashlib
    import json
    from datetime import UTC, datetime, timedelta

    import fmp_backpop as bulk
    import pytest

    captured = datetime.now(UTC).replace(tzinfo=None)
    artifact = tmp_path / "data/historical/fmp/AAPL_profile.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text('[{"symbol":"AAPL"}]')
    receipt_path = tmp_path / "receipts.json"
    receipts = [
        dict(
            ticker="AAPL",
            endpoint="profile",
            period="",
            outcome="success",
            captured_at=captured.isoformat(),
            http_status=200,
            file_path=str(artifact.relative_to(tmp_path)),
            content_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
        ),
        dict(
            ticker="AAPL",
            endpoint="stock-peers",
            period="",
            outcome="empty",
            captured_at=captured.isoformat(),
            http_status=200,
        ),
    ]
    manifest = [
        {"ticker": "AAPL", "endpoint": endpoint, "period": ""}
        for endpoint in ("profile", "stock-peers")
    ]
    receipt_path.write_text(json.dumps(receipts))
    summary = getattr(bulk, "_reconcile_fetch_receipts")(
        receipt_path, manifest, tmp_path, captured - timedelta(seconds=1)
    )
    assert summary["jobs_succeeded"] == 1
    assert summary["jobs_unavailable"] == 1
    assert summary["acquisition_status"] == "partial_unavailable"
    assert summary["archive_completeness"] == "unverified"
    other = artifact.with_name("OTHER_profile.json")
    other.write_bytes(artifact.read_bytes())
    receipts[0]["file_path"] = str(other.relative_to(tmp_path))
    receipt_path.write_text(json.dumps(receipts))
    with pytest.raises(ValueError, match="catalog"):
        getattr(bulk, "_reconcile_fetch_receipts")(
            receipt_path, manifest, tmp_path, captured - timedelta(seconds=1)
        )
    receipts[0]["file_path"] = str(artifact.relative_to(tmp_path))
    receipt_path.write_text(json.dumps(receipts))
    artifact.write_text("changed")
    with pytest.raises(ValueError, match="hash"):
        getattr(bulk, "_reconcile_fetch_receipts")(
            receipt_path, manifest, tmp_path, captured - timedelta(seconds=1)
        )


def test_zero_exit_without_receipts_stops_before_index(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from contextlib import nullcontext

    import fmp_backpop as bulk

    conn = sqlite3.connect(":memory:")
    _create_schema(conn)
    monkeypatch.setattr(
        sys, "argv", ["fmp_backpop", "--apply", "--max-calls", "3", "--repo-root", str(tmp_path)]
    )
    monkeypatch.setattr(bulk, "require_db_path", Mock(return_value=tmp_path / "retained.db"))
    monkeypatch.setattr(bulk, "connect_sqlite", Mock(return_value=conn))
    monkeypatch.setattr(bulk, "_resolve_tickers", Mock(return_value=[("AAPL", "portfolio")]))
    monkeypatch.setattr(bulk, "hold_run_lock", Mock(return_value=nullcontext()))
    monkeypatch.setattr(bulk, "_run_fetch", Mock(return_value=0))
    monkeypatch.setattr(
        bulk, "open_db", Mock(side_effect=AssertionError("writer opened without receipts"))
    )
    assert bulk.main() == 1


def test_receipt_reconciliation_rejects_missing_duplicate_and_failed_jobs(tmp_path: Path) -> None:
    import json
    from datetime import UTC, datetime, timedelta

    import fmp_backpop as bulk
    import pytest

    now = datetime.now(UTC).replace(tzinfo=None)
    path = tmp_path / "receipts.json"
    manifest = [{"ticker": "AAPL", "endpoint": "profile", "period": ""}]
    empty = dict(**manifest[0], outcome="empty", http_status=200, captured_at=now.isoformat())
    for rows in (
        [],
        [empty, empty],
        [dict(empty, ticker="OTHER")],
        [dict(empty, outcome="budget_not_attempted", http_status=None)],
        [dict(empty, captured_at=(now - timedelta(days=1)).isoformat())],
    ):
        path.write_text(json.dumps(rows))
        with pytest.raises(ValueError):
            getattr(bulk, "_reconcile_fetch_receipts")(
                path, manifest, tmp_path, now - timedelta(seconds=1)
            )


def test_index_failure_stops_extract(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from contextlib import nullcontext

    import fmp_backpop as bulk
    import pytest

    conn = sqlite3.connect(":memory:")
    _create_schema(conn)
    writer = sqlite3.connect(":memory:")
    monkeypatch.setattr(
        sys, "argv", ["fmp_backpop", "--apply", "--max-calls", "3", "--repo-root", str(tmp_path)]
    )
    monkeypatch.setattr(bulk, "require_db_path", Mock(return_value=tmp_path / "retained.db"))
    monkeypatch.setattr(bulk, "connect_sqlite", Mock(return_value=conn))
    monkeypatch.setattr(bulk, "_resolve_tickers", Mock(return_value=[("AAPL", "portfolio")]))
    monkeypatch.setattr(bulk, "hold_run_lock", Mock(return_value=nullcontext()))
    monkeypatch.setattr(bulk, "_run_fetch", Mock(return_value=0))
    monkeypatch.setattr(bulk, "_reconcile_fetch_receipts", Mock(return_value={}))
    monkeypatch.setattr(bulk, "open_db", Mock(return_value=writer))

    def fail_index(*args: object) -> None:
        from runtime.job_runtime import JobAlreadyRunningError, JobLock

        with (
            pytest.raises(JobAlreadyRunningError),
            JobLock(tmp_path, "independent-fetch", ["fmp-fetch-artifacts"], wait_s=0),
        ):
            pytest.fail("independent fetch entered during index")
        raise RuntimeError("synthetic index failure")

    monkeypatch.setattr(bulk, "_run_index", fail_index)
    monkeypatch.setattr(
        bulk, "_run_extract", Mock(side_effect=AssertionError("extract ran after index failed"))
    )
    with pytest.raises(RuntimeError, match="synthetic index"):
        bulk.main()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        writer.execute("SELECT 1")
    assert not list((tmp_path / ".tmp/job_locks").glob("*.lock"))


def test_explicit_peer_scope_preserves_existing_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    import fmp_backpop as bulk

    from models.companies import ListType

    roster = Mock(return_value=[SimpleNamespace(ticker="MSFT", list_type=ListType.INDEX_MEMBER)])
    monkeypatch.setattr(bulk, "_tracked_for_user", roster)
    conn = sqlite3.connect(":memory:")
    _create_schema(conn)
    tickers = getattr(bulk, "_resolve_tickers")(conn, ["MSFT"])
    assert tickers == [("MSFT", "index_member")]
    manifest, _ = bulk.build_manifest(conn, tickers)
    jobs = bulk.fmp_save.per_ticker_jobs("MSFT", list_type="index_member")
    assert {(r["endpoint"], r["period"]) for r in manifest} == {
        (j["path"], j["period"]) for j in jobs
    }
    assert roster.call_args.kwargs["list_types"] == frozenset(ListType)
    getattr(bulk, "_resolve_tickers")(conn, None)
    assert roster.call_args.kwargs["list_types"] == bulk.ANALYZED_LIST_TYPES


def test_exact_extract_scope_keeps_peer_without_expanding(monkeypatch: pytest.MonkeyPatch) -> None:
    import argparse
    from types import SimpleNamespace

    import extract_facts

    from models.companies import ListType

    monkeypatch.setattr(
        extract_facts,
        "tracked_companies_for_user",
        Mock(
            return_value=[
                SimpleNamespace(ticker="MSFT", list_type=ListType.INDEX_MEMBER),
                SimpleNamespace(ticker="AAPL", list_type=ListType.PORTFOLIO),
            ]
        ),
    )
    args = argparse.Namespace(tickers="MSFT,MSFT", ticker=None, include_index_members=False)
    conn = sqlite3.connect(":memory:")
    assert getattr(extract_facts, "_resolve_tickers")(conn, args) == ["MSFT"]
