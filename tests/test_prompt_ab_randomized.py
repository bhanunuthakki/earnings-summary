"""Tests for the randomized prompt-improvement cycle: scaffold derivation,
the strategy bandit, multi-arm composition, and the transport/variant failure
split (meta_eval_governance.md §4, extended 2026-07-24).

No LLM call is ever made — proposals are DI'd fakes.
"""

from __future__ import annotations

import random
import sqlite3
from pathlib import Path

from llm.prompt_ab import (
    INTERACTION_NEGATIVE,
    KEEP_BASELINE,
    PROMOTE_VARIANT,
    TRANSPORT_DEGRADED,
    VARIANT_ERRORED,
    PromptArm,
    PromptEdit,
    compose,
    decide_ab,
    detect_negative_interaction,
    load_arms,
    write_arms,
)
from llm.prompt_scaffold import block_containing, derive_scaffold
from llm.prompt_signal import ab_leverage
from llm.prompt_strategies import (
    STRATEGIES,
    StrategyStat,
    draw_purpose,
    draw_strategies,
)

# --------------------------------------------------------------------------
# Scaffold derivation
# --------------------------------------------------------------------------

_INSTRUCTIONS = (
    "You are a senior analyst writing a bear case.\n"
    "Be specific and tie every risk to a named mechanic of THIS business.\n"
    "Never pad with generic macro risks.\n"
)


def _render(ticker: str, revenue: str) -> str:
    return f"{_INSTRUCTIONS}\n=== DATA ===\nTicker: {ticker}\nRevenue: {revenue}\n"


def test_scaffold_isolates_instructions_from_data() -> None:
    scaffold = derive_scaffold([_render("NU", "1.2B"), _render("MELI", "4.5B")])
    assert scaffold.eligible
    joined = "\n".join(b.text for b in scaffold.blocks)
    assert "senior analyst writing a bear case" in joined
    # The per-ticker data must NOT survive into the scaffold.
    assert "NU" not in joined and "MELI" not in joined
    assert "1.2B" not in joined and "4.5B" not in joined


def test_scaffold_blocks_are_legal_anchors() -> None:
    """Every derived block occurs exactly once in every render — the precise
    condition apply_edits enforces. This is the whole point of deriving the
    anchor space rather than validating proposals after the fact."""
    renders = [_render("NU", "1.2B"), _render("MELI", "4.5B"), _render("BN", "9.9B")]
    scaffold = derive_scaffold(renders)
    for block in scaffold.blocks:
        for render in renders:
            assert render.count(block.text) == 1


def test_single_render_is_ineligible_not_degraded() -> None:
    """One render cannot distinguish scaffold from data. That must surface as an
    explicit skip, never as a scaffold that happens to include the data."""
    scaffold = derive_scaffold([_render("NU", "1.2B")])
    assert not scaffold.eligible
    assert "renders" in scaffold.reason
    assert scaffold.blocks == ()


def test_no_shared_structure_is_ineligible() -> None:
    scaffold = derive_scaffold(["completely alpha", "utterly different beta"])
    assert not scaffold.eligible
    assert scaffold.blocks == ()


def test_block_containing_rejects_out_of_scaffold_anchor() -> None:
    scaffold = derive_scaffold([_render("NU", "1.2B"), _render("MELI", "4.5B")])
    assert block_containing(scaffold, "generic macro risks") is not None
    assert block_containing(scaffold, "Revenue: 1.2B") is None


# --------------------------------------------------------------------------
# Strategy bandit
# --------------------------------------------------------------------------


def test_exploration_floor_keeps_a_losing_strategy_reachable() -> None:
    """Thompson sampling alone drives a losing strategy's draw rate to zero at
    top-k-of-11 selection, and a strategy that is never drawn can never update
    its posterior. Since a promotion rewrites the scaffold later experiments
    edit, these statistics go stale by design — the floor is what lets a
    strategy come back."""
    stats = {s.key: StrategyStat(s.key, 0, 0) for s in STRATEGIES}
    stats["negative_examples"] = StrategyStat("negative_examples", promotes=1, keeps=60)

    without_floor = sum(
        "negative_examples"
        in {s.key for s in draw_strategies(2, stats, random.Random(f"s{i}"), epsilon=0.0)}
        for i in range(400)
    )
    with_floor = sum(
        "negative_examples" in {s.key for s in draw_strategies(2, stats, random.Random(f"s{i}"))}
        for i in range(400)
    )
    assert without_floor == 0  # the failure mode being guarded
    assert with_floor > 0


def test_draw_is_reproducible_under_a_seed() -> None:
    stats = {s.key: StrategyStat(s.key, 0, 0) for s in STRATEGIES}
    first = draw_strategies(3, stats, random.Random("seed-1"))
    second = draw_strategies(3, stats, random.Random("seed-1"))
    assert first == second
    assert len({s.key for s in first}) == 3  # distinct


def test_bandit_favours_the_winning_strategy() -> None:
    """A strategy with a strong promote record should be drawn far more often
    than one that keeps losing — while neither reaches 0 or 1."""
    stats = {s.key: StrategyStat(s.key, 0, 0) for s in STRATEGIES}
    stats["output_contract"] = StrategyStat("output_contract", promotes=30, keeps=1)
    stats["negative_examples"] = StrategyStat("negative_examples", promotes=1, keeps=30)
    winner = loser = 0
    for i in range(300):
        drawn = {s.key for s in draw_strategies(2, stats, random.Random(f"s{i}"))}
        winner += "output_contract" in drawn
        loser += "negative_examples" in drawn
    assert winner > loser * 3
    assert loser > 0  # the ε-floor keeps it reachable


def test_purpose_draw_is_weighted_and_honest_when_empty() -> None:
    assert draw_purpose({}, random.Random("x")) is None
    assert draw_purpose({"a": 0.0, "b": 0.0}, random.Random("x")) is None
    picks = {draw_purpose({"rich": 100.0, "poor": 0.01}, random.Random(f"s{i}")) for i in range(40)}
    assert "rich" in picks


def test_ab_leverage_scales_with_both_cost_and_deficit() -> None:
    assert ab_leverage(100.0, 0.0) == 100.0
    assert ab_leverage(100.0, 0.5) == 200.0
    # A cheap-but-broken purpose must not outrank an expensive-and-broken one.
    assert ab_leverage(1.0, 1.0) < ab_leverage(100.0, 0.0)


# --------------------------------------------------------------------------
# Composition + interaction
# --------------------------------------------------------------------------


def _arm(label: str, find: str, replace: str) -> PromptArm:
    return PromptArm(
        arm_label=label,
        edits=(PromptEdit(find=find, replace=replace),),
        hypothesis=f"hypothesis {label}",
        strategy_key=f"strat_{label}",
    )


def test_compose_unions_compatible_arms() -> None:
    text = "alpha line here\nbeta line here\n"
    combined = compose(
        _arm("A", "alpha line here", "ALPHA"),
        _arm("B", "beta line here", "BETA"),
        arm_label="C",
        scaffold_text=text,
    )
    assert combined is not None
    assert combined.is_composed
    assert len(combined.edits) == 2
    assert combined.composed_from == ("A", "B")
    assert combined.strategy_key == "strat_A,strat_B"


def test_compose_refuses_overlapping_anchors() -> None:
    """Two edits touching the same text would make apply_edits raise mid-splice,
    which would be recorded as an authoring failure. Caught before spend."""
    combined = compose(
        _arm("A", "the quick brown fox", "X"),
        _arm("B", "quick brown", "Y"),
        arm_label="C",
        scaffold_text="the quick brown fox\n",
    )
    assert combined is None


def test_compose_refuses_when_a_replace_destroys_the_other_anchor() -> None:
    """Non-overlapping finds can still collide: A's replacement can delete the
    text B anchors on. Only the dry-run splice catches this."""
    text = "keep alpha\nkeep beta\n"
    combined = compose(
        _arm("A", "keep alpha\nkeep beta", "collapsed"),
        _arm("B", "keep beta", "B!"),
        arm_label="C",
        scaffold_text=text,
    )
    assert combined is None


def test_negative_interaction_is_detected() -> None:
    arms = (
        _arm("A", "x", "y"),
        _arm("B", "p", "q"),
        PromptArm(
            arm_label="C",
            edits=(),
            hypothesis="combo",
            strategy_key="strat_A,strat_B",
            source="composed",
            composed_from=("A", "B"),
        ),
    )
    results = {
        "A": (PROMOTE_VARIANT, 0.80),
        "B": (PROMOTE_VARIANT, 0.70),
        "C": (PROMOTE_VARIANT, 0.55),  # worse than either component
    }
    assert detect_negative_interaction(results, arms) == {"C": INTERACTION_NEGATIVE}


def test_no_false_interaction_when_combination_is_best() -> None:
    arms = (
        _arm("A", "x", "y"),
        PromptArm("C", (), "combo", "s", "composed", ("A",)),
    )
    results = {"A": (PROMOTE_VARIANT, 0.7), "C": (PROMOTE_VARIANT, 0.9)}
    assert detect_negative_interaction(results, arms) == {}


def test_transport_degraded_component_cannot_trigger_interaction() -> None:
    """Comparing a real win rate against an outage artefact would manufacture a
    bogus INTERACTION_NEGATIVE."""
    arms = (
        _arm("A", "x", "y"),
        PromptArm("C", (), "combo", "s", "composed", ("A",)),
    )
    results = {"A": (TRANSPORT_DEGRADED, 0.0), "C": (PROMOTE_VARIANT, 0.1)}
    assert detect_negative_interaction(results, arms) == {}


# --------------------------------------------------------------------------
# Transport vs authoring failure
# --------------------------------------------------------------------------


def test_failing_baseline_reads_as_transport_not_variant_error() -> None:
    """The regression this guards: with the CLI failing platform-wide (72% in
    2026-07), the old rule blamed every variant for the outage."""
    verdict, reason = decide_ab(
        per_judge={"claude": (0, 0, 0)},
        judge_agreement=0.0,
        n_cases_attempted=12,
        n_variant_errors=9,
        n_baseline_errors=8,
    )
    assert verdict == TRANSPORT_DEGRADED
    assert "transport" in reason.lower()


def test_variant_errors_alone_still_read_as_authoring_failure() -> None:
    verdict, _ = decide_ab(
        per_judge={"claude": (0, 0, 0)},
        judge_agreement=0.0,
        n_cases_attempted=12,
        n_variant_errors=9,
        n_baseline_errors=0,
    )
    assert verdict == VARIANT_ERRORED


def test_healthy_run_is_unaffected_by_the_new_parameter() -> None:
    verdict, _ = decide_ab(
        per_judge={"claude": (9, 0, 3), "gemini": (8, 1, 3)},
        judge_agreement=0.9,
        n_cases_attempted=12,
        n_variant_errors=0,
    )
    assert verdict == PROMOTE_VARIANT

    verdict, _ = decide_ab(
        per_judge={"claude": (1, 9, 2)},
        judge_agreement=0.9,
        n_cases_attempted=12,
        n_variant_errors=0,
    )
    assert verdict == KEEP_BASELINE


# --------------------------------------------------------------------------
# Arm persistence
# --------------------------------------------------------------------------


def _db_with_arms(tmp_path: Path) -> Path:
    db_path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE prompt_experiments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, experiment_id TEXT NOT NULL UNIQUE,
            purpose TEXT NOT NULL, baseline_prompt_version TEXT NOT NULL,
            variant_label TEXT NOT NULL, hypothesis TEXT NOT NULL,
            edits_json TEXT NOT NULL, frozen_model TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'proposed', decision TEXT,
            created_at TEXT NOT NULL, decided_at TEXT, notes TEXT,
            cycle_id TEXT, rng_seed TEXT, signal_json TEXT
        );
        CREATE TABLE prompt_arms (
            id INTEGER PRIMARY KEY AUTOINCREMENT, experiment_id TEXT NOT NULL,
            arm_label TEXT NOT NULL, edits_json TEXT NOT NULL,
            hypothesis TEXT NOT NULL DEFAULT '', strategy_key TEXT,
            source TEXT NOT NULL DEFAULT 'fresh', composed_from TEXT,
            created_at TEXT NOT NULL, UNIQUE (experiment_id, arm_label)
        );
        """
    )
    conn.commit()
    conn.close()
    return db_path


def test_arms_round_trip(tmp_path: Path) -> None:
    db_path = _db_with_arms(tmp_path)
    arms = (
        _arm("A", "find-a", "rep-a"),
        PromptArm("B", (PromptEdit("find-b", "rep-b"),), "h", "s2", "composed", ("A", "X")),
    )
    write_arms(db_path, "exp1", arms)
    loaded = load_arms(db_path, "exp1")
    assert [a.arm_label for a in loaded] == ["A", "B"]
    assert loaded[1].is_composed
    assert loaded[1].composed_from == ("A", "X")
    assert loaded[0].edits[0].find == "find-a"


def test_legacy_experiment_without_arms_reads_as_one_arm(tmp_path: Path) -> None:
    """Pre-0200 experiments must still run. The fallback is distinguishable —
    arm rows present or absent — not a guess about the data's shape."""
    db_path = _db_with_arms(tmp_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO prompt_experiments (experiment_id, purpose, baseline_prompt_version, "
        "variant_label, hypothesis, edits_json, frozen_model, created_at) "
        "VALUES ('legacy', 'bear_case', 'v1', 'exp-legacy', 'old hypothesis', "
        '\'[{"find": "old", "replace": "new"}]\', \'claude-sonnet-5\', \'2026-07-01T00:00:00\')'
    )
    conn.commit()
    conn.close()
    loaded = load_arms(db_path, "legacy")
    assert len(loaded) == 1
    assert loaded[0].arm_label == "A"
    assert loaded[0].edits[0].find == "old"
