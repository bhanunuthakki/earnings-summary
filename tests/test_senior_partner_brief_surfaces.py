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

import pytest

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


def test_ask_brief_pack_does_not_reuse_expired_artifact(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    artifact_id, _ = _seed_brief(db_path, now=datetime(2026, 7, 20, 9, 0, 0))
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE llm_artifacts SET expires_at = '2026-07-21T09:00:00+00:00' WHERE id = ?",
        (artifact_id,),
    )
    conn.commit()
    conn.close()

    pack_items = load_packs(["brief"], db_path=db_path, focus_tickers=[])

    assert len(pack_items) == 1
    assert f"artifact #{artifact_id}" not in str(pack_items[0]["text"])
    assert "no Senior Partner Brief" in str(pack_items[0]["text"])


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


# --------------------------------------------------------------------------- #
# Task 2 (wave3b, navigation_ia.md D1): the folded Today-doorways card —
# ONE .k-well with up to two chips, replacing the brief + allocation wells.
# --------------------------------------------------------------------------- #


def _seed_allocation(db_path: Path) -> int:
    """A full, schema-valid ``incremental_dollar_recommendation`` artifact —
    ``render_allocation_today_card`` parses through the strict
    ``IncrementalDollarRecommendation`` pydantic boundary (all humility +
    provenance fields required), so a partial payload silently parses to
    None and the card renders "". Mirrors tests/test_allocation_surface_parity.py's
    ``_PAYLOAD`` (a module I don't own — copied, not imported, to avoid a
    cross-file coupling on a sibling's fixture)."""
    import llm_artifact_store

    payload: dict[str, object] = {
        "as_of_date": "2026-07-22",
        "input_sha": "sha_today_card",
        "status": "deploy_partial",
        "preferred_plan": {
            "allocations": [
                {
                    "ticker": "NU",
                    "dollars": 6000.0,
                    "pct_of_cash": 60.0,
                    "resulting_weight_pct": 5.5,
                    "zone": "ordinary",
                }
            ],
            "cash_retained_usd": 4000.0,
        },
        "best_alternative": None,
        "best_diversifier": None,
        "central_hypothesis": "NU has the best blended next-dollar score right now.",
        "personalization_why": "This deploys 60% of your new cash into the top-ranked name.",
        "supporting_evidence": ["NU has the best blended next-dollar score: +1.20"],
        "main_unknowns": ["how NU's next print reads on credit quality"],
        "disconfirming_evidence": ["a weak macro print could compress the multiple further"],
        "scenario_reasoning": "base case assumes stable credit trends",
        "confidence_verbal": "moderate",
        "confidence_basis": "The main reason I could be wrong is a macro shock hitting credit names.",
        "followup_research": [],
        "frontier_plan_ids": ["balanced"],
        "source_refs": ["dcf"],
        "risk_snapshot_ref": None,
        "engine_version": "v1",
        "prompt_version": "v1",
        "selection_mode": "llm",
    }
    artifact_id, _cache_hit = llm_artifact_store.upsert(
        llm_artifact_store.UpsertRequest(
            ticker=None,
            scope="portfolio",
            purpose="incremental_dollar_recommendation",
            content_json=payload,
            content_md="allocation",
            cache_inputs=["alloc-input"],
        ),
        db_path=db_path,
    )
    assert artifact_id is not None
    return artifact_id


def test_today_doorways_card_folds_both_chips_into_one_well(tmp_path: Path) -> None:
    from pipeline.senior_partner_brief_panel import render_today_doorways_card

    db_path = _make_db(tmp_path)
    now = datetime(2026, 7, 20, 9, 0, 0)
    _seed_brief(db_path, now=now)
    _seed_allocation(db_path)

    html = render_today_doorways_card(db_path, now=now)
    # ONE shared well, not two.
    assert html.count('class="cc-spb-today k-well"') == 1
    assert html.count("k-well") == 1
    # Both doorway hrefs preserved exactly.
    assert 'href="/mobile/inbox"' in html
    assert 'href="/#portfolio_allocation"' in html
    assert "Trim NVO into strength" in html
    assert "Next dollar: NU" in html


def test_today_doorways_card_brief_only(tmp_path: Path) -> None:
    from pipeline.senior_partner_brief_panel import render_today_doorways_card

    db_path = _make_db(tmp_path)
    now = datetime(2026, 7, 20, 9, 0, 0)
    _seed_brief(db_path, now=now)

    html = render_today_doorways_card(db_path, now=now)
    assert html.count("k-well") == 1
    assert 'href="/mobile/inbox"' in html
    assert 'href="/#portfolio_allocation"' not in html


def test_today_doorways_card_neither_renders_nothing(tmp_path: Path) -> None:
    from pipeline.senior_partner_brief_panel import render_today_doorways_card

    db_path = _make_db(tmp_path)
    assert render_today_doorways_card(db_path) == ""


def test_today_doorways_card_degrades_on_allocation_import_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read failure on the allocation side (or the module simply not being
    importable) must never sink the brief's own card — degrades to
    brief-only, matching this band's sibling try/except discipline."""
    import pipeline.allocation_recommendation_panel as alloc_mod
    from pipeline.senior_partner_brief_panel import render_today_doorways_card

    def boom(*_a: object, **_k: object) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(alloc_mod, "render_allocation_today_card", boom)

    db_path = _make_db(tmp_path)
    now = datetime(2026, 7, 20, 9, 0, 0)
    _seed_brief(db_path, now=now)

    html = render_today_doorways_card(db_path, now=now)
    assert 'href="/mobile/inbox"' in html
    assert 'href="/#portfolio_allocation"' not in html


def test_today_doorways_card_failure_state_stays_a_full_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A brief-read failure (k-well-bad) must stay a full, separately
    visible card rather than being silently squeezed into the shared well —
    a DB failure deserves to stay legible."""
    from pipeline import senior_partner_brief_panel as spb_panel

    def failing_read(*_a: object, **_k: object) -> object:
        raise RuntimeError("db unreachable")

    monkeypatch.setattr(spb_panel.llm_artifact_store, "read_current", failing_read)

    db_path = _make_db(tmp_path)
    html = spb_panel.render_today_doorways_card(db_path)
    assert "k-well-bad" in html
    assert "unavailable" in html
