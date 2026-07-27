"""Tests for candidate_fit_cache.py — materialize/read of data/candidate_fit.json
and the BookContext assembly from the tracker + caches.

The tracker fetch, the weights cache, and the risk snapshot are monkeypatched so
the assembly logic (live-wins, snapshot-fallback, offline degradation) is tested
without a network or DB; the materialize→read round-trip writes and re-reads a
real cache file in a tmp repo.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import candidate_fit_cache as cfc  # noqa: E402
from allocation.candidate_fit import BookContext, CandidateFit, FitFactor  # noqa: E402

# --------------------------------------------------------------------------- #
# Tracker / cache stubs
# --------------------------------------------------------------------------- #


class _Bucket:
    def __init__(self, label: str, weight_pct: float | None) -> None:
        self.label = label
        self.weight_pct = weight_pct


class _Beta:
    def __init__(
        self, sharpe: float | None, rf: float | None, vol_ann: float | None = None
    ) -> None:
        self.sharpe = sharpe
        self.risk_free_annual = rf
        self.portfolio_volatility_annualized = vol_ann


class _Positioning:
    def __init__(self, by_sector: list[_Bucket], correlations: list[object]) -> None:
        self.by_sector = by_sector
        self.correlations = correlations


class _Analytics:
    def __init__(
        self,
        *,
        available: bool,
        beta: _Beta | None = None,
        positioning: _Positioning | None = None,
    ) -> None:
        self.available = available
        self.beta = beta
        self.positioning = positioning


class _Snapshot:
    def __init__(  # type: ignore[no-untyped-def]
        self,
        *,
        sharpe=None,
        growth_tilt=None,
        vol_ann=None,
        captured_at="2026-06-13T04:00:00",
    ) -> None:
        self.sharpe = sharpe
        self.growth_tilt = growth_tilt
        self.portfolio_volatility_annualized = vol_ann
        self.captured_at = captured_at


def _patch_book_sources(
    monkeypatch: pytest.MonkeyPatch,
    *,
    weights: dict[str, float],
    analytics: _Analytics,
    snapshot: _Snapshot | None,
    rollup_growth_tilt: float | None = 0.4,
) -> None:
    # assemble_book_context imports these names locally; patch at their source.
    monkeypatch.setattr(
        "portfolio_weights.read_materialized_weights", lambda _r: weights, raising=True
    )
    monkeypatch.setattr(
        "integrations.portfolio_tracker_client.fetch_portfolio_analytics",
        lambda **_k: analytics,
        raising=True,
    )
    monkeypatch.setattr(
        "portfolio_risk_snapshot_store.read_latest_snapshot",
        lambda **_k: snapshot,
        raising=True,
    )

    class _Rollup:
        growth_tilt = rollup_growth_tilt

    monkeypatch.setattr(
        "portfolio_risk.factor_exposure_rollup",
        lambda _rows: _Rollup() if rollup_growth_tilt is not None else None,
        raising=True,
    )


# --------------------------------------------------------------------------- #
# assemble_book_context
# --------------------------------------------------------------------------- #


def test_assemble_book_context_live_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A live tracker fetch supplies Sharpe + rf (beta), sector weights
    (positioning, percent → fraction) and growth tilt (the factor roll-up)."""
    analytics = _Analytics(
        available=True,
        beta=_Beta(sharpe=0.9, rf=0.04, vol_ann=0.18),
        positioning=_Positioning(
            by_sector=[_Bucket("Technology", 35.0), _Bucket("Energy", 5.0)], correlations=[object()]
        ),
    )
    _patch_book_sources(
        monkeypatch,
        weights={"AAA": 0.6, "BBB": 0.4},
        analytics=analytics,
        snapshot=_Snapshot(sharpe=0.1, growth_tilt=0.1, vol_ann=0.5),
        rollup_growth_tilt=0.42,
    )
    book = cfc.assemble_book_context(tmp_path, db_path=tmp_path / "x.db")
    assert book.weights == {"AAA": 0.6, "BBB": 0.4}
    assert book.sharpe == pytest.approx(0.9)  # live beta, not the snapshot's 0.1
    assert book.risk_free_annual == pytest.approx(0.04)
    assert book.growth_tilt == pytest.approx(0.42)  # the roll-up, not the snapshot
    assert book.vol_ann == pytest.approx(0.18)  # live beta, not the snapshot's 0.5
    assert book.sector_weights == {"Technology": pytest.approx(0.35), "Energy": pytest.approx(0.05)}
    assert book.captured_at == "2026-06-13T04:00:00"


def test_assemble_book_context_fetches_only_fit_inputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fit assembly must not wait on unrelated tracker analytics endpoints."""
    analytics = _Analytics(
        available=True,
        beta=_Beta(sharpe=0.9, rf=0.04, vol_ann=0.18),
        positioning=_Positioning(by_sector=[_Bucket("Technology", 35.0)], correlations=[object()]),
    )
    _patch_book_sources(
        monkeypatch,
        weights={"AAA": 1.0},
        analytics=analytics,
        snapshot=None,
    )
    fetch_kwargs: dict[str, object] = {}

    def _fetch(**kwargs: object) -> _Analytics:
        fetch_kwargs.update(kwargs)
        return analytics

    monkeypatch.setattr(
        "integrations.portfolio_tracker_client.fetch_portfolio_analytics",
        _fetch,
        raising=True,
    )

    cfc.assemble_book_context(tmp_path, db_path=tmp_path / "x.db")

    assert fetch_kwargs["only"] == {"beta", "positioning"}


def test_assemble_book_context_offline_falls_back_to_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Tracker down → Sharpe + growth tilt fall back to the cached snapshot; the
    risk-free rate and sector weights are tracker-only, so they stay absent (the
    Marginal-Sharpe + Sector factors will read partial, never faked)."""
    _patch_book_sources(
        monkeypatch,
        weights={"AAA": 1.0},
        analytics=_Analytics(available=False),
        snapshot=_Snapshot(sharpe=0.7, growth_tilt=0.25, vol_ann=0.22),
    )
    book = cfc.assemble_book_context(tmp_path, db_path=tmp_path / "x.db")
    assert book.sharpe == pytest.approx(0.7)  # snapshot fallback
    assert book.risk_free_annual is None  # tracker-only
    assert book.growth_tilt == pytest.approx(0.25)  # snapshot fallback
    assert book.vol_ann == pytest.approx(0.22)  # snapshot fallback
    assert book.sector_weights == {}  # tracker-only


# --------------------------------------------------------------------------- #
# materialize → read round-trip
# --------------------------------------------------------------------------- #


def _fit(ticker: str) -> CandidateFit:
    return CandidateFit(
        ticker=ticker,
        factors=[
            FitFactor("sharpe", "Marginal Sharpe", 1.12, "SR ...", False),
            FitFactor("divers", "Diversification", 1.20, "corr ...", False),
            FitFactor("factor", "Factor fit", 1.0, "n/a", True),
            FitFactor("sector", "Sector fit", 0.88, "Tech ...", False),
        ],
        fit=1.18,
        why="sharpe 1.12 (...) x ... = 1.18",
        partial=True,
        obs=180,
    )


def _seed_eval_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE tracked_companies (ticker TEXT, list_type TEXT, archived_at TEXT)")
    conn.executemany(
        "INSERT INTO tracked_companies (ticker, list_type, archived_at) VALUES (?, ?, ?)",
        [
            ("DLO", "evaluation", None),
            ("NU", "portfolio", None),
            ("OLD", "evaluation", "2026-01-01"),
        ],
    )
    conn.commit()
    conn.close()


def test_materialize_then_read_round_trip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """materialize writes a cache the reader reconstructs into CandidateFit
    objects; only non-archived evaluation names are scored."""
    db_path = tmp_path / "portfolio.db"
    _seed_eval_db(db_path)
    # Stub the heavy compute + book assembly — this test pins the cache I/O, not
    # the geometry (covered in test_candidate_fit.py).
    monkeypatch.setattr(
        cfc, "assemble_book_context", lambda *a, **k: BookContext(weights={}), raising=True
    )

    captured: dict[str, object] = {}

    def _fake_compute(repo_root, candidates, book, *, sectors=None, target=None, **_kw):  # type: ignore[no-untyped-def]
        captured["candidates"] = list(candidates)
        captured["sectors"] = dict(sectors or {})
        captured["target_source"] = getattr(target, "source", None)
        return {t: _fit(t) for t in candidates}

    monkeypatch.setattr(cfc, "compute_candidate_fit", _fake_compute, raising=True)
    # A profile cache so the sector lookup has something to find.
    fmp = tmp_path / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True)
    (fmp / "DLO_profile.json").write_text(json.dumps([{"sector": "Technology"}]), encoding="utf-8")

    conn = sqlite3.connect(db_path)
    try:
        n = cfc.materialize_candidate_fit(conn, tmp_path, db_path=db_path)
    finally:
        conn.close()

    assert n == 1  # only DLO (NU is portfolio, OLD is archived)
    assert captured["candidates"] == ["DLO"]
    assert captured["sectors"] == {"DLO": "Technology"}
    # No positioning_intents table in the seed DB → the resolved target is the
    # book default (behavior-preservation).
    assert captured["target_source"] == "book_default"
    assert (tmp_path / "data" / "candidate_fit.json").exists()

    out = cfc.read_materialized_candidate_fit(tmp_path)
    assert set(out) == {"DLO"}
    dlo = out["DLO"]
    assert dlo.fit == pytest.approx(1.18)
    assert dlo.partial
    assert dlo.obs == 180
    assert [f.key for f in dlo.factors] == ["sharpe", "divers", "factor", "sector"]
    assert {f.key: f.missing for f in dlo.factors}["factor"] is True

    # v2 payload shape: version stamp + book/target blocks readable via the
    # meta reader.
    meta = cfc.read_materialized_fit_meta(tmp_path)
    assert meta.get("version") == 2
    assert isinstance(meta.get("book"), dict)
    target_block = meta.get("target")
    assert isinstance(target_block, dict) and target_block["source"] == "book_default"


def test_read_missing_or_malformed_cache_degrades_to_empty(tmp_path: Path) -> None:
    assert cfc.read_materialized_candidate_fit(tmp_path) == {}  # no file
    assert cfc.read_materialized_fit_meta(tmp_path) == {}
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "candidate_fit.json").write_text("not json{", encoding="utf-8")
    assert cfc.read_materialized_candidate_fit(tmp_path) == {}
    assert cfc.read_materialized_fit_meta(tmp_path) == {}


def test_v1_payload_still_readable(tmp_path: Path) -> None:
    """A pre-v2 cache (no version/target keys, fits without the new fields)
    decodes into CandidateFit with the v2 fields at their defaults — last-good
    semantics across the upgrade."""
    data = tmp_path / "data"
    data.mkdir()
    v1 = {
        "computed_at": "2026-07-01T04:00:00",
        "book": {"sharpe": 0.8, "growth_tilt": 0.3, "risk_free_annual": 0.04},
        "fits": {
            "DLO": {
                "fit": 1.05,
                "why": "sharpe 1.05 (...) = 1.05",
                "partial": False,
                "obs": 200,
                "factors": [
                    {
                        "key": "sharpe",
                        "label": "Marginal Sharpe",
                        "multiplier": 1.05,
                        "detail": "...",
                        "missing": False,
                    }
                ],
            }
        },
    }
    (data / "candidate_fit.json").write_text(json.dumps(v1), encoding="utf-8")
    out = cfc.read_materialized_candidate_fit(tmp_path)
    dlo = out["DLO"]
    assert dlo.fit == pytest.approx(1.05)
    assert dlo.fit_target is None
    assert dlo.target_factors == []
    assert dlo.sharpe_delta_bps is None
    assert dlo.corr_trend is None
    assert dlo.degraded == ()
    meta = cfc.read_materialized_fit_meta(tmp_path)
    assert "target" not in meta and meta.get("version") is None


def test_assemble_book_context_degradation_reasons(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Offline with no snapshot → every absent input is NAMED (loud
    degradation), not silently neutral."""
    _patch_book_sources(
        monkeypatch,
        weights={},
        analytics=_Analytics(available=False),
        snapshot=None,
    )
    book = cfc.assemble_book_context(tmp_path, db_path=tmp_path / "x.db")
    joined = " | ".join(book.degraded)
    assert "book Sharpe unknown" in joined
    assert "risk-free rate" in joined
    assert "sector weights" in joined
    assert "growth tilt" in joined
    assert "book vol unknown" in joined
    assert "weights cache empty" in joined
    # Live-and-complete book carries no degradation reasons.
    analytics = _Analytics(
        available=True,
        beta=_Beta(sharpe=0.9, rf=0.04, vol_ann=0.16),
        positioning=_Positioning(by_sector=[_Bucket("Technology", 35.0)], correlations=[object()]),
    )
    _patch_book_sources(
        monkeypatch,
        weights={"AAA": 1.0},
        analytics=analytics,
        snapshot=None,
        rollup_growth_tilt=0.42,
    )
    book = cfc.assemble_book_context(tmp_path, db_path=tmp_path / "x.db")
    assert book.degraded == ()
