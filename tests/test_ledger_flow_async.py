"""Retained Notes logging, refresh surfaces and no-automatic-advice boundary."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import comments_server
import pytest
from flask.testing import FlaskClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def ctx(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: Callable[..., Path],
) -> tuple[FlaskClient, Path]:
    # Pin every capture-side tap deterministic: no wondering LLM, no answer
    # engine (each test patches the answer core itself).
    monkeypatch.setenv("LEDGER_RESEARCH_TAP", "0")
    monkeypatch.delenv("LEDGER_ANSWER_SYNC", raising=False)
    db = tmp_path / "data" / "portfolio.db"
    migrated_db(db)
    client = comments_server.create_app(tmp_path).test_client()
    return client, db


# ---------------------------------------------------------------------------
# Async answer tap
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# fragment=card (card-level refresh)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Console chrome merge
# ---------------------------------------------------------------------------


def test_standalone_builders_keep_their_chrome(ctx: tuple[FlaskClient, Path]) -> None:
    """The composite is the only embedded caller — the builders' own routes
    (still live for fragments/direct use) keep their tab-level chrome."""
    _client, db = ctx
    from pipeline.journal_panel import render_journal_panel
    from pipeline.ledger_panel import render_ledger_panel
    from pipeline.triage_panel import render_triage_panel

    ledger = render_ledger_panel(db)
    assert "<h2>Notes</h2>" in ledger and 'id="ledger-cap-text"' in ledger
    triage = render_triage_panel(db)
    assert "k-toolbar" in triage
    journal = render_journal_panel(db)
    assert "<h2>Journal</h2>" in journal


def test_no_window_prompt_anywhere_in_review_surfaces(ctx: tuple[FlaskClient, Path]) -> None:
    """window.prompt was banned from the ledger in PR9 — the composite console
    (feed + triage + journal) must not reintroduce it anywhere."""
    client, _db = ctx
    html = client.get("/api/panel/musings").data.decode()
    # The call form — comments referencing the ban are fine.
    assert "window.prompt(" not in html


# ---------------------------------------------------------------------------
# Stale-queue expiry
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Inline chat streams
# ---------------------------------------------------------------------------
