"""Phase-1 W1-4: the budget tier resolver (the one place $-caps are decided)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from research import tier

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0059_kpi_facts_restatement"


@pytest.fixture
def db_path(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    return migrated_db(tmp_path / "ledger.db", stamp=PRIOR_HEAD)


def test_hot_flag_outranks_weight_to_deep() -> None:
    t = tier.tier_for(0.0, hot_flagged=True)
    assert t.name == "deep"
    assert t.budget_usd == 2.00


def test_core_weight_is_standard() -> None:
    assert tier.tier_for(7.5, hot_flagged=False).name == "standard"


def test_small_or_unheld_is_cheap() -> None:
    assert tier.tier_for(1.0, hot_flagged=False).name == "cheap"
    assert tier.tier_for(0.0, hot_flagged=False).name == "cheap"


def test_no_tier_exceeds_the_web_ceiling() -> None:
    # S2 invariant: a resolved budget can only lower spend, never raise it.
    for weight, hot in ((0.0, True), (50.0, False), (0.0, False)):
        assert tier.tier_for(weight, hot_flagged=hot).budget_usd <= tier.WEB_CEILING_USD


def test_unknown_weight_without_repo_root_degrades_to_zero() -> None:
    assert tier.portfolio_weight_pct("NU", repo_root=None) == 0.0


def test_hot_flag_roundtrip_drives_resolve(db_path: Path) -> None:
    assert tier.is_hot_flagged("NU", db_path=db_path) is False
    tier.set_hot_flag("NU", ttl_hours=72, db_path=db_path)
    assert tier.is_hot_flagged("NU", db_path=db_path) is True
    assert tier.resolve_tier("NU", db_path=db_path, repo_root=None).name == "deep"


def test_expired_hot_flag_is_inactive(db_path: Path) -> None:
    tier.set_hot_flag("MELI", ttl_hours=-1, db_path=db_path)  # already in the past
    assert tier.is_hot_flagged("MELI", db_path=db_path) is False
    assert tier.resolve_tier("MELI", db_path=db_path, repo_root=None).name == "cheap"
