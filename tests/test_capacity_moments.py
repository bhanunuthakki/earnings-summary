"""tenet-2 Phase 3 governor moment classes — capacity_breach /
life_event_checkpoint / profile_drift (research.capacity_moments +
research.governor's freshness-gate extension).

Uses the SAME real full-schema fixture as test_position_review_integration.py
(``db.init_db()`` for the inline-managed baseline tables — tracked_companies
etc. — then ``alembic stamp 0000_baseline`` + ``upgrade head``) rather than
the lighter "stamp past a prior head" shortcut some tests use: run_governor's
EXISTING classes query ``decisions`` / ``analyst_notes`` / ``tracked_companies``
unconditionally (only falsifier_breach's alerts join is try/except-guarded),
and this module leans on ``alerts.store`` (fire_alert / find_by_signature /
dismiss_alert) directly, which needs the real column set (user_id,
signature_sha, memo_artifact_id, approved_at, dismiss_reason) — so every
table this suite touches must be the REAL migrated schema, not a hand-rolled
stand-in.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db as dbmod  # noqa: E402
from alerts.store import get_alert, list_alerts  # noqa: E402
from integrations.portfolio_tracker_client import (  # noqa: E402
    LivePortfolio,
    PortfolioAnalytics,
)
from owner_profile.store import append_fact, retire_fact  # noqa: E402
from research.capacity_moments import (  # noqa: E402
    capacity_breach_still_active,
    collect_capacity_breach_moments,
    collect_life_event_moments,
    collect_profile_drift_moments,
)
from research.governor import Moment, freshness_ok, run_governor  # noqa: E402

_NOW = datetime(2026, 7, 17, 8, 0, 0)


def _stub_analytics(analytics: PortfolioAnalytics) -> object:
    """A typed stand-in for ``fetch_portfolio_analytics`` — avoids the
    untyped-lambda pyright noise a bare ``lambda **_kw: analytics`` produces."""

    def _fake(**_kw: object) -> PortfolioAnalytics:
        return analytics

    return _fake


def _stub_live_portfolio(live: LivePortfolio) -> object:
    """Same idea for ``fetch_live_portfolio``."""

    def _fake(**_kw: object) -> LivePortfolio:
        return live

    return _fake


def _cfg(db_file: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")
    return cfg


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    """(repo_root, db_path) — a real, fully-migrated DB at
    <repo_root>/data/portfolio.db. Saves/restores db.py's process-global
    DB_PATH/DATA_DIR/FMP_DIR (mirrors test_position_review_integration.py) so
    this fixture never leaks state into other test modules sharing the run."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_file = data_dir / "portfolio.db"
    saved = (dbmod.DB_PATH, dbmod.DATA_DIR, dbmod.FMP_DIR)
    dbmod.set_db_path(str(db_file))
    dbmod.init_db()
    cfg = _cfg(db_file)
    command.stamp(cfg, "0000_baseline")
    command.upgrade(cfg, "head")
    try:
        yield tmp_path, db_file
    finally:
        dbmod.DB_PATH, dbmod.DATA_DIR, dbmod.FMP_DIR = saved


def _write_weights(repo_root: Path, weights: dict[str, float]) -> None:
    path = repo_root / "data" / "portfolio_weights.json"
    path.write_text(json.dumps({"computed_at": _NOW.isoformat(), "weights": weights}))


def _seed_human_capital_fact(db: Path, *, cap_pct: float = 20.0) -> int:
    conn = sqlite3.connect(str(db))
    try:
        fid = append_fact(
            conn,
            category="capacity",
            key="human_capital.big_tech_ads",
            value={"cap_pct": cap_pct, "members": ["META", "GOOGL"]},
            narrative="Human-capital bucket big_tech_ads capped at "
            f"{cap_pct:g}% (income correlation).",
            provenance="cio_context_import",
            status="affirmed",
        )
        conn.commit()
    finally:
        conn.close()
    return fid


def _seed_cash_floor_fact(db: Path, *, months: float = 6.0) -> int:
    conn = sqlite3.connect(str(db))
    try:
        fid = append_fact(
            conn,
            category="capacity",
            key="cash_buffer_months",
            value={"months": months},
            narrative=f"Cash buffer target: {months:g} months.",
            provenance="wealthplan_import",
            status="affirmed",
        )
        conn.commit()
    finally:
        conn.close()
    return fid


def _seed_life_event_fact(db: Path, *, event_date: str) -> int:
    conn = sqlite3.connect(str(db))
    try:
        fid = append_fact(
            conn,
            category="capacity",
            key="life_event.work_break_1",
            value={"kind": "work_break", "label": "Work break", "date": event_date},
            narrative=f"Work break starting {event_date}.",
            provenance="wealthplan_import",
            status="affirmed",
        )
        conn.commit()
    finally:
        conn.close()
    return fid


def _seed_expiring_fact(db: Path, *, review_horizon_days: int, affirmed_at: str) -> int:
    conn = sqlite3.connect(str(db))
    try:
        fid = append_fact(
            conn,
            category="appetite",
            key="dry_powder_policy",
            value={"months": 3.0},
            narrative="Dry-powder policy: keep 3 months uninvested.",
            provenance="owner",
            status="affirmed",
            review_horizon_days=review_horizon_days,
        )
        conn.execute(
            "UPDATE owner_profile_facts SET affirmed_at = ? WHERE id = ?", (affirmed_at, fid)
        )
        conn.commit()
    finally:
        conn.close()
    return fid


def _seed_tax_bucket_fact(db: Path, *, balances: dict[str, float], affirmed_at: str) -> int:
    conn = sqlite3.connect(str(db))
    try:
        fid = append_fact(
            conn,
            category="capacity",
            key="tax_bucket_balances",
            value={"balances": balances, "as_of": "2026-01-01"},
            narrative="Household investable balances by tax treatment.",
            provenance="wealthplan_import",
            status="affirmed",
        )
        conn.execute(
            "UPDATE owner_profile_facts SET affirmed_at = ? WHERE id = ?", (affirmed_at, fid)
        )
        conn.commit()
    finally:
        conn.close()
    return fid


# --------------------------------------------------------------------------
# capacity_breach — human-capital stacking
# --------------------------------------------------------------------------


def test_human_capital_breach_fires_and_writes_tier1_alert(repo: tuple[Path, Path]) -> None:
    root, db = repo
    _seed_human_capital_fact(db, cap_pct=20.0)
    _write_weights(root, {"META": 0.15, "GOOGL": 0.10})  # 25% combined > 20% cap

    conn = sqlite3.connect(str(db))
    try:
        moments = collect_capacity_breach_moments(conn, repo_root=root, now=_NOW, db_path=db)
    finally:
        conn.close()
    assert len(moments) == 1
    m = moments[0]
    assert m.class_ == "capacity_breach"
    assert m.key.startswith("alert:")
    assert "big_tech_ads" in m.body

    alerts = list_alerts(status="pending", db_path=db)
    assert len(alerts) == 1
    assert alerts[0].trigger_kind == "owner_capacity_breach"
    ev = json.loads(alerts[0].evidence_json)
    assert ev["policy_kind"] == "owner_capacity"
    assert ev["capacity_kind"] == "human_capital"

    from dashboard.inbox_rank import decisive_alert_reason

    assert (
        decisive_alert_reason(alerts[0].trigger_kind, alerts[0].evidence_json)
        == "owner policy breach"
    )


def test_human_capital_breach_episode_is_idempotent(repo: tuple[Path, Path]) -> None:
    """A PERSISTENT breach keeps the SAME alert id across governor runs — the
    idempotency-key/episode-keying requirement (mirrors falsifier_breach's
    ``alert:<id>`` key exactly)."""
    root, db = repo
    _seed_human_capital_fact(db, cap_pct=20.0)
    _write_weights(root, {"META": 0.15, "GOOGL": 0.10})

    conn = sqlite3.connect(str(db))
    try:
        first = collect_capacity_breach_moments(conn, repo_root=root, now=_NOW, db_path=db)
        second = collect_capacity_breach_moments(
            conn, repo_root=root, now=_NOW + timedelta(days=1), db_path=db
        )
    finally:
        conn.close()
    assert first[0].key == second[0].key
    assert len(list_alerts(status="pending", db_path=db)) == 1  # never a second alert row


def test_human_capital_breach_self_heals_when_resolved(repo: tuple[Path, Path]) -> None:
    root, db = repo
    _seed_human_capital_fact(db, cap_pct=20.0)
    _write_weights(root, {"META": 0.15, "GOOGL": 0.10})
    conn = sqlite3.connect(str(db))
    try:
        collect_capacity_breach_moments(conn, repo_root=root, now=_NOW, db_path=db)
    finally:
        conn.close()
    (alert,) = list_alerts(status="pending", db_path=db)

    # The owner trims META/GOOGL below the cap.
    _write_weights(root, {"META": 0.05, "GOOGL": 0.05})
    conn = sqlite3.connect(str(db))
    try:
        moments = collect_capacity_breach_moments(
            conn, repo_root=root, now=_NOW + timedelta(days=1), db_path=db
        )
    finally:
        conn.close()
    assert moments == []
    healed = get_alert(alert.id, db_path=db)
    assert healed.status == "dismissed"
    assert healed.dismiss_reason == "capacity breach resolved (automatic)"


def test_capacity_breach_still_active_freshness_recheck(repo: tuple[Path, Path]) -> None:
    root, db = repo
    _seed_human_capital_fact(db, cap_pct=20.0)
    _write_weights(root, {"META": 0.15, "GOOGL": 0.10})
    conn = sqlite3.connect(str(db))
    try:
        moments = collect_capacity_breach_moments(conn, repo_root=root, now=_NOW, db_path=db)
        alert_id = int(moments[0].source_ref.split(":")[1])
        assert capacity_breach_still_active(conn, alert_id, repo_root=root, db_path=db) is True
    finally:
        conn.close()

    _write_weights(root, {"META": 0.05, "GOOGL": 0.05})
    conn = sqlite3.connect(str(db))
    try:
        assert capacity_breach_still_active(conn, alert_id, repo_root=root, db_path=db) is False
    finally:
        conn.close()


def test_no_breach_when_weights_cache_missing(repo: tuple[Path, Path]) -> None:
    root, db = repo
    _seed_human_capital_fact(db, cap_pct=20.0)  # no portfolio_weights.json written
    conn = sqlite3.connect(str(db))
    try:
        moments = collect_capacity_breach_moments(conn, repo_root=root, now=_NOW, db_path=db)
    finally:
        conn.close()
    assert moments == []


# --------------------------------------------------------------------------
# capacity_breach — cash floor
# --------------------------------------------------------------------------


def test_cash_floor_breach_fires_on_near_zero_cash(
    repo: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, db = repo
    _seed_cash_floor_fact(db, months=6.0)

    from integrations import portfolio_tracker_client as tc

    bucket = tc.AllocationBucket(label="Cash", value=1000.0, weight_pct=0.4, count=1)
    positioning = tc.Positioning(
        snapshot_date="2026-07-17",
        total_value=100_000.0,
        concentration=None,
        weighted_avg_correlation_spy=None,
        by_asset_type=[bucket],
    )
    analytics = tc.PortfolioAnalytics(available=True, api_url="http://x", positioning=positioning)
    monkeypatch.setattr(tc, "fetch_portfolio_analytics", _stub_analytics(analytics))

    conn = sqlite3.connect(str(db))
    try:
        moments = collect_capacity_breach_moments(conn, repo_root=root, now=_NOW, db_path=db)
    finally:
        conn.close()
    assert len(moments) == 1
    ev = json.loads(list_alerts(status="pending", db_path=db)[0].evidence_json)
    assert ev["capacity_kind"] == "cash_floor"


def test_cash_floor_no_breach_when_cash_is_ample(
    repo: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, db = repo
    _seed_cash_floor_fact(db, months=6.0)

    from integrations import portfolio_tracker_client as tc

    bucket = tc.AllocationBucket(label="Cash", value=8000.0, weight_pct=8.0, count=1)
    positioning = tc.Positioning(
        snapshot_date="2026-07-17",
        total_value=100_000.0,
        concentration=None,
        weighted_avg_correlation_spy=None,
        by_asset_type=[bucket],
    )
    analytics = tc.PortfolioAnalytics(available=True, api_url="http://x", positioning=positioning)
    monkeypatch.setattr(tc, "fetch_portfolio_analytics", _stub_analytics(analytics))

    conn = sqlite3.connect(str(db))
    try:
        moments = collect_capacity_breach_moments(conn, repo_root=root, now=_NOW, db_path=db)
    finally:
        conn.close()
    assert moments == []


# --------------------------------------------------------------------------
# life_event_checkpoint
# --------------------------------------------------------------------------


def test_life_event_checkpoint_fires_within_lookahead(repo: tuple[Path, Path]) -> None:
    _root, db = repo
    fid = _seed_life_event_fact(db, event_date="2026-08-01")  # 15 days out from _NOW
    conn = sqlite3.connect(str(db))
    try:
        moments = collect_life_event_moments(conn, now=_NOW)
    finally:
        conn.close()
    assert len(moments) == 1
    assert moments[0].class_ == "life_event_checkpoint"
    assert moments[0].key == f"fact:{fid}"
    assert "dry-powder posture check" in moments[0].body


def test_life_event_checkpoint_silent_outside_lookahead(repo: tuple[Path, Path]) -> None:
    _root, db = repo
    _seed_life_event_fact(db, event_date="2030-01-01")  # far outside the 90-day window
    conn = sqlite3.connect(str(db))
    try:
        moments = collect_life_event_moments(conn, now=_NOW)
    finally:
        conn.close()
    assert moments == []


# --------------------------------------------------------------------------
# profile_drift
# --------------------------------------------------------------------------


def test_profile_drift_fires_for_expired_review_horizon(repo: tuple[Path, Path]) -> None:
    _root, db = repo
    old_affirmed = (_NOW - timedelta(days=400)).isoformat()
    fid = _seed_expiring_fact(db, review_horizon_days=90, affirmed_at=old_affirmed)
    conn = sqlite3.connect(str(db))
    try:
        moments = collect_profile_drift_moments(conn, now=_NOW)
    finally:
        conn.close()
    assert len(moments) == 1
    assert moments[0].class_ == "profile_drift"
    assert moments[0].key == f"drift:{fid}"
    assert "not an accusation" in moments[0].body


def test_profile_drift_silent_before_horizon_elapses(repo: tuple[Path, Path]) -> None:
    _root, db = repo
    recent = (_NOW - timedelta(days=5)).isoformat()
    _seed_expiring_fact(db, review_horizon_days=90, affirmed_at=recent)
    conn = sqlite3.connect(str(db))
    try:
        moments = collect_profile_drift_moments(conn, now=_NOW)
    finally:
        conn.close()
    assert moments == []


def test_profile_drift_tax_divergence_leg(
    repo: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _root, db = repo
    stale = (_NOW - timedelta(days=200)).isoformat()  # >2 quarters
    fid = _seed_tax_bucket_fact(
        db, balances={"pretax": 100_000.0, "taxable": 50_000.0}, affirmed_at=stale
    )

    from integrations import portfolio_tracker_client as tc

    live = tc.LivePortfolio(
        available=True,
        api_url="http://x",
        by_tax_treatment={"tax_deferred": 40_000.0, "taxable": 30_000.0, "tax_free": 0.0},
    )
    monkeypatch.setattr(tc, "fetch_live_portfolio", _stub_live_portfolio(live))

    conn = sqlite3.connect(str(db))
    try:
        moments = collect_profile_drift_moments(conn, now=_NOW)
    finally:
        conn.close()
    assert len(moments) == 1
    assert moments[0].key == f"drift:{fid}"
    assert "worth a re-affirm" in moments[0].body


def test_profile_drift_tax_divergence_silent_within_tolerance(
    repo: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _root, db = repo
    stale = (_NOW - timedelta(days=200)).isoformat()
    _seed_tax_bucket_fact(
        db, balances={"pretax": 100_000.0, "taxable": 50_000.0}, affirmed_at=stale
    )

    from integrations import portfolio_tracker_client as tc

    live = tc.LivePortfolio(
        available=True,
        api_url="http://x",
        by_tax_treatment={"tax_deferred": 95_000.0, "taxable": 55_000.0, "tax_free": 0.0},
    )
    monkeypatch.setattr(tc, "fetch_live_portfolio", _stub_live_portfolio(live))

    conn = sqlite3.connect(str(db))
    try:
        moments = collect_profile_drift_moments(conn, now=_NOW)
    finally:
        conn.close()
    assert moments == []


# --------------------------------------------------------------------------
# governor freshness-gate extension — "any ping grounded in a profile fact"
# --------------------------------------------------------------------------


def test_freshness_gate_blocks_a_retired_profile_fact(repo: tuple[Path, Path]) -> None:
    """The standing rule (§4 delivery seam 3 / freshness-gate extension):
    even a moment already collected must re-verify the backing fact is still
    affirmed before it's allowed to send."""
    _root, db = repo
    fid = _seed_life_event_fact(db, event_date="2026-08-01")
    moment = Moment(
        class_="life_event_checkpoint",
        key=f"fact:{fid}",
        ticker=None,
        body="x",
        source_ref=f"fact:{fid}",
    )
    assert freshness_ok(moment, db) is True

    conn = sqlite3.connect(str(db))
    try:
        assert retire_fact(conn, fid) is True
        conn.commit()
    finally:
        conn.close()
    assert freshness_ok(moment, db) is False


def test_run_governor_never_resurrects_a_retired_profile_fact(
    repo: tuple[Path, Path],
) -> None:
    """Once retired, a life_event_checkpoint fact stops being COLLECTED at all
    (collection itself reads only affirmed facts via ``get_current_profile``)
    — a stronger, source-level version of the freshness gate. A pending
    ``send_failed`` retry for it is simply never revisited (no further sends,
    no false "sent") rather than resurrected — the owner is never pinged
    about something no longer true, which is the standing rule's actual
    guarantee, delivered here via the collection query instead of the
    freshness recheck (see ``test_freshness_gate_blocks_a_retired_profile_fact``
    for the direct ``freshness_ok`` unit proof of the rule itself)."""
    _root, db = repo
    fid = _seed_life_event_fact(db, event_date="2026-08-01")
    first = run_governor(db, send_fn=lambda pid, m: False, now=_NOW)
    assert first["send_failed"] == 1  # attempted while still affirmed, delivery failed

    conn = sqlite3.connect(str(db))
    try:
        retire_fact(conn, fid)
        conn.commit()
    finally:
        conn.close()

    second = run_governor(db, send_fn=lambda pid, m: True, now=_NOW)
    assert second["sent"] == 0
    assert second["seen"] == 0  # not re-collected at all — nothing to retry


def test_run_governor_sends_life_event_checkpoint_and_never_repeats(
    repo: tuple[Path, Path],
) -> None:
    _root, db = repo
    _seed_life_event_fact(db, event_date="2026-08-01")
    sent: list[Moment] = []
    tally = run_governor(db, send_fn=lambda pid, m: sent.append(m) or True, now=_NOW)
    assert tally["sent"] == 1
    assert sent[0].class_ == "life_event_checkpoint"
    # Once-forever: a second run sees nothing new for this class.
    again = run_governor(db, send_fn=lambda pid, m: True, now=_NOW)
    assert again["seen"] == 0


def test_capacity_breach_class_honors_the_shared_mute_discipline(
    repo: tuple[Path, Path],
) -> None:
    """The new classes compete under the SAME caps/mute machinery — no new
    interruption plumbing (§7 ruling 4: same governor budget)."""
    from research.governor import MUTE_AFTER, record_dismissal

    root, db = repo
    _seed_human_capital_fact(db, cap_pct=20.0)
    _write_weights(root, {"META": 0.15, "GOOGL": 0.10})
    tally = run_governor(db, send_fn=lambda pid, m: True, now=_NOW, repo_root=root)
    assert tally["sent"] == 1

    conn = sqlite3.connect(str(db))
    try:
        ping_id = int(
            conn.execute("SELECT id FROM coach_pings WHERE class_ = 'capacity_breach'").fetchone()[
                0
            ]
        )
    finally:
        conn.close()
    recorded, muted = record_dismissal(ping_id, db_path=db)
    assert recorded and muted is None  # only 1 of MUTE_AFTER=3 so far
    assert MUTE_AFTER == 3
