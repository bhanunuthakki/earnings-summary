"""Phase-1 Wave-3 integration: the DCF apply against a REAL ``dcf_runs`` table.

``test_dcf_artifact.py`` validates the risky ``dict -> DcfRunRow`` reconstruction
by a round-trip and exercises the upsert through an INJECTED persist spy -- it
deliberately never touches a real ``dcf_runs`` so no unit test can corrupt live
valuations. That left one path unexercised end-to-end: the DEFAULT
``_default_persist`` -> ``dcf.persist.upsert`` live write.

This closes that gap once, in an isolated temp DB built to the real production
schema (``init_db()`` + ``alembic upgrade head``, so the migration-0076
over_under CHECK and the ``uq_dcf_runs_ticker`` key are the real ones and
migration 0126's ``artifact_json`` column exists). It drives
``apply_dcf_proposal`` with ALL DEFAULTS (real ``get_proposal`` + real
``_default_persist``) and asserts:

  - a row actually lands in ``dcf_runs`` with the reconstructed fields;
  - ``over_under_pct`` is DERIVED at the persist chokepoint as the decimal ratio
    (live - fair)/fair -- NOT the #368 percent-upside bug -- which the real
    post-0076 CHECK simultaneously enforces (a mis-mapped reconstruct would trip
    it);
  - the whole approve -> higher-bar gate -> registered applier -> live write
    chain works via ``apply_approved_proposal`` and, crucially, that an UNMET
    gate writes NOTHING;
  - a repeat write VERSIONS instead of overwriting (migration 0137): the prior
    row is superseded (is_latest=0, back-linked) and the new row is the single
    is_latest=1 current — readers see the second value win, history survives.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from research.apply import apply_approved_proposal
from research.dcf_artifact import apply_dcf_proposal
from research.proposals import create_proposal

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _cfg(db_file: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")
    return cfg


@pytest.fixture
def db_path(tmp_path: Path) -> Iterator[Path]:
    """A real ledger DB at head: ``research_proposals`` (with the 0126
    ``artifact_json`` column) AND ``dcf_runs`` (with the 0076 over_under CHECK
    and ``uq_dcf_runs_ticker``) in one file.

    Mirrors ``test_migration_0076``: ``init_db()`` lays the partial legacy base
    via ``CREATE TABLE IF NOT EXISTS``, then ``stamp(baseline)`` + ``upgrade
    (head)`` runs every migration (0013 creates ``dcf_runs``; 0121 creates
    ``research_proposals``) on top of it.
    """
    db_file = tmp_path / "ledger.db"
    import db as dbmod

    # init_db() writes to the module's global DB_PATH; set it, but SAVE + RESTORE
    # the three data globals set_db_path mutates (DB_PATH/DATA_DIR/FMP_DIR) so a
    # stale tmp path never leaks into a later test -- some writers resolve their
    # DB from db.DB_PATH rather than an explicit db_path (see set_db_path's note),
    # so a leaked path silently empties e.g. the ledger-insights FTS search.
    saved = (dbmod.DB_PATH, dbmod.DATA_DIR, dbmod.FMP_DIR)
    dbmod.set_db_path(str(db_file))
    dbmod.init_db()
    cfg = _cfg(db_file)
    command.stamp(cfg, "0000_baseline")
    command.upgrade(cfg, "head")
    try:
        yield db_file
    finally:
        dbmod.DB_PATH, dbmod.DATA_DIR, dbmod.FMP_DIR = saved


def _proposed_row(
    *, npv_per_share: float = 22.10, live_price: float | None = 12.29
) -> dict[str, object]:
    """An NU-shaped proposed row trading BELOW fair value (live 12.29 < fair
    22.10) -- the exact #368 incident shape whose ``over_under_pct`` must store
    as a NEGATIVE decimal (~ -0.44), never +79.82."""
    return {
        "ticker": "NU",
        "valuation_date": "2026-06-30",
        "horizon_years": 10,
        "wacc": 0.11,
        "npv": 50000.0,
        "npv_per_share": npv_per_share,
        "shares_outstanding": 4.8e9,
        "currency": "USD",
        "live_price": live_price,
        "live_price_at": "2026-06-30T08:00:00",
        "mos_bar_used": 0.25,
        "assumption_snapshot_json": '{"g": 0.4}',
        "notes": None,
        "run_id": None,
    }


def _make_dcf_proposal(
    db_path: Path, *, gate_clearing: bool, row: dict[str, object] | None = None
) -> int:
    """Persist a real inert ``kind='dcf'`` proposal. When ``gate_clearing`` is
    set, attach the evidence doorway + non-refuting adversarial verdict + the
    ``oracle_ok`` the higher bar reads, so ``apply_approved_proposal`` clears."""
    artifact = {"proposed_row": row or _proposed_row(), "oracle_ok": True}
    evidence = (
        json.dumps(
            [{"point": "NU FY guide", "source_url": "https://nu.com.br/ir", "date": "2026-06-30"}]
        )
        if gate_clearing
        else "[]"
    )
    verdict = (
        json.dumps({"refuted": False, "confidence": "high", "rationale": "holds"})
        if gate_clearing
        else None
    )
    return create_proposal(
        task_id=None,
        kind="dcf",
        ticker="NU",
        title="DCF revision: NU",
        body_md="fair value $22.10/sh",
        evidence_json=evidence,
        adversarial_verdict=verdict,
        artifact_json=json.dumps(artifact),
        provenance="derived",
        db_path=db_path,
    )


def _stored(db_path: Path, ticker: str) -> sqlite3.Row:
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM dcf_runs WHERE ticker = ?", (ticker,)).fetchone()
    assert row is not None, f"no dcf_runs row for {ticker}"
    return row


def _count(db_path: Path, ticker: str) -> int:
    with sqlite3.connect(str(db_path)) as conn:
        return int(
            conn.execute("SELECT COUNT(*) FROM dcf_runs WHERE ticker = ?", (ticker,)).fetchone()[0]
        )


def test_apply_dcf_proposal_writes_a_real_dcf_runs_row(db_path: Path) -> None:
    """``apply_dcf_proposal`` with ALL DEFAULTS drives ``_reconstruct_row`` ->
    ``dcf.persist.upsert`` against the real table -- the path no unit test hit."""
    pid = _make_dcf_proposal(db_path, gate_clearing=False)
    note = apply_dcf_proposal(pid, db_path=db_path)  # default get_fn + default _default_persist

    assert "22.10" in note and "live" in note
    row = _stored(db_path, "NU")
    assert row["npv_per_share"] == pytest.approx(22.10)
    assert row["horizon_years"] == 10
    assert row["wacc"] == pytest.approx(0.11)
    assert row["currency"] == "USD"
    # the ISO date/datetime strings survived the reconstruct round-trip.
    assert row["valuation_date"] == "2026-06-30"
    assert str(row["live_price_at"]).startswith("2026-06-30T08:00:00")


def test_live_write_derives_over_under_at_the_chokepoint(db_path: Path) -> None:
    """The stored ``over_under_pct`` is the derived decimal ratio (live-fair)/fair,
    NOT the #368 percent-upside bug -- proving the reconstruct fed the persist
    chokepoint, whose value the real post-0076 CHECK simultaneously accepts."""
    pid = _make_dcf_proposal(db_path, gate_clearing=False)
    apply_dcf_proposal(pid, db_path=db_path)
    ou = _stored(db_path, "NU")["over_under_pct"]
    assert ou == pytest.approx((12.29 - 22.10) / 22.10, abs=1e-6)
    assert ou < 0 and abs(ou) < 1.0  # negative, decimal-scale -- under fair value


def test_live_write_with_no_live_price_stores_null_over_under(db_path: Path) -> None:
    """A row with no live price reconstructs and upserts with a NULL
    ``over_under_pct`` (the #291/derive_over_under guard) -- no CHECK violation."""
    pid = _make_dcf_proposal(db_path, gate_clearing=False, row=_proposed_row(live_price=None))
    apply_dcf_proposal(pid, db_path=db_path)
    row = _stored(db_path, "NU")
    assert row["over_under_pct"] is None
    assert row["npv_per_share"] == pytest.approx(22.10)


def test_full_approve_gate_to_live_write(db_path: Path) -> None:
    """``apply_approved_proposal``: a gate-clearing dcf proposal flows approve ->
    higher bar -> registered applier -> live ``dcf_runs`` write, end-to-end with
    all defaults (no injection)."""
    pid = _make_dcf_proposal(db_path, gate_clearing=True)
    note = apply_approved_proposal(pid, db_path=db_path)
    assert "blocked" not in note
    assert "NU" in note
    assert _stored(db_path, "NU")["npv_per_share"] == pytest.approx(22.10)


def test_uncleared_gate_writes_nothing_to_dcf_runs(db_path: Path) -> None:
    """A bare dcf proposal (oracle_ok present but NO evidence doorway / verdict)
    is blocked by the higher bar and NOTHING lands in ``dcf_runs`` -- the live
    valuations table is untouched by an unapproved what-if."""
    pid = _make_dcf_proposal(db_path, gate_clearing=False)
    note = apply_approved_proposal(pid, db_path=db_path)
    assert "blocked (higher bar)" in note
    assert _count(db_path, "NU") == 0


def test_live_write_supersedes_the_prior_version(db_path: Path) -> None:
    """Two applies for the same ticker version instead of overwriting (migration
    0137 supersede-and-insert): the first row survives as is_latest=0 with a
    superseded_at stamp and a back-link to its successor; the second row is the
    single is_latest=1 current, so latest-first readers see the new value win."""
    pid1 = _make_dcf_proposal(db_path, gate_clearing=False, row=_proposed_row(npv_per_share=22.10))
    apply_dcf_proposal(pid1, db_path=db_path)
    pid2 = _make_dcf_proposal(db_path, gate_clearing=False, row=_proposed_row(npv_per_share=25.50))
    apply_dcf_proposal(pid2, db_path=db_path)

    assert _count(db_path, "NU") == 2  # history preserved, never overwritten
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        latest = conn.execute("SELECT * FROM dcf_runs WHERE ticker='NU' AND is_latest=1").fetchall()
        assert len(latest) == 1  # the 0137 partial unique index holds
        assert latest[0]["npv_per_share"] == pytest.approx(25.50)
        old = conn.execute("SELECT * FROM dcf_runs WHERE ticker='NU' AND is_latest=0").fetchone()
        assert old is not None
        assert old["npv_per_share"] == pytest.approx(22.10)
        assert old["superseded_at"] is not None
        assert old["superseded_by_id"] == latest[0]["id"]
