"""The Worldview injection (P3) — load_worldview_anchor + the compose 5th slot.

Only CURRENT Tenets ride in, flag-gated (LEDGER_WORLDVIEW_ANCHOR), dated for cache
stability, capped, degrade-safe, and spotlight-wrapped inside compose_anchor_block.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from llm.anchors import (
    WORLDVIEW_ANCHOR_CHAR_CAP,
    compose_anchor_block,
    load_worldview_anchor,
)
from synthesis.tenets import record_tenet

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0059_kpi_facts_restatement"


def _build_db(db_path: Path) -> None:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, "head")


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    _build_db(db)
    return tmp_path


def _db(repo_root: Path) -> Path:
    return repo_root / "data" / "portfolio.db"


@pytest.fixture(autouse=True)
def _flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEDGER_WORLDVIEW_ANCHOR", raising=False)


# ---------------------------------------------------------------------------
# load_worldview_anchor
# ---------------------------------------------------------------------------


def test_anchor_empty_when_flag_off(repo_root: Path) -> None:
    record_tenet(body_md="Let winning theses run.", db_path=_db(repo_root))
    assert load_worldview_anchor(repo_root) == ""  # default off ⇒ inert


def test_anchor_renders_current_tenets_when_on(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEDGER_WORLDVIEW_ANCHOR", "1")
    record_tenet(body_md="I sell my winners too early.", db_path=_db(repo_root))
    anchor = load_worldview_anchor(repo_root)
    assert "WORLDVIEW ANCHOR" in anchor
    assert "Soft priors, NOT rules" in anchor
    assert "sell my winners too early" in anchor
    assert "(since 20" in anchor  # dated for cache stability


def test_anchor_only_current_tenets(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEDGER_WORLDVIEW_ANCHOR", "1")
    record_tenet(body_md="a live belief", db_path=_db(repo_root))
    record_tenet(
        body_md="a proposed belief", status="proposed", source_note_ids=[1], db_path=_db(repo_root)
    )
    anchor = load_worldview_anchor(repo_root)
    assert "a live belief" in anchor
    assert "a proposed belief" not in anchor  # proposals never condition reasoning


def test_anchor_empty_when_no_tenets(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEDGER_WORLDVIEW_ANCHOR", "1")
    assert load_worldview_anchor(repo_root) == ""


def test_anchor_missing_db_degrades(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEDGER_WORLDVIEW_ANCHOR", "1")
    assert load_worldview_anchor(tmp_path) == ""  # no data/portfolio.db, never raises


def test_anchor_is_deterministic(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEDGER_WORLDVIEW_ANCHOR", "1")
    record_tenet(body_md="belief a", scope_key="alpha", db_path=_db(repo_root))
    record_tenet(body_md="belief b", scope_key="beta", db_path=_db(repo_root))
    # cache-stability: same inputs → byte-identical anchor across renders
    assert load_worldview_anchor(repo_root) == load_worldview_anchor(repo_root)


def test_anchor_respects_char_cap(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEDGER_WORLDVIEW_ANCHOR", "1")
    for i in range(60):
        record_tenet(
            body_md=f"belief number {i} " * 8, scope_key=f"scope-{i}", db_path=_db(repo_root)
        )
    anchor = load_worldview_anchor(repo_root)
    assert len(anchor) <= WORLDVIEW_ANCHOR_CHAR_CAP + len("\n[...truncated]")


# ---------------------------------------------------------------------------
# compose_anchor_block — the 5th slot
# ---------------------------------------------------------------------------


def test_compose_includes_worldview_slot() -> None:
    out = compose_anchor_block("THESIS_A", "BEAR_A", "", "", "WORLDVIEW_A")
    assert "THESIS_A" in out and "WORLDVIEW_A" in out


def test_compose_omits_empty_worldview() -> None:
    out = compose_anchor_block("THESIS_A", "BEAR_A")
    assert "THESIS_A" in out
    assert "WORLDVIEW" not in out  # empty slot contributes nothing


def test_compose_back_compat_four_args() -> None:
    out = compose_anchor_block("T", "B", "IR", "PRIORS")
    assert all(s in out for s in ("T", "B", "IR", "PRIORS"))


def test_compose_spotlights_the_block() -> None:
    # The worldview slot ships inside the untrusted spotlight wrap (Tenets distil
    # from captured musings) — the composed block is non-empty and carries content.
    out = compose_anchor_block("", "", "", "", "WORLDVIEW_TENETS")
    assert "WORLDVIEW_TENETS" in out
    assert out.strip() != "WORLDVIEW_TENETS"  # wrapped, not bare
