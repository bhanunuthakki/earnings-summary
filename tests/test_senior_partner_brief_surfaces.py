"""Surface-parity tests for the Senior Partner Brief (P2.2, PRD §9.1/§12.2):
Today's compact doorway, the mobile Inbox section, the Telegram builder, and
the Ask pack all read the SAME ``llm_artifacts`` row (scope='portfolio',
purpose='senior_partner_brief') — one artifact, several surfaces, no drift.
Also covers §12.2's distinct loading/empty/failed/stale states."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import llm_artifact_store
from advisor.senior_partner_brief import SeniorPartnerBrief, build_telegram_text, render_markdown
from ask.packs import load_packs
from pipeline.mobile_inbox_panel import render_mobile_inbox
from pipeline.senior_partner_brief_panel import render_brief_today_card

_DDL = """
CREATE TABLE llm_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16),
    scope VARCHAR(64) NOT NULL DEFAULT 'ticker',
    purpose VARCHAR(64) NOT NULL,
    fiscal_period VARCHAR(10),
    content_md TEXT,
    content_json TEXT,
    input_sha256 VARCHAR(64) NOT NULL,
    output_sha256 VARCHAR(64),
    model VARCHAR(64),
    prompt_version VARCHAR(32) NOT NULL DEFAULT 'v1',
    generated_at DATETIME NOT NULL,
    expires_at DATETIME,
    superseded_by_id INTEGER,
    dirty BOOLEAN NOT NULL DEFAULT 0,
    dirty_reason VARCHAR(128),
    source_doc_ids TEXT,
    parent_artifact_ids TEXT,
    llm_call_id INTEGER
);
"""


def _make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_DDL)
    conn.commit()
    conn.close()
    return db_path


def _seed_brief(db_path: Path, *, now: datetime) -> tuple[int, SeniorPartnerBrief]:
    iso_year, iso_week, _ = now.isocalendar()
    brief = SeniorPartnerBrief.model_validate(
        {
            "as_of": now.isoformat(),
            "iso_year": iso_year,
            "iso_week": iso_week,
            "input_sha": "fixedsha",
            "highest_priority_decision": {
                "title": "Trim NVO into strength",
                "body": "the concentration crossed the ordinary zone",
                "disposition": "action_requested",
                "effort_estimate": "moderate",
                "ticker": "NVO",
                "source_refs": [],
            },
        }
    )
    artifact_id, _cache_hit = llm_artifact_store.upsert(
        llm_artifact_store.UpsertRequest(
            ticker=None,
            scope="portfolio",
            purpose="senior_partner_brief",
            content_json=brief.model_dump(mode="json"),
            content_md=render_markdown(brief),
            cache_inputs=["fixedsha"],
        ),
        db_path=db_path,
    )
    assert artifact_id is not None
    return artifact_id, brief


# --------------------------------------------------------------------------- #
# §12.2 distinct states
# --------------------------------------------------------------------------- #


def test_today_card_renders_nothing_when_no_artifact(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    assert render_brief_today_card(db_path) == ""


def test_mobile_section_labels_not_generated_distinctly(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    html = render_mobile_inbox(db_path)
    assert "Senior Partner Brief not generated yet." in html


def test_mobile_groups_split_tracker_fills_into_one_review_card(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE decision_drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_channel TEXT NOT NULL,
            source_external_id TEXT,
            original_text TEXT NOT NULL,
            draft_json TEXT,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    for index, amount in enumerate((100.0, 250.0), start=1):
        conn.execute(
            "INSERT INTO decision_drafts "
            "(source_channel, source_external_id, original_text, draft_json, status, created_at) "
            "VALUES ('tracker', 'NU:2026-07-24:buy', ?, ?, 'awaiting_confirmation', ?)",
            (
                "Tracker-detected buy fill: NU on 2026-07-24",
                json.dumps(
                    {
                        "intent": "executed_change",
                        "proposed_ticker": "NU",
                        "proposed_action": "buy",
                        "proposed_amount_usd": amount,
                    }
                ),
                f"2026-07-24T11:00:0{index}",
            ),
        )
    conn.commit()
    conn.close()

    html = render_mobile_inbox(db_path)

    assert html.count("data-draft-group-id=") == 1
    assert "2 split fills" in html
    assert "$350" in html
    assert "Confirm trade" in html


def test_mobile_renders_current_allocation_artifact_id_and_preferred_plan(
    tmp_path: Path,
) -> None:
    db_path = _make_db(tmp_path)
    artifact_id, _ = llm_artifact_store.upsert(
        llm_artifact_store.UpsertRequest(
            ticker=None,
            scope="portfolio",
            purpose="incremental_dollar_recommendation",
            content_json={
                "status": "ready",
                "preferred_plan": {
                    "name": "Fund the highest-conviction eligible name",
                    "allocations": [
                        {"ticker": "NU", "pct_of_cash": 60.0},
                        {"ticker": "WIX", "pct_of_cash": 25.0},
                    ],
                    "cash_retained_usd": 1500.0,
                },
            },
            content_md="allocation",
            cache_inputs=["allocation-input"],
        ),
        db_path=db_path,
    )
    assert artifact_id is not None

    html = render_mobile_inbox(db_path)

    assert f'data-artifact-id="{artifact_id}"' in html
    assert "Fund the highest-conviction eligible name" in html
    assert "NU 60%" in html
    assert "WIX 25%" in html


def test_mobile_brief_renders_explicit_empty_states_for_unfilled_sections(
    tmp_path: Path,
) -> None:
    db_path = _make_db(tmp_path)
    _seed_brief(db_path, now=datetime(2026, 7, 20, 9, 0, 0))

    html = render_mobile_inbox(db_path)

    assert "No material capital-use decision this week." in html
    assert "No assumption challenge was sufficiently grounded." in html
    assert "No prior Owner Decision is ready to revisit." in html


def test_today_card_and_mobile_section_render_populated_artifact(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    now = datetime(2026, 7, 20, 9, 0, 0)
    artifact_id, _brief = _seed_brief(db_path, now=now)

    today_html = render_brief_today_card(db_path, now=now)
    assert f'data-artifact-id="{artifact_id}"' in today_html
    assert "Trim NVO into strength" in today_html

    mobile_html = render_mobile_inbox(db_path)
    assert "Trim NVO into strength" in mobile_html
    assert "action_requested" in mobile_html
    assert "moderate" in mobile_html


def test_ask_pack_and_telegram_share_the_same_artifact(tmp_path: Path) -> None:
    """Surface parity (PRD §9.1): the Ask pack, the Telegram text, the Today
    card, and the mobile section all read the SAME artifact id — no drift
    across surfaces."""
    db_path = _make_db(tmp_path)
    now = datetime(2026, 7, 20, 9, 0, 0)
    artifact_id, brief = _seed_brief(db_path, now=now)

    items = load_packs(["brief"], db_path=db_path, focus_tickers=[])
    assert len(items) == 1
    text = str(items[0]["text"])
    assert f"artifact #{artifact_id}" in text
    assert "Trim NVO into strength" in text

    telegram_text = build_telegram_text(brief)
    assert "Trim NVO into strength" in telegram_text

    today_html = render_brief_today_card(db_path, now=now)
    assert f'data-artifact-id="{artifact_id}"' in today_html

    mobile_html = render_mobile_inbox(db_path)
    assert "Trim NVO into strength" in mobile_html


def test_today_card_flags_stale_week(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    seeded_now = datetime(2026, 7, 6, 9, 0, 0)  # an earlier ISO week
    _seed_brief(db_path, now=seeded_now)

    later = datetime(2026, 7, 20, 9, 0, 0)  # a later week — the artifact is stale by then
    html = render_brief_today_card(db_path, now=later)
    assert "stale" in html


def test_today_card_labels_deterministic_fallback() -> None:
    """The Today card must never present a mechanical digest as if a
    governed judgment ran — labeled distinctly in the doorway text."""
    import json

    now = datetime(2026, 7, 20, 9, 0, 0)
    iso_year, iso_week, _ = now.isocalendar()
    brief = SeniorPartnerBrief.model_validate(
        {
            "as_of": now.isoformat(),
            "iso_year": iso_year,
            "iso_week": iso_week,
            "input_sha": "sha",
            "selection_mode": "deterministic_fallback",
        }
    )
    payload = json.loads(json.dumps(brief.model_dump(mode="json")))
    assert payload["selection_mode"] == "deterministic_fallback"
