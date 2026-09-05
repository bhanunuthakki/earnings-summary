from __future__ import annotations

from pathlib import Path

from quality.architecture import (
    HARD_GATES,
    SCORE_BLOCKS,
    ArchitectureMetrics,
    ArchitectureRatchetReceipt,
    ArchitectureReceipt,
    EvidenceEntry,
    ScoreEvidence,
    analyze_sources,
    architecture_regressions,
    score_quality,
)


def _sources() -> dict[str, str]:
    return {
        "src/a.py": "from b import helper\n\ndef public(value: int) -> int:\n    return helper(value)\n",
        "src/b.py": "from a import public\n\ndef helper(value: int) -> int:\n    return value + 1\n",
        "execution/comments_server.py": "def main() -> None:\n    pass\n",
        "src/pipeline/portfolio_panel.py": "def render() -> str:\n    return 'ok'\n",
    }


def _receipt(
    metrics: ArchitectureMetrics | None = None, commit: str = "abc"
) -> ArchitectureReceipt:
    return ArchitectureReceipt(
        scoped_revision="WORKTREE",
        scoped_commit=commit,
        scanner_sha256="0" * 64,
        source_sha256="1" * 64,
        python_version="3.11",
        ast_version="python-3.11",
        definitions={"x": "y"},
        metrics=metrics or analyze_sources(_sources()),
    )


def test_score_weights_total_exactly_100() -> None:
    assert sum(points for _key, _label, points in SCORE_BLOCKS) == 100


def test_architecture_scan_is_deterministic_and_resolves_cycle() -> None:
    first = analyze_sources(_sources())
    second = analyze_sources(dict(reversed(tuple(_sources().items()))))
    assert first == second
    assert first.strongly_connected_components == (("a", "b"),)
    assert first.scc_count == 1
    assert first.largest_scc == 2


def test_noncomment_loc_excludes_blank_and_comment_only_lines() -> None:
    sources = _sources()
    sources["src/a.py"] = "# comment\n\nvalue = 1  # inline\n"
    metrics = analyze_sources(sources)
    module = next(item for item in metrics.modules if item.path == "src/a.py")
    assert module.lines.physical == 3
    assert module.lines.nonblank == 2
    assert module.lines.noncomment == 1


def test_missing_evidence_is_hold_not_partial_pass() -> None:
    result = score_quality(_receipt(), evidence=None, baseline=None)
    assert result.verdict == "HOLD"
    assert result.score_points < 100
    assert "all hard-gate evidence" in result.hard_gate_missing
    assert any(block.state == "missing" for block in result.blocks)


def test_score_pass_requires_90_points_and_all_hard_gates() -> None:
    architecture = _receipt()
    architecture_keys = {
        "elegance.cycles",
        "elegance.composition_roots",
        "elegance.module_shape",
        "elegance.cohesive_typed_facades",
    }
    evidence = ScoreEvidence(
        schema_version="quality-score-evidence-v1",
        scoped_commit="abc",
        blocks={
            key: EvidenceEntry(state="pass", receipt=f".tmp/{key}.json")
            for key, _label, _points in SCORE_BLOCKS
            if key not in architecture_keys
        },
        hard_gates={key: EvidenceEntry(state="pass") for key in HARD_GATES},
    )
    result = score_quality(architecture, evidence=evidence, baseline=architecture)
    assert result.verdict == "PASS"
    assert result.score_points == 100
    assert result.score_out_of_ten == "10.0"


def test_commit_mismatch_fails_even_when_blocks_pass() -> None:
    architecture = _receipt()
    evidence = ScoreEvidence(
        schema_version="quality-score-evidence-v1",
        scoped_commit="different",
        blocks={key: EvidenceEntry(state="pass") for key, _label, _points in SCORE_BLOCKS},
        hard_gates={key: EvidenceEntry(state="pass") for key in HARD_GATES},
    )
    result = score_quality(architecture, evidence=evidence, baseline=None)
    assert result.verdict == "FAIL"
    assert "evidence commit differs from architecture commit" in result.hard_gate_failures


def test_absent_named_hard_gates_hold_the_claim() -> None:
    architecture = _receipt()
    evidence = ScoreEvidence(
        schema_version="quality-score-evidence-v1",
        scoped_commit="abc",
        blocks={key: EvidenceEntry(state="pass") for key, _label, _points in SCORE_BLOCKS},
        hard_gates={HARD_GATES[0]: EvidenceEntry(state="pass")},
    )
    result = score_quality(architecture, evidence=evidence, baseline=None)
    assert result.verdict == "HOLD"
    assert set(HARD_GATES[1:]) <= set(result.hard_gate_missing)


def test_architecture_ratchet_rejects_metric_growth() -> None:
    baseline = analyze_sources(_sources())
    current_sources = _sources()
    current_sources["src/large.py"] = "\n".join(f"x_{index} = {index}" for index in range(1002))
    current = analyze_sources(current_sources)
    regressions = architecture_regressions(current, baseline)
    assert any(item.startswith("total_noncomment_loc increased") for item in regressions)
    assert any(item.startswith("modules_over_1000_loc increased") for item in regressions)


def test_architecture_ratchet_detects_scc_member_replacement_without_order_noise() -> None:
    baseline = analyze_sources(_sources())
    reordered = baseline.model_copy(
        update={
            "strongly_connected_components": (("b", "a"),),
            "composition_root_loc": dict(baseline.composition_root_loc),
        }
    )
    assert architecture_regressions(reordered, baseline) == ()

    replacement = baseline.model_copy(update={"strongly_connected_components": (("a", "c"),)})
    regressions = architecture_regressions(replacement, baseline)
    assert "scc member set introduced: (a, c)" in regressions


def test_architecture_ratchet_catches_same_aggregate_scc_substitution() -> None:
    baseline = analyze_sources(_sources())
    replacement = baseline.model_copy(update={"strongly_connected_components": (("a", "c"),)})

    assert replacement.scc_count == baseline.scc_count
    assert replacement.scc_module_count == baseline.scc_module_count
    assert replacement.largest_scc == baseline.largest_scc
    assert "scc member set introduced: (a, c)" in architecture_regressions(replacement, baseline)


def test_architecture_ratchet_allows_scc_removal_and_strict_subset() -> None:
    baseline = analyze_sources(_sources()).model_copy(
        update={"strongly_connected_components": (("a", "b", "c"),)}
    )
    subset = baseline.model_copy(
        update={"strongly_connected_components": (("a", "b"),), "scc_module_count": 2}
    )
    assert architecture_regressions(subset, baseline) == ()

    removed = baseline.model_copy(
        update={"strongly_connected_components": (), "scc_count": 0, "scc_module_count": 0}
    )
    assert architecture_regressions(removed, baseline) == ()


def test_architecture_ratchet_compares_each_composition_root_loc() -> None:
    baseline = analyze_sources(_sources())
    unchanged = baseline.model_copy(
        update={"composition_root_loc": dict(baseline.composition_root_loc)}
    )
    assert architecture_regressions(unchanged, baseline) == ()

    grown = baseline.model_copy(
        update={
            "composition_root_loc": {
                **baseline.composition_root_loc,
                "execution/comments_server.py": baseline.composition_root_loc[
                    "execution/comments_server.py"
                ]
                + 1,
            }
        }
    )
    assert (
        "composition_root_loc[execution/comments_server.py] increased from "
        f"{baseline.composition_root_loc['execution/comments_server.py']} to "
        f"{baseline.composition_root_loc['execution/comments_server.py'] + 1}"
        in architecture_regressions(grown, baseline)
    )

    unavailable = baseline.model_copy(
        update={"composition_root_loc": {"execution/comments_server.py": -1}}
    )
    assert not any(
        item.startswith("composition_root_loc[")
        for item in architecture_regressions(unavailable, baseline)
    )


def test_architecture_ratchet_receipt_is_small_and_typed() -> None:
    receipt = ArchitectureRatchetReceipt(
        schema_version="architecture-ratchet-v1",
        status="PASS",
        scoped_commit="new",
        baseline_commit="old",
        current_source_sha256="a",
        baseline_source_sha256="b",
        regressions=(),
        scanner_mismatch=False,
    )
    assert receipt.model_dump()["status"] == "PASS"


def test_no_checkout_database_is_needed(tmp_path: Path) -> None:
    metrics = analyze_sources(_sources())
    assert metrics.executable_modules == 4
    assert not list(tmp_path.glob("*.db"))
