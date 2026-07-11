"""execution/export_book_cma.py — the wealthplan hand-off export. Exercises
``build_export`` (pure assembly, no CLI) plus the CLI's idempotency skip and
halt-on-no-data paths against a tmp repo root."""

from __future__ import annotations

import json
import math
import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import execution.export_book_cma as export_book_cma
from models.book_cma import BookCmaExport

_CASH_LIKE = frozenset({"SGOV", "FDRXX", "CUR:USD"})


def _trading_days(n: int, start: date = date(2023, 1, 3)) -> list[date]:
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _write_price_chart(repo_root: Path, ticker: str, days: list[date], *, vol: float) -> None:
    fmp = repo_root / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True, exist_ok=True)
    rng = random.Random(hash(ticker) & 0xFFFF)
    level = 100.0
    rows: list[dict[str, object]] = []
    for d in days:
        level *= math.exp(rng.gauss(0.0004, vol))
        rows.append({"date": d.isoformat(), "adjClose": level})
    (fmp / f"{ticker}_price_chart_10y_div_adj.json").write_text(json.dumps(rows), encoding="utf-8")


def _seed_repo(repo_root: Path, weights: dict[str, float], *, computed_at: str) -> None:
    days = _trading_days(300)
    for t in weights:
        if t not in _CASH_LIKE:
            _write_price_chart(repo_root, t, days, vol=0.02)
    weights_path = repo_root / "data" / "portfolio_weights.json"
    weights_path.parent.mkdir(parents=True, exist_ok=True)
    weights_path.write_text(
        json.dumps({"computed_at": computed_at, "weights": weights}), encoding="utf-8"
    )


def _make_db(repo_root: Path) -> Path:
    db_path = repo_root / "data" / "portfolio.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE dcf_runs ("
        "id INTEGER PRIMARY KEY, ticker TEXT, created_at TEXT, valuation_date TEXT, "
        "npv_per_share REAL, live_price REAL, live_price_at TEXT, "
        "assumption_snapshot_json TEXT)"
    )
    conn.commit()
    conn.close()
    return db_path


def test_build_export_no_weights_cache_halts(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    payload, reason = export_book_cma.build_export(tmp_path, db_path, n_paths=2_000)
    assert payload is None
    assert "no materialized weights cache" in reason


def test_build_export_insufficient_history_halts(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    weights_path = tmp_path / "data" / "portfolio_weights.json"
    weights_path.parent.mkdir(parents=True, exist_ok=True)
    weights_path.write_text(
        json.dumps({"computed_at": "2026-07-10T00:00:00", "weights": {"SGOV": 1.0}}),
        encoding="utf-8",
    )
    payload, reason = export_book_cma.build_export(tmp_path, db_path, n_paths=2_000)
    assert payload is None
    assert "not enough" in reason.lower()


def test_build_export_full_payload(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    weights = {"MELI": 0.4, "NU": 0.3, "NOW": 0.2, "SGOV": 0.1}
    _seed_repo(tmp_path, weights, computed_at="2026-07-10T11:35:02")
    payload, reason = export_book_cma.build_export(tmp_path, db_path, n_paths=5_000, seed=1)
    assert payload is not None, reason
    assert payload.weights_as_of == "2026-07-10T11:35:02"
    assert set(payload.tickers_modeled) == {"MELI", "NOW", "NU"}
    assert payload.modeled_weight_pct > 0.0
    assert payload.book_vol.simulated_normal_pct > 0.0
    assert payload.book_vol.cov_based_pct > 0.0
    assert payload.student_t_tail.pct_1st < payload.normal_tail.pct_1st
    assert payload.joint_latam_stress is not None
    assert payload.joint_latam_stress.scenario_id == "joint_latam"
    # Round-trips through the Pydantic model without drift.
    dumped = payload.model_dump_json()
    reloaded = BookCmaExport.model_validate_json(dumped)
    assert reloaded == payload


def test_cli_writes_json_and_is_idempotent(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    weights = {"MELI": 0.4, "NU": 0.3, "NOW": 0.3}
    _seed_repo(tmp_path, weights, computed_at="2026-07-10T11:35:02")
    out_path = tmp_path / "data" / "dashboard" / "book_cma.json"

    rc1 = export_book_cma.main(
        ["--repo-root", str(tmp_path), "--db-path", str(db_path), "--n-paths", "3000"]
    )
    assert rc1 == 0
    assert out_path.exists()
    first_text = out_path.read_text(encoding="utf-8")

    # Second run, same weights_as_of stamp -> idempotent no-op (file untouched).
    rc2 = export_book_cma.main(
        ["--repo-root", str(tmp_path), "--db-path", str(db_path), "--n-paths", "3000"]
    )
    assert rc2 == 0
    assert out_path.read_text(encoding="utf-8") == first_text

    # --force recomputes even with an unchanged stamp.
    rc3 = export_book_cma.main(
        [
            "--repo-root",
            str(tmp_path),
            "--db-path",
            str(db_path),
            "--n-paths",
            "3000",
            "--force",
        ]
    )
    assert rc3 == 0


def test_cli_halts_with_exit_1_when_no_weights(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    rc = export_book_cma.main(["--repo-root", str(tmp_path), "--db-path", str(db_path)])
    assert rc == 1
