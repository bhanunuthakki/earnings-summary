"""Pinned DCF row binding for the report read snapshot.

The generation plan lets a caller pin the report's valuation basis to one
exact ``dcf_runs.id`` via ``model_run_id``. That id must constrain BOTH the
snapshot valuation card and the per-metric provenance to the same row, and an
explicit id that is absent — or belongs to another issuer — must never fall
back to the latest run. ``None`` keeps the historical latest-run semantics.

Fixtures use the session-cached ``migrated_db`` template (the actual current
schema) and the real persist owner (``dcf.persist.persist_run``) — no bespoke
``dcf_runs``/``tracked_companies`` DDL, no per-test migrations, no FK
disables. The end-to-end tests drive ``_build_one`` → ``build_report`` →
``render_report_body`` for real: a WAL second connection commits a newer DCF
run plus changed supporting-panel data AFTER the reader snapshot is
established but BEFORE panel rendering, proving the writer commit succeeds
mid-read while every rendered read stays on the pinned original snapshot.
Only external acquisition/LLM side effects are replaced with asserted
no-effect doubles — the renderer's panel reading is never mocked away.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from db_paths import db_path_context
from dcf.persist import DcfRunRow, persist_run
from dcf.provenance import DcfInputProvenance
from report.builder import build_report
from report.renderers import workspace_html
from report.sections import snapshot as snapshot_section

PROJECT_ROOT = Path(__file__).resolve().parents[1]

ORIGINAL_TARGET_NOTE = "SNAPSHOT-ORIGINAL strategic commitment"
WAL_TARGET_NOTE = "WAL-WRITER mid-read commitment"


def _no_suppressed_sections(_ticker: str, _repo_root: Path) -> frozenset[str]:
    return frozenset()


def _no_sync(_ticker: str) -> None:
    return None


def _no_bypass(*_args: object, **_kwargs: object) -> bool:
    return False


def _load_build_artifacts() -> Any:
    """Load execution/build_artifacts.py as an isolated module (no sys.argv)."""
    src = PROJECT_ROOT / "execution" / "build_artifacts.py"
    spec = importlib.util.spec_from_file_location("build_artifacts_pinned_snapshot_test", src)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["build_artifacts_pinned_snapshot_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def builder(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Freshly-exec'd build_artifacts module with restorable db-module attrs."""
    module = _load_build_artifacts()
    for name in ("PROJECT_ROOT", "STATE_ROOT", "DB_PATH", "DATA_DIR", "FMP_DIR"):
        monkeypatch.setattr(module.db, name, getattr(module.db, name))
    return module


def _synthetic_provenance(ticker: str, valuation_date: date) -> DcfInputProvenance:
    """An explicitly synthetic receipt that satisfies the promotion guard.

    persist_run's promotion gate requires a candidate equity-bridge receipt of
    'verified' (or 'not_applicable') before a row may become current — the same
    convention as tests/test_dcf_persist_provenance.py::_row. The digests are
    deterministic per (ticker, valuation_date) so repeated seeds stay distinct
    under the exact-reuse comparison; the engine version is clearly synthetic
    and claims no real issuer, workbook, or decision grade.
    """
    return DcfInputProvenance(
        input_sha256=hashlib.sha256(
            f"synthetic-fixture:{ticker.upper()}:{valuation_date.isoformat()}".encode()
        ).hexdigest(),
        workbook_sha256=hashlib.sha256(
            f"synthetic-fixture-workbook:{ticker.upper()}:{valuation_date.isoformat()}".encode()
        ).hexdigest(),
        engine_version="synthetic_test_fixture_v1",
        inputs_as_of=datetime(
            valuation_date.year, valuation_date.month, valuation_date.day, 12, 0, tzinfo=UTC
        ),
        detail={
            "equity_bridge_receipt": {"status": "verified"},
            "sources": [],
            "market_price": None,
        },
    )


def _seed_dcf_run(
    db: Path,
    *,
    ticker: str,
    valuation_date: date,
    npv_per_share: float,
    live_price: float | None = None,
) -> int:
    """Persist one dcf_runs version through the real persist owner."""
    row = DcfRunRow(
        ticker=ticker.upper(),
        valuation_date=valuation_date,
        horizon_years=10,
        wacc=0.09,
        npv=1_000.0,
        npv_per_share=npv_per_share,
        shares_outstanding=10_000_000.0,
        currency="USD",
        live_price=live_price,
        live_price_at=(
            datetime(
                valuation_date.year, valuation_date.month, valuation_date.day, 12, 0, tzinfo=UTC
            )
            if live_price is not None
            else None
        ),
        mos_bar_used=0.2,
        assumption_snapshot_json='{"wacc":0.09}',
        notes="synthetic test fixture (pinned-snapshot suite)",
        provenance=_synthetic_provenance(ticker, valuation_date),
    )
    conn = sqlite3.connect(str(db))
    try:
        result = persist_run(conn, row)
        conn.commit()
        return result.revision.row_id
    finally:
        conn.close()


def _seed_strategic_target(db: Path, *, narrative: str) -> None:
    """One strategic_targets row the company-tab panel renders verbatim."""
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO strategic_targets"
            "(ticker, target_kind, target_value, target_unit, target_period,"
            " narrative_excerpt, extracted_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("TEST", "revenue_cagr", 5.0, "pct", "5y", narrative, "2026-01-31T00:00:00"),
        )
        conn.commit()
    finally:
        conn.close()


def _repo_with_db(migrated_db: Callable[..., Path], base: Path) -> tuple[Path, Path]:
    repo = base / "repo"
    db = migrated_db(repo / "data" / "portfolio.db")
    return repo, db


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    return conn


# --------------------------------------------------------------------------- #
# Explicit id validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad_id", [0, -1, True])
def test_non_positive_or_bool_model_run_id_is_rejected(tmp_path: Path, bad_id: int) -> None:
    with pytest.raises(ValueError):
        snapshot_section.build_per_metric("TEST", tmp_path, model_run_id=bad_id)


# --------------------------------------------------------------------------- #
# Valuation card pinning
# --------------------------------------------------------------------------- #


def test_pinned_valuation_selects_exact_run_not_latest(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    repo, db = _repo_with_db(migrated_db, tmp_path)
    old_id = _seed_dcf_run(
        db,
        ticker="TEST",
        valuation_date=date(2026, 1, 31),
        npv_per_share=100.0,
        live_price=42.0,
    )
    _seed_dcf_run(db, ticker="TEST", valuation_date=date(2026, 6, 30), npv_per_share=175.0)

    conn = _connect(db)
    try:
        section = snapshot_section.build("TEST", repo, None, conn=conn, model_run_id=old_id)
    finally:
        conn.close()

    # The column is VARCHAR(10) but ValuationSnapshot.valuation_date is a
    # pydantic `date` — the coerced calendar date is the actual value.
    assert section.valuation.valuation_date == date(2026, 1, 31)
    assert section.valuation.consolidated_npv_per_share == 100.0
    # The bound row's retained live price is the price context, not current latest.
    assert section.valuation.current_price == 42.0


def test_unpinned_valuation_keeps_latest_semantics(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    repo, db = _repo_with_db(migrated_db, tmp_path)
    _seed_dcf_run(db, ticker="TEST", valuation_date=date(2026, 1, 31), npv_per_share=100.0)
    _seed_dcf_run(db, ticker="TEST", valuation_date=date(2026, 1, 31), npv_per_share=100.0)
    _seed_dcf_run(db, ticker="TEST", valuation_date=date(2026, 6, 30), npv_per_share=175.0)

    conn = _connect(db)
    try:
        section = snapshot_section.build("TEST", repo, None, conn=conn)
    finally:
        conn.close()

    assert section.valuation.valuation_date == date(2026, 6, 30)
    assert section.valuation.consolidated_npv_per_share == 175.0


def test_absent_pinned_id_never_falls_back_to_latest(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    repo, db = _repo_with_db(migrated_db, tmp_path)
    _seed_dcf_run(db, ticker="TEST", valuation_date=date(2026, 6, 30), npv_per_share=175.0)

    conn = _connect(db)
    try:
        section = snapshot_section.build("TEST", repo, None, conn=conn, model_run_id=99999)
    finally:
        conn.close()

    assert section.valuation.valuation_date is None
    assert section.valuation.consolidated_npv_per_share is None


def test_pinned_id_for_other_issuer_never_matches(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    repo, db = _repo_with_db(migrated_db, tmp_path)
    other_id = _seed_dcf_run(
        db, ticker="OTHER", valuation_date=date(2026, 6, 30), npv_per_share=175.0
    )
    _seed_dcf_run(db, ticker="TEST", valuation_date=date(2026, 6, 30), npv_per_share=50.0)

    conn = _connect(db)
    try:
        section = snapshot_section.build("TEST", repo, None, conn=conn, model_run_id=other_id)
    finally:
        conn.close()

    assert section.valuation.consolidated_npv_per_share is None


# --------------------------------------------------------------------------- #
# Per-metric provenance pinning
# --------------------------------------------------------------------------- #


def test_pinned_per_metric_matches_exact_run(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    repo, db = _repo_with_db(migrated_db, tmp_path)
    old_id = _seed_dcf_run(db, ticker="TEST", valuation_date=date(2026, 1, 31), npv_per_share=150.0)
    _seed_dcf_run(db, ticker="TEST", valuation_date=date(2026, 6, 30), npv_per_share=175.0)

    conn = _connect(db)
    try:
        per_metric = snapshot_section.build_per_metric("TEST", repo, conn=conn, model_run_id=old_id)
    finally:
        conn.close()

    assert per_metric["valuation"]["fact_id"] == old_id
    # build_per_metric surfaces the raw column text, not the coerced date.
    assert per_metric["valuation"]["valuation_date"] == "2026-01-31"


def test_absent_pinned_id_per_metric_is_empty(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    repo, db = _repo_with_db(migrated_db, tmp_path)
    _seed_dcf_run(db, ticker="TEST", valuation_date=date(2026, 6, 30), npv_per_share=175.0)

    conn = _connect(db)
    try:
        per_metric = snapshot_section.build_per_metric("TEST", repo, conn=conn, model_run_id=99999)
    finally:
        conn.close()

    assert per_metric == {}


def test_default_per_metric_keeps_latest_run(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    repo, db = _repo_with_db(migrated_db, tmp_path)
    _seed_dcf_run(db, ticker="TEST", valuation_date=date(2026, 1, 31), npv_per_share=100.0)
    latest_id = _seed_dcf_run(
        db, ticker="TEST", valuation_date=date(2026, 6, 30), npv_per_share=175.0
    )

    conn = _connect(db)
    try:
        per_metric = snapshot_section.build_per_metric("TEST", repo, conn=conn)
    finally:
        conn.close()

    assert per_metric["valuation"]["fact_id"] == latest_id


# --------------------------------------------------------------------------- #
# Caller-owned connection lifetime
# --------------------------------------------------------------------------- #


def test_caller_connection_stays_open_after_build(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    repo, db = _repo_with_db(migrated_db, tmp_path)
    run_id = _seed_dcf_run(db, ticker="TEST", valuation_date=date(2026, 6, 30), npv_per_share=175.0)

    conn = _connect(db)
    try:
        snapshot_section.build("TEST", repo, None, conn=conn, model_run_id=run_id)
        snapshot_section.build_per_metric("TEST", repo, conn=conn, model_run_id=run_id)
        assert conn.execute("SELECT 1").fetchone() is not None
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# render_report_body database-authority guard
# --------------------------------------------------------------------------- #


def test_render_report_body_rejects_connection_from_other_database(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_a, db_a = _repo_with_db(migrated_db, tmp_path / "a")
    _, db_b = _repo_with_db(migrated_db, tmp_path / "b")

    monkeypatch.setattr("report.builder.suppressed_sections_for_ticker", _no_suppressed_sections)
    spec = build_report("TEST", repo_a, db_path=db_a)
    assert spec.db_path == str(db_a)

    def bomb(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("panel loader ran despite an authority mismatch")

    monkeypatch.setattr(workspace_html, "load_workspace_p3_panels", bomb)

    conn_b = _connect(db_b)
    conn_b.execute("BEGIN")
    try:
        with pytest.raises(ValueError, match="Report connection and database path disagree"):
            workspace_html.render_report_body(spec, conn=conn_b)
        # The rejection precedes any panel read and preserves the caller's
        # explicit transaction and row factory.
        assert conn_b.in_transaction
        assert conn_b.row_factory is sqlite3.Row
        assert conn_b.execute("SELECT 1").fetchone()[0] == 1
    finally:
        conn_b.rollback()
        conn_b.close()


def test_render_report_body_rejects_transient_connection_for_disk_spec(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A :memory: main file PRAGMAs to an empty path and must be rejected.

    The naive `if connected` shape let a named-disk spec accept an unrelated
    transient connection because its PRAGMA main filename is empty. The
    rejection must precede comment/panel reads and preserve the caller's
    explicit transaction and row factory.
    """
    repo, db = _repo_with_db(migrated_db, tmp_path)

    monkeypatch.setattr("report.builder.suppressed_sections_for_ticker", _no_suppressed_sections)
    spec = build_report("TEST", repo, db_path=db)
    assert spec.db_path == str(db)

    def bomb(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("panel loader ran despite a transient connection")

    monkeypatch.setattr(workspace_html, "load_workspace_p3_panels", bomb)
    monkeypatch.setattr(workspace_html, "_comment_boot_data", bomb, raising=True)

    transient = sqlite3.connect(":memory:")
    transient.row_factory = sqlite3.Row
    transient.execute("BEGIN")
    try:
        with pytest.raises(ValueError, match="no main database file"):
            workspace_html.render_report_body(spec, conn=transient)
        # Rejection happened before any panel read; the caller's explicit
        # transaction and row factory are preserved and the handle usable.
        assert transient.in_transaction
        assert transient.row_factory is sqlite3.Row
        assert transient.execute("SELECT 1").fetchone()[0] == 1
    finally:
        transient.rollback()
        transient.close()


def test_render_report_body_accepts_matching_connection(
    migrated_db: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, db = _repo_with_db(migrated_db, tmp_path)
    monkeypatch.setattr("report.builder.suppressed_sections_for_ticker", _no_suppressed_sections)
    spec = build_report("TEST", repo, db_path=db)

    conn = _connect(db)
    try:
        body = workspace_html.render_report_body(spec, conn=conn)
        assert body is not None
        # A matching caller-owned connection survives the render usable.
        assert conn.execute("SELECT 1").fetchone()[0] == 1
        assert conn.row_factory is sqlite3.Row
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# _build_one integration: WAL writer-during-read stays on the pinned snapshot
# --------------------------------------------------------------------------- #


def test_build_one_keeps_pinned_snapshot_when_wal_writer_commits_mid_read(
    builder: Any,
    migrated_db: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, db = _repo_with_db(migrated_db, tmp_path)

    # WAL persists in the database header, so the mode=ro reader below and a
    # plain second writer connection can overlap: the writer commits while
    # the reader's snapshot stays pinned to its BEGIN point.
    wal_conn = sqlite3.connect(str(db))
    try:
        wal_conn.execute("PRAGMA journal_mode=WAL")
        wal_conn.commit()
    finally:
        wal_conn.close()

    old_id = _seed_dcf_run(
        db,
        ticker="TEST",
        valuation_date=date(2026, 1, 31),
        npv_per_share=100.0,
        live_price=42.0,
    )
    _seed_strategic_target(db, narrative=ORIGINAL_TARGET_NOTE)

    # Only external acquisition / LLM side effects are doubled away; the
    # sync, build, per-metric, and render paths below all run for real.
    monkeypatch.setattr("report.builder.suppressed_sections_for_ticker", _no_suppressed_sections)
    monkeypatch.setattr(builder.db, "scan_and_sync_artifacts", _no_sync)
    monkeypatch.setattr(builder.ticker_settings, "get_bypass_budget", _no_bypass)

    real_connect_sqlite = builder.connect_sqlite
    real_collect = builder._collect_per_metric_provenance
    opened_readers: list[sqlite3.Connection] = []
    mid_read: dict[str, int] = {}

    def tracking_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        conn = real_connect_sqlite(*args, **kwargs)
        if kwargs.get("role") is builder.SQLiteConnectionRole.READ_ONLY:
            opened_readers.append(conn)
        return conn

    def collect_with_mid_read_writer(
        ticker: str,
        repo_root: Path,
        *,
        conn: sqlite3.Connection | None = None,
        model_run_id: int | None = None,
    ) -> dict[str, dict[str, object]]:
        assert isinstance(conn, sqlite3.Connection)
        # The reader snapshot is established (report_read_snapshot's BEGIN)
        # before per-metric collection; render has not run yet.
        assert conn.in_transaction, "reader snapshot must be open at the per-metric seam"
        writer = sqlite3.connect(str(db))
        try:
            newer_id = persist_run(
                writer,
                DcfRunRow(
                    ticker="TEST",
                    valuation_date=date(2026, 6, 30),
                    horizon_years=10,
                    wacc=0.09,
                    npv=1_100.0,
                    npv_per_share=175.0,
                    shares_outstanding=10_000_000.0,
                    currency="USD",
                    live_price=None,
                    live_price_at=None,
                    mos_bar_used=0.2,
                    assumption_snapshot_json='{"wacc":0.09}',
                    notes="synthetic mid-read WAL writer (pinned-snapshot suite)",
                    provenance=_synthetic_provenance("TEST", date(2026, 6, 30)),
                ),
            ).revision.row_id
            writer.execute(
                "UPDATE strategic_targets SET narrative_excerpt = ? WHERE ticker = ?",
                (WAL_TARGET_NOTE, "TEST"),
            )
            # The whole point: under WAL this commit SUCCEEDS while the
            # reader's snapshot is open (in rollback-journal mode it would
            # fail on the reader's shared lock after the busy timeout).
            writer.commit()
            mid_read["newer_id"] = newer_id
        finally:
            writer.close()
        return real_collect(ticker, repo_root, conn=conn, model_run_id=model_run_id)

    monkeypatch.setattr(builder, "connect_sqlite", tracking_connect)
    monkeypatch.setattr(builder, "_collect_per_metric_provenance", collect_with_mid_read_writer)

    with db_path_context(db):
        result = builder._build_one("TEST", repo, enable_llm=False, model_run_id=old_id)

    # The reader saw the ORIGINAL snapshot throughout: rendered panels carry
    # the pre-commit narrative, never the mid-read writer's version.
    html = Path(str(result["workspace_html"])).read_text(encoding="utf-8")
    assert ORIGINAL_TARGET_NOTE in html
    assert WAL_TARGET_NOTE not in html

    # Every READ_ONLY connection opened during the build was closed exactly
    # once (_resolve_kind's lookup + the report reader).
    assert opened_readers
    for conn in opened_readers:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

    check = sqlite3.connect(str(db))
    try:
        provenance = check.execute(
            "SELECT sources_used FROM brief_provenance_log "
            "WHERE ticker = ? ORDER BY id DESC LIMIT 1",
            ("TEST",),
        ).fetchone()
        assert provenance is not None
        sources = json.loads(provenance[0])
        # The logged per-metric provenance is the pinned old run, not the
        # newer run the WAL writer committed mid-read.
        assert sources["per_metric"]["valuation"]["fact_id"] == old_id
        latest = check.execute(
            "SELECT id FROM dcf_runs WHERE ticker = ? AND is_latest = 1", ("TEST",)
        ).fetchall()
        # The mid-read writer truly landed: its run is the durable latest.
        assert [row[0] for row in latest] == [mid_read["newer_id"]]
        excerpt = check.execute(
            "SELECT narrative_excerpt FROM strategic_targets WHERE ticker = ?", ("TEST",)
        ).fetchone()
        assert excerpt is not None and excerpt[0] == WAL_TARGET_NOTE
    finally:
        check.close()


# --------------------------------------------------------------------------- #
# _build_one integration: render failure closes the reader before propagating
# --------------------------------------------------------------------------- #


def test_build_one_closes_reader_and_writes_nothing_when_render_fails(
    builder: Any,
    migrated_db: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, db = _repo_with_db(migrated_db, tmp_path)

    monkeypatch.setattr("report.builder.suppressed_sections_for_ticker", _no_suppressed_sections)
    monkeypatch.setattr(builder.db, "scan_and_sync_artifacts", _no_sync)
    monkeypatch.setattr(builder.ticker_settings, "get_bypass_budget", _no_bypass)

    real_connect_sqlite = builder.connect_sqlite
    opened_readers: list[sqlite3.Connection] = []

    def tracking_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        conn = real_connect_sqlite(*args, **kwargs)
        if kwargs.get("role") is builder.SQLiteConnectionRole.READ_ONLY:
            opened_readers.append(conn)
        return conn

    def failing_render(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("synthetic render failure")

    monkeypatch.setattr(builder, "connect_sqlite", tracking_connect)
    monkeypatch.setattr(builder, "render_report_body", failing_render)

    with (
        db_path_context(db),
        pytest.raises(RuntimeError, match="synthetic render failure"),
    ):
        builder._build_one("TEST", repo, enable_llm=False)

    # The report reader is closed (exactly once) before the failure propagates.
    assert opened_readers
    for conn in opened_readers:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

    # Nothing downstream of the render was written.
    out_dir = repo / "output" / "research" / "TEST"
    assert not list(out_dir.glob("*_workspace.html"))
    assert not list(out_dir.glob("*_report.md"))
    assert not list(out_dir.glob("*_sections.json"))

    check = sqlite3.connect(str(db))
    try:
        logged = check.execute(
            "SELECT COUNT(*) FROM brief_provenance_log WHERE ticker = ?", ("TEST",)
        ).fetchone()
        assert logged is not None and logged[0] == 0
    finally:
        check.close()
