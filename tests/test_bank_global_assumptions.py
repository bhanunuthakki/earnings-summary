"""Bank builder ⨯ global DCF assumptions wiring (PR2).

`build_bank_dcf.load_assumptions` seeds the editable global macro defaults
(rf / erp / tax) before applying the per-ticker bank JSON, so an unpinned name
tracks the dashboard-set global while a pinned field still wins. These tests
pin that precedence + the NU zero-drift guard against a temp DB / JSON, with no
touch to the real portfolio.db.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import build_bank_dcf as bank  # noqa: E402

_GLOBAL_CREATE = (
    "CREATE TABLE global_dcf_assumptions "
    "(field TEXT PRIMARY KEY, value REAL NOT NULL, updated_at TEXT NOT NULL);"
)


def _make_repo(
    tmp_path: Path,
    *,
    global_vals: dict[str, float] | None,
    bank_json: dict[str, object] | None,
    ticker: str = "NU",
) -> Path:
    """A throwaway REPO with data/portfolio.db (optionally carrying the global
    table) and data/bank_assumptions/<T>.json (optional)."""
    repo = tmp_path / "repo"
    (repo / "data" / "bank_assumptions").mkdir(parents=True)
    if global_vals is not None:
        conn = sqlite3.connect(str(repo / "data" / "portfolio.db"))
        try:
            conn.executescript(_GLOBAL_CREATE)
            conn.executemany(
                "INSERT INTO global_dcf_assumptions VALUES (?, ?, ?)",
                [(k, v, "t") for k, v in global_vals.items()],
            )
            conn.commit()
        finally:
            conn.close()
    if bank_json is not None:
        (repo / "data" / "bank_assumptions" / f"{ticker}.json").write_text(
            json.dumps(bank_json), encoding="utf-8"
        )
    return repo


def test_global_flows_into_unpinned_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # NU JSON pins only model params (no rf/erp/tax) → all three track the global.
    repo = _make_repo(
        tmp_path,
        global_vals={"risk_free_rate": 0.05, "equity_risk_premium": 0.06, "tax_rate": 0.22},
        bank_json={"nim_near": 0.17},
    )
    monkeypatch.setattr(bank, "REPO", repo)
    s, _ = bank.load_assumptions("NU")
    assert (s.rf, s.erp, s.tax) == (0.05, 0.06, 0.22)


def test_pinned_json_wins_over_global(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # erp/tax pinned in JSON win; rf (unpinned) still follows the global.
    repo = _make_repo(
        tmp_path,
        global_vals={"risk_free_rate": 0.05, "equity_risk_premium": 0.06, "tax_rate": 0.22},
        bank_json={"erp": 0.05, "tax": 0.257},
    )
    monkeypatch.setattr(bank, "REPO", repo)
    s, _ = bank.load_assumptions("NU")
    assert (s.rf, s.erp, s.tax) == (0.05, 0.05, 0.257)


def test_nu_zero_drift_with_seed_and_pin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Global == migration seed, NU pins erp/tax (the production NU.json): the
    # resulting cost-of-equity inputs reproduce the historical Assum defaults.
    repo = _make_repo(
        tmp_path,
        global_vals={"risk_free_rate": 0.043, "equity_risk_premium": 0.045, "tax_rate": 0.24},
        bank_json={"erp": 0.05, "tax": 0.257},
    )
    monkeypatch.setattr(bank, "REPO", repo)
    s, _ = bank.load_assumptions("NU")
    defaults = bank.Assum()
    assert (s.rf, s.erp, s.tax) == (defaults.rf, defaults.erp, defaults.tax)


def test_degrades_to_seed_without_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # No portfolio.db, no JSON → resolver returns the in-code seed defaults.
    repo = _make_repo(tmp_path, global_vals=None, bank_json=None)
    monkeypatch.setattr(bank, "REPO", repo)
    s, _ = bank.load_assumptions("NU")
    from dcf import global_assumptions as ga

    assert s.rf == ga.SEED_DEFAULTS["risk_free_rate"]
    assert s.erp == ga.SEED_DEFAULTS["equity_risk_premium"]
    assert s.tax == ga.SEED_DEFAULTS["tax_rate"]
