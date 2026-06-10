"""Tests for the analyst-priors anchor (D2): load_priors_anchor, the 4-block
compose, and the chat system prompt's memory sections (priors + prior-build
thread tail).

DB-backed tests migrate a tmp SQLite at <repo>/data/portfolio.db (stamp 0072
→ head) because load_priors_anchor derives the DB from repo_root exactly like
the comments-server does.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import chat_session  # noqa: E402
from llm.anchors import (  # noqa: E402
    PRIORS_ANCHOR_CHAR_CAP,
    compose_anchor_block,
    load_priors_anchor,
)
from user_state import notes  # noqa: E402

PRIOR_HEAD = "0072_kpi_reporting_cadence"
RD = date(2026, 6, 1)


def _migrate(db: Path) -> None:
    db.parent.mkdir(parents=True, exist_ok=True)
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, "head")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo root with a migrated DB at data/portfolio.db."""
    root = tmp_path / "repo"
    _migrate(root / "data" / "portfolio.db")
    return root


def _db(repo: Path) -> Path:
    return repo / "data" / "portfolio.db"


# ----------------------------------------------------------------------------
# load_priors_anchor
# ----------------------------------------------------------------------------


def test_no_db_returns_empty(tmp_path: Path) -> None:
    assert load_priors_anchor(tmp_path / "nowhere", "NU") == ""


def test_no_open_notes_returns_empty(repo: Path) -> None:
    assert load_priors_anchor(repo, "NU") == ""
    n = notes.create_note(ticker="NU", kind="watch", body="resolved later", db_path=_db(repo))
    notes.resolve_note(n.id, db_path=_db(repo))
    assert load_priors_anchor(repo, "NU") == ""


def test_groups_kinds_in_order_and_excludes_non_open(repo: Path) -> None:
    db = _db(repo)
    notes.create_note(
        ticker="NU",
        kind="question",
        body="What's driving NPL seasonality?",
        anchor_type="kpi_ledger_row",
        anchor_key="NPL ratio",
        db_path=db,
    )
    notes.create_note(ticker="NU", kind="watch", body="Mexico deposit costs", db_path=db)
    notes.create_note(
        ticker="NU",
        kind="assumption",
        body="Selic glides below 12%",
        anchor_type="free_text",
        anchor_key="some highlighted text",
        db_path=db,
    )
    resolved = notes.create_note(ticker="NU", kind="question", body="ANSWERED", db_path=db)
    notes.resolve_note(resolved.id, db_path=db)
    other = notes.create_note(ticker="MELI", kind="watch", body="take-rate ceiling", db_path=db)

    anchor = load_priors_anchor(repo, "NU")
    assert anchor.startswith("## ANALYST PRIORS")
    assert "What's driving NPL seasonality?" in anchor
    assert "[NPL ratio]" in anchor  # structured anchor key surfaces
    assert "some highlighted text" not in anchor  # free_text keys don't
    assert "ANSWERED" not in anchor  # resolved excluded
    assert "take-rate ceiling" not in anchor  # other ticker excluded
    assert "(since 20" in anchor  # stable creation DATE, not a relative age

    # Kind groups render in actionable-first order.
    q = anchor.index("Open questions")
    w = anchor.index("Watch-items")
    a = anchor.index("Assumptions")
    assert q < w < a

    del other


def test_anchor_is_cache_stable(repo: Path) -> None:
    notes.create_note(ticker="NU", kind="watch", body="stable rendering", db_path=_db(repo))
    assert load_priors_anchor(repo, "NU") == load_priors_anchor(repo, "NU")


def test_long_bodies_clip_and_block_caps(repo: Path) -> None:
    db = _db(repo)
    for i in range(15):
        notes.create_note(
            ticker="NU", kind="observation", body=f"note {i}: " + "x" * 400, db_path=db
        )
    anchor = load_priors_anchor(repo, "NU")
    assert len(anchor) <= PRIORS_ANCHOR_CHAR_CAP + len("\n[...truncated]")
    assert anchor.endswith("[...truncated]")
    assert "..." in anchor  # per-line clip marker


# ----------------------------------------------------------------------------
# compose_anchor_block back-compat + 4th block
# ----------------------------------------------------------------------------


def test_compose_backcompat_and_priors() -> None:
    assert compose_anchor_block("T", "B") == "T\n\n---\n\nB\n\n---\n\n"
    assert compose_anchor_block("", "", "", "") == ""
    only_priors = compose_anchor_block("", "", "", "P")
    assert only_priors == "P\n\n---\n\n"
    full = compose_anchor_block("T", "B", "I", "P")
    assert full.index("T") < full.index("B") < full.index("I") < full.index("P")


# ----------------------------------------------------------------------------
# chat memory sections
# ----------------------------------------------------------------------------


def test_system_prompt_includes_priors_and_prior_thread(repo: Path) -> None:
    notes.create_note(
        ticker="NU", kind="question", body="Is FX hedging cost rising?", db_path=_db(repo)
    )
    chat_session.save_thread(
        repo,
        "NU",
        date(2026, 5, 20),
        [
            chat_session.ChatTurn(role="user", text="What did we conclude on NPLs?"),
            chat_session.ChatTurn(role="assistant", text="NPL formation is seasonal; watch 90+."),
        ],
    )
    prompt = chat_session._system_prompt(repo, "NU", RD)  # pyright: ignore[reportPrivateUsage]
    assert "## ANALYST PRIORS" in prompt
    assert "Is FX hedging cost rising?" in prompt
    assert "PRIOR DISCUSSION (your chat on the 2026-05-20 report" in prompt
    assert "What did we conclude on NPLs?" in prompt


def test_prior_thread_picks_latest_earlier_build_only(repo: Path) -> None:
    for d, marker in (
        (date(2026, 5, 1), "OLDEST"),
        (date(2026, 5, 20), "LATEST_EARLIER"),
        (RD, "CURRENT"),
        (date(2026, 6, 9), "FUTURE"),
    ):
        chat_session.save_thread(
            repo, "NU", d, [chat_session.ChatTurn(role="user", text=f"{marker} turn")]
        )
    block = chat_session._prior_threads_context(repo, "NU", RD)  # pyright: ignore[reportPrivateUsage]
    assert "LATEST_EARLIER" in block
    for absent in ("OLDEST", "CURRENT", "FUTURE"):
        assert absent not in block


def test_system_prompt_degrades_without_db_or_chats(tmp_path: Path) -> None:
    bare = tmp_path / "bare_repo"
    prompt = chat_session._system_prompt(bare, "NU", RD)  # pyright: ignore[reportPrivateUsage]
    assert "ANALYST PRIORS" not in prompt
    assert "PRIOR DISCUSSION" not in prompt
    assert "analyst assistant for NU" in prompt
