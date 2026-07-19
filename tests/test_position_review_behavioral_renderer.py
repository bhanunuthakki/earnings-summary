"""``_behavioral_rules`` becomes a renderer (tenet-2 Phase 4, docs/design/
tenet2_advisory_program.md §3.2 Tier C / §7 ruling 3).

Renderer behavior matrix:
  * zero affirmed behavioral facts -> byte-identical to the ORIGINAL frozen
    five-rule seed text (no db_path at all, or a db_path with nothing
    affirmed yet — today's default);
  * the first affirmed behavioral fact switches the whole block to live rows;
  * the canonical sell-winners-too-early key's evidence clause always
    re-interpolates the LIVE graded_sell_record line, never the snapshot
    baked into the fact at affirmation time; other affirmed facts render
    their own narrative verbatim.

The deterministic guard (apply_behavioral_guard) is untouched by any of this
— only the PROMPT TEXT goes live (out of scope here, covered by
test_position_review_graded_record.py).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from advisor import position_review  # noqa: E402
from owner_profile.store import append_fact  # noqa: E402

PRIOR_HEAD = "0059_kpi_facts_restatement"


def _cfg(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "portfolio.db"
    cfg = _cfg(db)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, "head")  # real 0159 owner_profile_facts migration runs here
    return db


def _affirm(
    db_path: Path,
    *,
    key: str,
    narrative: str,
    provenance: str = "owner",
) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        append_fact(
            conn,
            category="behavioral",
            key=key,
            value={},
            narrative=narrative,
            provenance=provenance,
            status="affirmed",
        )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Zero affirmed -> byte-identical seed fallback
# --------------------------------------------------------------------------- #


def test_no_db_path_renders_seed_verbatim() -> None:
    assert position_review.behavioral_rules_block(None) == position_review.seed_behavioral_rules(
        None
    )


def test_no_db_path_with_graded_line_renders_seed_verbatim() -> None:
    graded_line = "Graded record on sells/trims: 5 of 8 wrong (AMZN, GOOGL, MU, NVDA, TSM)"
    assert position_review.behavioral_rules_block(
        graded_line
    ) == position_review.seed_behavioral_rules(graded_line)


def test_zero_affirmed_rows_renders_seed_verbatim(db_path: Path) -> None:
    # owner_profile_facts EXISTS (post-0159) but nothing is affirmed yet —
    # today's real-world default.
    graded_line = "Graded record on sells/trims: 3 of 10 wrong (MU, NVDA, TSM)"
    rendered = position_review.behavioral_rules_block(graded_line, db_path=db_path)
    assert rendered == position_review.seed_behavioral_rules(graded_line)
    assert "1. SELL-WINNERS-TOO-EARLY is his dominant flaw" in rendered
    assert "live, owner-ratified" not in rendered


def test_only_proposed_facts_still_renders_seed(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        append_fact(
            conn,
            category="behavioral",
            key="behavior.catalyst_test_skipped",
            value={},
            narrative="You skip the catalyst test on cheap names.",
            provenance="derived",
            status="proposed",  # never affirmed
        )
        conn.commit()
    finally:
        conn.close()
    rendered = position_review.behavioral_rules_block(None, db_path=db_path)
    assert rendered == position_review.seed_behavioral_rules(None)


def test_missing_db_degrades_to_seed(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.db"
    assert position_review.behavioral_rules_block(
        None, db_path=missing
    ) == position_review.seed_behavioral_rules(None)


# --------------------------------------------------------------------------- #
# First affirmed fact -> live rows
# --------------------------------------------------------------------------- #


def test_first_affirmed_fact_switches_to_live_rows(db_path: Path) -> None:
    _affirm(
        db_path,
        key="behavior.catalyst_test_skipped",
        narrative="You skip the catalyst test on cheap names — cheap-without-catalyst is a trap.",
    )
    rendered = position_review.behavioral_rules_block(None, db_path=db_path)
    assert rendered != position_review.seed_behavioral_rules(None)
    assert "live, owner-ratified behavioral rules" in rendered
    assert "1. You skip the catalyst test on cheap names" in rendered
    # The seed's boilerplate rule text is gone — this is a full replacement,
    # not an append.
    assert "LEAP OVERLAY is his prescribed antidote" not in rendered


def test_multiple_affirmed_facts_render_in_id_order(db_path: Path) -> None:
    _affirm(db_path, key="behavior.rule_a", narrative="Rule A narrative.")
    _affirm(db_path, key="behavior.rule_b", narrative="Rule B narrative.")
    rendered = position_review.behavioral_rules_block(None, db_path=db_path)
    assert "1. Rule A narrative." in rendered
    assert "2. Rule B narrative." in rendered
    assert rendered.index("Rule A") < rendered.index("Rule B")


def test_rejected_fact_is_not_rendered(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        fact_id = append_fact(
            conn,
            category="behavioral",
            key="behavior.rule_a",
            value={},
            narrative="Rule A narrative.",
            provenance="owner",
            status="proposed",
        )
        from owner_profile.store import reject_fact

        reject_fact(conn, fact_id)
        conn.commit()
    finally:
        conn.close()
    rendered = position_review.behavioral_rules_block(None, db_path=db_path)
    assert rendered == position_review.seed_behavioral_rules(None)


# --------------------------------------------------------------------------- #
# The canonical sell-winners key: evidence always interpolates LIVE
# --------------------------------------------------------------------------- #


def test_sell_winners_key_interpolates_live_evidence_over_stale_snapshot(db_path: Path) -> None:
    # The affirmed narrative carries a STALE snapshot count baked in at
    # affirmation time — the renderer must ignore it and use the live
    # graded_line instead.
    _affirm(
        db_path,
        key=position_review.SELL_WINNERS_KEY,
        narrative="graded record: 2 of 3 cited decisions wrong (stale snapshot).",
    )
    live_line = "Graded record on sells/trims: 5 of 8 wrong (AMZN, GOOGL, MU, NVDA, TSM)"
    rendered = position_review.behavioral_rules_block(live_line, db_path=db_path)
    assert "SELL-WINNERS-TOO-EARLY is his dominant flaw" in rendered
    assert live_line in rendered
    assert "stale snapshot" not in rendered
    assert "2 of 3" not in rendered


def test_sell_winners_key_degrades_to_generic_phrase_without_graded_line(db_path: Path) -> None:
    _affirm(
        db_path,
        key=position_review.SELL_WINNERS_KEY,
        narrative="graded record: 2 of 3 cited decisions wrong.",
    )
    rendered = position_review.behavioral_rules_block(None, db_path=db_path)
    assert "his self-diagnosed sell-winners-too-early flaw" in rendered
    assert "2 of 3" not in rendered


def test_sell_winners_key_mixed_with_another_affirmed_fact(db_path: Path) -> None:
    _affirm(
        db_path,
        key="behavior.catalyst_test_skipped",
        narrative="You skip the catalyst test on cheap names.",
    )
    _affirm(
        db_path,
        key=position_review.SELL_WINNERS_KEY,
        narrative="stale narrative that must not render",
    )
    live_line = "Graded record on sells/trims: 5 of 8 wrong (AMZN, GOOGL, MU, NVDA, TSM)"
    rendered = position_review.behavioral_rules_block(live_line, db_path=db_path)
    assert "You skip the catalyst test on cheap names" in rendered
    assert "SELL-WINNERS-TOO-EARLY is his dominant flaw" in rendered
    assert live_line in rendered
    assert "stale narrative that must not render" not in rendered
