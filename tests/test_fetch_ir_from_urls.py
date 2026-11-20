"""Tests for execution/fetch_ir_from_urls.py — the browser-assisted URL fallback.

Bridges explicitly-provided document URLs (sourced by a real browser for sites the
headless crawler can't crack) into the manifest -> download+register -> anchor
pipeline. The download/categorize step (process_ticker) is mocked here; this guards
the wiring around it (manifest write, brief_dirty, status, URL dedup).
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path

import pytest

from execution import fetch_ir_from_urls as mod
from ir_pipeline.manifest import load_manifest
from provenance.source_inventory_seal import InventoryComponent, component_digest


def _make_db(db: Path, *, role: str = "portfolio", fiscal_year_end: str = "12-31") -> None:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE tracked_companies (ticker TEXT, list_type TEXT, archived_at TEXT, "
        "fiscal_year_end TEXT, brief_dirty INTEGER DEFAULT 0, instrument_type TEXT DEFAULT 'equity')"
    )
    conn.execute(
        "INSERT INTO tracked_companies (ticker, list_type, fiscal_year_end) VALUES ('NOW', ?, ?)",
        (role, fiscal_year_end),
    )
    conn.execute(
        "CREATE TABLE ir_fetch_status (ticker TEXT PRIMARY KEY, last_attempt_at TEXT, "
        "last_status TEXT, discovered INTEGER, downloaded INTEGER, reason TEXT, updated_at TEXT)"
    )
    conn.commit()
    conn.close()


def _noop(*_a: object, **_k: object) -> bool:
    return True


def _qualified_period(*_args: object, **_kwargs: object) -> bool:
    return True


def _excluded_period(*_args: object, **_kwargs: object) -> bool:
    return False


def _install_fake_process(
    monkeypatch: pytest.MonkeyPatch, *, downloaded: int, captured: dict[str, object]
) -> None:
    def _fake_process(
        ticker: str,
        *,
        root: Path,
        db_path: Path,
        categorize: bool,
        calendar: str | None,
        owner_requested: bool,
        fiscal_witness_document_version_id: str | None = None,
        fiscal_witness_urls: frozenset[str] = frozenset(),
    ) -> dict[str, object]:
        captured["calendar"] = calendar
        captured["categorize"] = categorize
        captured["root"] = root
        captured["owner_requested"] = owner_requested
        captured["fiscal_witness_document_version_id"] = fiscal_witness_document_version_id
        captured["fiscal_witness_urls"] = fiscal_witness_urls
        return {
            "ticker": ticker,
            "status": "done",
            "downloaded": downloaded,
            "registered": downloaded,
            "ready": downloaded,
            "failed": 0,
        }

    monkeypatch.setattr("execution.fetch_ir_from_urls.process_ticker", _fake_process)
    monkeypatch.setattr("execution.fetch_ir_from_urls._run_narrative", _noop)


def test_register_urls_writes_manifest_registers_and_anchors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "data" / "portfolio.db"
    _make_db(db)
    captured: dict[str, object] = {}
    _install_fake_process(monkeypatch, downloaded=2, captured=captured)
    monkeypatch.setattr(mod, "reported_quarter_selection_in_connection", _qualified_period)
    urls = [
        "https://s205.q4cdn.com/x/ER-Q1-FY26.pdf",
        "https://s205.q4cdn.com/x/ServiceNow-1Q26-Investor-Presentation.pdf",
    ]

    n = mod.register_urls(
        "now", urls, repo_root=tmp_path, db_path=db, year=2026, quarter=2, process=True
    )

    assert n == 2
    assert captured["categorize"] is True  # docs are content-classified, not trusted blindly
    assert captured["owner_requested"] is True
    assert captured["fiscal_witness_document_version_id"] is None
    assert captured["fiscal_witness_urls"] == frozenset()
    # The provided URLs are persisted to the canonical manifest.
    assert {e.url for e in load_manifest(tmp_path, "NOW")} == set(urls)
    conn = sqlite3.connect(str(db))
    brief_dirty = conn.execute(
        "SELECT brief_dirty FROM tracked_companies WHERE ticker='NOW'"
    ).fetchone()[0]
    status = conn.execute(
        "SELECT last_status, discovered, downloaded FROM ir_fetch_status WHERE ticker='NOW'"
    ).fetchone()
    conn.close()
    assert brief_dirty == 1  # queued for the daily --enable-llm rebuild
    assert status == ("ok", 2, 2)  # recovery surfaced in the dashboard's coverage tab


def test_register_urls_no_downloads_marks_failed_and_skips_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "data" / "portfolio.db"
    _make_db(db)
    captured: dict[str, object] = {}
    _install_fake_process(monkeypatch, downloaded=0, captured=captured)
    monkeypatch.setattr(mod, "reported_quarter_selection_in_connection", _qualified_period)

    n = mod.register_urls(
        "NOW",
        ["https://blocked.example/x.pdf"],
        repo_root=tmp_path,
        db_path=db,
        year=2026,
        quarter=2,
    )

    assert n == 0
    conn = sqlite3.connect(str(db))
    brief_dirty = conn.execute(
        "SELECT brief_dirty FROM tracked_companies WHERE ticker='NOW'"
    ).fetchone()[0]
    last_status = conn.execute(
        "SELECT last_status FROM ir_fetch_status WHERE ticker='NOW'"
    ).fetchone()[0]
    conn.close()
    assert brief_dirty == 0  # nothing landed → don't churn the brief
    assert last_status == "failed"


def test_collect_urls_dedups_args_and_file(tmp_path: Path) -> None:
    f = tmp_path / "urls.txt"
    f.write_text("# a comment\nhttps://x/b.pdf\nhttps://x/a.pdf\n\n", encoding="utf-8")
    urls = mod.collect_urls(["https://x/a.pdf"], f)
    assert urls == [
        "https://x/a.pdf",
        "https://x/b.pdf",
    ]  # arg first, file adds new, comment skipped


@pytest.mark.parametrize("role", ["none", "index_member"])
def test_approved_url_denial_never_crosses_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    db = tmp_path / "data" / "portfolio.db"
    _make_db(db, role=role)

    def fail_network(*_args: object, **_kwargs: object) -> None:
        pytest.fail("network boundary crossed")

    monkeypatch.setattr(mod, "process_ticker", fail_network)

    assert (
        mod.register_urls(
            "NOW",
            ["https://issuer.example/q2.pdf"],
            repo_root=tmp_path,
            db_path=db,
            year=2026,
            quarter=2,
        )
        == 0
    )
    assert not (tmp_path / ".tmp" / "ir_url_manifest" / "NOW_urls.json").exists()


def test_pending_source_window_retains_raw_without_catalog_or_brief(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "data" / "portfolio.db"
    _make_db(db)
    captured: dict[str, object] = {}
    _install_fake_process(monkeypatch, downloaded=1, captured=captured)
    url = "https://issuer.example/quarter.pdf"

    assert (
        mod.register_urls("NOW", [url], repo_root=tmp_path, db_path=db, year=2026, quarter=2) == 1
    )
    assert captured["categorize"] is False
    assert captured["owner_requested"] is True
    assert {entry.url for entry in load_manifest(tmp_path, "NOW")} == {url}
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT brief_dirty FROM tracked_companies WHERE ticker='NOW'"
        ).fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM ir_fetch_status").fetchone() == (0,)


def test_approved_url_rejects_period_outside_five_quarter_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "data" / "portfolio.db"
    _make_db(db)

    def fail_network(*_args: object, **_kwargs: object) -> None:
        pytest.fail("network boundary crossed")

    monkeypatch.setattr(mod, "process_ticker", fail_network)
    monkeypatch.setattr(mod, "reported_quarter_selection_in_connection", _excluded_period)

    assert (
        mod.register_urls(
            "NOW",
            ["https://issuer.example/old.pdf"],
            repo_root=tmp_path,
            db_path=db,
            year=2024,
            quarter=4,
        )
        == 0
    )


def _seed_sealed_sec_submissions(db: Path, *, ticker: str, recorded_at: str) -> None:
    comp = InventoryComponent(
        component_id="comp-sec-1",
        idempotency_key="comp-sec-1",
        snapshot_id="snap-sec-1",
        component_key="primary",
        component_kind="primary",
        source_url="https://data.sec.gov/submissions/CIK0001373715.json",
        source_observation_id="obs-sec-1",
        outcome="succeeded",
        required=True,
        ordinal=0,
        recorded_at=datetime.fromisoformat(recorded_at),
    )
    digest = component_digest((comp,))
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE v_source_inventory_current (
            snapshot_id TEXT, idempotency_key TEXT, inventory_key TEXT, revision INTEGER,
            issuer_id TEXT, ticker TEXT, source_kind TEXT, source_url TEXT,
            source_observation_id TEXT, outcome TEXT, authoritative INTEGER,
            retrieval_config_sha256 TEXT, collector_code_version TEXT,
            started_at TEXT, completed_at TEXT, recorded_at TEXT,
            supersedes_snapshot_id TEXT
        );
        CREATE TABLE source_inventory_snapshot_seals (
            snapshot_id TEXT PRIMARY KEY,
            expected_component_count INTEGER,
            component_digest_sha256 TEXT,
            completion_status TEXT,
            sealed_at TEXT
        );
        CREATE TABLE source_inventory_components (
            component_id TEXT, idempotency_key TEXT, snapshot_id TEXT,
            component_key TEXT, component_kind TEXT, source_url TEXT,
            source_observation_id TEXT, outcome TEXT, required INTEGER,
            failure_reason TEXT, ordinal INTEGER, recorded_at TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO v_source_inventory_current VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "snap-sec-1",
            "snap-sec-1",
            "sec_submissions:issuer-now",
            1,
            "issuer-now",
            ticker,
            "sec_submissions",
            "https://data.sec.gov/submissions/CIK0001373715.json",
            "obs-sec-1",
            "succeeded",
            1,
            "a" * 64,
            "sec_submissions@1",
            recorded_at,
            recorded_at,
            recorded_at,
            None,
        ),
    )
    conn.execute(
        "INSERT INTO source_inventory_snapshot_seals VALUES (?, 1, ?, 'complete', ?)",
        ("snap-sec-1", digest, recorded_at),
    )
    conn.execute(
        "INSERT INTO source_inventory_components VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            comp.component_id,
            comp.idempotency_key,
            comp.snapshot_id,
            comp.component_key,
            comp.component_kind,
            comp.source_url,
            comp.source_observation_id,
            comp.outcome,
            1,
            None,
            comp.ordinal,
            recorded_at,
        ),
    )
    conn.commit()
    conn.close()


def _install_direct_fake_process(
    monkeypatch: pytest.MonkeyPatch, *, captured: dict[str, object]
) -> None:
    def _fake_process(
        ticker: str,
        *,
        root: Path,
        db_path: Path,
        categorize: bool,
        calendar: str | None,
        owner_requested: bool,
        fiscal_witness_document_version_id: str | None = None,
        fiscal_witness_urls: frozenset[str] = frozenset(),
        direct_verified: bool = False,
        max_quarters: int = 5,
    ) -> dict[str, object]:
        captured["ticker"] = ticker
        captured["categorize"] = categorize
        captured["direct_verified"] = direct_verified
        captured["max_quarters"] = max_quarters
        return {
            "ticker": ticker,
            "status": "done",
            "downloaded": 1,
            "registered": 1,
            "ready": 1,
            "failed": 0,
        }

    monkeypatch.setattr("execution.fetch_ir_from_urls.process_ticker", _fake_process)
    monkeypatch.setattr("execution.fetch_ir_from_urls._run_narrative", _noop)


def _fixed_as_of() -> date:
    return date(2026, 11, 19)


def test_direct_verified_succeeds_with_sealed_sec_submissions_without_ir_crawl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "data" / "portfolio.db"
    _make_db(db)
    _seed_sealed_sec_submissions(db, ticker="NOW", recorded_at="2026-11-18T12:00:00+00:00")
    monkeypatch.setattr(mod, "current_utc_date", _fixed_as_of)
    captured: dict[str, object] = {}
    _install_direct_fake_process(monkeypatch, captured=captured)

    urls = ["https://s205.q4cdn.com/x/ER-Q3-FY26.pdf"]
    n = mod.register_urls(
        "NOW",
        urls,
        repo_root=tmp_path,
        db_path=db,
        year=2026,
        quarter=3,
        process=True,
        direct_verified=True,
        max_quarters=8,
    )
    assert n == 1
    assert captured["categorize"] is True
    assert captured["direct_verified"] is True
    assert captured["max_quarters"] == 8


def test_direct_verified_rejects_future_or_outside_window_and_holds_unsealed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "data" / "portfolio.db"
    _make_db(db)
    monkeypatch.setattr(mod, "current_utc_date", _fixed_as_of)

    # Without sealed sec_submissions or witness -> pending (categorize=False)
    captured: dict[str, object] = {}
    _install_direct_fake_process(monkeypatch, captured=captured)
    assert (
        mod.register_urls(
            "NOW",
            ["https://s205.q4cdn.com/x/q3.pdf"],
            repo_root=tmp_path,
            db_path=db,
            year=2026,
            quarter=3,
            direct_verified=True,
        )
        == 1
    )
    assert captured["categorize"] is False

    # Future period (Q4 2026 as of 2026-11-19) -> denied (0), never hits process_ticker
    def _fail_process(*_args: object, **_kwargs: object) -> None:
        pytest.fail("must not process future or out-of-window period")

    monkeypatch.setattr(mod, "process_ticker", _fail_process)
    assert (
        mod.register_urls(
            "NOW",
            ["https://s205.q4cdn.com/x/future.pdf"],
            repo_root=tmp_path,
            db_path=db,
            year=2026,
            quarter=4,
            direct_verified=True,
        )
        == 0
    )
    assert (
        mod.register_urls(
            "NOW",
            ["https://s205.q4cdn.com/x/old.pdf"],
            repo_root=tmp_path,
            db_path=db,
            year=2024,
            quarter=1,
            direct_verified=True,
            max_quarters=8,
        )
        == 0
    )
