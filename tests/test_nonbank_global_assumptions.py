"""Non-CAPM builders ⨯ global DCF assumptions wiring (PR3).

The platform (NU) and fintech-SOTP (SOFI) models use an opaque scalar ``ke``,
so the editable global risk-free / ERP reach them only through an *opt-in* CAPM
derivation (``derive_ke_capm``), off by default. The platform model also seeds
its tax from the global. These tests pin that behaviour + the zero-drift
defaults against a temp DB / JSON, with no touch to the real portfolio.db.
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

import build_fintech_sotp as fintech  # noqa: E402
import build_holdco_sotp as holdco  # noqa: E402
import build_nu_platform_dcf as plat  # noqa: E402

_GLOBAL_CREATE = (
    "CREATE TABLE global_dcf_assumptions "
    "(field TEXT PRIMARY KEY, value REAL NOT NULL, updated_at TEXT NOT NULL);"
)


def _repo(
    tmp_path: Path,
    *,
    global_vals: dict[str, float] | None,
    json_name: str | None = None,
    json_body: dict[str, object] | None = None,
) -> Path:
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
    if json_name is not None and json_body is not None:
        (repo / "data" / "bank_assumptions" / json_name).write_text(
            json.dumps(json_body), encoding="utf-8"
        )
    return repo


_SEED = {"risk_free_rate": 0.043, "equity_risk_premium": 0.045, "tax_rate": 0.24}


# ----------------------------------------------------------------------- NU platform


def test_platform_tax_seeds_from_global(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path, global_vals={**_SEED, "tax_rate": 0.22})
    monkeypatch.setattr(plat, "REPO", repo)
    s = plat.load_assumptions("NU")
    assert s.tax == 0.22  # unpinned tax tracks the global


def test_platform_pinned_tax_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(
        tmp_path,
        global_vals={**_SEED, "tax_rate": 0.22},
        json_name="NU_platform.json",
        json_body={"tax": 0.28},
    )
    monkeypatch.setattr(plat, "REPO", repo)
    s = plat.load_assumptions("NU")
    assert s.tax == 0.28  # pinned tax wins over the global
    assert s.ke == 0.125  # ke untouched (derive_ke_capm off)


def test_platform_derive_ke_capm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(
        tmp_path,
        global_vals={"risk_free_rate": 0.04, "equity_risk_premium": 0.05, "tax_rate": 0.24},
        json_name="NU_platform.json",
        json_body={"derive_ke_capm": 1, "beta": 1.0, "country_risk_premium": 0.02},
    )
    monkeypatch.setattr(plat, "REPO", repo)
    s = plat.load_assumptions("NU")
    assert s.ke == pytest.approx(0.04 + 1.0 * 0.05 + 0.02)  # 0.11, from the global


def test_platform_zero_drift_with_production_pins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Global == seed, NU pins ke + tax (the production NU_platform.json): the
    # discount + tax inputs reproduce the historical Assum defaults.
    repo = _repo(
        tmp_path,
        global_vals=dict(_SEED),
        json_name="NU_platform.json",
        json_body={"ke": 0.125, "tax": 0.28},
    )
    monkeypatch.setattr(plat, "REPO", repo)
    s = plat.load_assumptions("NU")
    defaults = plat.Assum()
    assert (s.ke, s.tax) == (defaults.ke, defaults.tax)


# ----------------------------------------------------------------------- fintech SOTP


def test_fintech_ke_default_no_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path, global_vals=dict(_SEED))
    monkeypatch.setattr(fintech, "REPO", repo)
    s = fintech._load("SOFI")
    assert s.ke == 0.133  # derive_ke_capm off → explicit ke unchanged


def test_fintech_derive_ke_capm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(
        tmp_path,
        global_vals={"risk_free_rate": 0.05, "equity_risk_premium": 0.06, "tax_rate": 0.24},
        json_name="SOFI_sotp.json",
        json_body={"derive_ke_capm": 1, "beta": 2.0},
    )
    monkeypatch.setattr(fintech, "REPO", repo)
    s = fintech._load("SOFI")
    assert s.ke == pytest.approx(0.05 + 2.0 * 0.06)  # 0.17, from the global


def test_fintech_capm_default_beta_reproduces_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With global == seed, opting into CAPM at the default beta=2.0 lands on the
    # explicit 0.133 — the derivation is consistent with the hardcoded scalar.
    repo = _repo(
        tmp_path,
        global_vals=dict(_SEED),
        json_name="SOFI_sotp.json",
        json_body={"derive_ke_capm": 1},
    )
    monkeypatch.setattr(fintech, "REPO", repo)
    s = fintech._load("SOFI")
    assert s.ke == pytest.approx(0.133)


# ----------------------------------------------------------------------- holdco (NAV)


def test_holdco_snapshot_records_globals_as_unused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The NAV model records the globals for transparency but flags them as not
    # affecting the valuation (ke is metadata only; carry tax is separate).
    repo = _repo(
        tmp_path,
        global_vals={"risk_free_rate": 0.05, "equity_risk_premium": 0.06, "tax_rate": 0.22},
    )
    monkeypatch.setattr(holdco, "REPO", repo)
    note = holdco._global_assumptions_note()
    assert (note["risk_free_rate"], note["equity_risk_premium"], note["tax_rate"]) == (
        0.05,
        0.06,
        0.22,
    )
    assert note["applies_to_valuation"] is False
