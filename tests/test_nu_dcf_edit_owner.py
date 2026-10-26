from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import TracebackType
from typing import Any

import build_nu_platform_dcf as nu
import openpyxl
import pytest

from dcf import persist as persist_mod
from dcf.artifact_promotion import ArtifactPromotion, StagedFilePromotion


def _write_owner_inputs(root: Path, *, price: float = 14.25, tax: float = 0.31) -> None:
    assumptions = root / "data" / "bank_assumptions" / "NU_platform.json"
    assumptions.parent.mkdir(parents=True, exist_ok=True)
    assumptions.write_text(json.dumps({"price": price, "tax": tax}), encoding="utf-8")

    profile = root / "data" / "historical" / "fmp" / "NU_profile.json"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text(json.dumps({"price": price}), encoding="utf-8")

    holdings = root / "micro_thesis" / "holdings" / "NU.json"
    holdings.parent.mkdir(parents=True, exist_ok=True)
    holdings.write_text(json.dumps({"mos_bar": 0.2}), encoding="utf-8")


def _prepared_owner(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> tuple[Path, Path, Path, nu.Assum, nu.Mirror]:
    root = tmp_path / "owner-root"
    _write_owner_inputs(root)
    db = migrated_db(tmp_path / "owner-state" / "portfolio.db")
    workbook = root / "dcf" / "NU.actual.xlsx"
    workbook.parent.mkdir(parents=True, exist_ok=True)
    s = nu.load_assumptions("NU", repo_root=root, db_path=db)
    m = nu.mirror(s)
    nu.build(s, m, workbook, ticker="NU")
    return root, db, workbook, s, m


def test_explicit_owner_authority_overrides_ambient_globals_and_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: Callable[..., Path],
) -> None:
    root = tmp_path / "explicit-root"
    ambient = tmp_path / "ambient-root"
    _write_owner_inputs(root, price=14.25, tax=0.31)
    _write_owner_inputs(ambient, price=99.0, tax=0.05)
    db = migrated_db(tmp_path / "explicit-state" / "portfolio.db")
    ambient_db = migrated_db(tmp_path / "ambient-state" / "portfolio.db")

    monkeypatch.setattr(nu, "REPO", ambient)
    monkeypatch.setattr(nu, "T", "ZZZ")
    monkeypatch.setattr(nu, "DEST", ambient / "dcf" / "ZZZ.xlsx")
    monkeypatch.setenv("DCF_REPO_ROOT", str(ambient))
    monkeypatch.setenv("DCF_TICKER", "ZZZ")
    monkeypatch.setenv("DCF_DEST", str(ambient / "dcf" / "ZZZ.xlsx"))
    monkeypatch.setenv("DCF_PROMOTE_DEST", str(ambient / "dcf" / "promoted.xlsx"))
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(ambient_db))

    s = nu.load_assumptions("NU", repo_root=root, db_path=db)
    assert s.price == 14.25
    assert s.tax == 0.31
    assert s.price_seed_path == "data/historical/fmp/NU_profile.json"

    m = nu.mirror(s)
    staged = root / "dcf" / "NU.staged.xlsx"
    live = root / "dcf" / "NU.live.xlsx"
    staged.parent.mkdir(parents=True, exist_ok=True)
    live.write_bytes(b"prior live bytes the promotion must replace")
    nu.build(s, m, staged, ticker="NU")
    staged_bytes = staged.read_bytes()

    wb = openpyxl.load_workbook(staged, data_only=False)
    try:
        assert wb["Dashboard"]["A1"].value == "NU - Customer-Driven Platform DCF | Dashboard"
        assert wb["Model"]["A1"].value == "NU - Platform engine (formula-first; $M)"
        assert wb["Valuation"]["A1"].value == "NU - Valuation ($M)"
    finally:
        wb.close()

    receipt = nu.persist_dcf_run_with_receipt(
        s,
        m,
        ticker="NU",
        repo_root=root,
        db_path=db,
        workbook_path=staged,
        workbook_locator_path=live,
        artifact_promotion=StagedFilePromotion(staged, live),
    )
    assert receipt is not None
    assert receipt.written is True
    assert receipt.revision.ticker == "NU"
    assert receipt.revision.workbook_sha256 == hashlib.sha256(staged_bytes).hexdigest()
    assert live.read_bytes() == staged_bytes
    assert not staged.exists()

    with sqlite3.connect(db) as conn:
        stored = conn.execute(
            "SELECT ticker, provenance_json, assumption_snapshot_json FROM dcf_runs WHERE id=?",
            (receipt.revision.row_id,),
        ).fetchone()
    assert stored is not None
    ticker, provenance_json, snapshot_json = stored
    assert ticker == "NU"
    provenance = json.loads(provenance_json)
    assert provenance["ticker"] == "NU"
    sources = provenance["sources"]
    calculation_workbook = next(
        source for source in sources if source["role"] == "calculation_workbook"
    )
    assert calculation_workbook["path"] == "dcf/NU.live.xlsx"
    assert calculation_workbook["sha256"] == hashlib.sha256(staged_bytes).hexdigest()
    assert json.loads(snapshot_json)["workbook"] == str(live)

    with sqlite3.connect(ambient_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone() == (0,)
    assert not (ambient / "dcf" / "promoted.xlsx").exists()
    assert ambient == nu.REPO
    assert nu.T == "ZZZ"
    assert ambient / "dcf" / "ZZZ.xlsx" == nu.DEST


def test_receipt_identity_and_legacy_bool_semantics(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    root, db, workbook, s, m = _prepared_owner(tmp_path, migrated_db)

    first = nu.persist_dcf_run_with_receipt(
        s,
        m,
        ticker="NU",
        repo_root=root,
        db_path=db,
        workbook_path=workbook,
    )
    repeated = nu.persist_dcf_run_with_receipt(
        s,
        m,
        ticker="NU",
        repo_root=root,
        db_path=db,
        workbook_path=workbook,
    )
    assert first is not None and first.written is True
    assert repeated is not None and repeated.written is False
    assert repeated.revision == first.revision

    assert (
        nu.persist_dcf_run(
            s,
            m,
            ticker="NU",
            repo_root=root,
            db_path=db,
            workbook_path=workbook,
        )
        is False
    )

    changed = replace(s, price=s.price + 1.0)
    assert (
        nu.persist_dcf_run(
            changed,
            nu.mirror(changed),
            ticker="NU",
            repo_root=root,
            db_path=db,
            workbook_path=workbook,
        )
        is True
    )


def test_returned_receipt_is_stable_after_interleaved_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: Callable[..., Path],
) -> None:
    root, db, workbook, s, m = _prepared_owner(tmp_path, migrated_db)
    original = persist_mod.persist_run

    def interleaved(
        conn: sqlite3.Connection,
        row: persist_mod.DcfRunRow,
        *,
        artifact_promotion: ArtifactPromotion | None = None,
    ) -> persist_mod.DcfPersistenceResult:
        result = original(conn, row, artifact_promotion=artifact_promotion)
        if row.provenance is not None:
            second = replace(
                row,
                npv_per_share=row.npv_per_share + 1.0,
                provenance=replace(row.provenance, input_sha256="f" * 64),
            )
            original(conn, second)
        return result

    monkeypatch.setattr(nu.persist_mod, "persist_run", interleaved)
    receipt = nu.persist_dcf_run_with_receipt(
        s,
        m,
        ticker="NU",
        repo_root=root,
        db_path=db,
        workbook_path=workbook,
    )
    assert receipt is not None and receipt.written is True

    with sqlite3.connect(db) as conn:
        latest_id = conn.execute(
            "SELECT id FROM dcf_runs WHERE ticker='NU' AND is_latest=1"
        ).fetchone()[0]
        persisted = conn.execute(
            "SELECT npv_per_share FROM dcf_runs WHERE id=?",
            (receipt.revision.row_id,),
        ).fetchone()
    assert latest_id != receipt.revision.row_id
    assert persisted == (m.vps,)


class _CommitFailer:
    """Real connection proxy whose commit fails after a real promotion applies."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __enter__(self) -> _CommitFailer:
        self._conn.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        return self._conn.__exit__(exc_type, exc_value, traceback)

    def commit(self) -> None:
        raise sqlite3.OperationalError("synthetic_commit_failure")

    def __getattr__(self, name: str) -> object:
        return getattr(self._conn, name)


def test_unverified_and_mismatched_workbook_authority_is_rejected(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    root, db, workbook, s, m = _prepared_owner(tmp_path, migrated_db)
    claimed = root / "dcf" / "NU.claimed.xlsx"

    with pytest.raises(ValueError, match="dcf_workbook_locator_unverified"):
        nu.persist_dcf_run_with_receipt(
            s,
            m,
            ticker="NU",
            repo_root=root,
            db_path=db,
            workbook_path=workbook,
            workbook_locator_path=claimed,
        )

    other_staged = root / "dcf" / "NU.foreign.staged.xlsx"
    other_staged.write_bytes(workbook.read_bytes())
    with pytest.raises(ValueError, match="dcf_artifact_promotion_mismatch"):
        nu.persist_dcf_run_with_receipt(
            s,
            m,
            ticker="NU",
            repo_root=root,
            db_path=db,
            workbook_path=workbook,
            workbook_locator_path=claimed,
            artifact_promotion=StagedFilePromotion(other_staged, claimed),
        )

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dcf_runs WHERE ticker='NU'").fetchone() == (0,)
    assert not claimed.exists()
    assert other_staged.exists()


def test_failed_commit_restores_prior_live_workbook_and_current_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: Callable[..., Path],
) -> None:
    root, db, workbook, s, m = _prepared_owner(tmp_path, migrated_db)
    first = nu.persist_dcf_run_with_receipt(
        s,
        m,
        ticker="NU",
        repo_root=root,
        db_path=db,
        workbook_path=workbook,
    )
    assert first is not None and first.written is True

    live = root / "dcf" / "NU.live.xlsx"
    prior_live = b"prior live workbook bytes"
    live.write_bytes(prior_live)
    staged = root / "dcf" / "NU.staged.xlsx"
    changed = replace(s, price=s.price + 2.0)
    changed_m = nu.mirror(changed)
    nu.build(changed, changed_m, staged, ticker="NU")
    candidate = staged.read_bytes()

    original_connect = nu.connect_sqlite

    def failing_connect(*args: Any, **kwargs: Any) -> _CommitFailer:
        return _CommitFailer(original_connect(*args, **kwargs))

    monkeypatch.setattr(nu, "connect_sqlite", failing_connect)
    with pytest.raises(sqlite3.OperationalError, match="synthetic_commit_failure"):
        nu.persist_dcf_run_with_receipt(
            changed,
            changed_m,
            ticker="NU",
            repo_root=root,
            db_path=db,
            workbook_path=staged,
            workbook_locator_path=live,
            artifact_promotion=StagedFilePromotion(staged, live),
        )

    assert live.read_bytes() == prior_live
    assert staged.read_bytes() == candidate
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT id, is_latest FROM dcf_runs WHERE ticker='NU' ORDER BY id"
        ).fetchall()
    assert rows == [(first.revision.row_id, 1)]


def test_explicit_repo_root_without_db_path_refuses_to_infer_checkout_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: Callable[..., Path],
) -> None:
    root = tmp_path / "injected-root"
    _write_owner_inputs(root)
    decoy = root / "data" / "portfolio.db"
    decoy.parent.mkdir(parents=True, exist_ok=True)
    decoy.write_bytes(b"decoy checkout database must not be read")
    ambient_db = migrated_db(tmp_path / "ambient-state" / "portfolio.db")
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(ambient_db))
    monkeypatch.setenv("DCF_REPO_ROOT", str(root))

    with pytest.raises(
        ValueError, match="explicit repo_root authority requires an explicit db_path"
    ):
        nu.load_assumptions("NU", repo_root=root)

    assert decoy.read_bytes() == b"decoy checkout database must not be read"
    with sqlite3.connect(ambient_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone() == (0,)


def test_partial_injection_fails_closed_before_any_db_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: Callable[..., Path],
) -> None:
    """Any new-mode injection without an explicit ``db_path`` fails closed.

    Pre-fix, ``workbook_path``/``ticker``-only injection resolved the ambient
    ``EARNINGS_SUMMARY_DB_PATH`` database and could persist into it; the
    guard must fire before any database path is selected or read.
    """
    root, db, _workbook, s, m = _prepared_owner(tmp_path, migrated_db)
    ambient_db = migrated_db(tmp_path / "ambient-state" / "portfolio.db")
    monkeypatch.setattr(nu, "T", "ZZZ")
    monkeypatch.setenv("DCF_TICKER", "ZZZ")
    monkeypatch.setenv("DCF_REPO_ROOT", str(tmp_path / "ambient-root"))
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(ambient_db))
    staged = root / "dcf" / "NU.staged.xlsx"
    staged.write_bytes(b"staged bytes must survive")

    partial_calls: tuple[dict[str, Any], ...] = (
        {"ticker": "NU"},
        {"repo_root": root},
        {"workbook_path": staged},
        {"workbook_locator_path": staged},
        {"ticker": "NU", "repo_root": root, "workbook_path": staged},
    )
    for kwargs in partial_calls:
        with pytest.raises(
            ValueError, match="explicit NU owner authority requires an explicit db_path"
        ):
            nu.persist_dcf_run(s, m, **kwargs)
    assert staged.read_bytes() == b"staged bytes must survive"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone() == (0,)
    with sqlite3.connect(ambient_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone() == (0,)


def test_omitted_ticker_under_explicit_authority_resolves_family_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: Callable[..., Path],
) -> None:
    """Explicit owner authority with an omitted ticker pins ``NU``.

    Pre-fix the omitted ticker borrowed the ambient ``DCF_TICKER``/module
    ``T`` ("ZZZ" here), so the row and the holdings load keyed on the decoy
    issuer; the fixed family identity must never track the environment.
    """
    root, db, workbook, s, m = _prepared_owner(tmp_path, migrated_db)
    ambient_db = migrated_db(tmp_path / "ambient-state" / "portfolio.db")
    monkeypatch.setattr(nu, "T", "ZZZ")
    monkeypatch.setenv("DCF_TICKER", "ZZZ")
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(ambient_db))
    holdings = root / "micro_thesis" / "holdings"
    holdings.mkdir(parents=True, exist_ok=True)
    (holdings / "ZZZ.json").write_text(json.dumps({"mos_bar": 0.99}), encoding="utf-8")

    written = nu.persist_dcf_run(s, m, repo_root=root, db_path=db, workbook_path=workbook)
    assert written is True
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dcf_runs WHERE ticker='ZZZ'").fetchone() == (0,)
        ticker, mos_bar_used, provenance_json = conn.execute(
            "SELECT ticker, mos_bar_used, provenance_json FROM dcf_runs WHERE ticker='NU'"
        ).fetchone()
    assert ticker == "NU"
    assert mos_bar_used == pytest.approx(0.2)
    provenance = json.loads(provenance_json)
    assert provenance["ticker"] == "NU"
    with sqlite3.connect(ambient_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone() == (0,)

    with pytest.raises(ValueError, match="NU platform owner requires ticker 'NU'"):
        nu.persist_dcf_run_with_receipt(
            s,
            m,
            ticker="MELI",
            repo_root=root,
            db_path=db,
            workbook_path=workbook,
        )

    db_only = root / "dcf" / "omitted.ticker.db-only.xlsx"
    nu.build(s, m, db_only, db_path=db)
    repo_only = root / "dcf" / "omitted.ticker.repo-only.xlsx"
    nu.build(s, m, repo_only, repo_root=root)
    for path in (db_only, repo_only):
        wb = openpyxl.load_workbook(path, data_only=False)
        try:
            assert wb["Dashboard"]["A1"].value == "NU - Customer-Driven Platform DCF | Dashboard"
        finally:
            wb.close()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dcf_runs WHERE ticker='ZZZ'").fetchone() == (0,)
