# pyright: reportPrivateUsage=false
"""Governed price resolution shared by every specialized DCF archetype."""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import build_bank_dcf as bank  # noqa: E402
import build_fintech_sotp as fintech  # noqa: E402
import build_meli_platform_dcf as meli  # noqa: E402
import build_nu_platform_dcf as nu  # noqa: E402

from dcf import specialized_price  # noqa: E402
from dcf.persist import DcfRunRow  # noqa: E402
from sources import price as price_source  # noqa: E402
from sources.price import LivePrice  # noqa: E402


def _provenance_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE dcf_runs (input_sha256 TEXT, workbook_sha256 TEXT, "
        "engine_version TEXT, inputs_as_of TEXT, provenance_json TEXT)"
    )
    return conn


def _global_store(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE global_dcf_assumptions (field TEXT PRIMARY KEY, value REAL, updated_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO global_dcf_assumptions VALUES (?,?,?)",
        (
            ("risk_free_rate", 0.04, "2026-08-25T10:00:00+00:00"),
            ("equity_risk_premium", 0.05, "2026-08-25T10:00:00+00:00"),
            ("tax_rate", 0.25, "2026-08-25T10:00:00+00:00"),
        ),
    )
    conn.commit()
    conn.close()


def test_governed_observation_wins_with_exact_clock_and_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed_at = datetime(2026, 8, 26, 3, 28, tzinfo=UTC)

    def fake_live_price(_root: Path, _ticker: str) -> LivePrice:
        return LivePrice(
            price=42.5,
            fetched_at=observed_at,
            source_name="yfinance",
            currency="USD",
        )

    monkeypatch.setattr(
        specialized_price,
        "read_live_price",
        fake_live_price,
    )

    observation = specialized_price.resolve_specialized_price(
        tmp_path,
        "NU",
        fallback_price=12.29,
        fallback_source_name="fmp_profile",
        fallback_source_path="data/historical/fmp/NU_profile.json",
    )

    assert observation.price == 42.5
    assert observation.observed_at == observed_at
    assert observation.source_name == "yfinance"
    assert observation.currency == "USD"


def test_missing_sources_preserve_seed_without_fabricating_freshness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_live_price(_root: Path, _ticker: str) -> None:
        return None

    monkeypatch.setattr(specialized_price, "read_live_price", no_live_price)

    observation = specialized_price.resolve_specialized_price(
        tmp_path,
        "NU",
        fallback_price=12.29,
        fallback_source_name="fmp_profile",
        fallback_source_path="data/historical/fmp/NU_profile.json",
    )

    assert observation.price == 12.29
    assert observation.observed_at is None
    assert observation.source_name == "fmp_profile"
    assert observation.currency is None
    assert specialized_price.price_seed_source_files(tmp_path, observation) == (
        (tmp_path / "data/historical/fmp/NU_profile.json", "market_price_seed"),
    )


def test_live_observation_excludes_superseded_fallback_file(tmp_path: Path) -> None:
    observation = specialized_price.SpecializedPriceObservation(
        price=42.5,
        observed_at=datetime(2026, 8, 26, 3, 28, tzinfo=UTC),
        source_name="yfinance",
    )

    assert specialized_price.price_seed_source_files(tmp_path, observation) == ()


def test_real_price_stack_hashes_fmp_cache_when_network_quote_misses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "data/historical/fmp/NU_profile.json"
    profile.parent.mkdir(parents=True)
    profile.write_text('[{"price":12.29,"currency":"USD"}]', encoding="utf-8")

    def network_miss(_ticker: str) -> None:
        return None

    monkeypatch.setattr(price_source, "_try_yfinance", network_miss)

    observation = specialized_price.resolve_specialized_price(
        tmp_path,
        "NU",
        fallback_price=9.0,
    )

    assert observation.source_name == "fmp_cache"
    assert observation.source_path == "data/historical/fmp/NU_profile.json"
    assert specialized_price.price_seed_source_files(tmp_path, observation) == (
        (profile, "market_price_seed"),
    )


def test_real_price_stack_rejects_nonfinite_fmp_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "data/historical/fmp/NU_profile.json"
    profile.parent.mkdir(parents=True)
    profile.write_text('[{"price":1e309,"currency":"USD"}]', encoding="utf-8")

    def network_miss(_ticker: str) -> None:
        return None

    monkeypatch.setattr(price_source, "_try_yfinance", network_miss)

    observation = specialized_price.resolve_specialized_price(
        tmp_path,
        "NU",
        fallback_price=12.29,
        fallback_source_name="model_seed",
    )

    assert observation.price == 12.29
    assert observation.source_name == "model_seed"
    assert observation.observed_at is None


def test_nonfinite_live_observation_degrades_to_valid_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def invalid_live(_root: Path, _ticker: str) -> LivePrice:
        return LivePrice(
            price=float("nan"),
            fetched_at=datetime(2026, 8, 26, tzinfo=UTC),
            source_name="yfinance",
        )

    monkeypatch.setattr(specialized_price, "read_live_price", invalid_live)

    observation = specialized_price.resolve_specialized_price(
        tmp_path,
        "NU",
        fallback_price=12.29,
    )

    assert observation.price == 12.29
    assert observation.observed_at is None


def test_valid_live_observation_wins_even_when_fallback_is_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed_at = datetime(2026, 8, 26, tzinfo=UTC)

    def valid_live(_root: Path, _ticker: str) -> LivePrice:
        return LivePrice(
            price=42.5,
            fetched_at=observed_at,
            source_name="yfinance",
            currency="USD",
        )

    monkeypatch.setattr(specialized_price, "read_live_price", valid_live)

    observation = specialized_price.resolve_specialized_price(
        tmp_path,
        "NU",
        fallback_price=0.0,
        fallback_source_name="missing_price_seed",
    )

    assert observation.price == 42.5
    assert observation.observed_at == observed_at
    assert observation.source_name == "yfinance"


def test_bank_entrypoint_uses_live_price_when_seed_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_at = datetime(2026, 8, 26, tzinfo=UTC)
    assumptions = bank.Assum()
    actuals = bank.Actuals(
        book=100.0,
        ea=200.0,
        nii=20.0,
        fees=10.0,
        opex=5.0,
        credit_cost=3.0,
        pretax=22.0,
        tax_rate=0.25,
        ni=16.5,
        equity=40.0,
        equity_prior=35.0,
        shares=10.0,
        price=0.0,
        price_seed_source="missing_price_seed",
    )
    captured: list[tuple[float, specialized_price.SpecializedPriceObservation | None]] = []

    def valid_live(_root: Path, _ticker: str) -> LivePrice:
        return LivePrice(
            price=42.5,
            fetched_at=observed_at,
            source_name="yfinance",
            currency="USD",
        )

    def load_assumptions(_ticker: str) -> tuple[bank.Assum, dict[str, object]]:
        return assumptions, {}

    def load_actuals(
        _ticker: str,
        _assumptions: bank.Assum,
        _override: dict[str, object] | None = None,
    ) -> bank.Actuals:
        return actuals

    def no_kpis(_ticker: str) -> dict[str, float]:
        return {}

    def no_breaks(_ticker: str) -> dict[str, object]:
        return {}

    def no_build(
        _actuals: bank.Actuals,
        _assumptions: bank.Assum,
        _mirror: bank.Mirror,
        _destination: Path,
        *,
        kpis: dict[str, float],
        holdings: dict[str, object],
    ) -> None:
        del kpis, holdings

    def capture_persist(
        model_actuals: bank.Actuals,
        _assumptions: bank.Assum,
        _mirror: bank.Mirror,
        price_observation: specialized_price.SpecializedPriceObservation | None,
    ) -> bool:
        captured.append((model_actuals.price, price_observation))
        return True

    monkeypatch.setattr(specialized_price, "read_live_price", valid_live)
    monkeypatch.setattr(bank, "load_assumptions", load_assumptions)
    monkeypatch.setattr(bank, "load_actuals", load_actuals)
    monkeypatch.setattr(bank, "load_kpis", no_kpis)
    monkeypatch.setattr(bank, "load_breaks", no_breaks)
    monkeypatch.setattr(bank, "build", no_build)
    monkeypatch.setattr(bank, "persist_dcf_run", capture_persist)

    assert bank.main() == 0
    assert captured[0][0] == 42.5
    assert captured[0][1] is not None
    assert captured[0][1].source_name == "yfinance"


@pytest.mark.parametrize("invalid_price", (0.0, -1.0, float("inf"), float("nan")))
def test_invalid_fallback_price_fails_at_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_price: float,
) -> None:
    def no_live_price(_root: Path, _ticker: str) -> None:
        return None

    monkeypatch.setattr(specialized_price, "read_live_price", no_live_price)

    with pytest.raises(ValueError, match="finite and positive"):
        specialized_price.resolve_specialized_price(
            tmp_path,
            "NU",
            fallback_price=invalid_price,
        )


def test_bank_persisted_provenance_includes_only_effective_price_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path
    profile = repo / "data/historical/fmp/NU_profile.json"
    profile.parent.mkdir(parents=True)
    profile.write_text('[{"price":12.29}]', encoding="utf-8")
    db = repo / "data/portfolio.db"
    db.touch()
    conn = _provenance_connection()
    captured: list[DcfRunRow] = []

    def fake_connect(
        *_args: object, **_kwargs: object
    ) -> AbstractContextManager[sqlite3.Connection]:
        return nullcontext(conn)

    def capture_upsert(_conn: sqlite3.Connection, row: DcfRunRow) -> bool:
        captured.append(row)
        return True

    monkeypatch.setattr(bank, "REPO", repo)
    monkeypatch.setattr(bank, "FMP", profile.parent)
    monkeypatch.setattr(bank, "DEST", repo / "dcf/NU.xlsx")
    monkeypatch.setattr(bank, "connect_sqlite", fake_connect)
    assert bank.persist_mod is not None
    monkeypatch.setattr(bank.persist_mod, "upsert", capture_upsert)
    actuals = bank.Actuals(
        book=100.0,
        ea=200.0,
        nii=20.0,
        fees=10.0,
        opex=5.0,
        credit_cost=3.0,
        pretax=22.0,
        tax_rate=0.25,
        ni=16.5,
        equity=40.0,
        equity_prior=35.0,
        shares=10.0,
        price=42.5,
    )
    assumptions = bank.Assum()
    mirror = bank.mirror(actuals, assumptions)
    live = specialized_price.SpecializedPriceObservation(
        price=42.5,
        observed_at=datetime(2026, 8, 26, 3, 28, tzinfo=UTC),
        source_name="yfinance",
    )
    fallback = specialized_price.SpecializedPriceObservation(
        price=12.29,
        observed_at=datetime(2026, 8, 25, 3, 28, tzinfo=UTC),
        source_name="fmp_cache",
        source_path="data/historical/fmp/NU_profile.json",
    )

    assert bank.persist_dcf_run(actuals, assumptions, mirror, live) is True
    assert bank.persist_dcf_run(actuals, assumptions, mirror, fallback) is True

    details: list[dict[str, object]] = []
    for row in captured:
        if row.provenance is not None and row.provenance.detail is not None:
            details.append(row.provenance.detail)
    assert len(details) == 2
    live_sources = cast("list[dict[str, object]]", details[0]["sources"])
    fallback_sources = cast("list[dict[str, object]]", details[1]["sources"])
    assert not any(source.get("role") == "market_price_seed" for source in live_sources)
    assert any(
        source.get("role") == "market_price_seed"
        and source.get("path") == "data/historical/fmp/NU_profile.json"
        for source in fallback_sources
    )


def test_bank_global_assumption_receipt_records_database_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "data/portfolio.db"
    db.parent.mkdir(parents=True)
    _global_store(db)
    monkeypatch.setattr(bank, "REPO", tmp_path)

    assumptions, _actuals = bank.load_assumptions("NU")

    assert assumptions.global_assumption_source["status"] == "database"
    assert assumptions.global_assumption_source["effective_fields"] == ["erp", "rf", "tax"]
    assert assumptions.global_assumption_source["influences_calculation"] is True


def test_fintech_global_assumption_receipt_records_degraded_capm_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assumptions_path = tmp_path / "data/bank_assumptions/SOFI_sotp.json"
    assumptions_path.parent.mkdir(parents=True)
    assumptions_path.write_text(json.dumps({"derive_ke_capm": 1}), encoding="utf-8")
    monkeypatch.setattr(fintech, "REPO", tmp_path)

    assumptions = fintech._load("SOFI")

    assert assumptions.global_assumption_source["status"] == "missing_database"
    assert assumptions.global_assumption_source["effective_fields"] == [
        "risk_free_rate",
        "equity_risk_premium",
    ]
    assert assumptions.global_assumption_source["influences_calculation"] is True
    assert assumptions.ke == pytest.approx(0.043 + assumptions.beta * 0.045)


def test_nu_entrypoint_threads_one_observation_through_model_and_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation = specialized_price.SpecializedPriceObservation(
        price=42.5,
        observed_at=datetime(2026, 8, 26, 3, 28, tzinfo=UTC),
        source_name="yfinance",
        currency="USD",
    )
    assumptions = nu.Assum()
    captured: list[tuple[float, specialized_price.SpecializedPriceObservation | None]] = []

    def load_assumptions(_ticker: str) -> nu.Assum:
        return assumptions

    def resolve_price(
        _root: Path,
        _ticker: str,
        *,
        fallback_price: float,
        fallback_source_name: str,
        fallback_source_path: str | None,
    ) -> specialized_price.SpecializedPriceObservation:
        del fallback_price, fallback_source_name, fallback_source_path
        return observation

    def no_holdings(_ticker: str) -> None:
        return None

    def no_build(
        _inputs: nu.Assum,
        _mirror: nu.Mirror,
        _destination: Path,
        _holdings: dict[str, object] | None = None,
    ) -> None:
        return None

    def capture_persist(
        model_inputs: nu.Assum,
        _mirror: nu.Mirror,
        _holdings: dict[str, object] | None,
        price_observation: specialized_price.SpecializedPriceObservation | None,
    ) -> bool:
        captured.append((model_inputs.price, price_observation))
        return True

    monkeypatch.setattr(nu, "load_assumptions", load_assumptions)
    monkeypatch.setattr(nu, "resolve_specialized_price", resolve_price)
    monkeypatch.setattr(nu, "_load_holdings", no_holdings)
    monkeypatch.setattr(nu, "build", no_build)
    monkeypatch.setattr(nu, "persist_dcf_run", capture_persist)

    assert nu.main() == 0
    assert captured == [(42.5, observation)]


def test_meli_entrypoint_threads_one_observation_through_model_and_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation = specialized_price.SpecializedPriceObservation(
        price=42.5,
        observed_at=datetime(2026, 8, 26, 3, 28, tzinfo=UTC),
        source_name="yfinance",
        currency="USD",
    )
    assumptions = meli.Assum(derive_capm=0)
    captured: list[tuple[float, specialized_price.SpecializedPriceObservation | None]] = []

    def load_assumptions(_ticker: str) -> meli.Assum:
        return assumptions

    def resolve_price(
        _root: Path,
        _ticker: str,
        *,
        fallback_price: float,
        fallback_source_name: str,
        fallback_source_path: str | None,
    ) -> specialized_price.SpecializedPriceObservation:
        del fallback_price, fallback_source_name, fallback_source_path
        return observation

    def no_holdings(_ticker: str) -> None:
        return None

    def no_build(
        _inputs: meli.Assum,
        _mirror: meli.Mirror,
        _destination: Path,
        _holdings: dict[str, object] | None = None,
    ) -> None:
        return None

    def capture_persist(
        model_inputs: meli.Assum,
        _mirror: meli.Mirror,
        _holdings: dict[str, object] | None,
        price_observation: specialized_price.SpecializedPriceObservation | None,
    ) -> bool:
        captured.append((model_inputs.price, price_observation))
        return True

    monkeypatch.setattr(meli, "load_assumptions", load_assumptions)
    monkeypatch.setattr(meli, "resolve_specialized_price", resolve_price)
    monkeypatch.setattr(meli, "_load_holdings", no_holdings)
    monkeypatch.setattr(meli, "build", no_build)
    monkeypatch.setattr(meli, "persist_dcf_run", capture_persist)

    assert meli.main() == 0
    assert captured == [(42.5, observation)]
