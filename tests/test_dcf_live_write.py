"""DCF proposal publication against a REAL ``dcf_runs`` table.

``test_dcf_artifact.py`` validates the risky ``dict -> DcfRunRow`` reconstruction
by a round-trip and exercises the upsert through an INJECTED persist spy -- it
deliberately never touches a real ``dcf_runs`` so no unit test can corrupt live
valuations. That left one path unexercised end-to-end: the DEFAULT
``_default_persist`` -> ``dcf.persist.upsert`` live write.

The legacy proposal payload carries valuation outputs but no reproducible DCF
input/provenance bundle. Editorial approval therefore cannot promote it: the
shared persistence chokepoint returns an explicit financial-evidence HOLD and
leaves the live table untouched. These tests pin that fail-closed behavior on an
isolated database at the real production schema.
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


def _count(db_path: Path, ticker: str) -> int:
    with sqlite3.connect(str(db_path)) as conn:
        return int(
            conn.execute("SELECT COUNT(*) FROM dcf_runs WHERE ticker = ?", (ticker,)).fetchone()[0]
        )


def test_direct_apply_without_verified_bridge_returns_hold(db_path: Path) -> None:
    pid = _make_dcf_proposal(db_path, gate_clearing=False)
    result = apply_dcf_proposal(pid, db_path=db_path)

    assert not isinstance(result, str)
    assert result.applied is False
    assert "blocked (DCF financial evidence)" in result.message
    assert "candidate_equity_bridge_unverified" in result.message
    assert _count(db_path, "NU") == 0


def test_blocked_apply_does_not_partially_persist_derived_fields(db_path: Path) -> None:
    pid = _make_dcf_proposal(db_path, gate_clearing=False)
    result = apply_dcf_proposal(pid, db_path=db_path)
    assert not isinstance(result, str) and result.applied is False
    assert _count(db_path, "NU") == 0


def test_missing_live_price_does_not_bypass_financial_evidence_gate(db_path: Path) -> None:
    pid = _make_dcf_proposal(db_path, gate_clearing=False, row=_proposed_row(live_price=None))
    result = apply_dcf_proposal(pid, db_path=db_path)
    assert not isinstance(result, str) and result.applied is False
    assert _count(db_path, "NU") == 0


def test_editorially_approved_proposal_still_requires_financial_evidence(db_path: Path) -> None:
    pid = _make_dcf_proposal(db_path, gate_clearing=True)
    note = apply_approved_proposal(pid, db_path=db_path)
    assert "blocked (DCF financial evidence)" in note
    assert "candidate_equity_bridge_unverified" in note
    assert _count(db_path, "NU") == 0


def test_uncleared_gate_writes_nothing_to_dcf_runs(db_path: Path) -> None:
    """A bare dcf proposal (oracle_ok present but NO evidence doorway / verdict)
    is blocked by the higher bar and NOTHING lands in ``dcf_runs`` -- the live
    valuations table is untouched by an unapproved what-if."""
    pid = _make_dcf_proposal(db_path, gate_clearing=False)
    note = apply_approved_proposal(pid, db_path=db_path)
    assert "blocked (higher bar)" in note
    assert _count(db_path, "NU") == 0


def test_repeated_unverified_proposals_never_create_versions(db_path: Path) -> None:
    pid1 = _make_dcf_proposal(db_path, gate_clearing=False, row=_proposed_row(npv_per_share=22.10))
    first = apply_dcf_proposal(pid1, db_path=db_path)
    pid2 = _make_dcf_proposal(db_path, gate_clearing=False, row=_proposed_row(npv_per_share=25.50))
    second = apply_dcf_proposal(pid2, db_path=db_path)

    assert not isinstance(first, str) and first.applied is False
    assert not isinstance(second, str) and second.applied is False
    assert _count(db_path, "NU") == 0
