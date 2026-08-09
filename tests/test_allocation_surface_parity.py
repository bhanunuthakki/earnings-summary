"""P0.4b surface-parity exit gate (PRD §7.4: "the current artifact — same
artifact ID — renders on Today, Telegram, and Ask") + §11.4 Telegram privacy
(no book-total dollar figure).

One artifact is seeded; the Allocation console section, the Today card, the
Telegram summary text, and the Ask allocation pack item must all reference
that SAME artifact id. A distinctive "book total" figure is seeded into the
risk-snapshot substrate and asserted absent from the Telegram text — proving
the privacy boundary, not just describing it.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from allocation.recommendation_schema import IncrementalDollarRecommendation
from allocation.telegram_summary import recommendation_message_text
from ask.packs import load_packs
from pipeline.allocation_recommendation_panel import (
    render_allocation_recommendation_section,
    render_allocation_today_card,
)

_PURPOSE = "incremental_dollar_recommendation"

# A distinctive figure that must NEVER appear in the Telegram text — modeling
# a "book total" style number (net worth / total portfolio value) that would
# be a §11.4 privacy violation if it leaked into a Telegram card.
_DISTINCTIVE_BOOK_TOTAL = "1,234,567.89"


def _make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "data" / "portfolio.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
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
        CREATE TABLE portfolio_risk_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL DEFAULT 'bhanu',
            captured_at TEXT NOT NULL,
            net_worth_total REAL
        );
        """
    )
    # The "book total" style figure lives in the risk-snapshot substrate — a
    # real, live value an owner's book actually carries. telegram_summary's
    # ``recommendation_message_text`` never reads this table (its signature
    # only accepts the parsed ``IncrementalDollarRecommendation`` + an
    # artifact id) — the omission is structural, not a filter that could miss
    # a field.
    conn.execute(
        "INSERT INTO portfolio_risk_snapshots (captured_at, net_worth_total) "
        "VALUES ('2026-07-22T09:00:00', ?)",
        (float(_DISTINCTIVE_BOOK_TOTAL.replace(",", "")),),
    )
    conn.commit()
    conn.close()
    return db_path


_PAYLOAD: dict[str, object] = {
    "as_of_date": "2026-07-22",
    "input_sha": "sha_parity",
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


def _insert_artifact(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    cur = conn.execute(
        "INSERT INTO llm_artifacts (scope, purpose, content_json, input_sha256, generated_at) "
        "VALUES ('portfolio', ?, ?, 'sha', '2026-07-22T09:00:00')",
        (_PURPOSE, json.dumps(_PAYLOAD)),
    )
    conn.commit()
    artifact_id = int(cur.lastrowid or 0)
    conn.close()
    return artifact_id


def test_same_artifact_id_across_all_four_surfaces(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    artifact_id = _insert_artifact(db_path)
    marker = f'data-artifact-id="{artifact_id}"'
    telegram_marker = f"artifact #{artifact_id}"
    ask_marker = f"artifact #{artifact_id}"

    allocation_html = render_allocation_recommendation_section(db_path, tmp_path)
    today_html = render_allocation_today_card(db_path)

    rec = IncrementalDollarRecommendation.model_validate(_PAYLOAD)
    telegram_text = recommendation_message_text(rec, artifact_id)

    pack_items = load_packs(["allocation"], db_path=db_path, focus_tickers=[])

    assert marker in allocation_html, "Allocation console section missing the artifact id marker"
    assert marker in today_html, "Today card missing the artifact id marker"
    assert telegram_marker in telegram_text, "Telegram text missing the artifact id"
    assert len(pack_items) == 1
    assert ask_marker in str(pack_items[0]["text"]), "Ask pack item missing the artifact id"

    # All four surfaces agree on the SAME ticker/status too, not just the id.
    assert "NU" in allocation_html
    assert "NU" in today_html
    assert "NU" in telegram_text
    assert "NU" in str(pack_items[0]["text"])


def test_ask_allocation_pack_does_not_reuse_dirty_recommendation(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    artifact_id = _insert_artifact(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE llm_artifacts SET dirty = 1 WHERE id = ?", (artifact_id,))
    conn.commit()
    conn.close()

    pack_items = load_packs(["allocation"], db_path=db_path, focus_tickers=[])

    assert len(pack_items) == 1
    assert f"artifact #{artifact_id}" not in str(pack_items[0]["text"])
    assert "no Incremental Dollar Recommendation" in str(pack_items[0]["text"])


def test_telegram_text_omits_book_total_dollar_figure(tmp_path: Path) -> None:
    """§11.4: Telegram may show recommended PERCENTAGES and the owner-supplied
    deploy amount, never a book-total/account-balance dollar figure. The
    distinctive book-total figure is seeded into the live risk-snapshot
    substrate (``_make_db``) — ``recommendation_message_text`` structurally
    cannot leak it: its signature accepts only the parsed
    ``IncrementalDollarRecommendation`` and an artifact id, never a db_path
    or the risk snapshot."""
    _make_db(tmp_path)  # seeds portfolio_risk_snapshots.net_worth_total
    rec = IncrementalDollarRecommendation.model_validate(_PAYLOAD)
    text = recommendation_message_text(rec, artifact_id=1)

    assert _DISTINCTIVE_BOOK_TOTAL not in text
    # Percentages ARE allowed and present.
    assert "60%" in text or "5.5%" in text
    # No per-allocation dollar breakdown either (only percentages).
    assert "$6,000" not in text
    assert "$6000" not in text
